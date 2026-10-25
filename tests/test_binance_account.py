"""Pruebas de binance_account.py: Binance simulado (sin red). Lo importante: que solo lea y que firme bien."""
import hashlib
import hmac
import json
import logging
import re
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import binance_account as ba  # noqa: E402

logging.disable(logging.CRITICAL)

KEY, SECRET = "llave-publica-de-prueba-WXYZ", "secreto-de-prueba"
SAFE = {"ipRestrict": True, "enableReading": True, "enableWithdrawals": False, "enableSpotAndMarginTrading": False,
        "enableInternalTransfer": False, "permitsUniversalTransfer": False, "enableMargin": False,
        "enableFutures": False, "enableVanillaOptions": False, "enablePortfolioMarginTrading": False,
        "enableFixApiTrade": False, "enableFixReadOnly": False}
PRICES = [{"symbol": "BTCUSDT", "price": "70000"}, {"symbol": "ETHUSDT", "price": "3000"},
          {"symbol": "LDOUSDT", "price": "2"}, {"symbol": "XYZBTC", "price": "0.0001"},
          {"symbol": "USDCUSDT", "price": "0.9998"}, {"symbol": "USDTCOP", "price": "4000"}]


class FakeBinance:
    """Responde como Binance y guarda cada llamada (método, ruta, parámetros, cabeceras)."""

    def __init__(self, perms=None, server_ms=1_700_000_000_500, fail=None):
        self.perms = perms or SAFE
        self.server_ms = server_ms
        self.fail = fail or {}
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        parts = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append((method, parts.path, params, headers))
        if parts.path in self.fail:
            return self.fail[parts.path]
        if "signature" in params:
            query = parts.query.rsplit("&signature=", 1)[0]
            good = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
            if params["signature"] != good or headers.get("X-MBX-APIKEY") != KEY:
                return 401, {"code": -1022, "msg": "Signature for this request is not valid."}
        body = {
            "/api/v3/time": {"serverTime": self.server_ms},
            "/api/v3/ticker/price": PRICES,
            "/sapi/v1/account/apiRestrictions": self.perms,
            "/api/v3/account": {"balances": [
                {"asset": "BTC", "free": "0.001", "locked": "0.0005"},
                {"asset": "USDT", "free": "120.5", "locked": "0"},
                {"asset": "LDUSDT", "free": "50", "locked": "0"},        # Earn flexible visto desde Spot
                {"asset": "LDO", "free": "10", "locked": "0"},           # moneda real, no Earn
                {"asset": "XYZ", "free": "100", "locked": "0"},          # solo par contra BTC
                {"asset": "RARO", "free": "3", "locked": "0"},           # sin precio
                {"asset": "SHIB", "free": "0", "locked": "0"}]},
            "/sapi/v1/simple-earn/flexible/position": {"rows": [{"asset": "USDT", "totalAmount": "50"}], "total": 1},
            "/sapi/v1/simple-earn/locked/position": {"rows": [{"asset": "ETH", "amount": "0.1"}], "total": 1},
            "/sapi/v1/asset/get-funding-asset": [{"asset": "USDT", "free": "30", "locked": "0", "freeze": "5",
                                                  "withdrawing": "0"}],
        }[parts.path]
        return 200, body


def client(fake, now=1_700_000_000.0):
    return ba.BinanceReadOnly(KEY, SECRET, http=fake, clock=lambda: now)


class ReadOnlyGuardTest(unittest.TestCase):
    def test_only_read_calls_are_known(self):
        for method, path in ba.READ_ONLY_CALLS:
            self.assertNotRegex(path.lower(), r"order|withdraw|transfer|trade|cancel|margin|futures|sapi/v1/capital")
        self.assertEqual({m for m, p in ba.READ_ONLY_CALLS if m != "GET"}, {"POST"})
        self.assertEqual([p for m, p in ba.READ_ONLY_CALLS if m == "POST"], ["/sapi/v1/asset/get-funding-asset"])

    def test_refuses_anything_else_before_touching_the_network(self):
        fake = FakeBinance()
        c = client(fake)
        for method, path in (("POST", "/api/v3/order"), ("DELETE", "/api/v3/order"), ("POST", "/api/v3/order/test"),
                             ("POST", "/sapi/v1/capital/withdraw/apply"), ("GET", "/api/v3/order"),
                             ("POST", "/api/v3/account")):
            with self.assertRaises(PermissionError):
                c._call(method, path, {"symbol": "BTCUSDT"})
        self.assertEqual(fake.calls, [])

    def test_source_has_no_order_endpoints(self):
        src = (Path(ba.__file__)).read_text(encoding="utf-8")
        self.assertNotRegex(src, r"/api/v3/order|/sapi/v1/capital/withdraw|/sapi/v1/asset/transfer|/fapi/")


