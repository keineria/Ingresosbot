"""alt_lab.py: el bloque de cartera con datos sintéticos (sin red)."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import alt_lab  # noqa: E402


def closes(n=500):
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    up = 100 * np.exp(np.linspace(0, 1.0, n))                    # sube siempre
    down = 100 * np.exp(np.linspace(0, -1.0, n))                 # baja siempre
    return pd.DataFrame({"UPUSDT": up, "DOWNUSDT": down}, index=idx)


class AltLabTest(unittest.TestCase):
    def test_simulate_costs_and_cash(self):
        c = closes()
        hold_up = pd.DataFrame({"UPUSDT": 1.0, "DOWNUSDT": 0.0}, index=c.index)
        s = alt_lab.simulate(c, hold_up, c.index[0])
        self.assertAlmostEqual(s.iloc[-1], np.e * (1 - alt_lab.COST), places=6)    # una sola compra
        cash = alt_lab.simulate(c, hold_up * 0, c.index[0])
        self.assertTrue((cash == 1.0).all())

    def test_portfolio_rows_sma_avoids_the_falling_asset(self):
        c = closes()
        rows = alt_lab.portfolio_rows(c, c.index[250], c.index[400])
        by = {r["variante"]: r for r in rows}
        hold = by["Comprar y mantener (cesta, rebalanceo mensual)"]
        sma = by["Media móvil 200 días por activo"]
        self.assertGreater(sma["ret"], hold["ret"])
        self.assertLess(sma["dd"], 1.0)                                 # nunca tuvo la que baja
        self.assertTrue(all(np.isfinite([r["ret"], r["train"], r["valid"]]).all() for r in rows))
        md = alt_lab.markdown(rows, ["UPUSDT", "DOWNUSDT"], c.index[250], c.index[400], c.index[-1])
        self.assertIn("Positivas en las dos partes", md)


if __name__ == "__main__":
    unittest.main()
