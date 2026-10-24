"""Pruebas de state_manager.py (python -m unittest discover -s tests -v)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quant_core_engine as q  # noqa: E402
from state_manager import StateManager, check_exit  # noqa: E402

T0 = "2026-03-01T10:00:00+00:00"


def open_long(sm, entry=100.0, units=10.0):
    # Entrada 100, ATR 2 -> SL 96.8, TP 107.2, BE 103.0 -> 100.2 (SKILL_2)
    return sm.open_position("BTCUSDT", "LONG", entry, 96.8, 107.2, 103.0, 100.2, units, units * 3.2, T0)


class TestSchema(unittest.TestCase):
    def test_tables_and_initial_state(self):
        sm = StateManager(":memory:", 10000)
        tables = {r[0] for r in sm.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"account_state", "open_positions", "trade_history"} <= tables)
        a = sm.account()
        self.assertEqual((a["balance"], a["initial_balance"], a["status"], a["cooldown_bars_remaining"]),
                         (10000, 10000, "ACTIVE", 0))
        self.assertEqual(sm.engine_account()["cooldown_candles_remaining"], 0)

    def test_persists_across_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "data" / "trading_state.db"
            sm = StateManager(path, 10000, fee_pct=0)
            pid = open_long(sm)
            sm.close()
            sm2 = StateManager(path, 99999)            # no pisa el estado existente
            self.assertEqual(sm2.account()["balance"], 10000)
            self.assertEqual(sm2.positions()[0]["id"], pid)
            sm2.close()


class TestBreakEven(unittest.TestCase):
    def test_trigger_moves_stop(self):
        sm = StateManager(":memory:", 10000, fee_pct=0)
        open_long(sm)
        ev = sm.update_price("BTCUSDT", 102.9)
        self.assertEqual(ev, [])
        ev = sm.update_price("BTCUSDT", 103.0)            # toca exactamente el trigger
        self.assertEqual(ev[0]["event"], "BREAK_EVEN")
        pos = sm.positions()[0]
        self.assertEqual((pos["stop_loss"], pos["is_at_breakeven"]), (100.2, 1))

    def test_close_at_break_even_is_neutral(self):
        sm = StateManager(":memory:", 10000, fee_pct=0.08)
        sm.conn.execute("UPDATE account_state SET consecutive_losses = 2")
        open_long(sm)
        sm.update_price("BTCUSDT", 103.5)
        ev = sm.update_price("BTCUSDT", 100.1)
        t = ev[0]
        # En vivo sale al precio actual (100.1): 10 uds, +1.00 bruto - (0.80 + 0.8008) comisiones
        self.assertEqual((t["result"], t["exit_reason"], t["exit_price"]), ("BREAKEVEN", "BREAK_EVEN", 100.1))
        self.assertAlmostEqual(t["pnl_usd"], -0.60, places=2)
        self.assertEqual(sm.account()["consecutive_losses"], 2)   # ni suma ni reinicia
        self.assertEqual(sm.positions(), [])

    def test_candle_moves_be_and_exit_next_candle(self):
        sm = StateManager(":memory:", 10000, fee_pct=0)
        open_long(sm)
        ev = sm.process_candle("BTCUSDT", T0, 101, 103.5, 100.0, 103.2)   # toca trigger y luego baja
        self.assertEqual([e["event"] for e in ev], ["BREAK_EVEN"])          # no sale en la misma vela
        ev = sm.process_candle("BTCUSDT", T0, 101, 101.5, 100.1, 100.5)
        self.assertEqual(ev[0]["result"], "BREAKEVEN")


class TestAccounting(unittest.TestCase):
    def test_win_loss_streak_dd_and_halt(self):
        sm = StateManager(":memory:", 10000, fee_pct=0)
        sm.on_new_bar(T0)
        for expected_status in ("ACTIVE", "THROTTLED", "HALTED"):
            open_long(sm, units=10)
            t = sm.update_price("BTCUSDT", 96.0)[0]                  # gap bajo el SL
            self.assertEqual(t["result"], "LOSS")
            self.assertEqual(sm.account()["status"], expected_status)
        a = sm.account()
        self.assertEqual((a["consecutive_losses"], a["cooldown_bars_remaining"]), (3, 15))
        self.assertAlmostEqual(a["balance"], 10000 - 3 * 40, places=6)
        self.assertAlmostEqual(a["daily_drawdown_pct"], 1.2, places=6)
        with self.assertRaises(RuntimeError):
            open_long(sm)
        for _ in range(15):
            sm.on_new_bar(T0)
        a = sm.account()
        self.assertEqual((a["status"], a["consecutive_losses"], a["cooldown_bars_remaining"]), ("ACTIVE", 0, 0))
        self.assertEqual([x["to"] for x in sm.status_transitions], ["THROTTLED", "HALTED", "ACTIVE"])

    def test_win_resets_streak_and_history(self):
        sm = StateManager(":memory:", 10000, fee_pct=0)
        sm.conn.execute("UPDATE account_state SET consecutive_losses = 2")
        open_long(sm, units=10)
        t = sm.update_price("BTCUSDT", 108.0)[0]
        self.assertEqual((t["result"], t["exit_reason"]), ("WIN", "TAKE_PROFIT"))
        self.assertAlmostEqual(t["pnl_usd"], 80.0)          # sale al precio actual (108) en vivo
        self.assertAlmostEqual(t["pnl_pct"], 8.0)
        self.assertEqual(sm.account()["consecutive_losses"], 0)
        self.assertEqual(len(sm.trades()), 1)

    def test_daily_dd_halt_and_reset(self):
        sm = StateManager(":memory:", 10000, fee_pct=0)
        sm.on_new_bar("2026-03-01T10:00:00+00:00")
        open_long(sm, units=100)                             # pierde 320 = 3.2% del día
        sm.update_price("BTCUSDT", 96.8)
        a = sm.account()
        self.assertEqual((a["status"], a["consecutive_losses"]), ("HALTED", 1))
        sm.on_new_bar("2026-03-02T00:00:00+00:00")           # nuevo día: DD a 0, enfriamiento sigue
        a = sm.account()
        self.assertEqual((a["daily_drawdown_pct"], a["status"], a["cooldown_bars_remaining"]), (0.0, "HALTED", 14))

    def test_open_from_engine(self):
        sm = StateManager(":memory:", 10000)
        r = q.run_engine(sm.engine_account(), candles=q.demo_bull_trend(), symbol="BTCUSDT")
        pid = sm.open_from_engine(r, opened_at=T0)
        pos = sm.positions()[0]
        rk = r["layer_3_atr_risk_guardian"]
        self.assertEqual(pos["id"], pid)
        self.assertEqual((pos["stop_loss"], pos["take_profit"], pos["breakeven_stop_price"], pos["position_units"]),
                         (rk["stop_loss"], rk["take_profit"], rk["break_even_new_sl"], rk["quantity"]))
        self.assertIsNone(sm.open_from_engine({"order": None}, T0))

    def test_short_exit_rules(self):
        self.assertEqual(check_exit("SHORT", 103.2, 92.8, False, 100, 104, 99), (103.2, "STOP_LOSS"))
        self.assertEqual(check_exit("SHORT", 103.2, 92.8, False, 100, 101, 92), (92.8, "TAKE_PROFIT"))
        self.assertEqual(check_exit("LONG", 96.8, 107.2, False, 100, 108, 96), (96.8, "STOP_LOSS"))  # ambos: SL
        self.assertEqual(check_exit("LONG", 96.8, 107.2, False, 95, 99, 94), (95, "STOP_LOSS_GAP"))


if __name__ == "__main__":
    unittest.main()
