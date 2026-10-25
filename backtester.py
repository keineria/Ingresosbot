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
  * Perfil "trend": el trailing stop se recalcula al cierre de cada vela y rige desde la siguiente.

Uso:
  python backtester.py --csv data/candles/BTCUSDT_1h.csv
  python backtester.py --symbol BTCUSDT --interval 1h --limit 8760           # descarga (o usa el CSV local)
  python backtester.py --csv velas.csv --params '{"max_leverage": 2.0}' --out-md reporte.md --out-json reporte.json

Modo comparativo (--sweep): prueba cada combinación de DEFAULT_GRID en cada par y temporalidad.
Cada serie se parte en entrenamiento (primer 70%) y validación (último 30%, nunca usado para elegir):
una variante solo es creíble si gana en las dos partes y en varios pares.
  python backtester.py --sweep BTCUSDT,ETHUSDT,SOLUSDT --intervals 1h,4h,1d --out-md variantes.md --out-csv variantes.csv
  python backtester.py --sweep BTCUSDT --intervals 4h --grid '{"tp_atr_mult": [2.0, 3.0], "be_atr_mult": [null]}'

Perfil paralelo Trend Rider (SKILL_4) contra el oficial, con vecinos de sus parámetros para ver si es robusto:
  python backtester.py --sweep BTCUSDT,ETHUSDT,SOLUSDT --intervals 1h,4h,1d --grid trend --out-md trend_vs_oficial.md
  python backtester.py --csv data/candles/BTCUSDT_4h.csv --params '{"strategy": "trend"}'
