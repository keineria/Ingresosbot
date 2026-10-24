#!/usr/bin/env python3
"""
backtest.py — Simulador vela a vela del núcleo cuantitativo (quant_core_engine.py).

Reglas de simulación (conservadoras, sin mirar el futuro):
  * La señal se evalúa con la vela t YA CERRADA; la entrada se hace en el OPEN de t+1
    (más deslizamiento). SL/TP/Break-Even se calculan con ese precio y el ATR de t.
  * Dentro de una vela no se sabe qué tocó primero: si una vela toca SL y TP, se asume SL.
  * El Break-Even se activa si la vela toca el trigger, y el nuevo SL rige desde la vela siguiente.
  * Si el precio abre más allá del SL o del TP (gap), se sale al OPEN.
  * Comisión por lado sobre el nocional (0.1% = taker de Binance spot por defecto).
  * La Capa 1 se audita tras cada cierre (postmortem_update) y el enfriamiento corre por vela.
  * El drawdown diario se reinicia a las 00:00 UTC.

Uso:
  python backtest.py --csv BTCUSDT_1h.csv --balance 10000
  python backtest.py --csv BTCUSDT_1h.csv --no-short --fee 0.1 --slippage 0.02 --trades-out trades.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import quant_core_engine as q


@dataclass
class BacktestConfig:
    initial_balance: float = 10000.0
    fee_pct: float = 0.1          # por lado, % del nocional
    slippage_pct: float = 0.02    # % en contra en la entrada y en salidas a mercado (SL)


@dataclass
class Position:
    side: str
    entry_time: Any
    entry_price: float
    qty: float
    stop_loss: float
    take_profit: float
    be_trigger: float
    be_new_sl: float
    risk_usd: float
    regime: str
    risk_mode: str
    be_moved: bool = False

    @property
    def d(self) -> int:
        return 1 if self.side == "LONG" else -1


def check_exit(pos: Position, o: float, h: float, l: float) -> tuple[float, str] | None:
    """Devuelve (precio_salida, motivo) si la vela cierra la posición; None si sigue abierta."""
    sl_reason = "BREAK_EVEN" if pos.be_moved else "STOP_LOSS"
    if pos.side == "LONG":
        if o <= pos.stop_loss:
            return o, sl_reason + "_GAP"
        if o >= pos.take_profit:
            return o, "TAKE_PROFIT_GAP"
        if l <= pos.stop_loss:
            return pos.stop_loss, sl_reason
        if h >= pos.take_profit:
            return pos.take_profit, "TAKE_PROFIT"
    else:
        if o >= pos.stop_loss:
            return o, sl_reason + "_GAP"
        if o <= pos.take_profit:
            return o, "TAKE_PROFIT_GAP"
        if h >= pos.stop_loss:
            return pos.stop_loss, sl_reason
        if l <= pos.take_profit:
            return pos.take_profit, "TAKE_PROFIT"
    return None


def maybe_move_break_even(pos: Position, h: float, l: float) -> None:
    if pos.be_moved:
        return
    touched = h >= pos.be_trigger if pos.side == "LONG" else l <= pos.be_trigger
    if touched:
        pos.stop_loss = pos.be_new_sl
        pos.be_moved = True


def _max_drawdown_pct(equity: np.ndarray) -> float:
    peak = np.maximum.accumulate(equity)
    return float(((peak - equity) / peak).max() * 100) if len(equity) else 0.0


def run_backtest(df: pd.DataFrame, p: q.Params | None = None, cfg: BacktestConfig | None = None) -> dict:
    p = p or q.Params()
    cfg = cfg or BacktestConfig()
    if len(df) < p.min_candles + 2:
        raise ValueError(f"Se necesitan al menos {p.min_candles + 2} velas (hay {len(df)}).")
    ind = q.compute_indicators(df, p)
    has_time = "time" in ind.columns
    times = ind["time"].to_list() if has_time else list(range(len(ind)))
    o_, h_, l_, c_ = (ind[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    fee, slip = cfg.fee_pct / 100.0, cfg.slippage_pct / 100.0

    balance = cfg.initial_balance
    state = {"balance": balance, "consecutive_losses": 0, "daily_drawdown_pct": 0.0,
             "cooldown_candles_remaining": 0, "day_peak_equity": balance}
    current_day = None
    pos: Position | None = None
    trades: list[dict] = []
    equity = np.full(len(ind), np.nan)
    stats = {"signals": 0, "signals_blocked_halted": 0, "halts": 0, "candles_halted": 0,
             "candles_throttled": 0, "regimes": {}}
    start = p.min_candles - 1

    def close_position(i: int, price: float, reason: str) -> None:
        nonlocal balance, pos, state
        gross = pos.qty * (price - pos.entry_price) * pos.d
        fees = pos.qty * pos.entry_price * fee + pos.qty * price * fee
        pnl = gross - fees
        balance += pnl
        was_halted = state.get("cooldown_candles_remaining", 0) > 0
        state = q.postmortem_update(state, pnl, balance, state["day_peak_equity"], p)
        if not was_halted and state.get("cooldown_candles_remaining", 0) > 0:
            stats["halts"] += 1
        trades.append({
            "entry_time": str(pos.entry_time), "exit_time": str(times[i]), "side": pos.side,
            "regime": pos.regime, "risk_mode": pos.risk_mode,
            "entry_price": round(pos.entry_price, 8), "exit_price": round(price, 8),
            "qty": pos.qty, "exit_reason": reason, "be_moved": pos.be_moved,
            "pnl_usd": round(pnl, 2), "fees_usd": round(fees, 2),
            "r_multiple": round(pnl / pos.risk_usd, 4) if pos.risk_usd > 0 else None,
            "balance_after": round(balance, 2),
        })
        pos = None

    for i in range(start, len(ind)):
        # Reinicio diario 00:00 UTC
        if has_time:
            day = pd.Timestamp(times[i]).date()
            if day != current_day:
                current_day = day
                state["day_peak_equity"] = balance
                state["daily_drawdown_pct"] = 0.0

        # El enfriamiento cuenta velas cerradas
        state = q.tick_cooldown(state, p)

        # Gestión de la posición abierta en esta vela
        if pos is not None:
            hit = check_exit(pos, o_[i], h_[i], l_[i])
            if hit is not None:
                price, reason = hit
                if reason.startswith(("STOP_LOSS", "BREAK_EVEN")):
                    price *= (1 - slip * pos.d)
                close_position(i, price, reason)
            else:
                maybe_move_break_even(pos, h_[i], l_[i])

        mtm = balance + (pos.qty * (c_[i] - pos.entry_price) * pos.d if pos else 0.0)
        equity[i] = mtm

        # Evaluación de entrada con la vela i cerrada -> entrada en open i+1
        if pos is None and i + 1 < len(ind):
            state["balance"] = balance
            cb = q.circuit_breaker(q.AccountState.from_dict(state), p)
            if cb["status"] == "HALTED":
                stats["candles_halted"] += 1
            elif cb["status"] == "THROTTLED":
                stats["candles_throttled"] += 1
            row = ind.iloc[i]
            vals = {k: float(row[k]) for k in ("close", "ema20", "ema50", "ema200", "rsi14", "atr14", "ema20_slope_pct")}
            if any(np.isnan(v) for v in vals.values()) or vals["atr14"] <= 0:
                continue
            mr = q.market_regime(vals, p)
            stats["regimes"][mr["regime"]] = stats["regimes"].get(mr["regime"], 0) + 1
            if mr["signal"] not in ("LONG", "SHORT"):
                continue
            stats["signals"] += 1
            if cb["abort_analysis"]:
                stats["signals_blocked_halted"] += 1
                continue
            d = 1 if mr["signal"] == "LONG" else -1
            entry = o_[i + 1] * (1 + slip * d)
            try:
                rk = q.atr_risk_guardian(mr["signal"], entry, vals["atr14"], balance, cb["risk_pct"], p)
            except ValueError:
                continue
            if rk["below_min_notional"] or rk["quantity"] <= 0:
                continue
            pos = Position(mr["signal"], times[i + 1], entry, rk["quantity"], rk["stop_loss"],
                           rk["take_profit"], rk["break_even_trigger"], rk["break_even_new_sl"],
                           rk["risk_usd_effective"], mr["regime"], cb["status"])

    if pos is not None:
        close_position(len(ind) - 1, c_[-1], "END_OF_DATA")
        equity[-1] = balance

    return _report(ind, times, trades, equity[start:], stats, cfg, p)


def _report(ind, times, trades, equity, stats, cfg, p) -> dict:
    tdf = pd.DataFrame(trades)
    n = len(tdf)
    wins = tdf[tdf.pnl_usd > 0] if n else tdf
    losses = tdf[tdf.pnl_usd < 0] if n else tdf
    gross_win = float(wins.pnl_usd.sum()) if n else 0.0
    gross_loss = float(-losses.pnl_usd.sum()) if n else 0.0
    final = trades[-1]["balance_after"] if trades else cfg.initial_balance
    start_px, end_px = float(ind["close"].iloc[p.min_candles - 1]), float(ind["close"].iloc[-1])

    def by(key):
        if not n:
            return {}
        g = tdf.groupby(key)
        return {str(k): {"trades": int(len(v)), "win_rate_pct": round(float((v.pnl_usd > 0).mean() * 100), 2),
                         "pnl_usd": round(float(v.pnl_usd.sum()), 2)} for k, v in g}

    per_year = {}
    if n:
        years = pd.to_datetime(tdf.exit_time, utc=True, errors="coerce").dt.year
        if years.notna().all():
            per_year = by(years.astype(int).astype(str).rename("year"))

    report = {
        "period": {"from": str(times[p.min_candles - 1]), "to": str(times[-1]),
                   "candles_evaluated": int(len(equity))},
        "config": {"initial_balance": cfg.initial_balance, "fee_pct_per_side": cfg.fee_pct,
                   "slippage_pct": cfg.slippage_pct, "allow_short": p.allow_short},
        "results": {
            "final_balance": round(final, 2),
            "net_return_pct": round((final / cfg.initial_balance - 1) * 100, 2),
            "buy_and_hold_return_pct": round((end_px / start_px - 1) * 100, 2),
            "max_drawdown_pct": round(_max_drawdown_pct(equity[~np.isnan(equity)]), 2),
            "trades": n,
            "win_rate_pct": round(len(wins) / n * 100, 2) if n else None,
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "avg_r_multiple": round(float(tdf.r_multiple.mean()), 3) if n else None,
            "expectancy_usd": round(float(tdf.pnl_usd.mean()), 2) if n else None,
            "total_fees_usd": round(float(tdf.fees_usd.sum()), 2) if n else 0.0,
            "max_consecutive_losses": int(_max_streak(tdf.pnl_usd.to_numpy() < 0)) if n else 0,
        },
        "circuit_breaker": {"halts": stats["halts"], "candles_halted": stats["candles_halted"],
                            "candles_throttled": stats["candles_throttled"],
                            "signals_total": stats["signals"],
                            "signals_blocked_by_halt": stats["signals_blocked_halted"]},
        "regime_distribution_flat_candles": stats["regimes"],
        "by_side": by("side"),
        "by_exit_reason": by("exit_reason"),
        "by_year": per_year,
    }
    return {"report": q._clean(report), "trades": trades}


def _max_streak(mask: np.ndarray) -> int:
    best = cur = 0
    for m in mask:
        cur = cur + 1 if m else 0
        best = max(best, cur)
    return best


def markdown_report(rep: dict) -> str:
    r, cb = rep["results"], rep["circuit_breaker"]
    rows = [
        ("Periodo", f"{rep['period']['from']} → {rep['period']['to']} ({rep['period']['candles_evaluated']} velas)"),
        ("Balance final", f"{r['final_balance']:.2f} USD ({r['net_return_pct']:+.2f}%)"),
        ("Buy & Hold", f"{r['buy_and_hold_return_pct']:+.2f}%"),
        ("Caída máxima", f"{r['max_drawdown_pct']:.2f}%"),
        ("Trades / acierto", f"{r['trades']} / {r['win_rate_pct']}%"),
        ("Profit factor / R medio", f"{r['profit_factor']} / {r['avg_r_multiple']}"),
        ("Comisiones", f"{r['total_fees_usd']:.2f} USD"),
        ("Racha perdedora máx.", str(r["max_consecutive_losses"])),
        ("Circuit Breaker", f"{cb['halts']} bloqueos, {cb['signals_blocked_by_halt']} señales frenadas"),
    ]
    return "\n".join(["| Métrica | Valor |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest vela a vela del núcleo cuantitativo.")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--balance", type=float, default=10000.0)
    ap.add_argument("--fee", type=float, default=0.1, help="Comisión por lado en %")
    ap.add_argument("--slippage", type=float, default=0.02, help="Deslizamiento en %")
    ap.add_argument("--no-short", action="store_true")
    ap.add_argument("--params", help="JSON con parámetros a sobrescribir (ver Params)")
    ap.add_argument("--trades-out", help="Ruta CSV para guardar cada trade")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args(argv)

    p = q.Params()
    if args.params:
        src = Path(args.params)
        for k, v in json.loads(src.read_text(encoding="utf-8") if src.exists() else args.params).items():
            if not hasattr(p, k):
                raise SystemExit(f"Parámetro desconocido: {k}")
            setattr(p, k, type(getattr(p, k))(v))
    if args.no_short:
        p.allow_short = False
    cfg = BacktestConfig(args.balance, args.fee, args.slippage)
    out = run_backtest(q.load_csv(args.csv), p, cfg)
    if args.trades_out:
        pd.DataFrame(out["trades"]).to_csv(args.trades_out, index=False)
    js = json.dumps(out["report"], ensure_ascii=False, indent=2, allow_nan=False)
    print(js if args.json_only else f"{markdown_report(out['report'])}\n\n```json\n{js}\n```")
    return 0


if __name__ == "__main__":
    sys.exit(main())
