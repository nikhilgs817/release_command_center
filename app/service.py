import json
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from .config import ENVIRONMENTS
from .db import (activities, add, audit, audit_log, commits, defects, deployments, guard_job,
                 jobs, lock_release, messages, notes, now, promotions, releases, replies, uid, release_closures)


def require_release(db, release_id):
    item = db.get(releases, release_id)
    if not item:
        raise HTTPException(404, 'Release not found.')
    return item


def enqueue(conn, release_id, kind, payload):
    return add(conn, jobs, release_id=release_id, kind=kind, payload=json.dumps(payload),
               status='pending', attempts=0, available_at=0, lease_until=0, error='',
               created_at=now(), updated_at=now())


def snapshot(db, release_id):
    from .catalog import describe
    release = describe(db, require_release(db, release_id))
    activity_rows = db.rows(activities, release_id=release_id)
    all_notes = db.all(select(notes).join(activities, notes.c.activity_id == activities.c.id)
                      .where(activities.c.release_id == release_id).order_by(notes.c.created_at))
    notes_by_activity = {}
    for note in all_notes:
        notes_by_activity.setdefault(note['activity_id'], []).append(note)
    for activity in activity_rows:
        activity['notes'] = notes_by_activity.get(activity['id'], [])
    message_rows = db.rows(messages, release_id=release_id)
    all_replies = db.all(select(replies).join(messages, replies.c.message_id == messages.c.id)
                        .where(messages.c.release_id == release_id).order_by(replies.c.received_at))
    replies_by_message = {}
    for reply in all_replies:
        replies_by_message.setdefault(reply['message_id'], []).append(reply)
    for message in message_rows:
        message['replies'] = replies_by_message.get(message['id'], [])
    commit_rows = db.rows(commits, release_id=release_id)
    deployment_rows = db.rows(deployments, release_id=release_id)
    for row in commit_rows + deployment_rows:
        row['issue_keys'] = json.loads(row['issue_keys'])
    defect_rows = db.rows(defects, release_id=release_id)
    for defect in defect_rows:
        defect['commits'] = [c for c in commit_rows if defect['issue_key'] in c['issue_keys']]
        defect['deployments'] = [d for d in deployment_rows if defect['issue_key'] in d['issue_keys']]
        defect['environments'] = sorted({d['environment'] for d in defect['deployments']
                                          if d['result'] == 'SUCCESS' and d['deployed']})
    current = [a for a in activity_rows if a['environment'] == release['current_env']]
    blockers = [f"{a['number'] or a['title']}: {a['status']}" for a in current if a['status'] != 'complete']
    if not current:
        blockers.append('No activities have been recorded for this environment.')
    index = ENVIRONMENTS.index(release['current_env'])
    next_env = ENVIRONMENTS[index + 1] if index < len(ENVIRONMENTS) - 1 else None
    if not next_env:
        blockers.append('This release has reached the final environment.')
    if release['status'] == 'completed':
        blockers.append('This release is completed and read-only.')
    return {'release': release, 'activities': activity_rows, 'defects': defect_rows,
            'commits': commit_rows, 'deployments': deployment_rows, 'messages': message_rows,
            'promotions': db.rows(promotions, release_id=release_id),
            'jobs': db.rows(jobs, release_id=release_id),
            'audit': db.all(db.limit(select(audit_log).where(audit_log.c.release_id == release_id)
                            .order_by(audit_log.c.created_at.desc()), 100)),
            'readiness': {'next_environment': next_env, 'can_prepare': not blockers, 'blockers': blockers}}


