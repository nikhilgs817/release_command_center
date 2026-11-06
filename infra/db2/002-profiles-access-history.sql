-- Release Command Center / Db2 LUW schema / version 002

-- Generated for DBA review. Apply 001 to an empty schema, then apply 002 once.

-- Assumption: Db2 LUW with UTF-8 database and DBA-approved tablespace/index page sizes.

-- DDL can commit independently on Db2; use DBA change control and verify every statement.

CREATE TABLE access_audit (
	id VARCHAR(32) NOT NULL,
	actor VARCHAR(255) NOT NULL,
	action VARCHAR(80) NOT NULL,
	details CLOB NOT NULL,
	created_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE access_control (
	id VARCHAR(32) NOT NULL,
	revision INT NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE access_groups (
	id VARCHAR(32) NOT NULL,
	name VARCHAR(180) NOT NULL,
	description CLOB NOT NULL,
	permissions CLOB NOT NULL,
	profile_ids CLOB NOT NULL,
	revision INT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name)
);

CREATE TABLE access_users (
	id VARCHAR(32) NOT NULL,
	principal VARCHAR(255) NOT NULL,
	name VARCHAR(180) NOT NULL,
	email VARCHAR(255) NOT NULL,
	enabled INT NOT NULL,
	revision INT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (principal)
);

CREATE TABLE profiles (
	id VARCHAR(32) NOT NULL,
	name VARCHAR(180) NOT NULL,
	description CLOB NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name)
);

CREATE TABLE access_memberships (
	id VARCHAR(32) NOT NULL,
	user_id VARCHAR(32) NOT NULL,
	group_id VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (user_id, group_id),
	FOREIGN KEY(user_id) REFERENCES access_users (id),
	FOREIGN KEY(group_id) REFERENCES access_groups (id)
);

CREATE TABLE release_closures (
	id VARCHAR(32) NOT NULL,
	release_id VARCHAR(32) NOT NULL,
	completed_at VARCHAR(40) NOT NULL,
	completed_by VARCHAR(255) NOT NULL,
	"summary" CLOB NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (release_id),
	FOREIGN KEY(release_id) REFERENCES releases (id)
);

CREATE TABLE release_details (
	id VARCHAR(32) NOT NULL,
	release_id VARCHAR(32) NOT NULL,
	profile_id VARCHAR(32) NOT NULL,
	"year" INT,
	quarter INT,
	number_one VARCHAR(80) NOT NULL,
	number_two VARCHAR(80) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (release_id),
	FOREIGN KEY(release_id) REFERENCES releases (id),
	FOREIGN KEY(profile_id) REFERENCES profiles (id)
);

CREATE INDEX ix_release_closures_release_id ON release_closures (release_id);

CREATE INDEX ix_release_details_release_id ON release_details (release_id);

INSERT INTO access_control (id, revision) VALUES ('policy', 0);

INSERT INTO profiles (id, name, description) VALUES ('m_o', 'M&O', 'M&O development track');

INSERT INTO profiles (id, name, description) VALUES ('cms', 'CMS', 'CMS development track');

INSERT INTO access_groups (id, name, description, permissions, profile_ids, revision) VALUES ('readers', 'Readers', 'Browse release records and ask status questions.', '["release.view"]', '["m_o", "cms"]', 0);

INSERT INTO access_groups (id, name, description, permissions, profile_ids, revision) VALUES ('operators', 'Release operators', 'Execute release activities and coordinate promotions.', '["activity.create", "activity.note", "activity.update", "email.send", "integration.sync", "promotion.complete", "promotion.prepare", "release.create", "release.view", "stakeholder.review", "ticket.export"]', '["m_o", "cms"]', 0);

INSERT INTO access_groups (id, name, description, permissions, profile_ids, revision) VALUES ('admins', 'Administrators', 'Manage application access and all release activities.', '["access.manage", "activity.create", "activity.note", "activity.update", "email.send", "integration.reconcile", "integration.sync", "promotion.complete", "promotion.prepare", "release.close", "release.create", "release.view", "stakeholder.review", "ticket.export"]', '["m_o", "cms"]', 0);

UPDATE schema_version SET "version" = '002', applied_at = '2026-11-06T00:00:00+00:00' WHERE id = 'schema' AND "version" = '001';
