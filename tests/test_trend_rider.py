"""Perfil paralelo Trend Rider (SKILL_4): Capa 2 por ruptura, Capa 3 con trailing stop, estado y backtest."""
import logging
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backtester as bt  # noqa: E402
import quant_core_engine as q  # noqa: E402
from bot_runner import BotRunner, RunnerConfig  # noqa: E402
from notifier import Notifier  # noqa: E402
from state_manager import StateManager  # noqa: E402

logging.disable(logging.CRITICAL)

TREND = q.Params(strategy="trend")
BREAKOUT = q.demo_bull_trend().iloc[:498].reset_index(drop=True)   # la última vela rompe el máximo de 20


def ind(**kw):
    base = {"close": 105.0, "ema20": 102.0, "ema50": 100.0, "ema200": 90.0, "rsi14": 70.0, "atr14": 2.0,
            "ema20_slope_pct": 0.5, "donchian_high": 104.0, "donchian_low": 95.0}
    base.update(kw)
    return base


class TestTrendLayer2(unittest.TestCase):
    def test_breakout_long_ignores_rsi_and_extension(self):
        mr = q.trend_regime(ind(close=120.0), TREND)        # RSI 70 y 9 ATR de extensión: el oficial no entra
        self.assertEqual((mr["signal"], mr["entry_trigger"]), ("LONG", "breakout"))
        self.assertEqual(q.market_regime(ind(close=120.0), q.Params())["signal"], "WAIT / NO_TRADE")

    def test_blocking_filters_name_what_failed(self):
        mr = q.trend_regime(ind(close=103.0), TREND)
        self.assertEqual(mr["signal"], "WAIT / NO_TRADE")
        self.assertEqual(mr["blocking_filters"], ["TREND.LONG.breakout_close_gt_max20"])
        mr = q.trend_regime(ind(close=85.0, ema200=90.0, donchian_low=88.0), TREND)
        self.assertEqual(mr["signal"], "WAIT / NO_TRADE")             # cortos apagados por defecto (spot)
        self.assertIn("TREND.LONG.macro_close_gt_ema200", mr["blocking_filters"])

    def test_short_breakout_only_when_enabled(self):
        p = q.Params(strategy="trend", trend_allow_short=True)
        mr = q.trend_regime(ind(close=85.0, ema20=88.0, ema50=89.0, ema200=90.0, donchian_low=88.0), p)
        self.assertEqual((mr["signal"], mr["entry_trigger"]), ("SHORT", "breakout"))

    def test_pullback_mode_is_the_official_entry(self):
        p = q.Params(strategy="trend", trend_entry="pullback")
        official_ok = ind(close=103.0, rsi14=55.0)
        self.assertEqual(q.market_regime(official_ok, q.Params())["signal"], "LONG")
        self.assertEqual(q.trend_regime(official_ok, p)["entry_trigger"], "pullback")
        self.assertEqual(q.trend_regime(ind(close=120.0), p)["signal"], "WAIT / NO_TRADE")
        both = q.Params(strategy="trend", trend_entry="both")
        self.assertEqual(q.trend_regime(ind(close=120.0), both)["entry_trigger"], "breakout")
        self.assertEqual(q.trend_regime(official_ok, both)["entry_trigger"], "pullback")

    def test_invalid_profile_values(self):
        with self.assertRaises(ValueError):
            q.evaluate_layer2(ind(), q.Params(strategy="magic"))
        with self.assertRaises(ValueError):
            q.trend_regime(ind(), q.Params(strategy="trend", trend_entry="dip"))


