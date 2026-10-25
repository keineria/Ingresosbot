"""Pruebas de arbitrage_scanner.py sin red: un `get` falso devuelve respuestas con el formato real de cada API."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import arbitrage_scanner as arb  # noqa: E402


def fake_get(prices):
    """prices: {exchange: (bid, ask)} para BTCUSDT; el resto de exchanges responde HTTP 451."""
    def get(url, data=None):
        host = urllib.parse.urlsplit(url).netloc
        if "binance.vision" in host and "bookTicker" in url and "symbol=" not in url and "symbols=" not in url:
            return [{"symbol": s, "bidPrice": "1", "askPrice": "1", "bidQty": "1", "askQty": "1"}
                    for s in ("BTCUSDT", "ETHUSDT", "ETHBTC")]
        if "binance.vision" in host and "symbols=" in url:
            return [{"symbol": "BTCUSDT", "bidPrice": "100000", "askPrice": "100001"},
                    {"symbol": "ETHUSDT", "bidPrice": "4000", "askPrice": "4000.1"},
                    {"symbol": "ETHBTC", "bidPrice": "0.04", "askPrice": "0.04001"}]
        if "binance.vision" in host and "binance" in prices:
            b, a = prices["binance"]
            return {"bidPrice": str(b), "askPrice": str(a), "bidQty": "2", "askQty": "2"}
        if host == "api.bybit.com" and "category=spot" in url and "bybit" in prices:
            b, a = prices["bybit"]
            return {"retCode": 0, "result": {"list": [{"bid1Price": str(b), "ask1Price": str(a),
                                                       "bid1Size": "1", "ask1Size": "1"}]}}
        if host == "api.bybit.com" and "category=linear" in url:
            return {"result": {"list": [{"fundingRate": "0.0001", "fundingIntervalHour": "8"}]}}
        if host == "api.hyperliquid.xyz":
            assert data == {"type": "metaAndAssetCtxs"}
            return [{"universe": [{"name": "ETH"}, {"name": "BTC"}]}, [{"funding": "0.00002"}, {"funding": "0.0000125"}]]
        raise urllib.error.HTTPError(url, 451, "Unavailable For Legal Reasons", {}, None)
    return get


class ParserTests(unittest.TestCase):
    def test_spot_parsers_read_real_shapes(self):
        cases = {
            "okx": {"data": [{"bidPx": "10", "askPx": "11", "bidSz": "1", "askSz": "2"}]},
            "kucoin": {"data": {"bestBid": "10", "bestAsk": "11", "bestBidSize": "1", "bestAskSize": "2"}},
            "gate": [{"highest_bid": "10", "lowest_ask": "11", "highest_size": "1", "lowest_size": "2"}],
            "bitget": {"data": [{"bidPr": "10", "askPr": "11", "bidSz": "1", "askSz": "2"}]},
            "mexc": {"bidPrice": "10", "askPrice": "11", "bidQty": "1", "askQty": "2"},
            "kraken": {"error": [], "result": {"XBTUSDT": {"b": ["10", "1", "1.000"], "a": ["11", "2", "2.000"]}}},
            "coinbase": {"bids": [["10", "1", 3]], "asks": [["11", "2", 1]]},
        }
        for ex, payload in cases.items():
            with self.subTest(ex=ex):
                q = arb.SPOT_SOURCES[ex]("BTCUSDT", lambda url, data=None, p=payload: p)
                self.assertEqual((q.bid, q.ask, q.bid_qty, q.ask_qty), (10.0, 11.0, 1.0, 2.0))

    def test_kraken_uses_xbt_and_coinbase_dash(self):
        seen = []

        def get(url, data=None):
            seen.append(url)
            if "kraken" in url:
                return {"error": [], "result": {"X": {"b": ["1", "1", "1"], "a": ["1", "1", "1"]}}}
            return {"bids": [["1", "1", 1]], "asks": [["1", "1", 1]]}
        arb.q_kraken("BTCUSDT", get)
        arb.q_coinbase("SOLUSDT", get)
        self.assertIn("pair=XBTUSDT", seen[0])
        self.assertIn("/products/SOL-USDT/", seen[1])

    def test_crossed_or_empty_book_is_rejected(self):
        with self.assertRaises(ValueError):
            arb._quote("0", "10")
        with self.assertRaises(ValueError):
            arb._quote("12", "10")

    def test_funding_parsers_and_intervals(self):
        self.assertEqual(arb.fr_okx("BTCUSDT", lambda u, d=None: {"data": [
            {"fundingRate": "0.0001", "fundingTime": "1000", "nextFundingTime": str(1000 + 4 * 3_600_000)}]}),
            (0.0001, 4.0))
        self.assertEqual(arb.fr_gate("BTCUSDT", lambda u, d=None: {"funding_rate": "0.0002", "funding_interval": 28800}),
                         (0.0002, 8.0))
        self.assertEqual(arb.fr_kucoin("BTCUSDT", lambda u, d=None: {"data": {"value": 0.0001, "granularity": 28800000}}),
                         (0.0001, 8.0))
        arb._HL_CACHE.clear()
        self.assertEqual(arb.fr_hyperliquid("BTCUSDT", fake_get({})), (0.0000125, 1.0))

    def test_annualize(self):
        self.assertAlmostEqual(arb.annualize(0.0001, 8), 10.95)
        self.assertAlmostEqual(arb.annualize(0.0000125, 1), 10.95)


class MathTests(unittest.TestCase):
    def test_spatial_net_subtracts_both_taker_fees(self):
        quotes = {"a": arb.Quote(100.0, 100.0, 1, 1), "b": arb.Quote(100.5, 100.6, 2, 2)}
        ops = {(o["buy"], o["sell"]): o for o in arb.spatial_opportunities(quotes, {"a": 0.001, "b": 0.001})}
        self.assertAlmostEqual(ops[("a", "b")]["gross_pct"], 0.5)
        self.assertAlmostEqual(ops[("a", "b")]["net_pct"], 0.3)
        self.assertEqual(ops[("a", "b")]["depth_usd"], 100.0)     # limita el lado con menos libro
        self.assertLess(ops[("b", "a")]["net_pct"], 0)

    def test_triangle_discovery_and_return(self):
        paths = arb.triangles({"BTCUSDT", "ETHUSDT", "ETHBTC", "SOLUSDT"}, coins=("BTC", "ETH", "SOL"))
        self.assertEqual(sorted(paths), [("BTC", "ETH", "ETHBTC"), ("ETH", "BTC", "ETHBTC")])
        fair = {"BTCUSDT": arb.Quote(100000, 100000), "ETHUSDT": arb.Quote(4000, 4000), "ETHBTC": arb.Quote(0.04, 0.04)}
        self.assertAlmostEqual(arb.triangle_return("BTC", "ETH", "ETHBTC", fair, 0.0), 0.0, places=9)
        self.assertAlmostEqual(arb.triangle_return("ETH", "BTC", "ETHBTC", fair, 0.0), 0.0, places=9)
        # Con precios justos solo quedan las 3 comisiones
        self.assertAlmostEqual(arb.triangle_return("BTC", "ETH", "ETHBTC", fair, 0.001), ((0.999 ** 3) - 1) * 100)
        # ETH barato en BTC: USDT->BTC->ETH->USDT gana
        cheap = dict(fair, ETHBTC=arb.Quote(0.0398, 0.0398))
        self.assertGreater(arb.triangle_return("BTC", "ETH", "ETHBTC", cheap, 0.0), 0.4)

    def test_carry_estimate_for_500(self):
        est = arb.carry_estimate(10.95, 500, leverage=2.0, spot_fee=0.001, perp_fee=0.0005, days=365)
        self.assertAlmostEqual(est["notional_usd"], 333.33)
        self.assertAlmostEqual(est["income_usd"], 36.5, places=1)
        self.assertAlmostEqual(est["costs_usd"], 1.0, places=2)


class ScannerTests(unittest.TestCase):
    def test_round_records_working_exchanges_and_skips_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            arb._HL_CACHE.clear()
            sc = arb.Scanner(["BTCUSDT"], ["binance", "bybit", "okx"], ["binance", "bybit", "hyperliquid"],
                             get=fake_get({"binance": (100000, 100001), "bybit": (100400, 100401)}),
                             recorder=arb.Recorder(Path(tmp)))
            for rnd in range(3):
                rows, errs = sc.spot_round(rnd)
            self.assertEqual({r["exchange"] for r in rows}, {"binance", "bybit"})
            self.assertEqual(errs[0]["error"], "BTCUSDT: HTTP 451")
            rows, _ = sc.spot_round(3)                           # okx falló 3 veces: se salta
            self.assertNotIn("okx", {r["exchange"] for r in rows})
            frows, ferrs = sc.funding_round()
            self.assertEqual({r["exchange"] for r in frows}, {"bybit", "hyperliquid"})
            tri = sc.triangle_round(0.001)
            self.assertEqual(len(tri), 2)

            rep = arb.build_report(Path(tmp), arb.SPOT_TAKER, arb.PERP_TAKER, 500, min_depth=50)
            best = rep["spatial"][0]
            self.assertEqual((best["buy"], best["sell"]), ("binance", "bybit"))
            self.assertEqual(best["pct_time_positive"], 100.0)    # +0.4% bruto - 0.2% comisiones, libro >= 50 USD
            self.assertIn("okx", rep["unreachable"])
            self.assertIn("binance (perp)", rep["unreachable"])
            self.assertEqual(rep["funding"][0]["avg_apr_pct"], 10.95)
            md = arb.report_markdown(rep)
            self.assertIn("binance→bybit", md)
            json.dumps(rep)

    def test_cli_once_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            arb._HL_CACHE.clear()
            with mock.patch.object(arb, "_http_json", fake_get({"binance": (100, 101), "bybit": (100, 101)})):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(arb.main(["--once", "--out-dir", tmp, "--symbols", "BTCUSDT"]), 0)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(arb.main(["--report", "--out-dir", tmp]), 0)
            self.assertIn("Informe de arbitraje", out.getvalue())
            self.assertTrue((Path(tmp) / "informe.md").exists())


if __name__ == "__main__":
    unittest.main()
