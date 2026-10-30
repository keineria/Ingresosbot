"""Laboratorio del Cazador (hunter_lab.py) con velas sintéticas: nada sale a internet."""
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hunter_lab as hl  # noqa: E402

SYMS = ["BTCUSDT", "AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]


def synth(n: int, seed: int, burst_p: float, start="2023-06-01") -> pd.DataFrame:
    """Paseo aleatorio; con burst_p > 0 hay arranques con volumen que siguen subiendo unas velas (ventaja plantada)."""
    rng = np.random.default_rng(seed)
    r = rng.normal(0.00003, 0.004, n)
    vol = rng.lognormal(10, 0.3, n)
    if burst_p:
        for i in np.flatnonzero(rng.random(n) < burst_p):
            r[i:i + 8] += 0.006
            vol[i] *= 6
    c = 100 * np.exp(np.cumsum(r))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.001, n)))
    low = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.001, n)))
    t = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"time": t, "open": o, "high": h, "low": low, "close": c, "volume": vol})


def market(burst_p: float, days: int = 400, interval: str = "15m") -> hl.Market:
    data = {s: synth(96 * days, k + 1, burst_p) for k, s in enumerate(SYMS)}
    if interval != "15m":
        data = {s: hl.resample(d, interval) for s, d in data.items()}
    return hl.Market(data, interval)


V = hl.Variant(interval="15m", don_n=48, vol_min=2.0, btc_filter=False)


class TestHunterLab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.edge = market(0.002)
        cls.noise = market(0.0)

    def test_planted_edge_is_found_and_noise_is_not(self):
        e = hl.simulate(self.edge, V, 500, t_from="2024-01-01")
        n = hl.simulate(self.noise, V, 500, t_from="2024-01-01")
        self.assertGreater(e["trades"], 40)
        self.assertGreater(e["return_pct"], 0)
        self.assertLess(n["return_pct"], e["return_pct"])
        self.assertGreater(e["fees_usd"], 0)

    def test_caps_open_positions_and_trades_per_day(self):
        v = hl.Variant(interval="15m", don_n=24, vol_min=1.0, max_ext_atr=5, btc_filter=False, max_open=2,
                       max_trades_day=3, kill_dd_pct=100)
        tr = hl.simulate(self.edge, v, 500, t_from="2024-01-01", keep_trades=True)["trades_df"]
        self.assertGreater(len(tr), 20)
        per_day = tr.opened.dt.floor("D").value_counts()
        self.assertLessEqual(per_day.max(), 3)
        # en ningún momento hay más de 2 posiciones abiertas a la vez
        ev = pd.concat([pd.Series(1, index=tr.opened), pd.Series(-1, index=tr.closed + pd.Timedelta("1ns"))])
        self.assertLessEqual(ev.sort_index().cumsum().max(), 2)
        self.assertEqual(tr.groupby("symbol").apply(lambda d: (d.opened.values[1:] < d.closed.values[:-1]).sum(),
                                                    include_groups=False).sum(), 0)

    def test_signals_do_not_look_ahead(self):
        sig = self.noise.signals(V)
        cut = len(self.noise.time) // 2
        data = {s: synth(96 * 400, k + 1, 0.0) for k, s in enumerate(SYMS)}
        for d in data.values():
            d.loc[cut:, ["open", "high", "low", "close"]] *= 3      # se cambia el futuro
            d.loc[cut:, "volume"] *= 10
        sig2 = hl.Market(data, "15m").signals(V)
        np.testing.assert_array_equal(sig[:cut], sig2[:cut])

    def test_kill_switch_stops_new_entries(self):
        v = hl.Variant(interval="15m", don_n=24, vol_min=1.0, max_ext_atr=5, btc_filter=False, kill_dd_pct=2.0)
        r = hl.simulate(self.noise, v, 500, t_from="2024-01-01", keep_trades=True)
        self.assertTrue(r["killed_at"])
        killed = pd.Timestamp(r["killed_at"])
        self.assertTrue((r["trades_df"].opened <= killed).all())

    def test_btc_filter_blocks_when_btc_below_sma200(self):
        down = synth(96 * 400, 99, 0.0)
        c = 100 * np.geomspace(3, 1, len(down))                    # BTC cae sin pausa: siempre bajo su SMA 200
        down["close"], down["open"] = c, np.r_[c[0], c[:-1]]
        down["high"], down["low"] = c * 1.001, c * 0.999
        m = hl.Market({**{s: synth(96 * 400, k + 1, 0.002) for k, s in enumerate(SYMS[1:])}, "BTCUSDT": down},
                      "15m")
        self.assertEqual(hl.simulate(m, hl.Variant(interval="15m", don_n=48), 500, t_from="2024-01-01")["trades"], 0)

    def test_run_writes_report_and_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            for k, s in enumerate(SYMS):
                synth(96 * 400, k + 1, 0.002).to_csv(Path(tmp) / f"{s}_15m.csv", index=False)
            with unittest.mock.patch.object(hl, "grid", lambda iv: [hl.Variant(interval=iv, don_n=48, vol_min=2.0),
                                                                    hl.Variant(interval=iv, don_n=96, vol_min=3.0)]):
                md = hl.run(tmp, SYMS, ["1h"], str(Path(tmp) / "out"), "2024-01-01", 500, 1)
            self.assertIn("Laboratorio del Cazador", md)
            self.assertIn("Veredicto", md)
            self.assertTrue((Path(tmp) / "out" / "informe_cazador.md").exists())
            self.assertTrue((Path(tmp) / "out" / "entrenamiento_1h.csv").exists())

    def test_download_is_incremental(self):
        step = 900_000
        t0 = int(pd.Timestamp("2026-10-01", tz="UTC").timestamp() * 1000)

        def rows(start, k):
            return [[start + i * step, "1", "2", "0.5", "1.5", "10", start + (i + 1) * step - 1] for i in range(k)]

        calls = []

        def fake_get(url, timeout=10.0):
            calls.append(url)
            start = int(url.split("startTime=")[1].split("&")[0])
            return rows(start, 3) if len(calls) == 1 else []

        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch("market_fetcher._http_get_json", fake_get), \
                unittest.mock.patch.object(hl.time, "sleep", lambda s: None):
            hl.download(tmp, ["AAAUSDT"], "15m", since="2026-10-01")
            df = pd.read_csv(Path(tmp) / "AAAUSDT_15m.csv")
            self.assertEqual(len(df), 3)
            self.assertIn(f"startTime={t0}", calls[0])
            calls.clear()
            hl.download(tmp, ["AAAUSDT"], "15m", since="2026-10-01")
            self.assertIn(f"startTime={t0 + 3 * step}", calls[0])


if __name__ == "__main__":
    unittest.main()
