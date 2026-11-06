import json

from sqlalchemy import select

from .db import activities, add, audit, commits, defects, deployments, leases, notes, now, releases


def seed(db):
    if db.one(db.limit(select(releases.c.id), 1)):
        return
    with db.connect() as conn:
        conn.execute(leases.insert().values(id='imap', owner='', expires_at=0))
        release_id = add(conn, releases, name='Payments platform', version='2026.11.1', current_env='INT',
                         change_number='CHG0010842', change_sys_id='', jira_jql='project = PAY AND fixVersion = "2026.11.1"',
                         created_at=now(), revision=0)
        activity_data = [
            ('Database migration · payment ledger', 'Database', 'Alex Morgan', 'alex.morgan@example.invalid', 'complete',
             'Applied migration V214. Row counts and rollback scripts verified.'),
            ('Deploy payment-service artifact', 'Deployment', 'Jordan Lee', 'jordan.lee@example.invalid', 'complete',
             'Artifact 2026.11.1 deployed using Jenkins build #284. Health checks passed.'),
            ('Update application configuration', 'Configuration', 'Sam Rivera', 'sam.rivera@example.invalid', 'complete',
             'Environment-specific timeouts updated. No secrets included in this note.'),
            ('Validate downstream connectivity', 'Validation', 'Taylor Chen', 'taylor.chen@example.invalid', 'complete',
             'Ledger and notification endpoints responded successfully.'),
            ('Run regression smoke tests', 'Testing', 'Casey Patel', 'casey.patel@example.invalid', 'in_progress',
             'Payment retry scenario is still being validated.'),
            ('Confirm operations handover', 'Operations', 'Riley Kim', 'riley.kim@example.invalid', 'pending',
             'Waiting for the operations runbook review.'),
        ]
        for env in ('DEV', 'INT'):
            for index, (title, category, owner, email, status, note) in enumerate(activity_data):
                task_id = add(conn, activities, release_id=release_id, environment=env,
                              number=f'CTASK00{3100 + index + (100 if env == "INT" else 0)}',
                              external_id=None, title=title, category=category, owner=owner, email=email,
                              status='complete' if env == 'DEV' else status, source='demo', updated_at=now())
                add(conn, notes, activity_id=task_id, body='SAMPLE DATA: ' + note, created_at=now())
        issues = [('PAY-1842', 'Duplicate payment on retry', 'Resolved'), ('PAY-1856', 'Ledger date offset', 'Resolved'),
                  ('PAY-1863', 'Timeout during settlement', 'In QA'), ('PAY-1871', 'Incorrect refund rounding', 'In Progress')]
        for index, (key, title, status) in enumerate(issues):
            add(conn, defects, release_id=release_id, issue_key=key, title=title, status=status, url='')
            add(conn, commits, release_id=release_id, repository='sample/payments', sha=str(index + 1) * 40,
                subject=f'{key}: {title}', issue_keys=json.dumps([key]), url='')
        for env, build, keys in [('DEV', 278, [i[0] for i in issues[:3]]), ('INT', 284, [i[0] for i in issues[:2]])]:
            add(conn, deployments, release_id=release_id, environment=env, job='sample/payments-deploy', build_number=build,
                repository='sample/payments', sha='3' * 40 if env == 'DEV' else '2' * 40, result='SUCCESS', deployed=1,
                issue_keys=json.dumps(keys), url='', created_at=now())
        audit(conn, release_id, 'demo.seeded', {'notice': 'All records are fictional sample data.'})
        add(conn, releases, name='Customer experience', version='2026.11.2', current_env='DEV',
            change_number='', change_sys_id='', jira_jql='', created_at=now(), revision=0)


def seed_catalog(db):
    """Assign existing fictional demo releases to their development profiles."""
    from .db import release_details
    with db.connect() as conn:
        for release in conn.execute(select(releases)).mappings():
            if conn.execute(select(release_details.c.id).where(release_details.c.release_id == release['id'])).first():
                continue
            profile = 'cms' if release['name'] == 'Customer experience' else 'm_o'
            prefix = 'CMS' if profile == 'cms' else 'MO'
            add(conn, release_details, release_id=release['id'], profile_id=profile, year=2026, quarter=4,
                number_one=prefix + '-2026-Q4-01', number_two=prefix + '-2026-Q4-02')
