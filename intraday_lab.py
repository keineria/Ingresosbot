#!/usr/bin/env python3
"""
intraday_lab.py — Laboratorio de "momentos": qué variables suben el acierto en temporalidades menores (15m, 1h, 4h).

Responde a dos preguntas, siempre DESPUÉS de comisiones (0.08% por orden + 0.02% de deslizamiento, ida y vuelta):

  1. explore   ¿Hay horas, sesiones, días, niveles de volatilidad o de volumen en los que comprar rinde más?
               Para cada vela calcula el retorno de comprar en la apertura siguiente y vender N horas después,
               y lo agrupa por variable. Una variable solo cuenta si el efecto tiene el mismo signo en la
               mayoría de los pares y en las dos mitades de la historia.

  2. walk      Estrategias candidatas de "comprar la caída dentro de la tendencia" (alto acierto por diseño),
               con filtros de tendencia diaria / 4h, hora del día y salida por rebote. Simulación rápida
               (un trade cada vez por par, Capa 1 simplificada, tamaño de la Capa 3) y walk-forward
               12 meses de entrenamiento → 3 meses de prueba que nunca vio.

La simulación rápida sirve para descartar ideas en segundos. La candidata final se valida después con el motor
real (backtester.run_portfolio_backtest, Capa 1 → 2 → 3 completa) en las 20 combinaciones de pares.

Reglas de la simulación (las mismas del backtester):
  * Señal con la vela t cerrada; entrada en la apertura de t+1 con deslizamiento en contra.
  * SL fijo a k × ATR14 de t. Si una vela toca SL y TP, se asume el SL. Gap más allá del SL: sale a la apertura.
  * Salida por rebote (cierre > SMA de salida o RSI2 > umbral) o por tiempo: a la apertura de la vela siguiente.
  * Tamaño: riesgo 1.5% (0.75% tras 2 pérdidas seguidas) / distancia al SL, tope 85% del balance.
  * 3 pérdidas seguidas o -3% en el día → 15 velas sin operar.

Uso:
  python intraday_lab.py explore --csv-dir data/candles --interval 15m --horizon-hours 4 --out-md momentos_15m.md
  python intraday_lab.py walk --csv-dir data/candles --interval 1h --out-md walk_1h.md
  python intraday_lab.py walk --csv-dir data/candles --interval 15m --balance 500 --trade-from 2024-01-01
  python intraday_lab.py download --csv-dir data/lab_velas --intervals 15m,1h,4h --since 2023-01-01
  python intraday_lab.py full --csv-dir data/lab_velas --out-dir data/lab_momentos --workers 2
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

import quant_core_engine as q

FEE = 0.0008
SLIP = 0.0002
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
BARS_PER_HOUR = {"5m": 12, "15m": 4, "30m": 2, "1h": 1, "2h": 0.5, "4h": 0.25}

# Sesiones en hora UTC (Colombia = UTC-5). Cortes clásicos de cripto: Asia, Londres, solape Londres-NY, NY, cierre.
SESSIONS = (("Asia 00-07", 0, 7), ("Londres 07-13", 7, 13), ("Solape LDN-NY 13-16", 13, 16),
            ("Nueva York 16-21", 16, 21), ("Cierre NY 21-24", 21, 24))
WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


# ════════════════════════════════════════════════════════════════════════════
# DATOS E INDICADORES
# ════════════════════════════════════════════════════════════════════════════
def load(csv_dir: str | Path, symbols, interval: str) -> dict[str, pd.DataFrame]:
    """<PAR>_<intervalo>.csv; si falta el intervalo pero existe uno menor, lo agrega (15m → 1h, 1h → 4h)."""
    out = {}
    for s in symbols:
        path = Path(csv_dir) / f"{s}_{interval}.csv"
        if path.exists():
            out[s] = q.load_csv(path)
            continue
        for src in ("15m", "1h"):
            p2 = Path(csv_dir) / f"{s}_{src}.csv"
            if p2.exists() and BARS_PER_HOUR[src] > BARS_PER_HOUR.get(interval, 0):
                out[s] = resample(q.load_csv(p2), interval)
                break
        else:
            raise SystemExit(f"No encuentro {path}")
    return out


def resample(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    rule = interval.replace("m", "min")
    g = df.set_index("time").resample(rule, label="left", closed="left")
    out = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(),
                        "close": g.close.last(), "volume": g.volume.sum()}).dropna()
    return out.reset_index()


def rsi_n(close: pd.Series, n: int) -> pd.Series:
    return q.rsi(close, n)


def _htf_flag(df: pd.DataFrame, rule: str, ema_len: int) -> np.ndarray:
    """+1/-1 si el ÚLTIMO cierre ya terminado de la temporalidad mayor está sobre/bajo su EMA (sin mirar el futuro)."""
    t = df.set_index("time")
    htf = t.close.resample(rule, label="left", closed="left").last().dropna()
    e = q.ema(htf, ema_len)
    sign = np.sign(htf - e)
    # La vela mayor que empieza en T cierra en T+rule: solo se conoce a partir de esa hora.
    sign.index = sign.index + pd.Timedelta(rule)
    flag = sign.reindex(t.index, method="ffill")
    # Un valor solo está disponible cuando la vela menor que lo contiene ya cerró: la vela que empieza
    # justo en T+rule ya puede usarlo (su señal se evalúa a su cierre).
    return flag.fillna(0.0).to_numpy(float)


def features(df: pd.DataFrame, interval: str, daily_sma: int = 200) -> pd.DataFrame:
    """Variables por vela, todas calculadas con información disponible al cierre de esa vela."""
    p = q.Params(daily_sma_len=daily_sma)
    f = q.compute_indicators(df, p)
    c = f.close
    f["rsi2"] = rsi_n(c, 2)
    f["rsi3"] = rsi_n(c, 3)
    for n in (5, 10, 20):
        f[f"sma{n}"] = c.rolling(n).mean()
    f["atr_pct"] = f.atr14 / c * 100
    bpd = int(24 * BARS_PER_HOUR[interval])                        # velas por día
    f["atr_rank"] = f.atr_pct.rolling(30 * bpd, min_periods=10 * bpd).rank(pct=True)
    f["vol_ratio"] = f.volume / f.volume.rolling(20).mean()
    f["ibs"] = ((c - f.low) / (f.high - f.low)).where(f.high > f.low, 0.5)
    f["ext_atr"] = (c - f.ema20) / f.atr14
    f["down_streak"] = _streak(c.diff() < 0)
    f["trend4h"] = _htf_flag(df, "4h", 50) if BARS_PER_HOUR[interval] > 0.25 else np.sign(c - f.ema50)
    f["trend1d_ema20"] = _htf_flag(df, "1D", 20)
    t = pd.to_datetime(f.time, utc=True)
    # La hora de la señal es la del CIERRE de la vela (la entrada es en la apertura siguiente).
    close_t = t + pd.Timedelta(hours=1 / BARS_PER_HOUR[interval])
    f["hour"] = close_t.dt.hour
    f["weekday"] = close_t.dt.weekday
    f["session"] = pd.cut(f.hour, [s[1] for s in SESSIONS] + [24], right=False,
                          labels=[s[0] for s in SESSIONS]).astype(str)
    return f


def _streak(mask: pd.Series) -> pd.Series:
    m = mask.astype(int).to_numpy()
    out = np.zeros(len(m), dtype=int)
    for i in range(len(m)):
        out[i] = out[i - 1] + 1 if m[i] and i else (1 if m[i] else 0)
    return pd.Series(out, index=mask.index)


# ════════════════════════════════════════════════════════════════════════════
# 1. EXPLORAR VARIABLES
# ════════════════════════════════════════════════════════════════════════════
def forward_net(f: pd.DataFrame, bars: int) -> np.ndarray:
    """Retorno neto (%) de comprar en la apertura de t+1 y vender en el cierre de t+bars, con costos."""
    o = f.open.to_numpy(float)
    c = f.close.to_numpy(float)
    n = len(f)
    out = np.full(n, np.nan)
    idx = np.arange(n - bars)
    entry = o[idx + 1] * (1 + SLIP)
    ex = c[idx + bars] * (1 - SLIP)
    out[idx] = (ex / entry - 1 - FEE * (1 + ex / entry)) * 100
    return out


BUCKETS = {
    "hour": lambda f: f.hour.astype(int),
    "session": lambda f: f.session,
    "weekday": lambda f: f.weekday.map(dict(enumerate(WEEKDAYS))),
    "tendencia diaria (SMA200d)": lambda f: f.daily_trend.map({1.0: "arriba", -1.0: "abajo", 0.0: "s/d"}),
    "tendencia 4h (EMA50)": lambda f: pd.Series(f.trend4h).map({1.0: "arriba", -1.0: "abajo", 0.0: "s/d"}),
    "volatilidad (ATR vs 30 días)": lambda f: pd.cut(f.atr_rank, [0, 1 / 3, 2 / 3, 1.0001],
                                                     labels=["baja", "media", "alta"]).astype(str),
    "volumen vs media 20": lambda f: pd.cut(f.vol_ratio, [0, 0.7, 1.3, 2.0, np.inf],
                                            labels=["<0.7x", "0.7-1.3x", "1.3-2x", ">2x"]).astype(str),
    "RSI2": lambda f: pd.cut(f.rsi2, [-1, 10, 30, 70, 90, 101],
                             labels=["<10", "10-30", "30-70", "70-90", ">90"]).astype(str),
    "velas rojas seguidas": lambda f: f.down_streak.clip(upper=4).map({0: "0", 1: "1", 2: "2", 3: "3", 4: "4+"}),
    "distancia a EMA20 (ATR)": lambda f: pd.cut(f.ext_atr, [-np.inf, -2, -1, 0, 1, 2, np.inf],
                                                labels=["<-2", "-2..-1", "-1..0", "0..1", "1..2", ">2"]).astype(str),
}


def explore(data: dict[str, pd.DataFrame], interval: str, horizon_hours: float = 4,
            trade_from: str | None = None, condition: str | None = None) -> dict[str, pd.DataFrame]:
    """Tablas por variable: n, acierto (% neto > 0), retorno neto medio (%), pares con efecto positivo,
    y si el signo se mantiene en la primera y la segunda mitad. `condition` filtra antes (expresión pandas,
    p. ej. "daily_trend > 0 and rsi2 < 10") para estudiar los momentos de una señal concreta."""
    bars = max(1, int(round(horizon_hours * BARS_PER_HOUR[interval])))
    frames = []
    for s, df in data.items():
        f = features(df, interval)
        f["fwd"] = forward_net(f, bars)
        f["symbol"] = s
        if trade_from:
            f = f[f.time >= pd.Timestamp(trade_from, tz="UTC")]
        if condition:
            f = f.query(condition)
        frames.append(f.dropna(subset=["fwd"]))
    allf = pd.concat(frames, ignore_index=True)
    mid = allf.time.min() + (allf.time.max() - allf.time.min()) / 2
    allf["half"] = np.where(allf.time < mid, 1, 2)
    base = {"n": len(allf), "hit": float((allf.fwd > 0).mean() * 100), "mean": float(allf.fwd.mean())}
    out = {"_base": pd.DataFrame([base])}
    for name, fn in BUCKETS.items():
        allf["b"] = fn(allf).astype(str).values
        g = allf.groupby("b")
        t = pd.DataFrame({"n": g.fwd.size(), "acierto_%": g.fwd.apply(lambda x: (x > 0).mean() * 100),
                          "neto_medio_%": g.fwd.mean()})
        per_sym = allf.groupby(["b", "symbol"]).fwd.mean().unstack()
        t["pares_+"] = (per_sym > 0).sum(axis=1).astype(int).astype(str) + f"/{per_sym.shape[1]}"
        halves = allf.groupby(["b", "half"]).fwd.mean().unstack()
        t["mitad1_%"] = halves.get(1)
        t["mitad2_%"] = halves.get(2)
        t["vs_media"] = t["neto_medio_%"] - base["mean"]
        if name == "hour":
            t = t.sort_index(key=lambda ix: ix.astype(int))
        out[name] = t.round(3)
    return out


def explore_md(tables: dict[str, pd.DataFrame], interval: str, horizon_hours: float, condition: str | None) -> str:
    b = tables["_base"].iloc[0]
    lines = [f"# Momentos del mercado ({interval}, comprar y vender {horizon_hours:g} h después)", "",
             f"Filtro previo: `{condition}`" if condition else "Sin filtro previo: todas las velas.", "",
             f"Base: {int(b.n):,} velas, acierto {b.hit:.1f}%, retorno neto medio {b['mean']:+.3f}% por operación "
             f"(ya restadas comisiones y deslizamiento ≈ {2 * (FEE + SLIP) * 100:.2f}% ida y vuelta).", "",
             "Una variable sirve solo si la fila es positiva en casi todos los pares y en las dos mitades.", ""]
    for name, t in tables.items():
        if name == "_base":
            continue
        lines += [f"## {name}", "", "| Valor | Velas | Acierto | Neto medio | vs base | Pares + | 1.ª mitad | 2.ª mitad |",
                  "|---|---|---|---|---|---|---|---|"]
        for k, r in t.iterrows():
            lines.append(f"| {k} | {int(r.n):,} | {r['acierto_%']:.1f}% | {r['neto_medio_%']:+.3f}% | "
                         f"{r.vs_media:+.3f} | {r['pares_+']} | {r['mitad1_%']:+.3f}% | {r['mitad2_%']:+.3f}% |")
        lines.append("")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# 2. SIMULACIÓN RÁPIDA DE LA ESTRATEGIA "REBOTE EN TENDENCIA"
# ════════════════════════════════════════════════════════════════════════════
DEFAULT_COMBO = {
    "daily_filter": True,       # cierre diario de ayer > SMA200 diaria
    "ema200_filter": True,      # cierre > EMA200 de la propia temporalidad
    "trend4h_filter": False,    # último cierre 4h > EMA50 4h
    "trigger": "rsi2",          # rsi2 | ibs | streak
    "trigger_level": 10.0,      # RSI2 < 10 | IBS < 0.2 | N velas rojas
    "exit": "sma5",             # sma5 | sma10 | rsi2_70
    "sl_atr": 2.5,
    "tp_atr": 0.0,              # 0 = sin TP fijo
    "max_bars": 24,             # salida por tiempo
    "hours": None,              # lista de horas UTC (cierre de vela) permitidas para entrar; None = todas
    "min_atr_rank": 0.0,        # volatilidad mínima (percentil 30 días)
}


def _signal(f: pd.DataFrame, c: dict) -> np.ndarray:
    sig = np.ones(len(f), dtype=bool)
    if c["daily_filter"]:
        sig &= f.daily_trend.to_numpy() > 0
    if c["ema200_filter"]:
        sig &= (f.close > f.ema200).to_numpy()
    if c["trend4h_filter"]:
        sig &= np.asarray(f.trend4h) > 0
    lvl = c["trigger_level"]
    if c["trigger"] == "rsi2":
        sig &= (f.rsi2 < lvl).to_numpy()
    elif c["trigger"] == "ibs":
        sig &= (f.ibs < lvl).to_numpy()
    elif c["trigger"] == "streak":
        sig &= (f.down_streak >= lvl).to_numpy()
    else:
        raise ValueError(c["trigger"])
    if c.get("hours"):
        sig &= f.hour.isin(c["hours"]).to_numpy()
    if c.get("min_atr_rank"):
        sig &= (f.atr_rank >= c["min_atr_rank"]).to_numpy()
    return sig & f.atr14.notna().to_numpy() & (f.atr14 > 0).to_numpy()


def _exit_flag(f: pd.DataFrame, c: dict) -> np.ndarray:
    if c["exit"] == "sma5":
        return (f.close > f.sma5).to_numpy()
    if c["exit"] == "sma10":
        return (f.close > f.sma10).to_numpy()
    if c["exit"] == "rsi2_70":
        return (f.rsi2 > 70).to_numpy()
    raise ValueError(c["exit"])


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def simulate(f: pd.DataFrame, c: dict, balance: float = 500.0, t_from=None, t_to=None,
             risk_pct: float = 1.5, throttled_pct: float = 0.75, cap: float = 0.85) -> dict:
    """Un par, una posición a la vez, largos. Devuelve métricas y la lista de trades."""
    o, h, l, cl = (f[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    atr_ = f.atr14.to_numpy(float)
    times = f.time.to_numpy()
    sig = _signal(f, c)
    exf = _exit_flag(f, c)
    n = len(f)
    lo = 0 if t_from is None else int(f.time.searchsorted(_utc(t_from)))
    hi = n if t_to is None else int(f.time.searchsorted(_utc(t_to)))
    days = f.time.dt.floor("D").to_numpy()
    hours = f.hour.to_numpy()
    bal = balance
    peak_day, cur_day = bal, None
    losses, cooldown_until, free_from = 0, -1, max(lo, 1)
    trades = []
    eq_pts = [bal]
    # Solo se recorren las velas con señal (rápido también en 15m); las demás no cambian nada.
    for i in np.flatnonzero(sig[max(lo, 1):max(hi - 1, 1)]) + max(lo, 1):
        if i < free_from or i <= cooldown_until:
            continue
        if days[i] != cur_day:
            cur_day, peak_day = days[i], bal
        entry = o[i + 1] * (1 + SLIP)
        sl = entry - c["sl_atr"] * atr_[i]
        if sl <= 0:
            continue
        tp = entry + c["tp_atr"] * atr_[i] if c["tp_atr"] else np.inf
        rp = throttled_pct if losses == 2 else risk_pct
        notional = min(bal * cap, bal * rp / 100 / ((entry - sl) / entry))
        if notional < 10:
            continue
        j, exit_px, reason = i + 1, None, None
        last = min(hi - 1, i + c["max_bars"])
        while j <= last:
            if o[j] <= sl and j > i + 1:
                exit_px, reason = o[j], "SL"
            elif l[j] <= sl:
                exit_px, reason = sl * (1 - SLIP), "SL"
            elif h[j] >= tp:
                exit_px, reason = (o[j] if o[j] >= tp and j > i + 1 else tp), "TP"
            elif j + 1 < n and (exf[j] or j == last):
                exit_px, reason = o[j + 1] * (1 - SLIP), ("REBOTE" if exf[j] else "TIEMPO")
                j += 1
            if exit_px is not None:
                break
            j += 1
        if exit_px is None:
            j = min(j, n - 1)
            exit_px, reason = cl[j], "FIN"
        qty = notional / entry
        pnl = qty * (exit_px - entry) - FEE * qty * (entry + exit_px)
        bal += pnl
        eq_pts.append(bal)
        trades.append({"opened": times[i + 1], "closed": times[min(j, n - 1)], "entry": entry, "exit": exit_px,
                       "pnl": pnl, "ret_pct": (exit_px / entry - 1 - FEE * (1 + exit_px / entry)) * 100,
                       "reason": reason, "bars": j - i, "hour": int(hours[i])})
        losses = 0 if pnl > 0 else losses + 1
        if losses >= 3 or (peak_day - bal) / peak_day * 100 >= 3.0:
            cooldown_until = j + 15
            losses = 0
        free_from = j          # como el motor: la vela de salida ya puede dar la siguiente señal
    eq = np.array(eq_pts)
    pk = np.maximum.accumulate(eq)
    tr = pd.DataFrame(trades)
    wins = tr.pnl[tr.pnl > 0].sum() if len(tr) else 0.0
    loss = -tr.pnl[tr.pnl < 0].sum() if len(tr) else 0.0
    return {"return_pct": (bal / balance - 1) * 100, "max_dd_pct": float(((pk - eq) / pk).max() * 100),
            "trades": len(tr), "win_rate_pct": float((tr.pnl > 0).mean() * 100) if len(tr) else 0.0,
            "profit_factor": float(wins / loss) if loss > 0 else (np.inf if wins > 0 else 0.0),
            "avg_net_pct": float(tr.ret_pct.mean()) if len(tr) else 0.0,
            "trades_df": tr}


# Rejilla del walk-forward: pocas variantes con sentido económico, no miles (para no ajustar ruido).
def lab_grid(interval: str) -> list[dict]:
    bph = BARS_PER_HOUR[interval]
    max_bars = sorted({max(2, int(x * bph)) for x in (6, 24)})
    grid = []
    for trig, lvl in (("rsi2", 5.0), ("rsi2", 10.0), ("rsi2", 20.0), ("ibs", 0.15), ("streak", 3)):
        for ex, sl, mb, t4 in itertools.product(("sma5", "rsi2_70"), (2.0, 3.0), max_bars, (False, True)):
            grid.append(dict(DEFAULT_COMBO, trigger=trig, trigger_level=lvl, exit=ex, sl_atr=sl, max_bars=mb,
                             trend4h_filter=t4))
    return grid


def combo_label(c: dict) -> str:
    trig = {"rsi2": f"RSI2<{c['trigger_level']:g}", "ibs": f"IBS<{c['trigger_level']:g}",
            "streak": f"{int(c['trigger_level'])} rojas"}[c["trigger"]]
    parts = [trig, f"salida {c['exit']}", f"SL {c['sl_atr']:g}", f"máx {c['max_bars']} velas"]
    if c["trend4h_filter"]:
        parts.append("4h↑")
    if not c["daily_filter"]:
        parts.append("sin filtro diario")
    if c.get("hours"):
        parts.append(f"horas {_hours_label(c['hours'])}")
    if c.get("tp_atr"):
        parts.append(f"TP {c['tp_atr']:g}")
    return " · ".join(parts)


def _hours_label(hs) -> str:
    hs = sorted(hs)
    return ",".join(str(x) for x in hs) if len(hs) < 6 else f"{len(hs)} horas"


def _score(rs: list[dict]) -> float:
    if not rs or sum(r["trades"] for r in rs) < 10:
        return float("-inf")
    ret = np.array([r["return_pct"] for r in rs])
    dd = np.array([r["max_dd_pct"] for r in rs])
    return float(ret.mean() - 0.5 * dd.mean() + 0.5 * ret.min())


_FEATS: dict = {}


def _init(feats):
    global _FEATS
    _FEATS = feats


def _sim_task(args):
    c, s, t_from, t_to, balance = args
    r = simulate(_FEATS[s], c, balance, t_from, t_to)
    r.pop("trades_df")
    return r


def windows(t0, t_end, train_months, test_months):
    out, start = [], t0
    while True:
        te_from = start + pd.DateOffset(months=train_months)
        te_to = te_from + pd.DateOffset(months=test_months)
        if te_from >= t_end:
            break
        out.append((start, te_from, min(te_to, t_end)))
        if te_to >= t_end:
            break
        start += pd.DateOffset(months=test_months)
    return out


def best_hours(feats: dict[str, pd.DataFrame], c: dict, t_from, t_to, keep: int = 12) -> list[int]:
    """Horas (cierre de vela, UTC) con mejor retorno medio por trade de la señal en el periodo dado, sumando pares.
    Se elige SOLO con el entrenamiento; en la prueba se aplica tal cual."""
    rows = []
    for f in feats.values():
        tr = simulate(f, dict(c, hours=None), 500.0, t_from, t_to)["trades_df"]
        if len(tr):
            rows.append(tr[["hour", "ret_pct"]])
    if not rows:
        return list(range(24))
    g = pd.concat(rows).groupby("hour").ret_pct.agg(["mean", "size"])
    g = g[g["size"] >= 5].sort_values("mean", ascending=False)
    return sorted(int(x) for x in g.index[:keep])


def walk_forward(data: dict[str, pd.DataFrame], interval: str, grid: list[dict] | None = None,
                 train_months: int = 12, test_months: int = 3, balance: float = 500.0,
                 trade_from: str = "2024-01-01", hour_filter: bool = True, workers: int | None = None,
                 progress=print) -> dict:
    grid = grid or lab_grid(interval)
    feats = {s: features(df, interval) for s, df in data.items()}
    t_end = max(f.time.max() for f in feats.values())
    t0 = pd.Timestamp(trade_from, tz="UTC") - pd.DateOffset(months=train_months)
    t0 = max(t0, min(f.time.min() for f in feats.values()) + pd.Timedelta(days=210))
    wins = windows(t0, t_end, train_months, test_months)
    oos = {"elegida": {s: [] for s in feats}, "fija": {s: [] for s in feats}}
    oos_stats = {"elegida": [], "fija": []}
    fixed = dict(DEFAULT_COMBO, max_bars=max(2, int(24 * BARS_PER_HOUR[interval])))
    log = []
    t_start = time.time()
    with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(feats,)) as pool:
        for w, (tr_from, te_from, te_to) in enumerate(wins, 1):
            tasks = [(c, s, tr_from, te_from, balance) for c in grid for s in feats]
            res = list(pool.map(_sim_task, tasks, chunksize=8))
            by = {}
            for (c, s, *_), r in zip(tasks, res):
                by.setdefault(json.dumps(c, sort_keys=True), []).append(r)
            best_key = max(by, key=lambda k: _score(by[k]))
            best = json.loads(best_key)
            if hour_filter:
                hrs = best_hours(feats, best, tr_from, te_from)
                cand = dict(best, hours=hrs)
                tr_c = [simulate(feats[s], cand, balance, tr_from, te_from) for s in feats]
                if _score(tr_c) > _score(by[best_key]):
                    best = cand
            info = {"window": w, "test": f"{te_from:%Y-%m-%d} → {te_to:%Y-%m-%d}", "chosen": combo_label(best),
                    "results": {}, "fixed": {}}
            for role, combo in (("elegida", best), ("fija", fixed)):
                for s in feats:
                    r = simulate(feats[s], combo, balance, te_from, te_to)
                    oos[role][s].append(r["return_pct"])
                    if len(r["trades_df"]):
                        oos_stats[role].append(r["trades_df"].assign(symbol=s, window=w))
                    (info["results"] if role == "elegida" else info["fixed"])[s] = round(float(r["return_pct"]), 2)
            log.append(info)
            progress(f"[walk {interval}] {w}/{len(wins)} {info['test']} «{info['chosen']}» "
                     f"{info['results']} ({time.time() - t_start:.0f}s)")

    def chain(v):
        return round((np.prod([1 + x / 100 for x in v]) - 1) * 100, 2)

    summ = {"interval": interval, "symbols": list(feats), "train_months": train_months,
            "test_months": test_months, "balance": balance, "grid_size": len(grid), "windows": log,
            "oos_pct": {k: {s: chain(v) for s, v in d.items()} for k, d in oos.items()},
            "positive_windows": {k: {s: f"{sum(x > 0 for x in v)}/{len(v)}" for s, v in d.items()}
                                 for k, d in oos.items()}, "trade_stats": {}}
    for role, frames in oos_stats.items():
        if frames:
            t = pd.concat(frames)
            summ["trade_stats"][role] = {"trades": len(t), "win_rate_pct": round(float((t.pnl > 0).mean() * 100), 1),
                                         "avg_net_pct": round(float(t.ret_pct.mean()), 3),
                                         "avg_bars": round(float(t.bars.mean()), 1),
                                         "exits": t.reason.value_counts().to_dict()}
    return summ


def walk_md(s: dict) -> str:
    lines = [f"# Walk-forward «Rebote en tendencia» ({s['interval']})", "",
             f"Entrenamiento {s['train_months']} meses → prueba {s['test_months']} meses. {s['grid_size']} variantes "
             f"por ventana (+ filtro de horas elegido con el entrenamiento). {s['balance']:,.0f} USD por par en cada "
             "prueba; los retornos se encadenan.", "",
             "| Par | Elegida con el pasado | Ventanas + | Regla fija (RSI2<10, salida SMA5, SL 2.5) | Ventanas + |",
             "|---|---|---|---|---|"]
    for sym in s["symbols"]:
        lines.append(f"| {sym} | {s['oos_pct']['elegida'][sym]:+.2f}% | {s['positive_windows']['elegida'][sym]} | "
                     f"{s['oos_pct']['fija'][sym]:+.2f}% | {s['positive_windows']['fija'][sym]} |")
    lines += [""]
    for role, st in s["trade_stats"].items():
        lines.append(f"- **{role}**: {st['trades']} trades fuera de muestra, acierto {st['win_rate_pct']}%, "
                     f"neto medio {st['avg_net_pct']:+.3f}% por trade, {st['avg_bars']} velas de media, "
                     f"salidas {st['exits']}")
    lines += ["", "## Ventanas", "", "| # | Prueba | Elegida | " + " | ".join(s["symbols"]) + " |",
              "|---|---|---|" + "---|" * len(s["symbols"])]
    for w in s["windows"]:
        lines.append(f"| {w['window']} | {w['test']} | {w['chosen']} | " +
                     " | ".join(f"{w['results'][x]:+.2f}%" for x in s["symbols"]) + " |")
    return "\n".join(lines) + "\n"


# ════════════════════════════════════════════════════════════════════════════
# DESCARGA, REJILLA FIJA Y CORRIDA COMPLETA
# ════════════════════════════════════════════════════════════════════════════
def download(csv_dir: str | Path, symbols, intervals, since: str = "2023-01-01") -> list[str]:
    """Velas públicas de Binance (api.binance.com y, si falla, data-api.binance.vision) desde `since`."""
    import market_fetcher as mf
    out = []
    Path(csv_dir).mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    since_ms = int(pd.Timestamp(since, tz="UTC").timestamp() * 1000)
    for iv in intervals:
        step = mf.INTERVAL_MS[iv]
        need = (now_ms - since_ms) // step + 2
        for s in symbols:
            rows = None
            for host in mf.BINANCE_HOSTS:
                try:
                    rows = mf.fetch_binance(s, iv, need, host)
                    break
                except Exception as exc:          # 451 en algunos países: se prueba el otro host
                    print(f"[download] {s} {iv} {host}: {exc}", file=sys.stderr)
            if not rows:
                raise SystemExit(f"No pude descargar {s} {iv}")
            df = mf._to_frame(rows, now_ms)
            df = df[df.time >= pd.Timestamp(since, tz="UTC")]
            path = Path(csv_dir) / f"{s}_{iv}.csv"
            df.to_csv(path, index=False)
            msg = f"{path.name}: {len(df):,} velas {df.time.iloc[0]:%Y-%m-%d %H:%M} → {df.time.iloc[-1]:%Y-%m-%d %H:%M}"
            print(f"[download] {msg}", file=sys.stderr)
            out.append(msg)
    return out


def grid_table(data: dict[str, pd.DataFrame], interval: str, trade_from: str, balance: float = 500.0,
               grid: list[dict] | None = None) -> pd.DataFrame:
    """Cada variante con parámetros FIJOS en todo el periodo, par por par. No sirve para elegir (eso es el
    walk-forward), sino para ver si una zona entera de parámetros gana o si solo gana una combinación suelta."""
    grid = grid or lab_grid(interval)
    feats = {s: features(df, interval) for s, df in data.items()}
    rows = []
    for c in grid:
        rs = {s: simulate(f, c, balance, trade_from) for s, f in feats.items()}
        tr = pd.concat([r["trades_df"] for r in rs.values() if len(r["trades_df"])] or [pd.DataFrame()])
        rets = np.array([r["return_pct"] for r in rs.values()])
        rows.append({"variante": combo_label(c), "ret_medio_%": rets.mean(), "peor_par_%": rets.min(),
                     "pares_+": f"{int((rets > 0).sum())}/{len(rets)}",
                     "dd_medio_%": np.mean([r["max_dd_pct"] for r in rs.values()]),
                     "trades": len(tr), "acierto_%": float((tr.pnl > 0).mean() * 100) if len(tr) else 0.0,
                     "neto_trade_%": float(tr.ret_pct.mean()) if len(tr) else 0.0,
                     "combo": json.dumps(c, sort_keys=True)})
    return pd.DataFrame(rows).sort_values("ret_medio_%", ascending=False).round(3)


def grid_md(t: pd.DataFrame, interval: str, top: int = 15) -> str:
    lines = [f"# Rejilla fija ({interval}): {len(t)} variantes, de la mejor a la peor por retorno medio", "",
             f"Variantes con retorno medio > 0: {int((t['ret_medio_%'] > 0).sum())}/{len(t)}. "
             f"Con los 5 pares en positivo: {int((t['pares_+'] == '5/5').sum())}.", "",
             "| Variante | Retorno medio | Peor par | Pares + | DD medio | Trades | Acierto | Neto/trade |",
             "|---|---|---|---|---|---|---|---|"]
    show = pd.concat([t.head(top), t.tail(3)]) if len(t) > top + 3 else t
    for _, r in show.iterrows():
        lines.append(f"| {r.variante} | {r['ret_medio_%']:+.2f}% | {r['peor_par_%']:+.2f}% | {r['pares_+']} | "
                     f"{r['dd_medio_%']:.1f}% | {int(r.trades)} | {r['acierto_%']:.1f}% | {r['neto_trade_%']:+.3f}% |")
    return "\n".join(lines) + "\n"


DIP_CONDITION = "daily_trend > 0 and close > ema200 and rsi2 < 10"


def run_full(csv_dir: str, symbols, intervals, out_dir: str, trade_from: str, balance: float,
             workers: int | None) -> str:
    """Todo de una vez, para correrlo en el VPS y traer solo los informes: explorar (todas las velas y solo
    las caídas en tendencia), rejilla fija y walk-forward por temporalidad. Devuelve el resumen."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = [f"# Laboratorio de momentos — resumen ({pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC)", "",
               f"Pares: {', '.join(symbols)}. Desde {trade_from}. {balance:,.0f} USD por par. "
               f"Costos: {FEE * 100:.2f}% por orden + {SLIP * 100:.2f}% de deslizamiento.", ""]
    for iv in intervals:
        t0 = time.time()
        data = load(csv_dir, symbols, iv)
        hz = {"15m": 2, "1h": 4, "4h": 16}.get(iv, 4)
        tabs = explore(data, iv, hz, trade_from)
        (out / f"momentos_{iv}.md").write_text(explore_md(tabs, iv, hz, None), encoding="utf-8")
        dip = explore(data, iv, hz, trade_from, DIP_CONDITION)
        (out / f"momentos_caidas_{iv}.md").write_text(explore_md(dip, iv, hz, DIP_CONDITION), encoding="utf-8")
        g = grid_table(data, iv, trade_from, balance)
        g.to_csv(out / f"rejilla_{iv}.csv", index=False)
        (out / f"rejilla_{iv}.md").write_text(grid_md(g, iv), encoding="utf-8")
        wf = walk_forward(data, iv, None, 12, 3, balance, trade_from, True, workers,
                          progress=lambda m: print(m, file=sys.stderr))
        (out / f"walk_{iv}.md").write_text(walk_md(wf), encoding="utf-8")
        (out / f"walk_{iv}.json").write_text(json.dumps(wf, default=str, indent=1), encoding="utf-8")
        b, d = tabs["_base"].iloc[0], dip["_base"].iloc[0]
        best = g.iloc[0]
        summary += [f"## {iv}", "",
                    f"- Comprar cualquier vela y salir {hz} h después: acierto {b.hit:.1f}%, neto {b['mean']:+.3f}%.",
                    f"- Comprar solo caídas en tendencia (`{DIP_CONDITION}`): {int(d.n):,} velas, acierto "
                    f"{d.hit:.1f}%, neto {d['mean']:+.3f}%.",
                    f"- Rejilla fija: {int((g['ret_medio_%'] > 0).sum())}/{len(g)} variantes con retorno medio > 0; "
                    f"mejor «{best.variante}» {best['ret_medio_%']:+.2f}% (peor par {best['peor_par_%']:+.2f}%, "
                    f"acierto {best['acierto_%']:.1f}%, {int(best.trades)} trades).",
                    "- Walk-forward fuera de muestra (elegida con el pasado): " +
                    ", ".join(f"{s} {v:+.2f}%" for s, v in wf["oos_pct"]["elegida"].items()) + ".",
                    "- Regla fija RSI2<10: " + ", ".join(f"{s} {v:+.2f}%" for s, v in wf["oos_pct"]["fija"].items()) + ".",
                    *[f"- {role}: {st['trades']} trades, acierto {st['win_rate_pct']}%, neto {st['avg_net_pct']:+.3f}%/trade"
                      for role, st in wf["trade_stats"].items()],
                    f"- Tiempo: {time.time() - t0:.0f} s", ""]
        print(f"[full] {iv} listo en {time.time() - t0:.0f} s", file=sys.stderr)
    text = "\n".join(summary)
    (out / "RESUMEN.md").write_text(text, encoding="utf-8")
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Laboratorio de momentos y estrategias de rebote en temporalidades menores.")
    ap.add_argument("mode", choices=("download", "explore", "walk", "grid", "fixed", "full"))
    ap.add_argument("--intervals", default="15m,1h,4h", help="download / full")
    ap.add_argument("--since", default="2023-01-01", help="download: primera vela")
    ap.add_argument("--out-dir", default="data/lab_momentos", help="full: carpeta de informes")
    ap.add_argument("--csv-dir", default="data/candles")
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--horizon-hours", type=float, default=4)
    ap.add_argument("--condition", help="Filtro pandas antes de explorar, p. ej. \"daily_trend > 0 and rsi2 < 10\"")
    ap.add_argument("--trade-from", default="2024-01-01")
    ap.add_argument("--balance", type=float, default=500.0)
    ap.add_argument("--train-months", type=int, default=12)
    ap.add_argument("--test-months", type=int, default=3)
    ap.add_argument("--no-hour-filter", action="store_true")
    ap.add_argument("--combo", help="JSON con la combinación para el modo fixed")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--out-md")
    ap.add_argument("--out-json")
    a = ap.parse_args(argv)
    symbols = [x.strip().upper() for x in a.symbols.split(",") if x.strip()]
    intervals = [x.strip() for x in a.intervals.split(",") if x.strip()]
    if a.mode == "download":
        print("\n".join(download(a.csv_dir, symbols, intervals, a.since)))
        return 0
    if a.mode == "full":
        print(run_full(a.csv_dir, symbols, intervals, a.out_dir, a.trade_from, a.balance, a.workers))
        return 0
    data = load(a.csv_dir, symbols, a.interval)
    if a.mode == "grid":
        t = grid_table(data, a.interval, a.trade_from, a.balance)
        md, payload = grid_md(t, a.interval), t.to_dict("records")
    elif a.mode == "explore":
        tabs = explore(data, a.interval, a.horizon_hours, a.trade_from, a.condition)
        md = explore_md(tabs, a.interval, a.horizon_hours, a.condition)
        payload = {k: v.reset_index().to_dict("records") for k, v in tabs.items()}
    elif a.mode == "walk":
        payload = walk_forward(data, a.interval, None, a.train_months, a.test_months, a.balance, a.trade_from,
                               not a.no_hour_filter, a.workers, progress=lambda m: print(m, file=sys.stderr))
        md = walk_md(payload)
    else:
        combo = dict(DEFAULT_COMBO, **json.loads(a.combo or "{}"))
        rows = []
        for s, df in data.items():
            r = simulate(features(df, a.interval), combo, a.balance, a.trade_from)
            tr = r.pop("trades_df")
            rows.append(dict(symbol=s, **{k: round(float(v), 3) for k, v in r.items()}))
        payload = rows
        cols = ("symbol", "return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor", "avg_net_pct")
        md = "\n".join([f"Regla: {combo_label(combo)}", "", "| " + " | ".join(cols) + " |", "|---" * len(cols) + "|"]
                       + ["| " + " | ".join(str(r[k]) for k in cols) + " |" for r in rows]) + "\n"
    if a.out_md:
        Path(a.out_md).write_text(md, encoding="utf-8")
    if a.out_json:
        Path(a.out_json).write_text(json.dumps(payload, default=str, indent=1), encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
