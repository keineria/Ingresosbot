"""Filtros de acierto / empuje y cierres opcionales del perfil Trend Rider (tercera ronda de laboratorio)."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backtester as bt  # noqa: E402
import quant_core_engine as q  # noqa: E402
from state_manager import StateManager  # noqa: E402


def ind(**kw):
    base = {"close": 105.0, "ema20": 102.0, "ema50": 100.0, "ema200": 90.0, "rsi14": 70.0, "atr14": 2.0,
            "ema20_slope_pct": 0.5, "donchian_high": 104.0, "donchian_low": 95.0, "vol_ratio": 1.5,
            "atr_rank": 0.6, "ext_atr": 1.5, "breakout_atr": 0.5, "adx14": 25.0, "exit_low": 98.0, "exit_high": 110.0}
    base.update(kw)
    return base


class TestEntryFilters(unittest.TestCase):
    def test_each_filter_blocks_and_names_itself(self):
        cases = [({"trend_min_vol_ratio": 2.0}, "TREND.LONG.volume_ge_2x_avg20"),
                 ({"trend_min_atr_rank": 0.7}, "TREND.LONG.atr_rank_ge_0.7"),
                 ({"trend_max_ext_atr": 1.0}, "TREND.LONG.ext_le_1atr"),
                 ({"trend_min_breakout_atr": 0.75}, "TREND.LONG.breakout_push_ge_0.75atr"),
                 ({"trend_min_adx": 30.0}, "TREND.LONG.adx_ge_30")]
        for extra, name in cases:
            p = q.Params(strategy="trend", **extra)
            mr = q.trend_regime(ind(), p)
            self.assertEqual(mr["signal"], "WAIT / NO_TRADE", extra)
            self.assertIn(name, mr["blocking_filters"])
            loose = {k: v / 2 if "max" not in k else v * 3 for k, v in extra.items()}
            self.assertEqual(q.trend_regime(ind(), q.Params(strategy="trend", **loose))["signal"], "LONG", loose)

    def test_filters_off_change_nothing_in_backtest(self):
        df = q.demo_bull_trend(1500, seed=7)
        a = bt.run_backtest(df, q.Params(strategy="trend"))["report"]["results"]
        b = bt.run_backtest(df, q.Params(strategy="trend", trend_min_vol_ratio=0.0, trend_exit_low_len=0,
                                         trend_tight_after_r=0.0))["report"]["results"]
        self.assertEqual(a, b)

    def test_indicators_have_no_nan_after_warmup(self):
        out = q.compute_indicators(q.demo_bull_trend(600, seed=3), q.Params(trend_exit_low_len=10))
        for k in ("vol_ratio", "atr_rank", "ext_atr", "breakout_atr", "adx14", "exit_low", "exit_high"):
            self.assertFalse(out[k].isna().any(), k)
        self.assertTrue(0 < out.adx14.iloc[-1] < 100)


class TestExits(unittest.TestCase):
    def test_exit_reasons(self):
        p = q.Params(strategy="trend")
        self.assertIsNone(q.trend_exit_reason(ind(close=90.0), "LONG", p, 100.0, 50))
        self.assertEqual(q.trend_exit_reason(ind(close=97.0), "LONG", q.Params(strategy="trend", trend_exit_low_len=10)),
                         "CHANNEL_EXIT")
        self.assertEqual(q.trend_exit_reason(ind(close=111.0), "SHORT", q.Params(strategy="trend", trend_exit_low_len=10)),
                         "CHANNEL_EXIT")
        self.assertEqual(q.trend_exit_reason(ind(close=101.0), "LONG", q.Params(strategy="trend", trend_exit_ema="ema20")),
                         "EMA_EXIT")
        stale = q.Params(strategy="trend", trend_stale_bars=6)
        self.assertEqual(q.trend_exit_reason(ind(close=99.0), "LONG", stale, 100.0, 6), "STALE_EXIT")
        self.assertIsNone(q.trend_exit_reason(ind(close=99.0), "LONG", stale, 100.0, 5))
        self.assertIsNone(q.trend_exit_reason(ind(close=101.0), "LONG", stale, 100.0, 6))
        self.assertTrue(q.trend_has_signal_exits(stale))
        self.assertFalse(q.trend_has_signal_exits(p))

    def test_channel_exit_in_single_and_portfolio_backtest(self):
        df = q.demo_bull_trend(1500, seed=7)
        p = q.Params(strategy="trend", trend_exit_low_len=5)
        single = bt.run_backtest(df, p)
        self.assertIn("CHANNEL_EXIT", {t["exit_reason"] for t in single["trades"]})
        port = bt.run_portfolio_backtest({"BTCUSDT": df}, p)
        self.assertIn("CHANNEL_EXIT", {t["exit_reason"] for t in port["trades"]})

    def test_tight_trailing_after_r(self):
        p = q.Params(strategy="trend", trail_atr_mult=5.0, trend_tight_after_r=2.0, trend_tight_trail_atr=2.0,
                     price_tick=0.01)
        sm = StateManager(":memory:", 1000.0, p)
        sm.open_position("BTCUSDT", "LONG", 100.0, 96.0, 0.0, 0.0, 0.0, 1.0, 4.0, "2024-01-01T00:00:00+00:00",
                         trail_atr_mult=5.0)
        sm.apply_trailing("BTCUSDT", "2024-01-01T04:00:00+00:00", 106.0, 100.0, 1.0)   # +1.5R: trailing normal
        self.assertEqual(sm.positions()[0]["stop_loss"], 101.0)
        sm.apply_trailing("BTCUSDT", "2024-01-01T08:00:00+00:00", 110.0, 105.0, 1.0)   # +2.5R: se ajusta a 2 ATR
        self.assertEqual(sm.positions()[0]["stop_loss"], 108.0)
        sm.close()


if __name__ == "__main__":
    unittest.main()