class SignatureTest(unittest.TestCase):
    def test_signs_with_server_time_and_sends_key_header(self):
        fake = FakeBinance(server_ms=1_700_000_002_000)        # Binance va 2 s por delante del VPS
        c = client(fake)
        c.permissions()
        self.assertEqual(fake.calls[0][1], "/api/v3/time")
        method, path, params, headers = fake.calls[1]
        self.assertEqual(headers["X-MBX-APIKEY"], KEY)
        self.assertEqual(int(params["timestamp"]), 1_700_000_002_000)
        self.assertEqual(params["recvWindow"], str(ba.RECV_WINDOW_MS))

    def test_public_prices_are_not_signed(self):
        fake = FakeBinance()
        client(fake).prices()
        method, path, params, headers = fake.calls[0]
        self.assertNotIn("signature", params)
        self.assertNotIn("X-MBX-APIKEY", headers)

    def test_wrong_secret_gives_a_clear_message(self):
        fake = FakeBinance()
        c = ba.BinanceReadOnly(KEY, "otro-secreto", http=fake, clock=lambda: 1_700_000_000.0)
        with self.assertRaisesRegex(ba.BinanceError, "Secret Key"):
            c.permissions()

    def test_clock_drift_resyncs_once(self):
        fake = FakeBinance()
        fake.fail["/sapi/v1/account/apiRestrictions"] = (400, {"code": -1021, "msg": "Timestamp outside recvWindow"})
        c = client(fake)
        with self.assertRaisesRegex(ba.BinanceError, "reloj"):
            c.permissions()
        self.assertEqual([p for _, p, _, _ in fake.calls].count("/api/v3/time"), 2)


class SnapshotTest(unittest.TestCase):
    def test_values_every_wallet_in_usd(self):
        snap = ba.build_snapshot(client(FakeBinance()))
        self.assertTrue(snap["ok"])
        rows = {r["asset"]: r for r in snap["assets"]}
        self.assertNotIn("LDUSDT", rows)                                  # Earn viene del detalle, sin duplicar
        self.assertNotIn("SHIB", rows)
        self.assertAlmostEqual(rows["BTC"]["value_usd"], 0.0015 * 70000)
        self.assertAlmostEqual(rows["LDO"]["value_usd"], 20.0)
        self.assertAlmostEqual(rows["XYZ"]["price_usd"], 7.0)             # vía XYZBTC × BTCUSDT
        self.assertIsNone(rows["RARO"]["value_usd"])
        usdt = rows["USDT"]
        self.assertAlmostEqual(usdt["amount"], 120.5 + 50 + 35)
        self.assertEqual(usdt["wallets"]["funding"], {"free": 30.0, "locked": 5.0})
        self.assertAlmostEqual(snap["wallets"]["spot"], 105 + 120.5 + 20 + 700)
        self.assertAlmostEqual(snap["wallets"]["earn"], 50 + 300)
        self.assertAlmostEqual(snap["wallets"]["funding"], 35)
        self.assertAlmostEqual(snap["total_usd"], sum(snap["wallets"].values()), places=2)
        self.assertEqual(snap["assets"][0]["asset"], "XYZ")              # ordenado por valor
        self.assertEqual(snap["assets"][-1]["asset"], "RARO")            # los sin precio, al final
        self.assertTrue(any("RARO" in n for n in snap["notes"]))
        self.assertEqual(snap["key_hint"], "···WXYZ")
        self.assertNotIn(SECRET, json.dumps(snap))
        self.assertNotIn(KEY, json.dumps(snap))

    def test_earn_falls_back_to_ld_tokens(self):
        fake = FakeBinance(fail={"/sapi/v1/simple-earn/flexible/position": (400, {"code": -1002, "msg": "no"})})
        snap = ba.build_snapshot(client(fake))
        rows = {r["asset"]: r for r in snap["assets"]}
        self.assertEqual(rows["USDT"]["wallets"]["earn"], {"free": 50.0, "locked": 0.0})
        self.assertTrue(any(n.startswith("Earn:") for n in snap["notes"]))

    def test_refuses_a_key_that_can_trade_or_withdraw(self):
        fake = FakeBinance(perms=SAFE | {"enableSpotAndMarginTrading": True, "enableWithdrawals": True})
        snap = ba.build_snapshot(client(fake))
        self.assertFalse(snap["ok"])
        self.assertTrue(snap["blocked"])
        self.assertIn("retirar fondos", snap["message"])
        self.assertNotIn("/api/v3/account", [p for _, p, _, _ in fake.calls])   # ni siquiera lee el saldo

    def test_warns_when_ip_is_not_restricted(self):
        snap = ba.build_snapshot(client(FakeBinance(perms=SAFE | {"ipRestrict": False})))
        self.assertTrue(snap["ok"])
        self.assertTrue(any("IP" in n for n in snap["notes"]))


