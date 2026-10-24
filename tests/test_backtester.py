"""Pruebas de backtester.py (python -m unittest discover -s tests -v)."""
import io
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backtester as bt  # noqa: E402
import quant_core_engine as q  # noqa: E402


def noisy(n=3000, seed=3, drift=0.0):
    rng = np.random.default_rng(seed)
    return q._synthetic(40000 * np.exp(np.cumsum(rng.normal(drift, 0.006, n))), seed, wick=0.003)


class TestBacktester(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = bt.run_backtest(noisy())

    def test_no_lookahead(self):
        df = q.demo_bull_trend(n=800)
        a = bt.run_backtest(df)["trades"]
        self.assertTrue(a, "se esperaba al menos un trade")
        cut = pd.Timestamp(a[0]["opened_at"])
        df2 = df.copy()
        df2.loc[df2.time > cut, ["open", "high", "low", "close"]] *= 1.5
        b = bt.run_backtest(df2)["trades"]
        self.assertEqual((a[0]["opened_at"], a[0]["entry_price"]), (b[0]["opened_at"], b[0]["entry_price"]))

    def test_accounting(self):
        rep, trades = self.out["report"], self.out["trades"]
        self.assertAlmostEqual(rep["results"]["final_balance"], 10000 + sum(t["pnl_usd"] for t in trades),
                               delta=0.01 * len(trades) + 0.01)
        r = rep["results"]
        self.assertEqual(r["wins"] + r["losses"] + r["breakevens"], r["trades"])
        self.assertTrue(all(t["risk_mode"] in ("ACTIVE", "THROTTLED") for t in trades))   # nunca en HALTED

    def test_break_even_and_breaker_counters(self):
        rep, trades = self.out["report"], self.out["trades"]
        be = rep["break_even"]
        at_be = [t for t in trades if t["was_at_breakeven"]]
        self.assertEqual(be["activations"], len(at_be))
        self.assertEqual(be["saved_by_break_even"], sum(t["result"] == "BREAKEVEN" for t in trades))
        self.assertLessEqual(be["saved_by_break_even"] + be["break_even_then_take_profit"], be["activations"])
        cb = rep["circuit_breaker"]
        self.assertGreater(cb["halted_activations"], 0)
        self.assertGreater(cb["throttled_activations"], 0)
        self.assertLessEqual(cb["signals_blocked_by_halt"], cb["signals_total"])

    def test_fees_cost_money(self):
        df = q.demo_bull_trend(n=800)
        free = bt.run_backtest(df, cfg=bt.BacktestConfig(fee_pct=0, slippage_pct=0))["report"]["results"]
        paid = bt.run_backtest(df)["report"]["results"]
        self.assertGreaterEqual(free["final_balance"], paid["final_balance"])
        self.assertEqual(free["total_fees_usd"], 0)

    def test_cli_exports(self):
        with tempfile.TemporaryDirectory() as d:
            csv, md, js = Path(d) / "BTCUSDT_1h.csv", Path(d) / "r.md", Path(d) / "r.json"
            noisy(1200).to_csv(csv, index=False)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = bt.main(["--csv", str(csv), "--out-md", str(md), "--out-json", str(js)])
            self.assertEqual(rc, 0)
            rep = json.loads(js.read_text(encoding="utf-8"))
            self.assertEqual(rep["symbol"], "BTCUSDT")
            self.assertEqual(rep["config"]["fee_pct_per_order"], 0.08)
            self.assertIn("Profit Factor", md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
