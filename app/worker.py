import json
import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select

from .config import Settings
from .connectors import Integrations
from .db import Database, active_job, actor, audit, jobs, leases, lock_release, messages, now, promotions, releases, release_closures, uid
from .mail import deliver, poll_replies
from .service import enqueue

log = logging.getLogger('release.worker')
SAFE_JOBS = ('servicenow', 'jira', 'github', 'jenkins')


def schedule_syncs(db, settings):
    if settings.demo or settings.auto_sync_seconds <= 0:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=settings.auto_sync_seconds)).isoformat()
    for release in db.rows(releases):
        with db.connect() as conn:
            lock_release(conn, release['id'], allow_completed=True)
            if conn.execute(select(release_closures.c.id).where(release_closures.c.release_id == release['id'])).first():
                continue
            upcoming = list(conn.execute(select(promotions.c.to_env).where(promotions.c.release_id == release['id'],
                                                                         promotions.c.status == 'prepared')).scalars())
            for source, enabled in [('servicenow', settings.sn_url and settings.sn_env_field and release['change_number']),
                                    ('jira', settings.jira_url and release['jira_jql'])]:
                if not enabled:
                    continue
                recent = list(conn.execute(db.limit(select(jobs).where(jobs.c.release_id == release['id'], jobs.c.kind == source)
                                      .order_by(jobs.c.created_at.desc()), 50)).mappings())
                environments = list(dict.fromkeys([release['current_env']] + upcoming)) if source == 'servicenow' else [release['current_env']]
                for environment in environments:
                    latest = next((job for job in recent if source == 'jira' or json.loads(job['payload']).get('environment') == environment), None)
                    if latest and (latest['created_at'] > cutoff or latest['status'] in ('pending', 'running', 'retry')):
                        continue
                    enqueue(conn, release['id'], source, {'source': source, 'environment': environment})


def process_one(db, settings, integrations):
    epoch = int(time.time())
    # Repeating a read-only sync is safe; external sends are handled separately.
    with db.connect() as conn:
        conn.execute(jobs.update().where(jobs.c.status == 'running', jobs.c.lease_until < epoch,
                     jobs.c.kind.in_(SAFE_JOBS)).values(status='retry', available_at=epoch, updated_at=now()))
        conn.execute(jobs.update().where(jobs.c.status == 'running', jobs.c.lease_until < epoch,
                     jobs.c.kind.in_(['export_task', 'export_promotion']))
                     .values(status='uncertain', error='Worker lease expired during an external write; reconcile before retry.', updated_at=now()))
    candidates = db.all(db.limit(select(jobs).where(jobs.c.status.in_(['pending', 'retry']), jobs.c.available_at <= epoch)
                        .order_by(jobs.c.created_at), 10))
    for item in candidates:
        with db.connect() as conn:
            claimed = conn.execute(jobs.update().where(jobs.c.id == item['id'], jobs.c.status == item['status'],
                                   jobs.c.attempts == item['attempts'])
                                   .values(status='running', attempts=item['attempts'] + 1, lease_until=epoch + 120,
                                           updated_at=now())).rowcount
        if claimed != 1:
            continue
        renewal_stop = threading.Event()
        def renew_claim():
            while not renewal_stop.wait(30):
                try:
                    with db.connect() as conn:
                        changed = conn.execute(jobs.update().where(jobs.c.id == item['id'], jobs.c.status == 'running',
                                    jobs.c.attempts == item['attempts'] + 1).values(lease_until=int(time.time()) + 120)).rowcount
                        if changed != 1:
                            return
                except Exception:
                    log.warning('job_lease_renewal_failed id=%s', item['id'])
        renewal = threading.Thread(target=renew_claim, daemon=True)
        renewal.start()
        claim_token = active_job.set((item['id'], item['attempts'] + 1))
        try:
            handler = getattr(integrations, 'sync_' + item['kind'])
            handler(item['release_id'], json.loads(item['payload']))
            result, error, available = 'succeeded', '', epoch
        except Exception as exc:
            # Source URLs, tokens and raw exceptions never enter application logs.
            log.warning('integration_job_failed id=%s kind=%s error_type=%s', item['id'], item['kind'], type(exc).__name__)
            error = exc.detail if isinstance(exc, HTTPException) else str(exc) if type(exc).__name__ == 'ConnectorError' else 'Integration failed; inspect source configuration and server diagnostics.'
            result = 'uncertain' if item['kind'].startswith('export_') else 'failed' if item['attempts'] >= 2 else 'retry'
            available = epoch + 30 * (2 ** item['attempts'])
        finally:
            active_job.reset(claim_token)
            renewal_stop.set()
            renewal.join(timeout=15)
        with db.connect() as conn:
            # An expired worker cannot overwrite a more recent worker's result.
            conn.execute(jobs.update().where(jobs.c.id == item['id'], jobs.c.status == 'running',
                         jobs.c.attempts == item['attempts'] + 1)
                         .values(status=result, error=error[:500], available_at=available, updated_at=now()))
            audit(conn, item['release_id'], 'job.' + result, {'job_id': item['id'], 'kind': item['kind']})
        return True
    return False


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings = Settings()
    db = Database(settings.database_url, settings.db_path, initialize=settings.demo)
    integrations = Integrations(settings, db)
    owner, stop = uid(), threading.Event()
    actor.set('integration-worker:' + owner)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    def heartbeat():
        while not stop.is_set():
            try:
                with db.connect() as conn:
                    existing = conn.execute(select(leases.c.id).where(leases.c.id == 'worker:' + owner)).first()
                    if existing:
                        conn.execute(leases.update().where(leases.c.id == 'worker:' + owner).values(expires_at=int(time.time()) + 180))
                    else:
                        conn.execute(leases.insert().values(id='worker:' + owner, owner=owner, expires_at=int(time.time()) + 180))
            except Exception:
                log.warning('worker_heartbeat_database_unavailable')
            stop.wait(30)
    pulse = threading.Thread(target=heartbeat, name='worker-heartbeat', daemon=True)
    pulse.start()
    while not stop.is_set():
        try:
            schedule_syncs(db, settings)
            process_one(db, settings, integrations)
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            with db.connect() as conn:
                conn.execute(messages.update().where(messages.c.status == 'sending', messages.c.sent_at < cutoff)
                             .values(status='uncertain', error='Worker interrupted; reconcile mail delivery before resending.'))
            if settings.external_writes:
                for message in db.all(db.limit(select(messages).where(messages.c.status == 'queued'), 10)):
                    if stop.is_set():
                        break
                    deliver(db, settings, message)
            if settings.imap_host:
                poll_replies(db, settings, owner)
        except Exception as exc:
            log.error('worker_cycle_failed error_type=%s', type(exc).__name__)
        stop.wait(10)
    integrations.close()
    pulse.join(timeout=15)
    db.engine.dispose()


if __name__ == '__main__':
    main()
