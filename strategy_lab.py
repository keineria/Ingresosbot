#!/usr/bin/env python3
"""
strategy_lab.py — Laboratorio de órdenes simuladas con validación walk-forward.

Responde a "¿qué parámetros habrían ganado dinero en el mercado reciente, sin hacer trampa?":
  1. Parte la historia en ventanas: entrenamiento de N meses y prueba de los M meses siguientes.
  2. En cada ventana prueba todas las combinaciones de la rejilla en todos los pares (órdenes simuladas
     con backtester.run_backtest: Capa 1 -> Capa 2 -> Capa 3, comisión y deslizamiento incluidos).
  3. Elige la combinación con mejor puntuación en ENTRENAMIENTO y la aplica en la PRUEBA, que nunca vio.
  4. Encadena las pruebas: ese es el resultado honesto, fuera de muestra, de "elegir con el pasado".

Todo queda en SQLite (por defecto data/lab_results.db): cada resultado por ventana/par/combinación y cada
orden simulada de las fases de prueba, para poder revisarlo o seguir iterando.

Uso:
  python strategy_lab.py --csv-dir data/candles --symbols BTCUSDT,ETHUSDT,SOLUSDT --interval 4h
  python strategy_lab.py --csv-dir data/candles --interval 1h --train-months 12 --test-months 3 --out-md lab.md
  python strategy_lab.py --fetch --symbols BTCUSDT,ETHUSDT --interval 4h --limit 6000
"""

from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

import backtester as bt
import quant_core_engine as q

