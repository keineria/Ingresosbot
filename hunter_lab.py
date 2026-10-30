#!/usr/bin/env python3
"""
hunter_lab.py — Laboratorio del "Cazador": rupturas cortas con volumen en ~40 monedas de Binance Spot.

Pregunta: ¿comprar arranques fuertes (ruptura del máximo reciente con volumen alto) en velas de 15 min o 1 h,
con verificaciones anti-trampa y topes duros, gana DESPUÉS de comisiones y en datos que no se usaron para elegir?

Simula UNA cuenta compartida, como operaría el bot de verdad:
  * Capa 1  3 pérdidas seguidas → 15 velas sin operar; 2 seguidas → riesgo a la mitad; caída del día ≥ 3% → sin
            operar hasta el día siguiente (UTC); caída total ≥ 20% desde el máximo → el Cazador se apaga.
  * Esc.    Cierre > máximo de las N velas anteriores y volumen ≥ V veces la media de 20 velas.
  * Fraude  (lo que se puede medir con velas) volumen de 24 h ≥ 5 M USD, la vela de la señal no concentra más
            del 25% del volumen de 24 h, la moneda lleva ≥ 7 días con datos, el cierre no está más de E ATR por
            encima del canal (sin FOMO) y cierra en la mitad alta de la vela. Spread y profundidad del libro no
            existen en el histórico: se verifican solo en el bot en papel.
  * Capa 2  BTC con el cierre diario de ayer sobre su SMA 200 y la moneda sobre su EMA 200.
  * Capa 3  SL a k ATR, riesgo 0,75% del saldo (0,375% con 2 pérdidas), máx. 25% del saldo por moneda,
            máx. 3 posiciones y 6 entradas por día. Salida por SL, trailing a m ATR del máximo, TP opcional
            o tiempo máximo.
  * Ejecución  entrada en la apertura siguiente; comisión 0,08% por orden y deslizamiento 0,05% en contra
            (más que en BTC porque son monedas menores). Si una vela toca SL y TP, cuenta el SL.

Método (sin mirar el futuro al elegir):
  1. Rejilla pequeña de variantes con sentido económico, evaluada SOLO en la primera mitad (entrenamiento).
  2. Las 5 mejores por rentabilidad / caída se miden en la segunda mitad (validación), en el periodo completo
     y en 20 grupos aleatorios de 20 monedas.
  3. Pasa si: completo > 0, validación > 0, ≥ 15 de 20 grupos > 0, caída máxima < 20% y ≥ 40 operaciones.

Advertencia: el universo son monedas que siguen listadas hoy (sesgo de supervivencia, hace ver mejor el
resultado). Por eso el criterio de paso es exigente y luego viene el papel.

Uso (en el VPS, como usuario ingresosbot, desde /opt/Ingresosbot):
  python hunter_lab.py download --csv-dir data/cazador/velas --since 2023-06-01      # ~30-40 min la 1.ª vez
  python hunter_lab.py run --csv-dir data/cazador/velas --out-dir data/cazador/informes
  python hunter_lab.py run ... --intervals 1h --trade-from 2024-01-01 --balance 500
"""

from __future__ import annotations

import argparse
import itertools
import json
import multiprocessing as mp
import sys
import time
import urllib.parse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

import quant_core_engine as q

FEE = 0.0008
SLIP = 0.0005
MIN_NOTIONAL = 5.0            # mínimo de Binance Spot
BARS_PER_DAY = {"15m": 96, "1h": 24}

UNIVERSE = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
            "TRXUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT", "NEARUSDT", "ATOMUSDT", "UNIUSDT", "SUIUSDT", "APTUSDT",
            "ARBUSDT", "OPUSDT", "INJUSDT", "FETUSDT", "ETCUSDT", "FILUSDT", "ICPUSDT", "HBARUSDT", "XLMUSDT",
            "AAVEUSDT", "TIAUSDT", "SEIUSDT", "PEPEUSDT", "SHIBUSDT", "WIFUSDT", "BONKUSDT", "FLOKIUSDT",
            "GALAUSDT", "SANDUSDT", "STXUSDT", "IMXUSDT", "LDOUSDT")


