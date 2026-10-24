#!/usr/bin/env python3
"""
quant_core_engine.py — Núcleo cuantitativo del bot (3 Skills en un solo motor).

Cadena de ejecución obligatoria (en este orden):
  CAPA 1  postmortem-circuit-breaker  -> estado de la cuenta (ACTIVE / THROTTLED / HALTED)
  CAPA 2  market-regime-quant         -> régimen de mercado y señal (LONG / SHORT / WAIT)
  CAPA 3  atr-risk-guardian           -> SL, TP, Break-Even y tamaño de posición

Sin APIs externas ni conexión a internet: solo Python estándar + pandas + numpy.

Entradas aceptadas:
  * CSV de velas OHLCV (exportado de TradingView o Binance, con o sin cabecera).
  * Diccionario / archivo JSON con velas (lista de dicts, lista de listas o columnas).
  * Datos manuales: indicadores ya leídos del gráfico (close, ema20, ema50, ema200,
    ema20_prev3, rsi14, atr14).

Uso rápido:
  python quant_core_engine.py --csv BTCUSDT_1h.csv --balance 10000 --state account_state.json
  python quant_core_engine.py --json velas.json --balance 10000 --losses 2 --dd 1.1
  python quant_core_engine.py --manual manual_input.json --balance 10000
  python quant_core_engine.py --demo          # ejecuta los 3 escenarios de verificación

Salida: tabla Markdown (resumen ejecutivo) + bloque JSON estricto para n8n / CCXT / Alpaca.
Con --json-only solo se imprime el JSON (para webhooks).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ENGINE_NAME = "quant_core_engine"
ENGINE_VERSION = "1.0.0"


# ════════════════════════════════════════════════════════════════════════════
# PARÁMETROS (todos editables a mano; ver SKILL_*.md)
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class Params:
    # --- Skill 1: market-regime-quant ---
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    rsi_len: int = 14
    atr_len: int = 14
    slope_lookback: int = 3            # velas para la pendiente de EMA20
    slope_min_pct: float = 0.2         # |pendiente| mínima en % (estrictamente mayor)
    rsi_long_min: float = 46.0
    rsi_long_max: float = 64.0
    rsi_short_min: float = 36.0
    rsi_short_max: float = 54.0
    max_extension_atr: float = 3.2     # |Close - EMA20| / ATR > 3.2 -> EXHAUSTION_CLIMAX
    allow_short: bool = True           # False en cuentas spot sin margen

    # --- Skill 2: atr-risk-guardian ---
    risk_base_pct: float = 1.5
    risk_throttled_pct: float = 0.75
    sl_atr_mult: float = 1.6
    tp_atr_mult: float = 3.6
    be_atr_mult: float = 1.5
    be_fee_offset_pct: float = 0.2     # SL nuevo = Entry * (1 + 0.2%) en long
    max_allocation_pct: float = 85.0   # tope del capital por posición
    price_tick: float = 0.01           # redondeo de precios ("al centavo")
    qty_step: float = 0.000001         # paso mínimo de cantidad del exchange
    min_notional_usd: float = 10.0     # por debajo de esto no se envía orden

    # --- Skill 3: postmortem-circuit-breaker ---
    throttle_losses: int = 2
    throttle_dd_pct: float = 2.0
    halt_losses: int = 3
    halt_dd_pct: float = 3.0
    cooldown_candles: int = 15

    @property
    def min_candles(self) -> int:
        return self.ema_slow + self.slope_lookback

    @property
    def recommended_candles(self) -> int:
        return self.ema_slow * 2


# ════════════════════════════════════════════════════════════════════════════
# UTILIDADES NUMÉRICAS
# ════════════════════════════════════════════════════════════════════════════
def _dec(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def round_to_step(value: float, step: float, mode=ROUND_HALF_UP) -> float:
    """Redondea a un múltiplo de `step` con aritmética decimal (sin errores de float)."""
    step_d = _dec(step)
    q = (_dec(value) / step_d).quantize(Decimal("1"), rounding=mode)
    return float(q * step_d)


def floor_to_step(value: float, step: float) -> float:
    return round_to_step(value, step, ROUND_DOWN)


def ceil_to_step(value: float, step: float) -> float:
    return round_to_step(value, step, ROUND_CEILING)


def _clean(obj: Any) -> Any:
    """Convierte numpy/NaN a tipos JSON estrictos (NaN/inf -> null)."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    return obj


