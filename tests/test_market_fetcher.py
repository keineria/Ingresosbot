"""Pruebas de market_fetcher.py sin red: se inyecta un `get` falso con respuestas reales de formato."""
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import market_fetcher as mf  # noqa: E402
import quant_core_engine as q  # noqa: E402

H = 3_600_000
START = 1_767_225_600_000           # 2026-01-01T00:00:00Z


def binance_rows(n, start=START):
    rows = []
    for k in range(n):
        t = start + k * H
        px = 60000 + k
        rows.append([t, f"{px:.2f}", f"{px + 50:.2f}", f"{px - 50:.2f}", f"{px + 10:.2f}", "12.5",
                     t + H - 1, "750000.0", 100, "6.0", "360000.0", "0"])
    return rows


class FakeBinance:
    def __init__(self, total):
        self.rows = binance_rows(total)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        qs = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        rows = [r for r in self.rows if "endTime" not in qs or r[0] <= int(qs["endTime"])]
        return rows[-int(qs["limit"]):]


class FakeBybit:
    """Imita /v5/market/kline: de la más nueva a la más vieja, máx. 1000, filtra por `end`."""
    def __init__(self, total, step=4 * H):
        self.step = step
        self.rows = [[str(START + k * step), "1", "2", "0.5", "1.5", "10", "15"] for k in range(total)]
        self.calls, self.limits = [], []

    def __call__(self, url):
        self.calls.append(url)
        qs = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        limit = int(qs["limit"])
        self.limits.append(limit)
        rows = [r for r in self.rows if "end" not in qs or int(r[0]) <= int(qs["end"])]
        return {"retCode": 0, "retMsg": "OK", "result": {"list": rows[-limit:][::-1]}}


def offline(url):
    raise urllib.error.URLError("sin red")


