"""API-7: does the code name columns the database really has?

FakeDB accepts any column, so a passing test suite proves nothing about this, and the bug
has shipped before. This reads every .table("x") chain under app/ (select, insert, update,
upsert and filters) and compares it with the schema PostgREST reports for the `app` schema.
Reads the schema only; writes nothing.

    uv run python scripts/check_db_columns.py                run against .env's database
    TAUKA_ENV_FILE=.env.demo uv run python scripts/...       against the local stack

Exit code 1 on any mismatch. It can't see a column name built at runtime, or a filter added
to a query held in a variable, so zero mismatches is strong evidence, not proof.
"""
import ast
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import settings  # noqa: E402

FILTERS = {"eq", "neq", "lt", "lte", "gt", "gte", "in_", "is_", "like", "ilike", "order"}
HELPERS = {"_t", "_app", "_tbl", "_table"}
# Not in the app schema on purpose. Keep the reason with each one.
KNOWN_ABSENT = {
    "usage_logs": "lives in the public schema (usage_service uses the default client)",
    "fsi_lessons": "not built; question_bank_service falls back to a stub, as its comment says",
}


def _names(select: str) -> list[str]:
    """Top-level column names only. Embedded (...) resources belong to other tables."""
    flat = select
    while "(" in flat:
        reduced = re.sub(r"[\w:!]*\([^()]*\)", "", flat)
        if reduced == flat:
            break
        flat = reduced
    out = []
    for part in flat.split(","):
        name = part.strip().split(":")[-1].strip()
        if name and name != "*" and re.fullmatch(r"\w+", name):
            out.append(name)
    return out


def references() -> dict[str, dict[str, str]]:
    refs: dict[str, dict[str, str]] = {}
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf8"))
        rel = path.relative_to(ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            chain, cur = [], node
            while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
                chain.append(cur)
                cur = cur.func.value
            table = None
            for call in chain:
                if call.func.attr == "table" and call.args and isinstance(call.args[0], ast.Constant):
                    table = call.args[0].value
            if isinstance(cur, ast.Call) and isinstance(cur.func, ast.Name) and cur.func.id in HELPERS \
                    and cur.args and isinstance(cur.args[0], ast.Constant):
                table = cur.args[0].value
            if not table:
                continue
            for call in chain:
                attr, args = call.func.attr, call.args
                if not args:
                    continue
                columns: list[str] = []
                if attr == "select" and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                    columns = _names(args[0].value)
                elif attr in ("insert", "update", "upsert") and isinstance(args[0], ast.Dict):
                    columns = [k.value for k in args[0].keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                elif attr in FILTERS and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                    columns = [args[0].value.split("->")[0]]
                for column in columns:
                    refs.setdefault(table, {}).setdefault(column, f"{rel}:{call.lineno}")
    return refs


def live_schema() -> dict[str, set[str]]:
    res = httpx.get(f"{settings.supabase_url.rstrip('/')}/rest/v1/", timeout=30,
                    headers={"apikey": settings.supabase_key, "Authorization": f"Bearer {settings.supabase_key}",
                             "Accept-Profile": "app"})
    res.raise_for_status()
    definitions = res.json().get("definitions") or {}
    return {name: set((d.get("properties") or {}).keys()) for name, d in definitions.items()}


def main() -> int:
    schema, refs = live_schema(), references()
    missing_tables, missing_columns, checked = [], [], 0
    for table, columns in sorted(refs.items()):
        if table in KNOWN_ABSENT:
            continue
        if table not in schema:
            missing_tables.append((table, sorted(set(columns.values()))[0]))
            continue
        for column, where in sorted(columns.items()):
            checked += 1
            if column not in schema[table]:
                missing_columns.append((table, column, where))
    print(f"{checked} column references across {len(refs)} tables, against {len(schema)} tables in the schema")
    for table, where in missing_tables:
        print(f"NO TABLE   app.{table}  ({where})")
    for table, column, where in missing_columns:
        print(f"NO COLUMN  app.{table}.{column}  ({where})")
    if not (missing_tables or missing_columns):
        print("No mismatches.")
    return 1 if missing_tables or missing_columns else 0


if __name__ == "__main__":
    if os.environ.get("PYTHONUTF8") != "1":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
