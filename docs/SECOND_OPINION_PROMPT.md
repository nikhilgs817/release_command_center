# Independent architecture and implementation review prompt

You are an independent enterprise software architect and senior Python engineer. Critically review this Release Command Center. Challenge the design, identify gaps and propose a practical implementation plan. Do not simply endorse the existing choices. Separate confirmed requirements, assumptions, implementation findings and recommendations. Inspect the repository when available; do not assume a feature is implemented because it appears in this prompt.

Repository: https://github.com/nikhilgs817/release_command_center

## Business problem

We manually coordinate enterprise releases across ServiceNow, Jira, Jenkins, GitHub, email and Excel. Release activities include database changes, configuration, deployment, testing, validation and operational handover. Activities have ServiceNow tickets, stakeholders, execution notes and statuses. After performing work in one environment, we prepare a release task for the next environment containing the completed work and stakeholder details. We email stakeholders, collect replies and readiness decisions, and coordinate the next promotion. Defect deployment tracking in INT and QAT is currently maintained in Excel.

Build a shared application that brings this information together, automates repetitive preparation and communication, preserves evidence and makes status easy to query.

## Confirmed requirements and constraints

1. Python and FastAPI backend. Native Python services alongside approved WebSphere/Liberty are explicitly allowed. FastAPI runs in its own Python runtime; it is not a Java application deployed inside Liberty.
2. Company-owned, on-premises servers. Absolutely no Kubernetes. Do not introduce Kubernetes as a prerequisite or migration target.
3. IBM Db2 is the selected approved production database. The exact Db2 edition, version and platform must be verified with the company. The current deployment proposal assumes Db2 LUW; do not silently assume LUW recommendations apply to Db2 for z/OS.
4. Microsoft Entra ID / Azure AD company authentication. No separate application passwords.
5. Company SMTP for outgoing email and IMAP for incoming replies.
6. Integrations: ServiceNow change requests and change tasks, Jira defects/issues, Jenkins deployment evidence and GitHub commits/revisions. ServiceNow field mappings and enterprise API versions must be configured and validated.
7. Environment sequence: DEV → INT → QAT → PROD.
8. Quarterly releases, with TWO distinct release numbers for each quarterly release in EACH development track. Do not mistake this for one number per track. The business meanings of the two numbers have not yet been specified; use neutral labels pending confirmation. Keep the ServiceNow change number separate.
9. Two switchable development profiles: M&O and CMS. Each has its own quarterly releases, two release numbers, activities, stakeholder conversations, history and defect evidence. They may become separate teams. Switching profile must change the working context, and the API must enforce profile access rather than treating the switch as a visual filter alone.
10. Release overview has Active and Completed views. Completed releases must retain dated evidence of what activities happened, when, and who recorded them. Completion is an explicit business transition, not merely reaching PROD. Historical records should be read-only through normal application workflows.
11. User/group access matrix: administrators can register an existing company identity for application access, assign users to groups and control activities groups may perform. Support access to one or both profiles. Registering an application user does not create an Entra directory account. Permissions should be enforced by the API and reflected in the UI. Access changes must be audited.
12. Embedded chat for questions such as “What is the status of defect 1234?”, “Is SCTASK 1234 completed in QAT?”, and “What is pending in QAT?” Answers must come from authorized release records, identify the environment/source and disclose freshness or ambiguity. Never invent a completion status or treat a successful build as proof of deployment.
13. Enterprise reliability: high availability, durable state, recoverable background work, auditability, security and operational documentation. A single physical server cannot provide host-level high availability. Production readiness must be established by deployment and failure testing, not claimed from a local demo.

## Expected workflows

### Release and activity coordination

Select M&O or CMS, then a year/quarter and release. Show both release numbers and the ServiceNow change reference. Import the release’s ServiceNow activities. Display category, environment, owner, stakeholder email, source ticket, status and execution notes. Preserve previous-environment work when preparing the next environment. Generate the consolidated next-environment release task and related stakeholder email drafts. Queue external operations through durable workers; track their outcomes and avoid duplicate creation on retries.

