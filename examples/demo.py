"""A real MCP client talking to the server over stdio, as Claude Desktop would.

    uv run python examples/demo.py

Used to record docs/demo.gif (see docs/demo.tape).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BOLD, DIM, GREEN, RED, CYAN, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[36m", "\033[0m"
PAUSE = float(os.environ.get("DEMO_PAUSE", "1.2"))


def say(text: str = "") -> None:
    print(text, flush=True)


def table(columns: list[str], rows: list[list]) -> None:
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) for i, c in enumerate(columns)]
    say("  " + "  ".join(f"{BOLD}{c:<{w}}{RESET}" for c, w in zip(columns, widths, strict=True)))
    for r in rows:
        say("  " + "  ".join(f"{v!s:<{w}}" for v, w in zip(r, widths, strict=True)))


async def call(session: ClientSession, tool: str, args: dict) -> dict:
    result = await session.call_tool(tool, args)
    return json.loads(result.content[0].text)


async def query(session: ClientSession, label: str, sql: str) -> None:
    say(f"\n{CYAN}run_query{RESET} {DIM}# {label}{RESET}")
    say(f"  {DIM}{sql}{RESET}")
    out = await call(session, "run_query", {"sql": sql})
    if "error" in out:
        say(f"  {RED}rejected: {out['reason']}{RESET}")
    else:
        table(out["columns"], out["rows"])
    time.sleep(PAUSE)


async def main() -> None:
    env = {**os.environ, "MCP_PII_SALT": "demo", "MCP_AUDIT_LOG": "/tmp/warehouse-mcp-demo.log"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "warehouse_mcp.server"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        say(f"{CYAN}list_tables{RESET}")
        tables = await session.call_tool("list_tables", {})
        rows = [json.loads(c.text) for c in tables.content]
        rows = rows[0] if isinstance(rows[0], list) else rows
        table(["table", "rows"], [[t["table"], t["rows"]] for t in rows])
        time.sleep(PAUSE)

        await query(
            session,
            "a business question",
            "select app_source, count(*) as orders, round(sum(total_cents) / 100.0, 2) as revenue_usd "
            "from fct_orders group by 1 order by 3 desc",
        )
        await query(
            session,
            "personal data comes back pseudonymized",
            "select customer_id, orders, lifetime_value_cents from dim_customers "
            "order by lifetime_value_cents desc limit 3",
        )
        say(f"\n{BOLD}What the model cannot do:{RESET}")
        await query(session, "write", "delete from fct_orders")
        await query(session, "read files", "select * from read_csv('/etc/passwd')")
        await query(session, "reach the raw tables", "select customer_id from wh.marts.fct_orders")
        await query(session, "run a cross join", "select count(*) from fct_orders, fct_order_items")
        say(f"\n{GREEN}Every call above is in the audit log.{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