"""

from __future__ import annotations

import argparse
import itertools
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


# Combinaciones del modo comparativo. be_atr_mult None = sin Break-Even. Incluye la regla oficial.
DEFAULT_GRID: dict[str, list] = {
    "sl_atr_mult": [1.6, 2.2],
    "tp_atr_mult": [3.6, 2.4],
    "be_atr_mult": [1.5, None],
    "allow_short": [True, False],
}
# Trend Rider contra el oficial: la configuración por defecto, sus otras entradas y un vecino de cada
# parámetro. Si solo gana la configuración exacta y no sus vecinos, es suerte y no ventaja.
TREND_COMPARE: list[dict] = [
    {"strategy": "official"},
    {"strategy": "trend"},
    {"strategy": "trend", "trend_entry": "pullback"},
    {"strategy": "trend", "trend_entry": "both"},
    {"strategy": "trend", "donchian_len": 55},
    {"strategy": "trend", "trend_sl_atr_mult": 2.0},
    {"strategy": "trend", "trend_sl_atr_mult": 3.0},
    {"strategy": "trend", "trail_atr_mult": 3.0},
    {"strategy": "trend", "trail_atr_mult": 5.0},
    {"strategy": "trend", "trend_allow_short": True},
]
GRID_PRESETS: dict[str, dict | list] = {"default": DEFAULT_GRID, "trend": TREND_COMPARE}
DEFAULT_SWEEP_LIMITS = {"1h": 8760, "4h": 4380, "1d": 1460}   # ~1, 2 y 4 años
BE_OFF_ATR = 1000.0                                          # trigger inalcanzable: el TP llega antes


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


def _bar_step(times: pd.Series) -> pd.Timedelta:
    d = pd.to_datetime(times, utc=True).diff().dropna()
    return d.median() if len(d) else pd.Timedelta(hours=1)


def _exit_reason(vals: dict, pos: dict, t, step: pd.Timedelta, p: q.Params) -> str | None:
    """Regla de cierre del perfil "trend" al cierre de la vela t (que empieza en t). bars_held cuenta la vela de entrada."""
    held = int((pd.Timestamp(t) - pd.Timestamp(pos["opened_at"])) / step) + 1
    return q.trend_exit_reason(vals, pos["side"], p, pos["entry_price"], held)


def run_backtest(df: pd.DataFrame, p: q.Params | None = None, cfg: BacktestConfig | None = None,
                 symbol: str = "BTCUSDT", trade_from=None) -> dict:
    """trade_from: no opera antes de esa fecha (las velas previas solo calientan indicadores, p. ej. la SMA diaria)."""
    p = p or q.Params()
    cfg = cfg or BacktestConfig()
    if len(df) < p.min_candles + 2:
        raise ValueError(f"Se necesitan al menos {p.min_candles + 2} velas (hay {len(df)}).")
    if "time" not in df.columns:
        raise ValueError("Las velas necesitan columna de tiempo (para el reinicio diario del drawdown).")

    ind = q.compute_indicators(df, p)
    times = ind["time"].to_list()
    o_, h_, l_, c_ = (ind[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    cols = q.INDICATOR_KEYS
    vals_all = ind[list(cols)].to_numpy(float)
    atr_all = ind["atr14"].to_numpy(float)
    trailing = p.strategy == "trend"
    slip = cfg.slippage_pct / 100.0

    sm = StateManager(cfg.db_path, cfg.initial_balance, p, cfg.fee_pct)
    start = p.min_candles - 1
    if trade_from is not None:
        tf = pd.Timestamp(trade_from)
        tf = tf.tz_localize("UTC") if tf.tzinfo is None else tf
        start = max(start, int((ind["time"] < tf).sum()))
        if start >= len(ind) - 1:
            raise ValueError("No hay velas para operar después de trade_from.")
    equity = np.full(len(ind) - start, np.nan)
    stats = {"signals": 0, "signals_blocked_by_halt": 0, "break_even_activations": 0, "trailing_updates": 0,
             "entry_triggers": {}, "decisions": {}, "regimes_when_flat": {}}

    signal_exits = q.trend_has_signal_exits(p)
    step = _bar_step(ind["time"])
    pending_exit = None
    for i in range(start, len(ind)):
        sm.on_new_bar(times[i])
        if pending_exit:     # una regla de cierre disparó al cierre anterior: salida a mercado en la apertura
            for pos in sm.positions(symbol):
                px = o_[i] * ((1 - slip) if pos["side"] == "LONG" else (1 + slip))
                sm.close_position(pos["id"], px, pending_exit, times[i])
            pending_exit = None
        for ev in sm.process_candle(symbol, times[i], o_[i], h_[i], l_[i], c_[i], cfg.slippage_pct):
            if ev["event"] == "BREAK_EVEN":
                stats["break_even_activations"] += 1
        if trailing:
            stats["trailing_updates"] += len(sm.apply_trailing(symbol, times[i], h_[i], l_[i], atr_all[i]))
        equity[i - start] = sm.equity({symbol: c_[i]})
        if signal_exits and i + 1 < len(ind):
            pos_now = sm.positions(symbol)
            if pos_now:
                pending_exit = _exit_reason(dict(zip(cols, vals_all[i])), pos_now[0], times[i], step, p)
                if pending_exit:
                    continue

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
            if q.evaluate_layer2(vals, p)["signal"] in ("LONG", "SHORT"):
                stats["signals"] += 1
                stats["signals_blocked_by_halt"] += 1
            continue
        if r["decision"] not in ("LONG", "SHORT"):
            continue
        stats["signals"] += 1
        if mr.get("entry_trigger"):
            stats["entry_triggers"][mr["entry_trigger"]] = stats["entry_triggers"].get(mr["entry_trigger"], 0) + 1
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


def run_portfolio_backtest(datasets: dict[str, pd.DataFrame], p: q.Params | None = None,
                           cfg: BacktestConfig | None = None, trade_from=None) -> dict:
    """Varios pares con UNA sola cuenta, como bot_runner.py: el mismo Circuit Breaker para todos, una posición
    por par y nunca más de 85% × max_leverage del balance comprometido entre todos (si no cabe: SKIPPED).
    La señal se toma al cierre de la vela t y se entra en la apertura de t+1 (en el orden de `datasets`)."""
    p = p or q.Params()
    cfg = cfg or BacktestConfig()
    slip = cfg.slippage_pct / 100.0
    cols = q.INDICATOR_KEYS
    ind = {s: q.compute_indicators(df, p).set_index("time") for s, df in datasets.items()}
    times = sorted(set().union(*(set(d.index) for d in ind.values())))
    first_ok = {s: d.index[p.min_candles - 1] for s, d in ind.items() if len(d) >= p.min_candles + 2}
    tf = None
    if trade_from is not None:
        tf = pd.Timestamp(trade_from)
        tf = tf.tz_localize("UTC") if tf.tzinfo is None else tf
    times = [t for t in times if tf is None or t >= tf]
    sm = StateManager(":memory:", cfg.initial_balance, p, cfg.fee_pct)
    pending: list[tuple[str, dict]] = []
    pending_exits: dict[str, str] = {}
    signal_exits = q.trend_has_signal_exits(p)
    step = min(_bar_step(d.index.to_series()) for d in ind.values())
    stats = {"signals": 0, "skipped_by_capital": 0}
    equity, last_close = [], {}
    for t in times:
        sm.on_new_bar(t)
        bars = {s: d.loc[t] for s, d in ind.items() if t in d.index}
        for s, reason in pending_exits.items():         # cierres por regla, a la apertura de esta vela
            if s in bars:
                for pos in sm.positions(s):
                    o = float(bars[s]["open"])
                    sm.close_position(pos["id"], o * ((1 - slip) if pos["side"] == "LONG" else (1 + slip)), reason, t)
        pending_exits = {}
        for s, vals in pending:                     # entradas de la vela anterior, a la apertura de esta
            if s not in bars or sm.positions(s):
                continue
            o = float(bars[s]["open"])
            d = 1 if q.evaluate_layer2(vals, p)["signal"] == "LONG" else -1
            r = q.run_engine(sm.engine_account(), manual=vals, symbol=s, entry_price=o * (1 + slip * d), params=p)
            if r["decision"] not in ("LONG", "SHORT"):
                continue
            cap = sm.account()["balance"] * p.max_allocation_pct / 100.0 * p.max_leverage
            if sm.open_notional() + r["order"]["notional_usd"] > cap + 0.01:
                stats["skipped_by_capital"] += 1
                continue
            sm.open_from_engine(r, opened_at=t)
        pending = []
        for s, b in bars.items():
            sm.process_candle(s, t, float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]),
                              cfg.slippage_pct)
            if p.strategy == "trend":
                sm.apply_trailing(s, t, float(b["high"]), float(b["low"]), float(b["atr14"]))
            last_close[s] = float(b["close"])
        equity.append(sm.equity(last_close))
        if signal_exits:
            for s, b in bars.items():
                for pos in sm.positions(s):
                    reason = _exit_reason({k: float(b[k]) for k in cols}, pos, t, step, p)
                    if reason:
                        pending_exits[s] = reason
        for s, b in bars.items():
            if sm.positions(s) or s in pending_exits or s not in first_ok or t < first_ok[s]:
                continue
            vals = {k: float(b[k]) for k in cols}
            if any(np.isnan(v) for v in vals.values()) or vals["atr14"] <= 0:
                continue
            r = q.run_engine(sm.engine_account(), manual=vals, symbol=s, params=p)
            if r["decision"] in ("LONG", "SHORT"):
                stats["signals"] += 1
                pending.append((s, vals))
    for pos in sm.positions():
        sm.close_position(pos["id"], last_close[pos["symbol"]], "END_OF_DATA", times[-1])
    tdf = pd.DataFrame(sm.trades())
    n = len(tdf)
    final = sm.account()["balance"]
    eq = np.array(equity + [final])
    gw = float(tdf.pnl_usd[tdf.pnl_usd > 0].sum()) if n else 0.0
    gl = float(-tdf.pnl_usd[tdf.pnl_usd < 0].sum()) if n else 0.0
    rep = {"symbols": list(datasets), "period": {"from": str(times[0]), "to": str(times[-1])},
           "strategy": p.strategy, "initial_balance": cfg.initial_balance, "final_balance": round(final, 2),
           "net_return_pct": round((final / cfg.initial_balance - 1) * 100, 2),
           "max_drawdown_pct": round(_max_drawdown_pct(eq), 2), "trades": n,
           "win_rate_pct": round(float((tdf.result == "WIN").mean() * 100), 2) if n else None,
           "profit_factor": round(gw / gl, 3) if gl > 0 else None,
           "total_fees_usd": round(float(tdf.fees_usd.sum()), 2) if n else 0.0,
           "halted_activations": sum(1 for x in sm.status_transitions if x["to"] == "HALTED"),
           "signals": stats["signals"], "skipped_by_capital": stats["skipped_by_capital"],
           "by_symbol": ({k: {"trades": int(len(g)), "pnl_usd": round(float(g.pnl_usd.sum()), 2)}
                          for k, g in tdf.groupby("symbol")} if n else {}),
           "by_year": ({k: round(float(g.pnl_usd.sum()), 2) for k, g in
                        tdf.groupby(pd.to_datetime(tdf.closed_at, utc=True).dt.year.astype(str))} if n else {})}
    trades = sm.trades()
    sm.close()
    return {"report": q._clean(rep), "trades": trades}


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
        "config": {"strategy": p.strategy, "initial_balance": cfg.initial_balance, "fee_pct_per_order": cfg.fee_pct,
                   "slippage_pct": cfg.slippage_pct,
                   "allow_short": p.trend_allow_short if p.strategy == "trend" else p.allow_short,
                   "max_leverage": p.max_leverage},
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
        "trend": ({"entry": p.trend_entry, "donchian_len": p.donchian_len, "sl_atr_mult": p.trend_sl_atr_mult,
                   "trail_atr_mult": p.trail_atr_mult, "trailing_updates": stats["trailing_updates"],
                   "entry_triggers": stats["entry_triggers"],
                   "avg_bars_in_trade": round(float((pd.to_datetime(tdf.closed_at, utc=True)
                                                     - pd.to_datetime(tdf.opened_at, utc=True)).mean()
                                                    / (ind["time"].iloc[-1] - ind["time"].iloc[-2])), 1) if n else None}
                  if p.strategy == "trend" else None),
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
        (("Trend Rider", f"entrada {rep['trend']['entry']} · Donchian {rep['trend']['donchian_len']} · "
                         f"SL inicial {rep['trend']['sl_atr_mult']:g} ATR · trailing {rep['trend']['trail_atr_mult']:g} ATR · "
                         f"{rep['trend']['trailing_updates']} movimientos del SL · "
                         f"{rep['trend']['avg_bars_in_trade']} velas por trade")
         if rep.get("trend") else
         ("Break-Even", f"{be['activations']} activaciones, {be['saved_by_break_even']} trades salvados, "
                        f"{be['break_even_then_take_profit']} llegaron luego al TP")),
        ("Circuit Breaker", f"THROTTLED {cb['throttled_activations']} veces, HALTED {cb['halted_activations']} veces, "
                            f"{cb['signals_blocked_by_halt']} señales frenadas"),
        ("Racha perdedora máx.", str(r["max_consecutive_losses"])),
    ]
    return "\n".join(["| Métrica | Valor |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows])


# ════════════════════════════════════════════════════════════════════════════
# MODO COMPARATIVO
# ════════════════════════════════════════════════════════════════════════════
def grid_combos(grid: dict[str, list] | list[dict]) -> list[dict]:
    """Un dict {param: [valores]} se expande en todas sus combinaciones; una lista de dicts se usa tal cual."""
    if isinstance(grid, list):
        return [dict(c) for c in grid]
    keys = list(grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]


def _params_for(base: q.Params, combo: dict) -> q.Params:
    p = q.Params(**{k: getattr(base, k) for k in base.__dataclass_fields__})
    for k, v in combo.items():
        if k == "be_atr_mult" and v is None:
            v = BE_OFF_ATR
        setattr(p, k, type(getattr(p, k))(v))
    return p


_TREND_LABELS = {"donchian_len": "Donchian {:g}", "trend_sl_atr_mult": "SL inicial {:g} ATR",
                 "trail_atr_mult": "trailing {:g} ATR"}
_ENTRY_LABELS = {"breakout": "ruptura", "pullback": "retroceso oficial", "both": "ruptura o retroceso",
                 "daily_filter": "solo filtro diario"}


def combo_label(combo: dict) -> str:
    parts = []
    for k, v in combo.items():
        if k == "strategy":
            parts.append("Oficial" if v == "official" else "Trend Rider")
        elif k == "trend_entry":
            parts.append("entrada " + _ENTRY_LABELS.get(v, str(v)))
        elif k in _TREND_LABELS:
            parts.append(_TREND_LABELS[k].format(v))
        elif k == "daily_sma_len":
            parts.append(f"filtro diario SMA{v:g}" if v else "sin filtro diario")
        elif k == "trend_exit_on_filter":
            parts.append("sale si el filtro cae" if v else "sale solo por trailing")
        elif k in ("allow_short", "trend_allow_short"):
            parts.append("con cortos" if v else "solo largos")
        elif k == "be_atr_mult":
            parts.append("sin BE" if v is None else f"BE {v:g}")
        else:
            name = {"sl_atr_mult": "SL", "tp_atr_mult": "TP"}.get(k, k)
            parts.append(f"{name} {v:g}" if isinstance(v, (int, float)) else f"{name} {v}")
    return " · ".join(parts)


def split_train_test(df: pd.DataFrame, p: q.Params, train_frac: float = 0.7) -> tuple[pd.DataFrame, pd.DataFrame]:
    """La validación arranca justo en el corte; las velas previas solo calientan los indicadores."""
    if not 0.1 <= train_frac <= 0.9:
        raise ValueError("train_frac debe estar entre 0.1 y 0.9")
    cut = int(len(df) * train_frac)
    warm = p.min_candles - 1
    return df.iloc[:cut].reset_index(drop=True), df.iloc[max(0, cut - warm):].reset_index(drop=True)


def _brief(res: dict) -> dict:
    r = res["results"]
    return {"trades": r["trades"], "return_pct": r["net_return_pct"], "pf": r["profit_factor"],
            "dd_pct": r["max_drawdown_pct"], "win_rate_pct": r["win_rate_pct"], "bh_pct": r["buy_and_hold_return_pct"]}


def run_sweep(datasets: dict[tuple[str, str], pd.DataFrame], grid: dict[str, list] | list[dict] | None = None,
              base: q.Params | None = None, cfg: BacktestConfig | None = None, train_frac: float = 0.7,
              progress=None) -> list[dict]:
    """Una fila por (par, temporalidad, combinación) con métricas de entrenamiento y de validación."""
    grid = grid or DEFAULT_GRID
    base = base or q.Params()
    cfg = cfg or BacktestConfig()
    cfg = BacktestConfig(cfg.initial_balance, cfg.fee_pct, cfg.slippage_pct, ":memory:")
    combos = grid_combos(grid)
    official = q.Params()
    rows = []
    for (symbol, interval), df in datasets.items():
        train, test = split_train_test(df, base, train_frac)
        if progress:
            progress(f"{symbol} {interval}: {len(combos)} variantes ({len(train)} velas entrenamiento, "
                     f"{len(test) - base.min_candles + 1} validación)")
        for combo in combos:
            p = _params_for(base, combo)
            tr = _brief(run_backtest(train, p, cfg, symbol)["report"])
            if p.daily_sma_len > 0:   # la SMA diaria necesita meses de historia previa: se calienta con todo el pasado
                te = _brief(run_backtest(df, p, cfg, symbol, trade_from=df.time.iloc[int(len(df) * train_frac)])["report"])
            else:
                te = _brief(run_backtest(test, p, cfg, symbol)["report"])
            rows.append({"symbol": symbol, "interval": interval, "variant": combo_label(combo),
                         "official": all(getattr(official, a) == b for a, b in combo.items()), **combo,
                         **{f"train_{a}": b for a, b in tr.items()}, **{f"test_{a}": b for a, b in te.items()}})
    return rows


def summarize_sweep(rows: list[dict]) -> list[dict]:
    """Agrupa por variante: en cuántas series gana en entrenamiento Y validación, y su retorno medio."""
    out = []
    for variant, g in pd.DataFrame(rows).groupby("variant", sort=False):
        both = (g.train_return_pct > 0) & (g.test_return_pct > 0)
        out.append({"variant": variant, "official": bool(g.official.any()), "series": int(len(g)),
                    "wins_both": int(both.sum()),
                    "mean_train_return_pct": round(float(g.train_return_pct.mean()), 2),
                    "mean_test_return_pct": round(float(g.test_return_pct.mean()), 2),
                    "mean_test_pf": round(float(g.test_pf.dropna().mean()), 3) if g.test_pf.notna().any() else None,
                    "worst_test_dd_pct": round(float(g.test_dd_pct.max()), 2),
                    "test_trades": int(g.test_trades.sum())})
    # Se ordena por entrenamiento: ordenar por validación sería elegir mirando el examen.
    return sorted(out, key=lambda r: (-r["wins_both"], -r["mean_train_return_pct"]))


def markdown_sweep(rows: list[dict], train_frac: float = 0.7, top: int = 10) -> str:
    def f(v, fmt="{:+.2f}%"):
        return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else fmt.format(v)

    summ = summarize_sweep(rows)
    n_series = summ[0]["series"] if summ else 0
    lines = ["# Comparativa de variantes",
             "",
             f"Entrenamiento = primer {train_frac:.0%} de cada serie; validación = último {1 - train_frac:.0%} "
             "(no se usa para ordenar). Una variante es creíble si gana en las dos partes en varias series.",
             "",
             f"## Variantes ordenadas por robustez ({n_series} series por variante)",
             "",
             "| Variante | Gana en ent. y val. | Retorno medio ent. | Retorno medio val. | PF medio val. | Peor DD val. | Trades val. |",
             "|---|---|---|---|---|---|---|"]
    for r in summ[:top] + [r for r in summ[top:] if r["official"]]:
        name = f"**{r['variant']}** (oficial)" if r["official"] else r["variant"]
        lines.append(f"| {name} | {r['wins_both']}/{r['series']} | {f(r['mean_train_return_pct'])} | "
                     f"{f(r['mean_test_return_pct'])} | {f(r['mean_test_pf'], '{:.3f}')} | "
                     f"{f(r['worst_test_dd_pct'], '{:.2f}%')} | {r['test_trades']} |")
    lines += ["", "## Mejor variante de entrenamiento en cada serie, y cómo le fue en validación", "",
              "| Serie | Variante | Ret. ent. | Ret. val. | PF val. | DD val. | Buy & Hold val. | Oficial en val. |",
              "|---|---|---|---|---|---|---|---|"]
    df = pd.DataFrame(rows)
    for (sym, iv), g in df.groupby(["symbol", "interval"], sort=False):
        best = g.loc[g.train_return_pct.idxmax()]
        off = g[g.official]
        off_txt = f(float(off.test_return_pct.iloc[0])) if len(off) else "—"
        lines.append(f"| {sym} {iv} | {best.variant} | {f(best.train_return_pct)} | {f(best.test_return_pct)} | "
                     f"{f(best.test_pf, '{:.3f}')} | {f(best.test_dd_pct, '{:.2f}%')} | {f(best.test_bh_pct)} | {off_txt} |")
    return "\n".join(lines) + "\n"


def _load_sweep_data(symbols: list[str], intervals: list[str], limit: int | None) -> dict:
    from market_fetcher import fetch_candles, save_csv, default_csv_path
    data = {}
    for sym in symbols:
        for iv in intervals:
            n = limit or DEFAULT_SWEEP_LIMITS.get(iv, 2000)
            df, info = fetch_candles(sym, iv, n, verbose=True)
            if not info["source"].startswith("csv:"):
                save_csv(df, default_csv_path(sym, iv))
            print(f"[sweep] {sym} {iv}: {info['candles']} velas desde {info['source']}", file=sys.stderr)
            data[(sym.upper(), iv)] = df
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest vela a vela del núcleo cuantitativo.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="CSV OHLCV histórico")
    src.add_argument("--symbol", help="Descarga velas públicas (BTCUSDT, ETHUSDT, SOLUSDT...)")
    src.add_argument("--sweep", metavar="SYMBOLS", help="Modo comparativo: pares separados por coma")
    src.add_argument("--portfolio", metavar="SYMBOLS",
                     help="Varios pares con UNA cuenta (como el bot 24/7): BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--trade-from", help="Fecha desde la que se opera (lo anterior solo calienta indicadores)")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--intervals", default="1h,4h,1d", help="Temporalidades del modo comparativo")
    ap.add_argument("--grid", help="Modo comparativo: 'default', 'trend' (Trend Rider contra el oficial), "
                                   "JSON {parámetro: [valores]} o JSON [{combinación}, ...] (null en be_atr_mult = sin BE)")
    ap.add_argument("--train-frac", type=float, default=0.7, help="Parte de entrenamiento en el modo comparativo")
    ap.add_argument("--out-csv", help="Modo comparativo: CSV con todas las filas")
    ap.add_argument("--limit", type=int, help="Velas a descargar (1000 con --symbol; ~1-4 años con --sweep)")
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

    if args.sweep:
        grid = DEFAULT_GRID
        if args.grid in GRID_PRESETS:
            grid = GRID_PRESETS[args.grid]
        elif args.grid:
            grid = json.loads(args.grid)
            if isinstance(grid, list):
                bad = [k for c in grid for k in (c if isinstance(c, dict) else {"?": 0}) if not hasattr(p, k)]
                if not grid or bad:
                    raise SystemExit(f"--grid: cada combinación debe ser un dict de parámetros de Params ({bad})")
            else:
                for k, v in grid.items():
                    if not hasattr(p, k) or not isinstance(v, list) or not v:
                        raise SystemExit(f"--grid: '{k}' debe ser un parámetro de Params con una lista de valores")
        symbols = [s.strip().upper() for s in args.sweep.split(",") if s.strip()]
        intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]
        data = _load_sweep_data(symbols, intervals, args.limit)
        rows = run_sweep(data, grid, p, BacktestConfig(args.balance, args.fee, args.slippage), args.train_frac,
                         progress=lambda m: print(f"[sweep] {m}", file=sys.stderr))
        md = markdown_sweep(rows, args.train_frac)
        if args.out_csv:
            pd.DataFrame(rows).to_csv(args.out_csv, index=False)
        if args.out_md:
            Path(args.out_md).write_text(md, encoding="utf-8")
        if args.out_json:
            Path(args.out_json).write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(md)
        return 0

    if args.portfolio:
        from market_fetcher import fetch_candles
        data = {}
        for sym in [x.strip().upper() for x in args.portfolio.split(",") if x.strip()]:
            data[sym], info = fetch_candles(sym, args.interval, args.limit or 6000, verbose=True)
            print(f"[backtester] {sym}: {info['candles']} velas desde {info['source']}", file=sys.stderr)
        out = run_portfolio_backtest(data, p, BacktestConfig(args.balance, args.fee, args.slippage), args.trade_from)
        js = json.dumps(out["report"], ensure_ascii=False, indent=2, allow_nan=False)
        if args.trades_out:
            pd.DataFrame(out["trades"]).to_csv(args.trades_out, index=False)
        if args.out_json:
            Path(args.out_json).write_text(js, encoding="utf-8")
        print(js)
        return 0

    if args.csv:
        df, symbol = q.load_csv(args.csv), Path(args.csv).stem.split("_")[0].upper()
    else:
        from market_fetcher import fetch_candles
        df, info = fetch_candles(args.symbol, args.interval, args.limit or 1000, verbose=True)
        symbol = args.symbol.upper()
        print(f"[backtester] velas: {info['candles']} desde {info['source']}", file=sys.stderr)

    out = run_backtest(df, p, BacktestConfig(args.balance, args.fee, args.slippage, args.db), symbol, args.trade_from)
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
