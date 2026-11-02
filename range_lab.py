#!/usr/bin/env python3
"""
range_lab.py — Laboratorio del bot lateral "Rebotes en rango" (comprar en el piso, vender en el techo, repetir).

Idea de Keiner (2026-10-31): una moneda toca varias veces el mismo piso y el mismo techo en un rango corto; comprar
en el rebote del piso, vender cerca del techo y volver a comprar, muchas veces, mientras el rango aguante.

Pregunta: ¿eso gana DESPUÉS de comisiones, en datos que no se usaron para elegir, y sin que una sola ruptura del
rango se coma todas las ganancias pequeñas?

Cómo funciona cada "episodio de rango" (lo que haría el bot en una moneda):
  1. Detección (al cierre de cada vela de 1 h o 4 h, con las N velas anteriores incluida la actual):
       techo = máximo de las N velas · piso = mínimo · ancho = techo / piso − 1, entre w_min y w_max %
       el precio tocó la zona del piso al menos T veces Y la zona del techo al menos T veces (zona = 15% del
       ancho; un "toque" es entrar en la zona después de haber salido de ella)
       el rango mide al menos 2 ATR y cada escalón deja ≥ 0,6% (3 veces la comisión de ida y vuelta)
       filtros opcionales: ADX bajo, moneda sobre su EMA 200, BTC diario sobre su SMA 200.
  2. Escalera de órdenes límite (como un bot "grid"): G escalones entre el piso y el techo (10% hacia dentro de
     cada borde). Cada escalón compra 1/G de lo asignado y vende un escalón más arriba; al venderse vuelve a
     quedar listo para comprar. G = 1 es el rebote simple: compra en el piso, vende en el techo.
     Solo compra por DEBAJO del precio (nunca persigue): los escalones de arriba se activan cuando el precio pasa.
  3. Corte de pérdidas por ruptura: si el precio toca piso − k ATR, vende todo a mercado y el rango se da por roto.
     Si cierra sobre techo + k ATR sin inventario, el rango termina (rompió hacia arriba, ya vendió todo).
     Si el rango dura más de 2 N velas, vende lo que quede y vuelve a buscar.
  4. Cuenta única con la Capa 1 (3 rangos perdedores seguidos → 15 velas sin abrir rangos; 2 seguidos → riesgo a
     la mitad; caída del día ≥ 3% → nada nuevo hasta mañana; -20% desde el máximo → el bot se apaga) y la Capa 3:
     lo asignado a un rango es lo que hace que, si TODOS los escalones se llenan y salta el stop, se pierda como
     mucho 1,5% del saldo; máximo 30% del saldo por rango, 85% en total y 3 rangos a la vez.

Ejecución honesta (simulada en velas de 15 min aunque la detección sea en 1 h o 4 h):
  * Límite llena solo si el precio la ATRAVIESA (mínimo < precio de compra / máximo > precio de venta); si abre
    al otro lado, llena en la apertura. Comisión 0,1% por orden (Binance sin descuento BNB).
  * Lo comprado en una vela no se puede vender en esa misma vela (no sabemos el orden dentro de la vela).
  * En la vela que toca el stop: primero se llenan todas las compras pendientes y luego salta el stop (peor caso);
    stop y salidas por tiempo a mercado: 0,1% + 0,05% de deslizamiento.

Método (sin mirar el futuro al elegir): rejilla pequeña de variantes evaluada SOLO en la primera mitad; las 5
mejores se miden en la segunda mitad, en el periodo completo, por año y en 20 grupos aleatorios de 20 monedas.
Pasa si: completo > 0, validación > 0, ≥ 15 de 20 grupos > 0, caída máx. < 20% y ≥ 40 rangos.
Además se compara con comprar y mantener y con la misma escalera SIN stop (como los "grid bots" que se anuncian).

Advertencia: las monedas son las que siguen listadas hoy (sesgo de supervivencia) y un rango saltado por falta
de cupo se cuenta como si la moneda estuviera ocupada mientras duraba (el bot real podría abrirlo más tarde).

Uso (en el VPS, como usuario ingresosbot, desde /opt/Ingresosbot). Reutiliza las velas de 15 min del Cazador:
  python range_lab.py download --csv-dir data/cazador/velas            # solo baja lo que falta (incremental)
  python range_lab.py run --csv-dir data/cazador/velas --out-dir data/rango/informes --workers 2
  python range_lab.py run --set corto --csv-dir data/cazador/velas --out-dir data/rango/informes_15m --workers 2
"""

from __future__ import annotations

import argparse
import itertools
import json
import multiprocessing as mp
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

import hunter_lab as hl
import quant_core_engine as q

FEE_LIMIT = 0.001       # orden límite (maker) sin descuento BNB
FEE_MARKET = 0.001      # orden a mercado (taker)
SLIP = 0.0005           # deslizamiento en salidas a mercado
MIN_NOTIONAL = 5.0
BASE = "15m"
BAR_MIN = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}
UNIVERSE = hl.UNIVERSE + ("PAXGUSDT",)     # el oro tokenizado suele moverse en rangos: se incluye


