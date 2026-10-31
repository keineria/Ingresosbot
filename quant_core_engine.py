#!/usr/bin/env python3
"""
quant_core_engine.py — Núcleo cuantitativo del bot (3 Skills en un solo motor).

Cadena de ejecución obligatoria (en este orden):
  CAPA 1  postmortem-circuit-breaker  -> estado de la cuenta (ACTIVE / THROTTLED / HALTED)
  CAPA 2  market-regime-quant         -> régimen de mercado y señal (LONG / SHORT / WAIT)
  CAPA 3  atr-risk-guardian           -> SL, TP, Break-Even y tamaño de posición

Perfiles (Params.strategy):
  "official"  reglas oficiales de SKILL_1..3 (por defecto, sin cambios).
  "trend"     Trend Rider (SKILL_4): misma Capa 1; la Capa 2 entra por ruptura de Donchian sobre la EMA200
              y la Capa 3 sale con un trailing stop por ATR en vez de TP fijo y Break-Even.

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
  python quant_core_engine.py --fetch BTCUSDT --interval 1h --db data/trading_state.db   # velas públicas + estado SQLite
  python quant_core_engine.py --demo          # ejecuta los 3 escenarios de verificación

Salida: tabla Markdown (resumen ejecutivo) + bloque JSON estricto para n8n / CCXT / Alpaca.
Con --json-only solo se imprime el JSON (para webhooks).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ENGINE_NAME = "quant_core_engine"
ENGINE_VERSION = "1.3.0"
STRATEGIES = ("official", "trend")
TREND_ENTRIES = ("breakout", "pullback", "both", "daily_filter")


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
    max_allocation_pct: float = 85.0   # tope del capital por posición (y del total comprometido entre pares)
    max_position_pct: float = 0.0      # >0: tope MÁS BAJO por posición, para tener varias monedas a la vez
    max_leverage: float = 1.0          # 1.0 = Spot. En Futuros el tope escala a 85% × max_leverage
    price_tick: float = 0.01           # redondeo de precios ("al centavo")
    qty_step: float = 0.000001         # paso mínimo de cantidad del exchange
    min_notional_usd: float = 10.0     # por debajo de esto no se envía orden

    # --- Skill 3: postmortem-circuit-breaker ---
    throttle_losses: int = 2
    throttle_dd_pct: float = 2.0
    halt_losses: int = 3
    halt_dd_pct: float = 3.0
    cooldown_candles: int = 15

    # --- Perfil paralelo "trend" (Trend Rider, SKILL_4). Con strategy="official" no se usa nada de esto ---
    strategy: str = "official"         # "official" | "trend"
    trend_entry: str = "breakout"      # "breakout" | "pullback" (la entrada oficial) | "both"
    donchian_len: int = 20             # ruptura: Close > máximo de las 20 velas anteriores
    trend_sl_atr_mult: float = 2.5     # SL inicial = Entry - 2.5 ATR (también fija el tamaño)
    trail_atr_mult: float = 4.0        # trailing: SL = máximo desde la entrada - 4 ATR; solo sube
    trend_allow_short: bool = False    # True solo en Futuros: ruptura bajista bajo la EMA200
    daily_sma_len: int = 0             # >0: filtro diario; LONG solo si el cierre diario de AYER > SMA de N días
    trend_exit_on_filter: bool = False  # True: cerrar la posición cuando el filtro diario se da la vuelta
    # Filtros de acierto opcionales sobre la ruptura (0 = apagado). Solo endurecen la entrada; nunca la crean.
    trend_min_vol_ratio: float = 0.0   # volumen de la vela de señal / media de volumen de 20 velas >= X
    trend_min_atr_rank: float = 0.0    # percentil (0-1) del ATR% de la vela entre las últimas atr_rank_len >= X
    trend_max_ext_atr: float = 0.0     # (Close - EMA20) / ATR <= X: no comprar una ruptura demasiado estirada
    atr_rank_len: int = 180            # velas para el percentil de volatilidad (180 velas de 4h = 30 días)
    trend_min_breakout_atr: float = 0.0  # empuje: (Close - máximo del canal) / ATR >= X en la vela de ruptura
    trend_min_adx: float = 0.0         # empuje: ADX14 >= X (fuerza de la tendencia, sin dirección)
    # Cierres opcionales además del SL / trailing (0 o "" = apagado). Se evalúan al cierre de vela y salen a mercado.
    trend_exit_low_len: int = 0        # Close < mínimo de las N velas anteriores (salida "tortuga", p. ej. 10)
    trend_exit_ema: str = ""           # "ema20" | "ema50": Close por debajo de esa media
    trend_stale_bars: int = 0          # tras N velas, si el cierre sigue en o bajo la entrada: fuera (trade sin empuje)
    trend_tight_after_r: float = 0.0   # al ganar X veces el riesgo inicial (R), el trailing se ajusta a...
    trend_tight_trail_atr: float = 2.5  # ...este múltiplo de ATR (asegura más ganancia en las subidas largas)
    # Modo lateral opcional (rebote al centro del rango) cuando NO hay ruptura. Apagado por defecto.
    # LONG: ADX14 < range_max_adx, Close < banda de Bollinger inferior y RSI14 <= range_rsi_long_max.
    # SHORT (solo con trend_allow_short): espejo en la banda superior. Salida con SL y TP fijos en ATR, sin trailing.
    range_entry: bool = False
    range_max_adx: float = 20.0
    range_bb_len: int = 20
    range_bb_std: float = 2.0
    range_rsi_long_max: float = 35.0
    range_rsi_short_min: float = 65.0
    range_sl_atr_mult: float = 1.5
    range_tp_atr_mult: float = 1.5

    @property
    def min_candles(self) -> int:
        return max(self.ema_slow + self.slope_lookback, self.donchian_len + 1)

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


def effective_tick(price: float, tick: float) -> float:
    """Tick de redondeo para este precio: `tick`, o uno 10, 100... veces más fino si `tick` supera el 0,01%
    del precio. Con 0.01 fijo, DOGE (0,07 USD) redondeaba el TP de un corto a 0,08: por encima de la entrada."""
    t = _dec(tick)
    limit = _dec(price) / Decimal(10000)
    while t > limit and t > Decimal("1e-12"):
        t /= 10
    return float(t)


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


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """ADX de Wilder (RMA), como TradingView. 0 mientras no hay historia suficiente."""
    h, l, c = (s.to_numpy(float) for s in (high, low, close))
    n = len(c)
    if n < 2 * length + 1:
        return pd.Series(np.zeros(n), index=close.index)
    up, down = np.diff(h, prepend=h[0]), -np.diff(l, prepend=l[0])
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    prev_c = np.roll(c, 1)
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    tr[0] = plus_dm[0] = minus_dm[0] = 0.0
    atr_ = rma(tr, length, start=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * rma(plus_dm, length, start=1) / atr_
        mdi = 100 * rma(minus_dm, length, start=1) / atr_
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    dx = np.nan_to_num(dx, nan=0.0)
    out = rma(dx, length, start=length)
    return pd.Series(np.nan_to_num(out, nan=0.0), index=close.index)


def compute_indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = ema(out["close"], p.ema_fast)
    out["ema50"] = ema(out["close"], p.ema_mid)
    out["ema200"] = ema(out["close"], p.ema_slow)
    out["rsi14"] = rsi(out["close"], p.rsi_len)
    out["atr14"] = atr(out["high"], out["low"], out["close"], p.atr_len)
    out["ema20_slope_pct"] = (out["ema20"] / out["ema20"].shift(p.slope_lookback) - 1.0) * 100.0
    # Canal de Donchian de las velas ANTERIORES (sin la actual): romperlo es cerrar por encima / debajo.
    out["donchian_high"] = out["high"].rolling(p.donchian_len).max().shift(1)
    out["donchian_low"] = out["low"].rolling(p.donchian_len).min().shift(1)
    # Filtro diario: +1 si el cierre diario del día ANTERIOR (ya cerrado) está sobre su SMA de N días,
    # -1 si está debajo, 0 si el filtro está apagado o aún no hay N días de historia.
    out["daily_trend"] = 0.0
    if p.daily_sma_len > 0 and "time" in out.columns:
        day = pd.to_datetime(out["time"], utc=True).dt.floor("D")
        daily_close = out["close"].groupby(day.values).last()
        sma = daily_close.rolling(p.daily_sma_len).mean()
        sign = np.sign(daily_close - sma).shift(1).fillna(0.0)
        out["daily_trend"] = day.map(pd.Series(sign.values, index=pd.to_datetime(sign.index, utc=True))).fillna(0.0).values
    # Variables de los filtros de acierto. Sin historia suficiente valen 0: un filtro encendido no deja entrar.
    vol_mean = out["volume"].rolling(20).mean()
    out["vol_ratio"] = (out["volume"] / vol_mean).where(vol_mean > 0).fillna(0.0)
    atr_pct = out["atr14"] / out["close"]
    out["atr_rank"] = atr_pct.rolling(p.atr_rank_len, min_periods=max(2, p.atr_rank_len // 3)).rank(pct=True).fillna(0.0)
    out["ext_atr"] = ((out["close"] - out["ema20"]) / out["atr14"]).where(out["atr14"] > 0).fillna(0.0)
    out["breakout_atr"] = ((out["close"] - out["donchian_high"]) / out["atr14"]).where(out["atr14"] > 0).fillna(0.0)
    out["adx14"] = adx(out["high"], out["low"], out["close"], p.atr_len)
    # Bandas de Bollinger del modo lateral (SMA ± k desviaciones de la vela actual).
    mid = out["close"].rolling(p.range_bb_len).mean()
    sd = out["close"].rolling(p.range_bb_len).std(ddof=0)
    out["bb_mid"], out["bb_lower"], out["bb_upper"] = mid, mid - p.range_bb_std * sd, mid + p.range_bb_std * sd
    # Canal de salida (velas ANTERIORES). 0 = apagado o sin historia: no dispara.
    n_exit = max(1, p.trend_exit_low_len)
    out["exit_low"] = out["low"].rolling(n_exit).min().shift(1).fillna(0.0) if p.trend_exit_low_len else 0.0
    out["exit_high"] = out["high"].rolling(n_exit).max().shift(1).fillna(0.0) if p.trend_exit_low_len else 0.0
    return out


INDICATOR_KEYS = ("close", "ema20", "ema50", "ema200", "rsi14", "atr14", "ema20_slope_pct",
                  "donchian_high", "donchian_low", "daily_trend", "vol_ratio", "atr_rank", "ext_atr",
                  "breakout_atr", "adx14", "exit_low", "exit_high", "bb_mid", "bb_lower", "bb_upper")


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
    manual_halt: bool = False          # kill switch manual (panel web / API)
    # Filtro externo opcional (risk_gate.py: sentimiento, calendario macro, alertas). Solo endurece la Capa 1:
    # "halt" bloquea entradas nuevas mientras dure (sin enfriamiento de 15 velas), "throttle" baja el riesgo.
    external_risk: str = "none"        # "none" | "throttle" | "halt"
    external_reasons: tuple = ()

    @classmethod
    def from_dict(cls, d: dict) -> "AccountState":
        return cls(
            balance=float(d["balance"]),
            consecutive_losses=int(d.get("consecutive_losses", 0)),
            daily_drawdown_pct=float(d.get("daily_drawdown_pct", 0.0)),
            cooldown_candles_remaining=int(d.get("cooldown_candles_remaining", 0)),
            manual_halt=bool(d.get("manual_halt", False)),
            external_risk=str((d.get("external_risk") or {}).get("level", "none")),
            external_reasons=tuple((d.get("external_risk") or {}).get("reasons", ())),
        )


def circuit_breaker(state: AccountState, p: Params) -> dict:
    reasons: list[str] = []
    if state.balance <= 0:
        return {"status": "HALTED", "risk_throttle": False, "risk_pct": 0.0,
                "cooldown_candles_remaining": p.cooldown_candles,
                "consecutive_losses": state.consecutive_losses,
                "daily_drawdown_pct": state.daily_drawdown_pct,
                "reasons": ["balance <= 0"], "abort_analysis": True}

    if state.manual_halt:
        reasons.append("kill switch manual activado")
        status, cooldown = "HALTED", state.cooldown_candles_remaining
    elif state.cooldown_candles_remaining > 0:
        reasons.append(f"enfriamiento en curso: quedan {state.cooldown_candles_remaining} velas")
        status, cooldown = "HALTED", state.cooldown_candles_remaining
    elif state.external_risk == "halt" and not (state.consecutive_losses >= p.halt_losses
                                                 or state.daily_drawdown_pct >= p.halt_dd_pct):
        reasons.extend(f"filtro externo: {r}" for r in state.external_reasons or ("pausa",))
        status, cooldown = "HALTED", 0
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
            if state.external_risk == "throttle":
                reasons.extend(f"filtro externo: {r}" for r in state.external_reasons or ("riesgo reducido",))
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
        "manual_halt": state.manual_halt,
        "external_risk": state.external_risk,
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


def trend_regime(ind: dict, p: Params) -> dict:
    """Capa 2 del perfil "trend" (SKILL_4): el mismo diagnóstico de régimen, otra regla de entrada.

    breakout: Close > EMA200 y Close > máximo de las `donchian_len` velas anteriores (LONG);
              en Futuros (trend_allow_short) el espejo: Close < EMA200 y Close < mínimo del canal.
    pullback: la entrada oficial de market_regime() (retroceso en tendencia, RSI 46-64).
    both:     cualquiera de las dos.
    daily_filter: solo el filtro diario (comprado mientras el cierre diario de ayer > SMA de daily_sma_len días).
    Con daily_sma_len > 0 el filtro diario se exige además en cualquier modo.
    Sin filtro de extensión en la ruptura por defecto: una ruptura suele ir estirada y es justo la que se quiere.
    Filtros de acierto opcionales (trend_min_vol_ratio, trend_min_atr_rank, trend_max_ext_atr): con valor > 0 se
    exigen además en la ruptura; con 0 (por defecto) el perfil se comporta exactamente igual que sin ellos.
    """
    if p.trend_entry not in TREND_ENTRIES:
        raise ValueError(f"trend_entry debe ser uno de {TREND_ENTRIES} (recibido {p.trend_entry!r})")
    if p.trend_entry == "daily_filter" and p.daily_sma_len <= 0:
        raise ValueError("trend_entry='daily_filter' necesita daily_sma_len > 0 (p. ej. 200)")
    base = market_regime(ind, replace(p, allow_short=p.trend_allow_short))
    use_breakout = p.trend_entry in ("breakout", "both")
    use_pullback = p.trend_entry in ("pullback", "both")
    dh, dl = ind.get("donchian_high"), ind.get("donchian_low")
    if use_breakout and any(v is None or (isinstance(v, float) and math.isnan(v)) for v in (dh, dl)):
        raise ValueError("Indicador 'donchian_high/low' no disponible (historial insuficiente o falta en el modo manual).")

    close, e200, n = ind["close"], ind["ema200"], p.donchian_len
    long_checks = {"macro_close_gt_ema200": close > e200,
                   f"breakout_close_gt_max{n}": bool(use_breakout and close > dh)}
    short_checks = {"macro_close_lt_ema200": close < e200,
                    f"breakout_close_lt_min{n}": bool(use_breakout and close < dl),
                    "short_enabled": p.trend_allow_short}
    if p.trend_entry == "daily_filter":
        long_checks, short_checks = {}, {"short_enabled": p.trend_allow_short}
    # Filtros de acierto (SKILL_4, opcionales): se exigen además de la regla de entrada, en largos y cortos.
    if p.trend_min_vol_ratio > 0:
        ok = float(ind.get("vol_ratio") or 0.0) >= p.trend_min_vol_ratio
        long_checks[f"volume_ge_{p.trend_min_vol_ratio:g}x_avg20"] = ok
        short_checks[f"volume_ge_{p.trend_min_vol_ratio:g}x_avg20"] = ok
    if p.trend_min_atr_rank > 0:
        ok = float(ind.get("atr_rank") or 0.0) >= p.trend_min_atr_rank
        long_checks[f"atr_rank_ge_{p.trend_min_atr_rank:g}"] = ok
        short_checks[f"atr_rank_ge_{p.trend_min_atr_rank:g}"] = ok
    if p.trend_min_breakout_atr > 0:
        b = float(ind.get("breakout_atr") or 0.0)
        long_checks[f"breakout_push_ge_{p.trend_min_breakout_atr:g}atr"] = b >= p.trend_min_breakout_atr
        short_checks[f"breakout_push_ge_{p.trend_min_breakout_atr:g}atr"] = (
            dl is not None and ind["atr14"] > 0 and (dl - close) / ind["atr14"] >= p.trend_min_breakout_atr)
    if p.trend_min_adx > 0:
        ok = float(ind.get("adx14") or 0.0) >= p.trend_min_adx
        long_checks[f"adx_ge_{p.trend_min_adx:g}"] = ok
        short_checks[f"adx_ge_{p.trend_min_adx:g}"] = ok
    if p.trend_max_ext_atr > 0:
        ext = float(ind.get("ext_atr") or 0.0)
        long_checks[f"ext_le_{p.trend_max_ext_atr:g}atr"] = ext <= p.trend_max_ext_atr
        short_checks[f"ext_ge_-{p.trend_max_ext_atr:g}atr"] = ext >= -p.trend_max_ext_atr
    daily_long = daily_short = True
    if p.daily_sma_len > 0:
        dt = float(ind.get("daily_trend") or 0.0)
        daily_long, daily_short = dt > 0, dt < 0
        long_checks[f"daily_close_gt_sma{p.daily_sma_len}d"] = daily_long
        short_checks[f"daily_close_lt_sma{p.daily_sma_len}d"] = daily_short
    breakout_long = (use_breakout or p.trend_entry == "daily_filter") and all(long_checks.values())
    breakout_short = (use_breakout or p.trend_entry == "daily_filter") and all(short_checks.values())
    pullback_long = use_pullback and base["signal"] == "LONG" and daily_long
    pullback_short = use_pullback and base["signal"] == "SHORT" and daily_short and p.trend_allow_short

    # Modo lateral: solo si no hay ruptura ni retroceso. ADX bajo = sin tendencia; se compra el extremo del rango.
    range_long_checks: dict = {}
    range_short_checks: dict = {}
    if p.range_entry:
        adx_ok = float(ind.get("adx14") or 0.0) < p.range_max_adx and float(ind.get("adx14") or 0.0) > 0
        lo, up = ind.get("bb_lower"), ind.get("bb_upper")
        rsi_v = float(ind["rsi14"])
        range_long_checks = {f"range_adx_lt_{p.range_max_adx:g}": adx_ok,
                             "range_close_lt_bb_lower": lo is not None and not math.isnan(lo) and close < lo,
                             f"range_rsi_le_{p.range_rsi_long_max:g}": rsi_v <= p.range_rsi_long_max}
        range_short_checks = {f"range_adx_lt_{p.range_max_adx:g}": adx_ok,
                              "range_close_gt_bb_upper": up is not None and not math.isnan(up) and close > up,
                              f"range_rsi_ge_{p.range_rsi_short_min:g}": rsi_v >= p.range_rsi_short_min,
                              "short_enabled": p.trend_allow_short}
    range_long = bool(range_long_checks) and all(range_long_checks.values())
    range_short = bool(range_short_checks) and all(range_short_checks.values())

    trigger = None
    if breakout_long or pullback_long:
        signal, trigger = "LONG", ("daily_filter" if p.trend_entry == "daily_filter" else
                                   "breakout" if breakout_long else "pullback")
    elif breakout_short or pullback_short:
        signal, trigger = "SHORT", ("daily_filter" if p.trend_entry == "daily_filter" else
                                    "breakout" if breakout_short else "pullback")
    elif range_long:
        signal, trigger = "LONG", "range"
    elif range_short:
        signal, trigger = "SHORT", "range"
    else:
        signal = "WAIT / NO_TRADE"

    blocking: list[str] = []
    if signal == "WAIT / NO_TRADE":
        if use_breakout or p.trend_entry == "daily_filter":
            blocking += ["TREND.LONG." + k for k, v in long_checks.items() if not v]
            if p.trend_allow_short:
                blocking += ["TREND.SHORT." + k for k, v in short_checks.items() if not v]
        if p.range_entry:
            blocking += ["RANGE.LONG." + k for k, v in range_long_checks.items() if not v]
            if p.trend_allow_short:
                blocking += ["RANGE.SHORT." + k for k, v in range_short_checks.items() if not v]
        if use_pullback:
            blocking += ["PULLBACK." + b for b in base["blocking_filters"]]
            if base["signal"] == "LONG" and not daily_long:
                blocking.append(f"PULLBACK.daily_close_gt_sma{p.daily_sma_len}d")

    out = dict(base)
    out["indicators"] = dict(base["indicators"],
                             donchian_high=None if dh is None else round(float(dh), 8),
                             donchian_low=None if dl is None else round(float(dl), 8))
    out.update(strategy="trend", entry_mode=p.trend_entry, entry_trigger=trigger, signal=signal,
               breakout_long_checks=long_checks, breakout_short_checks=short_checks, blocking_filters=blocking)
    if p.range_entry:
        out.update(range_long_checks=range_long_checks, range_short_checks=range_short_checks)
    return out


def trend_exit_reason(ind: dict, side: str, p: Params, entry_price: float | None = None,
                      bars_held: int | None = None) -> str | None:
    """Motivo de cierre a mercado al cierre de la vela (perfil "trend"), o None si ninguna regla encendida dispara.

    SIGNAL_EXIT:       el filtro diario se dio la vuelta (trend_exit_on_filter).
    CHANNEL_EXIT:      Close rompe el mínimo (LONG) / máximo (SHORT) de las trend_exit_low_len velas anteriores.
    EMA_EXIT:          Close cruza la media trend_exit_ema en contra.
    STALE_EXIT:        lleva trend_stale_bars velas y el cierre no supera la entrada (no hubo empuje).
    """
    d = 1 if side == "LONG" else -1
    close = float(ind["close"])
    if p.trend_exit_on_filter and p.daily_sma_len > 0:
        dt = float(ind.get("daily_trend") or 0.0)
        if (dt <= 0) if side == "LONG" else (dt >= 0):
            return "SIGNAL_EXIT"
    if p.trend_exit_low_len > 0:
        level = float(ind.get("exit_low" if side == "LONG" else "exit_high") or 0.0)
        if level > 0 and (close - level) * d < 0:
            return "CHANNEL_EXIT"
    if p.trend_exit_ema:
        if p.trend_exit_ema not in ("ema20", "ema50"):
            raise ValueError("trend_exit_ema debe ser '', 'ema20' o 'ema50'")
        if (close - float(ind[p.trend_exit_ema])) * d < 0:
            return "EMA_EXIT"
    if p.trend_stale_bars > 0 and entry_price and bars_held is not None and bars_held >= p.trend_stale_bars:
        if (close - entry_price) * d <= 0:
            return "STALE_EXIT"
    return None


def trend_exit_signal(ind: dict, side: str, p: Params, entry_price: float | None = None,
                      bars_held: int | None = None) -> bool:
    """True si alguna regla de cierre encendida dispara (ver trend_exit_reason)."""
    return trend_exit_reason(ind, side, p, entry_price, bars_held) is not None


def trend_has_signal_exits(p: Params) -> bool:
    return p.strategy == "trend" and bool((p.trend_exit_on_filter and p.daily_sma_len > 0) or p.trend_exit_low_len
                                          or p.trend_exit_ema or p.trend_stale_bars)


def evaluate_layer2(ind: dict, p: Params) -> dict:
    """Capa 2 según el perfil elegido en Params.strategy."""
    if p.strategy not in STRATEGIES:
        raise ValueError(f"strategy debe ser uno de {STRATEGIES} (recibido {p.strategy!r})")
    return trend_regime(ind, p) if p.strategy == "trend" else market_regime(ind, p)


# ════════════════════════════════════════════════════════════════════════════
# CAPA 3 — ATR RISK GUARDIAN
# ════════════════════════════════════════════════════════════════════════════
def atr_risk_guardian(side: str, entry: float, atr_value: float, balance: float,
                      risk_pct: float, p: Params) -> dict:
    if side not in ("LONG", "SHORT"):
        raise ValueError("side debe ser LONG o SHORT")
    if entry <= 0 or atr_value <= 0 or balance <= 0 or risk_pct <= 0:
        raise ValueError("entry, atr, balance y risk_pct deben ser > 0")
    if p.max_leverage < 1.0:
        raise ValueError("max_leverage debe ser >= 1.0 (1.0 = Spot)")

    d = 1 if side == "LONG" else -1
    sl_raw = entry - d * p.sl_atr_mult * atr_value
    tp_raw = entry + d * p.tp_atr_mult * atr_value
    be_raw = entry + d * p.be_atr_mult * atr_value
    be_sl_raw = entry * (1 + d * p.be_fee_offset_pct / 100.0)
    if sl_raw <= 0 or tp_raw <= 0:
        raise ValueError("SL/TP fuera de rango (<= 0): ATR demasiado grande para este precio.")

    # Redondeo al tick. El SL se redondea "hacia afuera" (nunca más ceñido que la fórmula),
    # el TP "hacia adentro" (nunca más ambicioso que la fórmula).
    tick = effective_tick(entry, p.price_tick)
    if side == "LONG":
        sl = floor_to_step(sl_raw, tick)
        tp = floor_to_step(tp_raw, tick)
        be = ceil_to_step(be_raw, tick)
        be_sl = ceil_to_step(be_sl_raw, tick)
    else:
        sl = ceil_to_step(sl_raw, tick)
        tp = ceil_to_step(tp_raw, tick)
        be = floor_to_step(be_raw, tick)
        be_sl = floor_to_step(be_sl_raw, tick)
    if d * (tp - entry) <= 0 or d * (entry - sl) <= 0:
        raise ValueError(f"SL {sl} / TP {tp} no quedan a cada lado de la entrada {entry}: tick {tick} demasiado grueso.")

    sl_distance_pct = round(abs(entry - sl) / entry, 12)         # en fracción
    risk_usd_target = balance * risk_pct / 100.0
    size_by_risk = risk_usd_target / sl_distance_pct
    per_pos = min(p.max_allocation_pct, p.max_position_pct) if p.max_position_pct > 0 else p.max_allocation_pct
    size_cap = balance * per_pos / 100.0 * p.max_leverage
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
        "max_leverage": p.max_leverage,
        "margin_required_usd": round(notional / p.max_leverage, 2),
        "position_size_usd": round(notional, 2),
        "quantity": qty,
        "risk_usd_effective": round(risk_usd, 2),
        "risk_pct_effective": round(risk_usd / balance * 100, 4),
        "reward_usd": round(reward_usd, 2),
        "reward_risk_ratio": round(abs(tp - entry) / abs(entry - sl), 4),
        "below_min_notional": bool(notional < p.min_notional_usd),
    }


def trend_risk_guardian(side: str, entry: float, atr_value: float, balance: float,
                        risk_pct: float, p: Params) -> dict:
    """Capa 3 del perfil "trend": misma fórmula de tamaño (riesgo % / distancia al SL, tope 85% × apalancamiento),
    SL inicial a trend_sl_atr_mult ATR y salida por trailing stop: sin TP fijo ni Break-Even.
    El trailing lo mueve state_manager al cierre de cada vela: SL = máximo desde la entrada - trail_atr_mult ATR."""
    m = p.trend_sl_atr_mult
    # TP y Break-Even no se usan aquí; se pasan de relleno a una distancia que nunca cae a <= 0 en un SHORT
    # (en un desplome el ATR puede superar precio / m y el TP de relleno quedaría negativo).
    filler = min(m, 0.5 * entry / atr_value) if entry > 0 and atr_value > 0 else m
    r = atr_risk_guardian(side, entry, atr_value, balance, risk_pct,
                          replace(p, sl_atr_mult=m, tp_atr_mult=filler, be_atr_mult=filler))
    r.update(exit_mode="trailing_stop", take_profit=None, break_even_trigger=None, break_even_new_sl=None,
             reward_usd=None, reward_risk_ratio=None, trail_atr_mult=p.trail_atr_mult,
             trail_distance_usd=round(p.trail_atr_mult * atr_value, 8))
    return r


def range_risk_guardian(side: str, entry: float, atr_value: float, balance: float,
                        risk_pct: float, p: Params) -> dict:
    """Capa 3 del modo lateral: mismo tamaño por riesgo, SL a range_sl_atr_mult ATR y TP fijo a range_tp_atr_mult ATR
    (el centro aproximado del rango). Sin trailing; el Break-Even se coloca en el propio TP, así que no actúa."""
    r = atr_risk_guardian(side, entry, atr_value, balance, risk_pct,
                          replace(p, sl_atr_mult=p.range_sl_atr_mult, tp_atr_mult=p.range_tp_atr_mult,
                                  be_atr_mult=p.range_tp_atr_mult))
    r.update(exit_mode="range_fixed")
    return r


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
    ind = {k: float(last[k]) for k in INDICATOR_KEYS}
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
    for k in ("donchian_high", "donchian_low", "daily_trend",      # solo los necesita el perfil "trend"
              "vol_ratio", "atr_rank", "ext_atr", "breakout_atr", "adx14", "exit_low", "exit_high",
              "bb_mid", "bb_lower", "bb_upper"):
        if k in m:
            ind[k] = float(m[k])
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
        "strategy": p.strategy,
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
    mr = evaluate_layer2(ind, p)
    result["layer_2_market_regime"] = mr
    if mr["signal"] not in ("LONG", "SHORT"):
        result["decision"] = "WAIT / NO_TRADE"
        result["layer_3_atr_risk_guardian"] = {"evaluated": False, "reason": "sin señal de entrada"}
        result["order"] = None
        return _clean(result)

    # CAPA 3
    entry = float(entry_price) if entry_price is not None else ind["close"]
    is_range = p.strategy == "trend" and mr.get("entry_trigger") == "range"
    guardian = range_risk_guardian if is_range else trend_risk_guardian if p.strategy == "trend" else atr_risk_guardian
    risk = guardian(mr["signal"], entry, ind["atr14"], state.balance, cb["risk_pct"], p)
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
    if is_range:
        result["order"].update(break_even=None, client_tag="qce-trend-range")
    elif p.strategy == "trend":
        # Sin TP ni Break-Even: el ejecutor debe mover el stop cuando llegue el evento "trailing_stop".
        result["order"].update(
            take_profit=None, break_even=None, client_tag=f"qce-trend-{mr['entry_trigger']}",
            trailing_stop={"type": "chandelier_atr", "atr_mult": p.trail_atr_mult,
                           "distance_usd": risk["trail_distance_usd"],
                           "rule": "SL = máximo desde la entrada - atr_mult x ATR14, al cierre de cada vela; solo sube"})
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
            ("Perfil", "Trend Rider (SKILL_4) · entrada " + mr["entry_mode"]
                       + (f" → disparó {mr['entry_trigger']}" if mr.get("entry_trigger") else "")
                       if mr.get("strategy") == "trend" else "oficial (SKILL_1..3)"),
            ("Capa 2 · Régimen", f"{mr['regime']} → {mr['signal']}"),
            ("Close / EMA20 / EMA50 / EMA200", f"{i['close']:.2f} / {i['ema20']:.2f} / {i['ema50']:.2f} / {i['ema200']:.2f}"),
            ("Pendiente EMA20 (3v) / RSI14", f"{i['ema20_slope_3c_pct']:+.3f}% / {i['rsi14']:.2f}"),
            ("ATR14 / Extensión", f"{i['atr14']:.2f} / {i['extension_atr']:+.2f} ATR"),
        ]
        if mr["blocking_filters"]:
            rows.append(("Filtros que bloquean", "; ".join(mr["blocking_filters"])))
    else:
        rows.append(("Capa 2 · Régimen", "no evaluada (" + mr["reason"] + ")"))
    if rk.get("evaluated") and rk.get("exit_mode") == "trailing_stop":
        rows += [
            ("Capa 3 · Entrada / SL inicial", f"{rk['entry_price']:.2f} / {rk['stop_loss']:.2f}"),
            ("Salida", f"trailing stop: máximo desde la entrada − {rk['trail_atr_mult']:g} ATR "
                       f"({rk['trail_distance_usd']:.2f}); sin TP fijo ni Break-Even"),
            ("Tamaño / Cantidad", f"{rk['position_size_usd']:.2f} USD / {rk['quantity']}"
                                  + (f" (tope {rk['position_cap_usd']:.2f} USD, x{rk['max_leverage']:g})"
                                     if rk["capped_by_max_allocation"] else "")),
            ("Riesgo efectivo", f"{rk['risk_usd_effective']:.2f} USD ({rk['risk_pct_effective']:.3f}%)"),
        ]
    elif rk.get("evaluated"):
        rows += [
            ("Capa 3 · Entrada / SL / TP", f"{rk['entry_price']:.2f} / {rk['stop_loss']:.2f} / {rk['take_profit']:.2f}"),
            ("Break-Even (trigger → nuevo SL)", f"{rk['break_even_trigger']:.2f} → {rk['break_even_new_sl']:.2f}"),
            ("Tamaño / Cantidad", f"{rk['position_size_usd']:.2f} USD / {rk['quantity']}"
                                  + (f" (tope {rk['position_cap_usd']:.2f} USD, x{rk['max_leverage']:g})"
                                     if rk["capped_by_max_allocation"] else "")),
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
    src.add_argument("--fetch", metavar="SYMBOL", help="Descarga velas públicas sin API key (market_fetcher.py)")
    src.add_argument("--demo", action="store_true", help="Ejecuta los 3 escenarios de verificación")
    ap.add_argument("--interval", default="1h", help="Temporalidad para --fetch")
    ap.add_argument("--limit", type=int, default=1000, help="Velas a descargar con --fetch")
    ap.add_argument("--db", help="Lee el estado de la cuenta de SQLite (state_manager.py)")
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
    if args.db:
        from state_manager import StateManager
        sm = StateManager(args.db)
        account.update(sm.engine_account())
        sm.close()
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
    elif args.fetch:
        from market_fetcher import FetchError, fetch_candles
        try:
            candles, info = fetch_candles(args.fetch, args.interval, args.limit, verbose=True)
        except FetchError as exc:
            print(json.dumps({"engine": ENGINE_NAME, "version": ENGINE_VERSION, "decision": "ERROR",
                              "error": str(exc), "order": None}, ensure_ascii=False, indent=2))
            return 2
        print(f"[quant_core_engine] {info['candles']} velas desde {info['source']}", file=sys.stderr)
        if args.symbol == "UNKNOWN":
            args.symbol = args.fetch.upper()
        if args.timeframe == "UNKNOWN":
            args.timeframe = args.interval
    elif args.manual:
        manual = json.loads(Path(args.manual).read_text(encoding="utf-8")) if Path(args.manual).exists() \
            else json.loads(args.manual)
    else:
        raise SystemExit("Indica una fuente: --csv, --json, --manual, --fetch o --demo.")
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
