"""Generate the initial Db2 LUW DDL for DBA review; never connects to a database."""
import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.schema import CreateIndex, CreateTable

from .db import metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--version', choices=['001', '002'], default='001')
    args = parser.parse_args()
    engine = create_engine('db2+ibm_db://DDL_ONLY:NOT_A_CREDENTIAL@localhost:50000/RCCDB')
    blocks = [f'-- Release Command Center / Db2 LUW schema / version {args.version}',
              '-- Generated for DBA review. Apply 001 to an empty schema, then apply 002 once.',
              '-- Assumption: Db2 LUW with UTF-8 database and DBA-approved tablespace/index page sizes.',
              '-- DDL can commit independently on Db2; use DBA change control and verify every statement.']
    added = {'release_closures', 'access_users', 'access_groups', 'access_memberships',
             'access_audit', 'access_control', 'profiles', 'release_details'}
    tables = [table for table in metadata.sorted_tables if (table.name in added) == (args.version == '002')]
    for table in tables:
        blocks.append(str(CreateTable(table).compile(dialect=engine.dialect)).strip() + ';')
    for table in tables:
        for index in sorted(table.indexes, key=lambda item: item.name):
            blocks.append(str(CreateIndex(index).compile(dialect=engine.dialect)).strip() + ';')
    if args.version == '001':
        blocks.extend(["INSERT INTO leases (id, owner, expires_at) VALUES ('imap', '', 0);",
                       "INSERT INTO schema_version (id, \"version\", applied_at) VALUES ('schema', '001', '2026-11-06T00:00:00+00:00');"])
    else:
        from .access import DEFAULT_GROUPS
        blocks.append("INSERT INTO access_control (id, revision) VALUES ('policy', 0);")
        for profile_id, name in [('m_o', 'M&O'), ('cms', 'CMS')]:
            blocks.append(f"INSERT INTO profiles (id, name, description) VALUES ('{profile_id}', '{name}', '{name} development track');")
        for group_id, name, description, permissions in DEFAULT_GROUPS:
            grants = json.dumps(sorted(permissions))
            blocks.append(f"INSERT INTO access_groups (id, name, description, permissions, profile_ids, revision) VALUES ('{group_id}', '{name}', '{description}', '{grants}', '[\"m_o\", \"cms\"]', 0);")
        blocks.append("UPDATE schema_version SET \"version\" = '002', applied_at = '2026-11-06T00:00:00+00:00' WHERE id = 'schema' AND \"version\" = '001';")
    Path(args.output).write_text('\n\n'.join(blocks) + '\n', encoding='utf-8')
    engine.dispose()


if __name__ == '__main__':
    main()