@dataclass(frozen=True)
class Variant:
    det_tf: str = "1h"          # temporalidad de la detección del rango
    lookback: int = 48          # N velas de det_tf que forman el rango
    w_min: float = 2.0          # ancho mínimo del rango, %
    w_max: float = 8.0          # ancho máximo, %
    touches: int = 2            # toques mínimos en el piso Y en el techo
    tol: float = 0.15           # zona de toque = 15% del ancho desde cada borde
    levels: int = 1             # escalones (1 = rebote simple piso → techo)
    edge: float = 0.10          # primer escalón a 10% del ancho sobre el piso; último a 10% bajo el techo
    stop_atr: float = 1.0       # stop = piso − k ATR (0 = SIN stop, solo para la comparación)
    life_mult: float = 2.0      # el rango vive como mucho life_mult × N velas
    trend: str = "none"         # none | ema200 (moneda sobre su EMA 200 en det_tf) | btc (BTC diario > SMA 200)
    max_adx: float = 0.0        # 0 = sin filtro de ADX
    # "Temporadas" (idea de Keiner, 1-nov): operar solo cuando el mercado está lateral y la estrategia va bien.
    max_er: float = 0.0         # lateral confirmado: eficiencia de Kaufman de las últimas 4N velas ≤ max_er (0 = off)
    season_n: int = 0           # semáforo: los últimos n rangos "en sombra" (los vea o no el bot) suman > 0 (0 = off)
    season_scope: str = "all"   # all = rangos de todas las monedas · coin = solo los de esa moneda
    # Operar la RUPTURA del lateral (idea de Keiner, 1-nov): al romperse el rango, entrar a favor de la ruptura.
    brk_long: bool = False      # cierra sobre techo + brk_atr ATR → compra en la apertura siguiente
    brk_short: bool = False     # salta el stop bajo el piso → abre corto ahí mismo (solo papel / Futuros 1x)
    brk_atr: float = 0.5
    trail_atr: float = 4.0      # stop inicial en la mitad del rango; luego trailing a m ATR del mejor precio
    min_range_atr: float = 2.0
    min_step_pct: float = 0.6
    # Cuenta (Capa 1 / Capa 3). No se optimizan.
    risk_pct: float = 1.5
    max_alloc_pct: float = 30.0
    max_open: int = 3
    kill_dd_pct: float = 20.0

    def label(self) -> str:
        kind = ("sin rebotes" if self.levels == 0 else "rebote simple" if self.levels == 1
                else f"escalera {self.levels}")
        stop = f"stop piso−{self.stop_atr:g} ATR" if self.stop_atr > 0 else "SIN stop"
        tr = {"none": "", "ema200": " · sobre EMA200", "btc": " · BTC>SMA200"}[self.trend]
        adx = f" · ADX<{self.max_adx:g}" if self.max_adx else ""
        er = f" · lateral ER≤{self.max_er:g}" if self.max_er else ""
        sea = (f" · semáforo {self.season_n} rangos {'todas' if self.season_scope == 'all' else 'la moneda'}"
               if self.season_n else "")
        brk = ""
        if self.brk_long or self.brk_short:
            sides = "+".join(x for x, on in (("largo", self.brk_long), ("corto", self.brk_short)) if on)
            brk = f" · ruptura→{sides} trail {self.trail_atr:g} ATR"
        risk = f" · riesgo {self.risk_pct:g}%" if self.risk_pct != 1.5 else ""
        return (f"{self.det_tf} · rango {self.lookback} velas · ancho {self.w_min:g}-{self.w_max:g}% · "
                f"{self.touches}+{self.touches} toques · {kind} · {stop}{tr}{adx}{er}{sea}{brk}{risk}")

    def det_key(self) -> tuple:
        return (self.det_tf, self.lookback, self.w_min, self.w_max, self.touches, self.tol, self.trend,
                self.max_adx, self.min_range_atr, self.min_step_pct, self.levels, self.edge, self.max_er)


# ════════════════════════════════════════════════════════════════════════════
# DATOS
# ════════════════════════════════════════════════════════════════════════════
def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    rule = {"15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1D"}[tf]
    g = df.set_index("time").resample(rule, label="left", closed="left")
    out = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(),
                        "close": g.close.last(), "volume": g.volume.sum()}).dropna()
    return out.reset_index()


class Market:
    """Todas las monedas en la misma rejilla de 15 min (huecos = vela plana con el último cierre)."""

    def __init__(self, data: dict[str, pd.DataFrame]):
        self.symbols = list(data)
        t0 = min(d.time.iloc[0] for d in data.values())
        t1 = max(d.time.iloc[-1] for d in data.values())
        self.time = pd.date_range(t0, t1, freq="15min")
        self.raw = data
        T, S = len(self.time), len(self.symbols)
        self.o, self.h, self.l, self.c = (np.full((T, S), np.nan) for _ in range(4))
        self.start = np.zeros(S, int)
        for j, s in enumerate(self.symbols):
            d = data[s].drop_duplicates("time").set_index("time").reindex(self.time)
            first = d.close.first_valid_index()
            i0 = int(self.time.searchsorted(first))
            c = d.close.ffill()
            self.c[:, j] = c
            for arr, col in ((self.o, "open"), (self.h, "high"), (self.l, "low")):
                arr[:, j] = d[col].fillna(c)
            self.o[:i0, j] = self.h[:i0, j] = self.l[:i0, j] = self.c[:i0, j] = np.nan
            self.start[j] = i0
        self.day = pd.factorize(self.time.floor("D"))[0]
        self.year = self.time.year.to_numpy()
        btc = data.get("BTCUSDT")
        if btc is None:
            self.btc_ok = np.ones(T, bool)
        else:
            daily = resample(btc, "1d").set_index("time").close
            ok = (daily > daily.rolling(200).mean()).astype(float)
            ok.index = ok.index + pd.Timedelta(days=1)          # el cierre de ayer se conoce hoy a las 00:00
            self.btc_ok = ok.reindex(self.time.floor("D")).fillna(0).to_numpy() > 0
        self._det: dict = {}

    # ── detección ──────────────────────────────────────────────────────────
    def activations(self, v: Variant) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
        """Por moneda: (índice en la rejilla de 15 min desde el que vale, piso, techo, ATR)."""
        key = v.det_key()
        if key in self._det:
            return self._det[key]
        out = []
        for s in self.symbols:
            out.append(detect(self.raw[s], v, self.time, self.btc_ok))
        self._det[key] = out
        return out


def _touch_events(win: np.ndarray, thr: np.ndarray, below: bool) -> np.ndarray:
    zone = win <= thr[:, None] if below else win >= thr[:, None]
    return zone[:, 0].astype(int) + (zone[:, 1:] & ~zone[:, :-1]).sum(axis=1)


