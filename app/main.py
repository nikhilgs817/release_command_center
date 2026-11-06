import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from .auth import Identity
from .access import bootstrap, matrix, required_permission, resolve_permissions, save as save_access
from .catalog import close_release, describe, history, profile_of
from .config import ROOT, Settings
from .db import (Database, activities, actor, add, audit, auth_sessions, jobs, leases, messages,
                 notes, now, promotions, releases, replies, schema_version, task_exports, uid,
                 lock_release, profiles, release_details)
from .schemas import (ActivityInput, ActivityStatus, ChatInput, DecisionInput, DeliveryResolution, DeploymentInput,
                      NoteInput, ReleaseInput, SyncInput, AccessGroupInput, AccessUserInput, CloseReleaseInput)
from .service import (accept_reply, enqueue, finalize, prepare, record_deployment,
                      require_release, snapshot)

REQUESTS = Counter('rcc_http_requests_total', 'HTTP requests', ['method', 'route', 'status'])
LATENCY = Histogram('rcc_http_request_seconds', 'HTTP response latency', ['route'])
WORKERS = Gauge('rcc_workers_healthy', 'Workers with an unexpired database heartbeat')
QUEUE = Gauge('rcc_jobs', 'Integration job count by state', ['status'])
MAIL = Gauge('rcc_messages', 'Email count by state', ['status'])
log = logging.getLogger('release.api')


