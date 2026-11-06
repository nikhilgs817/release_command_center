"""Application permissions, evaluated from shared database state on every request."""
import json
import re

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import (access_audit, access_control, access_groups, access_memberships,
                 access_users, actor, add, now, profiles)

PERMISSIONS = {
    'release.view': 'View releases, history and chat',
    'release.create': 'Create releases',
    'activity.create': 'Add activities',
    'activity.update': 'Update activity status',
    'activity.note': 'Add execution notes',
    'promotion.prepare': 'Prepare the next environment',
    'promotion.complete': 'Complete environment promotions',
    'release.close': 'Close releases after PROD',
    'integration.sync': 'Import source-system data',
    'ticket.export': 'Create ServiceNow tasks',
    'email.send': 'Send stakeholder emails',
    'stakeholder.review': 'Review stakeholder readiness',
    'integration.reconcile': 'Reconcile uncertain external writes',
    'access.manage': 'Manage users, groups and permissions',
}
ROLE_PERMISSIONS = {
    'Release.Reader': {'release.view'},
    'Release.Operator': set(PERMISSIONS) - {'access.manage', 'integration.reconcile', 'release.close'},
    'Release.Admin': set(PERMISSIONS),
}
DEFAULT_GROUPS = [
    ('readers', 'Readers', 'Browse release records and ask status questions.', ROLE_PERMISSIONS['Release.Reader']),
    ('operators', 'Release operators', 'Execute release activities and coordinate promotions.', ROLE_PERMISSIONS['Release.Operator']),
    ('admins', 'Administrators', 'Manage application access and all release activities.', ROLE_PERMISSIONS['Release.Admin']),
]


def bootstrap(db):
    with db.connect() as conn:
        for key, name in [('m_o', 'M&O'), ('cms', 'CMS')]:
            if not conn.execute(select(profiles.c.id).where(profiles.c.id == key)).first():
                add(conn, profiles, id=key, name=name, description=name + ' development track')
        if not conn.execute(select(access_control.c.id).where(access_control.c.id == 'policy')).first():
            add(conn, access_control, id='policy', revision=0)
        for key, name, description, permissions in DEFAULT_GROUPS:
            if not conn.execute(select(access_groups.c.id).where(access_groups.c.id == key)).first():
                add(conn, access_groups, id=key, name=name, description=description,
                    permissions=json.dumps(sorted(permissions)), profile_ids='["m_o", "cms"]', revision=0)


def permissions_for(conn, principal, roles, profile_id=None):
    user = conn.execute(select(access_users).where(access_users.c.principal == principal)).mappings().first()
    if not user:
        return set().union(*(ROLE_PERMISSIONS.get(role, set()) for role in roles))
    if not user['enabled']:
        raise HTTPException(403, 'Your application account has been disabled.')
    groups = conn.execute(select(access_groups).join(access_memberships,
        access_memberships.c.group_id == access_groups.c.id).where(access_memberships.c.user_id == user['id'])).mappings()
    return {permission for group in groups
            if profile_id is None or profile_id in json.loads(group['profile_ids'])
            for permission in json.loads(group['permissions'])} & set(PERMISSIONS)


def resolve_permissions(db, principal, roles, profile_id=None):
    with db.engine.connect() as conn:
        return permissions_for(conn, principal, roles, profile_id)


def required_permission(method, path):
    if path in ('/api/config', '/api/logout'):
        return None
    if path.startswith('/api/access'):
        return 'access.manage'
    if method == 'GET' or path == '/api/chat':
        return 'release.view'
    routes = [
        (r'/api/releases', 'release.create'),
        (r'/api/releases/[^/]+/activities', 'activity.create'),
        (r'/api/activities/[^/]+/status', 'activity.update'),
        (r'/api/activities/[^/]+/notes', 'activity.note'),
        (r'/api/releases/[^/]+/prepare', 'promotion.prepare'),
        (r'/api/releases/[^/]+/promotions/[^/]+/complete', 'promotion.complete'),
        (r'/api/releases/[^/]+/close', 'release.close'),
        (r'/api/releases/[^/]+/sync', 'integration.sync'),
        (r'/api/releases/[^/]+/promotions/[^/]+/service-task', 'ticket.export'),
        (r'/api/activities/[^/]+/export', 'ticket.export'),
        (r'/api/activities/[^/]+/reconcile-export', 'integration.reconcile'),
        (r'/api/messages/[^/]+/resolve-delivery', 'integration.reconcile'),
        (r'/api/messages/[^/]+/queue', 'email.send'),
        (r'/api/messages/[^/]+/(decision|demo-reply)', 'stakeholder.review'),
    ]
    return next((permission for pattern, permission in routes if re.fullmatch(pattern, path)), '__deny__')