# ════════════════════════════════════════════════════════════════════════════
# INDICADORES (idénticos al método de TradingView: semilla SMA + recursión)
# ════════════════════════════════════════════════════════════════════════════
def ema(series: pd.Series, length: int) -> pd.Series:
    """EMA estilo TradingView: primer valor = SMA(length), luego alpha = 2/(length+1)."""
    values = series.to_numpy(dtype=float)
    out = np.full(values.shape, np.nan)
    if len(values) < length:
        return pd.Series(out, index=series.index)
    alpha = 2.0 / (length + 1.0)
    out[length - 1] = values[:length].mean()
    for i in range(length, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return pd.Series(out, index=series.index)


def rma(values: np.ndarray, length: int, start: int = 0) -> np.ndarray:
    """Media de Wilder (ta.rma): semilla SMA, luego alpha = 1/length. `start` = primer índice válido."""
    out = np.full(values.shape, np.nan)
    first = start + length - 1
    if len(values) <= first:
        return out
    out[first] = values[start:first + 1].mean()
    alpha = 1.0 / length
    for i in range(first + 1, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    c = close.to_numpy(dtype=float)
    delta = np.diff(c, prepend=np.nan)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = rma(gain, length, start=1)
    avg_loss = rma(loss, length, start=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100.0 - 100.0 / (1.0 + rs)
    out = np.where((avg_loss == 0) & (avg_gain > 0), 100.0, out)
    out = np.where((avg_loss == 0) & (avg_gain == 0), 50.0, out)
    out[np.isnan(avg_gain)] = np.nan
    return pd.Series(out, index=close.index)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    h, l, c = (s.to_numpy(dtype=float) for s in (high, low, close))
    prev_c = np.roll(c, 1)
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    tr[0] = h[0] - l[0]
    return pd.Series(rma(tr, length, start=0), index=close.index)


def compute_indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = ema(out["close"], p.ema_fast)
    out["ema50"] = ema(out["close"], p.ema_mid)
    out["ema200"] = ema(out["close"], p.ema_slow)
    out["rsi14"] = rsi(out["close"], p.rsi_len)
    out["atr14"] = atr(out["high"], out["low"], out["close"], p.atr_len)
    out["ema20_slope_pct"] = (out["ema20"] / out["ema20"].shift(p.slope_lookback) - 1.0) * 100.0
    return out


# ════════════════════════════════════════════════════════════════════════════
# CARGA DE DATOS (CSV TradingView / Binance, JSON, manual)
# ════════════════════════════════════════════════════════════════════════════
_ALIASES = {
    "time": "time", "date": "time", "datetime": "time", "timestamp": "time",
    "open_time": "time", "open time": "time", "opentime": "time",
    "open": "open", "o": "open",
    "high": "high", "h": "high",
    "low": "low", "l": "low",
    "close": "close", "c": "close", "price": "close",
    "volume": "volume", "vol": "volume", "v": "volume", "volume usdt": "volume",
}
_BINANCE_KLINE_COLS = ["time", "open", "high", "low", "close", "volume", "close_time",
                       "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]


def _to_datetime(col: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(col):
        v = col.astype("float64")
        mx = v.abs().max()
        unit = "us" if mx > 1e14 else ("ms" if mx > 1e11 else "s")
        return pd.to_datetime(v, unit=unit, utc=True)
    return pd.to_datetime(col, utc=True, errors="coerce")


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza nombres de columnas, tipos y orden temporal."""
    df = df.copy()
    # CSV de Binance sin cabecera: 12 columnas numéricas
    if all(isinstance(c, int) for c in df.columns) or str(df.columns[0]).strip().isdigit():
        if df.shape[1] >= 6:
            df = df.iloc[:, :12]
            df.columns = _BINANCE_KLINE_COLS[: df.shape[1]]
    rename = {}
    for c in df.columns:
        key = str(c).strip().lower()
        if key in _ALIASES and _ALIASES[key] not in rename.values():
            rename[c] = _ALIASES[key]
    df = df.rename(columns=rename)
    missing = [c for c in ("open", "high", "low", "close") if c not in df.columns]
    if missing:
        raise ValueError(f"Faltan columnas OHLC: {missing}. Columnas recibidas: {list(df.columns)}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "time" in df.columns:
        df["time"] = _to_datetime(df["time"])
        df = df.sort_values("time")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    bad = (df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1))
    if bad.any():
        raise ValueError(f"{int(bad.sum())} velas con high/low incoherentes (ej. fila {int(bad.idxmax())}).")
    cols = ["time"] if "time" in df.columns else []
    return df[cols + ["open", "high", "low", "close", "volume"]]


def load_csv(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    with open(path, "r", encoding="utf-8-sig") as fh:
        first = fh.readline()
    sep = ";" if first.count(";") > first.count(",") else ","
    has_header = any(ch.isalpha() for ch in first.replace("e+", "").replace("E+", ""))
    df = pd.read_csv(path, sep=sep, header=0 if has_header else None)
    return normalize_ohlcv(df)


def load_json_candles(data: Any) -> pd.DataFrame:
    """Acepta: lista de dicts, lista de listas (klines), dict columnar o {'candles': [...]}."""
    if isinstance(data, (str, Path)):
        with open(data, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    if isinstance(data, dict):
        for key in ("candles", "ohlcv", "data", "klines"):
            if key in data:
                data = data[key]
                break
    if isinstance(data, dict):
        df = pd.DataFrame(data)
    elif isinstance(data, list) and data and isinstance(data[0], (list, tuple)):
        df = pd.DataFrame(data)
        df.columns = _BINANCE_KLINE_COLS[: df.shape[1]] if df.shape[1] > 6 else \
            ["time", "open", "high", "low", "close", "volume"][: df.shape[1]]
    else:
        df = pd.DataFrame(data)
    return normalize_ohlcv(df)


# ════════════════════════════════════════════════════════════════════════════
# CAPA 1 — POSTMORTEM CIRCUIT BREAKER
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class AccountState:
    balance: float
    consecutive_losses: int = 0
    daily_drawdown_pct: float = 0.0
    cooldown_candles_remaining: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "AccountState":
        return cls(
            balance=float(d["balance"]),
            consecutive_losses=int(d.get("consecutive_losses", 0)),
            daily_drawdown_pct=float(d.get("daily_drawdown_pct", 0.0)),
            cooldown_candles_remaining=int(d.get("cooldown_candles_remaining", 0)),
        )


def circuit_breaker(state: AccountState, p: Params) -> dict:
    reasons: list[str] = []
    if state.balance <= 0:
        return {"status": "HALTED", "risk_throttle": False, "risk_pct": 0.0,
                "cooldown_candles_remaining": p.cooldown_candles,
                "consecutive_losses": state.consecutive_losses,
                "daily_drawdown_pct": state.daily_drawdown_pct,
                "reasons": ["balance <= 0"], "abort_analysis": True}

    if state.cooldown_candles_remaining > 0:
        reasons.append(f"enfriamiento en curso: quedan {state.cooldown_candles_remaining} velas")
        status, cooldown = "HALTED", state.cooldown_candles_remaining
    else:
        if state.consecutive_losses >= p.halt_losses:
            reasons.append(f"consecutive_losses={state.consecutive_losses} >= {p.halt_losses}")
        if state.daily_drawdown_pct >= p.halt_dd_pct:
            reasons.append(f"daily_drawdown_pct={state.daily_drawdown_pct:.2f}% >= {p.halt_dd_pct}%")
        if reasons:
            status, cooldown = "HALTED", p.cooldown_candles
        else:
            if state.consecutive_losses >= p.throttle_losses:
                reasons.append(f"consecutive_losses={state.consecutive_losses} >= {p.throttle_losses}")
            if state.daily_drawdown_pct >= p.throttle_dd_pct:
                reasons.append(f"daily_drawdown_pct={state.daily_drawdown_pct:.2f}% >= {p.throttle_dd_pct}%")
            status = "THROTTLED" if reasons else "ACTIVE"
            cooldown = 0
            if not reasons:
                reasons.append("sin rachas ni drawdown relevantes")

    risk_pct = {"ACTIVE": p.risk_base_pct, "THROTTLED": p.risk_throttled_pct, "HALTED": 0.0}[status]
    return {
        "status": status,
        "risk_throttle": status == "THROTTLED",
        "risk_pct": risk_pct,
        "cooldown_candles_remaining": cooldown,
        "consecutive_losses": state.consecutive_losses,
        "daily_drawdown_pct": round(state.daily_drawdown_pct, 4),
        "reasons": reasons,
        "abort_analysis": status == "HALTED",
    }


def postmortem_update(state: dict, trade_pnl_usd: float, equity_after: float,
                      day_peak_equity: float, p: Params | None = None) -> dict:
    """Auditoría tras cerrar un trade: actualiza racha, drawdown diario y enfriamiento."""
    p = p or Params()
    s = dict(state)
    if trade_pnl_usd < 0:
        s["consecutive_losses"] = int(s.get("consecutive_losses", 0)) + 1
    elif trade_pnl_usd > 0:
        s["consecutive_losses"] = 0
    peak = max(day_peak_equity, equity_after)
    s["day_peak_equity"] = peak
    s["balance"] = equity_after
    s["daily_drawdown_pct"] = round((peak - equity_after) / peak * 100.0, 4) if peak > 0 else 0.0
    if s["consecutive_losses"] >= p.halt_losses or s["daily_drawdown_pct"] >= p.halt_dd_pct:
        s["cooldown_candles_remaining"] = p.cooldown_candles
    return s


def tick_cooldown(state: dict, p: Params | None = None) -> dict:
    """Llamar una vez por cada vela cerrada. Al terminar el enfriamiento se reinicia la racha."""
    p = p or Params()
    s = dict(state)
    remaining = int(s.get("cooldown_candles_remaining", 0))
    if remaining > 0:
        remaining -= 1
        s["cooldown_candles_remaining"] = remaining
        if remaining == 0:
            s["consecutive_losses"] = 0
            if float(s.get("daily_drawdown_pct", 0.0)) >= p.halt_dd_pct:
                s["cooldown_candles_remaining"] = p.cooldown_candles  # sigue bloqueado hasta reset diario
    return s


# ════════════════════════════════════════════════════════════════════════════
# CAPA 2 — MARKET REGIME QUANT
# ════════════════════════════════════════════════════════════════════════════
def market_regime(ind: dict, p: Params) -> dict:
    close, e20, e50, e200 = ind["close"], ind["ema20"], ind["ema50"], ind["ema200"]
    slope, r, a = ind["ema20_slope_pct"], ind["rsi14"], ind["atr14"]
    for k in ("close", "ema20", "ema50", "ema200", "ema20_slope_pct", "rsi14", "atr14"):
        if ind[k] is None or (isinstance(ind[k], float) and math.isnan(ind[k])):
            raise ValueError(f"Indicador '{k}' no disponible (historial insuficiente).")
    if a <= 0:
        raise ValueError("ATR14 <= 0: datos sin volatilidad, no se puede evaluar.")

    # Las métricas derivadas se redondean a 8 decimales antes de comparar, para que el ruido
    # de coma flotante (p. ej. 3.2000000000000028) no altere los límites exactos de la spec.
    extension_atr = round((close - e20) / a, 8)
    slope = round(slope, 8)

    long_checks = {
        "macro_close_gt_ema200": close > e200,
        "structure_ema20_gt_ema50": e20 > e50,
        "slope_ema20_gt_+%.2f%%" % p.slope_min_pct: slope > p.slope_min_pct,
        "rsi_in_%g_%g" % (p.rsi_long_min, p.rsi_long_max): p.rsi_long_min <= r <= p.rsi_long_max,
        "extension_le_%gATR" % p.max_extension_atr: abs(extension_atr) <= p.max_extension_atr,
    }
    short_checks = {
        "macro_close_lt_ema200": close < e200,
        "structure_ema20_lt_ema50": e20 < e50,
        "slope_ema20_lt_-%.2f%%" % p.slope_min_pct: slope < -p.slope_min_pct,
        "rsi_in_%g_%g" % (p.rsi_short_min, p.rsi_short_max): p.rsi_short_min <= r <= p.rsi_short_max,
        "extension_le_%gATR" % p.max_extension_atr: abs(extension_atr) <= p.max_extension_atr,
        "short_enabled": p.allow_short,
    }

    bull_structure = close > e200 and e20 > e50 and slope > p.slope_min_pct
    bear_structure = close < e200 and e20 < e50 and slope < -p.slope_min_pct

    if abs(extension_atr) > p.max_extension_atr:
        regime = "EXHAUSTION_CLIMAX"
    elif bull_structure:
        regime = "BULL_EXPANSION"
    elif bear_structure:
        regime = "BEAR_DISTRIBUTION"
    else:
        regime = "CHOP_RANGE"

    if regime == "BULL_EXPANSION" and all(long_checks.values()):
        signal, blocking = "LONG", []
    elif regime == "BEAR_DISTRIBUTION" and all(short_checks.values()):
        signal, blocking = "SHORT", []
    else:
        signal = "WAIT / NO_TRADE"
        if regime == "EXHAUSTION_CLIMAX":
            blocking = [f"EXHAUSTION_CLIMAX: |Close-EMA20| = {abs(extension_atr):.2f} ATR > {p.max_extension_atr} ATR (sin FOMO)"]
        elif regime == "BULL_EXPANSION":
            blocking = ["LONG." + k for k, v in long_checks.items() if not v]
        elif regime == "BEAR_DISTRIBUTION":
            blocking = ["SHORT." + k for k, v in short_checks.items() if not v]
        else:
            blocking = ["CHOP_RANGE: estructura sin tendencia"] + \
                       ["LONG." + k for k, v in long_checks.items() if not v]
            if p.allow_short:
                blocking += ["SHORT." + k for k, v in short_checks.items() if not v and k != "short_enabled"]

    return {
        "evaluated": True,
        "regime": regime,
        "signal": signal,
        "indicators": {
            "close": round(close, 8), "ema20": round(e20, 8), "ema50": round(e50, 8),
            "ema200": round(e200, 8), "ema20_slope_3c_pct": round(slope, 4),
            "rsi14": round(r, 4), "atr14": round(a, 8), "extension_atr": round(extension_atr, 4),
        },
        "long_checks": long_checks,
        "short_checks": short_checks,
        "blocking_filters": blocking,
    }


# ════════════════════════════════════════════════════════════════════════════
# CAPA 3 — ATR RISK GUARDIAN
# ════════════════════════════════════════════════════════════════════════════
def atr_risk_guardian(side: str, entry: float, atr_value: float, balance: float,
                      risk_pct: float, p: Params) -> dict:
    if side not in ("LONG", "SHORT"):
        raise ValueError("side debe ser LONG o SHORT")
    if entry <= 0 or atr_value <= 0 or balance <= 0 or risk_pct <= 0:
        raise ValueError("entry, atr, balance y risk_pct deben ser > 0")

    d = 1 if side == "LONG" else -1
    sl_raw = entry - d * p.sl_atr_mult * atr_value
    tp_raw = entry + d * p.tp_atr_mult * atr_value
    be_raw = entry + d * p.be_atr_mult * atr_value
    be_sl_raw = entry * (1 + d * p.be_fee_offset_pct / 100.0)
    if sl_raw <= 0 or tp_raw <= 0:
        raise ValueError("SL/TP fuera de rango (<= 0): ATR demasiado grande para este precio.")

    # Redondeo al tick. El SL se redondea "hacia afuera" (nunca más ceñido que la fórmula),
    # el TP "hacia adentro" (nunca más ambicioso que la fórmula).
    if side == "LONG":
        sl = floor_to_step(sl_raw, p.price_tick)
        tp = floor_to_step(tp_raw, p.price_tick)
        be = ceil_to_step(be_raw, p.price_tick)
        be_sl = ceil_to_step(be_sl_raw, p.price_tick)
    else:
        sl = ceil_to_step(sl_raw, p.price_tick)
        tp = ceil_to_step(tp_raw, p.price_tick)
        be = floor_to_step(be_raw, p.price_tick)
        be_sl = floor_to_step(be_sl_raw, p.price_tick)

    sl_distance_pct = round(abs(entry - sl) / entry, 12)         # en fracción
    risk_usd_target = balance * risk_pct / 100.0
    size_by_risk = risk_usd_target / sl_distance_pct
    size_cap = balance * p.max_allocation_pct / 100.0
    capped = size_by_risk > size_cap
    size_usd = floor_to_step(min(size_cap, size_by_risk), 0.01)
    qty = floor_to_step(size_usd / entry, p.qty_step)
    notional = qty * entry
    risk_usd = qty * abs(entry - sl)
    reward_usd = qty * abs(tp - entry)

    return {
        "evaluated": True,
        "side": side,
        "entry_price": round(entry, 8),
        "atr14": round(atr_value, 8),
        "stop_loss": sl,
        "take_profit": tp,
        "break_even_trigger": be,
        "break_even_new_sl": be_sl,
        "sl_distance_pct": round(sl_distance_pct * 100, 4),
        "risk_pct_target": risk_pct,
        "risk_usd_target": round(risk_usd_target, 2),
        "position_size_by_risk_usd": round(size_by_risk, 2),
        "position_cap_usd": round(size_cap, 2),
        "capped_by_max_allocation": bool(capped),
        "position_size_usd": round(notional, 2),
        "quantity": qty,
        "risk_usd_effective": round(risk_usd, 2),
        "risk_pct_effective": round(risk_usd / balance * 100, 4),
        "reward_usd": round(reward_usd, 2),
        "reward_risk_ratio": round(abs(tp - entry) / abs(entry - sl), 4),
        "below_min_notional": bool(notional < p.min_notional_usd),
    }


# ════════════════════════════════════════════════════════════════════════════
# ORQUESTADOR
# ════════════════════════════════════════════════════════════════════════════
def _last_indicators(df: pd.DataFrame, p: Params) -> tuple[dict, list[str], Any]:
    warnings: list[str] = []
    if len(df) < p.min_candles:
        raise ValueError(f"Se necesitan al menos {p.min_candles} velas (hay {len(df)}).")
    if len(df) < p.recommended_candles:
        warnings.append(f"Solo {len(df)} velas: la EMA{p.ema_slow} converge mejor con >= {p.recommended_candles}.")
    ind_df = compute_indicators(df, p)
    last = ind_df.iloc[-1]
    ind = {k: float(last[k]) for k in ("close", "ema20", "ema50", "ema200", "rsi14", "atr14", "ema20_slope_pct")}
    candle_time = last["time"].isoformat() if "time" in ind_df.columns and pd.notna(last["time"]) else None
    return ind, warnings, candle_time


def _manual_indicators(m: dict, p: Params) -> dict:
    ind = {k: float(m[k]) for k in ("close", "ema20", "ema50", "ema200", "rsi14", "atr14")}
    if "ema20_slope_pct" in m:
        ind["ema20_slope_pct"] = float(m["ema20_slope_pct"])
    elif "ema20_prev3" in m:
        ind["ema20_slope_pct"] = (ind["ema20"] / float(m["ema20_prev3"]) - 1.0) * 100.0
    else:
        raise ValueError("Modo manual: falta 'ema20_prev3' o 'ema20_slope_pct'.")
    return ind


def run_engine(account: dict, candles: pd.DataFrame | None = None, manual: dict | None = None,
               symbol: str = "UNKNOWN", timeframe: str = "UNKNOWN",
               entry_price: float | None = None, params: Params | None = None) -> dict:
    p = params or Params()
    state = AccountState.from_dict(account)
    result: dict[str, Any] = {
        "engine": ENGINE_NAME,
        "version": ENGINE_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "symbol": symbol,
        "timeframe": timeframe,
        "candle_time": None,
        "input_mode": "manual" if manual is not None else "ohlcv",
        "decision": None,
        "warnings": [],
    }

    # CAPA 1 — siempre primero; si HALTED se aborta sin mirar el gráfico.
    cb = circuit_breaker(state, p)
    result["layer_1_circuit_breaker"] = cb
    if cb["abort_analysis"]:
        result["decision"] = "HALTED"
        result["layer_2_market_regime"] = {"evaluated": False, "reason": "abortado por Circuit Breaker"}
        result["layer_3_atr_risk_guardian"] = {"evaluated": False, "reason": "abortado por Circuit Breaker"}
        result["order"] = None
        return _clean(result)

    # CAPA 2
    if manual is not None:
        ind = _manual_indicators(manual, p)
    elif candles is not None:
        ind, warns, result["candle_time"] = _last_indicators(candles, p)
        result["warnings"].extend(warns)
    else:
        raise ValueError("Se requiere `candles` (OHLCV) o `manual` (indicadores).")
    mr = market_regime(ind, p)
    result["layer_2_market_regime"] = mr
    if mr["signal"] not in ("LONG", "SHORT"):
        result["decision"] = "WAIT / NO_TRADE"
        result["layer_3_atr_risk_guardian"] = {"evaluated": False, "reason": "sin señal de entrada"}
        result["order"] = None
        return _clean(result)

    # CAPA 3
    entry = float(entry_price) if entry_price is not None else ind["close"]
    risk = atr_risk_guardian(mr["signal"], entry, ind["atr14"], state.balance, cb["risk_pct"], p)
    result["layer_3_atr_risk_guardian"] = risk
    if risk["below_min_notional"]:
        result["decision"] = "WAIT / NO_TRADE"
        result["warnings"].append(f"Tamaño {risk['position_size_usd']} USD < mínimo {p.min_notional_usd} USD.")
        result["order"] = None
        return _clean(result)

    result["decision"] = mr["signal"]
    result["order"] = {
        "symbol": symbol,
        "side": "buy" if mr["signal"] == "LONG" else "sell",
        "type": "market",
        "amount": risk["quantity"],
        "notional_usd": risk["position_size_usd"],
        "reference_price": risk["entry_price"],
        "stop_loss": {"type": "stop_market", "price": risk["stop_loss"]},
        "take_profit": {"type": "limit", "price": risk["take_profit"]},
        "break_even": {"trigger_price": risk["break_even_trigger"], "new_stop_loss": risk["break_even_new_sl"]},
        "risk_usd": risk["risk_usd_effective"],
        "risk_mode": cb["status"],
        "client_tag": f"qce-{mr['regime'].lower()}",
    }
    return _clean(result)


# ════════════════════════════════════════════════════════════════════════════
# SALIDA: TABLA MARKDOWN + JSON
# ════════════════════════════════════════════════════════════════════════════
def markdown_summary(r: dict) -> str:
    cb = r["layer_1_circuit_breaker"]
    mr = r["layer_2_market_regime"]
    rk = r["layer_3_atr_risk_guardian"]
    rows = [
        ("Símbolo / TF", f"{r['symbol']} / {r['timeframe']}"),
        ("DECISIÓN", f"**{r['decision']}**"),
        ("Capa 1 · Circuit Breaker", f"{cb['status']} (riesgo {cb['risk_pct']}%) — {'; '.join(cb['reasons'])}"),
    ]
    if mr.get("evaluated"):
        i = mr["indicators"]
        rows += [
            ("Capa 2 · Régimen", f"{mr['regime']} → {mr['signal']}"),
            ("Close / EMA20 / EMA50 / EMA200", f"{i['close']:.2f} / {i['ema20']:.2f} / {i['ema50']:.2f} / {i['ema200']:.2f}"),
            ("Pendiente EMA20 (3v) / RSI14", f"{i['ema20_slope_3c_pct']:+.3f}% / {i['rsi14']:.2f}"),
            ("ATR14 / Extensión", f"{i['atr14']:.2f} / {i['extension_atr']:+.2f} ATR"),
        ]
        if mr["blocking_filters"]:
            rows.append(("Filtros que bloquean", "; ".join(mr["blocking_filters"])))
    else:
        rows.append(("Capa 2 · Régimen", "no evaluada (" + mr["reason"] + ")"))
    if rk.get("evaluated"):
        rows += [
            ("Capa 3 · Entrada / SL / TP", f"{rk['entry_price']:.2f} / {rk['stop_loss']:.2f} / {rk['take_profit']:.2f}"),
            ("Break-Even (trigger → nuevo SL)", f"{rk['break_even_trigger']:.2f} → {rk['break_even_new_sl']:.2f}"),
            ("Tamaño / Cantidad", f"{rk['position_size_usd']:.2f} USD / {rk['quantity']}"
                                  + (" (tope 85%)" if rk["capped_by_max_allocation"] else "")),
            ("Riesgo efectivo / R:R", f"{rk['risk_usd_effective']:.2f} USD ({rk['risk_pct_effective']:.3f}%) / {rk['reward_risk_ratio']:.2f}"),
        ]
    else:
        rows.append(("Capa 3 · Riesgo", "no evaluada (" + rk["reason"] + ")"))
    lines = ["| Campo | Valor |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(lines)


def render(r: dict, json_only: bool = False) -> str:
    js = json.dumps(r, ensure_ascii=False, indent=2, allow_nan=False)
    return js if json_only else f"{markdown_summary(r)}\n\n```json\n{js}\n```"


# ════════════════════════════════════════════════════════════════════════════
# DEMO: 3 escenarios sintéticos reproducibles (sin internet)
# ════════════════════════════════════════════════════════════════════════════
def _synthetic(closes: np.ndarray, seed: int, wick: float = 0.0015) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    hi = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, wick, len(closes))))
    lo = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, wick, len(closes))))
    t = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    vol = rng.uniform(50, 150, len(closes))
    return pd.DataFrame({"time": t, "open": opens, "high": hi, "low": lo, "close": closes, "volume": vol})


def demo_bull_trend(n: int = 500, seed: int = 155) -> pd.DataFrame:
    """Tendencia alcista: paseo aleatorio con deriva positiva (+0.08%/vela, vol 0.6%)."""
    rng = np.random.default_rng(seed)
    closes = 60000 * np.exp(np.cumsum(rng.normal(0.0008, 0.006, n)))
    return _synthetic(closes, seed)


def demo_chop_range(n: int = 500, seed: int = 11) -> pd.DataFrame:
    """Mercado lateral: oscilación alrededor de un nivel fijo sin deriva."""
    rng = np.random.default_rng(seed)
    x = np.arange(n)
    closes = 60000 * (1 + 0.012 * np.sin(x / 9.0) + rng.normal(0, 0.002, n))
    return _synthetic(closes, seed)


def run_demo(json_only: bool = False) -> list[dict]:
    p = Params()
    scenarios = [
        ("ESCENARIO 1 — Long válido (cuenta sana, tendencia alcista)",
         dict(account={"balance": 10000, "consecutive_losses": 0, "daily_drawdown_pct": 0.0},
              candles=demo_bull_trend(), symbol="BTC/USDT", timeframe="1h")),
        ("ESCENARIO 2 — Mercado lateral bloqueado por régimen",
         dict(account={"balance": 10000, "consecutive_losses": 0, "daily_drawdown_pct": 0.0},
              candles=demo_chop_range(), symbol="BTC/USDT", timeframe="1h")),
        ("ESCENARIO 3 — Cuenta bloqueada por Circuit Breaker (3 pérdidas seguidas, DD 3.4%)",
         dict(account={"balance": 9660, "consecutive_losses": 3, "daily_drawdown_pct": 3.4},
              candles=demo_bull_trend(), symbol="BTC/USDT", timeframe="1h")),
    ]
    results = []
    for title, kwargs in scenarios:
        r = run_engine(params=p, **kwargs)
        results.append(r)
        print(f"\n## {title}\n")
        print(render(r, json_only))
    return results


# ════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Motor cuantitativo de 3 capas (offline).")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--csv", help="Archivo CSV OHLCV (TradingView/Binance)")
    src.add_argument("--json", help="Archivo JSON con velas")
    src.add_argument("--manual", help="Archivo JSON (o cadena JSON) con indicadores manuales")
    src.add_argument("--demo", action="store_true", help="Ejecuta los 3 escenarios de verificación")
    ap.add_argument("--state", help="account_state.json (balance, consecutive_losses, daily_drawdown_pct, cooldown_candles_remaining)")
    ap.add_argument("--balance", type=float)
    ap.add_argument("--losses", type=int, help="consecutive_losses")
    ap.add_argument("--dd", type=float, help="daily_drawdown_pct")
    ap.add_argument("--cooldown", type=int, help="cooldown_candles_remaining")
    ap.add_argument("--symbol", default="UNKNOWN")
    ap.add_argument("--timeframe", default="UNKNOWN")
    ap.add_argument("--entry", type=float, help="Precio de entrada (por defecto: último cierre)")
    ap.add_argument("--drop-last", action="store_true", help="Descarta la última vela (si aún no cerró)")
    ap.add_argument("--no-short", action="store_true", help="Desactiva SHORT (cuentas spot)")
    ap.add_argument("--params", help="JSON con parámetros a sobrescribir (ver clase Params)")
    ap.add_argument("--json-only", action="store_true", help="Imprime solo el JSON")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.demo:
        run_demo(args.json_only)
        return 0

    p = Params()
    if args.params:
        overrides = json.loads(Path(args.params).read_text(encoding="utf-8")) if Path(args.params).exists() \
            else json.loads(args.params)
        for k, v in overrides.items():
            if not hasattr(p, k):
                raise SystemExit(f"Parámetro desconocido: {k}")
            setattr(p, k, type(getattr(p, k))(v))
    if args.no_short:
        p.allow_short = False

    account: dict[str, Any] = {}
    if args.state:
        account.update(json.loads(Path(args.state).read_text(encoding="utf-8")))
    for key, val in (("balance", args.balance), ("consecutive_losses", args.losses),
                     ("daily_drawdown_pct", args.dd), ("cooldown_candles_remaining", args.cooldown)):
        if val is not None:
            account[key] = val
    if "balance" not in account:
        raise SystemExit("Falta el balance: usa --balance o --state.")

    candles = manual = None
    if args.csv:
        candles = load_csv(args.csv)
    elif args.json:
        candles = load_json_candles(args.json)
    elif args.manual:
        manual = json.loads(Path(args.manual).read_text(encoding="utf-8")) if Path(args.manual).exists() \
            else json.loads(args.manual)
    else:
        raise SystemExit("Indica una fuente: --csv, --json, --manual o --demo.")
    if candles is not None and args.drop_last:
        candles = candles.iloc[:-1].reset_index(drop=True)

    try:
        r = run_engine(account, candles=candles, manual=manual, symbol=args.symbol,
                       timeframe=args.timeframe, entry_price=args.entry, params=p)
    except ValueError as exc:
        err = {"engine": ENGINE_NAME, "version": ENGINE_VERSION, "decision": "ERROR", "error": str(exc), "order": None}
        print(json.dumps(err, ensure_ascii=False, indent=2))
        return 2
    print(render(r, args.json_only))
    return 0


if __name__ == "__main__":
    sys.exit(main())