def range_mask(d: pd.DataFrame, v: Variant):
    """Sobre velas de la temporalidad de detección: (ok, piso, techo, ATR) vela a vela. La usa también el bot en
    papel (range_bot.py) con la última vela cerrada, para que decida exactamente como el laboratorio."""
    n = v.lookback
    ind = q.compute_indicators(d, q.Params())
    hi = d.high.rolling(n).max().to_numpy()
    lo = d.low.rolling(n).min().to_numpy()
    rng = hi - lo
    from numpy.lib.stride_tricks import sliding_window_view as swv
    hw = swv(d.high.to_numpy(), n)
    lw = swv(d.low.to_numpy(), n)
    pad = np.full(n - 1, np.nan)
    hi_v, lo_v, rng_v = hi[n - 1:], lo[n - 1:], rng[n - 1:]
    t_lo = _touch_events(lw, lo_v + v.tol * rng_v, below=True)
    t_hi = _touch_events(hw, hi_v - v.tol * rng_v, below=False)
    t_lo = np.r_[pad, t_lo]
    t_hi = np.r_[pad, t_hi]
    atr = ind.atr14.to_numpy()
    close = d.close.to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        width = (hi / lo - 1) * 100
        step = width * (1 - 2 * v.edge) / max(v.levels, 1)
        step_ok = step >= v.min_step_pct if v.levels else np.ones(len(width), bool)
        ok = ((width >= v.w_min) & (width <= v.w_max) & (t_lo >= v.touches) & (t_hi >= v.touches)
              & (rng >= v.min_range_atr * atr) & step_ok & (atr > 0)
              & (close > lo) & (close < hi))
        if v.max_adx:
            ok &= (ind.adx14.to_numpy() < v.max_adx) & (ind.adx14.to_numpy() > 0)
        if v.trend == "ema200":
            ok &= close > ind.ema200.to_numpy()
        if v.max_er:
            m_er = 4 * n
            cs = d.close
            er = (cs - cs.shift(m_er)).abs() / cs.diff().abs().rolling(m_er).sum()
            ok &= (er <= v.max_er).to_numpy()
    ok &= ~np.isnan(width)
    return ok, lo, hi, atr


def detect(df15: pd.DataFrame, v: Variant, grid_time: pd.DatetimeIndex, btc_ok: np.ndarray):
    d = resample(df15, v.det_tf) if v.det_tf != BASE else df15.copy()
    empty = (np.array([], int), np.array([]), np.array([]), np.array([]))
    if len(d) <= v.lookback + 30:
        return empty
    ok, lo, hi, atr = range_mask(d, v)
    idx = np.flatnonzero(ok)
    if not len(idx):
        return empty
    # La vela de det_tf que empieza en T cierra en T + tf: las órdenes valen desde la vela de 15 min de ese instante.
    when = pd.DatetimeIndex(d.time.iloc[idx]) + pd.Timedelta(minutes=BAR_MIN[v.det_tf])
    pos = grid_time.searchsorted(when)
    keep = pos < len(grid_time)
    idx, pos = idx[keep], pos[keep]
    if v.trend == "btc":
        keep = btc_ok[pos]
        idx, pos = idx[keep], pos[keep]
    return pos.astype(int), lo[idx], hi[idx], atr[idx]


# ════════════════════════════════════════════════════════════════════════════
# EPISODIOS POR MONEDA (independientes del saldo: todo en fracción de lo asignado)
# ════════════════════════════════════════════════════════════════════════════
def ladder(lo: float, hi: float, v: Variant) -> np.ndarray:
    r = hi - lo
    bottom, top = lo + v.edge * r, hi - v.edge * r
    if v.levels == 0:
        return np.array([bottom])
    return bottom + np.arange(v.levels + 1) * (top - bottom) / v.levels


def worst_loss_frac(lv: np.ndarray, stop: float, v: Variant) -> float:
    """Pérdida (fracción de lo asignado) si se llenan TODOS los escalones de compra y salta el stop."""
    if v.stop_atr <= 0:
        return 1.0
    if v.levels == 0:
        return 0.0
    unit = 1.0 / v.levels
    px = stop * (1 - SLIP)
    loss = sum(unit * (1 + FEE_LIMIT) - unit / b * px * (1 - FEE_MARKET) for b in lv[:-1])
    return max(loss, 1e-4)


def ride(o, h, l, c, start: int, end: int, side: str, entry: float, eq: float, sl: float, atr: float,
         trail: float, same_bar: bool = False) -> tuple[int, float, list, float]:
    """Una posición a favor de la ruptura con stop inicial `sl` y trailing. Devuelve (vela de salida, valor final,
    ruta del valor vela a vela, comisiones). Con same_bar el corto nace en la vela del stop: en esa vela solo se
    mira si también tocó su stop (peor caso)."""
    path, fees = [], 0.0
    if side == "long":
        qty = eq / (entry * (1 + FEE_MARKET))
        fees += qty * entry * FEE_MARKET
        best, stop = entry, sl
        for i in range(start, end):
            if l[i] <= stop:
                px = min(o[i], stop) * (1 - SLIP)
                fees += qty * px * FEE_MARKET
                path.append(qty * px * (1 - FEE_MARKET))
                return i, path[-1], path, fees
            best = max(best, h[i])
            stop = max(stop, best - trail * atr)
            path.append(qty * c[i])
        px = c[end - 1] * (1 - SLIP)
        fees += qty * px * FEE_MARKET
        path[-1] = qty * px * (1 - FEE_MARKET)
        return end - 1, path[-1], path, fees
    qty = eq / entry
    fees += qty * entry * FEE_MARKET
    base = eq - qty * entry * FEE_MARKET
    best, stop = entry, sl
    for i in range(start, end):
        if h[i] >= stop:
            px = (stop if same_bar and i == start else max(o[i], stop)) * (1 + SLIP)
            fees += qty * px * FEE_MARKET
            path.append(base + qty * (entry - px) - qty * px * FEE_MARKET)
            return i, path[-1], path, fees
        if not (same_bar and i == start):
            best = min(best, l[i])
            stop = min(stop, best + trail * atr)
        path.append(base + qty * (entry - c[i]))
    px = c[end - 1] * (1 + SLIP)
    fees += qty * px * FEE_MARKET
    path[-1] = base + qty * (entry - px) - qty * px * FEE_MARKET
    return end - 1, path[-1], path, fees


