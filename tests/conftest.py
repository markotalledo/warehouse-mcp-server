import pytest

from warehouse_mcp.warehouse import Settings, Warehouse


@pytest.fixture
def settings(tmp_path):
    return Settings(audit_path=str(tmp_path / "audit.log"), pii_salt="test-salt")


@pytest.fixture
def wh(settings):
    return Warehouse(settings)
