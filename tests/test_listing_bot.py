"""Bot de nuevos listados (listing_bot.py) con un Binance falso: nada sale a internet."""
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import listing_bot as lb  # noqa: E402
from bot_runner import bot_settings  # noqa: E402
from notifier import Notifier  # noqa: E402
from web_server import DashboardApp  # noqa: E402

H = lb.H_MS
T0 = 1_788_220_800_000          # 2026-09-01 00:00 UTC
OLD = 1_502_942_400_000         # 2017-08-17: BTCUSDT


def bars(closes, start=T0, wick=0.01):
    """Velas de 1 h desde `start`: cada una abre en el cierre anterior."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append([start + i * H, o, max(o, c) * (1 + wick), min(o, c) * (1 - wick), c, 100.0,
                    start + (i + 1) * H - 1])
        prev = c
    return out


def listing(breakout_at=30, after=None, n=400):
    """Día 1 con máximo ~1,111; luego lateral bajo ese máximo; ruptura en la vela `breakout_at`."""
    closes = [1.0 + 0.1 * (i == 10) for i in range(24)] + [1.0] * (breakout_at - 24) + [1.2]
    closes += after if after is not None else [1.2 + 0.02 * k for k in range(15)] + [1.2] * 30
    closes += [closes[-1]] * (n - len(closes))
    return bars(closes)


class FakeBinance:
    def __init__(self, data: dict[str, list]):
        self.data = data
        self.now_ms = T0
        self.calls = []

    def get(self, url: str):
        self.calls.append(url)
        path, _, query = url.partition("?")
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        if path.endswith("/ticker/price"):
            return [{"symbol": s, "price": "1"} for s in self.data]
        rows = [r for r in self.data[qs["symbol"]] if r[0] >= int(qs["startTime"]) and r[0] <= self.now_ms]
        return rows[: int(qs["limit"])]

    def price(self, sym: str) -> float:
        """Precio en vivo = apertura de la vela en curso."""
        rows = [r for r in self.data[sym] if r[0] <= self.now_ms]
        return float(rows[-1][1])


class Harness:
    def __init__(self, data, tmp, balance=500.0):
        self.fake = FakeBinance({"BTCUSDT": bars([60000.0] * 5, start=OLD), **data})
        self.sent = []
        notifier = Notifier(telegram_token="t", telegram_chat_id="c", label=lb.LABEL,
                            post=lambda url, p: (self.sent.append(p["text"]), 200)[1])
        self.db = str(Path(tmp) / "listados.db")
        self.bot = lb.ListingBot(self.db, balance, notifier=notifier, get=self.fake.get,
                                 price_source=self.fake.price, clock=lambda: self.fake.now_ms / 1000,
                                 sleep=lambda s: None, hosts=["https://x"])

    def after_bar(self, k: int) -> dict:
        """Ciclo justo después de cerrar la vela k del listado."""
        self.fake.now_ms = T0 + (k + 1) * H + 10_000
        return self.bot.candle_cycle()

    def run(self, k_from: int, k_to: int) -> list[dict]:
        return [self.after_bar(k) for k in range(k_from, k_to)]


class TestListingBot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_breakout_entry_then_trailing_exit_and_only_one_trade(self):
        after = [1.2 + 0.05 * k for k in range(10)] + [1.3] * 10 + [1.25 + 0.1 * k for k in range(5)]
        h = Harness({"NEWUSDT": listing(30, after)}, self.tmp.name)
        cycles = h.run(0, 120)
        self.assertEqual(cycles[10]["symbols"]["NEWUSDT"]["decision"], "WAIT / NO_TRADE")
        self.assertIn("día 1", cycles[10]["symbols"]["NEWUSDT"]["reason"])
        self.assertIn("NEWUSDT", cycles[10]["watching"])
        self.assertEqual(cycles[30]["symbols"]["NEWUSDT"]["decision"], "LONG")
        trades = h.bot.sm.trades()
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertAlmostEqual(t["entry_price"], 1.2 * 1.002, places=9)       # apertura de la vela 31 + 0,2%
        self.assertEqual(t["exit_reason"], "TRAILING_STOP")
        self.assertEqual(t["result"], "WIN")
        peak = max(r[2] for r in h.fake.data["NEWUSDT"][31:])
        self.assertAlmostEqual(t["exit_price"], peak * 0.85 * (1 - lb.SLIP), places=6)
        self.assertEqual(h.bot.sm.positions(), [])
        self.assertIn("NEWUSDT", json.loads(h.bot.sm.meta_get(lb.K_DONE)))
        self.assertTrue(any("COMPRA (papel) NEWUSDT" in s for s in h.sent))
        self.assertTrue(any("ya no puede perder" in s for s in h.sent))
        self.assertTrue(all(s.startswith("[Nuevos listados]") for s in h.sent))

    def test_stop_loss_and_position_size(self):
        h = Harness({"NEWUSDT": listing(30, [1.0] * 5)}, self.tmp.name)
        h.run(29, 31)
        pos = h.bot.sm.positions()[0]
        self.assertAlmostEqual(pos["position_units"] * pos["entry_price"], 50.0)   # 10% de 500
        self.assertAlmostEqual(pos["stop_loss"], pos["entry_price"] * 0.85)
        h.run(31, 33)
        t = h.bot.sm.trades()[0]
        self.assertEqual(t["exit_reason"], "STOP_LOSS")
        self.assertAlmostEqual(t["exit_price"], pos["entry_price"] * 0.85 * (1 - lb.SLIP))   # tocó el stop
        self.assertEqual(t["result"], "LOSS")
        self.assertAlmostEqual(t["pnl_pct"], -15.0 - 0.2 * 0.85 - 0.08 * 1.85, delta=0.05)

    def test_late_breakout_is_not_bought(self):
        h = Harness({"NEWUSDT": listing(30)}, self.tmp.name)
        c = h.after_bar(40)                                  # el bot arranca cuando la ruptura ya pasó
        self.assertEqual(c["symbols"]["NEWUSDT"]["decision"], "SKIPPED")
        self.assertIn("no se compra tarde", c["symbols"]["NEWUSDT"]["reason"])
        self.assertEqual(h.bot.sm.positions(), [])
        c = h.after_bar(41)
        self.assertNotIn("NEWUSDT", c["symbols"])

    def test_time_exit_after_168_hours(self):
        h = Harness({"NEWUSDT": listing(30, [1.2 + 0.001 * k for k in range(300)])}, self.tmp.name)
        h.run(29, 31 + 170)
        t = h.bot.sm.trades()[0]
        self.assertEqual(t["exit_reason"], "TIEMPO")
        self.assertEqual(t["closed_at"], lb._iso(T0 + (31 + 168 + 1) * H))

    def test_no_more_than_three_positions(self):
        data = {f"{c * 3}USDT": listing(30) for c in "ACDE"}
        h = Harness(data, self.tmp.name)
        c = h.run(29, 31)[-1]
        self.assertEqual(len(h.bot.sm.positions()), 3)
        skipped = [s for s, d in c["symbols"].items() if d["decision"] == "SKIPPED"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("ya hay 3 posiciones", c["symbols"][skipped[0]]["reason"])

    def test_old_coins_stables_and_tokenized_stocks_are_ignored(self):
        self.assertFalse(lb.candidate("USDCUSDT"))
        self.assertFalse(lb.candidate("BTCUPUSDT"))
        self.assertFalse(lb.candidate("BNSOLUSDT"))
        self.assertFalse(lb.candidate("ETHBTC"))
        self.assertTrue(lb.candidate("NEWUSDT"))
        h = Harness({"NEWUSDT": listing(30), "NVDABUSDT": listing(30), "USDCUSDT": listing(30)}, self.tmp.name)
        c = h.after_bar(20)
        self.assertEqual(c["watching"], ["NEWUSDT"])
        self.assertNotIn("BTCUSDT", c["symbols"])
        # la fecha de listado se pregunta una sola vez por moneda
        n = len(h.fake.calls)
        h.after_bar(21)
        self.assertFalse(any("startTime=0" in u for u in h.fake.calls[n:]))

    def test_kill_switch_at_minus_20_percent(self):
        h = Harness({"NEWUSDT": listing(30)}, self.tmp.name)
        h.bot.sm.conn.execute("UPDATE account_state SET balance = 395 WHERE id = 1")
        h.bot.sm.conn.commit()
        h.bot.kill_check()
        self.assertTrue(h.bot.sm.account()["manual_halt"])
        self.assertTrue(any("KILL SWITCH ACTIVADO" in s for s in h.sent))
        c = h.run(29, 31)[-1]                               # la ruptura llega con el bot apagado
        self.assertEqual(c["symbols"]["NEWUSDT"]["decision"], "SKIPPED")
        self.assertEqual(h.bot.sm.positions(), [])
        h.bot.sm.set_manual_halt(False, "lo enciende Keiner")
        h.bot.kill_check()
        self.assertFalse(h.bot.sm.account()["manual_halt"])
        self.assertEqual(float(h.bot.sm.meta_get(lb.K_PEAK)), 395.0)

    def test_close_all_from_panel(self):
        h = Harness({"NEWUSDT": listing(30)}, self.tmp.name)
        h.run(29, 31)
        h.bot.sm.request_close_all("apagar", "panel")
        ev = h.bot.watch_cycle()
        self.assertEqual(ev[0]["exit_reason"], "MANUAL_CLOSE")
        self.assertIsNone(h.bot.sm.close_all_request())
        self.assertEqual(json.loads(h.bot.sm.meta_get(lb.K_POS)), {})

    def test_panel_card_uses_the_watched_coins(self):
        h = Harness({"NEWUSDT": listing(30)}, self.tmp.name)
        h.run(29, 32)
        h.bot.heartbeat(h.fake.now_ms / 1000)
        bot = {"id": "listados", "name": "Nuevos listados", "db": h.db, "interval": "1h", "symbols": [],
               "dynamic_symbols": True, "balance": 500, "service": "ingresosbot-listados"}
        cfg, p = bot_settings(bot)
        app = DashboardApp(cfg, p, "k" * 20, notifier=Notifier(), price_source=h.fake.price,
                           clock=lambda: h.fake.now_ms / 1000, bot=bot, service_state=lambda s: None)
        live = app.live()
        self.assertEqual(live["symbols"], ["NEWUSDT"])
        self.assertEqual(live["runner"]["health"], "running")
        self.assertEqual(live["runner"]["decisions"]["NEWUSDT"]["decision"], "HOLD")
        self.assertEqual(live["open_positions"][0]["symbol"], "NEWUSDT")
        self.assertEqual(app.state()["config"]["symbols"], ["NEWUSDT"])
        self.assertEqual(app.summary()["symbols"], ["NEWUSDT"])


if __name__ == "__main__":
    unittest.main()
