import json

import duckdb
import pytest

from warehouse_mcp.guard import Rejected
from warehouse_mcp.warehouse import Settings, Warehouse


def test_raw_pii_never_leaves_the_server(wh):
    # Upstream customer ids look like c_00042. No column of any table may show one.
    for table in wh.tables:
        result = wh.run_query(f"select * from {table}", max_rows=500)
        flat = json.dumps(result["rows"])
        assert '"c_0' not in flat, table


def test_pseudonyms_are_stable_and_still_join(wh):
    joined = wh.run_query("select count(*) from fct_orders o join dim_customers c using (customer_id)")
    orders = wh.run_query("select count(*) from fct_orders")
    assert joined["rows"] == orders["rows"]


def test_salt_changes_pseudonyms(settings):
    a = Warehouse(settings).run_query("select min(customer_id) from dim_customers")["rows"]
    settings.pii_salt = "another-salt"
    b = Warehouse(settings).run_query("select min(customer_id) from dim_customers")["rows"]
    assert a != b


def test_rows_are_capped_and_truncation_is_reported(wh):
    result = wh.run_query("select * from fct_order_items", max_rows=5)
    assert result["row_count"] == 5 and result["truncated"] is True
    assert wh.run_query("select * from fct_order_items", max_rows=10_000)["row_count"] <= wh.s.max_rows


def test_connection_blocks_files_even_if_the_guard_is_bypassed(wh):
    with pytest.raises(duckdb.Error):
        wh._con.execute("select * from read_csv('/etc/hosts')")
    with pytest.raises(duckdb.Error):
        wh._con.execute("set enable_external_access = true")


def test_raw_tables_are_read_only(wh):
    with pytest.raises(duckdb.Error):
        wh._con.execute("delete from wh.marts.fct_orders")


def test_slow_queries_are_interrupted(tmp_path):
    w = Warehouse(
        Settings(audit_path=str(tmp_path / "a.log"), timeout_seconds=0.2, max_estimated_rows=10**15)
    )
    with pytest.raises(Rejected) as err:
        w.run_query("select count(*) from range(10000000000) a join range(10) b on a.range % 10 = b.range")
    assert err.value.reason == "timeout"


def test_every_call_is_audited(wh, settings):
    wh.run_query("select 1 from fct_orders limit 1")
    with pytest.raises(Rejected):
        wh.run_query("delete from fct_orders")
    lines = [json.loads(line) for line in open(settings.audit_path)]
    assert [entry["status"] for entry in lines] == ["ok", "rejected"]
    assert lines[1]["reason"] == "not_a_select" and "ms" in lines[1]


def test_describe_marks_pii_and_carries_dbt_docs(wh):
    desc = wh.describe_table("fct_orders")
    cols = {c["column"]: c for c in desc["columns"]}
    assert cols["customer_id"]["pii"] is True and cols["order_id"]["pii"] is False
    assert "reconcil" in desc["description"].lower()