Correlate incoming replies with the original message and expected stakeholder. Store replies as evidence. A reply alone must not automatically approve readiness or change a ServiceNow task status. Record an authorized reviewer’s explicit approval or blocker decision. Complete a promotion only after the required activities, source evidence, stakeholder decisions and deployment evidence pass the gates.

### Defect traceability

Connect Jira issue → Git commit/revision → Jenkins build → confirmed deployment in a specific environment. A merged commit or successful build alone is insufficient proof of deployment. Prefer an archived deployment manifest containing environment, repository, exact revision, issue keys and deployment outcome. Preserve failed attempts and distinguish Jira workflow status from deployment status. Queries should replace manual Excel lookups, while historical Excel import is a future decision, not an assumed implemented feature.

### Completion and history

Close a release after PROD verification, with an explicit completion summary, timestamp and actor. Reject closure with unfinished activities, unresolved promotion work, outstanding/uncertain integration operations or missing deployment evidence. Show completed releases separately from active work. Allow inspection of historical notes, environment promotions, deployments, stakeholder messages/replies and decisions. Use recorded timestamps honestly: an import timestamp is not necessarily the actual execution time. Never silently rewrite completed history during background sync.

### Access management

Suggested permissions: view release/history/chat; create releases; add activities; update activity status; add notes; prepare promotions; complete promotions; close releases; sync integrations; create ServiceNow tasks; send email; review stakeholder readiness; reconcile uncertain external operations; manage users/groups.

Users may belong to multiple groups. Groups carry explicit activity permissions and profile scope. Disabled users must lose access even if an existing browser session is still valid. Prevent accidental self-lockout during access administration. Use stable Entra object IDs for identity matching, not display names or email alone. Decide explicitly whether access administration is global or delegated per team; the initial implementation treats it as a global administrator capability. Carefully assess any fallback mapping from Entra Reader/Operator/Admin roles for users who are not yet registered in the application, because overly broad defaults could undermine profile isolation.

## Proposed design philosophy

- Build a modular application with a separate worker process, keeping shared state in Db2. Avoid unnecessary microservices and additional infrastructure until actual scale requires them.
- Keep clear ownership: ServiceNow owns synced task status, Jira owns issue workflow status, and deployment evidence establishes environment delivery. The command center owns coordination notes, permission assignments, review decisions and the consolidated operational view.
- Treat external operations as potentially ambiguous. Use durable queues, database claims/leases, idempotent identifiers and reconciliation. Do not promise exactly-once SMTP delivery: a connection failure after acceptance may leave the outcome uncertain.
- Make automation reviewable. Draft preparation is safe to automate; sending, exporting tasks and advancing release state need explicit authorized workflow rules. The application does not automatically deploy code just because a chat response suggests it.
- Enforce permissions and profile boundaries server-side for direct URLs, searches, chat, object lookups, background tasks and exports. UI visibility is a convenience, not the authorization boundary.
- Start chat with deterministic parsing and authorized structured queries. The existing concept does not require an external LLM, vector database or free-form generated SQL. An approved model may later interpret language into a narrow validated query contract; it must not grant access or execute arbitrary SQL/actions.
- Keep historical evidence auditable. Distinguish execution time, source update time, ingestion time and decision time. Audit identity and authorization changes separately from release events.
- Label the demo honestly. Local SQLite and sample data support development only. Db2 compatibility, HA and company integrations require explicit validation in the approved environment.

## Infrastructure proposal to review

Company HTTPS VIP / redundant load balancers → at least two application hosts running stateless FastAPI services → shared Db2 HA endpoint. Run integration workers as separately supervised native services, potentially on the same two hosts initially. Linux systemd examples are supplied; the company’s server OS and service supervisor still need confirmation. No Kubernetes is involved.

For Db2 LUW, consider DBA-operated HADR with an approved failover manager and client reconnect strategy. Verify licensing, supported topology, quorum/fencing, failover procedures, backup retention and point-in-time recovery. HA replication is not a substitute for backups. Do not assume HADR performs all failover management by itself.

