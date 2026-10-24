"""Pruebas de notifier.py con envío simulado (sin red)."""
import os
import logging
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notifier as nt  # noqa: E402

POS = {"id": 1, "symbol": "BTCUSDT", "side": "LONG", "entry_price": 100.0, "stop_loss": 96.8, "take_profit": 107.2,
       "breakeven_trigger": 103.0, "breakeven_stop_price": 100.2, "position_units": 10.0, "risk_usd": 32.0,
       "risk_mode": "ACTIVE", "regime": "BULL_EXPANSION"}
ACC = {"balance": 10000.0, "consecutive_losses": 0, "daily_drawdown_pct": 0.0, "cooldown_bars_remaining": 0,
       "status": "ACTIVE"}


logging.disable(logging.CRITICAL)   # los avisos esperados no ensucian la salida


class Recorder:
    def __init__(self, status=200, fail=0):
        self.calls, self.status, self.fail = [], status, fail

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        if self.fail:
            self.fail -= 1
            raise urllib.error.URLError("caído")
        return self.status


class TestNotifier(unittest.TestCase):
    def test_webhook_and_telegram(self):
        rec = Recorder()
        n = nt.Notifier("https://n8n.local/webhook/bot", "123:ABC", "-100777", post=rec)
        self.assertTrue(n.position_opened(POS, ACC))
        (u1, p1), (u2, p2) = rec.calls
        self.assertEqual(u1, "https://n8n.local/webhook/bot")
        self.assertEqual(p1["event"], "position_opened")
        self.assertEqual(p1["data"]["position"]["stop_loss"], 96.8)
        self.assertIn("NUEVA POSICIÓN LONG BTCUSDT", p1["text"])
        self.assertEqual(u2, "https://api.telegram.org/bot123:ABC/sendMessage")
        self.assertEqual(p2["chat_id"], "-100777")
        self.assertIn("SL: 96.80", p2["text"])

    def test_four_events(self):
        rec = Recorder()
        n = nt.Notifier("https://hook", post=rec)
        n.position_opened(POS, ACC)
        n.break_even(dict(POS, stop_loss=100.2, is_at_breakeven=1), 103.1)
        trade = {"symbol": "BTCUSDT", "side": "LONG", "result": "BREAKEVEN", "exit_reason": "BREAK_EVEN",
                 "entry_price": 100.0, "exit_price": 100.2, "pnl_usd": 0.4, "pnl_pct": 0.04, "r_multiple": 0.01}
        n.position_closed(trade, dict(ACC, consecutive_losses=2))
        n.status_changed({"from": "THROTTLED", "to": "HALTED", "reasons": ["consecutive_losses=3 >= 3"]},
                         dict(ACC, status="HALTED", cooldown_bars_remaining=15))
        self.assertEqual([p["event"] for _, p in rec.calls],
                         ["position_opened", "break_even", "position_closed", "status_changed"])
        self.assertIn("Nuevo SL: 100.20", rec.calls[1][1]["text"])
        self.assertIn("Racha de pérdidas: 2", rec.calls[2][1]["text"])
        self.assertIn("Enfriamiento: 15 velas", rec.calls[3][1]["text"])

    def test_status_changed_only_alerts_throttled_or_halted(self):
        rec = Recorder()
        n = nt.Notifier("https://hook", post=rec)
        self.assertFalse(n.status_changed({"from": "HALTED", "to": "ACTIVE", "reasons": []}, ACC))
        self.assertTrue(n.status_changed({"from": "ACTIVE", "to": "THROTTLED", "reasons": ["x"]}, ACC))
        self.assertEqual(len(rec.calls), 1)

    def test_failures_never_raise(self):
        rec = Recorder(fail=1)
        self.assertTrue(nt.Notifier("https://hook", post=rec).position_opened(POS, ACC))   # reintento OK
        self.assertEqual(len(rec.calls), 2)
        rec = Recorder(fail=5)
        self.assertFalse(nt.Notifier("https://hook", post=rec).position_opened(POS, ACC))
        rec = Recorder(status=500)
        self.assertFalse(nt.Notifier("https://hook", post=rec).position_opened(POS, ACC))

    def test_disabled_without_config(self):
        rec = Recorder()
        n = nt.Notifier(None, "token-sin-chat", None, post=rec)
        self.assertFalse(n.enabled)
        self.assertFalse(n.position_opened(POS, ACC))
        self.assertEqual(rec.calls, [])

    def test_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text('# comentario\nWEBHOOK_URL="https://hook/x"\nTELEGRAM_CHAT_ID=42\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "ya-definido"}, clear=True):
                nt.load_env_file(env)
                n = nt.Notifier.from_env()
                self.assertEqual((n.webhook_url, n.telegram_chat_id), ("https://hook/x", "ya-definido"))


if __name__ == "__main__":
    unittest.main()
