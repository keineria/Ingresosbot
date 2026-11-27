#!/usr/bin/env python3
"""Laboratorio de SALIDAS: ¿asegurar ganancia antes hace que los bots ganen más (o pierdan menos)?

Queja de Keiner (2026-11-26): "los bots no cierran sus abiertos y solo cierran cuando van en pérdida". La auditoría
(exit_audit.py) mostró que no es un error: el trailing va a 5 ATR, sin take profit ni break-even, así que una
posición con +15% puede volver a la entrada antes de que el stop la proteja. Aquí se mide, con el motor real
(backtester.run_portfolio_backtest: Capa 1 → 2 → 3, una cuenta de 500 USD, comisión 0,08% y deslizamiento 0,02%),
qué pasa con cada bot si se cambia SOLO la salida:

  be        el stop pasa a la entrada (+0,2%) cuando el mejor precio ya va X R a favor (trend_be_after_r)
  parcial   vende una parte con orden límite en entrada + X R y deja correr el resto (trend_partial_at_r/_pct)
  ajuste    el trailing se acerca a k ATR cuando el mejor precio ya va X R a favor (trend_tight_after_r)
  trail     trailing fijo más corto (trail_atr_mult)
  cortos    (solo Nathalia y Trend Rider) también vende en corto en papel (trend_allow_short)

Ventanas: todo 2022→hoy (con el resultado de cada año), últimos 12 meses y últimos 3 meses; cada una empieza con
el mismo balance. Una salida solo se recomienda si no empeora la caída máxima ni el resultado total.

Uso (en el VPS, desde /opt/Ingresosbot; 1 núcleo → --workers 1):
    python exit_lab.py download --csv-dir data/salidas/velas
    python exit_lab.py run --csv-dir data/salidas/velas --out-md data/salidas/SALIDAS.md --out-json data/salidas/salidas.json
    python exit_lab.py run ... --bot tortuga --only base,be1         # subconjunto
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

import backtester as bt
import quant_core_engine as q

_DEPLOY = next((d for d in (Path(__file__).parent / "deploy", Path.cwd() / "deploy") if d.is_dir()), Path("deploy"))


def _bots() -> dict[str, dict]:
    cards = {b["id"]: b for b in json.loads((_DEPLOY / "bots.json").read_text(encoding="utf-8"))}
    out = {}
    for bid, label in (("oficial", "Nathalia · Rupturas 7 monedas (4h)"), ("trend", "Keiner · Trend Rider (4h)"),
                       ("tortuga", "Celeste · Tortuga 30 (1d)")):
        c = cards[bid]
        params = json.loads((_DEPLOY / Path(c["params"]).name).read_text(encoding="utf-8"))
        out[bid] = {"label": label, "interval": c["interval"], "symbols": c["symbols"], "params": params}
    return out


BOTS = _bots()

# id -> (descripción, cambios sobre los parámetros actuales del bot)
EXITS: dict[str, tuple[str, dict]] = {
    "base": ("Como está hoy (trailing 5 ATR, sin TP)", {}),
    "be1": ("Stop a la entrada al ir +1 R", {"trend_be_after_r": 1.0}),
    "be15": ("Stop a la entrada al ir +1,5 R", {"trend_be_after_r": 1.5}),
    "be2": ("Stop a la entrada al ir +2 R", {"trend_be_after_r": 2.0}),
    "p1": ("Vende 50% en +1 R", {"trend_partial_at_r": 1.0, "trend_partial_pct": 50.0}),
    "p15": ("Vende 50% en +1,5 R", {"trend_partial_at_r": 1.5, "trend_partial_pct": 50.0}),
    "p2": ("Vende 33% en +2 R", {"trend_partial_at_r": 2.0, "trend_partial_pct": 33.0}),
    "p3": ("Vende 33% en +3 R", {"trend_partial_at_r": 3.0, "trend_partial_pct": 33.0}),
    "aj15": ("Trailing a 3 ATR al ir +1,5 R", {"trend_tight_after_r": 1.5, "trend_tight_trail_atr": 3.0}),
    "aj2": ("Trailing a 2,5 ATR al ir +2 R", {"trend_tight_after_r": 2.0, "trend_tight_trail_atr": 2.5}),
    "aj3": ("Trailing a 3 ATR al ir +3 R", {"trend_tight_after_r": 3.0, "trend_tight_trail_atr": 3.0}),
    "t4": ("Trailing 4 ATR siempre", {"trail_atr_mult": 4.0}),
    "t3": ("Trailing 3 ATR siempre", {"trail_atr_mult": 3.0}),
    "p15_be1": ("Vende 50% en +1,5 R y stop a la entrada al ir +1 R",
                {"trend_partial_at_r": 1.5, "trend_partial_pct": 50.0, "trend_be_after_r": 1.0}),
    "p2_be15": ("Vende 33% en +2 R y stop a la entrada al ir +1,5 R",
                {"trend_partial_at_r": 2.0, "trend_partial_pct": 33.0, "trend_be_after_r": 1.5}),
    "p2_be15_aj3": ("Vende 33% en +2 R, stop a la entrada al ir +1,5 R, trailing 3 ATR al ir +3 R",
                    {"trend_partial_at_r": 2.0, "trend_partial_pct": 33.0, "trend_be_after_r": 1.5,
                     "trend_tight_after_r": 3.0, "trend_tight_trail_atr": 3.0}),
}
SHORTS = {"cortos": ("Igual que hoy + cortos en papel", {"trend_allow_short": True}),
          "cortos_p2_be15": ("Cortos en papel + vende 33% en +2 R + stop a la entrada al ir +1,5 R",
                             {"trend_allow_short": True, "trend_partial_at_r": 2.0, "trend_partial_pct": 33.0,
                              "trend_be_after_r": 1.5})}
ALL = "Todo 2022→hoy"


def variants(bot: str) -> dict[str, tuple[str, dict]]:
    v = dict(EXITS)
    if not BOTS[bot]["params"].get("trend_allow_short"):
        v.update(SHORTS)
    return v


def windows(last: pd.Timestamp) -> list[tuple[str, str]]:
    return [(ALL, "2022-01-01"), ("Últ. 12 meses", str((last - pd.DateOffset(months=12)).date())),
            ("Últ. 3 meses", str((last - pd.DateOffset(months=3)).date()))]


def load(csv_dir: Path, symbols: list[str], interval: str) -> dict[str, pd.DataFrame]:
    out = {}
    for s in symbols:
        path = csv_dir / f"{s}_{interval}.csv"
        if path.exists():
            out[s] = q.load_csv(path)
        else:
            print(f"[exit_lab] falta {path.name}; se omite {s}", file=sys.stderr)
    return out


def _run(job: tuple) -> dict:
    bot, vid, wname, start, csv_dir, balance = job
    desc, changes = variants(bot)[vid]
    b = BOTS[bot]
    p = q.Params(**{**b["params"], **changes})
    data = {s: d for s, d in load(Path(csv_dir), b["symbols"], b["interval"]).items() if len(d) >= p.min_candles + 2}
    t0 = time.time()
    res = bt.run_portfolio_backtest(data, p, bt.BacktestConfig(initial_balance=balance), trade_from=start)
    rep, trades = res["report"], res["trades"]
    # Una operación con toma parcial deja 2 filas; se cuentan como una (por par + apertura).
    by_pos: dict[tuple, float] = {}
    for t in trades:
        by_pos[(t["symbol"], t["opened_at"])] = by_pos.get((t["symbol"], t["opened_at"]), 0.0) + t["pnl_usd"]
    pos_pnl = list(by_pos.values())
    gw = sum(x for x in pos_pnl if x > 0)
    gl = -sum(x for x in pos_pnl if x < 0)
    return {"bot": bot, "variant": vid, "desc": desc, "window": wname, "return_pct": rep["net_return_pct"],
            "dd_pct": rep["max_drawdown_pct"], "positions": len(pos_pnl),
            "win_pct": round(sum(x > 0 for x in pos_pnl) / len(pos_pnl) * 100, 1) if pos_pnl else None,
            "pf": round(gw / gl, 2) if gl > 0 else None,
            "avg_win": round(gw / max(1, sum(x > 0 for x in pos_pnl)), 2),
            "avg_loss": round(-gl / max(1, sum(x < 0 for x in pos_pnl)), 2),
            "partials": sum(t["exit_reason"] == "PARTIAL_TP" for t in trades), "by_year": rep["by_year"],
            "halted": rep["halted_activations"], "secs": round(time.time() - t0, 1)}


def markdown(rows: list[dict], bot: str) -> str:
    df = pd.DataFrame([r for r in rows if r["bot"] == bot])
    if df.empty:
        return ""
    years = sorted({y for r in df[df.window == ALL].by_year for y in r})
    lines = [f"### {BOTS[bot]['label']}", "",
             f"**{ALL}** (retorno %, caída máx. %, operaciones, % que cierran ganando, factor de beneficio, USD por año) "
             "y las ventanas cortas (retorno % · caída %).", "",
             "| Salida | Retorno | Caída | Ops | Ganan | PF | Gana media / pierde media | " + " | ".join(years) +
             " | Últ. 12 m | Últ. 3 m |", "|---|" + "---|" * (8 + len(years))]
    for vid, (desc, _) in variants(bot).items():
        g = df[df.variant == vid].set_index("window")
        if ALL not in g.index:
            continue
        a = g.loc[ALL]
        short = [f"{g.loc[w, 'return_pct']:+.1f} · {g.loc[w, 'dd_pct']:.1f}" if w in g.index else "—"
                 for w in ("Últ. 12 meses", "Últ. 3 meses")]
        lines.append(f"| **{vid}** {desc} | {a.return_pct:+.1f}% | {a.dd_pct:.1f}% | {a.positions} | "
                     f"{a.win_pct if a.win_pct is not None else '—'}% | {a.pf if a.pf is not None else '—'} | "
                     f"{a.avg_win:+.2f} / {a.avg_loss:+.2f} | " +
                     " | ".join(f"{a.by_year.get(y, 0):+.0f}" for y in years) + " | " + " | ".join(short) + " |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("download", "run"))
    ap.add_argument("--csv-dir", required=True)
    ap.add_argument("--since", default="2021-01-01", help="download: primera vela (SMA200 diaria necesita ~1 año)")
    ap.add_argument("--bot", choices=("oficial", "trend", "tortuga", "all"), default="all")
    ap.add_argument("--only", help="ids de salidas separados por coma")
    ap.add_argument("--balance", type=float, default=500.0)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--out-md")
    ap.add_argument("--out-json")
    a = ap.parse_args(argv)
    bots = list(BOTS) if a.bot == "all" else [a.bot]
    if a.mode == "download":
        from intraday_lab import download
        for iv in sorted({BOTS[b]["interval"] for b in bots}):
            syms = sorted({s for b in bots if BOTS[b]["interval"] == iv for s in BOTS[b]["symbols"]})
            print("\n".join(download(a.csv_dir, syms, [iv], a.since)))
        return
    last = q.load_csv(Path(a.csv_dir) / f"BTCUSDT_{BOTS[bots[0]]['interval']}.csv").time.iloc[-1]
    only = set(a.only.split(",")) if a.only else None
    jobs = [(b, v, w, s, a.csv_dir, a.balance) for b in bots for v in variants(b) if not only or v in only
            for w, s in windows(last)]
    print(f"[exit_lab] {len(jobs)} backtests", file=sys.stderr)
    rows = []
    with ProcessPoolExecutor(a.workers) as ex:
        for r in ex.map(_run, jobs):
            print(f"[exit_lab] {r['bot']} {r['variant']} {r['window']}: {r['return_pct']:+.1f}% dd {r['dd_pct']:.1f}% "
                  f"({r['secs']} s)", file=sys.stderr, flush=True)
            rows.append(r)
            if a.out_json:          # se guarda a medida que avanza, por si se corta
                Path(a.out_json).write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str),
                                            encoding="utf-8")
    md = (f"# Laboratorio de salidas · datos hasta {last:%Y-%m-%d %H:%M} UTC · {a.balance:.0f} USD\n\n" +
          "\n".join(markdown(rows, b) for b in bots))
    print(md)
    if a.out_md:
        Path(a.out_md).write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
