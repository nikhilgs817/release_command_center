import json
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (Column, ForeignKey, Index, Integer, MetaData, String,
                        Table, Text, UniqueConstraint, create_engine, select)

actor = ContextVar('actor', default='local-demo')
active_job = ContextVar('active_job', default=None)
metadata = MetaData()


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid4().hex


def col(name, size=255, **kwargs):
    return Column(name, String(size), nullable=False, **kwargs)


def entity(name, *columns):
    return Table(name, metadata, col('id', 32, primary_key=True), *columns)


def release_fk():
    return Column('release_id', String(32), ForeignKey('releases.id'), nullable=False, index=True)


releases = entity('releases', col('name', 180), col('version', 80), col('current_env', 16),
                  col('change_number', 80, default=''), col('change_sys_id', 80, default=''),
                  Column('jira_jql', Text, nullable=False, default=''), col('created_at', 40),
                  Column('revision', Integer, nullable=False, default=0))
activities = entity('activities', release_fk(), col('environment', 16), col('number', 80, default=''),
                    Column('external_id', String(80)), col('title', 500), col('category', 80, default='General'),
                    col('owner', 180, default=''), col('email', 255, default=''), col('status', 32, default='pending'),
                    col('source', 32, default='local'), Column('copied_from', String(32)), col('updated_at', 40))
Index('ix_activity_external', activities.c.release_id, activities.c.environment, activities.c.external_id)
notes = entity('notes', Column('activity_id', String(32), ForeignKey('activities.id'), nullable=False),
               Column('body', Text, nullable=False), col('created_at', 40))
defects = entity('defects', release_fk(), col('issue_key', 80), col('title', 500), col('status', 80),
                 col('url', 1024, default=''), UniqueConstraint('release_id', 'issue_key'))
commits = entity('commits', release_fk(), col('repository', 255), col('sha', 64), col('subject', 500),
                 Column('issue_keys', Text, nullable=False), col('url', 1024, default=''),
                 UniqueConstraint('release_id', 'repository', 'sha'))
deployments = entity('deployments', release_fk(), col('environment', 16), col('job', 255),
                     Column('build_number', Integer, nullable=False), col('repository', 255), col('sha', 64),
                     col('result', 32), Column('deployed', Integer, nullable=False),
                     Column('issue_keys', Text, nullable=False), col('url', 1024, default=''), col('created_at', 40),
                     UniqueConstraint('release_id', 'environment', 'job', 'build_number'))
promotions = entity('promotions', release_fk(), col('from_env', 16), col('to_env', 16), col('status', 32),
                    Column('summary', Text, nullable=False), col('export_status', 32, default='draft'),
                    col('service_task_id', 80, default=''), col('service_task_number', 80, default=''),
                    col('created_at', 40), UniqueConstraint('release_id', 'from_env', 'to_env'))
messages = entity('messages', release_fk(),
                  Column('promotion_id', String(32), ForeignKey('promotions.id'), nullable=False),
                  Column('activity_id', String(32), ForeignKey('activities.id'), nullable=False),
                  col('recipient', 255), col('subject', 500), Column('body', Text, nullable=False),
                  col('correlation', 40, unique=True), col('message_id', 255, unique=True),
                  col('status', 32, default='draft'), col('created_at', 40), Column('sent_at', String(40)),
                  col('decision', 32, default='pending'),
                  col('error', 500, default=''), UniqueConstraint('promotion_id', 'activity_id'))
replies = entity('replies', Column('message_id', String(32), ForeignKey('messages.id'), nullable=False),
                 col('external_id', 255, unique=True), col('sender', 255),
                 Column('body', Text, nullable=False), col('received_at', 40))
task_exports = entity('task_exports',
                      Column('activity_id', String(32), ForeignKey('activities.id'), nullable=False, unique=True),
                      col('status', 32), col('external_id', 80, default=''), col('error', 500, default=''), col('updated_at', 40))
jobs = entity('jobs', release_fk(), col('kind', 32), Column('payload', Text, nullable=False),
              col('status', 32, default='pending'), Column('attempts', Integer, nullable=False, default=0),
              Column('available_at', Integer, nullable=False, default=0), Column('lease_until', Integer, nullable=False, default=0),
              col('error', 500, default=''), col('created_at', 40), col('updated_at', 40))
Index('ix_jobs_queue', jobs.c.status, jobs.c.available_at)
audit_log = entity('audit_log', release_fk(), col('actor', 255), col('action', 80),
                   Column('details', Text, nullable=False), col('created_at', 40))
mail_cursors = entity('mail_cursors', col('mailbox', 255, unique=True), col('uidvalidity', 80),
                      Column('last_uid', Integer, nullable=False))
