#!/usr/bin/env python3
"""
exit_audit.py — Auditoría de SOLO LECTURA de las salidas de todos los bots en papel.

Responde a la queja "los bots solo cierran en pérdida y nunca en ganancia":
  1. Por bot: balance, operaciones cerradas por motivo de salida, ganadas/perdidas.
  2. Por operación cerrada: cuánto llegó a ganar en su mejor momento (MFE, en % y en R) antes de cerrar,
     y dónde estaba el stop al cerrar (¿ya protegía ganancia?).
  3. Por posición abierta: ganancia latente al precio actual, mejor precio visto (trail_peak), dónde está el stop
     ahora (¿ya asegura ganancia o todavía arriesga?) y cuánto devolvería si lo toca.
  4. IA: votos del guardián de salidas, cierres pedidos y permisos, para ver si la IA interfiere.

No escribe en ninguna base ni toca servicios. Uso en el VPS (desde /opt/Ingresosbot):
  sudo -u ingresosbot nice .venv/bin/python exit_audit.py --out data/salidas/auditoria.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

import pandas as pd

from market_fetcher import fetch_candles, fetch_price

HOURS = {"15m": 0.25, "1h": 1, "4h": 4, "1d": 24}


def ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


_cache: dict = {}


def candles_since(symbol: str, start: pd.Timestamp) -> pd.DataFrame | None:
    """Velas de 1h desde start (o de 4h si es muy viejo). None si no hay datos."""
    now = pd.Timestamp.now(tz="UTC")
    hours = (now - start).total_seconds() / 3600 + 3
    iv = "1h" if hours <= 990 else "4h"
    n = int(hours / HOURS[iv]) + 3
    key = (symbol, iv)
    if key not in _cache or len(_cache[key]) < n:
        try:
            _cache[key] = fetch_candles(symbol, iv, min(n, 1000))[0]
        except Exception as exc:  # moneda deslistada, sin red...
            print(f"  (sin velas de {symbol}: {exc})")
            return None
    return _cache[key]


def excursion(symbol: str, side: str, opened: str, closed: str | None) -> tuple[float | None, float | None]:
    """(mejor precio, peor precio) entre apertura y cierre (vela a vela)."""
    df = candles_since(symbol, ts(opened))
    if df is None or df.empty:
        return None, None
    t = pd.to_datetime(df["time"], utc=True)
    m = t >= ts(opened).floor("h")
    if closed:
        m &= t <= ts(closed)
    w = df[m]
    if w.empty:
        return None, None
    hi, lo = float(w["high"].max()), float(w["low"].min())
    return (hi, lo) if side == "LONG" else (lo, hi)


def pct(a: float, b: float, side: str) -> float:
    return (b / a - 1) * 100 * (1 if side == "LONG" else -1)


def audit_bot(b: dict, lines: list[str]) -> dict:
    db = b.get("db")
    name = f"{b.get('trader') or ''} · {b.get('name')} ({b.get('id')}, {b.get('interval')})".strip(" ·")
    lines.append(f"\n## {name}\n")
    if not db or not Path(db).exists():
        lines.append(f"Sin base ({db}).\n")
        return {}
    con = ro(db)
    acc = dict(con.execute("SELECT * FROM account_state WHERE id = 1").fetchone() or {})
    lines.append(f"Balance {acc.get('balance', 0):.2f} de {acc.get('initial_balance', 0):.2f} · estado "
                 f"{acc.get('status')} · pérdidas seguidas {acc.get('consecutive_losses')} · apagado manual "
                 f"{acc.get('manual_halt')} · actualizado {acc.get('updated_at')}\n")
    trades = [dict(r) for r in con.execute("SELECT * FROM trade_history ORDER BY closed_at")]
    pos = [dict(r) for r in con.execute("SELECT * FROM open_positions ORDER BY opened_at")]
    stats = {"bot": b.get("id"), "trades": len(trades), "open": len(pos)}

    # ── Cerradas ──────────────────────────────────────────────────────────
    if trades:
        res = Counter(t["result"] for t in trades)
        why = Counter(t["exit_reason"] for t in trades)
        net = sum(t["pnl_usd"] for t in trades)
        fees = sum(t["fees_usd"] for t in trades)
        lines.append(f"**Cerradas: {len(trades)}** · {dict(res)} · motivos {dict(why)} · neto {net:+.2f} USD "
                     f"(comisiones {fees:.2f})\n")
        lines.append("| Par | Lado | Abrió | Cerró | Motivo | Entrada | Salida | Resultado | R | Mejor momento | "
                     "Mejor en R | Devolvió |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        gave_back = 0
        for t in trades:
            best, _ = excursion(t["symbol"], t["side"], t["opened_at"], t["closed_at"])
            r = t["r_multiple"]
            risk_unit = None
            if r and abs(r) > 1e-9 and t["position_units"]:
                risk_unit = abs(t["pnl_usd"] / r) / t["position_units"]
            mfe_pct = pct(t["entry_price"], best, t["side"]) if best else None
            mfe_r = (abs(best - t["entry_price"]) * (1 if mfe_pct and mfe_pct > 0 else -1) / risk_unit
                     if best and risk_unit else None)
            if mfe_r is not None and mfe_r >= 1 and t["pnl_usd"] < 0:
                gave_back += 1
            lines.append(
                f"| {t['symbol']} | {t['side']} | {t['opened_at'][:16]} | {t['closed_at'][:16]} | {t['exit_reason']} | "
                f"{t['entry_price']:.6g} | {t['exit_price']:.6g} | {t['pnl_usd']:+.2f} ({t['pnl_pct']:+.2f}%) | "
                f"{r:+.2f} | {'' if mfe_pct is None else f'{mfe_pct:+.2f}%'} | "
                f"{'' if mfe_r is None else f'{mfe_r:+.2f}'} | "
                f"{'' if mfe_pct is None else '%.2f pts' % (mfe_pct - t['pnl_pct'])} |")
        lines.append(f"\nOperaciones que llegaron a ganar ≥1 R y terminaron en pérdida: **{gave_back}**\n")
        stats.update(net=net, wins=res.get("WIN", 0), losses=res.get("LOSS", 0), gave_back=gave_back,
                     reasons=dict(why))

    # ── Abiertas ──────────────────────────────────────────────────────────
    if pos:
        lines.append(f"**Abiertas: {len(pos)}**\n")
        lines.append("| Par | Lado | Abrió | Entrada | Precio ahora | Latente | Latente R | Mejor visto | Stop inicial | "
                     "Stop ahora | Stop asegura | TP | En BE |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        locked_n = 0
        for p in pos:
            try:
                px = float(fetch_price(p["symbol"])[0])
            except Exception:
                px = None
            side, e = p["side"], p["entry_price"]
            risk_unit = abs(e - p["initial_stop_loss"]) or None
            best = p["trail_peak"]
            if best is None:
                best, _ = excursion(p["symbol"], side, p["opened_at"], None)
            lat = pct(e, px, side) if px else None
            lat_r = ((px - e) * (1 if side == "LONG" else -1) / risk_unit) if (px and risk_unit) else None
            lock = pct(e, p["stop_loss"], side)
            locked_n += lock > 0
            lines.append(
                f"| {p['symbol']} | {side} | {p['opened_at'][:16]} | {e:.6g} | {'' if px is None else f'{px:.6g}'} | "
                f"{'' if lat is None else f'{lat:+.2f}%'} | {'' if lat_r is None else f'{lat_r:+.2f}'} | "
                f"{'' if best is None else f'{best:.6g} ({pct(e, best, side):+.2f}%)'} | {p['initial_stop_loss']:.6g} | "
                f"{p['stop_loss']:.6g} | {lock:+.2f}% | {p['take_profit'] or '-'} | {p['is_at_breakeven']} |")
        lines.append(f"\nPosiciones cuyo stop ya asegura ganancia: **{locked_n} de {len(pos)}**\n")
        stats["locked"] = locked_n

    # ── Bitácora de control ───────────────────────────────────────────────
    try:
        ctl = [dict(r) for r in con.execute("SELECT * FROM control_log ORDER BY id DESC LIMIT 10")]
        if ctl:
            lines.append("Últimos controles: " + "; ".join(f"{c['at'][:16]} {c['action']} ({c['source']})" for c in ctl))
    except sqlite3.Error:
        pass
    return stats


def audit_ai(lines: list[str]) -> None:
    path = Path("data/ai_state.db")
    lines.append("\n## IA (guardián de salidas)\n")
    if not path.exists():
        lines.append("Sin data/ai_state.db.\n")
        return
    con = ro(str(path))
    q = lambda s: [dict(r) for r in con.execute(s)]  # noqa: E731
    try:
        rows = q("SELECT bot, decision, mode, COUNT(*) n FROM ai_decisions WHERE agent = 'salida' "
                 "GROUP BY bot, decision, mode ORDER BY bot")
        lines.append("Votos de salida por bot: " + (json.dumps(rows, ensure_ascii=False) if rows else "ninguno"))
        last = q("SELECT at, bot, ref, decision, mode, outcome_r, alt_r FROM ai_decisions WHERE agent = 'salida' "
                 "ORDER BY id DESC LIMIT 25")
        for r in last:
            lines.append(f"- {r['at'][:16]} {r['bot']} {r['ref']} → {r['decision']} ({r['mode']}) real R {r['outcome_r']} "
                         f"· si cerraba R {r['alt_r']}")
        req = q("SELECT * FROM ai_exit_requests ORDER BY id DESC LIMIT 30")
        lines.append(f"\nCierres pedidos por la IA: {len(req)}")
        for r in req:
            lines.append(f"- {r['created_at'][:16]} {r['bot']} {r['symbol']} {r['status']} · {str(r['reason'])[:160]}")
        perms = q("SELECT profile, permission, status, since FROM ai_permissions ORDER BY profile")
        lines.append("\nPermisos: " + json.dumps(perms, ensure_ascii=False))
        votes = q("SELECT bot, decision, mode, COUNT(*) n FROM ai_decisions WHERE agent = 'analista' "
                  "AND at >= date('now', '-30 day') GROUP BY bot, decision, mode ORDER BY bot")
        lines.append("\nAnalista (30 días): " + json.dumps(votes, ensure_ascii=False))
    except sqlite3.Error as exc:
        lines.append(f"Error leyendo ai_state.db: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--bots", default="deploy/bots.json")
    ap.add_argument("--out")
    a = ap.parse_args()
    bots = json.loads(Path(a.bots).read_text(encoding="utf-8"))
    lines = [f"# Auditoría de salidas · {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC"]
    summary = [audit_bot(b, lines) for b in bots]
    old = Path("data/trading_state.db")
    if old.exists():
        summary.append(audit_bot({"id": "oficial-viejo", "name": "Oficial (historial viejo)", "db": str(old),
                                  "interval": "1h"}, lines))
    audit_ai(lines)
    lines.append("\n## Resumen\n")
    lines.append("```json\n" + json.dumps([s for s in summary if s], ensure_ascii=False, indent=1) + "\n```")
    text = "\n".join(lines)
    print(text)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