class AccountViewTest(unittest.TestCase):
    def test_not_configured(self):
        snap = ba.AccountView(None).snapshot()
        self.assertFalse(snap["configured"])

    def test_caches_and_reports_errors(self):
        t = [1_700_000_000.0]
        fake = FakeBinance(fail={"/sapi/v1/account/apiRestrictions": (401, {"code": -2015, "msg": "Invalid API-key"})})
        view = ba.AccountView(ba.BinanceReadOnly(KEY, SECRET, http=fake, clock=lambda: t[0]), clock=lambda: t[0])
        snap = view.snapshot()
        self.assertFalse(snap["ok"])
        self.assertIn("IP del VPS", snap["message"])
        n = len(fake.calls)
        view.snapshot()
        self.assertEqual(len(fake.calls), n)                    # dentro de 30 s no vuelve a preguntar
        t[0] += ba.CACHE_TTL_S + 1
        view.snapshot()
        self.assertGreater(len(fake.calls), n)

    def test_from_env(self):
        import os
        old = {k: os.environ.pop(k, None) for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET")}
        try:
            self.assertIsNone(ba.BinanceReadOnly.from_env())
            os.environ.update(BINANCE_API_KEY=KEY, BINANCE_API_SECRET=SECRET)
            self.assertEqual(ba.BinanceReadOnly.from_env().key_hint, "···WXYZ")
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


class WebRouteTest(unittest.TestCase):
    """GET /api/binance en el panel: exige la clave en la cabecera (no en la URL)."""

    def test_route(self):
        import tempfile

        import quant_core_engine as q
        import web_server as ws
        from bot_runner import RunnerConfig
        from notifier import Notifier

        with tempfile.TemporaryDirectory() as tmp:
            cfg = RunnerConfig(["BTCUSDT"], "1h", str(Path(tmp) / "s.db"), 500.0)
            app = ws.DashboardApp(cfg, q.Params(), "clave-de-prueba-0123456789", Notifier(None),
                                  price_source=lambda s: 70000.0)
            view = ba.AccountView(client(FakeBinance()))
            server = ws.build_server(app, "127.0.0.1", 0, view)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}/api/binance"
            try:
                def get(url, key=None):
                    req = urllib.request.Request(url, headers={"X-Access-Key": key} if key else {})
                    try:
                        with urllib.request.urlopen(req, timeout=10) as r:
                            return r.status, json.loads(r.read())
                    except urllib.error.HTTPError as exc:
                        return exc.code, None
                self.assertEqual(get(base)[0], 401)
                self.assertEqual(get(base + "?key=clave-de-prueba-0123456789")[0], 401)
                status, body = get(base, "clave-de-prueba-0123456789")
                self.assertEqual(status, 200)
                self.assertTrue(body["ok"])
                self.assertGreater(body["total_usd"], 0)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
