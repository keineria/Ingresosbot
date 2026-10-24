#!/usr/bin/env python3
"""
backtester.py — Backtest vela a vela: quant_core_engine (3 capas) + state_manager (SQLite).

Reglas de simulación (conservadoras, sin mirar el futuro):
  * La señal se evalúa con la vela t YA CERRADA; la entrada es el OPEN de t+1 más deslizamiento.
    SL/TP/Break-Even se calculan con ese precio y el ATR de t.
  * Si una vela toca SL y TP, se asume que tocó primero el SL.
  * El Break-Even se activa al tocar el trigger y el nuevo SL rige desde la vela siguiente.
  * Si el precio abre más allá del SL o del TP (gap), se sale al OPEN.
  * Comisión por orden sobre el nocional (0.08% por defecto) en la entrada y en la salida.
  * El estado (racha, drawdown diario, enfriamiento) vive en state_manager; por defecto en memoria.

Uso:
  python backtester.py --csv data/candles/BTCUSDT_1h.csv
  python backtester.py --symbol BTCUSDT --interval 1h --limit 8760           # descarga (o usa el CSV local)
  python backtester.py --csv velas.csv --params '{"max_leverage": 2.0}' --out-md reporte.md --out-json reporte.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

import quant_core_engine as q
from state_manager import StateManager


@dataclass
class BacktestConfig:
    initial_balance: float = 10000.0
    fee_pct: float = 0.08         # por orden, % del nocional
    slippage_pct: float = 0.02    # % en contra en la entrada y en salidas por stop
    db_path: str = ":memory:"     # pon una ruta para inspeccionar la base después


def _max_drawdown_pct(equity: np.ndarray) -> float:
    if not len(equity):
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float(((peak - equity) / peak).max() * 100)


def _max_streak(mask) -> int:
    best = cur = 0
    for m in mask:
        cur = cur + 1 if m else 0
        best = max(best, cur)
    return best


def run_backtest(df: pd.DataFrame, p: q.Params | None = None, cfg: BacktestConfig | None = None,
                 symbol: str = "BTCUSDT") -> dict:
    p = p or q.Params()
    cfg = cfg or BacktestConfig()
    if len(df) < p.min_candles + 2:
        raise ValueError(f"Se necesitan al menos {p.min_candles + 2} velas (hay {len(df)}).")
    if "time" not in df.columns:
        raise ValueError("Las velas necesitan columna de tiempo (para el reinicio diario del drawdown).")

    ind = q.compute_indicators(df, p)
    times = ind["time"].to_list()
    o_, h_, l_, c_ = (ind[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    cols = ("close", "ema20", "ema50", "ema200", "rsi14", "atr14", "ema20_slope_pct")
    vals_all = ind[list(cols)].to_numpy(float)
    slip = cfg.slippage_pct / 100.0

    sm = StateManager(cfg.db_path, cfg.initial_balance, p, cfg.fee_pct)
    start = p.min_candles - 1
    equity = np.full(len(ind) - start, np.nan)
    stats = {"signals": 0, "signals_blocked_by_halt": 0, "break_even_activations": 0,
             "decisions": {}, "regimes_when_flat": {}}

    for i in range(start, len(ind)):
        sm.on_new_bar(times[i])
        for ev in sm.process_candle(symbol, times[i], o_[i], h_[i], l_[i], c_[i], cfg.slippage_pct):
            if ev["event"] == "BREAK_EVEN":
                stats["break_even_activations"] += 1
        equity[i - start] = sm.equity({symbol: c_[i]})

        if sm.positions(symbol) or i + 1 >= len(ind):
            continue
        vals = dict(zip(cols, vals_all[i]))
        if any(np.isnan(v) for v in vals.values()) or vals["atr14"] <= 0:
            continue
        r = q.run_engine(sm.engine_account(), manual=vals, symbol=symbol, entry_price=o_[i + 1], params=p)
        stats["decisions"][r["decision"]] = stats["decisions"].get(r["decision"], 0) + 1
        mr = r["layer_2_market_regime"]
        if mr.get("evaluated"):
            stats["regimes_when_flat"][mr["regime"]] = stats["regimes_when_flat"].get(mr["regime"], 0) + 1
        if r["decision"] == "HALTED":
            # La Capa 1 aborta sin mirar el gráfico; para la estadística medimos si había señal.
            if q.market_regime(vals, p)["signal"] in ("LONG", "SHORT"):
                stats["signals"] += 1
                stats["signals_blocked_by_halt"] += 1
            continue
        if r["decision"] not in ("LONG", "SHORT"):
            continue
        stats["signals"] += 1
        if slip:
            d = 1 if r["decision"] == "LONG" else -1
            r = q.run_engine(sm.engine_account(), manual=vals, symbol=symbol,
                             entry_price=o_[i + 1] * (1 + slip * d), params=p)
        sm.open_from_engine(r, opened_at=times[i + 1])

    for pos in sm.positions(symbol):
        sm.close_position(pos["id"], c_[-1], "END_OF_DATA", times[-1])
    equity[-1] = sm.account()["balance"]

    report = _report(ind, times, start, sm, equity, stats, cfg, p, symbol)
    trades = sm.trades()
    sm.close()
    return {"report": report, "trades": trades}


def _report(ind, times, start, sm, equity, stats, cfg, p, symbol) -> dict:
    tdf = pd.DataFrame(sm.trades())
    n = len(tdf)
    acc = sm.account()
    final = acc["balance"]
    gross_win = float(tdf.pnl_usd[tdf.pnl_usd > 0].sum()) if n else 0.0
    gross_loss = float(-tdf.pnl_usd[tdf.pnl_usd < 0].sum()) if n else 0.0
    start_px, end_px = float(ind["close"].iloc[start]), float(ind["close"].iloc[-1])
    transitions = sm.status_transitions

    def by(key) -> dict:
        if not n:
            return {}
        return {str(k): {"trades": int(len(g)), "win_rate_pct": round(float((g.result == "WIN").mean() * 100), 2),
                         "pnl_usd": round(float(g.pnl_usd.sum()), 2)} for k, g in tdf.groupby(key)}

    be = tdf[tdf.was_at_breakeven == 1] if n else tdf
    report = {
        "symbol": symbol,
        "period": {"from": str(times[start]), "to": str(times[-1]), "candles_evaluated": int(len(equity))},
        "config": {"initial_balance": cfg.initial_balance, "fee_pct_per_order": cfg.fee_pct,
                   "slippage_pct": cfg.slippage_pct, "allow_short": p.allow_short, "max_leverage": p.max_leverage},
        "results": {
            "final_balance": round(final, 2),
            "net_return_pct": round((final / cfg.initial_balance - 1) * 100, 2),
            "buy_and_hold_return_pct": round((end_px / start_px - 1) * 100, 2),
            "max_drawdown_pct": round(_max_drawdown_pct(equity[~np.isnan(equity)]), 2),
            "trades": n,
            "wins": int((tdf.result == "WIN").sum()) if n else 0,
            "losses": int((tdf.result == "LOSS").sum()) if n else 0,
            "breakevens": int((tdf.result == "BREAKEVEN").sum()) if n else 0,
            "win_rate_pct": round(float((tdf.result == "WIN").mean() * 100), 2) if n else None,
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "avg_r_multiple": round(float(tdf.r_multiple.mean()), 3) if n else None,
            "expectancy_usd": round(float(tdf.pnl_usd.mean()), 2) if n else None,
            "total_fees_usd": round(float(tdf.fees_usd.sum()), 2) if n else 0.0,
            "max_consecutive_losses": _max_streak((tdf.result == "LOSS").tolist()) if n else 0,
        },
        "break_even": {
            "activations": stats["break_even_activations"],
            "saved_by_break_even": int((be.result == "BREAKEVEN").sum()) if n else 0,
            "break_even_then_take_profit": int(be.exit_reason.str.startswith("TAKE_PROFIT").sum()) if n else 0,
        },
        "circuit_breaker": {
            "throttled_activations": sum(1 for t in transitions if t["to"] == "THROTTLED"),
            "halted_activations": sum(1 for t in transitions if t["to"] == "HALTED"),
            "signals_total": stats["signals"],
            "signals_blocked_by_halt": stats["signals_blocked_by_halt"],
            "trades_in_throttled_mode": int((tdf.risk_mode == "THROTTLED").sum()) if n else 0,
        },
        "decisions_when_flat": stats["decisions"],
        "regimes_when_flat": stats["regimes_when_flat"],
        "by_side": by("side"),
        "by_exit_reason": by("exit_reason"),
        "by_year": by(pd.to_datetime(tdf.closed_at, utc=True).dt.year.astype(str).rename("year")) if n else {},
    }
    return q._clean(report)


def markdown_report(rep: dict) -> str:
    r, be, cb = rep["results"], rep["break_even"], rep["circuit_breaker"]
    rows = [
        ("Símbolo / periodo", f"{rep['symbol']} · {rep['period']['from']} → {rep['period']['to']} "
                              f"({rep['period']['candles_evaluated']} velas)"),
        ("Retorno neto", f"{r['net_return_pct']:+.2f}% (balance final {r['final_balance']:.2f} USD)"),
        ("Buy & Hold", f"{r['buy_and_hold_return_pct']:+.2f}%"),
        ("Max Drawdown", f"{r['max_drawdown_pct']:.2f}%"),
        ("Trades (W / L / BE)", f"{r['trades']} ({r['wins']} / {r['losses']} / {r['breakevens']})"),
        ("Win Rate", f"{r['win_rate_pct']}%"),
        ("Profit Factor", str(r["profit_factor"])),
        ("R medio / esperanza", f"{r['avg_r_multiple']} R / {r['expectancy_usd']} USD por trade"),
        ("Comisiones pagadas", f"{r['total_fees_usd']:.2f} USD"),
        ("Break-Even", f"{be['activations']} activaciones, {be['saved_by_break_even']} trades salvados, "
                       f"{be['break_even_then_take_profit']} llegaron luego al TP"),
        ("Circuit Breaker", f"THROTTLED {cb['throttled_activations']} veces, HALTED {cb['halted_activations']} veces, "
                            f"{cb['signals_blocked_by_halt']} señales frenadas"),
        ("Racha perdedora máx.", str(r["max_consecutive_losses"])),
    ]
    return "\n".join(["| Métrica | Valor |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest vela a vela del núcleo cuantitativo.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="CSV OHLCV histórico")
    src.add_argument("--symbol", help="Descarga velas públicas (BTCUSDT, ETHUSDT, SOLUSDT...)")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--limit", type=int, default=1000, help="Velas a descargar con --symbol")
    ap.add_argument("--balance", type=float, default=10000.0)
    ap.add_argument("--fee", type=float, default=0.08, help="Comisión por orden en %")
    ap.add_argument("--slippage", type=float, default=0.02, help="Deslizamiento en %")
    ap.add_argument("--no-short", action="store_true")
    ap.add_argument("--params", help="JSON con parámetros a sobrescribir (ver Params)")
    ap.add_argument("--db", default=":memory:", help="Ruta SQLite para guardar el estado del backtest")
    ap.add_argument("--trades-out", help="CSV con cada trade")
    ap.add_argument("--out-md", help="Guarda el reporte en Markdown")
    ap.add_argument("--out-json", help="Guarda el reporte en JSON")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args(argv)

    p = q.Params()
    if args.params:
        src_path = Path(args.params)
        for k, v in json.loads(src_path.read_text(encoding="utf-8") if src_path.exists() else args.params).items():
            if not hasattr(p, k):
                raise SystemExit(f"Parámetro desconocido: {k}")
            setattr(p, k, type(getattr(p, k))(v))
    if args.no_short:
        p.allow_short = False

    if args.csv:
        df, symbol = q.load_csv(args.csv), Path(args.csv).stem.split("_")[0].upper()
    else:
        from market_fetcher import fetch_candles
        df, info = fetch_candles(args.symbol, args.interval, args.limit, verbose=True)
        symbol = args.symbol.upper()
        print(f"[backtester] velas: {info['candles']} desde {info['source']}", file=sys.stderr)

    out = run_backtest(df, p, BacktestConfig(args.balance, args.fee, args.slippage, args.db), symbol)
    rep = out["report"]
    js = json.dumps(rep, ensure_ascii=False, indent=2, allow_nan=False)
    md = f"# Backtest {rep['symbol']}\n\n{markdown_report(rep)}\n\n```json\n{js}\n```\n"
    if args.trades_out:
        pd.DataFrame(out["trades"]).to_csv(args.trades_out, index=False)
    if args.out_md:
        Path(args.out_md).write_text(md, encoding="utf-8")
    if args.out_json:
        Path(args.out_json).write_text(js, encoding="utf-8")
    print(js if args.json_only else md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