class TestTrendLayer3AndEngine(unittest.TestCase):
    def test_risk_has_no_tp_nor_break_even(self):
        r = q.trend_risk_guardian("LONG", 100.0, 2.0, 10000.0, 1.5, TREND)
        self.assertEqual(r["stop_loss"], 95.0)                          # 100 - 2.5 x 2
        self.assertEqual((r["take_profit"], r["break_even_trigger"], r["reward_risk_ratio"]), (None, None, None))
        self.assertEqual(r["position_size_usd"], 3000.0)                # 150 USD / 5% de distancia
        self.assertEqual((r["exit_mode"], r["trail_atr_mult"], r["trail_distance_usd"]), ("trailing_stop", 4.0, 8.0))

    def test_engine_order_and_layer_order(self):
        r = q.run_engine({"balance": 10000}, candles=BREAKOUT, symbol="BTCUSDT", params=TREND)
        self.assertEqual((r["strategy"], r["decision"]), ("trend", "LONG"))
        o = r["order"]
        self.assertIsNone(o["take_profit"])
        self.assertIsNone(o["break_even"])
        self.assertEqual(o["trailing_stop"]["atr_mult"], 4.0)
        self.assertEqual(o["client_tag"], "qce-trend-breakout")
        self.assertIn("trailing stop", q.markdown_summary(r))
        # La Capa 1 sigue mandando: con 3 pérdidas aborta sin mirar el gráfico.
        h = q.run_engine({"balance": 10000, "consecutive_losses": 3}, candles=BREAKOUT, params=TREND)
        self.assertEqual(h["decision"], "HALTED")
        self.assertFalse(h["layer_2_market_regime"]["evaluated"])
        t = q.run_engine({"balance": 10000, "consecutive_losses": 2}, candles=BREAKOUT, params=TREND)
        self.assertEqual(t["layer_3_atr_risk_guardian"]["risk_pct_target"], 0.75)

    def test_official_profile_unchanged(self):
        r = q.run_engine({"balance": 10000}, candles=q.demo_bull_trend(), symbol="BTC/USDT", params=q.Params())
        self.assertEqual((r["strategy"], r["decision"]), ("official", "LONG"))
        self.assertEqual(r["order"]["take_profit"]["type"], "limit")
        self.assertNotIn("trailing_stop", r["order"])


class TestTrailingState(unittest.TestCase):
    def open(self, sm, side="LONG"):
        rk = q.trend_risk_guardian(side, 100.0, 2.0, 10000.0, 1.5, q.Params(strategy="trend", trend_allow_short=True))
        res = {"order": {"symbol": "BTCUSDT", "risk_mode": "ACTIVE"}, "layer_3_atr_risk_guardian": rk,
               "layer_2_market_regime": {"regime": "BULL_EXPANSION"}}
        return sm.positions()[0] if sm.open_from_engine(res, "2026-01-01T00:00:00+00:00") else None

    def test_long_trailing_ratchets_and_exits_as_win(self):
        sm = StateManager(":memory:", 10000.0, TREND)
        pos = self.open(sm)
        self.assertEqual((pos["take_profit"], pos["breakeven_trigger"], pos["trail_atr_mult"]), (0.0, 0.0, 4.0))
        # Una vela enorme no cierra por TP (no hay) ni mueve a BE (no hay).
        self.assertEqual(sm.process_candle("BTCUSDT", "t1", 100, 130, 99, 128), [])
        ev = sm.apply_trailing("BTCUSDT", "t1", 130, 99, 2.0)
        self.assertEqual((ev[0]["event"], ev[0]["new_stop_loss"]), ("TRAILING_STOP", 122.0))   # 130 - 4 x 2
        # Si el ATR crece, el SL no baja.
        self.assertEqual(sm.apply_trailing("BTCUSDT", "t2", 125, 120, 5.0), [])
        self.assertEqual(sm.positions()[0]["stop_loss"], 122.0)
        out = sm.process_candle("BTCUSDT", "t3", 124, 125, 121, 121)
        self.assertEqual((out[0]["result"], out[0]["exit_reason"]), ("WIN", "TRAILING_STOP"))
        self.assertEqual(sm.account()["consecutive_losses"], 0)

    def test_untrailed_stop_is_a_plain_stop_loss(self):
        sm = StateManager(":memory:", 10000.0, TREND)
        self.open(sm)
        out = sm.process_candle("BTCUSDT", "t1", 100, 101, 94, 95)
        self.assertEqual((out[0]["result"], out[0]["exit_reason"]), ("LOSS", "STOP_LOSS"))

    def test_short_mirror_and_live_price(self):
        sm = StateManager(":memory:", 10000.0, TREND)
        self.open(sm, "SHORT")
        self.assertEqual(sm.positions()[0]["stop_loss"], 105.0)
        ev = sm.apply_trailing("BTCUSDT", "t1", 101, 80, 2.0)
        self.assertEqual(ev[0]["new_stop_loss"], 88.0)                  # 80 + 4 x 2
        out = sm.update_price("BTCUSDT", 88.5)
        self.assertEqual((out[0]["result"], out[0]["exit_reason"]), ("WIN", "TRAILING_STOP"))

    def test_old_database_gets_trailing_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            con = sqlite3.connect(path)
            con.executescript("CREATE TABLE open_positions (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, "
                              "side TEXT NOT NULL, entry_price REAL NOT NULL, stop_loss REAL NOT NULL, "
                              "take_profit REAL NOT NULL, breakeven_trigger REAL NOT NULL, "
                              "breakeven_stop_price REAL NOT NULL, position_units REAL NOT NULL, "
                              "initial_stop_loss REAL NOT NULL, risk_usd REAL NOT NULL, "
                              "is_at_breakeven INTEGER NOT NULL DEFAULT 0, regime TEXT, risk_mode TEXT, "
                              "opened_at TEXT NOT NULL);")
            con.close()
            sm = StateManager(path, 10000.0, TREND)
            self.assertIsNotNone(self.open(sm))
            sm.close()


