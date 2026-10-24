"""Pruebas offline del motor (solo stdlib unittest + pandas/numpy).

Ejecutar:  python -m unittest discover -s tests -v
"""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quant_core_engine as q  # noqa: E402

P = q.Params()
EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
HEALTHY = {"balance": 10000, "consecutive_losses": 0, "daily_drawdown_pct": 0.0}


def manual(**over):
    base = dict(close=100.0, ema20=99.0, ema50=98.0, ema200=90.0, ema20_prev3=98.5, rsi14=55.0, atr14=1.0)
    base.update(over)
    return base


class TestIndicators(unittest.TestCase):
    def test_ema_matches_definition(self):
        s = pd.Series(np.arange(1, 31, dtype=float))
        e = q.ema(s, 10)
        self.assertTrue(np.isnan(e.iloc[8]))
        self.assertAlmostEqual(e.iloc[9], 5.5)                      # semilla SMA(1..10)
        self.assertAlmostEqual(e.iloc[10], (2 / 11) * 11 + (9 / 11) * 5.5)

    def test_rsi_against_naive_wilder(self):
        rng = np.random.default_rng(1)
        c = pd.Series(100 + np.cumsum(rng.normal(0, 1, 120)))
        got = q.rsi(c, 14).to_numpy()
        d = np.diff(c.to_numpy())
        ag, al = d[:14].clip(min=0).mean(), (-d[:14]).clip(min=0).mean()
        ref = [100 - 100 / (1 + ag / al)]
        for x in d[14:]:
            ag = (ag * 13 + max(x, 0)) / 14
            al = (al * 13 + max(-x, 0)) / 14
            ref.append(100 - 100 / (1 + ag / al))
        np.testing.assert_allclose(got[14:], ref, rtol=1e-10)

    def test_rsi_bounds(self):
        up = pd.Series(np.arange(1, 50, dtype=float))
        self.assertEqual(q.rsi(up).iloc[-1], 100.0)

    def test_atr_against_naive_wilder(self):
        df = q.demo_chop_range(80)
        got = q.atr(df.high, df.low, df.close, 14).to_numpy()
        h, l, c = df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
        tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
        a = np.mean(tr[:14]); ref = [a]
        for x in tr[14:]:
            a = (a * 13 + x) / 14; ref.append(a)
        np.testing.assert_allclose(got[13:], ref, rtol=1e-10)


class TestCircuitBreaker(unittest.TestCase):
    def cb(self, **st):
        return q.circuit_breaker(q.AccountState.from_dict({**HEALTHY, **st}), P)

    def test_states(self):
        self.assertEqual(self.cb()["status"], "ACTIVE")
        self.assertEqual(self.cb(consecutive_losses=1, daily_drawdown_pct=1.99)["status"], "ACTIVE")
        t = self.cb(consecutive_losses=2)
        self.assertEqual((t["status"], t["risk_pct"], t["risk_throttle"]), ("THROTTLED", 0.75, True))
        self.assertEqual(self.cb(daily_drawdown_pct=2.0)["status"], "THROTTLED")
        h = self.cb(consecutive_losses=3)
        self.assertEqual((h["status"], h["risk_pct"], h["cooldown_candles_remaining"]), ("HALTED", 0.0, 15))
        self.assertEqual(self.cb(daily_drawdown_pct=3.0)["status"], "HALTED")
        self.assertEqual(self.cb(cooldown_candles_remaining=4)["cooldown_candles_remaining"], 4)

    def test_halt_aborts_before_chart(self):
        # Ni siquiera hacen falta velas: la capa 1 aborta primero.
        r = q.run_engine({**HEALTHY, "consecutive_losses": 3}, candles=None, manual=None)
        self.assertEqual(r["decision"], "HALTED")
        self.assertFalse(r["layer_2_market_regime"]["evaluated"])
        self.assertIsNone(r["order"])

    def test_postmortem_and_cooldown(self):
        s = dict(HEALTHY)
        s = q.postmortem_update(s, -100, 9900, 10000)
        s = q.postmortem_update(s, -100, 9800, 10000)
        self.assertEqual((s["consecutive_losses"], s["daily_drawdown_pct"]), (2, 2.0))
        s = q.postmortem_update(s, -50, 9750, 10000)
        self.assertEqual((s["consecutive_losses"], s["cooldown_candles_remaining"]), (3, 15))
        for _ in range(15):
            s = q.tick_cooldown(s)
        self.assertEqual((s["cooldown_candles_remaining"], s["consecutive_losses"]), (0, 0))
        self.assertEqual(q.circuit_breaker(q.AccountState.from_dict(s), P)["status"], "THROTTLED")  # DD 2.5%
        self.assertEqual(q.postmortem_update(s, 30, 9780, 10000)["consecutive_losses"], 0)

    def test_cooldown_extends_while_dd_over_limit(self):
        s = {**HEALTHY, "daily_drawdown_pct": 3.2, "cooldown_candles_remaining": 1, "consecutive_losses": 3}
        s = q.tick_cooldown(s)
        self.assertEqual(s["cooldown_candles_remaining"], 15)