def prepare(db, release_id, auto_queue_email=False):
    with db.connect() as conn:
        if not lock_release(conn, release_id):
            raise HTTPException(404, 'Release not found.')
        release = dict(conn.execute(select(releases).where(releases.c.id == release_id)).mappings().one())
        index = ENVIRONMENTS.index(release['current_env'])
        if index == len(ENVIRONMENTS) - 1:
            raise HTTPException(409, 'Release is already in PROD.')
        next_env = ENVIRONMENTS[index + 1]
        existing = conn.execute(select(promotions).where(promotions.c.release_id == release_id,
                    promotions.c.from_env == release['current_env'], promotions.c.to_env == next_env)).mappings().first()
        if existing:
            return dict(existing)
        source = list(conn.execute(select(activities).where(activities.c.release_id == release_id,
                           activities.c.environment == release['current_env'])).mappings())
        if not source or any(a['status'] != 'complete' for a in source):
            raise HTTPException(409, 'Complete all current-environment activities before preparing the next environment.')
        histories = {item['id']: list(conn.execute(select(notes.c.body).where(notes.c.activity_id == item['id'])).scalars())
                     for item in source}
        summary_lines = [f"Release: {release['name']} ({release['version']})", f"Change: {release['change_number']}",
                         f"Promotion: {release['current_env']} -> {next_env}", '', 'COMPLETED ACTIVITIES AND STAKEHOLDERS']
        for item in source:
            summary_lines.extend([f"\n{item['number'] or item['id']}: {item['title']}",
                                  f"Stakeholder: {item['owner']} <{item['email']}>",
                                  'Execution notes: ' + ('\n'.join(histories[item['id']]) or 'No notes recorded.')])
        promotion_id = add(conn, promotions, release_id=release_id, from_env=release['current_env'],
                           to_env=next_env, status='prepared', summary='\n'.join(summary_lines), export_status='draft',
                           service_task_id='', service_task_number='', created_at=now())
        for item in source:
            task_id = add(conn, activities, release_id=release_id, environment=next_env, number='', external_id=None,
                          title=item['title'], category=item['category'], owner=item['owner'], email=item['email'],
                          status='pending', source='local', copied_from=item['id'], updated_at=now())
            history = histories[item['id']]
            correlation = uid()
            summary = '\n'.join(history) or 'No execution notes recorded.'
            body = (f"Hello {item['owner'] or 'team'},\n\nPlease prepare {item['title']} for {next_env}.\n"
                    f"Release: {release['name']} ({release['version']})\nChange: {release['change_number']}\n"
                    f"Previous task: {item['number'] or item['id']} in {release['current_env']}\n\n"
                    f"Previous execution notes:\n{summary}\n\nPlease reply with readiness, timing, and any blockers.\n"
                    f"Tracking reference: [RCC:{correlation}]\n")
            add(conn, messages, release_id=release_id, promotion_id=promotion_id, activity_id=task_id,
                recipient=item['email'], subject=f"[RCC:{correlation}] {release['version']} / {next_env} / {item['title']}",
                body=body, correlation=correlation, message_id=f'<rcc.{correlation}@release-command-center.invalid>',
                status='queued' if auto_queue_email and item['email'] else 'draft', decision='pending', created_at=now(), sent_at=None, error='')
        audit(conn, release_id, 'promotion.prepared', {'from': release['current_env'], 'to': next_env, 'tasks': len(source)})
        return {'id': promotion_id, 'from_env': release['current_env'], 'to_env': next_env, 'status': 'prepared'}


def finalize(db, release_id, promotion_id, require_source_tickets=False, source_max_age_seconds=900):
    with db.connect() as conn:
        lock_release(conn, release_id)
        promotion = conn.execute(select(promotions).where(promotions.c.id == promotion_id,
                                                           promotions.c.release_id == release_id)).mappings().first()
        if not promotion:
            raise HTTPException(404, 'Promotion not found.')
        if promotion['status'] == 'completed':
            return {'status': 'completed'}
        release = conn.execute(select(releases).where(releases.c.id == release_id)).mappings().one()
        if release['current_env'] != promotion['from_env']:
            raise HTTPException(409, 'Release environment changed. Refresh the promotion plan.')
        previous = list(conn.execute(select(activities).where(activities.c.release_id == release_id,
                                      activities.c.environment == promotion['from_env'])).mappings())
        if not previous or any(t['status'] != 'complete' for t in previous):
            raise HTTPException(409, 'A previous-environment activity has reopened; resolve it before completing this promotion.')
        tasks = list(conn.execute(select(activities).where(activities.c.release_id == release_id,
                                  activities.c.environment == promotion['to_env'])).mappings())
        approvals = list(conn.execute(select(messages).where(messages.c.promotion_id == promotion_id)).mappings())
        evidence = conn.execute(select(deployments.c.id).where(deployments.c.release_id == release_id,
                               deployments.c.environment == promotion['to_env'], deployments.c.result == 'SUCCESS',
                               deployments.c.deployed == 1)).first()
        if not tasks or any(t['status'] != 'complete' for t in tasks):
            raise HTTPException(409, 'Target-environment activities are incomplete.')
        if require_source_tickets and (not promotion['service_task_id'] or
                any(t['source'] != 'servicenow' or not t['external_id'] for t in tasks + previous)):
            raise HTTPException(409, 'Live promotion requires the ServiceNow release task and authoritative activity tickets.')
        if require_source_tickets and any((datetime.now(timezone.utc) - datetime.fromisoformat(t['updated_at'])).total_seconds()
                                          > source_max_age_seconds for t in tasks + previous):
            raise HTTPException(409, 'ServiceNow activity status is stale. Sync both environments before completing promotion.')
        if not approvals or any(m['decision'] != 'approved' for m in approvals):
            raise HTTPException(409, 'Stakeholder responses still need explicit operator review and approval.')
        if not evidence:
            raise HTTPException(409, 'No successful confirmed deployment evidence for the target environment.')
        expected_keys = set(conn.execute(select(defects.c.issue_key).where(defects.c.release_id == release_id)).scalars())
        manifests = list(conn.execute(select(deployments.c.issue_keys).where(deployments.c.release_id == release_id,
                         deployments.c.environment == promotion['to_env'], deployments.c.result == 'SUCCESS',
                         deployments.c.deployed == 1)).scalars())
        deployed_keys = {key for manifest in manifests for key in json.loads(manifest)}
        if expected_keys - deployed_keys:
            raise HTTPException(409, 'Deployment evidence does not cover every Jira issue in the release scope.')
        conn.execute(releases.update().where(releases.c.id == release_id).values(current_env=promotion['to_env']))
        conn.execute(promotions.update().where(promotions.c.id == promotion_id).values(status='completed'))
        audit(conn, release_id, 'promotion.completed', {'to': promotion['to_env'], 'promotion_id': promotion_id})
    return {'status': 'completed'}


