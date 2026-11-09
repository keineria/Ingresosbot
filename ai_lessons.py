"""Cuaderno de lecciones de la IA: memoria larga que solo guarda lo que se demuestra con resultados.

Cómo funciona (todo con cuentas; la IA solo redacta):
1. Al votar cada señal, el código anota sus rasgos (ADX, RSI, distancia a la EMA200, cómo va BTC...).
2. Una vez por semana, el auditor mira las últimas señales con su resultado en R y propone como mucho 2
   lecciones del tipo "evitar señales con <rasgo> <, > o = <valor>". Solo puede usar los rasgos de RASGOS.
3. Cada lección empieza "en prueba" y se mide SOLO con señales que llegan DESPUÉS de escribirla (las anteriores
   ya las vio el auditor). Evitar una señal que perdió -0.8 R ahorra +0.8 R; evitar una que ganó +2.3 R cuesta
   -2.3 R, igual que un veto en la escalera.
4. Pasa a "validada" con al menos 8 casos, ahorro total positivo y un acierto 10 puntos por encima del azar.
   Se descarta si falla esa prueba, si una validada vuelve a perder dinero, o si en 120 días no junta casos.
5. El analista solo ve las validadas. Que el analista mejore de verdad lo sigue midiendo la escalera.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

MIN_CASES = 8                 # señales nuevas que tienen que cumplir la lección antes de juzgarla
EDGE_OVER_CHANCE = 0.10       # acierto exigido sobre la tasa de perdedoras de esas mismas señales
MAX_TEST_DAYS = 120           # una lección que en 4 meses no junta casos se descarta
MAX_OPEN_PER_PROFILE = 6      # lecciones "en prueba" a la vez por estrategia
MAX_SHOWN = 6                 # lecciones validadas que ve el analista

# Rasgos que el código calcula en cada señal. El auditor solo puede escribir condiciones sobre estos.
RASGOS: dict[str, str] = {
    "adx14": "ADX de 14 velas: fuerza de la tendencia (bajo 20 = sin tendencia)",
    "rsi14": "RSI de 14 velas",
    "atr_pct": "ATR como % del precio (volatilidad)",
    "dist_ema200_atr": "distancia del cierre a la EMA200, en ATR",
    "extension_atr": "extensión del cierre sobre la EMA20, en ATR",
    "volumen_vs_20": "volumen de la vela / media de 20 velas",
    "ret_1d_pct": "rendimiento del par en 1 día (%)",
    "ret_7d_pct": "rendimiento del par en 7 días (%)",
    "ret_30d_pct": "rendimiento del par en 30 días (%)",
    "stop_pct": "distancia del stop inicial (% del precio)",
    "btc_ret_7d_pct": "rendimiento de BTC en 7 días (%)",
    "btc_ret_30d_pct": "rendimiento de BTC en 30 días (%)",
    "btc_sobre_sma200": "1 si BTC está sobre su media de 200 días, 0 si no",
    "par": "par (texto, p. ej. SOLUSDT)",
    "lado": "LONG o SHORT",
}
TEXT_RASGOS = ("par", "lado")

LECCIONES_SYSTEM = (
    "Eres el auditor de un bot de trading. Recibes las últimas señales de una estrategia con sus rasgos "
    "(calculados por código) y el resultado real en R (negativo = perdió). Propón como mucho 2 lecciones del tipo "
    "'evitar señales con <condición>' que, según estos datos, habrían ahorrado dinero: que las señales que cumplen "
    "la condición pierdan bastante más a menudo que el resto. Cada lección tiene 1 o 2 condiciones sobre los "
    "rasgos permitidos (operador '<' o '>' con un número; '=' solo para par y lado). No repitas lecciones que ya "
    "están en <cuaderno>. Si no ves nada claro, devuelve una lista vacía: es mejor que inventar. Cada lección se "
    "probará después con señales nuevas y se borrará sola si no ahorra dinero. Texto en español, una frase.")
LECCIONES_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["lecciones"],
    "properties": {"lecciones": {"type": "array", "maxItems": 2, "items": {
        "type": "object", "additionalProperties": False, "required": ["texto", "condiciones"],
        "properties": {
            "texto": {"type": "string", "maxLength": 200},
            "condiciones": {"type": "array", "minItems": 1, "maxItems": 2, "items": {
                "type": "object", "additionalProperties": False, "required": ["rasgo", "op", "valor"],
                "properties": {"rasgo": {"type": "string", "enum": list(RASGOS)},
                               "op": {"type": "string", "enum": ["<", ">", "="]},
                               "valor": {"type": "string", "maxLength": 20}}}}}}}},   # número escrito como texto
}


# ════════════════════════════════════════════════════════════════════════════
# Rasgos de una señal
# ════════════════════════════════════════════════════════════════════════════
def _num(v: Any, nd: int = 3) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def signal_features(ctx: dict, df: pd.DataFrame, market: dict | None, side: str | None) -> dict:
    """Rasgos fijos de una señal, sacados del contexto del analista y de las velas. Sin IA."""
    ind = ctx.get("indicadores") or {}
    ret = ctx.get("rendimiento_pct") or {}
    adx = None
    if df is not None and {"high", "low", "close"} <= set(df.columns) and len(df) >= 30:
        import quant_core_engine as q
        try:
            adx = _num(q.adx(df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)).iloc[-1], 2)
        except Exception:                                 # un rasgo que falta no impide votar
            adx = None
    if market is None and str(ctx.get("par", "")).upper() == "BTCUSDT":
        market = {"rendimiento_pct": ret, "sobre_sma200_diaria": None}
    m_ret = (market or {}).get("rendimiento_pct") or {}
    above = (market or {}).get("sobre_sma200_diaria")
    return {
        "adx14": adx, "rsi14": _num(ind.get("rsi14"), 2), "atr_pct": _num(ctx.get("atr_pct_del_precio")),
        "dist_ema200_atr": _num(ctx.get("distancia_a_ema200_en_atr"), 2),
        "extension_atr": _num(ind.get("extension_atr"), 2), "volumen_vs_20": _num(ctx.get("volumen_vs_media_20"), 2),
        "ret_1d_pct": _num(ret.get("1_dia"), 2), "ret_7d_pct": _num(ret.get("7_dias"), 2),
        "ret_30d_pct": _num(ret.get("30_dias"), 2), "stop_pct": _num(ctx.get("stop_inicial_pct"), 2),
        "btc_ret_7d_pct": _num(m_ret.get("7_dias"), 2), "btc_ret_30d_pct": _num(m_ret.get("30_dias"), 2),
        "btc_sobre_sma200": None if above is None else int(bool(above)),
        "par": str(ctx.get("par") or "").upper() or None, "lado": (side or "LONG").upper(),
    }


# ════════════════════════════════════════════════════════════════════════════
# Condiciones
# ════════════════════════════════════════════════════════════════════════════
def clean_conditions(raw: list) -> list[dict] | None:
    """Condiciones válidas o None. Un número en par/lado, o '=' en un rasgo numérico, invalidan la lección."""
    out = []
    for c in raw or []:
        if not isinstance(c, dict) or c.get("rasgo") not in RASGOS or c.get("op") not in ("<", ">", "="):
            return None
        rasgo, op, valor = c["rasgo"], c["op"], c.get("valor")
        if rasgo in TEXT_RASGOS:
            if op != "=" or not isinstance(valor, str) or not valor.strip():
                return None
            valor = valor.strip().upper()
        else:
            valor = _num(valor, 4)
            if op == "=" or valor is None:
                return None
        out.append({"rasgo": rasgo, "op": op, "valor": valor})
    return out if 1 <= len(out) <= 2 else None


def matches(conditions: list[dict], features: dict) -> bool:
    """¿La señal cumple todas las condiciones? Un rasgo sin dato no cumple."""
    for c in conditions:
        v = features.get(c["rasgo"])
        if v is None:
            return False
        if c["op"] == "=":
            if str(v).upper() != str(c["valor"]).upper():
                return False
        elif c["op"] == "<":
            if not float(v) < float(c["valor"]):
                return False
        elif not float(v) > float(c["valor"]):
            return False
    return True


def describe(conditions: list[dict]) -> str:
    return " y ".join(f"{c['rasgo']} {c['op']} {c['valor']}" for c in conditions)


def same_rule(a: list[dict], b: list[dict]) -> bool:
    key = lambda cs: sorted((c["rasgo"], c["op"], str(c["valor"])) for c in cs)   # noqa: E731
    return key(a) == key(b)


# ════════════════════════════════════════════════════════════════════════════
# Validación
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class LessonEvidence:
    cases: int = 0                # señales nuevas que cumplen la lección
    saved_r: float = 0.0          # R ahorrado si se hubieran evitado (suma de -R)
    hit_rate: float = 0.0         # de esas, cuántas perdieron
    base_rate: float = 0.0        # cuántas perdieron de todas las señales nuevas (el azar)
    total_signals: int = 0
    refs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"casos": self.cases, "ahorro_r": round(self.saved_r, 2), "acierto": round(self.hit_rate, 3),
                "azar": round(self.base_rate, 3), "senales_nuevas": self.total_signals}

    def text(self) -> str:
        if not self.cases:
            return f"0 casos de {self.total_signals} señales nuevas"
        return (f"{self.cases} casos: habría ahorrado {self.saved_r:+.1f} R, acierta {self.hit_rate:.0%} "
                f"(azar {self.base_rate:.0%})")


def evaluate(conditions: list[dict], signals: list[dict], since: str) -> LessonEvidence:
    """signals: [{"ref", "at", "outcome_r", "features"}] con resultado. Solo cuentan las posteriores a since."""
    fresh = [s for s in signals if s["at"] > since and s.get("outcome_r") is not None]
    ev = LessonEvidence(total_signals=len(fresh))
    if fresh:
        ev.base_rate = sum(1 for s in fresh if s["outcome_r"] < 0) / len(fresh)
    hit = [s for s in fresh if matches(conditions, s["features"])]
    ev.cases, ev.refs = len(hit), [s["ref"] for s in hit]
    if hit:
        ev.saved_r = -sum(float(s["outcome_r"]) for s in hit)
        ev.hit_rate = sum(1 for s in hit if s["outcome_r"] < 0) / len(hit)
    return ev


def next_status(current: str, ev: LessonEvidence, created_at: str, now: datetime) -> tuple[str, str]:
    """(estado, motivo). Estados: prueba → validada | descartada; validada → descartada. Descartada es final."""
    if current == "descartada":
        return current, ""
    passes = ev.saved_r > 0 and ev.hit_rate >= ev.base_rate + EDGE_OVER_CHANCE
    if current == "validada":
        if ev.saved_r <= 0 or ev.hit_rate < ev.base_rate:
            return "descartada", "dejó de ahorrar dinero: " + ev.text()
        return current, ""
    if ev.cases >= MIN_CASES:
        return ("validada", ev.text()) if passes else ("descartada", "no superó la prueba: " + ev.text())
    if now - datetime.fromisoformat(created_at) > timedelta(days=MAX_TEST_DAYS):
        return "descartada", f"en {MAX_TEST_DAYS} días solo juntó {ev.cases} casos"
    return current, ""