def breakout_worst(lo: float, hi: float, atr: float, v: Variant) -> float:
    """Pérdida máxima aproximada de la pierna de ruptura (fracción de lo asignado): del disparo a la mitad del rango."""
    mid = (lo + hi) / 2
    w = 0.0
    if v.brk_long:
        e = hi + v.brk_atr * atr
        w = max(w, (e - mid) / e + 2 * FEE_MARKET + 2 * SLIP)
    if v.brk_short and v.stop_atr > 0:
        e = lo - v.stop_atr * atr
        w = max(w, (mid - e) / e + 2 * FEE_MARKET + 2 * SLIP)
    return w


def run_episode(o, h, l, c, i0: int, end: int, lo: float, hi: float, atr: float, v: Variant) -> dict:
    """Simula un rango en una moneda desde la vela i0 (15 min). Devuelve la ruta del valor (1 = lo asignado)."""
    lv = ladder(lo, hi, v)
    G = v.levels
    stop = lo - v.stop_atr * atr if v.stop_atr > 0 else -np.inf
    up_exit = hi + (v.brk_atr if v.brk_long else max(v.stop_atr, 1.0)) * atr
    mid = (lo + hi) / 2
    life = end - i0
    unit = 1.0 / G if G else 0.0
    cash = 1.0
    qty = np.zeros(G)
    held_from = np.full(G, -1)          # vela desde la que el escalón comprado puede venderse
    armed_from = np.where(lv[:-1] < c[i0 - 1], i0, -1)      # solo compra por debajo del precio actual
    path = []
    rt, reason, fees = 0, "TIEMPO", 0.0
    i = i0
    for i in range(i0, end):
        oi, hi_, li, ci = o[i], h[i], l[i], c[i]
        hit_stop = li <= stop
        # 1) ventas de escalones comprados en velas anteriores (no en la vela del stop: peor caso)
        if not hit_stop:
            for g in range(G):
                if qty[g] > 0 and 0 <= held_from[g] <= i and hi_ > lv[g + 1]:
                    px = max(oi, lv[g + 1])
                    cash += qty[g] * px * (1 - FEE_LIMIT)
                    fees += qty[g] * px * FEE_LIMIT
                    qty[g], held_from[g] = 0.0, -1
                    armed_from[g] = i + 1
                    rt += 1
        # 2) compras: el precio atraviesa el escalón
        for g in range(G):
            if qty[g] == 0 and 0 <= armed_from[g] <= i and li < lv[g]:
                px = min(oi, lv[g])
                qty[g] = unit / px
                cash -= unit * (1 + FEE_LIMIT)
                fees += unit * FEE_LIMIT
                held_from[g], armed_from[g] = i + 1, -1
        # 3) stop por ruptura hacia abajo
        if hit_stop:
            px = min(oi, stop) * (1 - SLIP)
            held = qty.sum()
            cash += held * px * (1 - FEE_MARKET)
            fees += held * px * FEE_MARKET
            qty[:] = 0
            reason = "RUPTURA_ABAJO"
            if v.brk_short and v.stop_atr > 0:
                j, cash, seg, f2 = ride(o, h, l, c, i, min(len(c), i + life), "short", px, cash, mid, atr,
                                        v.trail_atr, same_bar=True)
                path += seg
                fees += f2
                rt += 1
                i, reason = j, "CORTO_TRAS_RUPTURA"
            else:
                path.append(cash)
            break
        # 4) re-armar escalones libres que quedaron por debajo del precio
        for g in range(G):
            if qty[g] == 0 and armed_from[g] < 0 and lv[g] < ci:
                armed_from[g] = i + 1
        eq = cash + float((qty * ci).sum())
        path.append(eq)
        if v.brk_long and ci > up_exit:
            held = qty.sum()
            if held:                                        # lo comprado en esta vela se vende a mercado
                px = ci * (1 - SLIP)
                cash += held * px * (1 - FEE_MARKET)
                fees += held * px * FEE_MARKET
                qty[:] = 0
                path[-1] = cash
            if i + 1 < len(c):
                j, cash, seg, f2 = ride(o, h, l, c, i + 1, min(len(c), i + 1 + life), "long", o[i + 1] * (1 + SLIP),
                                        cash, mid, atr, v.trail_atr)
                path += seg
                fees += f2
                rt += 1
                i, reason = j, "LARGO_TRAS_RUPTURA"
            else:
                reason = "RUPTURA_ARRIBA"
            break
        if ci > up_exit and not qty.any():
            reason = "RUPTURA_ARRIBA"
            break
    else:
        held = qty.sum()
        if held:
            px = c[end - 1] * (1 - SLIP)
            cash += held * px * (1 - FEE_MARKET)
            fees += held * px * FEE_MARKET
            path[-1] = cash
        reason = "TIEMPO"
    return {"end": i, "ret": path[-1] - 1.0, "path": np.asarray(path, np.float32), "roundtrips": rt,
            "reason": reason, "fees": fees,
            "worst": max(worst_loss_frac(lv, stop, v) + (breakout_worst(lo, hi, atr, v) if v.brk_short else 0.0),
                         breakout_worst(lo, hi, atr, v), 1e-4)}