@dataclass(frozen=True)
class Variant:
    interval: str = "1h"
    don_n: int = 48             # ruptura del máximo de las N velas anteriores
    vol_min: float = 2.0        # volumen ≥ V × media de 20 velas
    max_ext_atr: float = 2.0    # cierre como mucho E ATR por encima del canal
    sl_atr: float = 2.0
    trail_atr: float = 4.0
    tp_atr: float = 0.0         # 0 = sin TP fijo
    max_hours: int = 72
    btc_filter: bool = True
    # Topes (los mismos del documento; se pueden variar para pruebas, no se optimizan)
    risk_pct: float = 0.75
    max_pos_pct: float = 25.0
    max_open: int = 3
    max_trades_day: int = 6
    kill_dd_pct: float = 20.0
    min_qvol_24h: float = 5_000_000.0
    max_conc: float = 0.25
    min_age_days: int = 7

    def label(self) -> str:
        tp = f" TP{self.tp_atr:g}" if self.tp_atr else ""
        f = "" if self.btc_filter else " sin-filtro-BTC"
        return (f"{self.interval} ruptura {self.don_n} · vol ≥{self.vol_min:g}x · ext ≤{self.max_ext_atr:g} ATR · "
                f"SL {self.sl_atr:g} · trail {self.trail_atr:g}{tp} · máx {self.max_hours} h{f}")


# ════════════════════════════════════════════════════════════════════════════
# DATOS
# ════════════════════════════════════════════════════════════════════════════
def download(csv_dir: str | Path, symbols, interval: str = "15m", since: str = "2023-06-01",
             pause: float = 0.15) -> list[str]:
    """Velas públicas de Binance hacia adelante desde `since` (o desde la última guardada: es incremental)."""
    import market_fetcher as mf
    Path(csv_dir).mkdir(parents=True, exist_ok=True)
    step = mf.INTERVAL_MS[interval]
    out = []
    for s in symbols:
        path = Path(csv_dir) / f"{s}_{interval}.csv"
        old = q.load_csv(path) if path.exists() else None
        start = (int(old.time.iloc[-1].timestamp() * 1000) + step if old is not None and len(old)
                 else int(pd.Timestamp(since, tz="UTC").timestamp() * 1000))
        rows, now_ms = [], int(time.time() * 1000)
        try:
            while start < now_ms - step:
                data = None
                for host in mf.BINANCE_HOSTS:
                    try:
                        qs = urllib.parse.urlencode({"symbol": s, "interval": interval, "startTime": start,
                                                     "limit": 1000})
                        data = mf._http_get_json(f"{host}/api/v3/klines?{qs}", timeout=20)
                        break
                    except Exception as exc:              # 451 en algunos países: se prueba el otro host
                        print(f"[download] {s} {host}: {exc}", file=sys.stderr)
                if data is None:
                    raise RuntimeError("ningún host respondió")
                if isinstance(data, dict):
                    raise RuntimeError(data.get("msg", data))
                if not data:
                    break
                rows += [list(r[:7]) for r in data]
                start = int(data[-1][0]) + step
                time.sleep(pause)
        except RuntimeError as exc:
            msg = f"{s}: NO descargada ({exc})"
            print(f"[download] {msg}", file=sys.stderr)
            out.append(msg)
            continue
        new = mf._to_frame(rows, now_ms) if rows else None
        df = pd.concat([d for d in (old, new) if d is not None and len(d)], ignore_index=True) \
            if (old is not None or new is not None) else None
        if df is None or not len(df):
            out.append(f"{s}: sin velas (¿no existe en Binance?)")
            continue
        df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
        df.to_csv(path, index=False)
        msg = f"{path.name}: {len(df):,} velas {df.time.iloc[0]:%Y-%m-%d} → {df.time.iloc[-1]:%Y-%m-%d %H:%M}"
        print(f"[download] {msg}", file=sys.stderr)
        out.append(msg)
    return out


