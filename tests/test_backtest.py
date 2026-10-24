"""Pruebas del simulador (python -m unittest discover -s tests -v)."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backtest as bt  # noqa: E402
import quant_core_engine as q  # noqa: E402


def long_pos(**over):
    base = dict(side="LONG", entry_time=0, entry_price=100.0, qty=1.0, stop_loss=96.8, take_profit=107.2,
                be_trigger=103.0, be_new_sl=100.2, risk_usd=3.2, regime="BULL_EXPANSION", risk_mode="ACTIVE")
    base.update(over)
    return bt.Position(**base)


class TestExits(unittest.TestCase):
    def test_sl_tp_and_conflict(self):
        self.assertIsNone(bt.check_exit(long_pos(), 100, 102, 98))
        self.assertEqual(bt.check_exit(long_pos(), 100, 101, 96), (96.8, "STOP_LOSS"))
        self.assertEqual(bt.check_exit(long_pos(), 100, 108, 99), (107.2, "TAKE_PROFIT"))
        self.assertEqual(bt.check_exit(long_pos(), 100, 108, 96), (96.8, "STOP_LOSS"))   # ambos: SL primero

    def test_gaps(self):
        self.assertEqual(bt.check_exit(long_pos(), 95, 99, 94), (95, "STOP_LOSS_GAP"))
        self.assertEqual(bt.check_exit(long_pos(), 109, 110, 108), (109, "TAKE_PROFIT_GAP"))

    def test_break_even_next_candle(self):
        pos = long_pos()
        self.assertIsNone(bt.check_exit(pos, 101, 103.5, 100.0))   # toca trigger, no sale en esa vela
        bt.maybe_move_break_even(pos, 103.5, 100.0)
        self.assertEqual((pos.stop_loss, pos.be_moved), (100.2, True))
        self.assertEqual(bt.check_exit(pos, 101, 101.5, 100.1), (100.2, "BREAK_EVEN"))

    def test_short_mirror(self):
        pos = long_pos(side="SHORT", stop_loss=103.2, take_profit=92.8, be_trigger=97.0, be_new_sl=99.8)
        self.assertEqual(bt.check_exit(pos, 100, 104, 99), (103.2, "STOP_LOSS"))
        self.assertEqual(bt.check_exit(pos, 100, 101, 92), (92.8, "TAKE_PROFIT"))


class TestRun(unittest.TestCase):
    def test_no_lookahead(self):
        # Cambiar velas posteriores a una entrada no puede cambiar esa entrada.
        df = q.demo_bull_trend(n=800)
        a = bt.run_backtest(df)["trades"]
        self.assertTrue(a, "se esperaba al menos un trade")
        cut = pd.Timestamp(a[0]["entry_time"])
        df2 = df.copy()
        later = df2.time > cut
        df2.loc[later, ["open", "high", "low", "close"]] *= 1.5
        b = bt.run_backtest(df2)["trades"]
        self.assertEqual(a[0]["entry_time"], b[0]["entry_time"])
        self.assertEqual(a[0]["entry_price"], b[0]["entry_price"])

    def test_accounting_and_halts(self):
        rng = np.random.default_rng(3)
        closes = 40000 * np.exp(np.cumsum(rng.normal(0, 0.006, 3000)))
        out = bt.run_backtest(q._synthetic(closes, 3, wick=0.003))
        rep, trades = out["report"], out["trades"]
        self.assertAlmostEqual(rep["results"]["final_balance"],
                               round(10000 + sum(t["pnl_usd"] for t in trades), 2), delta=0.05 * len(trades) + 0.01)
        cb = rep["circuit_breaker"]
        self.assertLessEqual(cb["signals_blocked_by_halt"], cb["signals_total"])
        # Ningún trade se abre mientras la cuenta está en HALTED.
        self.assertTrue(all(t["risk_mode"] in ("ACTIVE", "THROTTLED") for t in trades))

    def test_fee_zero_vs_positive(self):
        df = q.demo_bull_trend(n=800)
        free = bt.run_backtest(df, cfg=bt.BacktestConfig(fee_pct=0, slippage_pct=0))["report"]["results"]
        paid = bt.run_backtest(df)["report"]["results"]
        self.assertGreaterEqual(free["final_balance"], paid["final_balance"])


if __name__ == "__main__":
    unittest.main()
