import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.schema import CreateTable

from app.config import Settings
from app.connectors import ConnectorError, Integrations
from app.db import (Database, activities, add, commits, defects, deployments, jobs,
                    messages, metadata, now, releases, replies)
from app.main import create_app
from app.mail import deliver
from app.service import accept_reply, prepare, snapshot
from app.worker import process_one


def test_quarterly_release_has_profile_and_two_numbers(client):
    payload = {'name': 'CMS quarterly release', 'version': '2027.Q1', 'profile_id': 'cms',
               'year': 2027, 'quarter': 1, 'number_one': 'REL-1001', 'number_two': 'REL-1002'}
    response = client.post('/api/releases', json=payload)
    assert response.status_code == 201
    release = response.json()
    assert release['profile_id'] == 'cms' and release['quarter'] == 1
    assert release['number_one'] == 'REL-1001' and release['number_two'] == 'REL-1002'
    assert release['status'] == 'active'
    assert all(r['profile_id'] == 'cms' for r in client.get('/api/releases?profile_id=cms').json())
    payload['number_two'] = payload['number_one']
    assert client.post('/api/releases', json=payload).status_code == 422


def _session_for(client, principal, roles):
    import hashlib
    import time
    from app.db import auth_sessions
    token = 'session-' + principal
    with client.app.state.db.connect() as conn:
        add(conn, auth_sessions, token_hash=hashlib.sha256(token.encode()).hexdigest(),
            principal=principal, roles=json.dumps(roles), expires_at=int(time.time()) + 3600)
    client.cookies.set('release_session', token)
    client.app.state.settings.demo = False
    client.headers['Origin'] = client.app.state.settings.public_url


def test_profile_access_applies_to_urls_search_chat_and_mutations(client):
    release = primary(client)
    activity = client.get('/api/releases/' + release['id']).json()['activities'][0]
    group = client.post('/api/access/groups', json={'name': 'CMS observers', 'permissions': ['release.view'],
                                                  'profile_ids': ['cms']}).json()
    principal = '00000000-0000-0000-0000-000000000123'
    user = client.post('/api/access/users', json={'principal': principal, 'name': 'CMS colleague',
                                                'group_ids': [group['id']]}).json()
    _session_for(client, principal, ['Release.Admin'])
    # An explicit application account overrides the broad Entra role.
    assert all(r['profile_id'] == 'cms' for r in client.get('/api/releases').json())
    assert client.get('/api/releases/' + release['id']).status_code == 403
    assert client.get('/api/releases/' + release['id'] + '/history').status_code == 403
    assert client.post('/api/chat', json={'release_id': release['id'], 'question': 'What is pending?'}).status_code == 403
    assert client.get('/api/search', params={'q': 'payment', 'release_id': release['id']}).json()['results'] == []
    assert client.post('/api/activities/' + activity['id'] + '/notes', json={'body': 'Unauthorized'}).status_code == 403
    assert client.get('/api/access').status_code == 403
    from app.db import access_users
    with client.app.state.db.connect() as conn:
        conn.execute(access_users.update().where(access_users.c.id == user['id']).values(enabled=0))
    assert client.get('/api/config').status_code == 403


def test_group_permissions_update_without_new_session_and_detect_conflicts(client):
    group = client.post('/api/access/groups', json={'name': 'M&O notes', 'permissions': ['release.view', 'activity.note'],
                                                  'profile_ids': ['m_o']}).json()
    group_id = group['id']
    principal = '00000000-0000-0000-0000-000000000124'
    client.post('/api/access/users', json={'principal': principal, 'name': 'Recorder', 'group_ids': [group_id]})
    release = primary(client)
    task = client.get('/api/releases/' + release['id']).json()['activities'][0]
    _session_for(client, principal, ['Release.Reader'])
    url = '/api/activities/' + task['id'] + '/notes'
    assert client.post(url, json={'body': 'Authorized note'}).status_code == 201
    assert client.patch('/api/activities/' + task['id'] + '/status', json={'status': 'complete'}).status_code == 403
    client.app.state.settings.demo = True
    payload = {'name': 'M&O notes', 'permissions': ['release.view'], 'profile_ids': ['m_o'], 'revision': 0}
    assert client.put('/api/access/groups/' + group_id, json=payload).status_code == 200
    assert client.put('/api/access/groups/' + group_id, json=payload).status_code == 409
    client.app.state.settings.demo = False
    assert client.post(url, json={'body': 'Revoked'}).status_code == 403