def resample(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    rule = {"15m": "15min", "1h": "1h", "1d": "1D"}[interval]
    g = df.set_index("time").resample(rule, label="left", closed="left")
    out = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(),
                        "close": g.close.last(), "volume": g.volume.sum()}).dropna()
    return out.reset_index()


def load(csv_dir: str | Path, symbols, interval: str) -> dict[str, pd.DataFrame]:
    """<PAR>_<intervalo>.csv; si no existe, se arma desde las velas de 15 min."""
    out = {}
    for s in symbols:
        p = Path(csv_dir) / f"{s}_{interval}.csv"
        p15 = Path(csv_dir) / f"{s}_15m.csv"
        if p.exists():
            out[s] = q.load_csv(p)
        elif p15.exists():
            out[s] = resample(q.load_csv(p15), interval)
        else:
            print(f"[load] falta {p.name}: se omite {s}", file=sys.stderr)
    if not out:
        raise SystemExit(f"No hay velas en {csv_dir}. Corre primero: python hunter_lab.py download")
    return out


class Market:
    """Matrices tiempo × moneda con todo lo que no depende de la variante (se calcula una vez)."""

    def __init__(self, data: dict[str, pd.DataFrame], interval: str, btc: pd.DataFrame | None = None):
        self.interval = interval
        self.symbols = list(data)
        bpd = BARS_PER_DAY[interval]
        idx = pd.DatetimeIndex(sorted(set().union(*(d.time for d in data.values()))))
        self.time = idx
        cols = {k: [] for k in ("open", "high", "low", "close", "atr", "ema200", "vol_ratio", "qvol24",
                                "conc", "age", "ibs")}
        self._raw = {}
        for s in self.symbols:
            d = data[s].set_index("time")
            ind = q.compute_indicators(data[s], q.Params()).set_index("time")
            vol = d.volume
            qv = vol * d.close
            cols["open"].append(d.open)
            cols["high"].append(d.high)
            cols["low"].append(d.low)
            cols["close"].append(d.close)
            cols["atr"].append(ind.atr14)
            cols["ema200"].append(ind.ema200)
            cols["vol_ratio"].append(vol / vol.rolling(20).mean().shift(1))
            q24 = qv.rolling(bpd, min_periods=bpd).sum()
            cols["qvol24"].append(q24)
            cols["conc"].append(qv / q24)
            cols["age"].append(pd.Series(np.arange(len(d)) / bpd, index=d.index))
            rng = (d.high - d.low)
            cols["ibs"].append(((d.close - d.low) / rng).where(rng > 0, 0.5))
            self._raw[s] = d.high
        for k, series in cols.items():
            m = pd.concat(series, axis=1, keys=self.symbols).reindex(idx)
            setattr(self, k, m.to_numpy(float))
        self._don = {}
        # Filtro de BTC: cierre diario de AYER sobre su SMA 200 (se conoce desde las 00:00 UTC de hoy).
        src = btc if btc is not None else data.get("BTCUSDT")
        if src is None:
            self.btc_ok = np.ones(len(idx), bool)
        else:
            daily = resample(src, "1d").set_index("time").close
            ok = (daily > daily.rolling(200).mean()).astype(float)
            ok.index = ok.index + pd.Timedelta(days=1)
            day = idx.floor("D")
            self.btc_ok = ok.reindex(day).fillna(0).to_numpy() > 0
        self.day = pd.factorize(idx.floor("D"))[0]          # n.º de día UTC (no depende de la unidad del índice)

    def donchian(self, n: int) -> np.ndarray:
        if n not in self._don:
            m = pd.concat([self._raw[s].rolling(n).max().shift(1) for s in self.symbols], axis=1,
                          keys=self.symbols).reindex(self.time)
            self._don[n] = m.to_numpy(float)
        return self._don[n]

    def signals(self, v: Variant) -> np.ndarray:
        don = self.donchian(v.don_n)
        c, a = self.close, self.atr
        with np.errstate(invalid="ignore", divide="ignore"):
            sig = ((c > don) & (self.vol_ratio >= v.vol_min) & ((c - don) / a <= v.max_ext_atr)
                   & (c > self.ema200) & (self.qvol24 >= v.min_qvol_24h) & (self.conc <= v.max_conc)
                   & (self.age >= v.min_age_days) & (self.ibs >= 0.5) & (a > 0))
        if v.btc_filter:
            sig &= self.btc_ok[:, None]
        return sig


