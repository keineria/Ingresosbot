"""Modo lateral opcional del perfil Trend Rider (rebote en el extremo del rango cuando no hay tendencia)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backtester as bt  # noqa: E402
import quant_core_engine as q  # noqa: E402

ACCOUNT = {"balance": 500.0, "consecutive_losses": 0, "daily_drawdown_pct": 0.0, "cooldown_bars_remaining": 0}


def ind(**kw):
    """Mercado lateral bajo la EMA200: sin ruptura, ADX bajo, cierre bajo la banda inferior y RSI bajo."""
    base = {"close": 95.0, "ema20": 98.0, "ema50": 99.0, "ema200": 100.0, "rsi14": 30.0, "atr14": 2.0,
            "ema20_slope_pct": -0.1, "donchian_high": 104.0, "donchian_low": 94.0, "daily_trend": -1.0,
            "adx14": 15.0, "bb_mid": 98.0, "bb_lower": 96.0, "bb_upper": 100.0}
    base.update(kw)
    return base


class TestRangeMode(unittest.TestCase):
    def test_off_by_default(self):
        mr = q.trend_regime(ind(), q.Params(strategy="trend", daily_sma_len=200))
        self.assertEqual(mr["signal"], "WAIT / NO_TRADE")
        self.assertFalse(any(b.startswith("RANGE.") for b in mr["blocking_filters"]))

    def test_long_at_lower_band(self):
        mr = q.trend_regime(ind(), q.Params(strategy="trend", daily_sma_len=200, range_entry=True))
        self.assertEqual((mr["signal"], mr["entry_trigger"]), ("LONG", "range"))

    def test_each_check_blocks(self):
        p = q.Params(strategy="trend", daily_sma_len=200, range_entry=True)
        for kw, name in (({"adx14": 25.0}, "RANGE.LONG.range_adx_lt_20"),
                         ({"close": 97.0}, "RANGE.LONG.range_close_lt_bb_lower"),
                         ({"rsi14": 40.0}, "RANGE.LONG.range_rsi_le_35")):
            mr = q.trend_regime(ind(**kw), p)
            self.assertEqual(mr["signal"], "WAIT / NO_TRADE", kw)
            self.assertIn(name, mr["blocking_filters"])

    def test_short_needs_short_enabled(self):
        top = ind(close=101.0, rsi14=70.0, donchian_low=90.0, donchian_high=105.0)
        self.assertEqual(q.trend_regime(top, q.Params(strategy="trend", range_entry=True))["signal"], "WAIT / NO_TRADE")
        mr = q.trend_regime(top, q.Params(strategy="trend", range_entry=True, trend_allow_short=True))
        self.assertEqual((mr["signal"], mr["entry_trigger"]), ("SHORT", "range"))

    def test_breakout_has_priority(self):
        up = ind(close=106.0, ema200=90.0, daily_trend=1.0, bb_lower=107.0, rsi14=30.0)
        mr = q.trend_regime(up, q.Params(strategy="trend", daily_sma_len=200, range_entry=True))
        self.assertEqual(mr["entry_trigger"], "breakout")

    def test_layer3_fixed_sl_tp_no_trailing(self):
        p = q.Params(strategy="trend", daily_sma_len=200, range_entry=True, price_tick=0.0001)
        r = q.run_engine(ACCOUNT, manual=dict(ind(), ema20_prev3=98.1), symbol="BTCUSDT", params=p)
        self.assertEqual(r["decision"], "LONG")
        rk = r["layer_3_atr_risk_guardian"]
        self.assertAlmostEqual(rk["stop_loss"], 92.0)
        self.assertAlmostEqual(rk["take_profit"], 98.0)
        self.assertNotIn("trail_atr_mult", rk)
        self.assertIsNone(r["order"].get("trailing_stop"))
        self.assertAlmostEqual(r["order"]["take_profit"]["price"], 98.0)

    def test_trend_short_survives_crash_atr(self):
        # Desplome: ATR mayor que precio / trend_sl_atr_mult. El corto no usa TP; no debe fallar por el TP de relleno.
        p = q.Params(strategy="trend", trend_sl_atr_mult=3.0, price_tick=0.0001)
        r = q.trend_risk_guardian("SHORT", 1.0, 0.5, 500.0, 1.5, p)
        self.assertAlmostEqual(r["stop_loss"], 2.5)
        self.assertIsNone(r["take_profit"])
        with self.assertRaises(ValueError):           # un LONG con SL <= 0 sí se rechaza
            q.trend_risk_guardian("LONG", 1.0, 0.5, 500.0, 1.5, p)

    def test_backtest_runs_in_chop(self):
        df = q.demo_chop_range(1500, seed=3)
        p = q.Params(strategy="trend", range_entry=True, price_tick=0.0001)
        rep = bt.run_portfolio_backtest({"X": df}, p, bt.BacktestConfig(initial_balance=500))["report"]
        self.assertGreater(rep["trades"], 0)


if __name__ == "__main__":
    unittest.main()
