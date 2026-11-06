# Server deployment and recovery runbook

## Preparation

Use two company-managed Linux application VMs, a redundant load-balancer VIP, an existing Db2 HA service, a dedicated mailbox and scoped integration service accounts. The templates do not provision these systems. They do not require Kubernetes or containers. Have the infrastructure team confirm separate failure domains, certificate trust, network flows and a writable Db2 failover endpoint.

The DBA should confirm that the target is Db2 LUW, review `infra/db2/001-initial.sql`, select an application schema and grant the runtime account only the required data access. Run initial DDL using a separate DBA/change-management account. Application/worker startup never creates the production schema. Keep the versioned SQL file immutable after adoption; future migrations must be new reviewed files. Only mark schema version 001 after all statements and the IMAP lease row have succeeded.

The DDL was generated/compiled locally, not executed against Db2. Confirm quoted identifiers, schema resolution, indexes, CLOB behavior, row counts, bound `FETCH FIRST` queries, transactions and the supported driver combination on an integration database first. The runtime account's default schema must resolve the application tables, or configure the approved IBM CLI schema settings. A production `/health/ready` check requires schema version 001.

## Install on each application VM

1. Install the company-approved Python 3.12 runtime, the IBM CLI dependencies required for `ibm_db`, corporate CA certificates and OS updates. Follow IBM's packaging/licensing requirements for the exact platform.
2. Create an unprivileged service account `releasecc` with no interactive login. Put the versioned application source under `/opt/release-command-center/releases/<version>` and point `/opt/release-command-center/current` to that release. Keep source owned by the deployment account, read-only to `releasecc`.
3. Create `/opt/release-command-center/venv` using the approved Python interpreter, and install `requirements-db2.txt`. Resolve and lock the approved dependencies in your internal package repository before promotion; `requirements.lock.txt` records this development environment's versions and must be reviewed for your OS.
4. Put the server configuration at `/etc/release-command-center/app.env`, owned by root and readable only by the service group. Inject credentials through the approved secret manager where possible. Do not copy real credentials into this project.
5. Put each backend's corporate certificate and private key at the paths in the API service template. Grant the service account read access to its own key only. Load balancers must verify the issuing CA and host name.
6. Review/copy the two `infra/systemd/*.service` units into the company service configuration. Adapt paths, resource limits and OS hardening flags to the approved Linux distribution. The IBM driver must remain functional with a read-only filesystem and private temporary directory.
7. Enable/start API and worker services, verify readiness on each host with corporate CA verification, then add the backends to the load balancer. Restrict direct access with firewalls first.

The production environment must contain at least:

```text
DEMO_MODE=false
ALLOW_EXTERNAL_WRITES=false
PUBLIC_URL=https://release.company.example
DATABASE_URL=<secret Db2 TLS URL for the approved HA endpoint>
ENTRA_TENANT_ID=<actual tenant GUID>
ENTRA_AUDIENCE=<actual application client ID>
ENTRA_CLIENT_SECRET=<secret supplied by company secret management>
WEBHOOK_TOKEN=<separate high-entropy secret supplied to Jenkins>
TRUSTED_PROXY_IPS=<comma-separated load balancer source IPs>
```

Add source/mail settings from `.env.example`. Keep external writes disabled for the first read-only test. Confirm the URL parser and IBM driver accept the selected TLS/CA configuration without disabling certificate verification. URL-encode special characters in database credentials. Synchronize clocks on all hosts.

## Entra setup

Create a single-tenant web registration. Register the exact redirect URI `https://<your-host>/auth/callback`; no wildcard callbacks. Create the app roles `Release.Reader`, `Release.Operator` and `Release.Admin`; assign users/groups through the enterprise application. Require assignment, apply the company conditional-access/MFA policy, and set up secret rotation. The sign-in client expects v2 issuer/token metadata. Test all three roles and an unassigned user.

The browser sign-in uses an ID token only to establish a local, opaque session. If direct bearer API access is enabled for separate clients, use an Entra API registration/audience, a delegated API scope or workload role, and adapt the token validation/consent model to that registration before issuing such clients. The current browser workflow does not need direct bearer clients; Jenkins uses its isolated webhook token.

Sessions expire no later than the sign-in token's expiry, requiring sign-in again. There is no refresh-token persistence. Entra role changes take effect on the next sign-in or session invalidation. Establish an administrative session-revocation procedure before production rollout.

## Enable the workflow

Create a release with its exact ServiceNow change number and release-scoped Jira JQL. Configure the ServiceNow environment field and validate numeric task-state mappings. If an environment field is unavailable, submit explicit task numbers; periodic ServiceNow polling is intentionally disabled in that case.

Sync Jira and an explicit GitHub baseline/head comparison. Archive `release-manifest.json` from a trusted Jenkins deployment job. Set the CI webhook token or use the Jenkins read adapter to import the build's archived manifest. Test a failed build, a successful build without deployment confirmation and a confirmed deployment separately.

After current-environment tasks are complete, prepare the next environment. Review the consolidated ServiceNow release task and individual activity drafts. Enable `ALLOW_EXTERNAL_WRITES=true` only once the ticket and mail previews, accounts and destination environments have been verified. Queue the release task creation from its review screen. To automate stakeholder email queueing during preparation, also set `AUTO_QUEUE_EMAIL=true`. Workers send queued mail and poll replies independently of the web process.

