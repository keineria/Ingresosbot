"""Filtro externo opcional de la Capa 1 (external_risk) y laboratorio de sentimiento (sentiment_lab.py)."""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backtester as bt  # noqa: E402
import quant_core_engine as q  # noqa: E402
import sentiment_lab as sl  # noqa: E402

P = q.Params()
HALT = {"level": "halt", "reasons": ["Fear & Greed 90 >= 75"]}
THROTTLE = {"level": "throttle", "reasons": ["Fear & Greed 80 >= 75"]}


def cb(**kw):
    return q.circuit_breaker(q.AccountState.from_dict({"balance": 500.0, **kw}), P)


class TestExternalRiskLayer1(unittest.TestCase):
    def test_default_is_unchanged(self):
        self.assertEqual(cb()["status"], "ACTIVE")
        self.assertEqual(cb(external_risk={"level": "none", "reasons": []})["status"], "ACTIVE")

    def test_halt_blocks_without_cooldown(self):
        r = cb(external_risk=HALT)
        self.assertEqual((r["status"], r["cooldown_candles_remaining"], r["risk_pct"]), ("HALTED", 0, 0.0))
        self.assertTrue(r["abort_analysis"])
        self.assertIn("filtro externo: Fear & Greed 90 >= 75", r["reasons"])

    def test_throttle_lowers_risk_only_when_active(self):
        r = cb(external_risk=THROTTLE)
        self.assertEqual((r["status"], r["risk_pct"]), ("THROTTLED", P.risk_throttled_pct))
        self.assertEqual(cb(consecutive_losses=3, external_risk=THROTTLE)["status"], "HALTED")

    def test_never_softens_the_breaker(self):
        r = cb(consecutive_losses=3, external_risk=HALT)          # la racha manda: arranca el enfriamiento
        self.assertEqual((r["status"], r["cooldown_candles_remaining"]), ("HALTED", P.cooldown_candles))
        self.assertEqual(cb(manual_halt=True, external_risk={"level": "none"})["status"], "HALTED")
        self.assertEqual(cb(cooldown_candles_remaining=4, external_risk=THROTTLE)["status"], "HALTED")

    def test_engine_aborts_before_layer2(self):
        vals = {"close": 105.0, "ema20": 102.0, "ema50": 100.0, "ema200": 90.0, "rsi14": 55.0, "atr14": 2.0,
                "ema20_slope_pct": 0.5}
        r = q.run_engine({"balance": 500.0, "external_risk": HALT}, manual=vals)
        self.assertEqual(r["decision"], "HALTED")
        self.assertFalse(r["layer_2_market_regime"]["evaluated"])


class TestPortfolioGate(unittest.TestCase):
    def setUp(self):
        self.data = {"AAAUSDT": q.demo_bull_trend(), "BBBUSDT": q.demo_bull_trend(seed=7)}
        self.p = q.Params(strategy="trend")

    def test_halt_everything_means_no_trades(self):
        base = bt.run_portfolio_backtest(self.data, self.p, bt.BacktestConfig(500.0))["report"]
        self.assertGreater(base["trades"], 0)
        self.assertEqual(base["blocked_by_gate"], 0)
        r = bt.run_portfolio_backtest(self.data, self.p, bt.BacktestConfig(500.0), risk_gate=lambda t: HALT)["report"]
        self.assertEqual(r["trades"], 0)
        self.assertGreater(r["blocked_by_gate"], 0)

    def test_none_gate_is_identical(self):
        a = bt.run_portfolio_backtest(self.data, self.p, bt.BacktestConfig(500.0))["report"]
        b = bt.run_portfolio_backtest(self.data, self.p, bt.BacktestConfig(500.0),
                                      risk_gate=lambda t: sl.NO_GATE)["report"]
        self.assertEqual((a["final_balance"], a["trades"]), (b["final_balance"], b["trades"]))


class TestSentimentLab(unittest.TestCase):
    def setUp(self):
        self.fng = sl._daily(pd.Series([50.0, 80.0, 20.0], index=pd.to_datetime(["2024-03-01", "2024-03-02",
                                                                                  "2024-03-04"])))

    def test_missing_days_are_filled_forward(self):
        self.assertEqual(self.fng[pd.Timestamp("2024-03-03")], 80.0)

    def test_gates_use_the_day_value_or_yesterday(self):
        t = pd.Timestamp("2024-03-02 08:00", tz="UTC")
        self.assertEqual(sl.Gate("greed_halt", self.fng, 75)(t)["level"], "halt")
        self.assertEqual(sl.Gate("greed_halt", self.fng, 75, lag=1)(t)["level"], "none")
        self.assertEqual(sl.Gate("greed_throttle", self.fng, 75)(t)["level"], "throttle")
        self.assertEqual(sl.Gate("fear_halt", self.fng, 25)(pd.Timestamp("2024-03-04"))["level"], "halt")
        self.assertEqual(sl.Gate("shock", self.fng, 15)(pd.Timestamp("2024-03-02"))["level"], "halt")
        self.assertEqual(sl.Gate("greed_halt", self.fng, 75)(pd.Timestamp("2030-01-01"))["level"], "none")

    def test_event_days_cover_the_next_first_candle(self):
        g = sl.Gate("events", days=["2024-03-20"])
        self.assertEqual(g(pd.Timestamp("2024-03-20 20:00", tz="UTC"))["level"], "halt")
        self.assertEqual(g(pd.Timestamp("2024-03-21 00:00", tz="UTC"))["level"], "halt")
        self.assertEqual(g(pd.Timestamp("2024-03-21 04:00", tz="UTC"))["level"], "none")

    def test_csv_formats(self):
        with tempfile.TemporaryDirectory() as d:
            iso, alt = Path(d) / "iso.csv", Path(d) / "alt.csv"
            iso.write_text("v,date\n40,2024-01-01\n60,2024-01-02\n")
            alt.write_text("fng_value,fng_classification,date\n60,Greed,02-01-2024\n40,Fear,01-01-2024\n")
            for path in (iso, alt):
                s = sl.load_fear_greed_csv(path)
                self.assertEqual(list(s.values), [40.0, 60.0])
                self.assertEqual(s.index[0], pd.Timestamp("2024-01-01"))

    def test_summary_compares_against_no_filter(self):
        rows = pd.DataFrame([
            {"filtro": "sin filtro", "grupo": "A", "retorno_pct": 10.0, "caida_max_pct": 5.0, "trades": 3,
             "senales_bloqueadas": 0},
            {"filtro": "sin filtro", "grupo": "B", "retorno_pct": 20.0, "caida_max_pct": 6.0, "trades": 3,
             "senales_bloqueadas": 0},
            {"filtro": "x", "grupo": "A", "retorno_pct": 12.0, "caida_max_pct": 4.0, "trades": 2,
             "senales_bloqueadas": 1},
            {"filtro": "x", "grupo": "B", "retorno_pct": 10.0, "caida_max_pct": 4.0, "trades": 2,
             "senales_bloqueadas": 2}])
        s = sl.summarize(rows).set_index("filtro")
        self.assertEqual((s.loc["x", "grupos_mejor"], s.loc["x", "grupos_peor"]), (1, 1))
        self.assertEqual(s.loc["x", "vs_sin_filtro_pct"], -4.0)
        self.assertIn("| x | +11.0% | -4.0 pts | 1 / 1 |", sl.markdown(s.reset_index(), 2, "2024-01-01"))


if __name__ == "__main__":
    unittest.main()