# Rejilla amplia del perfil Trend Rider + el oficial y sus variantes de salida del PR #6.
LAB_GRID: list[dict] = (
    [{"strategy": "official"},
     {"strategy": "official", "be_atr_mult": None},
     {"strategy": "official", "allow_short": False},
     {"strategy": "official", "allow_short": False, "be_atr_mult": None}]
    + [{"strategy": "trend", "trend_entry": e, "donchian_len": n, "trend_sl_atr_mult": s,
        "trail_atr_mult": k, "trend_allow_short": sh}
       for e, n, s, k, sh in itertools.product(("breakout", "both"), (20, 55), (2.0, 3.0), (3.0, 4.0, 5.0),
                                               (False, True))]
    # Ruptura Donchian 20 solo con tendencia diaria alcista (cierre diario de ayer > SMA 200 días).
    + [{"strategy": "trend", "donchian_len": 20, "trend_sl_atr_mult": s, "trail_atr_mult": k,
        "daily_sma_len": 200, "trend_exit_on_filter": x}
       for s, k, x in itertools.product((2.0, 3.0), (4.0, 5.0), (False, True))]
    # Filtro diario solo: comprado mientras el cierre diario está sobre la SMA 200 días; si no, en efectivo.
    + [{"strategy": "trend", "trend_entry": "daily_filter", "daily_sma_len": 200, "trend_exit_on_filter": True,
        "trend_sl_atr_mult": s, "trail_atr_mult": k}
       for s, k in itertools.product((2.5, 3.0), (4.0, 6.0, 10.0))]
)
BENCHMARKS = ({"strategy": "official"}, {"strategy": "trend"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, config TEXT NOT NULL, summary TEXT);
CREATE TABLE IF NOT EXISTS lab_results (
    run_id INTEGER NOT NULL, window INTEGER NOT NULL, phase TEXT NOT NULL, period_from TEXT, period_to TEXT,
    symbol TEXT NOT NULL, interval TEXT NOT NULL, variant TEXT NOT NULL, params TEXT NOT NULL, chosen INTEGER,
    return_pct REAL, bh_pct REAL, max_dd_pct REAL, trades INTEGER, win_rate_pct REAL, profit_factor REAL,
    fees_usd REAL);
CREATE TABLE IF NOT EXISTS lab_trades (
    run_id INTEGER NOT NULL, window INTEGER NOT NULL, variant TEXT NOT NULL, symbol TEXT NOT NULL,
    interval TEXT NOT NULL, side TEXT, result TEXT, exit_reason TEXT, entry_price REAL, exit_price REAL,
    pnl_usd REAL, pnl_pct REAL, fees_usd REAL, r_multiple REAL, opened_at TEXT, closed_at TEXT);
"""


def load_csv_dir(folder: str | Path, symbols: list[str], interval: str) -> dict[str, pd.DataFrame]:
    out = {}
    for s in symbols:
        path = Path(folder) / f"{s}_{interval}.csv"
        if not path.exists():
            raise SystemExit(f"No encuentro {path}")
        out[s] = q.load_csv(path)
    return out


def windows(times: pd.Series, train_months: int, test_months: int) -> list[tuple[pd.Timestamp, ...]]:
    """[(train_from, test_from, test_to)] avanzando test_months cada vez."""
    t0, t_end = times.min(), times.max()
    out, start = [], t0
    while True:
        test_from = start + pd.DateOffset(months=train_months)
        test_to = test_from + pd.DateOffset(months=test_months)
        if test_from >= t_end:
            break
        out.append((start, test_from, min(test_to, t_end)))
        if test_to >= t_end:
            break
        start = start + pd.DateOffset(months=test_months)
    return out


def _slice(df: pd.DataFrame, t_to) -> pd.DataFrame:
    """Toda la historia anterior a t_to: lo previo a la ventana solo calienta indicadores (trade_from)."""
    return df[df.time < t_to].reset_index(drop=True)


BASE = q.Params(price_tick=0.0001)     # tick fino: con 0.01 el SL de XRP (~1 USD) se redondearía un 1%


def _run(task):
    combo, symbol, df, cfg, t_from = task
    p = bt._params_for(BASE, combo)
    try:
        out = bt.run_backtest(df, p, cfg, symbol, trade_from=t_from)
    except ValueError:
        return None
    r = out["report"]["results"]
    return {"return_pct": r["net_return_pct"], "bh_pct": r["buy_and_hold_return_pct"],
            "max_dd_pct": r["max_drawdown_pct"], "trades": r["trades"], "win_rate_pct": r["win_rate_pct"],
            "profit_factor": r["profit_factor"], "fees_usd": r["total_fees_usd"], "trades_list": out["trades"]}


def score(results: list[dict]) -> float:
    """Puntuación de entrenamiento entre pares: retorno medio menos media caída máxima (premia ganar
    en todos los pares sin hundirse). Sin trades = -inf (no se elige lo que no opera)."""
    if not results or any(r is None for r in results) or sum(r["trades"] for r in results) == 0:
        return float("-inf")
    rets = np.array([r["return_pct"] for r in results])
    dds = np.array([r["max_dd_pct"] for r in results])
    return float(rets.mean() - 0.5 * dds.mean() + 0.5 * rets.min())


def walk_forward(data: dict[str, pd.DataFrame], interval: str, grid: list[dict], train_months: int = 12,
                 test_months: int = 3, cfg: bt.BacktestConfig | None = None, db: str = "data/lab_results.db",
                 workers: int | None = None, progress=print) -> dict:
    cfg = cfg or bt.BacktestConfig()
    cfg = bt.BacktestConfig(cfg.initial_balance, cfg.fee_pct, cfg.slippage_pct, ":memory:")
    all_times = pd.concat([d.time for d in data.values()])
    wins = windows(all_times, train_months, test_months)
    if not wins:
        raise SystemExit("Historia demasiado corta para una ventana de entrenamiento + prueba.")
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    config = {"interval": interval, "symbols": list(data), "train_months": train_months,
              "test_months": test_months, "grid_size": len(grid), "fee_pct": cfg.fee_pct,
              "balance": cfg.initial_balance,
              "slippage_pct": cfg.slippage_pct}
    run_id = con.execute("INSERT INTO lab_runs (created_at, config) VALUES (?, ?)",
                         (pd.Timestamp.now(tz="UTC").isoformat(), json.dumps(config))).lastrowid
    con.commit()

    labels = {json.dumps(c, sort_keys=True): bt.combo_label(c) for c in grid + list(BENCHMARKS)}
    oos = {"chosen": {s: [] for s in data}, "official": {s: [] for s in data}, "trend": {s: [] for s in data},
           "bh": {s: [] for s in data}}
    per_window = []
    t_start = time.time()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for w, (tr_from, te_from, te_to) in enumerate(wins, 1):
            train = {s: _slice(d, te_from) for s, d in data.items()}
            test = {s: _slice(d, te_to + pd.Timedelta(seconds=1)) for s, d in data.items()}
            tasks = [(c, s, train[s], cfg, tr_from) for c in grid for s in data]
            res = list(pool.map(_run, tasks, chunksize=4))
            by_combo: dict[str, list] = {}
            rows = []
            for (c, s, _, _, _), r in zip(tasks, res):
                key = json.dumps(c, sort_keys=True)
                by_combo.setdefault(key, []).append(r)
                if r:
                    rows.append((run_id, w, "train", str(tr_from), str(te_from), s, interval, labels[key], key, 0,
                                 r["return_pct"], r["bh_pct"], r["max_dd_pct"], r["trades"], r["win_rate_pct"],
                                 r["profit_factor"], r["fees_usd"]))
            best_key = max(by_combo, key=lambda k: score(by_combo[k]))
            best = json.loads(best_key)

            test_combos = {best_key: "chosen"}
            for b, name in zip(BENCHMARKS, ("official", "trend")):
                test_combos.setdefault(json.dumps(b, sort_keys=True), name)
            ttasks = [(json.loads(k), s, test[s], cfg, te_from) for k in test_combos for s in data]
            tres = list(pool.map(_run, ttasks))
            win_info = {"window": w, "train": f"{tr_from:%Y-%m-%d} → {te_from:%Y-%m-%d}",
                        "test": f"{te_from:%Y-%m-%d} → {te_to:%Y-%m-%d}", "chosen": labels[best_key],
                        "train_score": round(score(by_combo[best_key]), 2), "results": {}}
            for (c, s, _, _, _), r in zip(ttasks, tres):
                key = json.dumps(c, sort_keys=True)
                roles = [test_combos[key]] + (["official"] if key == json.dumps(BENCHMARKS[0], sort_keys=True)
                                              and test_combos[key] == "chosen" else []) \
                    + (["trend"] if key == json.dumps(BENCHMARKS[1], sort_keys=True)
                       and test_combos[key] == "chosen" else [])
                ret = r["return_pct"] if r else 0.0
                for role in roles:
                    oos[role][s].append(ret)
                if test_combos[key] == "chosen":
                    oos["bh"][s].append(r["bh_pct"] if r else 0.0)
                    win_info["results"][s] = ret
                if r:
                    rows.append((run_id, w, "test", str(te_from), str(te_to), s, interval, labels[key], key,
                                 int(key == best_key), r["return_pct"], r["bh_pct"], r["max_dd_pct"], r["trades"],
                                 r["win_rate_pct"], r["profit_factor"], r["fees_usd"]))
                    con.executemany(
                        "INSERT INTO lab_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [(run_id, w, labels[key], s, interval, t["side"], t["result"], t["exit_reason"],
                          t["entry_price"], t["exit_price"], t["pnl_usd"], t["pnl_pct"], t["fees_usd"],
                          t["r_multiple"], t["opened_at"], t["closed_at"]) for t in r["trades_list"]])
            con.executemany("INSERT INTO lab_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            rows)
            con.commit()
            per_window.append(win_info)
            progress(f"[lab] ventana {w}/{len(wins)} {win_info['test']}: elegida «{win_info['chosen']}» "
                     f"→ {win_info['results']} ({time.time() - t_start:.0f}s)")

    def chain(rs):
        return round((np.prod([1 + x / 100 for x in rs]) - 1) * 100, 2) if rs else None

    summary = {"run_id": run_id, "config": config, "windows": per_window,
               "out_of_sample_pct": {role: {s: chain(v) for s, v in d.items()} for role, d in oos.items()},
               "positive_windows": {role: {s: f"{sum(x > 0 for x in v)}/{len(v)}" for s, v in d.items()}
                                    for role, d in oos.items() if role != "bh"}}
    con.execute("UPDATE lab_runs SET summary = ? WHERE run_id = ?", (json.dumps(summary, default=str), run_id))
    con.commit()
    con.close()
    return summary


def fixed_params_table(db: str, run_id: int) -> pd.DataFrame:
    """Cada combinación con parámetros FIJOS (sin elegir): retorno de entrenamiento encadenado por par.
    Sirve para ver qué zonas de parámetros son estables, no para elegir."""
    con = sqlite3.connect(db)
    df = pd.read_sql_query("SELECT * FROM lab_results WHERE run_id = ? AND phase = 'train'", con, params=(run_id,))
    con.close()
    g = df.groupby("variant").agg(mean_return_pct=("return_pct", "mean"), median_return_pct=("return_pct", "median"),
                                  pct_positive=("return_pct", lambda x: round(float((x > 0).mean() * 100), 1)),
                                  mean_dd_pct=("max_dd_pct", "mean"), trades=("trades", "sum"))
    return g.sort_values("median_return_pct", ascending=False).round(2)


def markdown(summary: dict, fixed: pd.DataFrame | None = None, top: int = 12) -> str:
    c = summary["config"]
    oos = summary["out_of_sample_pct"]
    pos = summary["positive_windows"]
    lines = [f"# Laboratorio walk-forward ({c['interval']}, {', '.join(c['symbols'])})", "",
             f"Entrenamiento {c['train_months']} meses → prueba {c['test_months']} meses, avanzando. "
             f"{c['grid_size']} combinaciones por ventana. Comisión {c['fee_pct']}% por orden, deslizamiento "
             f"{c['slippage_pct']}%. Cada prueba arranca con {c['balance']:,.0f} USD; los retornos se encadenan.", "",
             "## Resultado fuera de muestra (lo que habría pasado eligiendo con el pasado)", "",
             "| Par | Elegida en cada ventana | Ventanas +| Oficial fijo | Ventanas + | Trend Rider fijo | Ventanas + | Buy & Hold |",
             "|---|---|---|---|---|---|---|---|"]
    for s in c["symbols"]:
        def f(v):
            return "—" if v is None else f"{v:+.2f}%"
        lines.append(f"| {s} | {f(oos['chosen'][s])} | {pos['chosen'][s]} | {f(oos['official'][s])} | "
                     f"{pos['official'][s]} | {f(oos['trend'][s])} | {pos['trend'][s]} | {f(oos['bh'][s])} |")
    lines += ["", "## Ventanas", "", "| # | Entrenamiento | Prueba | Combinación elegida | " +
              " | ".join(c["symbols"]) + " |", "|---|---|---|---|" + "---|" * len(c["symbols"])]
    for w in summary["windows"]:
        lines.append(f"| {w['window']} | {w['train']} | {w['test']} | {w['chosen']} | " +
                     " | ".join(f"{w['results'].get(s, 0):+.2f}%" for s in c["symbols"]) + " |")
    if fixed is not None:
        lines += ["", f"## Zonas estables: combinaciones fijas en todas las ventanas de entrenamiento (top {top})", "",
                  "| Combinación | Retorno medio | Mediana | % ventanas×pares en positivo | DD medio | Trades |",
                  "|---|---|---|---|---|---|"]
        for v, r in fixed.head(top).iterrows():
            lines.append(f"| {v} | {r.mean_return_pct:+.2f}% | {r.median_return_pct:+.2f}% | {r.pct_positive}% | "
                         f"{r.mean_dd_pct:.2f}% | {int(r.trades)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Walk-forward con órdenes simuladas y resultados en SQLite.")
    ap.add_argument("--csv-dir", help="Carpeta con <PAR>_<intervalo>.csv")
    ap.add_argument("--fetch", action="store_true", help="Descarga con market_fetcher (usa --limit)")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--interval", default="4h")
    ap.add_argument("--limit", type=int, default=6000)
    ap.add_argument("--train-months", type=int, default=12)
    ap.add_argument("--test-months", type=int, default=3)
    ap.add_argument("--grid", help="JSON [{combinación}, ...]; por defecto LAB_GRID")
    ap.add_argument("--balance", type=float, default=500.0, help="Capital inicial de cada prueba (USD)")
    ap.add_argument("--fee", type=float, default=0.08)
    ap.add_argument("--slippage", type=float, default=0.02)
    ap.add_argument("--db", default="data/lab_results.db")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--out-md")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.csv_dir:
        data = load_csv_dir(args.csv_dir, symbols, args.interval)
    elif args.fetch:
        from market_fetcher import fetch_candles
        data = {s: fetch_candles(s, args.interval, args.limit, verbose=True)[0] for s in symbols}
    else:
        raise SystemExit("Indica --csv-dir o --fetch")
    grid = json.loads(args.grid) if args.grid else LAB_GRID
    summary = walk_forward(data, args.interval, grid, args.train_months, args.test_months,
                           bt.BacktestConfig(args.balance, args.fee, args.slippage), args.db, args.workers,
                           progress=lambda m: print(m, file=sys.stderr))
    md = markdown(summary, fixed_params_table(args.db, summary["run_id"]))
    if args.out_md:
        Path(args.out_md).write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
