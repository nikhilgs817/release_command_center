# Checkpoint — 6 November 2026

This is a tested development checkpoint, not a production-certified deployment. The user requested an immediate push of ready work and a complete prompt for independent review.

## Ready in this checkpoint

- FastAPI application, local demo dashboard, activity notes/status, promotion preparation and gates, defect traceability, stakeholder drafts/replies/review, audit display and structured read-only chat.
- Integration adapters and background worker code for ServiceNow change tasks, Jira, GitHub, Jenkins and company SMTP/IMAP. These have not been connected to company systems.
- Entra authentication code and Db2 schema/deployment examples. No Kubernetes.
- Backend M&O and CMS profiles, quarterly metadata, two distinct release numbers per release, profile-aware list/search/chat/object authorization. The create-release form accepts the profile, year, quarter and both numbers.
- Backend user registration by Entra object ID, groups, group permissions and profile scopes, account disabling, optimistic edit conflict checks, self-lockout protection and access auditing.
- Backend Active/Completed filtering, explicit closure with PROD gates, closure timestamp/actor/summary, dated history endpoint and read-only protection after closure. Scheduled source polling skips closed releases.
- 28 automated tests pass on local SQLite. Tests cover original workflows plus profile metadata, profile isolation, immediate permission changes, disabled sessions, access conflicts, self-lockout and completion protection.

## Interface work still pending

- A dedicated M&O/CMS profile switcher and profile-specific release picker. The existing release picker currently lists accessible releases together.
- Active and Completed overview tabs and a full dated execution-history screen. The API supports these; the existing UI still displays the original overview and recent audit events.
- Users & access screen with group membership editing and a permission matrix. The management endpoints exist, but this UI has not yet been wired.
- Permission-aware controls throughout the existing interface. The API rejects unauthorized actions, but some buttons remain visible to users who cannot perform them.
- A completion dialog and close-release action in the interface.

## API additions

| Operation | Endpoint |
| --- | --- |
| Available profiles and current permissions | `GET /api/config` |
| Releases by profile and lifecycle | `GET /api/releases?profile_id=m_o&status=active` |
| Completed releases | `GET /api/releases?profile_id=cms&status=completed` |
| Full recorded execution timeline | `GET /api/releases/{id}/history` |
| Close after PROD checks | `POST /api/releases/{id}/close` with `summary` |
| Access matrix and access audit | `GET /api/access` |
| Add/edit application user | `POST /api/access/users`, `PUT /api/access/users/{id}` |
| Add/edit permission group | `POST /api/access/groups`, `PUT /api/access/groups/{id}` |

Create-release requests now require `profile_id`, `year`, `quarter`, `number_one` and `number_two`, as well as the existing name/version. The two numbers currently describe one shared release lifecycle; independent promotion schedules per number require a business decision and model change. They are not the ServiceNow change number.

## Production migration

For a new Db2 schema, apply `infra/db2/001-initial.sql`, then `infra/db2/002-profiles-access-history.sql`, under DBA change control. For an existing version 001 schema, apply only 002. Readiness now requires version 002. DDL was generated offline and has not been executed against a live Db2 installation. Plan a maintenance deployment with API and worker processes stopped during this initial migration; a zero-downtime migration strategy has not been certified.

Demo initialization adds the new tables automatically without deleting the existing local database. Existing live releases without profile metadata are treated as M&O with unspecified quarter/numbers; explicitly review and backfill that mapping before enabling multiple teams. Do not silently apply the fictional demo identifiers to live releases.

## Limits requiring review before company use

- Entra roles map to default permissions for users not explicitly registered in the application; those defaults span both profiles. Register users with scoped groups before relying on team isolation. A managed account's group grants override the Entra role, and disabling it blocks its existing session. Decide whether production should require explicit provisioning for everyone.
- `access.manage` is global administration, even when assigned through a profile-scoped group. Delegated team administration is not implemented.
- No profile creation/rename workflow yet; M&O and CMS are seeded profiles.
- Current integration credentials and the Jenkins webhook secret are application-wide; independent team service identities and profile-scoped webhook credentials remain a hardening task.
- Closed releases reject subsequent normal writes. Late correlated replies for closed releases are currently ignored rather than stored as post-closure evidence; agree a retention/correction policy before production.
- Closure checks recorded activity status and deployment evidence but does not perform a fresh source-system validation at close time. Environment promotion has its own live freshness checks. Review required closure freshness semantics.
- Historical time values represent when evidence was recorded in this application unless the source supplies a meaningful timestamp. The system cannot reconstruct unrecorded past transitions.
- Release catalog and full history need server-side pagination at enterprise volumes. The generic search currently has a bounded release scan; review global-search completeness for larger portfolios.
- Live Db2 driver behavior, concurrency, failover, backup/restore, source API mappings, SMTP/IMAP permissions, Entra configuration, retention and load/security testing remain unverified.
- The checked-in native-service and load-balancer configurations are examples, not provisioned infrastructure or an availability guarantee.

Use [SECOND_OPINION_PROMPT.md](SECOND_OPINION_PROMPT.md) for independent architectural and implementation review.
