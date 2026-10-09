from pathlib import Path

import pytest
import yaml

from warehouse_mcp.guard import Rejected

CASES = yaml.safe_load((Path(__file__).parent.parent / "evals" / "security_cases.yaml").read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_security_case(wh, case):
    if case["expect"] == "ok":
        assert wh.run_query(case["sql"])["row_count"] >= 0
    else:
        with pytest.raises(Rejected) as err:
            wh.run_query(case["sql"])
        assert err.value.reason == case["expect"]