class TestRegime(unittest.TestCase):
    def regime(self, **over):
        return q.market_regime(q._manual_indicators(manual(**over), P), P)

    def test_bull_long(self):
        r = self.regime()
        self.assertEqual((r["regime"], r["signal"]), ("BULL_EXPANSION", "LONG"))

    def test_rsi_bounds_inclusive(self):
        self.assertEqual(self.regime(rsi14=46.0)["signal"], "LONG")
        self.assertEqual(self.regime(rsi14=64.0)["signal"], "LONG")
        r = self.regime(rsi14=64.01)
        self.assertEqual(r["signal"], "WAIT / NO_TRADE")
        self.assertIn("LONG.rsi_in_46_64", r["blocking_filters"])

    def test_slope_strict(self):
        r = self.regime(ema20=100.2, ema20_prev3=100.0, close=101)   # +0.2% exacto no basta
        self.assertEqual(r["regime"], "CHOP_RANGE")

    def test_exhaustion(self):
        r = self.regime(close=102.3, atr14=1.0)   # 3.3 ATR sobre EMA20
        self.assertEqual((r["regime"], r["signal"]), ("EXHAUSTION_CLIMAX", "WAIT / NO_TRADE"))
        self.assertEqual(self.regime(close=102.2)["regime"], "BULL_EXPANSION")   # 3.2 ATR exacto permitido

    def test_bear_short(self):
        r = self.regime(close=90, ema20=91, ema50=92, ema200=100, ema20_prev3=91.5, rsi14=45)
        self.assertEqual((r["regime"], r["signal"]), ("BEAR_DISTRIBUTION", "SHORT"))
        p = q.Params(allow_short=False)
        r2 = q.market_regime(q._manual_indicators(manual(close=90, ema20=91, ema50=92, ema200=100,
                                                         ema20_prev3=91.5, rsi14=45), p), p)
        self.assertEqual(r2["signal"], "WAIT / NO_TRADE")

    def test_chop_on_demo_range(self):
        r = q.run_engine(HEALTHY, candles=q.demo_chop_range())
        self.assertEqual(r["layer_2_market_regime"]["regime"], "CHOP_RANGE")


class TestRiskGuardian(unittest.TestCase):
    def test_long_formulas(self):
        r = q.atr_risk_guardian("LONG", 100.0, 2.0, 10000, 1.5, P)
        self.assertEqual((r["stop_loss"], r["take_profit"], r["break_even_trigger"], r["break_even_new_sl"]),
                         (96.8, 107.2, 103.0, 100.2))
        # 150 / 0.032 = 4687.50 USD  (< tope 8500)
        self.assertEqual(r["position_size_usd"], 4687.5)
        self.assertFalse(r["capped_by_max_allocation"])
        self.assertAlmostEqual(r["risk_usd_effective"], 150.0, places=2)
        self.assertEqual(r["reward_risk_ratio"], 2.25)

    def test_cap_85(self):
        r = q.atr_risk_guardian("LONG", 100.0, 0.5, 10000, 1.5, P)   # SL 0.8% -> 18750 USD -> tope
        self.assertTrue(r["capped_by_max_allocation"])
        self.assertEqual(r["position_size_usd"], 8500.0)

    def test_throttled_half_risk(self):
        r = q.atr_risk_guardian("LONG", 100.0, 2.0, 10000, 0.75, P)
        self.assertAlmostEqual(r["risk_usd_effective"], 75.0, places=2)

    def test_short_mirror(self):
        r = q.atr_risk_guardian("SHORT", 100.0, 2.0, 10000, 1.5, P)
        self.assertEqual((r["stop_loss"], r["take_profit"], r["break_even_trigger"], r["break_even_new_sl"]),
                         (103.2, 92.8, 97.0, 99.8))

    def test_throttled_end_to_end(self):
        r = q.run_engine({**HEALTHY, "consecutive_losses": 2}, manual=manual(atr14=2.0, close=100))
        self.assertEqual(r["layer_1_circuit_breaker"]["status"], "THROTTLED")
        self.assertEqual(r["layer_3_atr_risk_guardian"]["risk_pct_target"], 0.75)
        self.assertEqual(r["order"]["risk_mode"], "THROTTLED")


class TestIO(unittest.TestCase):
    def test_tradingview_csv(self):
        df = q.demo_bull_trend()
        tv = df.assign(time=(df.time - EPOCH) // pd.Timedelta("1s"))
        tv.columns = ["time", "open", "high", "low", "close", "Volume"]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "BINANCE_BTCUSDT, 60.csv"
            tv.to_csv(path, index=False)
            r = q.run_engine(HEALTHY, candles=q.load_csv(path))
        self.assertEqual(r["decision"], "LONG")
        self.assertEqual(r["candle_time"], df.time.iloc[-1].isoformat())

    def test_binance_headerless_csv_and_json_klines(self):
        df = q.demo_bull_trend()
        ms = ((df.time - EPOCH) // pd.Timedelta("1ms")).to_numpy()
        rows = [[int(t), o, h, l, c, v, int(t) + 3599999, 0, 0, 0, 0, 0]
                for t, o, h, l, c, v in zip(ms, df.open, df.high, df.low, df.close, df.volume)]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "BTCUSDT-1h.csv"
            pd.DataFrame(rows).to_csv(path, index=False, header=False)
            a = q.run_engine(HEALTHY, candles=q.load_csv(path))
        b = q.run_engine(HEALTHY, candles=q.load_json_candles({"candles": rows}))
        self.assertEqual(a["decision"], "LONG")
        self.assertEqual(a["layer_3_atr_risk_guardian"], b["layer_3_atr_risk_guardian"])
        self.assertEqual(a["candle_time"], df.time.iloc[-1].isoformat())

    def test_strict_json_and_short_history(self):
        r = q.run_engine(HEALTHY, candles=q.demo_bull_trend())
        json.dumps(r, allow_nan=False)
        with self.assertRaises(ValueError):
            q.run_engine(HEALTHY, candles=q.demo_bull_trend().iloc[:150])

    def test_cli_manual(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = q.main(["--manual", json.dumps(manual()), "--balance", "10000", "--json-only"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["decision"], "LONG")


if __name__ == "__main__":
    unittest.main()
