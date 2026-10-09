"""DuckDB-backed warehouse access with three independent safety layers.

1. guard.check: one SELECT over allowed tables, no file or engine functions.
2. The connection: the warehouse file is attached READ_ONLY, external access is
   disabled and the configuration is locked, so even a query that slips past the
   guard cannot read files, reach the network or re-enable either.
3. The data: queries only see views in the `analytics` schema, where PII columns
   are replaced by salted pseudonyms. Raw values are not reachable from SQL.

Then an EXPLAIN pre-flight rejects plans that are too big, a timer interrupts slow
queries, results are capped, and every call is written to an audit log.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from warehouse_mcp.guard import Rejected, check

SCHEMA = "analytics"
PII_COLUMNS = {"customer_id", "anonymous_id"}
ESTIMATE = re.compile(r"~\s*([\d,]+)\s*rows", re.IGNORECASE)


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.environ.get("WAREHOUSE_DB", "fixtures/warehouse.duckdb"))
    source_schema: str = field(default_factory=lambda: os.environ.get("WAREHOUSE_SOURCE_SCHEMA", "marts"))
    catalog_path: str = field(
        default_factory=lambda: os.environ.get("WAREHOUSE_CATALOG", "fixtures/catalog.json")
    )
    audit_path: str = field(default_factory=lambda: os.environ.get("MCP_AUDIT_LOG", "audit.log"))
    # Without a fixed salt, pseudonyms change on every restart. Set one in production.
    pii_salt: str = field(default_factory=lambda: os.environ.get("MCP_PII_SALT") or secrets.token_hex(16))
    default_rows: int = 50
    max_rows: int = 500
    max_estimated_rows: int = 5_000_000
    timeout_seconds: float = 10.0


class Warehouse:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or Settings()
        self.catalog = (
            json.loads(Path(self.s.catalog_path).read_text()) if Path(self.s.catalog_path).exists() else {}
        )
        self._lock = threading.Lock()
        self._con = duckdb.connect(":memory:")
        self._con.execute(f"ATTACH '{self.s.db_path}' AS wh (READ_ONLY)")
        self._con.create_function(
            "pseudonymize", self._pseudonymize, ["VARCHAR"], "VARCHAR", null_handling="special"
        )
        self.tables = self._create_views()
        self._con.execute(f"SET search_path = '{SCHEMA}'")
        self._con.execute("SET enable_external_access = false")
        self._con.execute("SET lock_configuration = true")

    def _pseudonymize(self, value: str | None) -> str | None:
        if value is None:
            return None
        digest = hashlib.sha256(f"{self.s.pii_salt}:{value}".encode()).hexdigest()[:12]
        return f"anon_{digest}"

    def _create_views(self) -> dict[str, list[tuple[str, str]]]:
        self._con.execute(f"CREATE SCHEMA {SCHEMA}")
        rows = self._con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_catalog = 'wh' AND table_schema = ? ORDER BY table_name, ordinal_position",
            [self.s.source_schema],
        ).fetchall()
        tables: dict[str, list[tuple[str, str]]] = {}
        for table, column, dtype in rows:
            tables.setdefault(table, []).append((column, dtype))
        for table, columns in tables.items():
            select = ", ".join(
                f'pseudonymize("{c}") AS "{c}"' if c in PII_COLUMNS else f'"{c}"' for c, _ in columns
            )
            self._con.execute(
                f'CREATE VIEW {SCHEMA}."{table}" AS SELECT {select} FROM wh.{self.s.source_schema}."{table}"'
            )
        return tables

    # Tools ---------------------------------------------------------------

    def list_tables(self) -> list[dict]:
        out = []
        for table in sorted(self.tables):
            rows = self._con.execute(f'SELECT count(*) FROM {SCHEMA}."{table}"').fetchone()[0]
            out.append(
                {
                    "table": table,
                    "rows": rows,
                    "description": self.catalog.get(table, {}).get("description", ""),
                }
            )
        self._audit("list_tables", status="ok", rows=len(out))
        return out

    def describe_table(self, table: str) -> dict:
        if table not in self.tables:
            self._audit("describe_table", status="rejected", reason="unknown_table", detail=table)
            raise Rejected("unknown_table", table)
        docs = self.catalog.get(table, {})
        columns = [
            {
                "column": c,
                "type": t,
                "pii": c in PII_COLUMNS,
                "description": docs.get("columns", {}).get(c, ""),
            }
            for c, t in self.tables[table]
        ]
        self._audit("describe_table", status="ok", detail=table)
        return {"table": table, "description": docs.get("description", ""), "columns": columns}

    def run_query(self, sql: str, max_rows: int | None = None) -> dict:
        limit = min(max_rows or self.s.default_rows, self.s.max_rows)
        started = time.monotonic()
        try:
            checked = check(sql, set(self.tables), SCHEMA)
            self._preflight(checked.sql)
            columns, rows = self._execute(f"SELECT * FROM ({checked.sql}) AS q LIMIT {limit + 1}")
        except Rejected as e:
            self._audit(
                "run_query", sql=sql, status="rejected", reason=e.reason, detail=e.detail, started=started
            )
            raise
        except duckdb.Error as e:
            self._audit("run_query", sql=sql, status="error", detail=str(e).splitlines()[0], started=started)
            raise Rejected("query_error", str(e).splitlines()[0]) from None
        truncated = len(rows) > limit
        rows = rows[:limit]
        self._audit("run_query", sql=sql, status="ok", rows=len(rows), started=started)
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}

    # Internals -----------------------------------------------------------

    def _preflight(self, sql: str) -> None:
        plan = "\n".join(str(r[-1]) for r in self._con.execute(f"EXPLAIN {sql}").fetchall())
        if "CROSS_PRODUCT" in plan:
            raise Rejected("cross_join", "add a join condition")
        estimates = [int(n.replace(",", "")) for n in ESTIMATE.findall(plan)]
        if estimates and max(estimates) > self.s.max_estimated_rows:
            raise Rejected("too_expensive", f"plan estimates {max(estimates):,} rows")

    def _execute(self, sql: str) -> tuple[list[str], list[list]]:
        with self._lock:
            timer = threading.Timer(self.s.timeout_seconds, self._con.interrupt)
            timer.start()
            try:
                cursor = self._con.execute(sql)
                columns = [d[0] for d in cursor.description]
                rows = [[_jsonable(v) for v in row] for row in cursor.fetchall()]
            except duckdb.InterruptException:
                raise Rejected("timeout", f"over {self.s.timeout_seconds}s") from None
            finally:
                timer.cancel()
        return columns, rows

    def _audit(self, tool: str, started: float | None = None, **fields) -> None:
        entry = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "tool": tool, **fields}
        if started is not None:
            entry["ms"] = round((time.monotonic() - started) * 1000, 1)
        with open(self.s.audit_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")


def _jsonable(value):
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    return str(value)