def test_access_change_cannot_lock_out_current_admin(client):
    response = client.post('/api/access/users', json={'principal': 'local-demo', 'name': 'Demo admin',
                                                    'group_ids': ['readers']})
    assert response.status_code == 409
    assert client.get('/api/access').json()['users'] == []
    assert client.post('/api/access/groups', json={'name': 'Bad permission', 'permissions': ['system.superuser'],
                                                 'profile_ids': ['cms']}).status_code == 422


def _ready_to_close(client):
    release = primary(client)
    db = client.app.state.db
    with db.connect() as conn:
        conn.execute(releases.update().where(releases.c.id == release['id']).values(current_env='PROD'))
        conn.execute(activities.update().where(activities.c.release_id == release['id']).values(status='complete'))
        add(conn, activities, release_id=release['id'], environment='PROD', number='CTASK9999', title='PROD verification',
            category='Validation', owner='Production owner', email='', status='complete', source='demo', updated_at=now())
        keys = list(conn.execute(select(defects.c.issue_key).where(defects.c.release_id == release['id'])).scalars())
        add(conn, deployments, release_id=release['id'], environment='PROD', job='sample/deploy', build_number=99,
            repository='sample/payments', sha='1' * 40, result='SUCCESS', deployed=1,
            issue_keys=json.dumps(keys), url='', created_at=now())
    return release


def test_completion_preserves_history_and_blocks_edits(client):
    release = primary(client)
    url = '/api/releases/' + release['id']
    assert client.post(url + '/close', json={'summary': 'Too early'}).status_code == 409
    _ready_to_close(client)
    task = client.get(url).json()['activities'][0]
    client.post('/api/activities/' + task['id'] + '/notes', json={'body': 'Verified before closure'})
    first = client.post(url + '/close', json={'summary': 'PROD verification complete'})
    assert first.status_code == 200
    assert client.post(url + '/close', json={'summary': 'Repeated click'}).json()['id'] == first.json()['id']
    assert client.get(url).json()['release']['status'] == 'completed'
    assert release['id'] not in {r['id'] for r in client.get('/api/releases?status=active').json()}
    assert release['id'] in {r['id'] for r in client.get('/api/releases?status=completed').json()}
    assert client.post('/api/activities/' + task['id'] + '/notes', json={'body': 'Too late'}).status_code == 409
    assert client.post(url + '/activities', json={'title': 'Too late', 'environment': 'PROD'}).status_code == 409
    assert client.patch('/api/activities/' + task['id'] + '/status', json={'status': 'blocked'}).status_code == 409
    events = client.get(url + '/history').json()['events']
    assert any(event['body'] == 'Verified before closure' and event['actor'] == 'local-demo' for event in events)
    assert any(event['title'] == 'release · completed' for event in events)


def test_completion_blocks_pending_jobs_and_scheduler_skips_completed(client):
    from app.worker import schedule_syncs
    release = _ready_to_close(client)
    db = client.app.state.db
    from app.service import enqueue
    with db.connect() as conn:
        job_id = enqueue(conn, release['id'], 'jira', {})
    assert client.post('/api/releases/' + release['id'] + '/close', json={'summary': 'Not yet'}).status_code == 409
    with db.connect() as conn:
        conn.execute(jobs.update().where(jobs.c.id == job_id).values(status='succeeded'))
    assert client.post('/api/releases/' + release['id'] + '/close', json={'summary': 'Verified'}).status_code == 200
    schedule_syncs(db, SimpleNamespace(demo=False, auto_sync_seconds=300, sn_url='https://example.invalid',
                                      sn_env_field='u_env', jira_url='https://example.invalid'))
    assert len(db.rows(jobs, release_id=release['id'])) == 1


@pytest.fixture
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / 'test.sqlite3'), database_url='', demo=True,
                        api_key='', webhook_token='ci-test-only', external_writes=False)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def primary(client):
    return next(r for r in client.get('/api/releases').json() if r['name'] == 'Payments platform')


def ready(client):
    release = primary(client)
    data = client.get('/api/releases/' + release['id']).json()
    for item in data['activities']:
        if item['environment'] == 'INT':
            assert client.patch('/api/activities/' + item['id'] + '/status', json={'status': 'complete'}).status_code == 200
    return release


