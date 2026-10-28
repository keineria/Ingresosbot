import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import bot_review as br
import quant_core_engine as q
from state_manager import StateManager


def fake_candles(symbol, interval, limit, *a, **k):
    step = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D"}[interval]
    end = pd.Timestamp.now(tz="UTC").floor(step) - pd.Timedelta(step)
    t = pd.date_range(end=end, periods=limit, freq=step)
    rng = np.random.default_rng(len(symbol) + limit)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, limit)))
    o = np.r_[c[0], c[:-1]]
    df = pd.DataFrame({"time": t, "open": o, "high": np.maximum(o, c) * 1.004,
                       "low": np.minimum(o, c) * 0.996, "close": c, "volume": 1.0})
    return df, {"source": "fake"}


class BotReviewTest(unittest.TestCase):
    def test_report_runs_and_only_reads(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "s.db"
            p = q.Params(allow_short=False)
            with StateManager(db, 500, p):
                pass
            before = db.stat().st_mtime
            bot = {"id": "oficial", "name": "Bot Oficial", "trader": "Nathalia", "db": str(db),
                   "interval": "4h", "symbols": ["BTCUSDT", "ETHUSDT"], "balance": 500}
            since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)
            with mock.patch.object(br, "fetch_candles", fake_candles), \
                    mock.patch.object(br, "fetch_price", lambda s: (100.0, "fake")):
                lines, ctx = br.account_part(bot, since, pd.Timestamp.now(tz="UTC"))
                cache = {}
                lines += br.filter_part(ctx["cfg"], ctx["p"], since, cache)
                lines += br.backtest_part(ctx["cfg"], ctx["p"], 500, since, cache)
            text = "\n".join(lines)
            self.assertIn("Nathalia", text)
            self.assertIn("Por qué no entró", text)
            self.assertIn("Comprar y mantener", text)
            self.assertIn("Reglas Tortuga 55 (1d)", text)
            self.assertNotIn("error:", text)
            self.assertNotIn("sin datos", text)
            self.assertEqual(before, db.stat().st_mtime)


if __name__ == "__main__":
    unittest.main()
