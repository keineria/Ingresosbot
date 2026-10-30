#!/usr/bin/env python3
"""
bot_review.py — Revisión de un bot en paper trading durante un periodo (p. ej. "los últimos 2 días de Nathalia").

Solo LEE: la base SQLite del bot, su log y velas públicas de Binance. No toca la base, ni los servicios, ni
dinero real. Responde a tres preguntas, en Markdown para pegar en el chat:

  1. ¿Qué hizo el bot? Balance, estado de la Capa 1, operaciones cerradas y posiciones abiertas en el periodo.
  2. ¿Por qué no entró? Recalcula la Capa 2 en cada vela cerrada del periodo y cuenta qué filtro bloqueó.
  3. ¿Otra configuración lo habría hecho mejor? Backtest con el motor real (Capa 1 → 2 → 3) de unas pocas
     configuraciones sobre los MISMOS pares, en el periodo, en los últimos 90 días y en el último año.

Uso (en el VPS, desde /opt/Ingresosbot):
  sudo -u ingresosbot nice .venv/bin/python bot_review.py --bot oficial --since 2026-10-26T01:13
  ... --out data/revisiones/nathalia.md      # además lo guarda en un archivo
  ... --no-backtest                          # solo las partes 1 y 2 (rápido)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pandas as pd

import quant_core_engine as q
from backtester import BacktestConfig, run_portfolio_backtest
from bot_runner import bot_settings, find_bot, load_bots, parse_params
from market_fetcher import fetch_candles, fetch_price
from notifier import load_env_file
from state_manager import StateManager

BARS_PER_DAY = {"15m": 96, "1h": 24, "4h": 6, "1d": 1}
WINDOWS = (("periodo", None), ("90 días", 90), ("1 año", 365))


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def candidates(p_live: q.Params, interval: str) -> list[dict]:
    """Configuraciones a comparar. Todas usan el mismo motor de 3 capas; solo cambian los parámetros."""
    spot = replace(p_live, allow_short=False)
    rows = [{"label": f"Nathalia como está ({interval})", "interval": interval, "p": p_live}]
    if p_live.allow_short:
        rows.append({"label": f"Oficial solo compras ({interval})", "interval": interval, "p": spot})
    if interval != "4h":
        rows.append({"label": "Oficial en 4h, solo compras", "interval": "4h", "p": spot})
    rows.append({"label": "Oficial + filtro diario SMA200 ({})".format(interval), "interval": interval,
                 "p": replace(spot, strategy="trend", trend_entry="pullback", daily_sma_len=200,
                              trend_sl_atr_mult=p_live.sl_atr_mult, trail_atr_mult=4.0)})
    for label, iv, path in (("Reglas Trend Rider (4h)", "4h", "deploy/trend_params.json"),
                            ("Reglas Tortuga 55 (1d)", "1d", "deploy/tortuga_params.json")):
        if Path(path).exists():
            rows.append({"label": label, "interval": iv, "p": parse_params(path)})
    return rows


def warmup_bars(p: q.Params, interval: str) -> int:
    w = p.ema_slow * 2
    if p.daily_sma_len > 0:
        w = max(w, (p.daily_sma_len + 5) * BARS_PER_DAY[interval])
    return w


def load_market(symbols: list[str], interval: str, bars: int, cache: dict) -> dict[str, pd.DataFrame]:
    out = {}
    for s in symbols:
        key = (s, interval)
        if key not in cache or len(cache[key]) < bars:
            cache[key] = fetch_candles(s, interval, bars)[0]
        out[s] = cache[key].tail(bars).reset_index(drop=True)
    return out


def account_part(bot: dict, since: pd.Timestamp, now: pd.Timestamp) -> tuple[list[str], dict]:
    cfg, p = bot_settings(bot)
    lines = [f"## 1. Qué hizo {bot.get('trader') or bot['name']} ({bot['name']})", "",
             f"Periodo: {since:%Y-%m-%d %H:%M} → {now:%Y-%m-%d %H:%M} UTC "
             f"({(now - since).total_seconds() / 3600:.0f} h). Temporalidad {cfg.interval}, "
             f"pares {', '.join(s.replace('USDT', '') for s in cfg.symbols)}, cortos "
             f"{'sí' if p.allow_short else 'no'}, estrategia {p.strategy}.", ""]
    if not Path(cfg.db_path).exists():
        return lines + [f"No existe {cfg.db_path}: el bot no se ha encendido."], {"cfg": cfg, "p": p}
    since_iso = since.isoformat()
    with StateManager(cfg.db_path, cfg.initial_balance, p) as sm:
        acc, m = sm.account(), sm.metrics()
        trades = [t for t in sm.trades() if _ts(t["closed_at"]) >= since]
        positions = sm.positions()
        controls = [c for c in sm.control_log(200) if _ts(c["at"]) >= since]
        cycle = sm.meta_get("last_cycle")
        older = [t for t in sm.trades() if _ts(t["closed_at"]) < since]
        bal_before = older[-1]["balance_after"] if older else acc["initial_balance"]
    prices = {}
    for pos in positions:
        try:
            prices[pos["symbol"]] = fetch_price(pos["symbol"])[0]
        except Exception:
            pass
    latent = sum(x["position_units"] * (prices[x["symbol"]] - x["entry_price"]) * (1 if x["side"] == "LONG" else -1)
                 for x in positions if x["symbol"] in prices)
    pnl = sum(t["pnl_usd"] for t in trades)
    fees = sum(t["fees_usd"] for t in trades)
    res = Counter(t["result"] for t in trades)
    wr = "—" if m["win_rate_pct"] is None else f"{m['win_rate_pct']:.1f}%"
    lines += ["| Dato | Valor |", "|---|---|",
              f"| Balance inicial de la cuenta | {acc['initial_balance']:.2f} USD |",
              f"| Balance al empezar el periodo | {bal_before:.2f} USD |",
              f"| Balance ahora (realizado) | {acc['balance']:.2f} USD |",
              f"| Resultado realizado en el periodo | {pnl:+.2f} USD ({pnl / bal_before * 100:+.2f}%) |",
              f"| Latente de posiciones abiertas | {latent:+.2f} USD |",
              f"| Comisiones del periodo | {fees:.2f} USD |",
              f"| Cierres en el periodo | {len(trades)} (ganadas {res['WIN']}, perdidas {res['LOSS']}, "
              f"break-even {res['BREAKEVEN']}) |",
              f"| Capa 1 ahora | {acc['status']}, pérdidas seguidas {acc['consecutive_losses']}, "
              f"enfriamiento {acc['cooldown_bars_remaining']} velas{', KILL SWITCH' if acc['manual_halt'] else ''} |",
              f"| Historial completo | {m['trades']} operaciones, win rate {wr}, "
              f"PF {m['profit_factor'] if m['profit_factor'] is not None else '—'}, "
              f"neto {m['net_pnl_usd']:+.2f} USD ({m['net_pnl_pct']:+.2f}%) |", ""]
    if trades:
        lines += ["| Par | Lado | Resultado | Salida | Entrada → salida | PnL USD | Abierta → cerrada (UTC) |",
                  "|---|---|---|---|---|---|---|"]
        for t in trades:
            lines.append(f"| {t['symbol']} | {t['side']} | {t['result']} | {t['exit_reason']} | "
                         f"{t['entry_price']:g} → {t['exit_price']:g} | {t['pnl_usd']:+.2f} | "
                         f"{_ts(t['opened_at']):%m-%d %H:%M} → {_ts(t['closed_at']):%m-%d %H:%M} |")
        lines.append("")
    for x in positions:
        px = prices.get(x["symbol"])
        lines.append(f"- Abierta {x['symbol']} {x['side']} desde {_ts(x['opened_at']):%m-%d %H:%M}: entrada "
                     f"{x['entry_price']:g}, SL {x['stop_loss']:g}, TP {x['take_profit']:g}, precio ahora "
                     f"{'—' if px is None else f'{px:g}'}")
    for c in controls:
        lines.append(f"- Control {c['at']}: {c['action']} {c['status_before']}→{c['status_after']} "
                     f"({c.get('source') or ''} {c.get('reason') or ''})")
    if cycle:
        cyc = json.loads(cycle)
        lines.append(f"- Última vela procesada: {cyc.get('processed_at')}")
    lines.append("")
    return lines, {"cfg": cfg, "p": p}


def filter_part(cfg, p: q.Params, since: pd.Timestamp, cache: dict) -> list[str]:
    """Recalcula la Capa 2 en cada vela cerrada del periodo, con las mismas velas que usa el bot."""
    n_bars = int((pd.Timestamp.now(tz="UTC") - since).total_seconds() // (q_step(cfg.interval) / 1000)) + 2
    data = load_market(cfg.symbols, cfg.interval, warmup_bars(p, cfg.interval) + n_bars, cache)
    lines = ["## 2. Por qué no entró: la Capa 2 vela a vela", "",
             "| Par | Mercado en el periodo | Velas | Señales | Régimen más frecuente | Filtro que más bloqueó |",
             "|---|---|---|---|---|---|"]
    total_block, total_bars, total_sig = Counter(), 0, 0
    for s, df in data.items():
        ind = q.compute_indicators(df, p)
        ind = ind[pd.to_datetime(ind["time"], utc=True) >= since]
        if ind.empty:
            lines.append(f"| {s} | sin velas | 0 | 0 | — | — |")
            continue
        reg, blk, sig = Counter(), Counter(), 0
        for _, row in ind.iterrows():
            vals = {k: float(row[k]) for k in q.INDICATOR_KEYS}
            try:
                r = q.evaluate_layer2(vals, p)
            except ValueError:
                continue
            reg[r["regime"]] += 1
            if r["signal"] in ("LONG", "SHORT"):
                sig += 1
            for b in r["blocking_filters"]:
                blk[b.split(":")[0]] += 1
        first, last = float(ind["open"].iloc[0]), float(ind["close"].iloc[-1])
        hi, lo = float(ind["high"].max()), float(ind["low"].min())
        total_block.update(blk)
        total_bars += len(ind)
        total_sig += sig
        top_reg = ", ".join(f"{k} {v}" for k, v in reg.most_common(2))
        top_blk = ", ".join(f"{k} {v}" for k, v in blk.most_common(2)) or "—"
        lines.append(f"| {s.replace('USDT', '')} | {(last / first - 1) * 100:+.2f}% (rango {(hi / lo - 1) * 100:.1f}%) | "
                     f"{len(ind)} | {sig} | {top_reg} | {top_blk} |")
    lines += ["", f"Total: {total_bars} velas evaluadas, {total_sig} señales de entrada.", "",
              "Filtros que bloquearon (veces, sumando todos los pares):", ""]
    lines += [f"- {k}: {v}" for k, v in total_block.most_common(8)]
    return lines + [""]


def q_step(interval: str) -> int:
    from market_fetcher import INTERVAL_MS
    return INTERVAL_MS[interval]


def backtest_part(cfg, p_live: q.Params, balance: float, since: pd.Timestamp, cache: dict) -> list[str]:
    now = pd.Timestamp.now(tz="UTC")
    lines = ["## 3. ¿Otra configuración lo habría hecho mejor?", "",
             f"Backtest con el motor real (Capa 1 → 2 → 3), {balance:.0f} USD, mismos pares, comisión 0,08% y "
             "deslizamiento 0,02% por orden. Retorno % · caída máxima % · operaciones · win rate.", "",
             "| Configuración | " + " | ".join(w for w, _ in WINDOWS) + " |",
             "|---|" + "---|" * len(WINDOWS)]
    for c in candidates(p_live, cfg.interval):
        bpd = BARS_PER_DAY[c["interval"]]
        bars = warmup_bars(c["p"], c["interval"]) + 366 * bpd + 2
        try:
            data = load_market(cfg.symbols, c["interval"], bars, cache)
        except Exception as exc:
            lines.append(f"| {c['label']} | sin datos: {exc} |")
            continue
        cells = []
        for _, days in WINDOWS:
            start = since if days is None else now - pd.Timedelta(days=days)
            try:
                rep = run_portfolio_backtest(data, c["p"], BacktestConfig(initial_balance=balance),
                                             trade_from=start)["report"]
                wr = "—" if rep["win_rate_pct"] is None else f"{rep['win_rate_pct']:.0f}%"
                cells.append(f"{rep['net_return_pct']:+.2f}% · {rep['max_drawdown_pct']:.1f}% · "
                             f"{rep['trades']} · {wr}")
            except Exception as exc:
                cells.append(f"error: {type(exc).__name__}")
        lines.append(f"| {c['label']} | " + " | ".join(cells) + " |")
        print(f"[bot_review] {c['label']}: listo", file=sys.stderr)
    hold = []
    for s in cfg.symbols:
        df = cache.get((s, "1d"))
        if df is None:
            df = fetch_candles(s, "1d", 400)[0]
        df = df.assign(t=pd.to_datetime(df["time"], utc=True))
        row = []
        for _, days in WINDOWS:
            start = since if days is None else now - pd.Timedelta(days=days)
            sub = df[df.t >= start.floor("D")]
            row.append((float(sub["close"].iloc[-1]) / float(sub["open"].iloc[0]) - 1) * 100 if len(sub) else float("nan"))
        hold.append(row)
    avg = [sum(r[i] for r in hold) / len(hold) for i in range(len(WINDOWS))]
    lines.append("| Comprar y mantener (promedio de los pares, sin caída máx.) | " +
                 " | ".join(f"{a:+.2f}%" for a in avg) + " |")
    return lines + ["", "Lectura: una ventana de 2 días es ruido; la de 1 año es la que pesa para decidir.", ""]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Revisión de solo lectura de un bot de deploy/bots.json")
    ap.add_argument("--bot", default="oficial")
    ap.add_argument("--since", required=True, help="Inicio del periodo, UTC (p. ej. 2026-10-26T01:13)")
    ap.add_argument("--balance", type=float, default=500.0, help="Balance de los backtests (por defecto 500)")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--out", help="Además, guarda el informe en este archivo")
    ap.add_argument("--no-backtest", action="store_true")
    args = ap.parse_args(argv)
    load_env_file(args.env_file)

    bot = find_bot(args.bot)
    since, now = _ts(args.since), pd.Timestamp.now(tz="UTC")
    lines = [f"# Revisión de {bot.get('trader') or bot['name']} · {now:%Y-%m-%d %H:%M} UTC", "",
             "Todo en paper trading (dinero simulado). Este informe solo lee datos.", ""]
    part1, ctx = account_part(bot, since, now)
    lines += part1
    cache: dict = {}
    lines += filter_part(ctx["cfg"], ctx["p"], since, cache)
    others = [b for b in load_bots() if b["id"] != bot["id"]]
    if others:
        lines += ["## Los otros bots en el mismo periodo", "", "| Bot | Balance | Cierres | Realizado |", "|---|---|---|---|"]
        for b in others:
            cfg_b, p_b = bot_settings(b)
            if not Path(cfg_b.db_path).exists():
                continue
            with StateManager(cfg_b.db_path, cfg_b.initial_balance, p_b) as sm:
                acc, per = sm.account(), sm.pnl_since(since.isoformat())
                n_open = len(sm.positions())
            lines.append(f"| {b.get('trader') or ''} · {b['name']} | {acc['balance']:.2f} | {per['trades']} "
                         f"(+{n_open} abiertas) | {per['pnl_usd']:+.2f} USD |")
        lines.append("")
    if not args.no_backtest:
        lines += backtest_part(ctx["cfg"], ctx["p"], args.balance, since, cache)
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[bot_review] guardado en {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