def test_prepare_blocked_then_idempotent_and_notes_copied(client):
    release = primary(client)
    endpoint = '/api/releases/' + release['id'] + '/prepare'
    assert client.post(endpoint).status_code == 409
    ready(client)
    first, second = client.post(endpoint), client.post(endpoint)
    assert first.status_code == 200 and first.json()['id'] == second.json()['id']
    data = client.get('/api/releases/' + release['id']).json()
    assert data['release']['current_env'] == 'INT'
    assert len([a for a in data['activities'] if a['environment'] == 'QAT']) == 6
    assert len(data['messages']) == 6
    assert all('Previous execution notes:' in m['body'] for m in data['messages'])
    assert all(m['status'] == 'draft' for m in data['messages'])
    assert any('V214' in m['body'] for m in data['messages'])
    assert 'V214' in data['promotions'][0]['summary']
    assert 'CTASK003200' in data['promotions'][0]['summary']
    assert all(a['owner'] in data['promotions'][0]['summary'] for a in data['activities'] if a['environment'] == 'INT')


def test_concurrent_prepare_creates_one_plan(client):
    release = ready(client)
    db = client.app.state.db
    with ThreadPoolExecutor(max_workers=4) as executor:
        plans = list(executor.map(lambda _: prepare(db, release['id']), range(4)))
    assert len({plan['id'] for plan in plans}) == 1
    assert len(db.rows(messages, release_id=release['id'])) == 6


def test_reply_correlation_sender_and_deduplication(client):
    release = ready(client)
    client.post('/api/releases/' + release['id'] + '/prepare')
    db = client.app.state.db
    message = db.rows(messages, release_id=release['id'])[0]
    assert not accept_reply(db, 'wrong-person', 'other@example.invalid', 'ready', [message['message_id']], '')
    assert not accept_reply(db, 'unmatched', message['recipient'], 'ready', [], 'A different conversation')
    assert accept_reply(db, 'reply-1', message['recipient'], 'ready', [message['message_id']], '')
    assert not accept_reply(db, 'reply-1', message['recipient'], 'ready', [message['message_id']], '')
    assert db.get(messages, message['id'])['decision'] == 'pending'
    assert len(db.rows(replies, message_id=message['id'])) == 1


def test_promotion_requires_tasks_approval_and_deployment(client):
    release = ready(client)
    plan = client.post('/api/releases/' + release['id'] + '/prepare').json()
    endpoint = f"/api/releases/{release['id']}/promotions/{plan['id']}/complete"
    assert client.post(endpoint).status_code == 409
    data = client.get('/api/releases/' + release['id']).json()
    for activity in data['activities']:
        if activity['environment'] == 'QAT':
            client.patch('/api/activities/' + activity['id'] + '/status', json={'status': 'complete'})
    for message in data['messages']:
        decision_url = '/api/messages/' + message['id'] + '/decision'
        assert client.post(decision_url, json={'decision': 'approved', 'reason': 'Reviewed'}).status_code == 409
        client.post('/api/messages/' + message['id'] + '/demo-reply')
        assert client.post(decision_url, json={'decision': 'approved', 'reason': 'Reviewed sample reply'}).status_code == 200
    assert client.post(endpoint).status_code == 409
    payload = {'environment': 'QAT', 'job': 'sample/payments-deploy', 'build_number': 300,
               'repository': 'sample/payments', 'sha': '2' * 40, 'result': 'SUCCESS', 'deployed': True,
               'issue_keys': ['PAY-1842', 'PAY-1856'], 'url': ''}
    evidence_url = '/api/releases/' + release['id'] + '/deployment-evidence'
    assert client.post(evidence_url, json=payload).status_code == 401
    response = client.post(evidence_url, json=payload, headers={'X-Webhook-Token': 'ci-test-only'})
    assert response.status_code == 201, response.text
    assert client.post(endpoint).status_code == 409
    payload['build_number'] = 301
    payload['issue_keys'] = ['PAY-1842', 'PAY-1856', 'PAY-1863', 'PAY-1871']
    assert client.post(evidence_url, json=payload, headers={'X-Webhook-Token': 'ci-test-only'}).status_code == 201
    assert client.post(endpoint).status_code == 200
    assert client.post(endpoint).status_code == 200
    assert client.get('/api/releases/' + release['id']).json()['release']['current_env'] == 'QAT'