class TestFetcher(unittest.TestCase):
    def test_binance_drops_open_candle_and_normalizes(self):
        fake = FakeBinance(300)
        now = START + 299 * H + 10          # la vela 299 sigue abierta
        df, info = mf.fetch_candles("BTCUSDT", "1h", 250, get=fake, now_ms=now)
        self.assertEqual(info["source"], "binance_vision")
        self.assertEqual(len(df), 250)
        self.assertEqual(list(df.columns), ["time", "open", "high", "low", "close", "volume"])
        self.assertEqual(df.time.iloc[-1].isoformat(), "2026-01-13T10:00:00+00:00")   # vela 298
        self.assertTrue(df.time.is_monotonic_increasing)
        self.assertIn("data-api.binance.vision", fake.calls[0])

    def test_binance_pagination(self):
        fake = FakeBinance(2500)
        df, _ = mf.fetch_candles("ETHUSDT", "1h", 2200, get=fake, now_ms=START + 10**12)
        self.assertEqual(len(df), 2200)
        self.assertEqual(df.time.diff().dropna().nunique(), 1)          # sin huecos ni duplicados
        self.assertGreaterEqual(len(fake.calls), 3)

    def test_binance_pagination_8760_in_batches_of_1000(self):
        fake = FakeBinance(10000)
        df, info = mf.fetch_candles("BTCUSDT", "1h", 8760, get=fake, now_ms=START + 10**13)
        limits = [int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query))["limit"]) for u in fake.calls]
        self.assertEqual(info["source"], "binance_vision")
        self.assertEqual(len(df), 8760)
        self.assertTrue(df.time.is_monotonic_increasing)
        self.assertEqual(int(df.time.duplicated().sum()), 0)
        self.assertEqual(df.time.diff().dropna().unique().tolist(), [pd.Timedelta(hours=1)])
        self.assertEqual(df.time.iloc[-1], pd.Timestamp(fake.rows[-1][0], unit="ms", tz="UTC"))
        self.assertLessEqual(max(limits), 1000)
        self.assertEqual(len(fake.calls), 9)                      # 8761 pedidas / 1000 por lote

    def test_bybit_pagination_8760_in_batches_of_1000(self):
        fake = FakeBybit(10000)
        df, info = mf.fetch_candles("SOLUSDT", "4h", 8760, get=fake, sources=("bybit",), now_ms=START + 10**14)
        self.assertEqual(info["source"], "bybit")
        self.assertEqual(len(df), 8760)
        self.assertTrue(df.time.is_monotonic_increasing)
        self.assertEqual(int(df.time.duplicated().sum()), 0)
        self.assertEqual(df.time.diff().dropna().unique().tolist(), [pd.Timedelta(hours=4)])
        self.assertLessEqual(max(fake.limits), 1000)
        self.assertEqual(len(fake.calls), 9)

    def test_fetch_price_fallback(self):
        def get(url):
            if "binance.vision" in url:
                raise urllib.error.URLError("bloqueado")
            if "api.binance.com" in url:
                return {"symbol": "BTCUSDT", "price": "67123.45000000"}
            raise AssertionError("no debería llegar a Bybit")
        self.assertEqual(mf.fetch_price("BTCUSDT", get=get), (67123.45, "binance"))
        bybit = lambda url: {"retCode": 0, "result": {"list": [{"lastPrice": "150.5"}]}}  # noqa: E731
        self.assertEqual(mf.fetch_price("SOLUSDT", sources=("bybit",), get=bybit), (150.5, "bybit"))
        with self.assertRaises(mf.FetchError):
            mf.fetch_price("BTCUSDT", get=offline)

    def test_data_api_first_never_touches_blocked_host(self):
        fake = FakeBinance(300)

        def get(url):
            if "api.binance.com" in url:
                raise urllib.error.HTTPError(url, 451, "restricted location", None, None)
            return fake(url)
        df, info = mf.fetch_candles("SOLUSDT", "1h", 100, get=get, now_ms=START + 10**12)
        self.assertEqual(info["source"], "binance_vision")
        self.assertEqual(info["errors"], {})
        self.assertTrue(all("data-api.binance.vision" in u for u in fake.calls))

    def test_falls_back_to_binance_then_bybit(self):
        def binance_only(url):
            if "binance.vision" in url:
                raise urllib.error.HTTPError(url, 451, "restricted location", None, None)
            return FakeBinance(300)(url)
        df, info = mf.fetch_candles("SOLUSDT", "1h", 100, get=binance_only, now_ms=START + 10**12)
        self.assertEqual(info["source"], "binance")
        self.assertIn("binance_vision", info["errors"])

        def bybit_only(url):
            if "bybit" not in url:
                raise urllib.error.URLError("bloqueado")
            rows = [[str(START + k * H), "1", "2", "0.5", "1.5", "10", "15"] for k in range(150)][::-1]
            return {"retCode": 0, "retMsg": "OK", "result": {"list": rows[:100]}}
        df, info = mf.fetch_candles("SOLUSDT", "1h", 100, get=bybit_only, now_ms=START + 10**12)
        self.assertEqual(info["source"], "bybit")
        self.assertTrue(df.time.is_monotonic_increasing)
        self.assertAlmostEqual(df.close.iloc[-1], 1.5)

    def test_csv_fallback_and_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "BTCUSDT_1h.csv"
            mf.save_csv(q.demo_bull_trend(), path)
            df, info = mf.fetch_candles("BTCUSDT", "1h", 300, fallback_csv=path, get=offline)
            self.assertTrue(info["source"].startswith("csv:"))
            self.assertEqual(len(df), 300)
            self.assertEqual(set(info["errors"]), {"binance", "binance_vision", "bybit"})
            with self.assertRaises(mf.FetchError):
                mf.fetch_candles("BTCUSDT", "1h", 300, fallback_csv=Path(d) / "no_existe.csv", get=offline)

    def test_engine_runs_on_fetched_candles(self):
        df, _ = mf.fetch_candles("BTCUSDT", "1h", 500, get=FakeBinance(600), now_ms=START + 10**12)
        r = q.run_engine({"balance": 10000}, candles=df, symbol="BTCUSDT", timeframe="1h")
        self.assertIn(r["decision"], ("LONG", "SHORT", "WAIT / NO_TRADE"))

    def test_bad_args(self):
        with self.assertRaises(ValueError):
            mf.fetch_candles("BTC/USDT", "1h", 10, get=offline)
        with self.assertRaises(ValueError):
            mf.fetch_candles("BTCUSDT", "3h", 10, get=offline)


if __name__ == "__main__":
    unittest.main()