def record_deployment(db, release_id, data):
    require_release(db, release_id)
    keys = sorted(set(data['issue_keys']))
    unknown = set(keys) - {d['issue_key'] for d in db.rows(defects, release_id=release_id)}
    if unknown:
        raise HTTPException(409, f"Sync Jira first. Unknown issues: {', '.join(sorted(unknown))}")
    if not db.one(select(commits.c.id).where(commits.c.release_id == release_id,
                   commits.c.repository == data['repository'], commits.c.sha == data['sha'])):
        raise HTTPException(409, 'Sync the exact deployed Git revision into this release first.')
    if data['deployed'] and data['result'] != 'SUCCESS':
        raise HTTPException(422, 'Only a successful deployment can be marked deployed.')
    with db.connect() as conn:
        lock_release(conn, release_id)
        guard_job(conn)
        existing = conn.execute(select(deployments).where(deployments.c.release_id == release_id,
                   deployments.c.environment == data['environment'], deployments.c.job == data['job'],
                   deployments.c.build_number == data['build_number'])).mappings().first()
        comparable = dict(data, issue_keys=json.dumps(keys), deployed=int(data['deployed']))
        if existing:
            if any(existing[key] != value for key, value in comparable.items()):
                raise HTTPException(409, 'Build evidence is immutable; conflicting replay was rejected.')
            return dict(existing)
        item_id = add(conn, deployments, release_id=release_id, **comparable, created_at=now())
        audit(conn, release_id, 'deployment.recorded', {'environment': data['environment'], 'sha': data['sha'], 'job': data['job'],
                                                      'build': data['build_number'], 'deployed': data['deployed']})
    return db.get(deployments, item_id)


def accept_reply(db, external_id, sender, body, references, subject):
    # Email is evidence, never an automatic approval or task-status update.
    matches = db.all(select(messages).where(messages.c.recipient == sender.lower()))
    correlations = re.findall(r'\[RCC:([a-f0-9]{32})\]', subject)
    candidates = [m for m in matches if m['message_id'] in references or m['correlation'] in correlations]
    if len(candidates) != 1 or db.one(select(replies.c.id).where(replies.c.external_id == external_id)):
        return False
    message = candidates[0]
    with db.connect() as conn:
        lock_release(conn, message['release_id'], allow_completed=True)
        if conn.execute(select(release_closures.c.id).where(release_closures.c.release_id == message['release_id'])).first():
            return False
        if conn.execute(select(replies.c.id).where(replies.c.external_id == external_id)).first():
            return False
        add(conn, replies, message_id=message['id'], external_id=external_id, sender=sender.lower(), body=body,
            received_at=now())
        conn.execute(messages.update().where(messages.c.id == message['id']).values(status='replied'))
        audit(conn, message['release_id'], 'stakeholder.reply_received', {'message_id': message['id'], 'sender': sender.lower()})
    return True