def test_evidence_immutable_and_unknown_revision_rejected(client):
    release = primary(client)
    endpoint = '/api/releases/' + release['id'] + '/deployment-evidence'
    headers = {'X-Webhook-Token': 'ci-test-only'}
    payload = {'environment': 'QAT', 'job': 'sample/deploy', 'build_number': 301, 'repository': 'sample/payments',
               'sha': 'f' * 40, 'result': 'SUCCESS', 'deployed': True, 'issue_keys': ['PAY-1842'], 'url': ''}
    assert client.post(endpoint, json=payload, headers=headers).status_code == 409
    payload['sha'] = '1' * 40
    assert client.post(endpoint, json=payload, headers=headers).status_code == 201
    assert client.post(endpoint, json=payload, headers=headers).status_code == 201
    payload['sha'] = '2' * 40
    assert client.post(endpoint, json=payload, headers=headers).status_code == 409
    payload['build_number'] = 302
    payload['result'] = 'FAILURE'
    assert client.post(endpoint, json=payload, headers=headers).status_code == 422


def test_search_issue_returns_environment_evidence(client):
    release = primary(client)
    data = client.get('/api/search', params={'q': 'PAY-1842', 'release_id': release['id'], 'environment': 'INT'}).json()
    issue = next(x['record'] for x in data['results'] if x['type'] == 'defects')
    assert issue['environments'] == ['DEV', 'INT']
    assert issue['deployments'][1]['build_number'] in (278, 284)


def test_smtp_failure_never_blindly_resends(client, monkeypatch):
    release = ready(client)
    client.post('/api/releases/' + release['id'] + '/prepare')
    db = client.app.state.db
    item = db.rows(messages, release_id=release['id'])[0]
    with db.connect() as conn:
        conn.execute(messages.update().where(messages.c.id == item['id']).values(status='queued'))
    attempts = []
    class FailedSMTP:
        def __init__(self, *args, **kwargs):
            attempts.append(1)
            raise TimeoutError('Simulated disconnect after uncertain delivery')
    monkeypatch.setattr('app.mail.smtplib.SMTP', FailedSMTP)
    settings = SimpleNamespace(external_writes=True, smtp_host='smtp.invalid', smtp_port=587,
                               mail_from='release@example.invalid', mail_user='release', mail_password='test')
    assert not deliver(db, settings, item)
    assert db.get(messages, item['id'])['status'] == 'uncertain'
    assert not deliver(db, settings, item)
    assert len(attempts) == 1


def test_live_auth_enforces_reader_and_expiry(client, monkeypatch):
    import hashlib
    import time
    from app.db import auth_sessions
    db = client.app.state.db
    client.app.state.settings.demo = False
    token = 'test-session'
    with db.connect() as conn:
        add(conn, auth_sessions, token_hash=hashlib.sha256(token.encode()).hexdigest(),
            principal='reader-user', roles=json.dumps(['Release.Reader']), expires_at=int(time.time()) + 300)
    assert client.get('/api/releases').status_code == 401
    client.cookies.set('release_session', token)
    assert client.get('/api/releases').status_code == 200
    assert client.post('/api/releases', json={'name': 'Denied', 'version': '1'},
                       headers={'Origin': client.app.state.settings.public_url}).status_code == 403
    assert client.post('/api/logout', headers={'Origin': 'https://wrong.invalid'}).status_code == 403


def test_github_compare_paginates_and_does_not_mark_deployed(client):
    release = primary(client)
    pages = []
    def source(request):
        page = int(request.url.params['page']); pages.append(page)
        count = 100 if page == 1 else 1
        return httpx.Response(200, json={'status': 'ahead', 'total_commits': 101,
            'commits': [{'sha': f'{page * 100 + n:040x}', 'commit': {'message': 'PAY-1871: change'}} for n in range(count)]})
    settings = Settings(github_token='test-token')
    db = client.app.state.db
    connector = Integrations(settings, db, transport=httpx.MockTransport(source))
    count = connector.sync_github(release['id'], {'repository': 'sample/test', 'base': 'v1', 'head': 'v2'})
    connector.close()
    assert count == 101 and pages == [1, 2]
    data = snapshot(db, release['id'])
    assert next(x for x in data['defects'] if x['issue_key'] == 'PAY-1871')['environments'] == []


