"""Varios bots con nombre propio: deploy/bots.json, Tortuga 55 en velas diarias, panel multi-bot e informe diario."""
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import daily_report as dr  # noqa: E402
import quant_core_engine as q  # noqa: E402
import web_server as ws  # noqa: E402
from bot_runner import BotRunner, RunnerConfig, bot_settings, find_bot, load_bots  # noqa: E402
from notifier import Notifier  # noqa: E402
from state_manager import StateManager  # noqa: E402

logging.disable(logging.CRITICAL)
KEY = "clave-de-prueba-0123456789"
DAY = 86_400


def daily_breakout(n=420, seed=7):
    """Subida lenta con ruido y, en la última vela, un cierre por encima del máximo de 55 días."""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.002, 0.01, n)))
    close[-1] = close[-56:-1].max() * 1.03
    time = pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")
    opens = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"time": time, "open": opens, "high": np.maximum(opens, close) * 1.004,
                         "low": np.minimum(opens, close) * 0.996, "close": close, "volume": 1.0})


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append(payload)
        return 200


class TestBotsFile(unittest.TestCase):
    def test_repo_bots_file(self):
        bots = load_bots(ROOT / "deploy" / "bots.json")
        self.assertEqual([b["id"] for b in bots], ["oficial", "trend", "tortuga"])
        for b in bots:
            self.assertTrue(b["summary"] and b["how"] and b["backtest"], b["id"])
            self.assertTrue(b.get("trader"), b["id"])           # nombre de persona en el panel
            if b.get("params"):
                self.assertTrue((ROOT / b["params"]).exists(), b["params"])
        cfg, p = bot_settings(find_bot("tortuga", ROOT / "deploy" / "bots.json"))
        self.assertEqual((cfg.interval, cfg.db_path, cfg.initial_balance), ("1d", "data/tortuga_state.db", 500.0))
        self.assertIn("PAXGUSDT", cfg.symbols)
        self.assertEqual((p.strategy, p.donchian_len, p.trend_sl_atr_mult, p.trail_atr_mult, p.daily_sma_len),
                         ("trend", 55, 3.0, 5.0, 200))
        self.assertFalse(p.trend_allow_short)
        # Trend Rider en el panel = lo mismo que su servicio systemd (4h, su base, sus parámetros).
        cfg, p = bot_settings(find_bot("trend", ROOT / "deploy" / "bots.json"))
        self.assertEqual((cfg.interval, cfg.db_path, p.donchian_len, p.trail_atr_mult), ("4h", "data/trend_state.db", 20, 5.0))

    def test_invalid_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bots.json"
            self.assertEqual(load_bots(path), [])                       # sin archivo: un solo bot, como antes
            path.write_text(json.dumps([{"id": "a", "name": "A", "db": "a.db"}, {"id": "a", "name": "B", "db": "b.db"}]))
            with self.assertRaises(SystemExit):
                load_bots(path)
            path.write_text(json.dumps([{"id": "a", "name": "A"}]))
            with self.assertRaises(SystemExit):
                load_bots(path)
            with self.assertRaises(SystemExit):
                find_bot("nadie", ROOT / "deploy" / "bots.json")


class TestTortugaDaily(unittest.TestCase):
    def test_opens_on_daily_55_breakout_with_its_label(self):
        df = daily_breakout()
        _, p = bot_settings(find_bot("tortuga", ROOT / "deploy" / "bots.json"))
        last_close = df.time.iloc[-1] + pd.Timedelta(days=1)
        rec = Rec()
        bot = BotRunner(RunnerConfig(["BTCUSDT"], "1d", ":memory:", 500.0), p,
                        Notifier("https://hook", post=rec, label="Tortuga 55"),
                        candle_source=lambda s, i, n: df, price_source=lambda s: float(df.close.iloc[-1]),
                        clock=lambda: last_close.timestamp() + 10)
        summary = bot.candle_cycle()
        self.assertEqual(summary["symbols"]["BTCUSDT"]["decision"], "LONG", summary)
        pos = bot.sm.positions("BTCUSDT")
        self.assertEqual(len(pos), 1)
        atr = q.compute_indicators(df, p)["atr14"].iloc[-1]
        self.assertAlmostEqual(pos[0]["entry_price"] - pos[0]["stop_loss"], 3.0 * atr, delta=0.01 * atr + 0.001)
        self.assertTrue(rec.calls and all(c["bot"] == "Tortuga 55" for c in rec.calls))
        self.assertTrue(rec.calls[0]["text"].startswith("[Tortuga 55]"))
        bot.close()

    def test_no_entry_without_breakout(self):
        df = daily_breakout()
        df.loc[df.index[-1], "close"] = df.close.iloc[-2]        # sin ruptura
        _, p = bot_settings(find_bot("tortuga", ROOT / "deploy" / "bots.json"))
        bot = BotRunner(RunnerConfig(["BTCUSDT"], "1d", ":memory:", 500.0), p, Notifier(),
                        candle_source=lambda s, i, n: df, price_source=lambda s: float(df.close.iloc[-1]),
                        clock=lambda: (df.time.iloc[-1] + pd.Timedelta(days=1)).timestamp() + 10)
        self.assertNotEqual(bot.candle_cycle()["symbols"]["BTCUSDT"]["decision"], "LONG")
        self.assertEqual(bot.sm.positions(), [])
        bot.close()