# ════════════════════════════════════════════════════════════════════════════
# SIMULACIÓN DE LA CUENTA
# ════════════════════════════════════════════════════════════════════════════
def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def simulate(m: Market, v: Variant, balance: float = 500.0, t_from=None, t_to=None, cols=None,
             keep_trades: bool = False) -> dict:
    """Cuenta única sobre las monedas `cols` (índices; None = todas) entre t_from y t_to."""
    sig = m.signals(v)
    if cols is not None:
        mask = np.zeros(sig.shape[1], bool)
        mask[list(cols)] = True
        sig = sig & mask[None, :]
    o, h, l, c, a, vr = m.open, m.high, m.low, m.close, m.atr, m.vol_ratio
    T = len(m.time)
    lo = 0 if t_from is None else int(m.time.searchsorted(_ts(t_from)))
    hi = T if t_to is None else int(m.time.searchsorted(_ts(t_to)))
    bpd = BARS_PER_DAY[m.interval]
    max_bars = int(v.max_hours * bpd / 24)
    start_bal = bal = peak = balance
    day_cur, day_peak, day_halt, trades_day = None, bal, False, 0
    losses, halt_until, killed, killed_at = 0, -1, False, None
    pos: dict[int, dict] = {}
    trades, eq_min_dd, fees = [], 0.0, 0.0
    eq_peak = bal
    for t in range(lo, hi):
        if m.day[t] != day_cur:
            day_cur, day_peak, day_halt, trades_day = m.day[t], bal, False, 0
        # 1) salidas de lo abierto
        for s in list(pos):
            p = pos[s]
            if np.isnan(l[t, s]):
                continue
            stop = max(p["sl"], p["trail"])
            px = why = None
            if o[t, s] <= stop and t > p["t_in"]:
                px, why = o[t, s], "SL" if p["trail"] <= p["sl"] else "TRAIL"
            elif l[t, s] <= stop:
                px, why = stop * (1 - SLIP), "SL" if p["trail"] <= p["sl"] else "TRAIL"
            elif p["tp"] and h[t, s] >= p["tp"]:
                px, why = (o[t, s] if o[t, s] >= p["tp"] and t > p["t_in"] else p["tp"]), "TP"
            elif t - p["t_in"] >= max_bars or t == hi - 1:
                px, why = c[t, s] * (1 - SLIP), "TIEMPO" if t < hi - 1 else "FIN"
            if px is None:
                p["high"] = max(p["high"], h[t, s])
                if not np.isnan(a[t, s]):
                    p["trail"] = max(p["trail"], p["high"] - v.trail_atr * a[t, s])
                continue
            fee = FEE * p["qty"] * (p["entry"] + px)
            pnl = p["qty"] * (px - p["entry"]) - fee
            bal += pnl
            fees += fee
            del pos[s]
            losses = 0 if pnl > 0 else losses + 1
            if keep_trades:
                trades.append({"symbol": m.symbols[s], "opened": m.time[p["t_in"]], "closed": m.time[t],
                               "entry": p["entry"], "exit": px, "pnl": pnl, "reason": why,
                               "ret_pct": (px / p["entry"] - 1) * 100 - FEE * 100 * (1 + px / p["entry"]),
                               "why_in": p["why_in"]})
            else:
                trades.append(pnl)
            peak = max(peak, bal)
            if losses >= 3:
                halt_until, losses = t + 15, 0
            if (day_peak - bal) / day_peak * 100 >= 3.0:
                day_halt = True
            if not killed and (peak - bal) / peak * 100 >= v.kill_dd_pct:
                killed, killed_at = True, m.time[t]
        day_peak = max(day_peak, bal)
        # 2) caída máxima con precios de cierre (marcado a mercado)
        eq = bal + sum(p["qty"] * (c[t, s] - p["entry"]) for s, p in pos.items() if not np.isnan(c[t, s]))
        eq_peak = max(eq_peak, eq)
        eq_min_dd = max(eq_min_dd, (eq_peak - eq) / eq_peak * 100)
        # 3) entradas: señal al cierre de t → compra en la apertura de t+1
        if t + 1 >= hi or killed or day_halt or t <= halt_until or trades_day >= v.max_trades_day:
            continue
        free = v.max_open - len(pos)
        if free <= 0:
            continue
        cand = [s for s in np.flatnonzero(sig[t]) if s not in pos and not np.isnan(o[t + 1, s])]
        if not cand:
            continue
        cand.sort(key=lambda s: -vr[t, s])
        used = sum(p["qty"] * p["entry"] for p in pos.values())
        for s in cand[:free]:
            if trades_day >= v.max_trades_day:
                break
            entry = o[t + 1, s] * (1 + SLIP)
            sl = entry - v.sl_atr * a[t, s]
            if sl <= 0:
                continue
            rp = v.risk_pct / 2 if losses == 2 else v.risk_pct
            notional = min(bal * v.max_pos_pct / 100, bal * rp / 100 / ((entry - sl) / entry),
                           bal * 0.85 - used)
            if notional < MIN_NOTIONAL:
                continue
            used += notional
            trades_day += 1
            pos[s] = {"t_in": t + 1, "entry": entry, "qty": notional / entry, "sl": sl, "trail": sl,
                      "high": entry, "tp": entry + v.tp_atr * a[t, s] if v.tp_atr else 0.0,
                      "why_in": f"ruptura {v.don_n} · vol {vr[t, s]:.1f}x · "
                                f"ext {(c[t, s] - m.donchian(v.don_n)[t, s]) / a[t, s]:.2f} ATR"}
    pnls = np.array([tr["pnl"] if keep_trades else tr for tr in trades], float)
    wins, loss = pnls[pnls > 0].sum(), -pnls[pnls < 0].sum()
    out = {"return_pct": (bal / start_bal - 1) * 100, "max_dd_pct": eq_min_dd, "trades": len(pnls),
           "win_rate_pct": float((pnls > 0).mean() * 100) if len(pnls) else 0.0,
           "profit_factor": float(wins / loss) if loss > 0 else (float("inf") if wins > 0 else 0.0),
           "fees_usd": fees, "final_balance": bal, "killed_at": str(killed_at) if killed else ""}
    if keep_trades:
        out["trades_df"] = pd.DataFrame(trades)
    return out