def test_servicenow_sync_requires_environment_scope(client):
    release = primary(client)
    def source(request):
        return httpx.Response(200, json={'result': [{'sys_id': 'a' * 32, 'number': release['change_number']}]})
    settings = Settings(sn_url='https://sn.invalid', sn_user='test', sn_token='test')
    connector = Integrations(settings, client.app.state.db, transport=httpx.MockTransport(source))
    with pytest.raises(ConnectorError, match='SERVICENOW_ENV_FIELD'):
        connector.sync_servicenow(release['id'], {'environment': 'INT'})
    connector.close()


def test_external_writes_are_off_and_invalid_status_rejected(client):
    release = primary(client)
    item = client.get('/api/releases/' + release['id']).json()['activities'][0]
    assert client.post('/api/activities/' + item['id'] + '/export').status_code == 409
    assert client.patch('/api/activities/' + item['id'] + '/status', json={'status': 'invented'}).status_code == 422
    assert client.get('/health/ready').status_code == 200
    assert 'rcc_http_requests_total' in client.get('/metrics').text


def test_worker_retries_read_syncs_and_preserves_failure(client):
    from app.service import enqueue
    release = primary(client)
    db = client.app.state.db
    with db.connect() as conn:
        job_id = enqueue(conn, release['id'], 'jira', {})
    class Offline:
        def sync_jira(self, *args):
            raise ConnectorError('Source returned HTTP 503')
    for attempt in range(3):
        with db.connect() as conn:
            conn.execute(jobs.update().where(jobs.c.id == job_id).values(available_at=0))
        assert process_one(db, None, Offline())
    assert db.get(jobs, job_id)['status'] == 'failed'
    assert db.get(jobs, job_id)['attempts'] == 3


def test_db2_schema_compiles_without_a_live_database():
    from sqlalchemy import create_engine
    engine = create_engine('db2+ibm_db://test:test@localhost:50000/SAMPLE')
    for table in metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=engine.dialect))
        assert 'CREATE TABLE' in ddl
    db = Database.__new__(Database)
    db.dialect = engine.dialect.name
    assert 'FETCH FIRST' in str(db.limit(select(releases), 1).compile(dialect=engine.dialect)).upper()
    engine.dispose()


def test_live_settings_fail_closed():
    with pytest.raises(ValueError, match='Db2'):
        Settings(demo=False, database_url='sqlite:///test.sqlite3')
    with pytest.raises(ValueError, match='External writes'):
        Settings(demo=True, external_writes=True)


def test_consolidated_task_export_reconciles_without_duplicate_post(client):
    from app.db import promotions
    release = ready(client)
    plan = client.post('/api/releases/' + release['id'] + '/prepare').json()
    db = client.app.state.db
    with db.connect() as conn:
        conn.execute(releases.update().where(releases.c.id == release['id']).values(change_sys_id='a' * 32))
        conn.execute(promotions.update().where(promotions.c.id == plan['id']).values(export_status='queued'))
    calls = []
    created = []
    def source(request):
        calls.append(request.method)
        if request.method == 'GET':
            return httpx.Response(200, json={'result': created})
        body = json.loads(request.content)
        assert 'V214' in body['description'] and 'Alex Morgan' in body['description']
        assert body['correlation_id'] == 'RCC-PROMOTION-' + plan['id']
        created.append({'sys_id': 'b' * 32, 'number': 'CTASK0099999'})
        return httpx.Response(201, json={'result': created[0]})
    settings = Settings(sn_url='https://sn.invalid', sn_user='test', sn_token='test')
    settings.external_writes = True
    connector = Integrations(settings, db, transport=httpx.MockTransport(source))
    connector.sync_export_promotion(release['id'], {'promotion_id': plan['id']})
    connector.sync_export_promotion(release['id'], {'promotion_id': plan['id'], 'reconcile': True})
    connector.close()
    assert calls.count('POST') == 1
    assert db.get(promotions, plan['id'])['service_task_number'] == 'CTASK0099999'


def test_external_export_failure_does_not_retry(client):
    from app.service import enqueue
    release = primary(client)
    db = client.app.state.db
    with db.connect() as conn:
        job_id = enqueue(conn, release['id'], 'export_task', {})
    class Uncertain:
        def sync_export_task(self, *args):
            raise ConnectorError('Uncertain source write')
    assert process_one(db, None, Uncertain())
    assert db.get(jobs, job_id)['status'] == 'uncertain'
    assert not process_one(db, None, Uncertain())


