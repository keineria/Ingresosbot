#!/usr/bin/env python3
"""
sentiment_lab.py — ¿Mejora Trend Rider si pausamos entradas por sentimiento o por días de noticias macro?

Prueba filtros externos GRATIS que entran a la Capa 1 (Circuit Breaker) como `external_risk`, sin cambiar el
orden de capas: "halt" bloquea entradas nuevas mientras dure (las posiciones abiertas siguen con su stop) y
"throttle" baja el riesgo de 1.5% a 0.75%. Cada filtro se corre en una cuenta compartida de 500 USD con todas
las combinaciones de 2 y 3 pares (backtester.run_portfolio_backtest), igual que el bot 24/7.

Fuentes:
  * Fear & Greed diario de alternative.me (gratis, sin clave). El valor del día D sale a las 00:00 UTC de D;
    la vela que abre el día D usa el valor de D (o el de D-1 con --lag 1, más conservador).
  * Calendario macro fijo: días de decisión de la FED (FOMC) y de inflación de EE. UU. (CPI) 2024-2026.

Resultado de referencia (2024-01 a 2026-10, ver docs/FILTRO_SENTIMIENTO.md): ningún filtro mejora de forma
robusta; el que parece ayudar con BTC+ETH+SOL empeora la media de los 20 grupos.

Uso (en el VPS, donde alternative.me y Binance responden):
  python sentiment_lab.py --csv-dir data/candles --out-md data/filtro_sentimiento.md
  python sentiment_lab.py --csv-dir data/candles --fng-csv data/fear_greed.csv --symbols BTCUSDT,ETHUSDT,SOLUSDT
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

import backtester as bt
import quant_core_engine as q
from strategy_lab import load_csv_dir

FNG_URL = "https://api.alternative.me/fng/?limit=0&format=json"
# Día de la decisión de tipos de la FED (segundo día de la reunión) y día de publicación del CPI de EE. UU.
FOMC_DAYS = ("2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31", "2024-09-18", "2024-11-07",
             "2024-12-18", "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17",
             "2025-10-29", "2025-12-10", "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
             "2026-09-16", "2026-10-28", "2026-12-09")
CPI_DAYS = ("2024-01-11", "2024-02-13", "2024-03-12", "2024-04-10", "2024-05-15", "2024-06-12", "2024-07-11",
            "2024-08-14", "2024-09-11", "2024-10-10", "2024-11-13", "2024-12-11", "2025-01-15", "2025-02-12",
            "2025-03-12", "2025-04-10", "2025-05-13", "2025-06-11", "2025-07-15", "2025-08-12", "2025-09-11",
            "2025-10-24", "2025-12-18")
NO_GATE = {"level": "none", "reasons": []}


def fetch_fear_greed(url: str = FNG_URL, timeout: float = 20.0) -> pd.Series:
    req = urllib.request.Request(url, headers={"User-Agent": "Ingresosbot/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        rows = json.loads(resp.read().decode("utf-8"))["data"]
    s = pd.Series({pd.Timestamp(int(r["timestamp"]), unit="s").normalize(): float(r["value"]) for r in rows})
    return _daily(s)


def load_fear_greed_csv(path: str | Path) -> pd.Series:
    """CSV con columnas date + v (o value). Fechas ISO o dd-mm-aaaa (el formato CSV de alternative.me)."""
    df = pd.read_csv(path)
    col = "v" if "v" in df.columns else "value" if "value" in df.columns else "fng_value"
    dates = pd.to_datetime(df["date"], dayfirst=not str(df["date"].iloc[0])[:4].isdigit())
    return _daily(pd.Series(df[col].astype(float).to_numpy(), index=dates.dt.normalize()))


def _daily(s: pd.Series) -> pd.Series:
    s = s[~s.index.duplicated()].sort_index()
    return s.reindex(pd.date_range(s.index.min(), s.index.max())).ffill()   # algún día falta en la fuente


class Gate:
    """Filtro externo para run_portfolio_backtest: (apertura de la vela) -> {"level", "reasons"}.
    kind: greed_halt | greed_throttle | fear_halt | shock | events. Picklable para correr en paralelo."""

    def __init__(self, kind: str, fng: pd.Series | None = None, x: float = 0.0, lag: int = 0, days=()):
        self.kind, self.fng, self.x, self.lag = kind, fng, x, lag
        self.days = {pd.Timestamp(d).date() for d in days}

    def _fng(self, t: pd.Timestamp, extra_lag: int = 0) -> float:
        return float(self.fng.get(t.normalize() - pd.Timedelta(days=self.lag + extra_lag), float("nan")))

    def __call__(self, t) -> dict:
        t = pd.Timestamp(t)
        t = t.tz_convert(None) if t.tzinfo else t
        if self.kind == "events":
            # Todo el día UTC del dato y la primera vela 4h del día siguiente (reacción del mercado asiático).
            if t.date() in self.days or ((t - pd.Timedelta(hours=4)).date() in self.days and t.hour < 4):
                return {"level": "halt", "reasons": ["día de FOMC/CPI"]}
            return NO_GATE
        v = self._fng(t)
        if v != v:   # NaN: sin dato, no filtra
            return NO_GATE
        if self.kind == "greed_halt" and v >= self.x:
            return {"level": "halt", "reasons": [f"Fear & Greed {v:.0f} >= {self.x:.0f}"]}
        if self.kind == "greed_throttle" and v >= self.x:
            return {"level": "throttle", "reasons": [f"Fear & Greed {v:.0f} >= {self.x:.0f}"]}
        if self.kind == "fear_halt" and v <= self.x:
            return {"level": "halt", "reasons": [f"Fear & Greed {v:.0f} <= {self.x:.0f}"]}
        if self.kind == "shock":
            prev = self._fng(t, 1)
            if prev == prev and abs(v - prev) >= self.x:
                return {"level": "halt", "reasons": [f"Fear & Greed saltó {prev:.0f} -> {v:.0f}"]}
        return NO_GATE


def default_gates(fng: pd.Series) -> dict[str, Gate | None]:
    g: dict[str, Gate | None] = {"sin filtro": None}
    for x in (70, 75, 80, 85):
        g[f"pausa si F&G >= {x}"] = Gate("greed_halt", fng, x)
    g["pausa si F&G >= 75 (dato de ayer)"] = Gate("greed_halt", fng, 75, lag=1)
    for x in (70, 75):
        g[f"riesgo 0.75% si F&G >= {x}"] = Gate("greed_throttle", fng, x)
    for x in (15, 25):
        g[f"pausa si F&G <= {x}"] = Gate("fear_halt", fng, x)
    g["pausa si F&G salta >= 15 pts"] = Gate("shock", fng, 15)
    g["pausa días FOMC"] = Gate("events", days=FOMC_DAYS)
    g["pausa días FOMC + CPI"] = Gate("events", days=FOMC_DAYS + CPI_DAYS)
    return g


def _run(task):
    name, gate, group, data, p, cfg, trade_from = task
    r = bt.run_portfolio_backtest({s: data[s] for s in group}, p, cfg, trade_from, risk_gate=gate)["report"]
    return {"filtro": name, "grupo": "+".join(s.removesuffix("USDT") for s in group),
            "retorno_pct": r["net_return_pct"], "caida_max_pct": r["max_drawdown_pct"], "trades": r["trades"],
            "senales_bloqueadas": r["blocked_by_gate"]}


def run_lab(data: dict[str, pd.DataFrame], gates: dict, p: q.Params, cfg: bt.BacktestConfig,
            trade_from: str, group_sizes=(2, 3), workers: int | None = None) -> pd.DataFrame:
    syms = list(data)
    groups = [c for k in group_sizes for c in itertools.combinations(syms, k)] or [tuple(syms)]
    tasks = [(n, g, grp, data, p, cfg, trade_from) for n, g in gates.items() for grp in groups]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return pd.DataFrame(list(ex.map(_run, tasks)))


def summarize(rows: pd.DataFrame) -> pd.DataFrame:
    base = rows[rows.filtro == "sin filtro"].set_index("grupo")
    out = []
    for name, g in rows.groupby("filtro", sort=False):
        g = g.set_index("grupo")
        diff = g.retorno_pct - base.retorno_pct
        out.append({"filtro": name, "retorno_medio_pct": round(g.retorno_pct.mean(), 1),
                    "vs_sin_filtro_pct": round(diff.mean(), 1), "grupos_mejor": int((diff > 0).sum()),
                    "grupos_peor": int((diff < 0).sum()), "peor_grupo_pct": g.retorno_pct.min(),
                    "caida_max_media_pct": round(g.caida_max_pct.mean(), 1),
                    "senales_bloqueadas": int(g.senales_bloqueadas.sum())})
    return pd.DataFrame(out)


def markdown(summary: pd.DataFrame, n_groups: int, trade_from: str) -> str:
    lines = [f"# Filtros de sentimiento y noticias macro sobre Trend Rider (desde {trade_from}, {n_groups} grupos)", "",
             "| Filtro | Retorno medio | vs sin filtro | Grupos mejor / peor | Peor grupo | Caída máx. media | "
             "Señales bloqueadas |", "|---|---|---|---|---|---|---|"]
    for r in summary.itertuples():
        lines.append(f"| {r.filtro} | {r.retorno_medio_pct:+.1f}% | {r.vs_sin_filtro_pct:+.1f} pts | "
                     f"{r.grupos_mejor} / {r.grupos_peor} | {r.peor_grupo_pct:+.1f}% | {r.caida_max_media_pct:.1f}% | "
                     f"{r.senales_bloqueadas} |")
    lines += ["", "Un filtro solo vale si mejora la mayoría de los grupos y no solo uno (eso sería suerte)."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Filtros externos (Fear & Greed, FOMC/CPI) sobre Trend Rider.")
    ap.add_argument("--csv-dir", default="data/candles", help="Carpeta con SYMBOL_4h.csv")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT")
    ap.add_argument("--interval", default="4h")
    ap.add_argument("--params", default="deploy/trend_params.json", help="JSON de parámetros (Trend Rider)")
    ap.add_argument("--fng-csv", help="Fear & Greed desde un CSV en vez de descargarlo de alternative.me")
    ap.add_argument("--balance", type=float, default=500.0)
    ap.add_argument("--trade-from", default="2024-01-01")
    ap.add_argument("--groups", default="2,3", help="Tamaños de grupo; '' = todos los pares juntos")
    ap.add_argument("--out-md")
    ap.add_argument("--out-csv")
    args = ap.parse_args(argv)

    fng = load_fear_greed_csv(args.fng_csv) if args.fng_csv else fetch_fear_greed()
    print(f"[sentiment_lab] Fear & Greed: {fng.index.min().date()} -> {fng.index.max().date()}", file=sys.stderr)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    data = load_csv_dir(args.csv_dir, symbols, args.interval)
    raw = Path(args.params).read_text(encoding="utf-8") if Path(args.params).exists() else args.params
    p = q.Params(**json.loads(raw))
    sizes = tuple(int(x) for x in args.groups.split(",") if x.strip())
    rows = run_lab(data, default_gates(fng), p, bt.BacktestConfig(args.balance, 0.08, 0.02), args.trade_from, sizes)
    summary = summarize(rows)
    md = markdown(summary, rows.grupo.nunique(), args.trade_from)
    if args.out_csv:
        rows.to_csv(args.out_csv, index=False)
    if args.out_md:
        Path(args.out_md).write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
