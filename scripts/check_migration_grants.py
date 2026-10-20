#!/usr/bin/env python3
"""Fail if a migration creates a public table without granting Data API access.

Supabase no longer grants anon/authenticated/service_role access to new tables
in the `public` schema automatically (from 2026-10-30). A table created without
an explicit GRANT is unreachable from supabase-js / PostgREST, and the failure
only shows up at runtime. This check catches it at PR time.

Rule: every `create table` in public (unqualified or `public.`) must be followed,
in the same migration file, by a `grant ... on [table] [public.]<name> to ...`
that names both `authenticated` and `service_role`.

Migrations up to and including BASELINE are covered by the backfill migration
20261020130000_data_api_grants.sql and are skipped.

Usage: python3 scripts/check_migration_grants.py [migrations_dir]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BASELINE = "20261020130000"
REQUIRED_ROLES = {"authenticated", "service_role"}

CREATE_RE = re.compile(
    r"\bcreate\s+(?:unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?"
    r"(?:(?P<schema>\"?\w+\"?)\.)?\"?(?P<name>\w+)\"?",
    re.IGNORECASE,
)
GRANT_RE = re.compile(
    r"\bgrant\s+(?P<privs>[\w\s,]+?)\s+on\s+(?:table\s+)?(?P<targets>[\w\s,.\"]+?)"
    r"\s+to\s+(?P<roles>[\w\s,]+?)\s*;",
    re.IGNORECASE,
)


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", sql)


def check_file(path: Path) -> list[str]:
    sql = strip_comments(path.read_text())
    tables = {
        m["name"].lower()
        for m in CREATE_RE.finditer(sql)
        if m["schema"] is None or m["schema"].strip('"').lower() == "public"
    }
    granted: dict[str, set[str]] = {}
    for g in GRANT_RE.finditer(sql):
        roles = {r.strip().lower() for r in g["roles"].split(",")}
        for target in g["targets"].split(","):
            name = target.strip().strip('"').lower()
            if name.startswith("public."):
                name = name[len("public."):]
            granted.setdefault(name.strip('"'), set()).update(roles)

    errors = []
    for table in sorted(tables):
        missing = REQUIRED_ROLES - granted.get(table, set())
        if missing:
            errors.append(
                f"{path.name}: table public.{table} has no grant to "
                f"{', '.join(sorted(missing))}. Add, in the same migration:\n"
                f"  grant select, insert, update, delete on public.{table} "
                f"to authenticated, service_role;"
            )
    return errors


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "supabase/migrations")
    errors: list[str] = []
    for path in sorted(root.glob("*.sql")):
        if path.name[:14] <= BASELINE:
            continue
        errors.extend(check_file(path))
    for e in errors:
        print(f"::error::{e}")
    if not errors:
        print("Migration grants OK.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