def test_stale_worker_cannot_commit_source_evidence(client):
    from app.db import active_job
    from app.service import enqueue, record_deployment
    release = primary(client)
    db = client.app.state.db
    with db.connect() as conn:
        job_id = enqueue(conn, release['id'], 'jenkins', {})
        conn.execute(jobs.update().where(jobs.c.id == job_id).values(status='running', attempts=2))
    token = active_job.set((job_id, 1))
    try:
        with pytest.raises(RuntimeError, match='no longer owns'):
            record_deployment(db, release['id'], {'environment': 'QAT', 'job': 'stale-job', 'build_number': 1,
                'repository': 'sample/payments', 'sha': '1' * 40, 'result': 'SUCCESS', 'deployed': True,
                'issue_keys': ['PAY-1842'], 'url': ''})
    finally:
        active_job.reset(token)
    assert not db.rows(deployments, release_id=release['id'], job='stale-job')


def test_chat_defect_distinguishes_jira_and_deployment(client):
    release = primary(client)
    response = client.post('/api/chat', json={'release_id': release['id'], 'question': 'What is status of defect 1842 in QAT?'})
    assert response.status_code == 200
    answer = response.json()
    assert 'Jira status: Resolved' in answer['answer']
    assert 'No confirmed deployment evidence is stored for QAT' in answer['answer']
    assert answer['records'][0]['label'] == 'PAY-1842'


def test_chat_task_normalizes_spaces_and_leading_zeros(client):
    release = primary(client)
    response = client.post('/api/chat', json={'release_id': release['id'], 'question': 'Is CTASK 3204 completed in INT?'})
    assert 'in progress' in response.json()['answer']
    assert response.json()['records'][0]['label'] == 'CTASK003204'
    assert 'last stored status' in response.json()['answer']


def test_chat_catalog_tasks_are_honest_about_missing_data(client):
    release = primary(client)
    response = client.post('/api/chat', json={'release_id': release['id'], 'question': 'Is SCTASK 1234 completed in QAT?'})
    assert response.json()['records'] == []
    assert 'cannot find SCTASK1234 in QAT' in response.json()['answer']
    assert 'catalog-task integration' in response.json()['answer']
    db = client.app.state.db
    with db.connect() as conn:
        add(conn, activities, release_id=release['id'], environment='QAT', number='SCTASK0001234',
            title='Catalog task evidence', category='Catalog', owner='Operator', email='', status='complete',
            source='test', updated_at=now())
    answer = client.post('/api/chat', json={'release_id': release['id'], 'question': 'Is SCTASK 1234 completed in QAT?'}).json()
    assert 'SCTASK0001234 in QAT: complete' in answer['answer']


def test_chat_ambiguity_and_release_scope(client):
    release = primary(client)
    db = client.app.state.db
    with db.connect() as conn:
        add(conn, defects, release_id=release['id'], issue_key='OTHER-1842', title='Different defect', status='Open', url='')
    answer = client.post('/api/chat', json={'release_id': release['id'], 'question': 'defect 1842'}).json()
    assert 'more than one issue' in answer['answer']
    other = next(r for r in client.get('/api/releases').json() if r['id'] != release['id'])
    answer = client.post('/api/chat', json={'release_id': other['id'], 'question': 'defect 1842'}).json()
    assert answer['records'] == []


def test_chat_reader_can_query_without_mutation_permission(client):
    import hashlib
    import time
    from app.db import auth_sessions
    release = primary(client)
    with client.app.state.db.connect() as conn:
        add(conn, auth_sessions, token_hash=hashlib.sha256(b'chat-reader').hexdigest(), principal='reader',
            roles=json.dumps(['Release.Reader']), expires_at=int(time.time()) + 300)
    client.app.state.settings.demo = False
    client.cookies.set('release_session', 'chat-reader')
    headers = {'Origin': client.app.state.settings.public_url}
    assert client.post('/api/chat', headers=headers, json={'release_id': release['id'], 'question': 'What is pending in INT?'}).status_code == 200
    assert client.post('/api/releases/' + release['id'] + '/prepare', headers=headers).status_code == 403
