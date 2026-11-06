import json
import re
from urllib.parse import quote

import httpx
from sqlalchemy import select

from .db import activities, add, audit, commits, defects, guard_job, lock_release, messages, now, promotions, releases, task_exports
from .schemas import DeploymentInput
from .service import record_deployment, require_release


class ConnectorError(RuntimeError):
    pass


def value(record, field):
    data = record.get(field, '')
    return str(data.get('value', '') if isinstance(data, dict) else data or '')


def display(record, field):
    data = record.get(field, '')
    return str(data.get('display_value', data.get('value', '')) if isinstance(data, dict) else data or '')


class Integrations:
    def __init__(self, settings, db, transport=None):
        self.s, self.db = settings, db
        self.client = httpx.Client(timeout=httpx.Timeout(20, connect=5), transport=transport,
                                   follow_redirects=False)

    def close(self):
        self.client.close()

    def get(self, url, **kwargs):
        try:
            response = self.client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise ConnectorError('Source connection failed; verify network access and TLS.') from exc
        if response.status_code != 200:
            # Never copy raw source responses or credential-bearing URLs into logs.
            raise ConnectorError(f'Source returned HTTP {response.status_code}; check permissions, rate limits, and configuration.')
        return response.json()

    def configured(self, *values):
        if not all(values):
            raise ConnectorError('Integration settings are incomplete. Configure the server environment first.')

    def sync_servicenow(self, release_id, payload):
        self.configured(self.s.sn_url, self.s.sn_user, self.s.sn_token)
        release = require_release(self.db, release_id)
        if not release['change_number']:
            raise ConnectorError('Set this release change request number first.')
        auth = (self.s.sn_user, self.s.sn_token)
        parents = self.get(self.s.sn_url + '/api/now/table/change_request', auth=auth, params={
            'sysparm_query': 'number=' + release['change_number'], 'sysparm_fields': 'sys_id,number', 'sysparm_limit': 2})['result']
        if len(parents) != 1:
            raise ConnectorError('Change request lookup did not return exactly one record.')
        parent_id = value(parents[0], 'sys_id')
        if not re.fullmatch(r'[a-f0-9]{32}', parent_id):
            raise ConnectorError('Source returned an invalid change request sys_id.')
        env = payload['environment']
        query = 'change_request=' + parent_id
        if self.s.sn_env_field:
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', self.s.sn_env_field):
                raise ConnectorError('Invalid ServiceNow environment field configuration.')
            query += '^' + self.s.sn_env_field + '=' + env
        elif payload.get('task_numbers'):
            query += '^numberIN' + ','.join(payload['task_numbers'])
        else:
            raise ConnectorError('Configure SERVICENOW_ENV_FIELD or explicitly select task_numbers; tasks cannot be assigned to an environment by guesswork.')
        records = []
        for page in range(100):
            batch = self.get(self.s.sn_url + '/api/now/table/change_task', auth=auth, params={
                'sysparm_query': query + '^ORDERBYsys_id', 'sysparm_offset': page * 100, 'sysparm_limit': 100,
                'sysparm_display_value': 'all', 'sysparm_fields':
                'sys_id,number,short_description,assigned_to,assigned_to.email,state'})['result']
            records.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise ConnectorError('Task sync exceeded 10,000 records; narrow the release scope.')
        states = {'3': 'complete', '2': 'in_progress', '-5': 'pending', '1': 'pending', '4': 'cancelled'}
        with self.db.connect() as conn:
            lock_release(conn, release_id)
            guard_job(conn)
            conn.execute(releases.update().where(releases.c.id == release_id).values(change_sys_id=parent_id))
            fetched_ids = {value(record, 'sys_id') for record in records}
            tracked_query = select(activities).where(activities.c.release_id == release_id,
                             activities.c.environment == env, activities.c.source == 'servicenow')
            if not self.s.sn_env_field:
                tracked_query = tracked_query.where(activities.c.number.in_(payload['task_numbers']))
            for tracked in conn.execute(tracked_query).mappings():
                if tracked['external_id'] not in fetched_ids:
                    conn.execute(activities.update().where(activities.c.id == tracked['id'])
                                 .values(status='blocked', updated_at=now()))
            for record in records:
                external_id = value(record, 'sys_id')
                existing = conn.execute(select(activities).where(activities.c.release_id == release_id,
                     activities.c.environment == env, activities.c.external_id == external_id)).mappings().first()
                fields = {'number': value(record, 'number'), 'external_id': external_id,
                          'title': display(record, 'short_description')[:500], 'owner': display(record, 'assigned_to')[:180],
                          'email': value(record, 'assigned_to.email').lower(), 'status': states.get(value(record, 'state'), 'blocked'),
                          'source': 'servicenow', 'updated_at': now()}
                if existing:
                    conn.execute(activities.update().where(activities.c.id == existing['id']).values(**fields))
                else:
                    add(conn, activities, release_id=release_id, environment=env, category='Service task', **fields)
            audit(conn, release_id, 'sync.servicenow', {'environment': env, 'records': len(records)})
        return len(records)

    def sync_jira(self, release_id, payload):
        self.configured(self.s.jira_url, self.s.jira_user, self.s.jira_token)
        release = require_release(self.db, release_id)
        if not release['jira_jql']:
            raise ConnectorError('Set a release-scoped Jira JQL query before syncing.')
        issues, token = [], None
        for _ in range(100):
            params = {'jql': release['jira_jql'], 'maxResults': 100, 'fields': 'summary,status'}
            if token:
                params['nextPageToken'] = token
            data = self.get(self.s.jira_url + '/rest/api/3/search/jql', auth=(self.s.jira_user, self.s.jira_token), params=params)
            issues.extend(data.get('issues', []))
            token = data.get('nextPageToken')
            if not token:
                break
        else:
            raise ConnectorError('Jira sync exceeded pagination limit; narrow the JQL query.')
        with self.db.connect() as conn:
            lock_release(conn, release_id)
            guard_job(conn)
            for issue in issues:
                fields = {'title': issue['fields']['summary'][:500], 'status': issue['fields']['status']['name'][:80],
                          'url': self.s.jira_url + '/browse/' + issue['key']}
                existing = conn.execute(select(defects.c.id).where(defects.c.release_id == release_id,
                                                          defects.c.issue_key == issue['key'])).scalar()
                if existing:
                    conn.execute(defects.update().where(defects.c.id == existing).values(**fields))
                else:
                    add(conn, defects, release_id=release_id, issue_key=issue['key'], **fields)
            audit(conn, release_id, 'sync.jira', {'records': len(issues)})
        return len(issues)

    def sync_github(self, release_id, payload):
        self.configured(self.s.github_token, payload.get('repository'), payload.get('base'), payload.get('head'))
        repo = payload['repository']
        url = f"{self.s.github_url}/repos/{repo}/compare/{quote(payload['base'], safe='')}...{quote(payload['head'], safe='')}"
        headers = {'Authorization': 'Bearer ' + self.s.github_token, 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2026-03-10'}
        rows = []
        for page in range(1, 101):
            data = self.get(url, headers=headers, params={'per_page': 100, 'page': page})
            if data.get('status') not in ('ahead', 'identical'):
                raise ConnectorError('Git comparison diverged or is behind; choose an ancestor baseline.')
            rows.extend(data['commits'])
            if len(rows) >= data['total_commits']:
                break
        else:
            raise ConnectorError('Git comparison exceeds 10,000 commits; narrow the range.')
        if len(rows) != data['total_commits']:
            raise ConnectorError('Incomplete Git comparison; no commits were stored.')
        # An identical range still needs its head revision available for deployment evidence.
        if not rows and data.get('base_commit'):
            rows = [data['base_commit']]
        with self.db.connect() as conn:
            lock_release(conn, release_id)
            guard_job(conn)
            for record in rows:
                existing = conn.execute(select(commits.c.id).where(commits.c.release_id == release_id,
                        commits.c.repository == repo, commits.c.sha == record['sha'])).first()
                if existing:
                    continue
                message = record['commit']['message']
                keys = sorted(set(re.findall(r'\b[A-Z][A-Z0-9_]*-\d+\b', message)))
                add(conn, commits, release_id=release_id, repository=repo, sha=record['sha'],
                    subject=message.splitlines()[0][:500], issue_keys=json.dumps(keys), url=record.get('html_url', ''))
            audit(conn, release_id, 'sync.github', {'repository': repo, 'commits': len(rows)})
        return len(rows)

    def sync_jenkins(self, release_id, payload):
        self.configured(self.s.jenkins_url, self.s.jenkins_user, self.s.jenkins_token, payload.get('job'), payload.get('build_number'))
        path = '/'.join('job/' + quote(part, safe='') for part in payload['job'].split('/'))
        url = f"{self.s.jenkins_url}/{path}/{payload['build_number']}"
        auth = (self.s.jenkins_user, self.s.jenkins_token)
        build = self.get(url + '/api/json', auth=auth, params={'tree': 'number,result,building'})
        if build['building'] or not build['result']:
            raise ConnectorError('Jenkins build is still running.')
        manifest = self.get(url + '/artifact/release-manifest.json', auth=auth)
        if manifest.get('release_id') != release_id or manifest.get('environment') != payload['environment']:
            raise ConnectorError('Jenkins manifest release/environment does not match this sync request.')
        validated = DeploymentInput.model_validate(dict(manifest, job=payload['job'], build_number=build['number'],
                                                        result=build['result'], url=url))
        return record_deployment(self.db, release_id, validated.model_dump())['id']

    def sync_export_task(self, release_id, payload):
        if not self.s.external_writes:
            raise ConnectorError('External writes are disabled.')
        self.configured(self.s.sn_url, self.s.sn_user, self.s.sn_token)
        task = self.db.get(activities, payload['activity_id'])
        release = require_release(self.db, release_id)
        if not task or task['release_id'] != release_id or not release['change_sys_id']:
            raise ConnectorError('Sync the parent change request and verify the activity before export.')
        marker = 'RCC-' + task['id']
        auth = (self.s.sn_user, self.s.sn_token)
        # A stable source marker permits reconciliation after an ambiguous POST.
        found = self.get(self.s.sn_url + '/api/now/table/change_task', auth=auth,
                         params={'sysparm_query': 'correlation_id=' + marker, 'sysparm_limit': 2})['result']
        if len(found) > 1:
            raise ConnectorError('Duplicate correlation markers in ServiceNow; operator reconciliation is required.')
        export = self.db.one(select(task_exports).where(task_exports.c.activity_id == task['id']))
        if not export:
            raise ConnectorError('Export must be queued through the command center first.')
        if found:
            result = found[0]
        else:
            if payload.get('reconcile') or export['status'] != 'queued':
                raise ConnectorError('No matching source task found. Verify ServiceNow audit history before resetting this export.')
            assignee = ''
            if task['email']:
                if not re.fullmatch(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', task['email']):
                    raise ConnectorError('Stakeholder email cannot be used for exact ServiceNow user lookup.')
                users = self.get(self.s.sn_url + '/api/now/table/sys_user', auth=auth, params={
                    'sysparm_query': 'email=' + task['email'], 'sysparm_fields': 'sys_id', 'sysparm_limit': 2})['result']
                if len(users) != 1:
                    raise ConnectorError('Stakeholder does not resolve to exactly one ServiceNow user.')
                assignee = value(users[0], 'sys_id')
            draft = self.db.one(select(messages).where(messages.c.activity_id == task['id']))
            data = {'change_request': release['change_sys_id'], 'short_description': f"[{task['environment']}] {task['title']}"[:160],
                    'description': draft['body'] if draft else task['title'], 'correlation_id': marker}
            if assignee:
                data['assigned_to'] = assignee
            if self.s.sn_env_field:
                data[self.s.sn_env_field] = task['environment']
            with self.db.connect() as conn:
                guard_job(conn)
                claimed = conn.execute(task_exports.update().where(task_exports.c.id == export['id'],
                                       task_exports.c.status == 'queued').values(status='creating', updated_at=now())).rowcount
            if claimed != 1:
                raise ConnectorError('Task export was already claimed. Reconcile it instead of repeating the POST.')
            try:
                response = self.client.post(self.s.sn_url + '/api/now/table/change_task', auth=auth, json=data)
                if response.status_code != 201:
                    raise ConnectorError('ServiceNow create did not return HTTP 201.')
                result = response.json()['result']
            except Exception as exc:
                with self.db.connect() as conn:
                    conn.execute(task_exports.update().where(task_exports.c.id == export['id'])
                                 .values(status='uncertain', error='Reconcile correlation marker before any retry.', updated_at=now()))
                raise ConnectorError('ServiceNow write outcome is uncertain; reconcile before retry.') from exc
        with self.db.connect() as conn:
            lock_release(conn, release_id)
            conn.execute(task_exports.update().where(task_exports.c.id == export['id'])
                         .values(status='created', external_id=value(result, 'sys_id'), error='', updated_at=now()))
            conn.execute(activities.update().where(activities.c.id == task['id'])
                         .values(external_id=value(result, 'sys_id'), number=value(result, 'number'), source='servicenow',
                                 status='pending', updated_at=now()))
            audit(conn, release_id, 'task.exported', {'activity_id': task['id'], 'number': value(result, 'number')})
        return value(result, 'number')

    def sync_export_promotion(self, release_id, payload):
        if not self.s.external_writes:
            raise ConnectorError('External writes are disabled.')
        self.configured(self.s.sn_url, self.s.sn_user, self.s.sn_token)
        release = require_release(self.db, release_id)
        plan = self.db.get(promotions, payload['promotion_id'])
        if not plan or plan['release_id'] != release_id or not release['change_sys_id']:
            raise ConnectorError('Sync the parent change request before exporting the release task.')
        marker = 'RCC-PROMOTION-' + plan['id']
        auth = (self.s.sn_user, self.s.sn_token)
        found = self.get(self.s.sn_url + '/api/now/table/change_task', auth=auth, params={
            'sysparm_query': 'correlation_id=' + marker, 'sysparm_limit': 2})['result']
        if len(found) > 1:
            raise ConnectorError('Duplicate release task markers; reconcile ServiceNow records.')
        if found:
            result = found[0]
        else:
            if payload.get('reconcile') or plan['export_status'] != 'queued':
                raise ConnectorError('Release task outcome needs manual reconciliation; automatic repeat is disabled.')
            with self.db.connect() as conn:
                guard_job(conn)
                claimed = conn.execute(promotions.update().where(promotions.c.id == plan['id'], promotions.c.export_status == 'queued')
                                       .values(export_status='creating')).rowcount
            if claimed != 1:
                raise ConnectorError('Release task creation was already claimed.')
            data = {'change_request': release['change_sys_id'], 'correlation_id': marker,
                    'short_description': f"[{plan['to_env']}] Release preparation: {release['name']} {release['version']}"[:160],
                    'description': plan['summary']}
            if self.s.sn_env_field:
                data[self.s.sn_env_field] = plan['to_env']
            try:
                response = self.client.post(self.s.sn_url + '/api/now/table/change_task', auth=auth, json=data)
                if response.status_code != 201:
                    raise ConnectorError('ServiceNow did not return HTTP 201.')
                result = response.json()['result']
            except Exception as exc:
                with self.db.connect() as conn:
                    conn.execute(promotions.update().where(promotions.c.id == plan['id']).values(export_status='uncertain'))
                raise ConnectorError('Release task create outcome is uncertain; reconcile the correlation marker.') from exc
        with self.db.connect() as conn:
            conn.execute(promotions.update().where(promotions.c.id == plan['id']).values(export_status='created',
                         service_task_id=value(result, 'sys_id'), service_task_number=value(result, 'number')))
            audit(conn, release_id, 'promotion.release_task_created', {'promotion_id': plan['id'], 'number': value(result, 'number')})
        return value(result, 'number')
