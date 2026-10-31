"""Laboratorio de nuevos listados (listing_lab.py) con listados sintéticos: nada sale a internet."""
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import listing_lab as ll  # noqa: E402


def listing(start, closes, wick=0.01):
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    t = pd.date_range(start, periods=len(c), freq="1h", tz="UTC")
    return pd.DataFrame({"time": t, "open": o, "high": np.maximum(o, c) * (1 + wick),
                         "low": np.minimum(o, c) * (1 - wick), "close": c, "volume": 1.0})


def pump_dump_rebound(start, seed):
    """Sube 3 veces en 6 h, cae 60% en dos días y rebota 40% (patrón plantado para la familia 'caida')."""
    rng = np.random.default_rng(seed)
    up = np.geomspace(1, 3, 6)
    down = np.geomspace(3, 1.2, 48)
    rebound = np.geomspace(1.2, 1.7, 48)
    flat = 1.7 * np.exp(np.cumsum(rng.normal(0, 0.003, 900)))
    return listing(start, np.r_[up, down, rebound, flat])


class TestListingLab(unittest.TestCase):
    def setUp(self):
        starts = pd.date_range("2023-02-01", periods=60, freq="7D", tz="UTC")
        self.data = {f"N{i}USDT": pump_dump_rebound(t, i) for i, t in enumerate(starts)}

    def test_buying_the_listing_hour_loses_and_dip_rule_wins_on_planted_pattern(self):
        early = ll.Rule("hora", h=4, hold=72, sl=0.3)
        dip = ll.Rule("caida", dip=0.58, tp=0.25, hold=168, sl=0.3)
        e = ll.events(self.data, early)
        d = ll.events(self.data, dip)
        self.assertTrue((e.ret < 0).all())
        self.assertTrue((d.ret > 0.2).all())
        self.assertTrue((d.reason == "TP").all())

    def test_costs_are_charged(self):
        flat = {"AUSDT": listing("2024-01-01", np.ones(200), wick=0.0)}
        ev = ll.events(flat, ll.Rule("hora", h=1, hold=10, sl=0.5))
        self.assertAlmostEqual(ev.ret.iloc[0], (1 - ll.SLIP_EARLY) / (1 + ll.SLIP_EARLY) * (1 - ll.FEE) ** 2 - 1,
                               places=9)
        ev = ll.events(flat, ll.Rule("hora", h=48, hold=10, sl=0.5))
        self.assertAlmostEqual(ev.ret.iloc[0], (1 - ll.SLIP_LATE) / (1 + ll.SLIP_LATE) * (1 - ll.FEE) ** 2 - 1,
                               places=9)

    def test_stop_loss_and_breakout_entry(self):
        crash = {"BUSDT": listing("2024-01-01", np.r_[np.ones(30), np.linspace(1, 0.5, 30), np.ones(100) * 0.5])}
        ev = ll.events(crash, ll.Rule("hora", h=1, hold=200, sl=0.2))
        self.assertEqual(ev.reason.iloc[0], "SL")
        self.assertLess(ev.ret.iloc[0], -0.2)
        brk = {"CUSDT": listing("2024-01-01", np.r_[np.ones(24), np.ones(10) * 0.98, np.geomspace(1.0, 2, 50)])}
        ev = ll.events(brk, ll.Rule("ruptura", window=168, trail=0.15, hold=336, sl=0.15))
        self.assertGreater(ev.entry_bar.iloc[0], 24)

    def test_account_caps_concurrency_and_kills(self):
        t = pd.Timestamp("2024-01-01", tz="UTC")
        ev = pd.DataFrame({"symbol": [f"S{i}" for i in range(5)], "t_in": [t] * 5,
                           "t_out": [t + pd.Timedelta(hours=5)] * 5, "ret": [0.1] * 5})
        self.assertEqual(ll.account(ev)["trades"], 3)
        days = pd.date_range("2024-01-01", periods=40, freq="2D", tz="UTC")
        ev = pd.DataFrame({"symbol": [f"L{i}" for i in range(40)], "t_in": days,
                           "t_out": days + pd.Timedelta(hours=1), "ret": [-0.9] * 40})
        r = ll.account(ev)
        self.assertTrue(r["killed_at"])
        self.assertLess(r["trades"], 40)

    def test_btc_filter(self):
        idx = pd.date_range("2022-01-01", "2026-01-01", freq="1D", tz="UTC")
        off = pd.Series(False, index=idx)
        self.assertTrue(ll.events(self.data, ll.Rule("hora", h=1, btc_filter=True), off).empty)
        on = pd.Series(True, index=idx)
        self.assertEqual(len(ll.events(self.data, ll.Rule("hora", h=1, btc_filter=True), on)), 60)

    def test_evaluate_and_report(self):
        rules = [ll.Rule("hora", h=4, hold=72, sl=0.3), ll.Rule("caida", dip=0.58, tp=0.25, hold=168, sl=0.3)]
        res = ll.evaluate(self.data, None, rules=rules, top=2)
        best = res["finals"][0]
        self.assertEqual(best["rule"].kind, "caida")
        self.assertTrue(best["passed"])
        md = ll.report_md(res, 500)
        self.assertIn("Veredicto", md)
        self.assertIn("SÍ", md)

    def test_symbol_filter_and_download(self):
        info = {"symbols": [
            {"symbol": "NEWUSDT", "baseAsset": "NEW", "quoteAsset": "USDT", "status": "TRADING"},
            {"symbol": "OLDUSDT", "baseAsset": "OLD", "quoteAsset": "USDT", "status": "TRADING"},
            {"symbol": "GONEUSDT", "baseAsset": "GONE", "quoteAsset": "USDT", "status": "BREAK"},
            {"symbol": "FDUSDUSDT", "baseAsset": "FDUSD", "quoteAsset": "USDT", "status": "TRADING"},
            {"symbol": "BTCUPUSDT", "baseAsset": "BTCUP", "quoteAsset": "USDT", "status": "BREAK"},
            {"symbol": "NEWBTC", "baseAsset": "NEW", "quoteAsset": "BTC", "status": "TRADING"}]}
        self.assertEqual([s for s, _ in ll.usdt_symbols(info)], ["NEWUSDT", "OLDUSDT", "GONEUSDT"])
        h = 3_600_000
        first = {"NEWUSDT": pd.Timestamp("2024-05-01", tz="UTC"), "OLDUSDT": pd.Timestamp("2019-01-01", tz="UTC"),
                 "GONEUSDT": pd.Timestamp("2023-03-01", tz="UTC")}
        calls = []

        def fake(url, timeout=10.0):
            calls.append(url)
            if "exchangeInfo" in url:
                return info
            sym = url.split("symbol=")[1].split("&")[0]
            if sym == "BTCUSDT":
                t0 = int(pd.Timestamp("2022-01-01", tz="UTC").timestamp() * 1000)
                return [[t0 + i * 24 * h, "1", "1", "1", "1", "1", t0 + (i + 1) * 24 * h - 1] for i in range(300)]
            t0 = int(first[sym].timestamp() * 1000)
            return [[t0 + i * h, "1", "2", "0.5", "1.5", "10", t0 + (i + 1) * h - 1] for i in range(1000)]

        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch("market_fetcher._http_get_json", fake), \
                unittest.mock.patch.object(ll.time, "sleep", lambda s: None):
            ll.download(tmp, "2023-01-01")
            files = sorted(p.name for p in Path(tmp).glob("*.csv"))
            self.assertEqual(files, ["BTCUSDT_1d.csv", "GONEUSDT_1h.csv", "NEWUSDT_1h.csv", "indice.csv"])
            n_first = len(calls)
            ll.download(tmp, "2023-01-01")          # 2.ª vez: no vuelve a pedir monedas ya completas
            self.assertEqual(len(calls) - n_first, 2)   # exchangeInfo + BTC diario

    def test_tokenized_stocks_are_excluded(self):
        self.assertTrue(ll.is_tokenized_stock("NVDABUSDT", pd.Timestamp("2026-07-01", tz="UTC")))
        self.assertFalse(ll.is_tokenized_stock("NVDABUSDT", pd.Timestamp("2025-07-01", tz="UTC")))
        self.assertFalse(ll.is_tokenized_stock("ROBOUSDT", pd.Timestamp("2026-07-01", tz="UTC")))
        with tempfile.TemporaryDirectory() as tmp:
            pump_dump_rebound("2026-07-22", 1).to_csv(Path(tmp) / "TSLABUSDT_1h.csv", index=False)
            pump_dump_rebound("2026-07-22", 2).to_csv(Path(tmp) / "ROBOUSDT_1h.csv", index=False)
            excluded = []
            data, _ = ll.load(tmp, excluded=excluded)
            self.assertEqual(list(data), ["ROBOUSDT"])
            self.assertEqual(excluded, ["TSLABUSDT"])
            self.assertEqual(len(ll.load(tmp, keep_stocks=True)[0]), 2)
            self.assertIn("Se excluyeron 1", ll.report_md(ll.evaluate(data | {f"N{i}USDT": d for i, d in
                                                                              enumerate(self.data.values())}, None,
                                                                     rules=[ll.Rule("hora", h=4)], top=1),
                                                          500, excluded))


if __name__ == "__main__":
    unittest.main()