def coin_episodes(m: Market, j: int, v: Variant, acts) -> list[dict]:
    pos, los, his, atrs = acts
    o, h, l, c = m.o[:, j], m.h[:, j], m.l[:, j], m.c[:, j]
    T = len(c)
    life = int(v.life_mult * v.lookback * BAR_MIN[v.det_tf] / BAR_MIN[BASE])
    eps, free_at = [], 0
    for k in range(len(pos)):
        i0 = int(pos[k])
        if i0 < free_at or i0 < m.start[j] + 1 or i0 >= T - 1:
            continue
        e = run_episode(o, h, l, c, i0, min(T, i0 + life), los[k], his[k], atrs[k], v)
        e.update(coin=j, i0=i0, lo=float(los[k]), hi=float(his[k]))
        eps.append(e)
        free_at = e["end"] + 1
    return eps


def all_episodes(m: Market, v: Variant) -> list[dict]:
    acts = m.activations(v)
    eps = []
    for j in range(len(m.symbols)):
        eps += coin_episodes(m, j, v, acts[j])
    eps.sort(key=lambda e: (e["i0"], e["coin"]))
    return eps


# ════════════════════════════════════════════════════════════════════════════
# CUENTA ÚNICA (Capa 1 + Capa 3)
# ════════════════════════════════════════════════════════════════════════════
def _season_gate(eps: list[dict], v: Variant, colset):
    """Semáforo de temporadas. El bot sigue todos los rangos "en sombra" (sin dinero) y solo opera de verdad si los
    últimos season_n rangos que YA terminaron (de todas las monedas o de esa moneda) suman ganancia. Solo usa el
    pasado: un rango cuenta cuando su vela de cierre ya pasó."""
    if not v.season_n:
        return None
    sh = [e for e in eps if e["fees"] > 0 and (colset is None or e["coin"] in colset)]
    groups: dict = {}
    for e in sh:
        groups.setdefault("all" if v.season_scope == "all" else e["coin"], []).append(e)
    tables = {}
    for k, lst in groups.items():
        lst.sort(key=lambda e: e["end"])
        ends = np.array([e["end"] for e in lst])
        cum = np.r_[0.0, np.cumsum([e["ret"] for e in lst])]
        tables[k] = (ends, cum)
    n = v.season_n

    def ok(t: int, coin: int) -> bool:
        tab = tables.get("all" if v.season_scope == "all" else coin)
        if tab is None:
            return False
        ends, cum = tab
        k = int(np.searchsorted(ends, t, side="left"))      # terminados antes de la vela t
        return k >= n and cum[k] - cum[k - n] > 0
    return ok


def account(m: Market, eps: list[dict], v: Variant, balance: float = 500.0, lo: int = 0, hi: int | None = None,
            cols=None, keep: bool = False) -> dict:
    hi = len(m.time) if hi is None else hi
    span = hi - lo
    realized = np.zeros(span + 1)
    mtm = np.zeros(span + 1)
    bal = peak = balance
    active: list[tuple[int, float, dict]] = []          # (fin, asignado, episodio)
    used = 0.0
    losses, halt_until, killed, killed_at = 0, -1, False, ""
    day_cur, day_peak, day_halt = None, bal, False
    halt_bars = 15 * BAR_MIN[v.det_tf] // BAR_MIN[BASE]
    taken, fees, rts, reasons = [], 0.0, 0, {}
    busy = np.zeros(span + 1, int)
    colset = None if cols is None else set(cols)
    gate = _season_gate(eps, v, colset)
    cand = passed = 0

    def close_until(t):
        nonlocal bal, peak, used, losses, halt_until, killed, killed_at, day_halt, fees, rts
        active.sort(key=lambda x: x[0])
        while active and active[0][0] < t:
            end, alloc, e = active.pop(0)
            pnl = alloc * e["ret"]
            bal += pnl
            used -= alloc
            fees += alloc * e["fees"]
            rts += e["roundtrips"]
            reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
            if end - lo <= span:
                realized[max(0, end - lo)] += pnl
            peak = max(peak, bal)
            losses = 0 if pnl > 0 else (losses + 1 if pnl < 0 else losses)
            if losses >= 3:
                halt_until, losses = end + halt_bars, 0
            if day_peak and (day_peak - bal) / day_peak * 100 >= 3.0 and m.day[min(end, len(m.day) - 1)] == day_cur:
                day_halt = True
            if not killed and (peak - bal) / peak * 100 >= v.kill_dd_pct:
                killed, killed_at = True, str(m.time[min(end, len(m.time) - 1)])
            if keep and e["fees"] > 0:          # solo los rangos en los que llegó a comprar
                taken.append({"symbol": m.symbols[e["coin"]], "opened": m.time[e["i0"]],
                              "closed": m.time[min(e["end"], len(m.time) - 1)], "floor": e["lo"],
                              "ceiling": e["hi"], "allocated": alloc, "pnl": pnl, "ret_pct": e["ret"] * 100,
                              "roundtrips": e["roundtrips"], "reason": e["reason"],
                              "hours": (e["end"] - e["i0"] + 1) / 4})

    for e in eps:
        t = e["i0"]
        if t < lo or t >= hi:
            continue
        if colset is not None and e["coin"] not in colset:
            continue
        close_until(t)
        if m.day[t] != day_cur:
            day_cur, day_peak, day_halt = m.day[t], bal, False
        day_peak = max(day_peak, bal)
        if killed or day_halt or t <= halt_until or len(active) >= v.max_open:
            continue
        cand += 1
        if gate is not None and not gate(t, e["coin"]):
            continue
        passed += 1
        rp = v.risk_pct / 2 if losses == 2 else v.risk_pct
        cap = bal * v.max_alloc_pct / 100
        alloc = min(cap, bal * rp / 100 / e["worst"], bal * 0.85 - used) if v.stop_atr > 0 else \
            min(cap, bal * 0.85 - used)
        if alloc / max(v.levels, 1) < MIN_NOTIONAL:
            continue
        used += alloc
        active.append((e["end"], alloc, e))
        a, b = t - lo, min(t - lo + len(e["path"]) - 1, span)      # la ruta cubre i0..fin; el fin se realiza
        if b > a:
            mtm[a:b] += alloc * (e["path"][:b - a] - 1.0)
            busy[a:b] += 1
    close_until(10 ** 12)
    eq = balance + np.cumsum(realized)[:span] + mtm[:span]
    eq_peak = np.maximum.accumulate(eq) if span else np.array([balance])
    dd = float(((eq_peak - eq) / eq_peak * 100).max()) if span else 0.0
    out_pnls = np.array([t["pnl"] for t in taken], float) if keep else None
    res = {"return_pct": (bal / balance - 1) * 100, "max_dd_pct": dd,
           "episodes": len(taken) if keep else sum(reasons.values()), "ranges_seen": sum(reasons.values()),
           "roundtrips": rts, "fees_usd": fees, "final_balance": bal, "killed_at": killed_at,
           "reasons": reasons, "busy_pct": float((busy[:span] > 0).mean() * 100) if span else 0.0,
           "gate_open_pct": passed / cand * 100 if cand else 0.0}
    if keep:
        res["trades_df"] = pd.DataFrame(taken)
        w, ls = out_pnls[out_pnls > 0].sum(), -out_pnls[out_pnls < 0].sum()
        res["win_rate_pct"] = float((out_pnls > 0).mean() * 100) if len(out_pnls) else 0.0
        res["profit_factor"] = float(w / ls) if ls > 0 else (float("inf") if w > 0 else 0.0)
    return res