Source synchronization is read-only except explicit ServiceNow task export. Execution notes are stored in this app; this version does not append them back into ServiceNow journal fields. Deployment evidence is ingested; deployment jobs and database scripts are not executed by the app.

## Rolling releases

1. Build/test an immutable version and review the dependency changes. Back up Db2 and verify the restore point before schema changes.
2. Apply only backward-compatible, approved schema migrations. Version 001 is the initial baseline.
3. Drain application server 1 from the load balancer and stop its worker. Let in-flight HTTP requests finish.
4. Deploy the new version, restart its API and worker, verify `/health/ready`, sign-in and a read-only lookup, and restore traffic.
5. Repeat for server 2. Confirm both workers are healthy and no jobs or external writes are stranded.

For rollback, restore the previous source/dependency release on one server at a time. Database rollback requires the DBA-approved migration plan. Do not restore an old database merely to roll back an API binary: it could discard valid release work or cause an external message/ticket to be repeated.

## Recovery procedures

### App server failure

The load balancer removes a backend after failed readiness checks. The other API continues serving shared Db2 state. Its worker can claim pending work. Read-only jobs use a two-minute lease renewed every 30 seconds; after a crashed worker stops renewal, another worker can reclaim the work. Source writes check the current claim within their database transaction so a stale worker cannot commit an obsolete source result. External-write jobs become uncertain when a lease expires and are not automatically repeated.

Restart/rebuild the failed server from the approved artifact and restore it only after readiness checks pass. If the failed node was the current IMAP reader, another worker can take the mailbox lease after approximately 90 seconds plus its polling interval. The reader renews ownership while processing. Existing ticket/defect data remains readable while mail catches up.

### Database failover or outage

Use the DBA cluster's supported failover procedure and fencing. Confirm there is exactly one writable primary. Affected transactions can fail and must be retried at the application/domain level only when safe. The connection pool replaces disconnected connections. Check both API readiness endpoints, job claims and mailbox cursor progress after recovery.

Never replay external writes blindly. If failover occurs after ServiceNow/SMTP accepts a request but before the app records success, follow the uncertain-write procedures below.

### Uncertain SMTP delivery

Find the message's stable Message-ID and recipient in the saved record. Check sent items and the company SMTP trace. An administrator can call `POST /api/messages/{id}/resolve-delivery` with `status="sent"` when delivery is confirmed, or `status="draft"` only when non-delivery is confirmed, and a reason recording the evidence. Requeueing a draft is a separate action. A positive stakeholder reply can also be correlated without resending the original message.

If the mail system cannot establish an outcome, keep the record uncertain. SMTP acceptance is not proof of final delivery; bounces and mailbox rules are operational considerations. The code does not retry an uncertain send automatically.

### Uncertain ServiceNow creation

Search ServiceNow for `correlation_id=RCC-<activity-id>` or `RCC-PROMOTION-<promotion-id>`. Activity reconciliation is available through `POST /api/activities/{id}/reconcile-export` for administrators. Consolidated release-task reconciliation uses `POST /api/releases/{release_id}/promotions/{id}/service-task?reconcile=true` and only searches/links an existing source record.

If no source record can be established, review ServiceNow transaction/audit logs before any reset or retry. This starter deliberately does not offer an automatic reset-and-repeat-POST action. A crash can leave an export marked creating or a job marked uncertain; its stable marker still permits safe read reconciliation.

### Restore from backup

Restore into an isolated environment with **external writes disabled**. Confirm schema version, release/task counts, audit history, job claims, message states, mailbox UIDVALIDITY/cursors and source correlation markers. Reconcile every in-flight external write before resuming workers. Validate restoration time against the agreed RTO and the recovered timestamp against RPO. Test this quarterly or according to company policy.

## Acceptance tests with company infrastructure

- Run real CRUD, duplicate-key, transaction-rollback, row-lock and concurrent-prepare tests on the exact Db2 platform; exercise the IBM driver's failover/reroute behavior.
- Stop one application server during reads and during a queued sync; confirm the other server serves users and eventually recovers work.
- Terminate a worker before and after each external side effect. Confirm no automatic duplicate email or ServiceNow POST and verify reconciliation.
- Fail a load-balancer node using the existing infrastructure procedure; verify VIP continuity.
- Perform a DBA-managed Db2 failover while reading and while updating a release; verify recovery, one writable primary and no corrupted transition.
- Simulate ServiceNow/Jira/Jenkins/GitHub rate limits/timeouts, SMTP ambiguity, wrong-sender replies, duplicate replies and mailbox UIDVALIDITY changes.
- Verify Reader/Operator/Admin permissions, expired/wrong-tenant tokens, cross-origin writes, CSRF, logout, TLS trust and secret rotation.
- Load test realistic release sizes and concurrent users; set resource and connection limits from observed results. Test large notes/mail bodies and set limits at the company edge.
- Restore a backup on an isolated host and prove recovery time/data retention. Scan dependencies and review application security with the company process.

## Known operational scope

This starter has no production load/fault-injection certification, external APM integration, full-text search index, historical data partitioning, attachment ingestion, automated bounce processing, automatic schema upgrades, archival policy or immutable audit export. HTTP API metrics and database worker/queue gauges are included; configure additional alerts for job age, source freshness, external mail/ticket reconciliation and Db2 health. Retention, session cleanup and audit forwarding to company monitoring must be added before long-running production use.
