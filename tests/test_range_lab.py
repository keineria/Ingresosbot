"""Laboratorio del bot lateral (range_lab.py) con velas sintéticas: nada sale a internet."""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import range_lab as rl  # noqa: E402


def synth(n: int, seed: int, kind: str) -> pd.DataFrame:
    """'range': precio que vuelve a su nivel (rangos reales, el nivel cambia cada ~10 días); 'walk': azar puro."""
    rng = np.random.default_rng(seed)
    if kind == "range":
        x, lvl = np.zeros(n), 0.0
        for i in range(1, n):
            if i % 960 == 0:
                lvl += rng.normal(0, 0.05)
            x[i] = x[i - 1] + 0.03 * (lvl - x[i - 1]) + rng.normal(0, 0.003)
        c = 100 * np.exp(x)
    else:
        c = 100 * np.exp(np.cumsum(rng.normal(0.00002, 0.004, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.001, n)))
    low = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.001, n)))
    t = pd.date_range("2023-06-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"time": t, "open": o, "high": h, "low": low, "close": c, "volume": 1.0})


def market(kind: str, days: int = 450) -> rl.Market:
    return rl.Market({f"C{k}USDT": synth(96 * days, k + 1, kind) for k in range(5)})


def frame(rows):
    """Velas de 15 min a mano: lista de (open, high, low, close)."""
    a = np.array(rows, float)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3]


class TestEpisode(unittest.TestCase):
    V = rl.Variant(levels=1, edge=0.0, stop_atr=1.0)

    def test_buy_floor_sell_ceiling_then_stop_sells_everything(self):
        # rango 100-110, ATR 2 → stop 98. Compra al atravesar 100, vende al atravesar 110, recompra y salta el stop.
        o, h, l, c = frame([(105, 106, 104, 105), (105, 105, 99.5, 101), (101, 110.5, 101, 110),
                            (110, 110, 99, 100), (100, 100, 97, 97.5)])
        e = rl.run_episode(o, h, l, c, 1, 5, 100.0, 110.0, 2.0, self.V)
        self.assertEqual(e["roundtrips"], 1)
        self.assertEqual(e["reason"], "RUPTURA_ABAJO")
        win = 110 / 100 * (1 - rl.FEE_LIMIT) - (1 + rl.FEE_LIMIT)          # ida y vuelta 100 → 110
        loss = 98 * (1 - rl.SLIP) / 100 * (1 - rl.FEE_MARKET) - (1 + rl.FEE_LIMIT)
        self.assertAlmostEqual(e["ret"], win + loss, places=9)
        self.assertLess(e["ret"], win)               # la ruptura se come parte de la ganancia
        self.assertGreater(e["ret"], -0.05)

    def test_no_buy_and_sell_in_the_same_candle(self):
        o, h, l, c = frame([(105, 106, 104, 105), (105, 111, 99, 105), (105, 106, 104, 105)])
        e = rl.run_episode(o, h, l, c, 1, 3, 100.0, 110.0, 2.0, self.V)
        self.assertEqual(e["roundtrips"], 0)
        self.assertEqual(e["reason"], "TIEMPO")       # salió vendiendo a mercado al final de su vida

    def test_only_buys_below_the_price(self):
        # El precio arranca bajo el escalón: no compra hasta que lo pasa por arriba y vuelve a bajar.
        o, h, l, c = frame([(99.5, 99.8, 99.2, 99.5), (99.5, 99.9, 99.1, 99.6), (99.6, 103, 99.6, 102),
                            (102, 102, 99.5, 101)])
        v = rl.Variant(levels=1, edge=0.0, stop_atr=0.1)
        e = rl.run_episode(o, h, l, c, 1, 4, 100.0, 110.0, 10.0, v)
        self.assertGreater(e["fees"], 0)
        e2 = rl.run_episode(o, h, l, c, 1, 3, 100.0, 110.0, 10.0, v)
        self.assertEqual(e2["fees"], 0)

    def test_worst_case_loss_respects_risk(self):
        v = rl.Variant(levels=4)
        lv = rl.ladder(100, 108, v)
        w = rl.worst_loss_frac(lv, 100 - 1.0, v)
        alloc = min(500 * 0.30, 500 * 0.015 / w)
        self.assertLessEqual(alloc * w, 500 * 0.015 + 1e-9)