def account_stats(m: Market, eps, v: Variant, balance: float, lo: int, hi: int | None = None, cols=None) -> dict:
    r = account(m, eps, v, balance, lo, hi, cols, keep=True)
    r.pop("trades_df")
    return r


# ════════════════════════════════════════════════════════════════════════════
# REJILLA, VALIDACIÓN E INFORME
# ════════════════════════════════════════════════════════════════════════════
def grid() -> list[Variant]:
    """Pocas variantes con sentido (no miles: así no se ajusta ruido). ~192."""
    shapes = [("1h", 24, 1.5, 6.0), ("1h", 48, 2.0, 8.0), ("1h", 96, 3.0, 12.0),
              ("4h", 30, 3.0, 12.0), ("4h", 60, 4.0, 15.0), ("1h", 48, 1.0, 4.0)]
    out = []
    for (tf, n, w0, w1), t, lv, k, tr in itertools.product(shapes, (2, 3), (1, 3, 6), (0.5, 1.5),
                                                          ("none", "ema200")):
        out.append(Variant(det_tf=tf, lookback=n, w_min=w0, w_max=w1, touches=t, levels=lv, stop_atr=k,
                           trend=tr))
    return out


def grid_short() -> list[Variant]:
    """Rangos cortos detectados en velas de 15 min (6 a 24 horas), pedidos por Keiner el 31-oct. ~120.
    Escalones 1/2/4: con rangos estrechos más escalones no dejan el 0,6% mínimo por escalón."""
    shapes = [("15m", 24, 0.8, 2.5), ("15m", 48, 0.8, 3.0), ("15m", 48, 1.5, 5.0), ("15m", 96, 1.0, 4.0),
              ("15m", 96, 2.0, 6.0)]
    out = []
    for (tf, n, w0, w1), t, lv, k, tr in itertools.product(shapes, (2, 3), (1, 2, 4), (0.5, 1.5),
                                                          ("none", "ema200")):
        out.append(Variant(det_tf=tf, lookback=n, w_min=w0, w_max=w1, touches=t, levels=lv, stop_atr=k,
                           trend=tr))
    return out


def grid_seasons() -> list[Variant]:
    """Temporadas (Keiner, 1-nov): las mejores formas de 15 y 30 min, pero operando solo cuando el lateral está
    confirmado (eficiencia baja) y/o el semáforo de rangos en sombra está en verde. 48 variantes."""
    shapes = [("15m", 96, 2.0, 6.0), ("15m", 48, 1.5, 5.0), ("30m", 48, 2.0, 6.0), ("30m", 96, 3.0, 8.0)]
    seasons = [(0, "all"), (20, "all"), (5, "coin")]
    out = []
    for (tf, n, w0, w1), lv, er, (sn, sc) in itertools.product(shapes, (1, 2), (0.0, 0.3), seasons):
        out.append(Variant(det_tf=tf, lookback=n, w_min=w0, w_max=w1, touches=2, levels=lv, stop_atr=1.5,
                           max_er=er, season_n=sn, season_scope=sc))
    return out


def grid_breakout() -> list[Variant]:
    """Ruptura del lateral (Keiner, 1-nov): rangos confirmados (incluidos anchos de hasta 25%), con o sin rebotes
    dentro, y al romperse se entra a favor: largo arriba, y en papel también corto abajo. Riesgo 1,5% o 3%. 160."""
    shapes = [("15m", 96, 2.0, 6.0), ("1h", 96, 3.0, 12.0), ("4h", 30, 3.0, 12.0), ("4h", 60, 4.0, 15.0),
              ("4h", 90, 6.0, 25.0)]
    out = []
    for (tf, n, w0, w1), lv, short, k, tr, risk in itertools.product(shapes, (0, 1), (False, True), (0.5, 1.5),
                                                                     (3.0, 5.0), (1.5, 3.0)):
        out.append(Variant(det_tf=tf, lookback=n, w_min=w0, w_max=w1, touches=2, levels=lv, stop_atr=k,
                           brk_long=True, brk_short=short, trail_atr=tr, risk_pct=risk))
    return out


def grid_breakout_4h() -> list[Variant]:
    """Confirmación de la ruptura en 4 h (2-nov): la corrida 'ruptura' mostró que en 4 h varias variantes ganan en las
    dos mitades, pero ninguna pasó por los 20 grupos. Aquí se mide TODO el vecindario (5 formas × 3 stops × 3
    trailings × largo / largo+corto, riesgo 1,5%, sin rebotes) con la prueba completa: si la idea es real, debe
    funcionar en buena parte del vecindario, no solo en una combinación suelta. 90 variantes; correr con --top 90."""
    shapes = [("4h", 30, 3.0, 12.0), ("4h", 45, 3.0, 12.0), ("4h", 60, 4.0, 15.0), ("4h", 75, 5.0, 20.0),
              ("4h", 90, 6.0, 25.0)]
    out = []
    for (tf, n, w0, w1), k, tr, short in itertools.product(shapes, (0.5, 1.0, 1.5), (3.0, 4.0, 5.0), (False, True)):
        out.append(Variant(det_tf=tf, lookback=n, w_min=w0, w_max=w1, touches=2, levels=0, stop_atr=k,
                           brk_long=True, brk_short=short, trail_atr=tr))
    return out


