"""LLM eval: can Claude answer business questions using only the three tools?

    uv run --group eval python evals/run_llm_eval.py            # calls the Claude API
    uv run --group eval python evals/run_llm_eval.py --dry-run  # reference answers only, free

Each question has a reference SQL query. Claude gets the same tools the MCP server
exposes (same guard, same pseudonymized views), must end with `ANSWER: <value>`, and
is scored against the reference value. Prints accuracy, tool calls and cost.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from warehouse_mcp.guard import Rejected  # noqa: E402
from warehouse_mcp.warehouse import Settings, Warehouse  # noqa: E402

MODEL = "claude-opus-5-5"
PRICE_IN, PRICE_OUT = 4.00 / 1e6, 20.00 / 1e6  # USD per token
SYSTEM = (
    "You answer questions about an e-commerce warehouse using the tools. Amounts are stored in cents. "
    "Look at the tables and columns before querying. Finish with a last line 'ANSWER: <value>' that "
    "contains only the value: no units, no currency symbols, no thousands separators."
)


def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("list_tables", "List the tables you can query, with row counts and descriptions.", {}),
    _tool(
        "describe_table",
        "Columns of one table with type, description and whether it is pseudonymized PII.",
        {"table": {"type": "string"}},
    ),
    _tool(
        "run_query",
        "Run one read-only SELECT in DuckDB SQL. Results are capped at 50 rows.",
        {"sql": {"type": "string"}},
    ),
]


def run_tool(wh: Warehouse, name: str, args: dict) -> tuple[object, bool]:
    try:
        if name == "list_tables":
            return wh.list_tables(), False
        if name == "describe_table":
            return wh.describe_table(args["table"]), False
        if name == "run_query":
            return wh.run_query(args["sql"]), False
        return {"error": f"unknown tool {name}"}, True
    except Rejected as e:
        return {"error": str(e)}, True


def ask(client, wh: Warehouse, question: str, max_turns: int = 12) -> dict:
    messages = [{"role": "user", "content": question}]
    stats = {"answer": None, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0, "stop": None}
    for _ in range(max_turns):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=messages,
        )
        stats["input_tokens"] += response.usage.input_tokens
        stats["output_tokens"] += response.usage.output_tokens
        stats["stop"] = response.stop_reason
        if response.stop_reason in ("refusal", "max_tokens"):
            return stats
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text")
            found = re.findall(r"ANSWER:\s*(.+)", text)
            stats["answer"] = found[-1].strip() if found else None
            return stats
        results = []
        for block in response.content:
            if block.type == "tool_use":
                stats["tool_calls"] += 1
                output, is_error = run_tool(wh, block.name, block.input)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(output, default=str),
                        "is_error": is_error,
                    }
                )
        messages.append({"role": "user", "content": results})
    return stats


def matches(got: str | None, expected) -> bool:
    if got is None:
        return False
    try:
        return abs(float(str(got).replace(",", "").rstrip("%")) - float(expected)) <= 0.011
    except ValueError:
        return str(got).strip().strip("'\"").lower() == str(expected).strip().lower()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="only compute reference answers")
    parser.add_argument("--out", default="evals/results.json")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    wh = Warehouse(
        Settings(
            db_path=str(root / "fixtures/warehouse.duckdb"),
            catalog_path=str(root / "fixtures/catalog.json"),
            audit_path=str(root / "evals/audit.log"),
            pii_salt="eval",  # fixed, so reference answers and Claude see the same pseudonyms
        )
    )
    questions = yaml.safe_load((root / "evals/questions.yaml").read_text())
    for q in questions:
        q["expected"] = wh.run_query(q["reference_sql"])["rows"][0][0]

    if args.dry_run:
        for q in questions:
            print(f"{q['expected']!s:>22}  {q['question']}")
        return

    import anthropic

    client = anthropic.Anthropic()
    rows = []
    for q in questions:
        stats = ask(client, wh, q["question"])
        ok = matches(stats["answer"], q["expected"])
        rows.append({**q, **stats, "correct": ok})
        print(f"{'PASS' if ok else 'FAIL'}  {q['question']}  got={stats['answer']} expected={q['expected']}")

    correct = sum(r["correct"] for r in rows)
    cost = sum(r["input_tokens"] * PRICE_IN + r["output_tokens"] * PRICE_OUT for r in rows)
    calls = sum(r["tool_calls"] for r in rows)
    print(f"\n{correct}/{len(rows)} correct, {calls} tool calls, ${cost:.2f} total, model {MODEL}")
    Path(args.out).write_text(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main()