def create_app(settings=None):
    settings = settings or Settings()
    db = Database(settings.database_url, settings.db_path, initialize=settings.demo)
    identity = Identity(settings, db)

    @asynccontextmanager
    async def lifespan(app):
        if settings.demo:
            bootstrap(db)
            from .seed import seed
            seed(db)
            from .seed import seed_catalog
            seed_catalog(db)
        yield
        db.engine.dispose()

    app = FastAPI(title='Release Command Center', version='0.1.0', lifespan=lifespan,
                  docs_url='/docs' if settings.demo else None, redoc_url=None,
                  openapi_url='/openapi.json' if settings.demo else None)
    app.state.db, app.state.settings = db, settings

    @app.middleware('http')
    async def secure_and_measure(request, call_next):
        started, request_id, token = time.monotonic(), uid(), None
        try:
            if request.url.path.startswith('/api/'):
                webhook = bool(re.fullmatch(r'/api/releases/[^/]+/deployment-evidence', request.url.path))
                if webhook:
                    supplied = request.headers.get('X-Webhook-Token', '')
                    if not settings.webhook_token or not secrets.compare_digest(supplied, settings.webhook_token):
                        raise HTTPException(401, 'CI webhook authentication failed.')
                    principal, roles = 'jenkins-webhook', {'Release.Operator'}
                else:
                    principal, roles = await run_in_threadpool(identity.principal, request)
                profile_id = None
                release_match = re.match(r'^/api/releases/([^/]+)', request.url.path)
                object_match = re.match(r'^/api/(activities|messages)/([^/]+)', request.url.path)
                release_id = release_match.group(1) if release_match else None
                if object_match:
                    table = activities if object_match.group(1) == 'activities' else messages
                    item = await run_in_threadpool(db.get, table, object_match.group(2))
                    release_id = item['release_id'] if item else None
                if request.method == 'POST' and request.url.path in ('/api/releases', '/api/chat'):
                    try:
                        payload = await request.json()
                    except ValueError:
                        raise HTTPException(422, 'A JSON request body is required.')
                    if isinstance(payload, dict):
                        profile_id = payload.get('profile_id') if request.url.path == '/api/releases' else None
                        release_id = payload.get('release_id') if request.url.path == '/api/chat' else None
                if release_id:
                    profile_id = await run_in_threadpool(profile_of, db, release_id)
                permissions = await run_in_threadpool(resolve_permissions, db, principal, roles, profile_id)
                permission = required_permission(request.method, request.url.path)
                if not webhook and permission and permission not in permissions:
                    raise HTTPException(403, 'Your application permissions do not allow this action in this profile.')
                if request.query_params.get('reconcile') == 'true' and 'integration.reconcile' not in permissions:
                    raise HTTPException(403, 'Reconciliation permission is required.')
                request.state.roles = roles
                request.state.permissions = permissions
                request.state.principal = principal
                token = actor.set(principal)
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
        except Exception as exc:
            log.error('request_failed request_id=%s error_type=%s', request_id, type(exc).__name__)
            response = JSONResponse({'detail': 'Request failed. Use the request ID when contacting support.', 'request_id': request_id}, status_code=500)
        finally:
            if token is not None:
                actor.reset(token)
        route = request.scope.get('route')
        route_name = route.path if route else 'unmatched'
        REQUESTS.labels(request.method, route_name, str(response.status_code)).inc()
        LATENCY.labels(route_name).observe(time.monotonic() - started)
        response.headers.update({'X-Request-ID': request_id, 'X-Content-Type-Options': 'nosniff',
                                 'Referrer-Policy': 'same-origin', 'X-Frame-Options': 'DENY',
                                 'Content-Security-Policy': "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
        if request.url.path.startswith(('/api/', '/auth/')):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/health/live')
    def live():
        return {'status': 'alive'}

    @app.get('/health/ready')
    def ready():
        try:
            with db.engine.connect() as conn:
                conn.execute(db.limit(select(releases.c.id), 1))
                if not settings.demo and conn.execute(select(schema_version.c.version).where(schema_version.c.id == 'schema')).scalar() != '002':
                    return JSONResponse({'status': 'schema_mismatch'}, status_code=503)
            return {'status': 'ready'}
        except Exception:
            return JSONResponse({'status': 'not_ready'}, status_code=503)

    @app.get('/metrics', include_in_schema=False)
    def metrics():
        try:
            with db.engine.connect() as conn:
                WORKERS.set(conn.execute(select(func.count()).select_from(leases).where(
                    leases.c.id.like('worker:%'), leases.c.expires_at > int(time.time()))).scalar())
                for status in ('pending', 'running', 'retry', 'failed', 'uncertain'):
                    QUEUE.labels(status).set(conn.execute(select(func.count()).select_from(jobs).where(jobs.c.status == status)).scalar())
                for status in ('draft', 'queued', 'sending', 'uncertain'):
                    MAIL.labels(status).set(conn.execute(select(func.count()).select_from(messages).where(messages.c.status == status)).scalar())
        except Exception:
            return JSONResponse({'status': 'database_unavailable'}, status_code=503)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get('/auth/login', include_in_schema=False)
    def login():
        return identity.login()

    @app.get('/auth/callback', include_in_schema=False)
    def callback(request: Request, code: str, state: str):
        return identity.callback(request, code, state)

    @app.post('/api/logout')
    def logout(request: Request):
        import hashlib
        token_hash = hashlib.sha256(request.cookies.get('release_session', '').encode()).hexdigest()
        with db.connect() as conn:
            conn.execute(auth_sessions.delete().where(auth_sessions.c.token_hash == token_hash))
        response = JSONResponse({'signed_out': True})
        response.delete_cookie('release_session')
        return response

    @app.get('/api/config')
    def config(request: Request):
        return {'demo': settings.demo, 'external_writes': settings.external_writes, 'roles': sorted(request.state.roles),
                'permissions': sorted(request.state.permissions),
                'profiles': [dict(p, permissions=sorted(grants)) for p in db.rows(profiles)
                    if 'release.view' in (grants := resolve_permissions(db, request.state.principal, request.state.roles, p['id']))],
                'integrations': {'servicenow': bool(settings.sn_url and settings.sn_token),
                                 'jira': bool(settings.jira_url and settings.jira_token),
                                 'github': bool(settings.github_token), 'jenkins': bool(settings.jenkins_url and settings.jenkins_token),
                                 'email': bool(settings.smtp_host and settings.imap_host)}}

    @app.get('/api/releases')
    def list_releases(request: Request, profile_id: str = '', status: str = ''):
        allowed = {p['id'] for p in db.rows(profiles) if 'release.view' in
                   resolve_permissions(db, request.state.principal, request.state.roles, p['id'])}
        items = [describe(db, r) for r in db.all(select(releases).order_by(releases.c.created_at.desc()))]
        return [r for r in items if r['profile_id'] in allowed and (not profile_id or r['profile_id'] == profile_id)
                and (not status or r['status'] == status)]

    @app.get('/api/access')
    def get_access():
        return matrix(db)

    @app.post('/api/access/users', status_code=201)
    @app.put('/api/access/users/{item_id}')
    def save_user(data: AccessUserInput, request: Request, item_id: str = ''):
        if data.principal == 'local-demo' and not settings.demo:
            raise HTTPException(422, 'Use the user’s Entra object ID in company mode.')
        return save_access(db, 'user', data, item_id, request.state.principal, request.state.roles)

    @app.post('/api/access/groups', status_code=201)
    @app.put('/api/access/groups/{item_id}')
    def save_group(data: AccessGroupInput, request: Request, item_id: str = ''):
        return save_access(db, 'group', data, item_id, request.state.principal, request.state.roles)

    @app.post('/api/chat')
    def chat(data: ChatInput):
        from .chat import answer_question
        return answer_question(db, data.release_id, data.question)

    @app.post('/api/releases', status_code=201)
    def create_release(data: ReleaseInput):
        if not db.get(profiles, data.profile_id):
            raise HTTPException(422, 'Select an existing development profile.')
        values = data.model_dump()
        detail = {key: values.pop(key) for key in ('profile_id', 'year', 'quarter', 'number_one', 'number_two')}
        with db.connect() as conn:
            release_id = add(conn, releases, **values, change_sys_id='', created_at=now(), revision=0)
            add(conn, release_details, release_id=release_id, **detail)
            audit(conn, release_id, 'release.created', {'name': data.name, **detail})
        return describe(db, db.get(releases, release_id))

    @app.get('/api/releases/{release_id}')
    def get_release(release_id: str):
        return snapshot(db, release_id)

    @app.get('/api/releases/{release_id}/history')
    def get_history(release_id: str):
        require_release(db, release_id)
        return {'events': history(db, release_id)}

    @app.post('/api/releases/{release_id}/close')
    def complete_release(release_id: str, data: CloseReleaseInput):
        return close_release(db, release_id, data.summary)

    @app.post('/api/releases/{release_id}/activities', status_code=201)
    def create_activity(release_id: str, data: ActivityInput):
        require_release(db, release_id)
        with db.connect() as conn:
            lock_release(conn, release_id)
            task_id = add(conn, activities, release_id=release_id, **data.model_dump(),
                          status='pending', source='local', number='', updated_at=now())
            audit(conn, release_id, 'activity.created', {'activity_id': task_id})
        return db.get(activities, task_id)

    @app.patch('/api/activities/{activity_id}/status')
    def update_activity(activity_id: str, data: ActivityStatus):
        item = db.get(activities, activity_id)
        if not item:
            raise HTTPException(404, 'Activity not found.')
        if item['source'] == 'servicenow' and not settings.demo:
            raise HTTPException(409, 'ServiceNow owns this task status. Update it there and sync again.')
        with db.connect() as conn:
            from .db import lock_release
            lock_release(conn, item['release_id'])
            release = conn.execute(select(releases).where(releases.c.id == item['release_id'])).mappings().one()
            from .config import ENVIRONMENTS
            if ENVIRONMENTS.index(item['environment']) < ENVIRONMENTS.index(release['current_env']):
                raise HTTPException(409, 'Past-environment activity status is preserved. Add a note or use the source-system reconciliation process.')
            conn.execute(activities.update().where(activities.c.id == activity_id).values(status=data.status, updated_at=now()))
            audit(conn, item['release_id'], 'activity.status_changed', {'activity_id': activity_id, 'status': data.status})
        return db.get(activities, activity_id)

    @app.post('/api/activities/{activity_id}/notes', status_code=201)
    def add_note(activity_id: str, data: NoteInput):
        item = db.get(activities, activity_id)
        if not item:
            raise HTTPException(404, 'Activity not found.')
        with db.connect() as conn:
            lock_release(conn, item['release_id'])
            note_id = add(conn, notes, activity_id=activity_id, body=data.body, created_at=now())
            audit(conn, item['release_id'], 'activity.note_added', {'activity_id': activity_id, 'note_id': note_id})
        return {'id': note_id}

    @app.post('/api/releases/{release_id}/prepare')
    def prepare_release(release_id: str, request: Request):
        return prepare(db, release_id, auto_queue_email=settings.auto_queue_email and settings.external_writes
                       and 'email.send' in request.state.permissions)

    @app.post('/api/releases/{release_id}/promotions/{promotion_id}/complete')
    def complete_promotion(release_id: str, promotion_id: str):
        return finalize(db, release_id, promotion_id, require_source_tickets=not settings.demo,
                        source_max_age_seconds=settings.source_max_age_seconds)

    @app.post('/api/releases/{release_id}/promotions/{promotion_id}/service-task', status_code=202)
    def create_promotion_task(release_id: str, promotion_id: str, reconcile: bool = False):
        if not settings.external_writes:
            raise HTTPException(409, 'External writes are disabled. The release task summary is saved in the promotion plan.')
        plan = db.get(promotions, promotion_id)
        if not plan or plan['release_id'] != release_id:
            raise HTTPException(404, 'Promotion not found.')
        with db.connect() as conn:
            lock_release(conn, release_id)
            if not reconcile:
                changed = conn.execute(promotions.update().where(promotions.c.id == promotion_id,
                                       promotions.c.export_status == 'draft').values(export_status='queued')).rowcount
                if not changed:
                    raise HTTPException(409, 'Release task export already exists. Reconcile an uncertain outcome.')
            job_id = enqueue(conn, release_id, 'export_promotion', {'promotion_id': promotion_id, 'reconcile': reconcile})
            audit(conn, release_id, 'promotion.release_task_queued', {'promotion_id': promotion_id, 'reconcile': reconcile})
        return {'job_id': job_id}

    @app.post('/api/releases/{release_id}/sync', status_code=202)
    def sync(release_id: str, data: SyncInput):
        require_release(db, release_id)
        if settings.demo:
            raise HTTPException(409, 'Demo mode uses sample data. Configure live mode before connecting company systems.')
        with db.connect() as conn:
            lock_release(conn, release_id)
            job_id = enqueue(conn, release_id, data.source, data.model_dump())
            audit(conn, release_id, 'sync.queued', {'job_id': job_id, 'source': data.source})
        return {'job_id': job_id, 'status': 'pending'}

    @app.post('/api/activities/{activity_id}/export', status_code=202)
    def export(activity_id: str):
        if not settings.external_writes:
            raise HTTPException(409, 'External writes are disabled in server configuration.')
        item = db.get(activities, activity_id)
        if not item:
            raise HTTPException(404, 'Activity not found.')
        if item['external_id']:
            return {'status': 'already_exported', 'number': item['number']}
        from .db import lock_release
        with db.connect() as conn:
            lock_release(conn, item['release_id'])
            if conn.execute(select(task_exports.c.id).where(task_exports.c.activity_id == activity_id)).first():
                raise HTTPException(409, 'Export already exists. Use the reconciliation endpoint if its outcome is uncertain.')
            add(conn, task_exports, activity_id=activity_id, status='queued', external_id='', error='', updated_at=now())
            job_id = enqueue(conn, item['release_id'], 'export_task', {'activity_id': activity_id})
            audit(conn, item['release_id'], 'task.export_queued', {'activity_id': activity_id})
        return {'job_id': job_id}

    @app.post('/api/activities/{activity_id}/reconcile-export', status_code=202)
    def reconcile_export(activity_id: str, request: Request):
        item = db.get(activities, activity_id)
        if not item:
            raise HTTPException(404, 'Activity not found.')
        with db.connect() as conn:
            lock_release(conn, item['release_id'])
            job_id = enqueue(conn, item['release_id'], 'export_task', {'activity_id': activity_id, 'reconcile': True})
            audit(conn, item['release_id'], 'task.reconciliation_requested', {'activity_id': activity_id})
        return {'job_id': job_id}

    @app.post('/api/messages/{message_id}/queue')
    def queue_message(message_id: str):
        if not settings.external_writes:
            raise HTTPException(409, 'Sending is disabled. This draft is saved locally for review.')
        item = db.get(messages, message_id)
        if not item:
            raise HTTPException(404, 'Message not found.')
        if not item['recipient']:
            raise HTTPException(409, 'Stakeholder email is missing.')
        with db.connect() as conn:
            lock_release(conn, item['release_id'])
            changed = conn.execute(messages.update().where(messages.c.id == message_id, messages.c.status == 'draft')
                                   .values(status='queued')).rowcount
            if changed:
                audit(conn, item['release_id'], 'email.queued', {'message_id': message_id})
        return {'status': 'queued' if changed else item['status']}

    @app.post('/api/messages/{message_id}/decision')
    def review_message(message_id: str, data: DecisionInput):
        item = db.get(messages, message_id)
        if not item:
            raise HTTPException(404, 'Message not found.')
        if not db.rows(replies, message_id=message_id):
            raise HTTPException(409, 'A correlated stakeholder reply is required before review.')
        with db.connect() as conn:
            from .db import lock_release
            lock_release(conn, item['release_id'])
            plan = conn.execute(select(promotions).where(promotions.c.id == item['promotion_id'])).mappings().one()
            if plan['status'] != 'prepared':
                raise HTTPException(409, 'This promotion decision is finalized. Record follow-up information as a new activity note.')
            conn.execute(messages.update().where(messages.c.id == message_id).values(decision=data.decision))
            audit(conn, item['release_id'], 'stakeholder.' + data.decision, {'message_id': message_id, 'reason': data.reason})
        return {'decision': data.decision}

    @app.post('/api/messages/{message_id}/resolve-delivery')
    def resolve_delivery(message_id: str, data: DeliveryResolution, request: Request):
        item = db.get(messages, message_id)
        if not item:
            raise HTTPException(404, 'Message not found.')
        with db.connect() as conn:
            lock_release(conn, item['release_id'])
            changed = conn.execute(messages.update().where(messages.c.id == message_id, messages.c.status == 'uncertain')
                                   .values(status=data.status, error='')).rowcount
            if not changed:
                raise HTTPException(409, 'Only an uncertain delivery can be reconciled.')
            audit(conn, item['release_id'], 'email.delivery_reconciled',
                  {'message_id': message_id, 'outcome': data.status, 'reason': data.reason})
        return {'status': data.status}

    @app.post('/api/messages/{message_id}/demo-reply')
    def demo_reply(message_id: str):
        if not settings.demo:
            raise HTTPException(404, 'Not available.')
        item = db.get(messages, message_id)
        if not item or not item['recipient']:
            raise HTTPException(409, 'A draft and stakeholder email are required.')
        accepted = accept_reply(db, 'demo-' + message_id, item['recipient'],
                                'Sample reply: ready for the next environment. This is demo data.', [item['message_id']], item['subject'])
        return {'accepted': accepted}

    @app.post('/api/releases/{release_id}/deployment-evidence', status_code=201)
    def deployment_evidence(release_id: str, data: DeploymentInput):
        return record_deployment(db, release_id, data.model_dump())

    @app.get('/api/search')
    def search(request: Request, q: str = Query(min_length=1, max_length=300), environment: str = '', release_id: str = '', profile_id: str = ''):
        results = []
        targets = [require_release(db, release_id)] if release_id else db.all(db.limit(select(releases), 200))
        term = q.lower().strip()
        for release in targets:
            release_profile = profile_of(db, release['id'])
            if (profile_id and release_profile != profile_id) or 'release.view' not in resolve_permissions(
                    db, request.state.principal, request.state.roles, release_profile):
                continue
            data = snapshot(db, release['id'])
            for kind in ('activities', 'defects', 'commits', 'deployments'):
                for item in data[kind]:
                    searchable = {k: v for k, v in item.items() if k not in ('deployments', 'commits')}
                    if term not in json.dumps(searchable).lower():
                        continue
                    if environment and item.get('environment') != environment and environment not in item.get('environments', []):
                        continue
                    results.append({'type': kind, 'release': release['name'], 'release_id': release['id'], 'record': item})
        return {'query': q, 'results': results[:200], 'truncated': len(results) > 200}

    @app.get('/')
    def home():
        return FileResponse(ROOT / 'app' / 'static' / 'index.html')

    app.mount('/static', StaticFiles(directory=ROOT / 'app' / 'static'), name='static')
    return app


app = create_app()
