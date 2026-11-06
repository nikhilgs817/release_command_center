# Release Command Center

> Development checkpoint: see [implemented features and remaining work](docs/CHECKPOINT.md). For independent review, use the [complete requirements and design prompt](docs/SECOND_OPINION_PROMPT.md). Live Db2 installs now require migration 002 after 001.


A working FastAPI starter for release coordination across **DEV → INT → QAT → PROD**. The production target is **company servers + IBM Db2 + Microsoft Entra ID**, running as native Python services. **No Kubernetes is used or required.**

## What is implemented

- Release dashboard with environment progress, activities, stakeholder ownership and execution notes.
- ServiceNow change request lookup and environment-scoped change task synchronization.
- Next-environment preparation, with copied activity definitions and a consolidated release task containing previous task numbers, stakeholders and execution notes.
- ServiceNow task creation through a durable queue, stable correlation markers and reconciliation after an uncertain write.
- Stakeholder email drafts; SMTP delivery with STARTTLS; IMAP reply ingestion using tracking references, reply headers and expected sender matching.
- Explicit operator review of replies. Email does not automatically approve a promotion or complete a task.
- Jira Cloud issue synchronization, GitHub comparison imports, Jenkins deployment-manifest ingestion and searchable defect/environment history.
- Transactional promotion gates, database-backed job claims, bounded read-sync retries, role checks, audit records, health checks and metrics.
- Native Linux service templates, a load-balancer example and a Db2 schema for DBA review.

**Delivery status:** locally tested starter, not a production-certified system. The demo uses SQLite and fictional data. Production configuration requires Db2 and Entra ID; live integration, Db2 execution, failover, security and capacity testing remain necessary. Infrastructure has not been provisioned.

## Run the local demo

On Windows, run `run-demo.ps1` from this directory. It creates its own Python environment, installs dependencies and starts the app at **http://127.0.0.1:8000**. Python 3.12 is the tested interpreter.

Alternatively:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

The demo binds to the local computer. Do not expose demo mode on a company network. Demo startup creates fictional releases only when the database has no releases. The database lives in `data/command-center-v1.sqlite3`; synced project references are not modified.

### Try the workflow

1. Open **Payments platform**, currently in INT.
2. Open the two unfinished activities and change their status to complete. Add any execution notes you want carried forward.
3. Select **Prepare QAT**. This creates six new QAT activities, six stakeholder email drafts and one consolidated release task draft. Preparation does not advance the release or deploy code.
4. Select **Review consolidated release task** to inspect the full handover summary.
5. Open **Stakeholder inbox**, simulate sample replies, then record readiness decisions.
6. Search for `PAY-1842` or open **Defect traceability** to inspect its Git revision and confirmed DEV/INT builds.

Completing a QAT promotion also requires completed QAT activities and successful, confirmed QAT deployment evidence. The app does not start a Jenkins deployment.

## Enterprise deployment

Start with [the infrastructure design](docs/INFRASTRUCTURE.md), then [the server runbook](docs/OPERATIONS.md) and [integration contracts](docs/INTEGRATIONS.md).

Production uses two independent application servers behind a redundant company load balancer. Each runs an API process and an integration worker. Shared state, browser sessions, jobs and email cursors are stored in Db2. A shared application filesystem, sticky sessions and a separate message broker are unnecessary for this starter.

WebSphere/Liberty remains part of your approved platform, but the FastAPI process runs separately. It cannot be installed as a Java WAR inside Liberty. The same company load-balancing and certificate infrastructure can route to the Python service.

## Configuration

Copy `.env.example` to `.env` for local work. On servers use the protected environment file described in the runbook. Never commit real credentials.

- `DEMO_MODE=false` requires a `db2+ibm_db://...` database URL, an Entra tenant/client and an HTTPS public URL.
- `ALLOW_EXTERNAL_WRITES=false` disables SMTP sends and ServiceNow task creation while allowing source reads.
- `AUTO_SYNC_SECONDS=300` polls release-scoped Jira issues and ServiceNow tasks for the current and prepared next environment when their settings and environment field are configured. Set `0` to disable periodic synchronization.
- `AUTO_QUEUE_EMAIL=true`, combined with enabled external writes, queues stakeholder emails automatically during preparation. The default keeps drafts for review.
- Next-environment ServiceNow tasks are queued explicitly from their review screens/API. This keeps the external tickets concrete and reviewable before creation.
- Company SMTP/IMAP credentials, service accounts, token rotation and certificate trust follow your organization's policies.

## API examples

The **Ask Release** chat button answers supported status questions from the selected release's stored records. Examples: “What is the status of defect 1842?”, “Is CTASK 3204 completed in INT?” and “What is pending in QAT?” Replies cite source records and distinguish Jira workflow status from deployment evidence. Bare defect numbers that match multiple projects require a full Jira key. Each question is independent; include the identifier rather than relying on conversational pronouns.

Chat is a deterministic read-only lookup, available to Readers as well as Operators/Admins. It makes no calls to an external AI service and does not change tickets or send messages. Conversation history stays in browser memory and is lost on reload. Responses reflect the last stored source status, not a live source call. SCTASK identifiers can be looked up when records exist, but the shipped ServiceNow connector imports CTASK change tasks; a catalog-task adapter is needed to populate real SCTASK records.

In demo mode, visit `/docs` for interactive API documentation. In production that route is disabled.

```text
GET  /api/releases
GET  /api/releases/{release_id}
POST /api/releases/{release_id}/prepare
POST /api/releases/{release_id}/promotions/{promotion_id}/service-task
POST /api/releases/{release_id}/sync
GET  /api/search?q=PAY-1842&environment=INT&release_id={release_id}
POST /api/releases/{release_id}/deployment-evidence
```

The search endpoint returns structured records and their deployment evidence; it is a text/filter lookup, not a natural-language or arbitrary SQL interface. Search is bounded to 200 releases and 200 results. The release detail endpoint is intended for moderate release sizes; enterprise-wide historical analytics and pagination beyond this limit are future work.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-db2.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Tests cover concurrent/idempotent preparation, carried-forward notes, reply correlation, sender mismatch, duplicate replies, promotion gates, immutable deployment evidence, CI authentication, role enforcement, mail ambiguity, pagination and read-sync retries. Db2 tests compile schema/query syntax without connecting to a live Db2 server; that is not proof of runtime compatibility.

Before production, validate the exact Db2 edition, driver and operating-system combination; ServiceNow state/field mappings; Jira edition; company authentication and mail policies; and the recovery scenarios in the runbook.
