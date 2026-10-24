"""Pruebas de web_server.py: servidor real en 127.0.0.1 (puerto libre), velas/precios/alertas simulados."""
import json
import logging
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quant_core_engine as q  # noqa: E402
import web_server as ws  # noqa: E402
from bot_runner import RunnerConfig, settings_from_env  # noqa: E402
from notifier import Notifier  # noqa: E402
from state_manager import StateManager  # noqa: E402

logging.disable(logging.CRITICAL)

KEY = "clave-de-prueba-0123456789"
BULL = q.demo_bull_trend()
CHOP = q.demo_chop_range()


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append(payload)
        return 200


class WebServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "trading_state.db")
        self.cfg = RunnerConfig(["BTCUSDT", "ETHUSDT"], "1h", self.db, 10000.0)
        self.rec = Rec()
        self.prices = {"BTCUSDT": 72600.0, "ETHUSDT": 3000.0}
        data = {"BTCUSDT": BULL, "ETHUSDT": CHOP}
        self.app = ws.DashboardApp(self.cfg, q.Params(), KEY, Notifier("https://hook", post=self.rec),
                                   candle_source=lambda s, i, n: data[s], price_source=lambda s: self.prices[s])
        self.server = ws.build_server(self.app, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, path, method="GET", body=None, key=KEY, query_key=None):
        url = self.base + path + (f"?key={query_key}" if query_key else "")
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(url, data=data, method=method)
        if key:
            req.add_header("X-Access-Key", key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if "json" in resp.headers["Content-Type"] else raw.decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    # ── Autenticación ──
    def test_auth(self):
        self.assertEqual(self.call("/api/state", key=None)[0], 401)
        self.assertEqual(self.call("/api/state", key="incorrecta")[0], 401)
        self.assertEqual(self.call("/api/state")[0], 200)
        self.assertEqual(self.call("/api/state", key=None, query_key=KEY)[0], 200)        # ?key= en GET
        self.assertEqual(self.call("/api/kill-switch", "POST", {"action": "halt"}, key=None, query_key=KEY)[0], 401)
        self.assertEqual(self.call("/api/scan-now", "POST", {}, key=None)[0], 401)
        self.assertEqual(self.call("/healthz", key=None), (200, {"ok": True}))
        status, html = self.call("/", key=None)
        self.assertEqual(status, 200)
        self.assertIn("Kill Switch", html)
        self.assertEqual(self.call("/no-existe")[0], 404)
        with self.assertRaises(ValueError):
            ws.DashboardApp(self.cfg, q.Params(), "")

    # ── /api/state ──
    def test_state(self):
        with StateManager(self.db, 10000, fee_pct=0) as sm:
            sm.meta_set("last_bar_open_ms", "1768932000000")
            pid = sm.open_position("BTCUSDT", "LONG", 72500.0, 71570.0, 74590.0, 73370.0, 72645.0, 0.1, 93.0,
                                   "2026-01-21T20:00:00+00:00")
            sm.open_position("ETHUSDT", "LONG", 2900.0, 2850.0, 3020.0, 2950.0, 2905.8, 1.0, 50.0,
                             "2026-01-21T20:00:00+00:00")
            sm.update_price("ETHUSDT", 3025.0)          # TP -> WIN
        status, s = self.call("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(s["account"]["status"], "ACTIVE")
        self.assertAlmostEqual(s["account"]["balance"], 10125.0)
        self.assertEqual(s["runner"]["last_bar_open"], "2026-01-20T18:00:00+00:00")
        pos = s["open_positions"]
        self.assertEqual([p["id"] for p in pos], [pid])
        self.assertEqual(pos[0]["current_price"], 72600.0)
        self.assertAlmostEqual(pos[0]["unrealized_pnl_usd"], 10.0)
        self.assertAlmostEqual(s["exposure"]["open_notional_usd"], 7250.0)
        self.assertAlmostEqual(s["exposure"]["cap_usd"], 10125.0 * 0.85)
        self.assertEqual(len(s["recent_trades"]), 1)
        self.assertEqual(s["recent_trades"][0]["result"], "WIN")
        self.assertEqual((s["metrics"]["trades"], s["metrics"]["win_rate_pct"]), (1, 100.0))
        self.assertEqual(s["config"]["symbols"], ["BTCUSDT", "ETHUSDT"])

    def test_state_without_price_still_works(self):
        with StateManager(self.db, 10000) as sm:
            sm.open_position("BTCUSDT", "LONG", 72500.0, 71570.0, 74590.0, 73370.0, 72645.0, 0.1, 93.0, "x")
        self.app.price_source = lambda s: (_ for _ in ()).throw(OSError("sin red"))
        self.app._price_cache.clear()
        status, s = self.call("/api/state")
        self.assertEqual(status, 200)
        self.assertIsNone(s["open_positions"][0]["current_price"])

    # ── /api/scan-now ──
    def test_scan_now_is_diagnostic_only(self):
        status, r = self.call("/api/scan-now", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(r["diagnostic_only"])
        btc, eth = r["symbols"]["BTCUSDT"], r["symbols"]["ETHUSDT"]
        self.assertEqual((btc["decision"], btc["would_open"]), ("LONG", True))
        self.assertEqual(btc["layer_1"]["status"], "ACTIVE")
        self.assertEqual(btc["layer_2"]["regime"], "BULL_EXPANSION")
        self.assertEqual(btc["layer_3"]["stop_loss"], 71622.73)
        self.assertEqual(eth["decision"], "WAIT / NO_TRADE")
        self.assertTrue(eth["layer_2"]["blocking_filters"])
        with StateManager(self.db, 10000) as sm:
            self.assertEqual(sm.positions(), [])           # no abre órdenes
        self.assertEqual(self.call("/api/state")[1]["last_scan"]["symbols"]["BTCUSDT"]["decision"], "LONG")

    def test_scan_reports_open_position_and_errors(self):
        with StateManager(self.db, 10000) as sm:
            sm.open_position("BTCUSDT", "LONG", 72500.0, 71570.0, 74590.0, 73370.0, 72645.0, 0.1, 93.0, "x")
        self.app.candle_source = lambda s, i, n: BULL if s == "BTCUSDT" else (_ for _ in ()).throw(OSError("caído"))
        r = self.call("/api/scan-now", "POST", {})[1]
        self.assertEqual((r["symbols"]["BTCUSDT"]["position_open"], r["symbols"]["BTCUSDT"]["would_open"]), (True, False))
        self.assertEqual(r["symbols"]["ETHUSDT"]["decision"], "ERROR")

    # ── /api/kill-switch ──
    def test_kill_switch_cycle(self):
        status, r = self.call("/api/kill-switch", "POST", {"action": "halt", "reason": "prueba"})
        self.assertEqual(status, 200)
        self.assertEqual((r["status_before"], r["status_after"], r["manual_halt"]), ("ACTIVE", "HALTED", True))
        self.assertTrue(r["notified"])
        self.assertEqual(self.rec.calls[-1]["event"], "kill_switch")
        self.assertIn("KILL SWITCH ACTIVADO", self.rec.calls[-1]["text"])

        s = self.call("/api/state")[1]
        self.assertEqual((s["account"]["status"], s["account"]["manual_halt"]), ("HALTED", True))
        self.assertEqual(s["control_log"][0]["action"], "KILL_SWITCH_ON")
        self.assertEqual(s["control_log"][0]["reason"], "prueba")

        # El motor y el bot (otro proceso, otra conexión) respetan el kill switch.
        scan = self.call("/api/scan-now", "POST", {})[1]
        self.assertEqual(scan["symbols"]["BTCUSDT"]["decision"], "HALTED")
        self.assertIn("kill switch manual activado", scan["symbols"]["BTCUSDT"]["layer_1"]["reasons"])
        with StateManager(self.db, 10000) as other:
            with self.assertRaises(RuntimeError):
                other.open_position("BTCUSDT", "LONG", 1, 0.9, 1.2, 1.1, 1.002, 1, 0.1, "x")
            other.on_new_bar("2026-01-22T00:00:00+00:00")   # un guardado del bot no borra el interruptor
            self.assertEqual(other.account()["status"], "HALTED")

        r = self.call("/api/kill-switch", "POST", {"action": "toggle"})[1]
        self.assertEqual((r["status_after"], r["manual_halt"]), ("ACTIVE", False))
        self.assertIn("KILL SWITCH DESACTIVADO", self.rec.calls[-1]["text"])

    def test_resume_does_not_bypass_circuit_breaker(self):
        with StateManager(self.db, 10000) as sm:
            sm.conn.execute("UPDATE account_state SET consecutive_losses = 3, cooldown_bars_remaining = 9, status = 'HALTED'")
            sm.conn.commit()
        self.call("/api/kill-switch", "POST", {"action": "halt"})
        r = self.call("/api/kill-switch", "POST", {"action": "resume"})[1]
        self.assertEqual((r["manual_halt"], r["status_after"]), (False, "HALTED"))

    def test_bad_requests(self):
        self.assertEqual(self.call("/api/kill-switch", "POST", {"action": "explotar"})[0], 400)
        req = urllib.request.Request(self.base + "/api/kill-switch", data=b"{no es json", method="POST",
                                     headers={"X-Access-Key": KEY})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)


class SettingsTest(unittest.TestCase):
    def test_env_settings_shared_by_bot_and_panel(self):
        from unittest import mock
        env = {"BOT_SYMBOLS": "btcusdt, solusdt", "BOT_INTERVAL": "4h", "BOT_DB": "x.db", "BOT_BALANCE": "2500",
               "BOT_PARAMS": '{"max_leverage": 2.0}', "BOT_NO_SHORT": "true"}
        with mock.patch.dict("os.environ", env, clear=True):
            cfg, p = settings_from_env()
        self.assertEqual((cfg.symbols, cfg.interval, cfg.db_path, cfg.initial_balance),
                         (["BTCUSDT", "SOLUSDT"], "4h", "x.db", 2500.0))
        self.assertEqual((p.max_leverage, p.allow_short), (2.0, False))

    def test_old_database_is_migrated(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE account_state (id INTEGER PRIMARY KEY, balance REAL NOT NULL, initial_balance REAL NOT NULL, "
                        "consecutive_losses INTEGER NOT NULL DEFAULT 0, daily_drawdown_pct REAL NOT NULL DEFAULT 0, "
                        "cooldown_bars_remaining INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ACTIVE', "
                        "day_peak_equity REAL NOT NULL, current_day TEXT, updated_at TEXT NOT NULL)")
            con.execute("INSERT INTO account_state VALUES (1, 9000, 10000, 1, 0.5, 0, 'ACTIVE', 9100, NULL, 'x')")
            con.commit()
            con.close()
            with StateManager(path) as sm:
                a = sm.account()
                self.assertEqual((a["balance"], a["manual_halt"]), (9000, 0))
                self.assertEqual(sm.set_manual_halt(True)["status_after"], "HALTED")


if __name__ == "__main__":
    unittest.main()
