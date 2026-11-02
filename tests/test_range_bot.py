"""Bot Rupturas de rango (range_bot.py) con un Binance falso: nada sale a internet."""
import json
import math
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import range_bot as rb  # noqa: E402
import range_lab as rl  # noqa: E402
from notifier import Notifier  # noqa: E402

M15 = rb.M15
H4 = 16 * M15
T0 = 1_788_220_800_000          # 2026-09-01 00:00 UTC (múltiplo de 4 h)
DAYS_RANGE = 24                  # el lateral dura 24 días; se confirma a partir del día ~20
V = rl.Variant(det_tf="4h", lookback=90, w_min=6.0, w_max=25.0, touches=2, levels=0, stop_atr=1.5,
               brk_long=True, brk_short=True, trail_atr=3.0)


def series(after: list[float]) -> list[float]:
    """Cierres de 15 min: lateral 100-110 con un vaivén de 3 días, y luego `after` (un cierre por vela)."""
    n = DAYS_RANGE * 96
    closes = [105 + 5 * math.sin(2 * math.pi * i / (3 * 96)) for i in range(n)]
    return closes + after


def rows15(closes: list[float], wick: float = 0.001) -> list[list]:
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append([T0 + i * M15, o, max(o, c) * (1 + wick), min(o, c) * (1 - wick), c, 100.0, T0 + (i + 1) * M15 - 1])
        prev = c
    return out


