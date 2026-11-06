# Integration contracts

## ServiceNow

The adapter uses the Table API and basic authentication with an integration account/token. Adapt authentication to company-approved OAuth if required; token refresh is not implemented. Use read access to scoped `change_request`, `change_task`, and `sys_user` records, plus scoped `change_task` create permission only when exports are enabled.

The parent change request is resolved by an exact `CHG` number. Activities are selected under that parent **and** an explicit environment field or exact CTASK list. The app never assumes that every task under a change belongs to the current environment. Tables are paged in a stable `sys_id` order. Verify `SERVICENOW_ENV_FIELD` is filterable and stores the same environment codes, or adapt the mapping.

Default task-state mapping: `3` complete; `2` in progress; `1` and `-5` pending; `4` cancelled. Unknown values are blocked. These are common change-task values, not a guaranteed company configuration. Verify/adapt the mapping before relying on promotion gates. Cancelled tasks do not satisfy the completion gate in this starter.

The app stores its own append-only execution notes. It reads status from ServiceNow for imported tasks. The consolidated next-environment change task has the previous environment's completed task numbers, descriptions, stakeholder names/emails and note snapshots in its description. It is created under the same parent change request; if each environment has a different parent change, add that relationship before rollout.

Creation correlation markers are `RCC-<activity id>` and `RCC-PROMOTION-<promotion id>`. Confirm the `correlation_id` field is available, writable and searchable by the integration account. A unique source-side constraint/business rule on this marker is recommended if your ServiceNow configuration supports it. Reconciliation reads the marker; uncertain creation is not automatically repeated.

Consolidated coordination tasks should be identified in the company's ServiceNow categorization/environment queries. Otherwise a periodic task sync may import the coordination task as another executable release activity. Agree whether those tasks are part of the completion gate and adapt the query/category mapping accordingly.

## Jira

The supplied adapter targets **Jira Cloud enhanced JQL search** at `/rest/api/3/search/jql`, using account email and API token, and follows `nextPageToken`. A release must have an explicit JQL scope, such as `project = PAY AND fixVersion = "2026.11.1"`.

If your Jira is Data Center/Server, replace the search/auth adapter with the installed version's supported REST API and startAt pagination. Jira's workflow status is displayed separately from deployment state. Closed/resolved issues do not imply deployment.

## GitHub

Supply an explicit repository, baseline and target branch/tag/full SHA. The adapter pages the compare endpoint, rejects diverged/behind comparisons and verifies the returned commit count before storing records. Commit messages containing Jira keys establish candidate issue/commit links. The exact head revision is also imported for an identical comparison.

This does not discover every issue from PR descriptions, squash metadata or a manually maintained Jira relationship. Enforce Jira keys in the pipeline's change manifest or extend the integration to read pull-request associations. GitHub Enterprise API base/version compatibility must be validated against your appliance; `GITHUB_API_URL` is configurable, while the supplied API version header targets GitHub's documented 2026-03-10 REST version.

## Jenkins and deployment manifests

The adapter reads a named build using the Jenkins Remote Access API and retrieves its archived `release-manifest.json`. Use a controlled deployment job, not an arbitrary compilation job. Archive the manifest after actual environment deployment and verification.

Example archived file:

```json
{
  "release_id": "APP_RELEASE_ID_FROM_COMMAND_CENTER",
  "environment": "INT",
  "repository": "payments/payment-service",
  "sha": "0123456789abcdef0123456789abcdef01234567",
  "deployed": true,
  "issue_keys": ["PAY-1842", "PAY-1856"]
}
```

The Jenkins adapter supplies the trusted build number, job name, build result and URL from the configured Jenkins endpoint. `deployed=true` is accepted only with `SUCCESS`. Jira issues and the exact repository/revision must already be known to this release. A replay for the same release/environment/job/build is idempotent only when its contents match; conflicting evidence is rejected.

Alternatively, Jenkins can POST a complete payload to `/api/releases/{id}/deployment-evidence` using `X-Webhook-Token`:

```json
{
  "environment": "INT",
  "job": "payments/deploy-int",
  "build_number": 284,
  "repository": "payments/payment-service",
  "sha": "0123456789abcdef0123456789abcdef01234567",
  "result": "SUCCESS",
  "deployed": true,
  "issue_keys": ["PAY-1842", "PAY-1856"],
  "url": "https://jenkins.company.example/job/payments/job/deploy-int/284/"
}
```

Store the token in Jenkins credentials and never print it. Construct JSON with a serializer rather than interpolating a shell string. Generate `issue_keys` from the deployed artifact's controlled change manifest, including cumulative fixes. The app trusts the authenticated pipeline's issue list; it cannot independently prove what code was physically deployed. Imported commit-message links are useful traceability but do not establish that an issue is included in an artifact by themselves.

Deployment history records environments ever reached. Rollbacks, superseded builds and multi-component release baselines need additional domain modeling if you want an authoritative "currently running" inventory. The app does not invoke Jenkins jobs in this version.

## Company SMTP / IMAP

Use a dedicated release-coordination mailbox. The sender address must be allowed for the configured SMTP account. The implementation uses authenticated SMTP with STARTTLS on port 587 and TLS IMAP on port 993, with certificate verification enabled.

The implementation expects password/application-password authentication. If company policy requires OAuth2/XOAUTH2 or prohibits SMTP AUTH, adapt this adapter and token acquisition before connecting. No email is sent from this development task.

Each draft contains a stable `[RCC:<32-character reference>]` and Message-ID. Incoming replies must match either that reference in the subject or an In-Reply-To/References header and the expected email address. The reply is saved as inert text, deduplicated by Message-ID and tied to its release/activity. HTML is not rendered. The current implementation does not persist attachments or cryptographically validate a sender independently of the company's mail system.

The mailbox is selected read-only. UIDVALIDITY/UID cursors are persisted in Db2 and the mailbox reader uses a shared lease. On UIDVALIDITY changes it safely rescans; Message-ID deduplication prevents replay of already captured replies. The reader processes up to 50 messages per cycle, including unrelated messages when advancing its cursor. Use a dedicated folder/mailbox to control backlog and retention.

Incoming replies never trigger a deployment, task completion or readiness approval. An operator records the decision and reason after reading the response. Company retention and access rules apply to stored stakeholder text.

## Primary API references

- [ServiceNow REST APIs](https://www.servicenow.com/docs/r/api-reference/rest-api-explorer/c_RESTAPI.html)
- [Atlassian JQL search](https://confluence.atlassian.com/jirakb/run-jql-search-query-using-jira-cloud-rest-api-1289424308.html)
- [GitHub commit/compare API](https://docs.github.com/en/rest/commits/commits)
- [Jenkins Remote Access API](https://www.jenkins.io/doc/book/using/remote-access-api/)