GRIDS = {"normal": grid, "corto": grid_short, "temporadas": grid_seasons, "ruptura": grid_breakout,
         "ruptura4h": grid_breakout_4h}


def score(r: dict) -> float:
    if r["episodes"] < 30:
        return -1e9
    return r["return_pct"] / max(r["max_dd_pct"], 5.0)


_M: Market | None = None
_W: dict = {}


def _init(m, windows):
    global _M, _W
    _M, _W = m, windows


def _task(v: Variant) -> tuple[Variant, dict]:
    m, w = _M, _W
    eps = all_episodes(m, v)
    out = {k: account_stats(m, eps, v, w["balance"], *rng) for k, rng in
           (("train", (w["lo"], w["mid"])), ("valid", (w["mid"], None)), ("full", (w["lo"], None)))}
    return v, out


def _final_task(v: Variant) -> tuple[Variant, dict]:
    m, w = _M, _W
    eps = all_episodes(m, v)
    out = {"groups": [account_stats(m, eps, v, w["balance"], w["lo"], None, g)["return_pct"]
                      for g in w["groups"]]}
    years = {}
    for y in sorted(set(m.year[w["lo"]:])):
        idx = np.flatnonzero(m.year == y)
        a, b = max(int(idx[0]), w["lo"]), int(idx[-1]) + 1
        years[int(y)] = account_stats(m, eps, v, w["balance"], a, b)["return_pct"]
    out["years"] = years
    nostop = replace(v, stop_atr=0.0)
    eps2 = all_episodes(m, nostop)
    out["nostop"] = account_stats(m, eps2, nostop, w["balance"], w["lo"])
    return v, out


def buy_hold(m: Market, lo: int, hi: int | None = None) -> float:
    hi = len(m.time) - 1 if hi is None else hi - 1
    rets = []
    for j in range(len(m.symbols)):
        a = max(lo, m.start[j])
        if a < hi and m.c[a, j] > 0:
            rets.append(m.c[hi, j] / m.c[a, j] - 1)
    return float(np.mean(rets) * 100) if rets else 0.0


def run(csv_dir: str, symbols, out_dir: str, trade_from: str, balance: float, workers: int,
        variants: list[Variant] | None = None, top: int = 5) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    data = hl.load(csv_dir, symbols, BASE)
    m = Market(data)
    lo = int(m.time.searchsorted(hl._ts(trade_from)))
    mid = lo + (len(m.time) - lo) // 2
    windows = {"balance": balance, "lo": lo, "mid": mid, "groups": hl.subsets(len(m.symbols))}
    variants = variants or grid()
    print(f"[run] {len(m.symbols)} monedas, {len(m.time):,} velas de 15 min, {len(variants)} variantes",
          file=sys.stderr)
    ctx = mp.get_context("fork")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_init,
                             initargs=(m, windows)) as ex:
        res = []
        for k, r in enumerate(ex.map(_task, variants, chunksize=1)):
            res.append(r)
            if (k + 1) % 10 == 0:
                print(f"[run] {k + 1}/{len(variants)}", file=sys.stderr)
        res.sort(key=lambda x: -score(x[1]["train"]))
        best = [x[0] for x in res[:top]]
        extra = dict(ex.map(_final_task, best, chunksize=1))
    finals = []
    for v in best:
        r = next(x[1] for x in res if x[0] == v)
        e = extra[v]
        gpos = sum(g > 0 for g in e["groups"])
        fu = r["full"]
        passed = (fu["return_pct"] > 0 and r["valid"]["return_pct"] > 0 and gpos >= 15 and fu["max_dd_pct"] < 20
                  and fu["episodes"] >= 40)
        finals.append({"variant": v, **r, **e, "groups_pos": gpos, "passed": passed})
    pd.DataFrame([{"label": f["variant"].label(), "train": f["train"]["return_pct"], "valid": f["valid"]["return_pct"],
                   "full": f["full"]["return_pct"], "max_dd": f["full"]["max_dd_pct"], "episodes": f["full"]["episodes"],
                   "pf": f["full"]["profit_factor"], "groups_pos": f["groups_pos"], "passed": f["passed"],
                   "killed_at": f["full"]["killed_at"], **{f"y{y}": r for y, r in f["years"].items()}}
                  for f in finals]).to_csv(Path(out_dir) / "finales_rango.csv", index=False)
    rows = [{"label": v.label(), **asdict(v),
             **{f"{w}_{k}": x for w in ("train", "valid", "full") for k, x in r[w].items() if k != "reasons"}}
            for v, r in res]
    pd.DataFrame(rows).to_csv(Path(out_dir) / "variantes_rango.csv", index=False)
    v0 = finals[0]["variant"]
    full = account(m, all_episodes(m, v0), v0, balance, lo, keep=True)
    full["trades_df"].to_csv(Path(out_dir) / "rangos_mejor.csv", index=False)
    meta = {"t0": m.time[lo], "mid": m.time[mid], "t1": m.time[-1], "n_symbols": len(m.symbols),
            "bh_full": buy_hold(m, lo), "bh_valid": buy_hold(m, mid), "n_variants": len(res),
            "train_pos": sum(x[1]["train"]["return_pct"] > 0 for x in res),
            "valid_pos": sum(x[1]["valid"]["return_pct"] > 0 for x in res),
            "full_pos": sum(x[1]["full"]["return_pct"] > 0 for x in res),
            "best_full": max(res, key=lambda x: x[1]["full"]["return_pct"]),
            "balance": balance}
    md = report_md(finals, meta)
    (Path(out_dir) / "informe_rango.md").write_text(md, encoding="utf-8")
    ok = [f for f in finals if f["passed"]]
    if ok:
        (Path(out_dir) / "rango_params_candidato.json").write_text(json.dumps(asdict(ok[0]["variant"]), indent=2),
                                                                     encoding="utf-8")
    return md


