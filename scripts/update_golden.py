"""Regenerate golden backtest files (TEST-9). Run only after an intended behaviour change,
and say why in the commit message: `uv run python scripts/update_golden.py`."""

from __future__ import annotations

import json
from pathlib import Path

from tests.backtest_fixtures import EXAMPLES, bars, spec

from helm.app.backtest import run_backtest

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    data = bars()
    for name in EXAMPLES:
        r = run_backtest(spec(name), data)
        m = r.metrics.to_dict()
        golden = {
            "orders": len(r.orders),
            "fills": len(r.fills),
            "trade_count": m["trade_count"],
            "end_equity": m["end_equity"],
            "fees_paid": m["fees_paid"],
            "max_drawdown": m["max_drawdown"],
            "first_orders": r.order_sequence()[:5],
        }
        (OUT / f"{name}.json").write_text(json.dumps(golden, indent=2) + "\n")
        print(name, golden["orders"], golden["end_equity"])


if __name__ == "__main__":
    main()
