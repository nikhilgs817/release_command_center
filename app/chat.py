"""Read-only release questions. All answers come from stored evidence, with no LLM."""
import json
import re

from sqlalchemy import select

from .db import activities, defects, deployments, now
from .service import require_release


def answer_question(db, release_id, question):
    release = require_release(db, release_id)
    upper = question.upper()
    environment_match = re.search(r'\b(DEV|INT|QAT|PROD)\b', upper)
    environment = environment_match.group(1) if environment_match else None
    result = {'answer': '', 'records': [], 'release_name': release['name'], 'environment': environment,
              'answered_at': now(), 'source': 'Stored release records'}
    # Multiple identifiers must be clarified rather than silently answering only one.
    tickets = re.findall(r'\b(SCTASK|CTASK|TASK)\s*[-#]?\s*(\d+)\b', upper)
    issues = re.findall(r'\b([A-Z][A-Z0-9_]*-\d+)\b', upper)
    if len(tickets) > 1 or (not tickets and len(issues) > 1):
        result['answer'] = 'Please ask about one ticket or defect at a time, and include its environment if relevant.'
        return result
    if tickets:
        prefix, number = tickets[0]
        query = select(activities).where(activities.c.release_id == release_id)
        if environment:
            query = query.where(activities.c.environment == environment)
        rows = db.all(query)
        matches = []
        for row in rows:
            match = re.fullmatch(r'(SCTASK|CTASK|TASK)0*(\d+)', row['number'].upper())
            if match and match.group(1) == prefix and int(match.group(2)) == int(number):
                matches.append(row)
        if not matches:
            result['answer'] = f"I cannot find {prefix}{number}{' in ' + environment if environment else ''} in {release['name']}. That does not establish whether it is complete. Check the ticket number, selected release and latest source sync."
            if prefix == 'SCTASK':
                result['answer'] += ' The current ServiceNow connector imports change tasks (CTASK); catalog tasks (SCTASK) require a catalog-task integration.'
            return result
        result['records'] = [{'type': 'activity', 'id': r['id'], 'label': r['number'], 'environment': r['environment'],
                              'status': r['status'], 'updated_at': r['updated_at']} for r in matches]
        lines = [f"{r['number']} in {r['environment']}: {'complete' if r['status'] == 'complete' else r['status'].replace('_', ' ')}. Owner: {r['owner'] or 'unassigned'}. Last recorded update: {r['updated_at']}." for r in matches]
        result['answer'] = ('I found this ticket in multiple environments. Specify an environment for a single answer.\n\n' if len(matches) > 1 else '') + '\n\n'.join(lines)
        result['answer'] += '\n\nThis is the last stored status; it is not a live ServiceNow check.'
        return result
    numeric = re.search(r'\b(?:DEFECT|BUG|ISSUE)\s*(?:NUMBER\s*)?#?\s*(\d+)\b', upper)
    if issues or numeric:
        rows = db.rows(defects, release_id=release_id)
        matches = [r for r in rows if (r['issue_key'].upper() == issues[0] if issues else
                   r['issue_key'].rsplit('-', 1)[-1].isdigit() and int(r['issue_key'].rsplit('-', 1)[-1]) == int(numeric.group(1)))]
        result['records'] = [{'type': 'defect', 'id': r['id'], 'label': r['issue_key'], 'status': r['status']} for r in matches]
        if not matches:
            result['answer'] = f"I cannot find that defect in {release['name']}. Try its full Jira key, or select the release that contains it."
        elif len(matches) > 1:
            result['answer'] = 'That number matches more than one issue: ' + ', '.join(r['issue_key'] for r in matches) + '. Please use the full Jira key.'
        else:
            issue = matches[0]
            evidence = [d for d in db.rows(deployments, release_id=release_id) if issue['issue_key'] in json.loads(d['issue_keys'])
                        and d['result'] == 'SUCCESS' and d['deployed']]
            environments = sorted({d['environment'] for d in evidence})
            result['answer'] = f"{issue['issue_key']} — {issue['title']}\nJira status: {issue['status']}.\n"
            if environment:
                relevant = [d for d in evidence if d['environment'] == environment]
                result['answer'] += (f"Confirmed deployment evidence exists for {environment}." if relevant else
                                     f"No confirmed deployment evidence is stored for {environment}; I cannot confirm it reached that environment.")
            else:
                relevant = evidence
                result['answer'] += 'Confirmed deployment history: ' + (', '.join(environments) if environments else 'none recorded') + '.'
            for d in relevant[:5]:
                result['answer'] += f"\n{d['environment']}: {d['job']} #{d['build_number']}, revision {d['sha'][:12]}."
            result['answer'] += '\n\nJira status and deployment status are separate. Deployment history does not prove what is currently running after a rollback.'
        return result
    if re.search(r'\b(BLOCK|BLOCKED|BLOCKERS|PENDING|INCOMPLETE|REMAINING|OUTSTANDING)\b', upper):
        env = environment or release['current_env']
        rows = [r for r in db.rows(activities, release_id=release_id, environment=env) if r['status'] != 'complete']
        result['answer'] = (f"{len(rows)} activities are not complete in {env}:\n" + '\n'.join(
            f"• {r['number'] or r['title']}: {r['status'].replace('_', ' ')} — {r['owner'] or 'unassigned'}" for r in rows[:20])) if rows else f'No incomplete activities are stored for {env}. This alone does not establish promotion readiness.'
        result['records'] = [{'type': 'activity', 'id': r['id'], 'label': r['number'] or r['title'], 'environment': env, 'status': r['status']} for r in rows[:20]]
        return result
    result['answer'] = 'I can look up a defect, check a ticket in an environment, or list incomplete activities. Try “What is the status of defect 1842?”, “Is CTASK 3204 completed in INT?” or “What is pending in QAT?” Include the identifier in each question. I cannot change records through chat.'
    return result
