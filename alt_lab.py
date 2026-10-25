#!/usr/bin/env python3
"""
alt_lab.py — Laboratorio de estrategias y activos alternativos (docs/ALTERNATIVAS.md).

Dos bloques, ambos con 500 USD, 0.08% de comisión + 0.02% de deslizamiento y corte 70/30 (entrenamiento /
validación que nunca se usó para elegir):

  A. Cartera (velas diarias, cálculo rápido): comprar y mantener, cesta rebalanceada cada mes, filtro de media
     móvil por activo (Faber) y rotación por momentum (las N que más subieron). Sirve para descartar ideas.
  B. Motor real (Capa 1 → Capa 2 → Capa 3, una cuenta compartida, backtester.run_portfolio_backtest):
     Tortuga 55 y sus vecinos (Donchian 20/55, SL 2/3 ATR, trailing 4/5/8 ATR, con y sin SMA200 diaria).

Uso:
  python alt_lab.py --csv-dir data/candles --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT --out-md data/alt_lab.md
  # En el VPS, con más historia y con oro (PAXG): descarga y guarda en data/candles/
  python alt_lab.py --fetch --limit 2500 --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,PAXGUSDT --trade-from 2020-01-01 --out-md data/alt_lab.md
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

import backtester as bt
import quant_core_engine as q

COST = 0.0010                 # 0.08% comisión + 0.02% deslizamiento, por orden
TORTUGA = Path(__file__).resolve().parent / "deploy" / "tortuga_params.json"


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t if t.tzinfo else t.tz_localize("UTC")


# ── Datos ────────────────────────────────────────────────────────────────────
def load_daily(folder: str | Path | None, symbols: list[str], fetch: bool, limit: int) -> dict[str, pd.DataFrame]:
    """Velas diarias por par: de <PAR>_1d.csv, o agregando <PAR>_4h.csv, o descargándolas (--fetch)."""
    out = {}
    for s in symbols:
        if fetch:
            from market_fetcher import default_csv_path, fetch_candles, save_csv
            df, info = fetch_candles(s, "1d", limit, verbose=True)
            if not info["source"].startswith("csv:"):
                save_csv(df, default_csv_path(s, "1d"))
            print(f"[alt_lab] {s}: {len(df)} velas diarias desde {info['source']}", file=sys.stderr)
        else:
            d1, h4 = Path(folder) / f"{s}_1d.csv", Path(folder) / f"{s}_4h.csv"
            if d1.exists():
                df = q.load_csv(d1)
            elif h4.exists():
                h = q.load_csv(h4).set_index("time")
                df = h.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                           "volume": "sum"}).dropna().iloc[:-1].reset_index()
            else:
                print(f"[alt_lab] sin datos de {s} en {folder}: se omite", file=sys.stderr)
                continue
        out[s] = df.reset_index(drop=True)
    return out


# ── A. Cartera (cálculo vectorizado sobre cierres diarios) ───────────────────
def simulate(closes: pd.DataFrame, target: pd.DataFrame, start, end=None, monthly: bool = False) -> pd.Series:
    """Solo se opera la moneda cuyo objetivo pasa de 0 a >0 o al revés (las demás derivan);
    con monthly=True además se reajusta todo al objetivo el primer día de cada mes."""
    c = closes.loc[:end] if end is not None else closes
    rets = c.pct_change().fillna(0.0).values
    tg = target.reindex(c.index).fillna(0.0).values
    i0 = c.index.searchsorted(_utc(start))
    w, eq, curve, prev_on = np.zeros(c.shape[1]), 1.0, [], np.zeros(c.shape[1], bool)
    for i in range(i0, len(c)):
        if i > i0:
            g = w * (1 + rets[i])
            growth = g.sum() + (1 - w.sum())
            w, eq = g / growth, eq * growth
        on = tg[i] > 0
        new_w, changed = w.copy(), on != prev_on
        new_w[changed] = tg[i][changed]
        if monthly and (i == i0 or c.index[i].month != c.index[i - 1].month):
            new_w = tg[i].copy()
        if new_w.sum() > 1:
            new_w = new_w / new_w.sum()
        traded = np.abs(new_w - w).sum()
        eq *= 1 - traded * COST
        w, prev_on = new_w, on
        curve.append(eq)
    return pd.Series(curve, index=c.index[i0:])


def curve_stats(s: pd.Series) -> dict:
    return {"ret": (s.iloc[-1] / s.iloc[0] - 1) * 100, "dd": -(s / s.cummax() - 1).min() * 100}


def portfolio_rows(closes: pd.DataFrame, start, split) -> list[dict]:
    n = closes.shape[1]
    slots = pd.DataFrame(np.where(closes.notna(), 1 / n, 0.0), index=closes.index, columns=closes.columns)

    def sma_on(length):
        return (closes > closes.rolling(length).mean()).astype(float)

    def rotation(look, k):
        mom = closes / closes.shift(look) - 1
        w = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
        cur: list[str] = []
        for t in closes.index:
            if t.dayofweek == 6:                       # se decide los domingos
                m = mom.loc[t].dropna()
                cur = [x for x in m.sort_values(ascending=False).index[:k] if m[x] > 0]
            w.loc[t, cur] = 1 / k
        return w

    variants = [("Comprar y mantener (cesta, rebalanceo mensual)", "cesta", slots, True)]
    variants += [(f"Media móvil {m} días por activo", "sma", slots * sma_on(m), False) for m in (50, 100, 150, 200)]
    variants += [(f"Rotación {lk} días, top {k}", "rotacion", rotation(lk, k), False)
                 for lk, k in itertools.product((28, 56, 90), (1, 2))]
    rows = []
    for name, fam, w, monthly in variants:
        full = curve_stats(simulate(closes, w, start, monthly=monthly))
        tr = curve_stats(simulate(closes, w, start, split, monthly))
        va = curve_stats(simulate(closes, w, split, monthly=monthly))
        rows.append({"bloque": "A", "familia": fam, "variante": name, "ret": full["ret"], "dd": full["dd"],
                     "train": tr["ret"], "valid": va["ret"], "valid_dd": va["dd"]})
    return rows


# ── B. Motor real de 3 capas ─────────────────────────────────────────────────
def engine_rows(data: dict[str, pd.DataFrame], start, split, balance: float) -> list[dict]:
    base = q.Params(**json.loads(TORTUGA.read_text(encoding="utf-8")))
    cfg = bt.BacktestConfig(balance, 0.08, 0.02)
    split_ts = _utc(split)
    rows = []
    for dn, sl, tr, dsma in itertools.product((20, 55), (2.0, 3.0), (4.0, 5.0, 8.0), (0, 200)):
        p = replace(base, donchian_len=dn, trend_sl_atr_mult=sl, trail_atr_mult=tr, daily_sma_len=dsma)
        full = bt.run_portfolio_backtest(data, p, cfg, start)["report"]
        train = bt.run_portfolio_backtest({s: d[d.time < split_ts] for s, d in data.items()}, p, cfg, start)["report"]
        valid = bt.run_portfolio_backtest(data, p, cfg, split_ts)["report"]
        name = f"Donchian {dn} · SL {sl:g} ATR · trailing {tr:g} ATR · {'SMA200 diaria' if dsma else 'sin filtro'}"
        if (dn, sl, tr, dsma) == (base.donchian_len, base.trend_sl_atr_mult, base.trail_atr_mult, base.daily_sma_len):
            name = "**Tortuga 55** · " + name
        rows.append({"bloque": "B", "familia": "tortuga" if dn == 55 else "donchian20", "variante": name,
                     "ret": full["net_return_pct"], "dd": full["max_drawdown_pct"], "trades": full["trades"],
                     "pf": full["profit_factor"], "fees": full["total_fees_usd"], "train": train["net_return_pct"],
                     "valid": valid["net_return_pct"], "valid_dd": valid["max_drawdown_pct"]})
        print(f"[alt_lab] {name}: {full['net_return_pct']:+.1f}%", file=sys.stderr)
    return rows


def markdown(rows: list[dict], symbols: list[str], start, split, end) -> str:
    def f(v, sign=True):
        return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else (f"{v:+.1f}%" if sign else f"{v:.1f}%")

    out = [f"# Laboratorio de alternativas · {', '.join(symbols)}", "",
           f"Opera desde {pd.Timestamp(start).date()} hasta {pd.Timestamp(end).date()}; "
           f"entrenamiento hasta {pd.Timestamp(split).date()} (70%), validación después (30%).", ""]
    for bloque, title in (("A", "A. Cartera (velas diarias)"), ("B", "B. Motor real de 3 capas")):
        sel = [r for r in rows if r["bloque"] == bloque]
        if not sel:
            continue
        out += [f"## {title}", "", "| Variante | Retorno | Caída máx. | Entrenamiento | Validación | Caída en validación |",
                "|---|---|---|---|---|---|"]
        out += [f"| {r['variante']} | {f(r['ret'])} | {f(r['dd'], False)} | {f(r['train'])} | {f(r['valid'])} | "
                f"{f(r['valid_dd'], False)} |" for r in sel]
        both = sum(1 for r in sel if r["train"] > 0 and r["valid"] > 0)
        out += ["", f"Positivas en las dos partes: {both}/{len(sel)}.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Estrategias y activos alternativos con corte 70/30.")
    ap.add_argument("--csv-dir", default="data/candles")
    ap.add_argument("--fetch", action="store_true", help="Descarga velas diarias (Binance Vision) y las guarda")
    ap.add_argument("--limit", type=int, default=2500, help="Velas diarias a descargar con --fetch")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT")
    ap.add_argument("--trade-from", default="2024-01-01", help="Lo anterior solo calienta indicadores")
    ap.add_argument("--train-frac", type=float, default=0.7)
    ap.add_argument("--balance", type=float, default=500.0)
    ap.add_argument("--skip-engine", action="store_true", help="Solo el bloque A (rápido)")
    ap.add_argument("--out-md")
    ap.add_argument("--out-csv")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    data = load_daily(args.csv_dir, symbols, args.fetch, args.limit)
    if not data:
        raise SystemExit("Sin datos")
    closes = pd.DataFrame({s: d.set_index("time")["close"] for s, d in data.items()}).sort_index()
    start = _utc(args.trade_from)
    days = closes.loc[start:].index
    split = days[int(len(days) * args.train_frac)]
    rows = portfolio_rows(closes, start, split)
    if not args.skip_engine:
        rows += engine_rows(data, start, split, args.balance)
    md = markdown(rows, list(data), start, split, days[-1])
    if args.out_md:
        Path(args.out_md).write_text(md, encoding="utf-8")
    if args.out_csv:
        pd.DataFrame(rows).to_csv(args.out_csv, index=False)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