def _f(x: float) -> str:
    return "∞" if x == float("inf") else f"{x:+.1f}"


def report_md(finals: list[dict], meta: dict) -> str:
    L = ["# Laboratorio del bot lateral (rebotes en rango)", "",
         f"Cuenta única de {meta['balance']:,.0f} USD sobre {meta['n_symbols']} monedas de Binance Spot, simulada en "
         f"velas de 15 min. Comisión 0,1% por orden (límites y mercado) y 0,05% de deslizamiento en stops. Capa 1 "
         f"completa, riesgo 1,5% por rango si se llenan todos los escalones y salta el stop, máx. 30% por rango, "
         f"3 rangos a la vez, apagado al -20%.", "",
         f"Entrenamiento {meta['t0']:%Y-%m-%d} → {meta['mid']:%Y-%m-%d} · validación {meta['mid']:%Y-%m-%d} → "
         f"{meta['t1']:%Y-%m-%d}. Comprar y mantener todas las monedas a partes iguales: completo "
         f"{_f(meta['bh_full'])}%, validación {_f(meta['bh_valid'])}%.", "",
         f"De {meta['n_variants']} variantes: {meta['train_pos']} positivas en entrenamiento, {meta['valid_pos']} en "
         f"validación y {meta['full_pos']} en el periodo completo.", "",
         f"Pasan todas las condiciones: {sum(f['passed'] for f in finals)} de {len(finals)} medidas a fondo "
         f"(largo solo: {sum(f['passed'] for f in finals if not f['variant'].brk_short)}, "
         f"largo+corto: {sum(f['passed'] for f in finals if f['variant'].brk_short)}). Positivas en entrenamiento Y "
         f"validación: {sum(f['train']['return_pct'] > 0 and f['valid']['return_pct'] > 0 for f in finals)}.", "",
         f"## Las {len(finals)} mejores del entrenamiento", "",
         "| Variante | Entren. % | Valid. % | Completo % | Caída máx. % | Rangos | Idas y vueltas | Acierto % | "
         "PF | Comisiones USD | Grupos > 0 | ¿Pasa? |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for f in finals:
        fu = f["full"]
        off = f" (apagado {fu['killed_at'][:10]})" if fu["killed_at"] else ""
        L.append(f"| {f['variant'].label()} | {_f(f['train']['return_pct'])} | {_f(f['valid']['return_pct'])} | "
                 f"{_f(fu['return_pct'])}{off} | {fu['max_dd_pct']:.1f} | {fu['episodes']} | {fu['roundtrips']} | "
                 f"{fu['win_rate_pct']:.0f} | {fu['profit_factor']:.2f} | {fu['fees_usd']:.0f} | "
                 f"{f['groups_pos']}/{len(f['groups'])} | {'SÍ' if f['passed'] else 'no'} |")
    L += ["", "## Por año y sin stop", "",
          "| Variante | " + " | ".join(str(y) for y in finals[0]["years"]) + " | Misma escalera SIN stop % "
          "(caída máx.) | Semáforo en verde % | Cómo terminaron los rangos |",
          "| --- | " + " | ".join("---" for _ in finals[0]["years"]) + " | --- | --- | --- |"]
    for f in finals:
        ns = f["nostop"]
        why = ", ".join(f"{k} {n}" for k, n in sorted(f["full"]["reasons"].items()))
        L.append(f"| {f['variant'].label()} | " + " | ".join(_f(x) for x in f["years"].values())
                 + f" | {_f(ns['return_pct'])} ({ns['max_dd_pct']:.1f}) | {f['full']['gate_open_pct']:.0f} | {why} |")
    bv, br = meta["best_full"]
    L += ["", f"Referencia (mirando el futuro, NO vale para elegir): la mejor variante del periodo completo fue "
              f"«{bv.label()}» con {_f(br['full']['return_pct'])}% y caída {br['full']['max_dd_pct']:.1f}%.", "",
          "## Veredicto", ""]
    if any(f["passed"] for f in finals):
        L.append("Al menos una variante pasa todas las condiciones: se puede construir el bot lateral en papel con "
                 "ella (ver rango_params_candidato.json).")
    else:
        L.append("Ninguna variante pasa todas las condiciones: el bot lateral NO se enciende con estas reglas.")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Laboratorio del bot lateral (rebotes en rango).")
    ap.add_argument("mode", choices=("download", "run"))
    ap.add_argument("--csv-dir", default="data/cazador/velas")
    ap.add_argument("--out-dir", default="data/rango/informes")
    ap.add_argument("--symbols", default=",".join(UNIVERSE))
    ap.add_argument("--since", default="2023-06-01")
    ap.add_argument("--trade-from", default="2024-01-01")
    ap.add_argument("--balance", type=float, default=500.0)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--top", type=int, default=5, help="cuántas (por entrenamiento) pasan a la prueba completa")
    ap.add_argument("--set", choices=tuple(GRIDS), default="normal",
                    help="normal = rangos en 1 h / 4 h; corto = rangos en velas de 15 min; "
                         "temporadas = 15/30 min con lateral confirmado y semáforo; "
                         "ruptura = operar la ruptura del lateral (largo y corto)")
    a = ap.parse_args(argv)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if a.mode == "download":
        print("\n".join(hl.download(a.csv_dir, symbols, BASE, a.since)))
    else:
        print(run(a.csv_dir, symbols, a.out_dir, a.trade_from, a.balance, a.workers, variants=GRIDS[a.set](),
                  top=a.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