class TestMultiBotPanel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.bots = [
            {"id": "oficial", "name": "Bot Oficial", "db": str(d / "o.db"), "interval": "1h", "symbols": ["BTCUSDT"],
             "balance": 10000, "summary": "s", "how": ["h"], "backtest": "b"},
            {"id": "tortuga", "name": "Tortuga 55", "db": str(d / "t.db"), "interval": "1d", "symbols": ["BTCUSDT"],
             "balance": 500, "params": str(ROOT / "deploy" / "tortuga_params.json"),
             "summary": "Ruptura de 55 días", "how": ["regla"], "backtest": "+51%"},
        ]
        df = daily_breakout()
        self.reg = ws.BotRegistry.from_bots(self.bots, KEY, candle_source=lambda s, i, n: df,
                                            price_source=lambda s: 123.0)
        self.server = ws.build_server(self.reg, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, path, method="GET", body=None, key=KEY):
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("X-Access-Key", key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_bots_list_and_state_per_bot(self):
        with StateManager(self.bots[1]["db"], 500, fee_pct=0) as sm:
            pid = sm.open_position("BTCUSDT", "LONG", 100.0, 95.0, 120.0, 110.0, 100.2, 1.0, 5.0, "x")
            sm.close_position(pid, 110.0, "MANUAL", "2026-10-24T00:00:00+00:00")
        self.assertEqual(self.call("/api/bots", key="mala")[0], 401)
        status, out = self.call("/api/bots")
        self.assertEqual(status, 200)
        self.assertEqual(out["default"], "oficial")
        t = {b["id"]: b for b in out["bots"]}["tortuga"]
        self.assertEqual((t["name"], t["interval"], t["balance"], t["return_pct"], t["trades"]),
                         ("Tortuga 55", "1d", 510.0, 2.0, 1))
        self.assertEqual(t["how"], ["regla"])
        s = self.call("/api/state?bot=tortuga")[1]
        self.assertEqual((s["bot"]["name"], s["config"]["interval"], s["config"]["donchian_len"]), ("Tortuga 55", "1d", 55))
        self.assertEqual(s["account"]["balance"], 510.0)
        self.assertEqual(self.call("/api/state")[1]["bot"]["id"], "oficial")
        self.assertEqual(self.call("/api/state?bot=nadie")[0], 404)

    def test_kill_switch_and_scan_only_touch_their_bot(self):
        status, r = self.call("/api/kill-switch?bot=tortuga", "POST", {"action": "halt"})
        self.assertEqual((status, r["account"]["manual_halt"]), (200, True))
        self.assertFalse(self.call("/api/state?bot=oficial")[1]["account"]["manual_halt"])
        self.assertTrue(self.call("/api/state?bot=tortuga")[1]["account"]["manual_halt"])
        self.call("/api/kill-switch", "POST", {"action": "resume", "bot": "tortuga"})
        self.assertFalse(self.call("/api/state?bot=tortuga")[1]["account"]["manual_halt"])
        status, scan = self.call("/api/scan-now?bot=tortuga", "POST", {})
        self.assertEqual(status, 200)
        l2 = scan["symbols"]["BTCUSDT"]["layer_2"]
        self.assertEqual((scan["symbols"]["BTCUSDT"]["decision"], l2["strategy"]), ("LONG", "trend"))
        self.assertTrue(all(l2["breakout_long_checks"].values()))
        self.assertIn("breakout_close_gt_max55", l2["breakout_long_checks"])


class TestLiveOverview(unittest.TestCase):
    """GET /api/overview: los bots en vivo a la vez, y bots.json que se recarga solo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.now = pd.Timestamp("2026-10-25T13:30:00Z").timestamp()
        self.services = {"ingresosbot-trend": "active", "ingresosbot-tortuga": "inactive"}
        self.bots = [
            {"id": "trend", "name": "Trend Rider", "trader": "Keiner", "service": "ingresosbot-trend",
             "db": str(self.d / "tr.db"),
             "interval": "4h", "symbols": ["BTCUSDT", "ETHUSDT"], "balance": 500,
             "params": str(ROOT / "deploy" / "trend_params.json")},
            {"id": "tortuga", "name": "Tortuga 55", "service": "ingresosbot-tortuga", "db": str(self.d / "to.db"),
             "interval": "1d", "symbols": ["BTCUSDT"], "balance": 500, "color": 3,
             "params": str(ROOT / "deploy" / "tortuga_params.json")},
        ]
        self.path = self.d / "bots.json"
        self.path.write_text(json.dumps(self.bots))
        self.reg = ws.BotRegistry.from_file(self.path, KEY, price_source=lambda s: 110.0,
                                            clock=lambda: self.now, service_state=self.services.get)

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_card_data(self):
        with StateManager(self.bots[0]["db"], 500, fee_pct=0) as sm:
            pid = sm.open_position("ETHUSDT", "LONG", 100.0, 95.0, 120.0, 110.0, 100.2, 1.0, 5.0,
                                   "2026-10-20T00:00:00+00:00")
            sm.close_position(pid, 104.0, "TRAILING_STOP", "2026-10-25T08:00:00+00:00")
            sm.open_position("BTCUSDT", "LONG", 100.0, 95.0, 0.0, 0.0, 0.0, 2.0, 5.0, "2026-10-25T12:00:00+00:00")
            sm.meta_set("last_cycle", json.dumps({"processed_at": "2026-10-25T12:00:06+00:00",
                                                  "bar_open": "2026-10-25T08:00:00+00:00",
                                                  "symbols": {"BTCUSDT": {"decision": "LONG", "position_id": 2},
                                                              "ETHUSDT": {"decision": "WAIT / NO_TRADE",
                                                                          "blocking": ["breakout"]}}}))
            sm.meta_set("heartbeat", "2026-10-25T13:29:10+00:00")
        out = self.reg.overview()
        self.assertEqual([b["id"] for b in out["bots"]], ["trend", "tortuga"])
        t, to = out["bots"]
        self.assertEqual((t["account"]["balance"], t["unrealized_pnl_usd"], t["equity"]), (504.0, 20.0, 524.0))
        self.assertEqual((t["return_pct"], t["realized_return_pct"]), (4.8, 0.8))
        pos = t["open_positions"][0]
        self.assertEqual((pos["symbol"], pos["current_price"], pos["unrealized_pct"]), ("BTCUSDT", 110.0, 10.0))
        self.assertEqual(t["recent_trades"][0]["exit_reason"], "TRAILING_STOP")
        self.assertEqual(t["started_at"], "2026-10-20T00:00:00+00:00")
        r = t["runner"]
        self.assertEqual((r["health"], r["heartbeat_age_s"], r["next_bar_close"]),
                         ("running", 50, "2026-10-25T16:00:00+00:00"))
        self.assertEqual(r["decisions"]["ETHUSDT"], {"decision": "WAIT / NO_TRADE", "blocking": ["breakout"]})
        self.assertEqual(t["limits"]["halt_losses"], 3)
        self.assertEqual((t["trader"], t["name"]), ("Keiner", "Trend Rider"))
        # Tortuga: base nueva, servicio apagado.
        self.assertEqual((to["runner"]["health"], to["equity"], to["color"]), ("stopped", 500.0, 3))
        self.assertEqual(to["runner"]["next_bar_close"], "2026-10-26T00:00:00+00:00")

    def test_health_without_heartbeat(self):
        self.services.clear()                              # sin systemd (p. ej. en local)
        self.assertEqual(self.reg.overview()["bots"][0]["runner"]["health"], "never")
        with StateManager(self.bots[0]["db"], 500) as sm:
            sm.meta_set("heartbeat", "2026-10-25T12:00:00+00:00")   # hace 90 min
        self.assertEqual(self.reg.overview()["bots"][0]["runner"]["health"], "stale")

    def test_new_bot_appears_without_restart(self):
        import os
        old = self.reg.apps["trend"]
        self.bots.append({"id": "cuarto", "name": "Cuarto bot", "db": str(self.d / "c.db"), "interval": "1h",
                          "symbols": ["BTCUSDT"], "balance": 500})
        self.path.write_text(json.dumps(self.bots))
        os.utime(self.path, (self.now, self.now + 5))
        self.assertEqual([b["id"] for b in self.reg.overview()["bots"]], ["trend", "tortuga", "cuarto"])
        self.assertIs(self.reg.apps["trend"], old)          # los que no cambian se conservan
        self.path.write_text("[{]")                         # mal escrito: se queda con lo anterior
        os.utime(self.path, (self.now, self.now + 10))
        self.assertEqual(len(self.reg.overview()["bots"]), 3)

    def test_heartbeat_from_runner(self):
        clock = [self.now]
        bot = BotRunner(RunnerConfig(["BTCUSDT"], "4h", ":memory:", 500.0), q.Params(), Notifier(),
                        candle_source=lambda s, i, n: daily_breakout(), price_source=lambda s: 1.0,
                        clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        bot.cfg.close_delay_seconds = 10 ** 9                # solo vigilancia, sin ciclo de vela
        bot.run_forever(max_loops=6)                         # 6 vueltas de 15 s
        self.assertEqual(bot.sm.meta_get("started_at"), "2026-10-25T13:30:00+00:00")
        self.assertEqual(bot.sm.meta_get("heartbeat"), "2026-10-25T13:31:00+00:00")
        bot.close()

    def test_overview_endpoint_needs_key(self):
        server = ws.build_server(self.reg, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}/api/overview"
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base, timeout=10)
            self.assertEqual(ctx.exception.code, 401)
            req = urllib.request.Request(base, headers={"X-Access-Key": KEY})
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(len(json.loads(resp.read())["bots"]), 2)
        finally:
            server.shutdown()
            server.server_close()


class TestDailyReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.bots = [{"id": "tortuga", "name": "Tortuga 55", "db": str(self.d / "t.db"), "interval": "1d",
                      "symbols": ["BTCUSDT", "PAXGUSDT"], "balance": 500,
                      "params": str(ROOT / "deploy" / "tortuga_params.json")},
                     {"id": "apagado", "name": "Apagado", "db": str(self.d / "nada.db"), "interval": "4h",
                      "symbols": ["BTCUSDT"], "balance": 500}]
        with StateManager(self.bots[0]["db"], 500, fee_pct=0) as sm:
            pid = sm.open_position("BTCUSDT", "LONG", 100.0, 95.0, 120.0, 110.0, 100.2, 1.0, 5.0, "2026-10-20T00:00:00+00:00")
            sm.close_position(pid, 104.0, "TRAILING_STOP", "2026-10-25T00:00:00+00:00")
            sm.open_position("PAXGUSDT", "LONG", 4000.0, 3900.0, 4500.0, 4200.0, 4008.0, 0.01, 1.0,
                             "2026-10-25T00:00:00+00:00")
            sm.meta_set("last_cycle", json.dumps({"processed_at": "2026-10-25T00:00:10+00:00",
                                                  "symbols": {"BTCUSDT": {"decision": "WAIT / NO_TRADE"}}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_and_markdown(self):
        rep = dr.build_report(self.bots, pd.Timestamp("2026-10-25T00:20:00Z"))
        t, off = rep["bots"]
        self.assertEqual((t["balance"], t["return_pct"], len(t["trades_24h"]), len(t["open_positions"])),
                         (504.0, 0.8, 1, 1))
        self.assertEqual(t["last_decisions"]["BTCUSDT"]["decision"], "WAIT / NO_TRADE")
        self.assertFalse(off["running"])
        self.assertFalse(Path(self.bots[1]["db"]).exists())       # el informe no crea bases de bots apagados
        md = dr.markdown(rep)
        self.assertIn("Tortuga 55", md)
        self.assertIn("sin encender", md)
        self.assertIn("PAXGUSDT", md)

    @unittest.skipUnless(shutil.which("git"), "git no instalado")
    def test_push_to_branch(self):
        remote = self.d / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        repo = self.d / "repo"
        dr.init_repo(repo, str(remote))
        rep = dr.build_report(self.bots, pd.Timestamp("2026-10-25T00:20:00Z"))
        self.assertEqual(dr.push_report(rep, repo), "publicado")
        self.assertEqual(dr.push_report(rep, repo), "sin cambios")
        files = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", dr.REPORT_BRANCH],
                               check=True, capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(files), ["README.md", "reports/2026-10-25.json", "reports/latest.md"])
        dr.init_repo(self.d / "repo2", str(remote))                   # un segundo clon sigue la rama existente
        self.assertTrue((self.d / "repo2" / "reports" / "latest.md").exists())


if __name__ == "__main__":
    unittest.main()