class TestRangeLab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rng = market("range")
        cls.walk = market("walk")

    def run_acc(self, m, v):
        lo = int(m.time.searchsorted(pd.Timestamp("2024-01-01", tz="UTC")))
        return rl.account(m, rl.all_episodes(m, v), v, 500, lo, keep=True)

    def test_planted_range_edge_is_found_and_random_walk_is_not(self):
        v = rl.Variant(det_tf="4h", lookback=30, w_min=3, w_max=12, levels=3)
        r, w = self.run_acc(self.rng, v), self.run_acc(self.walk, v)
        self.assertGreater(r["episodes"], 40)
        self.assertGreater(r["return_pct"], 10)
        self.assertLess(w["return_pct"], r["return_pct"])
        self.assertGreater(r["fees_usd"], 0)

    def test_caps_and_kill_switch(self):
        v = rl.Variant(levels=6, max_open=2, kill_dd_pct=5)
        lo = int(self.walk.time.searchsorted(pd.Timestamp("2024-01-01", tz="UTC")))
        eps = rl.all_episodes(self.walk, v)
        r = rl.account(self.walk, eps, v, 500, lo, keep=True)
        df = r["trades_df"]
        self.assertTrue(r["killed_at"])
        self.assertLess(r["max_dd_pct"], 15)
        self.assertTrue((df.allocated <= 500 * 0.30 * 1.25).all())
        events = sorted([(t, 1) for t in df.opened] + [(t, -1) for t in df.closed], key=lambda x: (x[0], x[1]))
        open_now = peak = 0
        for _, d in events:
            open_now += d
            peak = max(peak, open_now)
        self.assertLessEqual(peak, 2)

    def test_short_set_detects_on_15m(self):
        vs = rl.grid_short()
        self.assertTrue(all(v.det_tf == "15m" for v in vs))
        v = rl.Variant(det_tf="15m", lookback=96, w_min=1.0, w_max=4.0, levels=2)
        r = self.run_acc(self.rng, v)
        self.assertGreater(r["episodes"], 40)

    def test_season_gate_uses_only_finished_ranges(self):
        v = rl.Variant(det_tf="30m", lookback=48, w_min=2, w_max=6, levels=1, stop_atr=1.5, season_n=5)
        eps = rl.all_episodes(self.walk, v)
        gate = rl._season_gate(eps, v, None)
        traded = [e for e in eps if e["fees"] > 0]
        t = traded[10]["i0"]
        done = sorted((e for e in traded if e["end"] < t), key=lambda e: e["end"])[-5:]
        self.assertEqual(gate(t, 0), sum(e["ret"] for e in done) > 0)
        self.assertFalse(gate(traded[0]["i0"], 0))            # sin historia: en rojo
        r = self.run_acc(self.walk, v)
        self.assertLess(r["gate_open_pct"], 100)

    def test_lateral_filter_reduces_ranges(self):
        base = rl.Variant(det_tf="30m", lookback=48, w_min=2, w_max=6, levels=1)
        er = rl.replace(base, max_er=0.2)
        self.assertLessEqual(len(rl.all_episodes(self.walk, er)), len(rl.all_episodes(self.walk, base)))

    def test_run_writes_report(self):
        with tempfile.TemporaryDirectory() as d:
            syms = ["BTCUSDT"] + [f"C{k}USDT" for k in range(5)]
            for k, s in enumerate(syms):
                synth(96 * 420, k + 1, "range" if k % 2 else "walk").to_csv(f"{d}/{s}_15m.csv", index=False)
            md = rl.run(d, syms, f"{d}/out", "2024-01-01", 500, 1, variants=rl.grid()[:3], top=2)
            self.assertIn("Veredicto", md)
            self.assertTrue(Path(f"{d}/out/variantes_rango.csv").exists())
            self.assertTrue(Path(f"{d}/out/rangos_mejor.csv").exists())


if __name__ == "__main__":
    unittest.main()