def matrix(db):
    groups = db.rows(access_groups)
    for group in groups:
        group['permissions'] = json.loads(group['permissions'])
        group['profile_ids'] = json.loads(group['profile_ids'])
    users = db.rows(access_users)
    memberships = db.rows(access_memberships)
    for user in users:
        user['group_ids'] = [m['group_id'] for m in memberships if m['user_id'] == user['id']]
        user['permissions'] = sorted({p for g in groups if g['id'] in user['group_ids'] for p in g['permissions']}) if user['enabled'] else []
    return {'permissions': PERMISSIONS, 'groups': groups, 'users': users, 'profiles': db.rows(profiles),
            'audit': db.all(db.limit(select(access_audit).order_by(access_audit.c.created_at.desc()), 100))}


def save(db, kind, data, item_id, principal, roles):
    table = access_groups if kind == 'group' else access_users
    values = data.model_dump()
    revision = values.pop('revision')
    memberships = values.pop('group_ids', [])
    if kind == 'group':
        if set(values['permissions']) - set(PERMISSIONS):
            raise HTTPException(422, 'Unknown permission.')
        values['permissions'] = json.dumps(sorted(set(values['permissions'])))
        if set(values['profile_ids']) - {p['id'] for p in db.rows(profiles)}:
            raise HTTPException(422, 'Unknown profile.')
        values['profile_ids'] = json.dumps(sorted(set(values['profile_ids'])))
    else:
        values['enabled'] = int(values['enabled'])
    try:
        with db.connect() as conn:
            # Serialize policy changes and recheck authority inside that transaction.
            conn.execute(access_control.update().where(access_control.c.id == 'policy')
                         .values(revision=access_control.c.revision + 1))
            if 'access.manage' not in permissions_for(conn, principal, roles):
                raise HTTPException(403, 'Access management permission required.')
            if kind == 'user':
                known = set(conn.execute(select(access_groups.c.id)).scalars())
                if set(memberships) - known:
                    raise HTTPException(422, 'An assigned group no longer exists.')
            before = None
            if item_id:
                before = conn.execute(select(table).where(table.c.id == item_id)).mappings().first()
                if not before:
                    raise HTTPException(404, 'Account or group not found.')
                if kind == 'user' and values['principal'] != before['principal']:
                    raise HTTPException(409, 'The Entra identity cannot be changed. Disable this account and add the correct identity.')
                changed = conn.execute(table.update().where(table.c.id == item_id, table.c.revision == revision)
                                       .values(**values, revision=revision + 1)).rowcount
                if changed != 1:
                    raise HTTPException(409, 'This record changed. Refresh before saving again.')
            else:
                item_id = add(conn, table, **values, revision=0)
            if kind == 'user':
                conn.execute(access_memberships.delete().where(access_memberships.c.user_id == item_id))
                for group_id in sorted(set(memberships)):
                    add(conn, access_memberships, user_id=item_id, group_id=group_id)
            try:
                retains_admin = 'access.manage' in permissions_for(conn, principal, roles)
            except HTTPException:
                retains_admin = False
            if not retains_admin:
                raise HTTPException(409, 'You cannot remove your own access-management permission. Ask another administrator to make this change.')
            add(conn, access_audit, actor=actor.get(), action=kind + ('.updated' if before else '.created'),
                details=json.dumps({'id': item_id, 'before': dict(before) if before else None,
                                    'after': values, 'group_ids': memberships}), created_at=now())
    except IntegrityError:
        raise HTTPException(409, 'That Entra identity or group name already exists.')
    return {'id': item_id}
