"""Static checks on a SQL string before it reaches the database.

This is the first of three layers. The other two do not depend on it being right:
the database connection blocks file and network access and cannot be reconfigured,
and the only tables it exposes are pseudonymized views.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

# Table functions that read files, other databases, or engine internals.
DENIED_FUNCTION = re.compile(
    r"^(read_\w+|\w+_scan|glob|sniff_csv|query|query_table|getenv|current_setting|duckdb_\w+|pragma_\w+)$",
    re.IGNORECASE,
)
WRITE_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Attach,
    exp.Detach,
    exp.Pragma,
    exp.Set,
    exp.Use,
    exp.Into,
)


class Rejected(Exception):
    """The query was not allowed. `reason` is a short, stable code for logs and evals."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Checked:
    sql: str
    tables: frozenset[str]


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this)
    return node.sql_name()


def check(sql: str, allowed_tables: set[str], schema: str = "analytics") -> Checked:
    """Return the normalized query if it is a single read over allowed tables, else raise Rejected."""
    if not sql or not sql.strip():
        raise Rejected("empty")
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise Rejected("parse_error", str(e).splitlines()[0]) from None
    if len(statements) != 1:
        raise Rejected("multiple_statements")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise Rejected("not_a_select", type(tree).__name__)
    for node in tree.walk():
        if isinstance(node, WRITE_NODES):
            raise Rejected("write_or_ddl", type(node).__name__)
        if isinstance(node, exp.Func) and DENIED_FUNCTION.match(_function_name(node)):
            raise Rejected("denied_function", _function_name(node))

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    used = set()
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not name:  # a table function such as range(10)
            continue
        if name in cte_names and not table.db:
            continue
        if table.catalog or (table.db and table.db.lower() != schema):
            raise Rejected("outside_schema", table.sql())
        if name not in allowed_tables:
            raise Rejected("unknown_table", name)
        used.add(name)

    return Checked(sql=tree.sql(dialect="duckdb"), tables=frozenset(used))
