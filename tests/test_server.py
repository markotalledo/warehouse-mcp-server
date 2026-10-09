import asyncio

from warehouse_mcp import server


def test_tools_are_registered_read_only():
    tools = {t.name: t for t in asyncio.run(server.server.list_tools())}
    assert set(tools) == {"list_tables", "describe_table", "run_query"}
    assert all(t.annotations.read_only_hint for t in tools.values())


def test_rejections_come_back_as_data_not_exceptions(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_AUDIT_LOG", str(tmp_path / "a.log"))
    monkeypatch.setattr(server, "_warehouse", None)
    result = server.run_query("delete from fct_orders")
    assert result["reason"] == "not_a_select"
    assert server.describe_table("nope")["error"].startswith("unknown_table")
