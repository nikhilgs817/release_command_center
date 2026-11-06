import hashlib
import imaplib
import re
import smtplib
import ssl
import time
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr

from sqlalchemy import select

from .db import add, audit, leases, mail_cursors, messages, now, uid
from .service import accept_reply


def deliver(db, settings, item):
    if not settings.external_writes:
        return False
    with db.connect() as conn:
        claimed = conn.execute(messages.update().where(messages.c.id == item['id'], messages.c.status == 'queued')
                               .values(status='sending', sent_at=now())).rowcount
    if claimed != 1:
        return False
    try:
        if not all((settings.smtp_host, settings.mail_from, settings.mail_user, settings.mail_password, item['recipient'])):
            raise ValueError('Mail configuration or stakeholder address is missing.')
        message = EmailMessage()
        message['From'], message['To'] = settings.mail_from, item['recipient']
        message['Subject'], message['Message-ID'] = item['subject'], item['message_id']
        message.set_content(item['body'])
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(settings.mail_user, settings.mail_password)
            refused = smtp.send_message(message)
            if refused:
                raise RuntimeError('Recipient was refused.')
        status, error = 'sent', ''
    except Exception:
        # SMTP has no idempotency guarantee. A timeout may follow delivery.
        status, error = 'uncertain', 'Mail outcome requires operator reconciliation; no automatic resend.'
    with db.connect() as conn:
        conn.execute(messages.update().where(messages.c.id == item['id'], messages.c.status == 'sending')
                     .values(status=status, error=error))
        audit(conn, item['release_id'], 'email.' + status, {'message_id': item['id']})
    return status == 'sent'


def acquire_mail_lease(db, owner):
    epoch = int(time.time())
    with db.connect() as conn:
        return conn.execute(leases.update().where(leases.c.id == 'imap', leases.c.expires_at <= epoch)
                             .values(owner=owner, expires_at=epoch + 90)).rowcount == 1


def poll_replies(db, settings, owner):
    if not settings.imap_host or not acquire_mail_lease(db, owner):
        return 0
    count = 0
    mailbox = hashlib.sha256(f'{settings.imap_host}/{settings.mail_user}/{settings.imap_folder}'.encode()).hexdigest()
    try:
        with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, ssl_context=ssl.create_default_context(), timeout=20) as imap:
            imap.login(settings.mail_user, settings.mail_password)
            status, _ = imap.select(settings.imap_folder, readonly=True)
            if status != 'OK':
                raise RuntimeError('Cannot open IMAP folder.')
            validity_response = imap.response('UIDVALIDITY')[1]
            if not validity_response or not validity_response[0]:
                raise RuntimeError('Mailbox did not return UIDVALIDITY.')
            validity = validity_response[0].decode()
            cursor = db.one(select(mail_cursors).where(mail_cursors.c.mailbox == mailbox))
            last = cursor['last_uid'] if cursor and cursor['uidvalidity'] == validity else 0
            status, data = imap.uid('search', None, f'UID {last + 1}:*')
            if status != 'OK':
                raise RuntimeError('IMAP UID search failed.')
            identifiers = [n for n in data[0].split() if int(n) > last][:50]
            for identifier in identifiers:
                with db.connect() as conn:
                    held = conn.execute(leases.update().where(leases.c.id == 'imap', leases.c.owner == owner,
                                        leases.c.expires_at > int(time.time())).values(expires_at=int(time.time()) + 90)).rowcount
                if held != 1:
                    raise RuntimeError('Mailbox lease expired; another worker may have taken ownership.')
                status, data = imap.uid('fetch', identifier, '(BODY.PEEK[])')
                if status != 'OK':
                    raise RuntimeError('IMAP fetch failed; cursor was not advanced.')
                raw = next((p[1] for p in data if isinstance(p, tuple)), None)
                if raw is None:
                    raise RuntimeError('IMAP message payload is missing.')
                mail = BytesParser(policy=policy.default).parsebytes(raw)
                sender = parseaddr(str(mail.get('From', '')))[1].lower()
                part = mail.get_body(preferencelist=('plain',)) if mail.is_multipart() else mail
                # Keep HTML-only messages as inert text; never render mailbox HTML.
                text_body = str(part.get_content())[:100000] if part else '[No plain text body; review in company mailbox.]'
                external_id = str(mail.get('Message-ID', ''))[:255] or hashlib.sha256(raw).hexdigest()
                references = re.findall(r'<[^>]+>', str(mail.get('In-Reply-To', '')) + ' ' + str(mail.get('References', '')))
                count += int(accept_reply(db, external_id, sender, text_body, references, str(mail.get('Subject', ''))))
                with db.connect() as conn:
                    existing = conn.execute(select(mail_cursors.c.id).where(mail_cursors.c.mailbox == mailbox)).scalar()
                    if existing:
                        conn.execute(mail_cursors.update().where(mail_cursors.c.id == existing)
                                     .values(uidvalidity=validity, last_uid=int(identifier)))
                    else:
                        add(conn, mail_cursors, mailbox=mailbox, uidvalidity=validity, last_uid=int(identifier))
    finally:
        with db.connect() as conn:
            conn.execute(leases.update().where(leases.c.id == 'imap', leases.c.owner == owner).values(expires_at=0))
    return count