Use shared database-backed sessions, worker leases and email cursors to avoid host-local coordination. Restrict metrics and administration endpoints appropriately at the network edge. Separate liveness from database readiness; add worker health, queue age, sync freshness and uncertain-operation alerts. Document rolling deployments, schema migration sequencing, rollback, secret rotation, certificate renewal, backup restoration and disaster recovery. Agree actual RTO/RPO and availability targets with the business; none have been specified yet.

## Application structure to examine

```text
app/
  main.py           FastAPI routes, authorization integration, health and metrics
  config.py         Environment configuration and live-mode checks
  auth.py           Entra OIDC, browser sessions, optional API bearer tokens
  access.py         User/group permissions, profile grants, access audit
  db.py             SQLAlchemy Core schema, transactions and lock helpers
  schemas.py        Validated API request contracts
  catalog.py        Profile/release metadata, lifecycle and execution history
  service.py        Promotion workflow, snapshots and deployment recording
  connectors.py     ServiceNow, Jira, GitHub and Jenkins adapters
  mail.py           SMTP delivery, IMAP retrieval and correlation
  worker.py         Durable background jobs, scheduling and worker leases
  chat.py           Read-only structured status question answering
  seed.py           Fictional local demo records
  schema.py         Offline Db2 DDL generation for DBA review
  static/           Browser interface: HTML, CSS and JavaScript
tests/              Workflow, authorization, isolation and lifecycle tests
infra/              Native-service, load-balancer, Db2 and monitoring examples
docs/               Operations, integrations, infrastructure and review material
```

The data model includes releases, profiles, quarterly release metadata with two numbers, completion records, activities, notes, Jira defects, commits, deployment evidence, promotions, messages, replies, export tracking, durable jobs, audit events, authentication sessions, OAuth states, shared leases/cursors, users, groups, memberships and schema versions. Examine the actual committed schema rather than assuming this list is complete or deployed.

## Current implementation limits and open decisions

There is a working local FastAPI demo with existing workflow/integration code and automated tests. Live company credentials and connectivity have not been provided, no company infrastructure has been provisioned, and no live Db2/HA certification has been performed. Profiles, quarterly numbering, access management and completed-release views were requested after the initial demo; consult CHECKPOINT.md and the current commit to distinguish completed backend work from remaining interface work.

Confirm: exact meaning/format of the two release numbers; whether every activity belongs to the whole quarterly release or specifically one of its two numbers; whether both numbers share one promotion schedule or progress independently; fiscal versus calendar quarters; number of releases allowed per profile per quarter; handling of cancellations, emergency releases and corrections to closed history; future profile/team creation and renaming; global versus per-profile administrator delegation; ServiceNow custom fields, SCTASK versus CTASK table coverage; Jira/ServiceNow/Jenkins/GitHub enterprise versions; expected volume and retention; server OS; approved reverse proxy; Db2 platform and HA standard; secrets manager; mailbox policy; Entra app-role/group provisioning; notification timing and approval rules; RTO/RPO; accessibility and audit retention requirements.

## Requested review output

1. Restate the requirements and flag ambiguities that materially affect the data model.
2. Review the actual implementation. List concrete correctness/security/reliability issues by severity, with file/function references where possible. Do not claim tests or deployments you did not perform.
3. Evaluate profile isolation, the two-number quarterly release model, user/group authorization and immutable completed history. Identify race conditions and authorization bypasses, including stale sessions and indirect search/chat paths.
4. Evaluate the job/email/external-write design under crashes, retries, duplicated webhooks, stale leases and partial failures. Identify where reconciliation is required.
5. Propose the simplest robust on-premises architecture that respects Python/FastAPI, Db2, Entra, SMTP/IMAP and the absolute no-Kubernetes constraint.
6. Give a prioritized plan: defects to fix before company use, MVP completion, production hardening and optional later capabilities. Include meaningful acceptance tests and a failure-test plan.
7. Explain trade-offs plainly. Avoid adding infrastructure or AI complexity without a demonstrated need. Identify assumptions rather than presenting them as confirmed user requirements.
