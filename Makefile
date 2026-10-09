.PHONY: setup test lint serve eval-dry eval

setup:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

serve:
	uv run warehouse-mcp

eval-dry:
	uv run --group eval python evals/run_llm_eval.py --dry-run

eval:
	uv run --group eval python evals/run_llm_eval.py
