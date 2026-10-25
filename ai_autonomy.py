#!/usr/bin/env python3
"""
ai_autonomy.py — Escalera de autonomía de los agentes de IA (sin IA: solo cuentas con los resultados reales).

Cada perfil (estrategia + temporalidad, p. ej. "trend_params_4h") tiene tres permisos. Todos empiezan en sombra:
la IA opina, el código anota su opinión y, cuando la operación cierra, mide cuánto dinero (en R) habría
ahorrado o hecho perder seguirla. Un permiso se activa solo cuando las pruebas lo justifican, y se apaga solo si
deja de justificarse.

  Escalón 1  sombra    opina y se anota (siempre).
  Escalón 2  veto      puede impedir una entrada.           Beneficio de un caso = -R de la operación vetada.
  Escalón 3  reducir   puede bajar a la mitad el riesgo.    Beneficio = -0.5 × R.
  Escalón 4  salida    puede cerrar antes una posición.     Beneficio = R si cerraba al votar - R real.

Para activarse hacen falta, con el modelo que está configurado hoy (el historial de un modelo no vale para otro):
  - casos suficientes (MIN_CASES) y, para veto/reducir, al menos MIN_VOTES votos con resultado en total;
  - beneficio total > 0 R y el percentil 10 de un bootstrap del beneficio medio > 0 (que no sea suerte);
  - acierto (casos con beneficio > 0) por encima del azar + 10 puntos. En veto, "el azar" es el % de
    operaciones perdedoras de la estrategia (~64% en Trend Rider): vetar sin criterio acierta eso mismo.
Se desactiva si el beneficio total cae a 0 o menos, si los últimos 10 casos suman menos de -2 R, o si el acierto
baja del azar. Abrir operaciones o subir el riesgo no forma parte de la escalera: eso es siempre de las reglas.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

PERMISSIONS = ("veto", "reducir", "salida")
LABELS = {"veto": "vetar entradas", "reducir": "reducir el tamaño a la mitad", "salida": "cerrar antes una posición"}
MIN_VOTES = 50                          # votos del analista con resultado (veto y reducir)
MIN_CASES = {"veto": 10, "reducir": 10, "salida": 20}
MIN_HIT_SALIDA = 0.60                   # en salidas no hay un "azar" natural: se exige 60%
EDGE_PP = 0.10                          # puntos sobre el azar
RECENT_N, RECENT_FLOOR_R = 10, -2.0
BOOT_N, BOOT_Q = 2000, 0.10


@dataclass
class Evidence:
    permission: str
    model: str
    cases: int
    needed: int
    votes: int
    benefit_r: float
    boot_low_r: float | None
    hit_rate: float | None
    base_rate: float | None
    recent_r: float
    qualifies: bool
    keeps: bool
    why: str

    def as_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.__dict__.items()}

    def text(self, risk_usd: float | None = None) -> str:
        usd = f" (≈ {self.benefit_r * risk_usd:+.2f} USD con el riesgo actual)" if risk_usd else ""
        hit = (f", acertó {self.hit_rate * 100:.0f}%" + (f" (al azar {self.base_rate * 100:.0f}%)"
                                                         if self.base_rate is not None else "")
               if self.hit_rate is not None else "")
        return f"{self.cases}/{self.needed} casos, {self.benefit_r:+.2f} R{usd}{hit}"


def _bootstrap_low(values: list[float], seed: int = 7) -> float | None:
    if len(values) < 2:
        return None
    rng = np.random.default_rng(seed)
    arr = np.asarray(values, dtype=float)
    means = arr[rng.integers(0, len(arr), size=(BOOT_N, len(arr)))].mean(axis=1)
    return float(np.quantile(means, BOOT_Q))


def benefits(permission: str, rows: list[dict]) -> list[float]:
    """Beneficio en R de cada caso en que la IA pidió actuar (los demás valen 0)."""
    if permission == "veto":
        return [-r["outcome_r"] for r in rows if r["decision"] == "VETO"]
    if permission == "reducir":
        return [-0.5 * r["outcome_r"] for r in rows if r["decision"] == "REDUCIR"]
    return [r["alt_r"] - r["outcome_r"] for r in rows if r["decision"] == "SALIR" and r.get("alt_r") is not None]


def evaluate(permission: str, rows: list[dict], model: str) -> Evidence:
    """rows: votos resueltos del analista (veto/reducir) o del guardián de salidas, del modelo actual."""
    b = benefits(permission, rows)
    needed = MIN_CASES[permission]
    total = float(sum(b))
    hit = (sum(x > 0 for x in b) / len(b)) if b else None
    if permission == "salida":
        base, min_hit = None, MIN_HIT_SALIDA
    else:
        losers = [r for r in rows if r["outcome_r"] is not None]
        base = (sum(r["outcome_r"] < 0 for r in losers) / len(losers)) if losers else None
        min_hit = (base or 0.5) + EDGE_PP
    low = _bootstrap_low(b)
    recent = float(sum(b[-RECENT_N:]))
    votes = len(rows)
    missing = []
    if len(b) < needed:
        missing.append(f"faltan casos ({len(b)}/{needed})")
    if permission != "salida" and votes < MIN_VOTES:
        missing.append(f"faltan votos con resultado ({votes}/{MIN_VOTES})")
    if total <= 0:
        missing.append(f"no ahorró dinero ({total:+.2f} R)")
    if low is None or low <= 0:
        missing.append("todavía puede ser suerte")
    if hit is None or hit < min_hit:
        missing.append(f"acierto bajo ({(hit or 0) * 100:.0f}% < {min_hit * 100:.0f}%)")
    qualifies = not missing
    keep_fail = []
    if total <= 0:
        keep_fail.append(f"el beneficio total cayó a {total:+.2f} R")
    if len(b) >= RECENT_N and recent < RECENT_FLOOR_R:
        keep_fail.append(f"los últimos {RECENT_N} casos suman {recent:+.2f} R")
    if hit is not None and base is not None and hit < base:
        keep_fail.append(f"acierta menos que el azar ({hit * 100:.0f}% < {base * 100:.0f}%)")
    if hit is not None and permission == "salida" and hit < 0.5:
        keep_fail.append(f"acierta menos de la mitad ({hit * 100:.0f}%)")
    return Evidence(permission, model, len(b), needed, votes, total, low, hit, base, recent, qualifies,
                    not keep_fail, "; ".join(missing if not qualifies else keep_fail) or "cumple")


def next_status(current: str, ev: Evidence, allowed: bool) -> tuple[str, str] | None:
    """Nuevo estado y motivo, o None si no cambia. 'revocado' solo lo cambia Keiner."""
    if current == "revocado":
        return None
    if current == "sombra" and allowed and ev.qualifies:
        return "activo", "las pruebas lo justifican: " + ev.text()
    if current == "activo" and (not allowed or not ev.keeps):
        return "sombra", ("permiso desactivado en la configuración" if not allowed else "dejó de cumplir: " + ev.why)
    return None