leases = Table('leases', metadata, col('id', 80, primary_key=True), col('owner', 32),
               Column('expires_at', Integer, nullable=False))
auth_sessions = entity('auth_sessions', col('token_hash', 64, unique=True), col('principal', 255),
                       Column('roles', Text, nullable=False), Column('expires_at', Integer, nullable=False))
oauth_states = entity('oauth_states', col('nonce', 32), col('verifier', 96),
                      Column('expires_at', Integer, nullable=False), Column('used', Integer, nullable=False, default=0))
schema_version = entity('schema_version', col('version', 32), col('applied_at', 40))

# Additive schema 002: no alteration of existing release records is required.
release_closures = entity('release_closures', release_fk(), col('completed_at', 40),
                          col('completed_by', 255), Column('summary', Text, nullable=False),
                          UniqueConstraint('release_id'))
access_users = entity('access_users', col('principal', 255, unique=True), col('name', 180),
                      col('email', 255), Column('enabled', Integer, nullable=False),
                      Column('revision', Integer, nullable=False, default=0))
access_groups = entity('access_groups', col('name', 180, unique=True),
                       Column('description', Text, nullable=False), Column('permissions', Text, nullable=False),
                       Column('profile_ids', Text, nullable=False),
                       Column('revision', Integer, nullable=False, default=0))
access_memberships = entity('access_memberships',
    Column('user_id', String(32), ForeignKey('access_users.id'), nullable=False),
    Column('group_id', String(32), ForeignKey('access_groups.id'), nullable=False),
    UniqueConstraint('user_id', 'group_id'))
access_audit = entity('access_audit', col('actor', 255), col('action', 80),
                      Column('details', Text, nullable=False), col('created_at', 40))
access_control = entity('access_control', Column('revision', Integer, nullable=False, default=0))
profiles = entity('profiles', col('name', 180, unique=True), Column('description', Text, nullable=False))
release_details = entity('release_details', release_fk(),
    Column('profile_id', String(32), ForeignKey('profiles.id'), nullable=False),
    Column('year', Integer), Column('quarter', Integer),
    col('number_one', 80, default=''), col('number_two', 80, default=''), UniqueConstraint('release_id'))


class Database:
    def __init__(self, url='', path='', initialize=False):
        if not url:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            url = 'sqlite:///' + path
        options = {'pool_pre_ping': True, 'pool_size': 5, 'max_overflow': 2, 'pool_timeout': 10}
        if url.startswith('sqlite'):
            options['connect_args'] = {'check_same_thread': False, 'timeout': 20}
        self.engine = create_engine(url, **options)
        self.dialect = self.engine.dialect.name
        if initialize:
            metadata.create_all(self.engine)

    def connect(self):
        return self.engine.begin()

    def all(self, statement):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(statement).mappings()]

    def one(self, statement):
        with self.engine.connect() as conn:
            row = conn.execute(statement).mappings().first()
            return dict(row) if row else None

    def rows(self, table, **filters):
        statement = select(table)
        for key, value in filters.items():
            statement = statement.where(table.c[key] == value)
        return self.all(statement)

    def get(self, table, item_id):
        return self.one(select(table).where(table.c.id == item_id))

    def limit(self, statement, count):
        # Db2 LIMIT requires a compatibility setting; use native FETCH FIRST.
        return statement.fetch(count) if self.dialect == 'ibm_db_sa' else statement.limit(count)


def add(conn, table, **values):
    values.setdefault('id', uid())
    conn.execute(table.insert().values(**values))
    return values['id']


def audit(conn, release_id, action, details):
    add(conn, audit_log, release_id=release_id, actor=actor.get(), action=action,
        details=json.dumps(details), created_at=now())


def lock_release(conn, release_id, allow_completed=False):
    # A real row UPDATE serializes release transitions on Db2 and SQLite.
    changed = conn.execute(releases.update().where(releases.c.id == release_id)
                           .values(revision=releases.c.revision + 1)).rowcount
    if not allow_completed and conn.execute(select(release_closures.c.id).where(release_closures.c.release_id == release_id)).first():
        from fastapi import HTTPException
        raise HTTPException(409, 'Completed releases are read-only. Their execution history is preserved.')
    return changed


def guard_job(conn):
    claim = active_job.get()
    if claim:
        job_id, attempt = claim
        owned = conn.execute(jobs.update().where(jobs.c.id == job_id, jobs.c.status == 'running',
                                                jobs.c.attempts == attempt).values(lease_until=int(time.time()) + 120)).rowcount
        if owned != 1:
            raise RuntimeError('Worker no longer owns the job; stale source result discarded.')
