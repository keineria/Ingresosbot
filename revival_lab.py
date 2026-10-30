"""Laboratorio para reactivar Trend Rider (Keiner, 4h) y Tortuga 55 (Celeste, 1d) en un mercado bajista.

Pregunta: ¿qué cambio les da más operaciones SIN perder más capital? Cada variante se corre con el motor real
(backtester.run_portfolio_backtest: Capa 1 → 2 → 3, una cuenta compartida, 500 USD, comisión 0,08% y
deslizamiento 0,02% por orden) en varias ventanas independientes, cada una empezando con el mismo balance:
el año bajista 2022, 2023, 2024, 2025, los últimos 12 meses y los últimos 3 meses. Una variante solo se acepta
si gana o empata en las ventanas bajistas y no sube la caída máxima.

Uso (CSV <PAR>_4h.csv / <PAR>_1d.csv en --csv-dir, p. ej. bajados con intraday_lab.py download):
    python revival_lab.py --csv-dir data/lab4/velas --out-md data/lab4/REACTIVAR.md
    python revival_lab.py --csv-dir ... --bot trend --only base,u12      # subconjunto
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

import backtester as bt
import quant_core_engine as q

CORE = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TORTUGA = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "PAXGUSDT"]
U12 = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "PAXGUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT",
       "TRXUSDT", "AVAXUSDT", "LTCUSDT"]
U23 = U12 + ["DOTUSDT", "BCHUSDT", "NEARUSDT", "ATOMUSDT", "UNIUSDT", "SUIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
             "INJUSDT", "FETUSDT"]

# Parámetros actuales de cada bot. Se leen de deploy/ junto al script o, si se corre una copia (p. ej. en data/lab4
# del VPS), de deploy/ en la carpeta de trabajo.
_DEPLOY = next((d for d in (Path(__file__).parent / "deploy", Path.cwd() / "deploy") if d.is_dir()), Path("deploy"))
TREND_BASE = json.loads((_DEPLOY / "trend_params.json").read_text(encoding="utf-8"))
TORTUGA_BASE = json.loads((_DEPLOY / "tortuga_params.json").read_text(encoding="utf-8"))

# id -> (descripción, pares, cambios sobre los parámetros actuales del bot)
VARIANTS = {
    "trend": {
        "base": ("Como está hoy (BTC ETH SOL)", CORE, {}),
        "u12": ("12 monedas + oro, tope 30% por posición", U12, {"max_position_pct": 30}),
        "u23": ("23 monedas + oro, tope 25% por posición", U23, {"max_position_pct": 25}),
        "sma100": ("Filtro diario SMA100 en vez de SMA200", CORE, {"daily_sma_len": 100}),
        "sma50": ("Filtro diario SMA50 en vez de SMA200", CORE, {"daily_sma_len": 50}),
        "vol": ("Solo con volatilidad alta (rango ATR ≥ 0,5)", CORE, {"trend_min_atr_rank": 0.5}),
        "u12_vol": ("12 monedas + volatilidad alta", U12, {"max_position_pct": 30, "trend_min_atr_rank": 0.5}),
        "u12_sma100": ("12 monedas + SMA100", U12, {"max_position_pct": 30, "daily_sma_len": 100}),
        "u12_sma100_vol": ("12 monedas + SMA100 + volatilidad alta", U12,
                           {"max_position_pct": 30, "daily_sma_len": 100, "trend_min_atr_rank": 0.5}),
        "u23_vol": ("23 monedas + volatilidad alta", U23, {"max_position_pct": 25, "trend_min_atr_rank": 0.5}),
        "u12_adx": ("12 monedas + ADX ≥ 20 (tendencia con fuerza)", U12, {"max_position_pct": 30, "trend_min_adx": 20}),
        "u12_stale": ("12 monedas, fuera si a las 6 velas no sube", U12, {"max_position_pct": 30, "trend_stale_bars": 6}),
        "u12_d30": ("12 monedas, ruptura de 30 velas", U12, {"max_position_pct": 30, "donchian_len": 30}),
        "u12_tight": ("12 monedas, trailing 2,5 ATR tras ganar 2R", U12,
                      {"max_position_pct": 30, "trend_tight_after_r": 2.0}),
        "short_info": ("SOLO INFORMATIVO: también vende en corto (Futuros 1x)", CORE, {"trend_allow_short": True}),
    },
    "tortuga": {
        "base": ("Como está hoy (5 cripto + oro)", TORTUGA, {}),
        "d20": ("Ruptura de 20 días", TORTUGA, {"donchian_len": 20}),
        "d30": ("Ruptura de 30 días", TORTUGA, {"donchian_len": 30}),
        "sma100": ("Filtro diario SMA100", TORTUGA, {"daily_sma_len": 100}),
        "u12": ("12 monedas + oro, tope 30%", U12, {"max_position_pct": 30}),
        "u23": ("23 monedas + oro, tope 25%", U23, {"max_position_pct": 25}),
        "u12_d30": ("12 monedas, ruptura de 30 días", U12, {"max_position_pct": 30, "donchian_len": 30}),
        "u23_d30": ("23 monedas, ruptura de 30 días", U23, {"max_position_pct": 25, "donchian_len": 30}),
        "u12_d30_sma100": ("12 monedas, 30 días, SMA100", U12,
                           {"max_position_pct": 30, "donchian_len": 30, "daily_sma_len": 100}),
        "short_info": ("SOLO INFORMATIVO: también vende en corto (Futuros 1x)", TORTUGA, {"trend_allow_short": True}),
    },
}
INTERVAL = {"trend": "4h", "tortuga": "1d"}


def windows(last: pd.Timestamp) -> list[tuple[str, str, str | None]]:
    """(nombre, desde, hasta). Las ventanas no comparten dinero: cada una empieza con el balance inicial."""
    return [("2022 (bajista)", "2022-01-01", "2023-01-01"), ("2023", "2023-01-01", "2024-01-01"),
            ("2024", "2024-01-01", "2025-01-01"), ("2025", "2025-01-01", "2026-01-01"),
            ("Últ. 12 meses", str((last - pd.DateOffset(months=12)).date()), None),
            ("Últ. 3 meses", str((last - pd.DateOffset(months=3)).date()), None),
            ("Todo 2022→hoy", "2022-01-01", None)]


def load(csv_dir: Path, symbols: list[str], interval: str) -> dict[str, pd.DataFrame]:
    out = {}
    for s in symbols:
        path = csv_dir / f"{s}_{interval}.csv"
        if path.exists():
            out[s] = q.load_csv(path)
        else:
            print(f"[revival] falta {path.name}; se omite {s}", file=sys.stderr)
    return out


def _run(job: tuple) -> dict:
    bot, vid, wname, start, end, csv_dir, balance = job
    desc, symbols, changes = VARIANTS[bot][vid]
    base = TREND_BASE if bot == "trend" else TORTUGA_BASE
    p = q.Params(**{**base, **changes})
    data = load(Path(csv_dir), symbols, INTERVAL[bot])
    if end:
        data = {s: d[d.time < pd.Timestamp(end, tz="UTC")] for s, d in data.items()}
    data = {s: d for s, d in data.items() if len(d) >= p.min_candles + 2}
    rep = bt.run_portfolio_backtest(data, p, bt.BacktestConfig(initial_balance=balance), trade_from=start)["report"]
    months = max((pd.Timestamp(rep["period"]["to"]) - pd.Timestamp(rep["period"]["from"])).days / 30.44, 0.1)
    return {"bot": bot, "variant": vid, "desc": desc, "window": wname, "return_pct": rep["net_return_pct"],
            "dd_pct": rep["max_drawdown_pct"], "trades": rep["trades"], "trades_month": round(rep["trades"] / months, 2),
            "win_rate": rep["win_rate_pct"], "pf": rep["profit_factor"], "fees": rep["total_fees_usd"],
            "skipped": rep["skipped_by_capital"], "by_symbol": rep["by_symbol"], "symbols": len(data)}


def markdown(rows: list[dict], bot: str) -> str:
    df = pd.DataFrame(rows)
    df = df[df.bot == bot]
    wins = list(dict.fromkeys(df.window))
    lines = [f"### {'Keiner · Trend Rider (4h)' if bot == 'trend' else 'Celeste · Tortuga 55 (1d)'}", "",
             "Retorno % · caída máx. % · operaciones por ventana (cada ventana empieza con el mismo balance).", "",
             "| Variante | " + " | ".join(wins) + " | Ops/mes (12m) |", "|---|" + "---|" * (len(wins) + 1)]
    for vid in VARIANTS[bot]:
        g = df[df.variant == vid].set_index("window")
        if g.empty:
            continue
        cells = [f"{g.loc[w, 'return_pct']:+.1f} · {g.loc[w, 'dd_pct']:.1f} · {g.loc[w, 'trades']}" if w in g.index
                 else "—" for w in wins]
        tm = g.loc["Últ. 12 meses", "trades_month"] if "Últ. 12 meses" in g.index else float("nan")
        lines.append(f"| **{vid}** {g['desc'].iloc[0]} | " + " | ".join(cells) + f" | {tm:.1f} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv-dir", required=True)
    ap.add_argument("--bot", choices=("trend", "tortuga", "both"), default="both")
    ap.add_argument("--only", help="ids de variantes separados por coma")
    ap.add_argument("--balance", type=float, default=500.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out-md")
    ap.add_argument("--out-json")
    a = ap.parse_args(argv)
    bots = ["trend", "tortuga"] if a.bot == "both" else [a.bot]
    last = q.load_csv(Path(a.csv_dir) / "BTCUSDT_4h.csv").time.iloc[-1]
    only = set(a.only.split(",")) if a.only else None
    jobs = [(b, v, w, s, e, a.csv_dir, a.balance) for b in bots for v in VARIANTS[b] if not only or v in only
            for w, s, e in windows(last)]
    with ProcessPoolExecutor(a.workers) as ex:
        rows = list(ex.map(_run, jobs))
    md = f"# Reactivar a Keiner y Celeste · datos hasta {last:%Y-%m-%d %H:%M} UTC\n\n" + "\n".join(
        markdown(rows, b) for b in bots)
    print(md)
    if a.out_md:
        Path(a.out_md).write_text(md, encoding="utf-8")
    if a.out_json:
        Path(a.out_json).write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
