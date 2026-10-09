"""MCP entrypoint: three read-only tools over the warehouse, served on stdio."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from warehouse_mcp.guard import Rejected
from warehouse_mcp.warehouse import Warehouse

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

server = MCPServer(
    "warehouse",
    instructions=(
        "Read-only access to an e-commerce warehouse. Call list_tables first, then describe_table, "
        "then run_query with a single SELECT. customer_id and anonymous_id are pseudonyms: they "
        "join across tables but do not identify anyone. Amounts are in cents."
    ),
)
_warehouse: Warehouse | None = None


def warehouse() -> Warehouse:
    global _warehouse
    if _warehouse is None:
        _warehouse = Warehouse()
    return _warehouse


@server.tool(annotations=READ_ONLY)
def list_tables() -> list[dict]:
    """List the tables you can query, with row counts and a description of each."""
    return warehouse().list_tables()


@server.tool(annotations=READ_ONLY)
def describe_table(table: str) -> dict:
    """Columns of one table with type, description and whether the column is pseudonymized PII."""
    try:
        return warehouse().describe_table(table)
    except Rejected as e:
        return {"error": str(e)}


@server.tool(annotations=READ_ONLY)
def run_query(sql: str, max_rows: int = 50) -> dict:
    """Run one read-only SELECT (DuckDB SQL). Results are capped; check `truncated`.

    Rejected: writes, DDL, multiple statements, file or engine functions, tables outside
    list_tables, cross joins and plans estimated above the size limit.
    """
    try:
        return warehouse().run_query(sql, max_rows)
    except Rejected as e:
        return {"error": str(e), "reason": e.reason}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