# ════════════════════════════════════════════════════════════════════════════
# REJILLA, VALIDACIÓN E INFORME
# ════════════════════════════════════════════════════════════════════════════
def grid(interval: str) -> list[Variant]:
    """Pocas variantes con sentido económico (no miles: así no se ajusta ruido)."""
    if interval == "15m":
        dons, vols, hours = (48, 96), (2.0, 3.0), (24, 72)       # 12 h / 24 h de canal
    else:
        dons, vols, hours = (24, 48, 80), (1.3, 2.0), (48, 168)
    out = []
    for n, vm, ext, sl, tr, mh in itertools.product(dons, vols, (1.0, 2.0), (1.5, 2.0), (3.0, 5.0), hours):
        out.append(Variant(interval=interval, don_n=n, vol_min=vm, max_ext_atr=ext, sl_atr=sl, trail_atr=tr,
                           max_hours=mh))
    return out


def score(r: dict) -> float:
    if r["trades"] < 30:
        return -1e9
    return r["return_pct"] / max(r["max_dd_pct"], 5.0)


def subsets(n_symbols: int, k: int = 20, n: int = 20, seed: int = 7) -> list[list[int]]:
    rng = np.random.default_rng(seed)
    k = min(k, n_symbols)
    return [sorted(rng.choice(n_symbols, k, replace=False).tolist()) for _ in range(n)]


