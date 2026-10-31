#!/usr/bin/env python3
"""
listing_bot.py — Bot "Nuevos listados" en paper trading 24/7 (la única regla de listing_lab.py que pasó).

Regla (docs/LISTADOS.md, corrida del 2026-10-31 en el VPS):
  - Vigila cada moneda nueva de Binance Spot contra USDT durante sus primeros 14 días (336 velas de 1 h).
  - El máximo del día 1 es el máximo de sus primeras 24 velas de 1 h.
  - Compra cuando una vela de 1 h cierra por encima de ese máximo, solo si es la PRIMERA vez (si ya rompió antes,
    no compra tarde). Una sola operación por listado.
  - Stop a -15% de la entrada; luego trailing: 15% bajo el máximo desde la entrada (solo sube). Sale como mucho a
    las 168 h (7 días). Sin take profit fijo.
  - 10% del saldo por operación (5% si la Capa 1 está en THROTTLED), máx. 3 abiertas.
  - Capa 1 igual que los demás bots (3 pérdidas → 15 velas sin operar; caída del día ≥ 3% → HALTED) y, además,
    si el saldo cae 20% desde su máximo el bot se apaga solo (kill switch) y avisa.
Costos simulados como en el laboratorio: 0,08% de comisión por orden (StateManager) y 0,2% de deslizamiento
por lado. Se excluyen stablecoins, tokens apalancados, bStocks (acciones tokenizadas) y tickers nuevos de activos
viejos (listing_lab.NOT_NEW).

Las salidas se revisan con cada vela de 1 h cerrada (como una orden stop: si la vela abre bajo el stop sale a la
apertura; si solo lo toca, sale al stop), igual que el laboratorio. El botón "Apagar y cerrar" del panel se
atiende cada 15 s.

Panel: misma base SQLite que los demás (data/listados_state.db) y "watching" en last_cycle con las monedas que
vigila; en deploy/bots.json la ficha lleva "dynamic_symbols": true.

Uso en el VPS (servicio deploy/ingresosbot-listados.service):
  python listing_bot.py --env-file .env --log-file logs/bot-listados.log
  python listing_bot.py --once          # un ciclo de vela y termina (pruebas)
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

import market_fetcher as mf
import quant_core_engine as q
from listing_lab import LEVERAGED, NOT_NEW, STABLES, is_tokenized_stock
from notifier import Notifier, load_env_file
from state_manager import StateManager

log = logging.getLogger("listing_bot")

H_MS = 3_600_000
DAY1_BARS = 24                 # el máximo del día 1 = primeras 24 velas de 1 h
WINDOW_BARS = 336              # se acepta la ruptura hasta el día 14
HOLD_BARS = 168                # salida por tiempo a las 168 h
SL_PCT = 0.15
TRAIL_PCT = 0.15
ALLOC = 0.10
MAX_OPEN = 3
MIN_NOTIONAL = 5.0
SLIP = 0.002
KILL_DD_PCT = 20.0
HEARTBEAT_S = 60.0
LABEL = "Nuevos listados"

K_KNOWN, K_DONE, K_POS, K_PEAK, K_KILLED = "listings_known", "listings_done", "listing_positions", "peak_balance", \
    "listings_killed"


def _iso(ms: int) -> str:
    return pd.Timestamp(ms, unit="ms", tz="UTC").isoformat()


def _hosts() -> list[str]:
    """Hosts de Binance en el orden de MARKET_SOURCES (Bybit no lista igual las monedas nuevas)."""
    by_name = {"binance": mf.BINANCE_HOSTS[0], "binance_vision": mf.BINANCE_HOSTS[1]}
    hosts = [by_name[n] for n in mf.configured_sources() if n in by_name]
    return hosts or list(mf.BINANCE_HOSTS)


def candidate(symbol: str) -> bool:
    """Pareja USDT que puede ser una moneda nueva (sin stablecoins, apalancados ni activos viejos re-listados)."""
    if not symbol.endswith("USDT") or not symbol.isalnum():
        return False
    base = symbol[:-4]
    return bool(base) and base not in STABLES and base not in NOT_NEW and not base.endswith(LEVERAGED)


class ListingBot:
    def __init__(self, db_path: str = "data/listados_state.db", balance: float = 500.0, fee_pct: float = 0.08,
                 notifier: Notifier | None = None, get: Callable[[str], object] | None = None,
                 price_source: Callable[[str], float] | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, hosts: list[str] | None = None,
                 watch_seconds: float = 15.0, close_delay_seconds: float = 10.0, pause: float = 0.05):
        self.sm = StateManager(db_path, balance, q.Params(allow_short=False), fee_pct)
        self.db_path = db_path
        self.notifier = notifier or Notifier.from_env(label=LABEL)
        self.http = get or (lambda url: mf._http_get_json(url, timeout=20))
        self.hosts = hosts or _hosts()
        self.price_source = price_source or (lambda s: mf.fetch_price(s)[0])
        self.clock, self.sleep = clock, sleep
        self.watch_seconds, self.close_delay = watch_seconds, close_delay_seconds
        self.pause = pause
        self._seen_transitions = 0
        self._last_beat = 0.0
        self._stop = False

    # ── Utilidades ──────────────────────────────────────────────────────────
    def _get(self, path: str):
        last = None
        for host in self.hosts:
            try:
                return self.http(f"{host}{path}")
            except Exception as exc:          # 451 desde algunos países: se prueba el otro host
                last = exc
        raise mf.FetchError(f"ningún host de Binance respondió {path.split('?')[0]}: {last}")

    def _meta(self, key: str, default):
        raw = self.sm.meta_get(key)
        try:
            return json.loads(raw) if raw else default
        except ValueError:
            return default

    def _set_meta(self, key: str, value) -> None:
        self.sm.meta_set(key, json.dumps(value, ensure_ascii=False))

    def klines(self, symbol: str, start_ms: int, now_ms: int) -> pd.DataFrame:
        qs = urllib.parse.urlencode({"symbol": symbol, "interval": "1h", "startTime": int(start_ms), "limit": 1000})
        data = self._get(f"/api/v3/klines?{qs}")
        if not isinstance(data, list):
            raise mf.FetchError(f"{symbol}: respuesta inesperada {str(data)[:120]}")
        if not data:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        return mf._to_frame([list(r[:7]) for r in data], now_ms)

    def last_closed_bar_open_ms(self, now_s: float) -> int:
        return (int(now_s * 1000) // H_MS) * H_MS - H_MS

    def heartbeat(self, now_s: float) -> None:
        if now_s - self._last_beat < HEARTBEAT_S:
            return
        at = pd.Timestamp(now_s, unit="s", tz="UTC").isoformat()
        if self.sm.meta_get("started_at") is None:
            self.sm.meta_set("started_at", at)
        self.sm.meta_set("heartbeat", at)
        self._last_beat = now_s

    def stop(self, *_):
        self._stop = True

    def _flush_status_alerts(self) -> None:
        new = self.sm.status_transitions[self._seen_transitions:]
        self._seen_transitions = len(self.sm.status_transitions)
        acc = self.sm.account()
        for t in new:
            log.info("Circuit Breaker %s -> %s (%s)", t["from"], t["to"], "; ".join(t["reasons"]))
            self.notifier.status_changed(t, acc)

    def _closed(self, trade: dict) -> dict:
        log.info("Cerrada %s %s %s PnL %.2f", trade["symbol"], trade["exit_reason"], trade["result"], trade["pnl_usd"])
        self.notifier.position_closed(trade, self.sm.account())
        pos = self._meta(K_POS, {})
        pos.pop(str(trade["position_id"]), None)
        self._set_meta(K_POS, pos)
        return {"event": "CLOSE", **trade}

    # ── Descubrir listados ──────────────────────────────────────────────────
    def discover(self) -> dict[str, int]:
        """symbol → ms de su primera vela, para cada pareja USDT que Binance cotiza hoy. Solo pregunta la fecha
        de listado de las que aún no conoce (la primera vez son todas: ~1 minuto)."""
        known: dict[str, int] = {k: int(v) for k, v in self._meta(K_KNOWN, {}).items()}
        tickers = self._get("/api/v3/ticker/price")
        new = [t["symbol"] for t in tickers if isinstance(t, dict) and candidate(t.get("symbol", ""))
               and t["symbol"] not in known]
        for sym in new:
            try:
                first = self._get("/api/v3/klines?" + urllib.parse.urlencode(
                    {"symbol": sym, "interval": "1h", "startTime": 0, "limit": 1}))
            except mf.FetchError as exc:
                log.warning("Sin fecha de listado para %s: %s", sym, exc)
                continue
            if isinstance(first, list) and first:
                known[sym] = int(first[0][0])
            if self.pause:
                self.sleep(self.pause)
        if new:
            self._set_meta(K_KNOWN, known)
        return known

    def watchlist(self, known: dict[str, int], bar_open: int, done: set[str]) -> dict[str, int]:
        out = {}
        for sym, first in known.items():
            age = (bar_open - first) // H_MS          # índice de la última vela cerrada desde el listado
            if sym in done or age < 0 or age >= WINDOW_BARS:
                continue
            if is_tokenized_stock(sym, pd.Timestamp(first, unit="ms", tz="UTC")):
                continue
            out[sym] = first
        return dict(sorted(out.items(), key=lambda kv: kv[1]))

    # ── Entradas ────────────────────────────────────────────────────────────
    def check_entry(self, sym: str, first: int, bar_open: int, now_ms: int) -> dict:
        """WAIT (sigue vigilando), LONG (ruptura en esta vela), SKIPPED (ya no se comprará) o STALE_DATA."""
        df = self.klines(sym, first, now_ms)
        df = df[df["time"] <= pd.Timestamp(bar_open, unit="ms", tz="UTC")].reset_index(drop=True)
        if df.empty or pd.Timestamp(df["time"].iloc[-1]) != pd.Timestamp(bar_open, unit="ms", tz="UTC"):
            return {"decision": "STALE_DATA", "reason": "Binance aún no publica la última vela"}
        n = len(df)
        if n <= DAY1_BARS:
            return {"decision": "WAIT / NO_TRADE",
                    "reason": f"día 1: formando el máximo de las primeras 24 h ({n}/24 h)"}
        h, c = df["high"].to_numpy(float), df["close"].to_numpy(float)
        day1 = float(h[:DAY1_BARS].max())
        last = n - 1
        hits = np.flatnonzero(c[DAY1_BARS:last + 1] > day1) + DAY1_BARS
        if len(hits) and hits[0] < last:
            when = pd.Timestamp(df["time"].iloc[hits[0]]).strftime("%d-%b %H:%M")
            return {"decision": "SKIPPED", "done": True,
                    "reason": f"ya rompió el máximo del día 1 ({day1:.6g}) el {when} UTC; no se compra tarde"}
        if last >= WINDOW_BARS:
            return {"decision": "SKIPPED", "done": True, "reason": "pasaron 14 días sin ruptura"}
        if not len(hits):
            gap = (day1 / c[last] - 1) * 100
            return {"decision": "WAIT / NO_TRADE", "day1_high": day1,
                    "reason": f"esperando que cierre sobre el máximo del día 1 ({day1:.6g}, falta {gap:.1f}%)"}
        return {"decision": "LONG", "day1_high": day1, "close": float(c[last]),
                "reason": f"cerró en {c[last]:.6g} sobre el máximo del día 1 ({day1:.6g})"}

    def open_trade(self, sym: str, bar_open: int, at: str, day1: float) -> dict:
        acc = self.sm.account()
        if acc["manual_halt"] or acc["status"] == "HALTED":
            why = "apagado desde el panel o por el -20%" if acc["manual_halt"] else "Capa 1 en HALTED"
            return {"decision": "SKIPPED", "done": True, "reason": f"había ruptura, pero {why}"}
        if len(self.sm.positions()) >= MAX_OPEN:
            return {"decision": "SKIPPED", "done": True, "reason": f"había ruptura, pero ya hay {MAX_OPEN} posiciones"}
        alloc = ALLOC / 2 if acc["status"] == "THROTTLED" else ALLOC
        price = float(self.price_source(sym))
        entry = price * (1 + SLIP)
        notional = acc["balance"] * alloc
        if notional < MIN_NOTIONAL or not entry > 0:
            return {"decision": "SKIPPED", "done": True, "reason": "saldo insuficiente para el mínimo de Binance"}
        units = notional / entry
        sl = entry * (1 - SL_PCT)
        pid = self.sm.open_position(sym, "LONG", entry, sl, 0.0, 0.0, 0.0, units, units * (entry - sl), at,
                                    regime="LISTADO", risk_mode=f"{alloc:.0%} del saldo")
        self.sm.conn.execute("UPDATE open_positions SET trail_peak = ? WHERE id = ?", (entry, pid))
        self.sm.conn.commit()
        pos = self._meta(K_POS, {})
        pos[str(pid)] = {"entry_bar": bar_open + H_MS, "last_bar": bar_open, "day1_high": day1}
        self._set_meta(K_POS, pos)
        log.info("Compra %s @ %.8g (%s USD, SL %.8g)", sym, entry, round(notional, 2), sl)
        self.notifier.send("position_opened", {"symbol": sym, "entry_price": entry, "stop_loss": sl,
                                               "units": units, "notional_usd": notional},
                           f"🟢 COMPRA (papel) {sym}\n"
                           f"Rompió el máximo de su primer día ({day1:.6g}). Entrada: {entry:.6g}\n"
                           f"Monto: {notional:,.2f} USD ({alloc:.0%} del saldo) · SL: {sl:.6g} (-15%)\n"
                           f"Trailing 15% bajo el máximo (solo sube) · salida como mucho en 7 días\n"
                           f"Balance: {acc['balance']:,.2f} USD")
        return {"decision": "LONG", "done": True, "position_id": pid,
                "reason": f"compra a {entry:.6g} tras romper el máximo del día 1 ({day1:.6g})"}

    # ── Salidas (con cada vela de 1 h cerrada) ──────────────────────────────
    def manage(self, pos: dict, bar_open: int, now_ms: int) -> dict:
        meta = self._meta(K_POS, {}).get(str(pos["id"]))
        if meta is None:                     # posición sin ficha (no debería pasar): se adopta desde ahora
            meta = {"entry_bar": bar_open + H_MS, "last_bar": bar_open}
        df = self.klines(pos["symbol"], meta["last_bar"] + H_MS, now_ms)
        df = df[df["time"] <= pd.Timestamp(bar_open, unit="ms", tz="UTC")]
        stop, peak = pos["stop_loss"], pos["trail_peak"] or pos["entry_price"]
        entry_bar = int(meta["entry_bar"])
        for row in df.itertuples(index=False):
            t = int(pd.Timestamp(row.time).timestamp() * 1000)
            px, why = None, None
            if t > entry_bar and row.open <= stop:
                px = row.open
            elif row.low <= stop:
                px = stop
            elif t >= entry_bar + HOLD_BARS * H_MS:
                px, why = row.close, "TIEMPO"
            if px is not None:
                why = why or ("STOP_LOSS" if stop == pos["initial_stop_loss"] else "TRAILING_STOP")
                trade = self.sm.close_position(pos["id"], px * (1 - SLIP), why, _iso(t + H_MS))
                self._closed(trade)
                return {"decision": "SKIPPED", "reason": f"vendida: {why} ({trade['pnl_usd']:+.2f} USD)"}
            peak = max(peak, row.high)
            new_stop = max(stop, peak * (1 - TRAIL_PCT))
            if new_stop > stop:
                if stop <= pos["entry_price"] < new_stop:
                    self.notifier.send("trailing_stop", {"symbol": pos["symbol"], "new_stop_loss": new_stop},
                                       f"📈 TRAILING {pos['symbol']}: el stop sube a {new_stop:.6g}, sobre la entrada "
                                       f"({pos['entry_price']:.6g}). Esta operación ya no puede perder.")
                stop = new_stop
            meta["last_bar"] = t
        self.sm.conn.execute("UPDATE open_positions SET stop_loss = ?, trail_peak = ? WHERE id = ?",
                             (stop, peak, pos["id"]))
        self.sm.conn.commit()
        allm = self._meta(K_POS, {})
        allm[str(pos["id"])] = meta
        self._set_meta(K_POS, allm)
        left = (entry_bar + HOLD_BARS * H_MS - bar_open - H_MS) // H_MS
        return {"decision": "HOLD", "reason": f"SL {stop:.6g} (máximo {peak:.6g}) · sale como mucho en {left} h"}

    # ── Kill switch propio: -20% desde el máximo del saldo ──────────────────
    def kill_check(self) -> None:
        acc = self.sm.account()
        bal = acc["balance"]
        killed = self.sm.meta_get(K_KILLED)
        if killed and not acc["manual_halt"]:    # lo volvieron a encender desde el panel: se cuenta desde hoy
            self.sm.conn.execute("DELETE FROM runner_meta WHERE key = ?", (K_KILLED,))
            self.sm.conn.commit()
            self.sm.meta_set(K_PEAK, str(bal))
            return
        peak = max(float(self.sm.meta_get(K_PEAK) or acc["initial_balance"]), bal)
        self.sm.meta_set(K_PEAK, str(peak))
        dd = (1 - bal / peak) * 100 if peak > 0 else 0.0
        if dd >= KILL_DD_PCT and not acc["manual_halt"]:
            reason = f"el saldo cayó {dd:.1f}% desde su máximo ({peak:,.2f} → {bal:,.2f} USD)"
            res = self.sm.set_manual_halt(True, reason, source="listing_bot")
            self.sm.meta_set(K_KILLED, json.dumps({"at": _iso(int(self.clock() * 1000)), "reason": reason}))
            self.notifier.kill_switch(res | {"open_positions": len(self.sm.positions())}, self.sm.account(),
                                      reason, "listing_bot (-20%)")

    # ── Ciclo de vela ───────────────────────────────────────────────────────
    def candle_cycle(self, now_s: float | None = None) -> dict:
        now_s = self.clock() if now_s is None else now_s
        now_ms = int(now_s * 1000)
        bar_open = self.last_closed_bar_open_ms(now_s)
        last = self.sm.meta_get("last_bar_open_ms")
        if last is not None and int(last) >= bar_open:
            return {"skipped": True, "bar_open": _iso(bar_open)}
        missed = 1 if last is None else max(1, min((bar_open - int(last)) // H_MS, 10_000))
        for k in range(missed):
            self.sm.on_new_bar(_iso(bar_open - (missed - 1 - k) * H_MS))
        self._flush_status_alerts()

        summary: dict = {"skipped": False, "bar_open": _iso(bar_open), "bars_ticked": missed, "symbols": {}}
        for pos in self.sm.positions():
            try:
                summary["symbols"][pos["symbol"]] = self.manage(pos, bar_open, now_ms)
            except Exception as exc:
                log.exception("Error revisando la posición de %s", pos["symbol"])
                summary["symbols"][pos["symbol"]] = {"decision": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
        self.kill_check()

        done = set(self._meta(K_DONE, []))
        try:
            known = self.discover()
        except Exception as exc:
            log.warning("No se pudo actualizar la lista de monedas: %s", exc)
            summary["discovery_error"] = f"{type(exc).__name__}: {exc}"
            known = {k: int(v) for k, v in self._meta(K_KNOWN, {}).items()}
        at = _iso(now_ms)
        for sym, first in self.watchlist(known, bar_open, done).items():
            try:
                d = self.check_entry(sym, first, bar_open, now_ms)
                if d["decision"] == "LONG":
                    d = self.open_trade(sym, bar_open, at, d["day1_high"])
            except Exception as exc:
                log.warning("Error con %s: %s", sym, exc)
                d = {"decision": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
            if d.pop("done", False):
                done.add(sym)
            d.pop("day1_high", None)
            d.pop("close", None)
            d["listed"] = _iso(first)
            summary["symbols"][sym] = d
        self._set_meta(K_DONE, sorted(done))
        self._flush_status_alerts()
        holding = {p["symbol"] for p in self.sm.positions()}
        summary["watching"] = sorted(s for s, d in summary["symbols"].items()
                                     if s in holding or d.get("decision") in ("WAIT / NO_TRADE", "LONG", "STALE_DATA"))
        summary["status"] = self.sm.account()["status"]
        summary["processed_at"] = _iso(int(self.clock() * 1000))
        self.sm.meta_set("last_bar_open_ms", str(bar_open))
        self.sm.meta_set("last_cycle", json.dumps(summary, ensure_ascii=False, default=str))
        return summary

    # ── Vigilancia rápida: solo el "Apagar y cerrar" del panel ──────────────
    def watch_cycle(self, now_s: float | None = None) -> list[dict]:
        at = _iso(int((self.clock() if now_s is None else now_s) * 1000))
        events = []
        if self.sm.close_all_request() is not None:
            pending = 0
            for pos in self.sm.positions():
                try:
                    price = float(self.price_source(pos["symbol"]))
                except Exception as exc:
                    log.warning("Cierre manual: sin precio para %s (%s); se reintenta", pos["symbol"], exc)
                    pending += 1
                    continue
                events.append(self._closed(self.sm.close_position(pos["id"], price * (1 - SLIP), "MANUAL_CLOSE", at)))
            if not pending:
                self.sm.cancel_close_all()
        self._flush_status_alerts()
        return events

    def run_forever(self, max_loops: int | None = None) -> None:
        log.info("Bot de nuevos listados iniciado (1h, base %s)", self.db_path)
        loops = 0
        while not self._stop and (max_loops is None or loops < max_loops):
            now = self.clock()
            try:
                self.watch_cycle(now)
                self.heartbeat(now)
                since_close = (now * 1000 - (self.last_closed_bar_open_ms(now) + H_MS)) / 1000
                if since_close >= self.close_delay:
                    s = self.candle_cycle(now)
                    if not s["skipped"]:
                        log.info("Vela %s: vigila %s; %s", s["bar_open"], ",".join(s["watching"]) or "nada",
                                 json.dumps({k: v.get("decision") for k, v in s["symbols"].items()}, ensure_ascii=False))
            except Exception:
                log.exception("Error en el ciclo; se reintenta en el siguiente")
            loops += 1
            if not self._stop and (max_loops is None or loops < max_loops):
                self.sleep(self.watch_seconds)
        log.info("Bot detenido")

    def close(self) -> None:
        self.sm.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bot de nuevos listados de Binance en paper trading.")
    ap.add_argument("--db", default="data/listados_state.db")
    ap.add_argument("--balance", type=float, default=500.0, help="Balance inicial (solo si la base es nueva)")
    ap.add_argument("--fee", type=float, default=0.08)
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--log-file")
    ap.add_argument("--once", action="store_true", help="Un ciclo de vela y termina")
    args = ap.parse_args(argv)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    load_env_file(args.env_file)
    bot = ListingBot(args.db, args.balance, args.fee)
    if not bot.notifier.enabled:
        log.warning("Sin WEBHOOK_URL ni TELEGRAM_*: las alertas solo irán al log")
    signal.signal(signal.SIGINT, bot.stop)
    signal.signal(signal.SIGTERM, bot.stop)
    try:
        if args.once:
            print(json.dumps(bot.candle_cycle(), ensure_ascii=False, indent=2, default=str))
        else:
            bot.run_forever()
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