class TestTrendBacktestAndSweep(unittest.TestCase):
    def test_backtest_uses_trailing_and_never_take_profit(self):
        out = bt.run_backtest(q.demo_bull_trend(1500, seed=7), TREND)
        rep = out["report"]
        self.assertEqual(rep["config"]["strategy"], "trend")
        self.assertGreater(rep["results"]["trades"], 0)
        self.assertGreater(rep["trend"]["trailing_updates"], 0)
        self.assertFalse(any(t["exit_reason"].startswith("TAKE_PROFIT") for t in out["trades"]))
        self.assertEqual(rep["break_even"]["activations"], 0)
        self.assertIn("Trend Rider", bt.markdown_report(rep))

    def test_trend_preset_sweep(self):
        data = {("BTCUSDT", "1h"): q.demo_bull_trend(1200, seed=3)}
        rows = bt.run_sweep(data, bt.TREND_COMPARE[:3])
        self.assertEqual([r["variant"] for r in rows],
                         ["Oficial", "Trend Rider", "Trend Rider · entrada retroceso oficial"])
        self.assertEqual([r["official"] for r in rows], [True, False, False])
        self.assertIn("**Oficial** (oficial)", bt.markdown_sweep(rows))


class TestTrendRunner(unittest.TestCase):
    def test_runner_moves_stop_at_candle_close_and_labels_alerts(self):
        calls = []
        clock_t = [(BREAKOUT.time.iloc[-1] + pd.Timedelta(hours=1)).timestamp() + 10]
        data = [BREAKOUT]
        bot = BotRunner(RunnerConfig(["BTCUSDT"], "1h", ":memory:", 10000.0), q.Params(strategy="trend"),
                        Notifier("https://hook", post=lambda u, p: calls.append(p) or 200),
                        candle_source=lambda s, i, n: data[0], price_source=lambda s: float(data[0].close.iloc[-1]),
                        clock=lambda: clock_t[0])
        self.assertEqual(bot.candle_cycle()["symbols"]["BTCUSDT"]["decision"], "LONG")
        pos = bot.sm.positions("BTCUSDT")[0]
        self.assertTrue(calls[-1]["text"].startswith("[Trend Rider] 🟢 NUEVA POSICIÓN"))
        self.assertIn("Trailing stop", calls[-1]["text"])
        # Vela siguiente: sube fuerte sin tocar el SL -> el trailing sube y avisa al webhook.
        t = BREAKOUT.time.iloc[-1] + pd.Timedelta(hours=1)
        e = pos["entry_price"]
        nxt = pd.DataFrame({"time": [t], "open": [e], "high": [e * 1.06], "low": [e * 0.999],
                            "close": [e * 1.05], "volume": [1.0]})
        data[0] = pd.concat([BREAKOUT, nxt], ignore_index=True)
        clock_t[0] += 3600
        bot.candle_cycle()
        moved = bot.sm.positions("BTCUSDT")[0]
        self.assertGreater(moved["stop_loss"], pos["stop_loss"])
        trail = [c for c in calls if c["event"] == "trailing_stop"]
        self.assertEqual(len(trail), 1)
        self.assertEqual((trail[0]["bot"], trail[0]["data"]["new_stop_loss"]), ("Trend Rider", moved["stop_loss"]))
        bot.close()


if __name__ == "__main__":
    unittest.main()