_M: Market | None = None


def _init(m):
    global _M
    _M = m


def _task(args):
    kind, v, kw = args
    r = simulate(_M, v, **kw)
    return kind, v, kw.get("cols"), r


def run_interval(m: Market, trade_from: str, balance: float, workers: int, top: int = 5) -> dict:
    t0, t1 = _ts(trade_from), m.time[-1]
    mid = t0 + (t1 - t0) / 2
    variants = grid(m.interval)
    # fork: los procesos comparten las matrices sin copiarlas (Linux, el VPS).
    ctx = mp.get_context("fork")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_init, initargs=(m,)) as ex:
        train = list(ex.map(_task, [("train", v, {"balance": balance, "t_from": t0, "t_to": mid})
                                    for v in variants], chunksize=2))
        train.sort(key=lambda x: -score(x[3]))
        best = [x[1] for x in train[:top]]
        jobs = []
        for v in best:
            jobs.append(("valid", v, {"balance": balance, "t_from": mid}))
            jobs.append(("full", v, {"balance": balance, "t_from": t0}))
            for g in subsets(len(m.symbols)):
                jobs.append(("group", v, {"balance": balance, "t_from": t0, "cols": tuple(g)}))
        res = list(ex.map(_task, jobs, chunksize=2))
    finals = []
    for v in best:
        tr = next(x[3] for x in train if x[1] == v)
        va = next(x[3] for x in res if x[0] == "valid" and x[1] == v)
        fu = next(x[3] for x in res if x[0] == "full" and x[1] == v)
        gs = [x[3]["return_pct"] for x in res if x[0] == "group" and x[1] == v]
        pos = sum(g > 0 for g in gs)
        passed = (fu["return_pct"] > 0 and va["return_pct"] > 0 and pos >= 15 and fu["max_dd_pct"] < 20
                  and fu["trades"] >= 40)
        finals.append({"variant": v, "train": tr, "valid": va, "full": fu, "groups_pos": pos,
                       "groups_n": len(gs), "groups_median": float(np.median(gs)) if gs else 0.0,
                       "passed": passed})
    return {"interval": m.interval, "mid": mid, "t0": t0, "t1": t1, "train_all": train, "finals": finals}


def _f(x: float, d: int = 1) -> str:
    return "∞" if x == float("inf") else f"{x:+.{d}f}" if d else f"{x:.0f}"


