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


class TestSweep(unittest.TestCase):
    GRID = {"tp_atr_mult": [3.6, 2.4], "be_atr_mult": [1.5, None]}

    @classmethod
    def setUpClass(cls):
        cls.data = {("BTCUSDT", "4h"): noisy(1500, seed=5), ("ETHUSDT", "1d"): noisy(1200, seed=8, drift=0.0004)}
        cls.rows = bt.run_sweep(cls.data, cls.GRID)

    def test_grid_and_labels(self):
        combos = bt.grid_combos(bt.DEFAULT_GRID)
        self.assertEqual(len(combos), 16)
        self.assertIn({"sl_atr_mult": 1.6, "tp_atr_mult": 3.6, "be_atr_mult": 1.5, "allow_short": True}, combos)
        self.assertEqual(bt.combo_label({"sl_atr_mult": 2.2, "be_atr_mult": None, "allow_short": False}),
                         "SL 2.2 · sin BE · solo largos")

    def test_split_has_no_overlap_in_evaluated_candles(self):
        df, p = noisy(1000), q.Params()
        train, test = bt.split_train_test(df, p, 0.7)
        self.assertEqual(len(train), 700)
        first_eval = test.time.iloc[p.min_candles - 1]
        self.assertEqual(first_eval, df.time.iloc[700])          # validación empieza justo en el corte
        self.assertGreater(first_eval, train.time.iloc[-1])
        with self.assertRaises(ValueError):
            bt.split_train_test(df, p, 0.95)

    def test_be_off_never_activates(self):
        off = [r for r in self.rows if r["be_atr_mult"] is None]
        p = bt._params_for(q.Params(), {"be_atr_mult": None})
        rep = bt.run_backtest(self.data[("BTCUSDT", "4h")], p)["report"]
        self.assertEqual(rep["break_even"]["activations"], 0)
        self.assertEqual(len(off), 4)

    def test_rows_official_and_summary(self):
        self.assertEqual(len(self.rows), 2 * 4)
        official = [r for r in self.rows if r["official"]]
        self.assertEqual({(r["symbol"], r["tp_atr_mult"], r["be_atr_mult"]) for r in official},
                         {("BTCUSDT", 3.6, 1.5), ("ETHUSDT", 3.6, 1.5)})
        direct = bt.run_backtest(bt.split_train_test(self.data[("BTCUSDT", "4h")], q.Params())[1])["report"]
        self.assertEqual(official[0]["test_return_pct"], direct["results"]["net_return_pct"])
        summ = bt.summarize_sweep(self.rows)
        self.assertEqual(len(summ), 4)
        keys = [(-r["wins_both"], -r["mean_train_return_pct"]) for r in summ]
        self.assertEqual(keys, sorted(keys))                         # ordenado por entrenamiento, no validación
        md = bt.markdown_sweep(self.rows)
        self.assertIn("(oficial)", md)
        self.assertIn("| BTCUSDT 4h |", md)

    def test_cli_sweep(self):
        with tempfile.TemporaryDirectory() as d:
            md, csv = Path(d) / "v.md", Path(d) / "v.csv"
            orig = bt._load_sweep_data
            bt._load_sweep_data = lambda syms, ivs, limit: {(s, i): self.data[("BTCUSDT", "4h")] for s in syms for i in ivs}
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc = bt.main(["--sweep", "BTCUSDT", "--intervals", "4h", "--grid", '{"allow_short": [true, false]}',
                                  "--out-md", str(md), "--out-csv", str(csv)])
            finally:
                bt._load_sweep_data = orig
            self.assertEqual(rc, 0)
            self.assertEqual(len(pd.read_csv(csv)), 2)
            self.assertIn("solo largos", md.read_text(encoding="utf-8"))
            with self.assertRaises(SystemExit):
                bt.main(["--sweep", "BTCUSDT", "--grid", '{"no_existe": [1]}'])


if __name__ == "__main__":
    unittest.main()
