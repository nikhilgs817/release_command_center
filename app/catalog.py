"""Profile catalog, release lifecycle and dated execution history."""
import json

from fastapi import HTTPException
from sqlalchemy import select

from .db import (activities, actor, add, audit, audit_log, defects, deployments, jobs,
                 lock_release, messages, notes, now, promotions, release_closures,
                 release_details, releases, replies)


def describe(db, release):
    detail = db.one(select(release_details).where(release_details.c.release_id == release['id']))
    closure = db.one(select(release_closures).where(release_closures.c.release_id == release['id']))
    return dict(release, profile_id=detail['profile_id'] if detail else 'm_o',
                year=detail['year'] if detail else None, quarter=detail['quarter'] if detail else None,
                number_one=detail['number_one'] if detail else '', number_two=detail['number_two'] if detail else '',
                status='completed' if closure else 'active', completion=closure)


def profile_of(db, release_id):
    detail = db.one(select(release_details.c.profile_id).where(release_details.c.release_id == release_id))
    return detail['profile_id'] if detail else 'm_o'


def close_release(db, release_id, summary):
    with db.connect() as conn:
        if not lock_release(conn, release_id, allow_completed=True):
            raise HTTPException(404, 'Release not found.')
        existing = conn.execute(select(release_closures).where(release_closures.c.release_id == release_id)).mappings().first()
        if existing:
            return dict(existing)
        release = conn.execute(select(releases).where(releases.c.id == release_id)).mappings().one()
        tasks = list(conn.execute(select(activities).where(activities.c.release_id == release_id)).mappings())
        if release['current_env'] != 'PROD' or not any(t['environment'] == 'PROD' for t in tasks):
            raise HTTPException(409, 'Complete the PROD release activities before closing this release.')
        if any(t['status'] != 'complete' for t in tasks):
            raise HTTPException(409, 'All release activities must be complete before closure.')
        if conn.execute(select(promotions.c.id).where(promotions.c.release_id == release_id, promotions.c.status != 'completed')).first():
            raise HTTPException(409, 'An environment promotion is still open.')
        if conn.execute(select(jobs.c.id).where(jobs.c.release_id == release_id,
                                               jobs.c.status.in_(['pending', 'running', 'retry', 'uncertain', 'failed']))).first():
            raise HTTPException(409, 'Resolve outstanding integration jobs before closing the release.')
        if conn.execute(select(messages.c.id).where(messages.c.release_id == release_id,
            (messages.c.status.in_(['queued', 'sending', 'uncertain'])) | (messages.c.decision != 'approved'))).first():
            raise HTTPException(409, 'Resolve mail delivery and stakeholder decisions before closing the release.')
        manifests = list(conn.execute(select(deployments.c.issue_keys).where(deployments.c.release_id == release_id,
            deployments.c.environment == 'PROD', deployments.c.result == 'SUCCESS', deployments.c.deployed == 1)).scalars())
        expected = set(conn.execute(select(defects.c.issue_key).where(defects.c.release_id == release_id)).scalars())
        confirmed = {key for manifest in manifests for key in json.loads(manifest)}
        if not manifests or expected - confirmed:
            raise HTTPException(409, 'Confirmed PROD deployment evidence must cover the release defect scope.')
        completed_at = now()
        result = dict(release_id=release_id, completed_at=completed_at, completed_by=actor.get(), summary=summary.strip())
        if not result['summary']:
            raise HTTPException(422, 'Enter a completion summary.')
        item_id = add(conn, release_closures, **result)
        audit(conn, release_id, 'release.completed', {'summary': result['summary'], 'environment': 'PROD'})
        return dict(result, id=item_id)


def history(db, release_id):
    tasks = {a['id']: a for a in db.rows(activities, release_id=release_id)}
    events = []
    audit_events = db.all(select(audit_log).where(audit_log.c.release_id == release_id))
    note_authors = {json.loads(event['details']).get('note_id'): event['actor'] for event in audit_events
                    if event['action'] == 'activity.note_added'}
    for event in audit_events:
        if event['action'] in ('activity.note_added', 'deployment.recorded', 'stakeholder.reply_received'):
            continue
        details = json.loads(event['details'])
        task = tasks.get(details.get('activity_id'), {})
        events.append(dict(id=event['id'], at=event['created_at'], actor=event['actor'],
            title=event['action'].replace('.', ' · ').replace('_', ' '),
            environment=task.get('environment', details.get('environment', details.get('to', ''))),
            activity_id=task.get('id'), body=(task.get('title', '') + '\n' + json.dumps(details)).strip()))
    for note in db.all(select(notes).join(activities, notes.c.activity_id == activities.c.id).where(activities.c.release_id == release_id)):
        task = tasks[note['activity_id']]
        events.append(dict(id=note['id'], at=note['created_at'], actor=note_authors.get(note['id'], 'Author not recorded'), title='Execution note · ' + task['title'],
            environment=task['environment'], activity_id=task['id'], body=note['body']))
    for deployment in db.rows(deployments, release_id=release_id):
        events.append(dict(id=deployment['id'], at=deployment['created_at'], actor='Deployment evidence',
            title=deployment['job'] + ' #' + str(deployment['build_number']), environment=deployment['environment'],
            body=f"{deployment['result']} · {'Confirmed deployment' if deployment['deployed'] else 'Deployment not confirmed'}\n{deployment['sha']}"))
    for reply in db.all(select(replies).join(messages, replies.c.message_id == messages.c.id).where(messages.c.release_id == release_id)):
        events.append(dict(id=reply['id'], at=reply['received_at'], actor=reply['sender'],
                          title='Stakeholder reply received', environment='', body=reply['body']))
    return sorted(events, key=lambda event: (event['at'], event['id']), reverse=True)