def report_md(results: list[dict], balance: float, n_symbols: int) -> str:
    L = ["# Laboratorio del Cazador", "",
         f"Cuenta única de {balance:,.0f} USD sobre {n_symbols} monedas de Binance Spot. Comisión 0,08% por orden "
         f"y deslizamiento 0,05% en contra. Riesgo 0,75%, máx. 3 posiciones, 25% por moneda, 6 entradas/día, "
         f"Capa 1 completa y apagado al -20%.", "",
         "Pasa si: completo > 0, validación > 0, al menos 15 de 20 grupos de 20 monedas > 0, caída máxima < 20% "
         "y al menos 40 operaciones. Las variantes se eligieron solo con la primera mitad.", ""]
    any_pass = False
    for r in results:
        L += [f"## Velas de {r['interval']}", "",
              f"Entrenamiento {r['t0']:%Y-%m-%d} → {r['mid']:%Y-%m-%d} · validación {r['mid']:%Y-%m-%d} → "
              f"{r['t1']:%Y-%m-%d} · {len(r['train_all'])} variantes probadas.", "",
              "| Variante | Entren. % | Valid. % | Completo % | Caída máx. % | Ops | Acierto % | PF | "
              "Comisiones USD | Grupos > 0 | ¿Pasa? |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for f in r["finals"]:
            fu = f["full"]
            any_pass |= f["passed"]
            off = f" (apagado {fu['killed_at'][:10]})" if fu["killed_at"] else ""
            L.append(f"| {f['variant'].label()} | {_f(f['train']['return_pct'])} | {_f(f['valid']['return_pct'])} | "
                     f"{_f(fu['return_pct'])}{off} | {fu['max_dd_pct']:.1f} | {fu['trades']} | "
                     f"{fu['win_rate_pct']:.0f} | {fu['profit_factor']:.2f} | {fu['fees_usd']:.0f} | "
                     f"{f['groups_pos']}/{f['groups_n']} | {'SÍ' if f['passed'] else 'no'} |")
        pos_train = sum(x[3]["return_pct"] > 0 for x in r["train_all"])
        L += ["", f"En entrenamiento, {pos_train} de {len(r['train_all'])} variantes terminaron en positivo.", ""]
    L += ["## Veredicto", "",
          ("Al menos una variante pasa todas las condiciones: se puede construir el Cazador en papel con ella."
           if any_pass else
           "Ninguna variante pasa todas las condiciones: el Cazador NO se construye con estas reglas. "
           "Se descarta igual que la rotación por momentum y la compra de caídas."), ""]
    return "\n".join(L)


def run(csv_dir: str, symbols, intervals, out_dir: str, trade_from: str, balance: float, workers: int) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    results, n_sym = [], 0
    for iv in intervals:
        data = load(csv_dir, symbols, iv)
        btc = data.get("BTCUSDT")
        m = Market(data, iv, btc)
        n_sym = len(m.symbols)
        print(f"[run] {iv}: {n_sym} monedas, {len(m.time):,} velas", file=sys.stderr)
        r = run_interval(m, trade_from, balance, workers)
        results.append(r)
        rows = [{"label": v.label(), **asdict(v), **{k: x for k, x in res.items() if k != "trades_df"}}
                for _, v, _, res in r["train_all"]]
        pd.DataFrame(rows).to_csv(Path(out_dir) / f"entrenamiento_{iv}.csv", index=False)
        for f in r["finals"][:1]:
            full = simulate(m, f["variant"], balance, t_from=trade_from, keep_trades=True)
            full["trades_df"].to_csv(Path(out_dir) / f"operaciones_mejor_{iv}.csv", index=False)
    md = report_md(results, balance, n_sym)
    (Path(out_dir) / "informe_cazador.md").write_text(md, encoding="utf-8")
    best = [f for r in results for f in r["finals"] if f["passed"]]
    if best:
        best.sort(key=lambda f: -score(f["full"]))
        (Path(out_dir) / "cazador_params_candidato.json").write_text(
            json.dumps(asdict(best[0]["variant"]), indent=2), encoding="utf-8")
    return md


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Laboratorio del Cazador (rupturas cortas en ~40 monedas).")
    ap.add_argument("mode", choices=("download", "run"))
    ap.add_argument("--csv-dir", default="data/cazador/velas")
    ap.add_argument("--out-dir", default="data/cazador/informes")
    ap.add_argument("--symbols", default=",".join(UNIVERSE))
    ap.add_argument("--since", default="2023-06-01", help="download: primera vela")
    ap.add_argument("--intervals", default="15m,1h")
    ap.add_argument("--trade-from", default="2024-01-01")
    ap.add_argument("--balance", type=float, default=500.0)
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args(argv)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if a.mode == "download":
        print("\n".join(download(a.csv_dir, symbols, "15m", a.since)))
    else:
        print(run(a.csv_dir, symbols, [x.strip() for x in a.intervals.split(",")], a.out_dir, a.trade_from,
                  a.balance, a.workers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
