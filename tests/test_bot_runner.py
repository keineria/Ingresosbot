"""Ciclo de vida completo de bot_runner.py con velas, precios, reloj y alertas simulados."""
import logging
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quant_core_engine as q  # noqa: E402
from bot_runner import BotRunner, RunnerConfig  # noqa: E402
from notifier import Notifier  # noqa: E402

BULL = q.demo_bull_trend()          # último cierre 2026-01-21 19:00 UTC -> LONG válido
CHOP = q.demo_chop_range()
H = 3600


logging.disable(logging.CRITICAL)   # los avisos esperados no ensucian la salida


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append(payload)
        return 200

    def events(self):
        return [c["event"] for c in self.calls]


def retimed(df, clock):
    """Las mismas velas, desplazadas para que la última sea la vela recién cerrada según el reloj."""
    bar_open = pd.Timestamp((int(clock() * 1000) // (H * 1000)) * H * 1000 - H * 1000, unit="ms", tz="UTC")
    return df.assign(time=df.time + (bar_open - df.time.iloc[-1]))


def make_bot(data, prices, symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"), params=None):
    first_close = BULL.time.iloc[-1] + pd.Timedelta(hours=1)
    clock = Clock(first_close.timestamp() + 10)
    rec = Rec()
    bot = BotRunner(RunnerConfig(list(symbols), "1h", ":memory:", 10000.0, fee_pct=0.08), params,
                    Notifier("https://hook", post=rec),
                    candle_source=lambda s, i, n: retimed(data[s], clock), price_source=lambda s: prices[s],
                    clock=clock)
    return bot, clock, rec


class TestLifecycle(unittest.TestCase):
    def test_open_break_even_close(self):
        prices = {"BTCUSDT": float(BULL.close.iloc[-1]), "ETHUSDT": 1.0, "SOLUSDT": 1.0}
        bot, clock, rec = make_bot({"BTCUSDT": BULL, "ETHUSDT": CHOP, "SOLUSDT": CHOP}, prices)

        s = bot.candle_cycle()
        self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "LONG")
        self.assertEqual(s["symbols"]["ETHUSDT"]["decision"], "WAIT / NO_TRADE")
        self.assertEqual(rec.events(), ["position_opened"])
        pos = bot.sm.positions("BTCUSDT")[0]
        self.assertEqual(pos["entry_price"], round(prices["BTCUSDT"], 8))

        self.assertTrue(bot.candle_cycle()["skipped"])                  # misma vela: no repite

        clock.t += 15                                                   # vigilancia: sin cambios
        prices["BTCUSDT"] = pos["breakeven_trigger"] - 1
        self.assertEqual(bot.watch_cycle(), [])

        clock.t += 15                                                   # toca +1.5 ATR
        prices["BTCUSDT"] = pos["breakeven_trigger"]
        ev = bot.watch_cycle()
        self.assertEqual(ev[0]["event"], "BREAK_EVEN")
        moved = bot.sm.positions("BTCUSDT")[0]
        self.assertEqual((moved["stop_loss"], moved["is_at_breakeven"]), (pos["breakeven_stop_price"], 1))
        self.assertEqual(rec.events()[-1], "break_even")

        clock.t += 15                                                   # vuelve al SL protegido
        prices["BTCUSDT"] = pos["breakeven_stop_price"] - 0.5
        ev = bot.watch_cycle()
        self.assertEqual((ev[0]["event"], ev[0]["result"]), ("CLOSE", "BREAKEVEN"))
        self.assertEqual(bot.sm.positions(), [])
        self.assertEqual(bot.sm.account()["consecutive_losses"], 0)
        self.assertEqual(rec.events()[-1], "position_closed")
        self.assertIn("Racha de pérdidas: 0", rec.calls[-1]["text"])

    def test_one_position_per_pair_and_safety_net(self):
        prices = {"BTCUSDT": float(BULL.close.iloc[-1])}
        bot, clock, rec = make_bot({"BTCUSDT": BULL}, prices, symbols=("BTCUSDT",))
        bot.candle_cycle()
        pos = bot.sm.positions("BTCUSDT")[0]
        # La siguiente vela llega con una mecha al TP que la vigilancia no vio.
        t = BULL.time.iloc[-1] + pd.Timedelta(hours=1)
        nxt = pd.DataFrame({"time": [t], "open": [pos["entry_price"]], "high": [pos["take_profit"] + 1],
                            "low": [pos["entry_price"] - 1], "close": [pos["entry_price"]], "volume": [1.0]})
        bot.candle_source = lambda s, i, n: pd.concat([BULL, nxt], ignore_index=True)
        clock.t += H
        s = bot.candle_cycle()
        trades = bot.sm.trades()
        self.assertEqual((trades[0]["result"], trades[0]["exit_reason"]), ("WIN", "TAKE_PROFIT"))
        self.assertIn("position_closed", rec.events())
        self.assertIn(s["symbols"]["BTCUSDT"]["decision"], ("LONG", "WAIT / NO_TRADE"))
        clock.t += H
        if bot.sm.positions("BTCUSDT"):
            self.assertEqual(bot.candle_cycle()["symbols"]["BTCUSDT"]["decision"], "HOLD")

    def test_capital_is_not_overcommitted(self):
        prices = {s: float(BULL.close.iloc[-1]) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")}
        bot, _, _ = make_bot({s: BULL for s in prices}, prices)
        s = bot.candle_cycle()
        decisions = [s["symbols"][x]["decision"] for x in ("BTCUSDT", "ETHUSDT", "SOLUSDT")]
        self.assertEqual(decisions, ["LONG", "SKIPPED", "SKIPPED"])     # la 1ª ya usa el tope del 85%
        self.assertLessEqual(bot.sm.open_notional(), 8500.0 + 0.01)

        lev, _, _ = make_bot({s: BULL for s in prices}, prices, params=q.Params(max_leverage=3.0))
        s = lev.candle_cycle()
        self.assertEqual([s["symbols"][x]["decision"] for x in ("BTCUSDT", "ETHUSDT", "SOLUSDT")],
                         ["LONG", "LONG", "SKIPPED"])                    # 11.7k + 11.7k <= 25.5k

    def test_circuit_breaker_alerts_and_cooldown_per_candle(self):
        prices = {s: float(BULL.close.iloc[-1]) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")}
        bot, clock, rec = make_bot({s: BULL for s in prices}, prices)
        bot.sm.on_new_bar(pd.Timestamp(clock.t, unit="s", tz="UTC"))
        for _ in range(3):                                             # 3 pérdidas seguidas
            bot.sm.open_position("ETHUSDT", "LONG", 100.0, 96.8, 107.2, 103.0, 100.2, 5.0, 16.0, "2026-01-21T20:00:00+00:00")
            prices["ETHUSDT"] = 96.0
            bot.watch_cycle()
        prices["ETHUSDT"] = float(BULL.close.iloc[-1])
        status_alerts = [c["data"]["transition"]["to"] for c in rec.calls if c["event"] == "status_changed"]
        self.assertEqual(status_alerts, ["THROTTLED", "HALTED"])
        self.assertEqual(bot.sm.account()["cooldown_bars_remaining"], 15)

        s = bot.candle_cycle()                                          # 3 pares, pero 1 sola vela
        self.assertEqual(bot.sm.account()["cooldown_bars_remaining"], 14)
        self.assertTrue(all(v["decision"] == "HALTED" for v in s["symbols"].values()))
        self.assertEqual(bot.sm.positions(), [])
        for _ in range(14):
            clock.t += H
            bot.candle_cycle()
        acc = bot.sm.account()
        self.assertEqual((acc["cooldown_bars_remaining"], acc["consecutive_losses"]), (0, 0))
        self.assertEqual(len(bot.sm.positions()), 1)                    # vuelve a operar (y respeta el tope)

    def test_stale_candles_never_trade(self):
        prices = {"BTCUSDT": float(BULL.close.iloc[-1])}
        bot, clock, rec = make_bot({"BTCUSDT": BULL}, prices, symbols=("BTCUSDT",))
        bot.candle_source = lambda s, i, n: BULL                       # CSV viejo: termina 2 velas antes
        clock.t += 2 * H
        s = bot.candle_cycle()
        self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "STALE_DATA")
        self.assertEqual(bot.sm.positions(), [])
        self.assertEqual(rec.calls, [])

    def test_live_price_far_from_close_is_ignored(self):
        prices = {"BTCUSDT": float(BULL.close.iloc[-1]) * 1.20}        # precio absurdo (+20%)
        bot, _, _ = make_bot({"BTCUSDT": BULL}, prices, symbols=("BTCUSDT",))
        bot.candle_cycle()
        self.assertEqual(bot.sm.positions()[0]["entry_price"], round(float(BULL.close.iloc[-1]), 8))

    def test_missed_bars_are_ticked(self):
        prices = {"BTCUSDT": 1.0}
        bot, clock, _ = make_bot({"BTCUSDT": CHOP}, prices, symbols=("BTCUSDT",))
        bot.candle_cycle()
        bot.sm.conn.execute("UPDATE account_state SET cooldown_bars_remaining = 10, consecutive_losses = 3")
        clock.t += 4 * H                                                # el VPS estuvo caído 4 velas
        s = bot.candle_cycle()
        self.assertEqual(s["bars_ticked"], 4)
        self.assertEqual(bot.sm.account()["cooldown_bars_remaining"], 6)

    def test_run_forever_loop(self):
        prices = {"BTCUSDT": float(BULL.close.iloc[-1])}
        bot, clock, rec = make_bot({"BTCUSDT": BULL}, prices, symbols=("BTCUSDT",))
        sleeps = []
        bot.sleep = lambda s: (sleeps.append(s), setattr(clock, "t", clock.t + s))
        bot.run_forever(max_loops=3)
        self.assertEqual(sleeps, [15.0, 15.0])
        self.assertEqual(rec.events().count("position_opened"), 1)
        self.assertEqual(len(bot.sm.positions()), 1)


if __name__ == "__main__":
    unittest.main()
