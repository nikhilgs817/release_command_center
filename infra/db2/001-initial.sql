-- Release Command Center / initial Db2 LUW schema / version 001

-- Generated for DBA review. Execute only against an empty, approved application schema.

-- Assumption: Db2 LUW with UTF-8 database and DBA-approved tablespace/index page sizes.

-- DDL can commit independently on Db2; use DBA change control and verify every statement.

CREATE TABLE auth_sessions (
	id VARCHAR(32) NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	principal VARCHAR(255) NOT NULL,
	roles CLOB NOT NULL,
	expires_at INT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (token_hash)
);

CREATE TABLE leases (
	id VARCHAR(80) NOT NULL,
	owner VARCHAR(32) NOT NULL,
	expires_at INT NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE mail_cursors (
	id VARCHAR(32) NOT NULL,
	mailbox VARCHAR(255) NOT NULL,
	uidvalidity VARCHAR(80) NOT NULL,
	last_uid INT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (mailbox)
);

CREATE TABLE oauth_states (
	id VARCHAR(32) NOT NULL,
	nonce VARCHAR(32) NOT NULL,
	verifier VARCHAR(96) NOT NULL,
	expires_at INT NOT NULL,
	used INT NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE releases (
	id VARCHAR(32) NOT NULL,
	name VARCHAR(180) NOT NULL,
	"version" VARCHAR(80) NOT NULL,
	current_env VARCHAR(16) NOT NULL,
	change_number VARCHAR(80) NOT NULL,
	change_sys_id VARCHAR(80) NOT NULL,
	jira_jql CLOB NOT NULL,
	created_at VARCHAR(40) NOT NULL,
	revision INT NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE schema_version (
	id VARCHAR(32) NOT NULL,
	"version" VARCHAR(32) NOT NULL,
	applied_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE activities (
	id VARCHAR(32) NOT NULL,
	release_id VARCHAR(32) NOT NULL,
	environment VARCHAR(16) NOT NULL,
	number VARCHAR(80) NOT NULL,
	external_id VARCHAR(80),
	title VARCHAR(500) NOT NULL,
	category VARCHAR(80) NOT NULL,
	owner VARCHAR(180) NOT NULL,
	email VARCHAR(255) NOT NULL,
	status VARCHAR(32) NOT NULL,
	"source" VARCHAR(32) NOT NULL,
	copied_from VARCHAR(32),
	updated_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(release_id) REFERENCES releases (id)
);

CREATE TABLE audit_log (
	id VARCHAR(32) NOT NULL,
	release_id VARCHAR(32) NOT NULL,
	actor VARCHAR(255) NOT NULL,
	action VARCHAR(80) NOT NULL,
	details CLOB NOT NULL,
	created_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(release_id) REFERENCES releases (id)
);

CREATE TABLE commits (
	id VARCHAR(32) NOT NULL,
	release_id VARCHAR(32) NOT NULL,
	repository VARCHAR(255) NOT NULL,
	sha VARCHAR(64) NOT NULL,
	subject VARCHAR(500) NOT NULL,
	issue_keys CLOB NOT NULL,
	url VARCHAR(1024) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (release_id, repository, sha),
	FOREIGN KEY(release_id) REFERENCES releases (id)
);

CREATE TABLE defects (
	id VARCHAR(32) NOT NULL,
	release_id VARCHAR(32) NOT NULL,
	issue_key VARCHAR(80) NOT NULL,
	title VARCHAR(500) NOT NULL,
	status VARCHAR(80) NOT NULL,
	url VARCHAR(1024) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (release_id, issue_key),
	FOREIGN KEY(release_id) REFERENCES releases (id)
);

CREATE TABLE deployments (
	id VARCHAR(32) NOT NULL,
	release_id VARCHAR(32) NOT NULL,
	environment VARCHAR(16) NOT NULL,
	job VARCHAR(255) NOT NULL,
	build_number INT NOT NULL,
	repository VARCHAR(255) NOT NULL,
	sha VARCHAR(64) NOT NULL,
	"result" VARCHAR(32) NOT NULL,
	deployed INT NOT NULL,
	issue_keys CLOB NOT NULL,
	url VARCHAR(1024) NOT NULL,
	created_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (release_id, environment, job, build_number),
	FOREIGN KEY(release_id) REFERENCES releases (id)
);

CREATE TABLE jobs (
	id VARCHAR(32) NOT NULL,
	release_id VARCHAR(32) NOT NULL,
	kind VARCHAR(32) NOT NULL,
	payload CLOB NOT NULL,
	status VARCHAR(32) NOT NULL,
	attempts INT NOT NULL,
	available_at INT NOT NULL,
	lease_until INT NOT NULL,
	error VARCHAR(500) NOT NULL,
	created_at VARCHAR(40) NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(release_id) REFERENCES releases (id)
);

CREATE TABLE promotions (
	id VARCHAR(32) NOT NULL,
	release_id VARCHAR(32) NOT NULL,
	from_env VARCHAR(16) NOT NULL,
	to_env VARCHAR(16) NOT NULL,
	status VARCHAR(32) NOT NULL,
	"summary" CLOB NOT NULL,
	export_status VARCHAR(32) NOT NULL,
	service_task_id VARCHAR(80) NOT NULL,
	service_task_number VARCHAR(80) NOT NULL,
	created_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (release_id, from_env, to_env),
	FOREIGN KEY(release_id) REFERENCES releases (id)
);

CREATE TABLE messages (
	id VARCHAR(32) NOT NULL,
	release_id VARCHAR(32) NOT NULL,
	promotion_id VARCHAR(32) NOT NULL,
	activity_id VARCHAR(32) NOT NULL,
	recipient VARCHAR(255) NOT NULL,
	subject VARCHAR(500) NOT NULL,
	body CLOB NOT NULL,
	correlation VARCHAR(40) NOT NULL,
	message_id VARCHAR(255) NOT NULL,
	status VARCHAR(32) NOT NULL,
	created_at VARCHAR(40) NOT NULL,
	sent_at VARCHAR(40),
	decision VARCHAR(32) NOT NULL,
	error VARCHAR(500) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (promotion_id, activity_id),
	FOREIGN KEY(release_id) REFERENCES releases (id),
	FOREIGN KEY(promotion_id) REFERENCES promotions (id),
	FOREIGN KEY(activity_id) REFERENCES activities (id),
	UNIQUE (correlation),
	UNIQUE (message_id)
);

CREATE TABLE notes (
	id VARCHAR(32) NOT NULL,
	activity_id VARCHAR(32) NOT NULL,
	body CLOB NOT NULL,
	created_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(activity_id) REFERENCES activities (id)
);

CREATE TABLE task_exports (
	id VARCHAR(32) NOT NULL,
	activity_id VARCHAR(32) NOT NULL,
	status VARCHAR(32) NOT NULL,
	external_id VARCHAR(80) NOT NULL,
	error VARCHAR(500) NOT NULL,
	updated_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (activity_id),
	FOREIGN KEY(activity_id) REFERENCES activities (id)
);

CREATE TABLE replies (
	id VARCHAR(32) NOT NULL,
	message_id VARCHAR(32) NOT NULL,
	external_id VARCHAR(255) NOT NULL,
	sender VARCHAR(255) NOT NULL,
	body CLOB NOT NULL,
	received_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(message_id) REFERENCES messages (id),
	UNIQUE (external_id)
);

CREATE INDEX ix_activities_release_id ON activities (release_id);

CREATE INDEX ix_activity_external ON activities (release_id, environment, external_id);

CREATE INDEX ix_audit_log_release_id ON audit_log (release_id);

CREATE INDEX ix_commits_release_id ON commits (release_id);

CREATE INDEX ix_defects_release_id ON defects (release_id);

CREATE INDEX ix_deployments_release_id ON deployments (release_id);

CREATE INDEX ix_jobs_queue ON jobs (status, available_at);

CREATE INDEX ix_jobs_release_id ON jobs (release_id);

CREATE INDEX ix_promotions_release_id ON promotions (release_id);

CREATE INDEX ix_messages_release_id ON messages (release_id);

INSERT INTO leases (id, owner, expires_at) VALUES ('imap', '', 0);

INSERT INTO schema_version (id, "version", applied_at) VALUES ('schema', '001', '2026-11-06T00:00:00+00:00');