def agg(rows: list[list], step: int) -> list[list]:
    out: dict[int, list] = {}
    for r in rows:
        k = (r[0] // step) * step
        if k not in out:
            out[k] = [k, r[1], r[2], r[3], r[4], r[5], k + step - 1]
        else:
            b = out[k]
            b[2], b[3], b[4], b[5] = max(b[2], r[2]), min(b[3], r[3]), r[4], b[5] + r[5]
    return list(out.values())


class FakeBinance:
    def __init__(self, data: dict[str, list[float]]):
        self.rows = {s: rows15(c) for s, c in data.items()}
        self.now_ms = T0

    def get(self, url: str):
        _, _, query = url.partition("?")
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        live = [r for r in self.rows[qs["symbol"]] if r[0] <= self.now_ms]       # incluye la vela en curso
        rows = live if qs["interval"] == "15m" else agg(live, H4)
        if "startTime" in qs:
            rows = [r for r in rows if r[0] >= int(qs["startTime"])]
            return rows[: int(qs["limit"])]
        return rows[-int(qs["limit"]):]

    def price(self, sym: str) -> float:
        return float([r for r in self.rows[sym] if r[0] <= self.now_ms][-1][1])


class Harness:
    def __init__(self, data, tmp, v=V, balance=500.0):
        self.fake = FakeBinance(data)
        self.sent = []
        notifier = Notifier(telegram_token="t", telegram_chat_id="c", label=rb.LABEL,
                            post=lambda url, p: (self.sent.append(p["text"]), 200)[1])
        self.bot = rb.RangeBot(v, list(data), str(Path(tmp) / "rango.db"), balance, notifier=notifier,
                               get=self.fake.get, price_source=self.fake.price,
                               clock=lambda: self.fake.now_ms / 1000, sleep=lambda s: None, hosts=["https://x"])

    def run(self, day_from: float, day_to: float) -> list[dict]:
        out = []
        k = int(day_from * 96)
        while k < day_to * 96:
            self.fake.now_ms = T0 + (k + 1) * M15 + 11_000          # 11 s después de cerrar la vela k
            out.append(self.bot.candle_cycle())
            k += 1
        return out


def decisions(cycles, sym):
    return [c["symbols"][sym]["decision"] for c in cycles if sym in c.get("symbols", {})]


UP = [110 + 0.15 * k for k in range(150)] + [132.5 - 0.3 * k for k in range(80)]      # rompe, sube 20%, cae
DOWN = [100 - 0.1 * k for k in range(150)] + [85 + 0.3 * k for k in range(80)]       # rompe, cae, rebota


class TestRangeBot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_arms_range_then_buys_breakout_and_trails_out_with_profit(self):
        h = Harness({"AAAUSDT": series(UP)}, self.tmp.name)
        cycles = h.run(21, DAYS_RANGE + 3)
        d = decisions(cycles, "AAAUSDT")
        self.assertIn("ARMED", d)
        self.assertIn("LONG", d)
        self.assertLess(d.index("ARMED"), d.index("LONG"))
        trades = h.bot.sm.trades()
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["side"], t["exit_reason"], t["result"]), ("LONG", "TRAILING_STOP", "WIN"))
        self.assertTrue(any("LATERAL CONFIRMADO AAAUSDT" in s for s in h.sent))
        self.assertTrue(any("COMPRA (papel) AAAUSDT" in s for s in h.sent))
        self.assertTrue(all(s.startswith("[Rupturas de rango]") for s in h.sent))
        self.assertEqual(json.loads(h.bot.sm.meta_get(rb.K_RANGES)), {})

    def test_position_size_follows_capa3(self):
        h = Harness({"AAAUSDT": series(UP)}, self.tmp.name)
        h.run(21, DAYS_RANGE)
        r = json.loads(h.bot.sm.meta_get(rb.K_RANGES))["AAAUSDT"]
        worst = rl.breakout_worst(r["lo"], r["hi"], r["atr"], V)
        self.assertAlmostEqual(r["alloc"], min(500 * 0.30, 500 * 0.015 / worst), places=6)
        self.assertLess(r["lo"], 100.2)
        self.assertGreater(r["hi"], 109.8)

    def test_breakdown_opens_paper_short(self):
        h = Harness({"AAAUSDT": series(DOWN)}, self.tmp.name)
        h.run(21, DAYS_RANGE + 3)
        t = h.bot.sm.trades()[0]
        self.assertEqual((t["side"], t["result"]), ("SHORT", "WIN"))
        self.assertTrue(any("VENTA EN CORTO (papel) AAAUSDT" in s for s in h.sent))

    def test_long_only_discards_broken_range(self):
        v = rl.replace(V, brk_short=False)
        h = Harness({"AAAUSDT": series(DOWN)}, self.tmp.name, v=v)
        cycles = h.run(21, DAYS_RANGE + 3)
        self.assertEqual(h.bot.sm.trades(), [])
        reasons = [c["symbols"]["AAAUSDT"]["reason"] for c in cycles if "AAAUSDT" in c["symbols"]
                   and c["symbols"]["AAAUSDT"]["decision"] == "SKIPPED"]
        self.assertTrue(any("descartado" in r for r in reasons))

    def test_no_more_than_three_ranges_and_halt_blocks_arming(self):
        data = {f"{c * 3}USDT": series([105.0] * 50) for c in "ABCD"}
        h = Harness(data, self.tmp.name)
        cycles = h.run(21, 21.5)
        self.assertEqual(len(json.loads(h.bot.sm.meta_get(rb.K_RANGES))), 3)
        waits = [c["symbols"]["DDDUSDT"]["reason"] for c in cycles if "DDDUSDT" in c["symbols"]]
        self.assertTrue(any("ya hay 3 rangos" in r for r in waits))
        h2 = Harness({"AAAUSDT": series([105.0] * 50)}, tempfile.mkdtemp())
        h2.bot.sm.set_manual_halt(True, "prueba")
        h2.run(21, 21.5)
        self.assertEqual(json.loads(h2.bot.sm.meta_get(rb.K_RANGES) or "{}"), {})

    def test_params_file_round_trip(self):
        p = Path(self.tmp.name) / "p.json"
        p.write_text(json.dumps({**{k: getattr(V, k) for k in ("det_tf", "lookback", "w_min", "w_max", "levels",
                                                                 "stop_atr", "brk_long", "brk_short", "trail_atr")},
                                 "symbols": ["BTCUSDT"]}), encoding="utf-8")
        v = rb.load_variant(p)
        self.assertEqual((v.lookback, v.brk_short, v.trail_atr), (90, True, 3.0))
        p.write_text(json.dumps({"lookbak": 3}), encoding="utf-8")
        with self.assertRaises(ValueError):
            rb.load_variant(p)

    def test_shipped_params_match_lab_candidate(self):
        v = rb.load_variant(Path(__file__).resolve().parents[1] / "deploy" / "rango_reglas.json")
        self.assertEqual((v.det_tf, v.lookback, v.w_min, v.w_max, v.levels, v.stop_atr, v.trail_atr),
                         ("4h", 90, 6.0, 25.0, 0, 1.5, 3.0))
        self.assertTrue(v.brk_long)


if __name__ == "__main__":
    unittest.main()
