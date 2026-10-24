#!/usr/bin/env python3
"""
bot_runner.py — Motor 24/7 en modo Paper Trading (librería estándar + módulos del repo).

Dos ciclos:
  a) Cierre de vela (cada 1h o 4h, unos segundos después del cierre):
     - descuenta el enfriamiento (cooldown_bars_remaining) una vez por vela si la cuenta está en HALTED,
     - descarga las velas de cada par (BTCUSDT, ETHUSDT, SOLUSDT...),
     - revisa con la vela cerrada las posiciones abiertas (red de seguridad por si un toque se escapó entre sondeos),
     - ejecuta quant_core_engine y, si hay LONG/SHORT y el par no tiene posición abierta, registra la entrada
       en state_manager (SQLite).
  b) Vigilancia rápida (cada 15 s): consulta el precio de los pares con posiciones abiertas y aplica al instante
     Stop-Loss, Take-Profit y el movimiento a Break-Even.

Cada evento se envía con notifier.py (webhook n8n y/o Telegram).

Uso en el VPS:
  python bot_runner.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --interval 1h --balance 10000 --env-file .env
  python bot_runner.py --once           # un ciclo de vela + uno de vigilancia y termina (pruebas / cron)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

import quant_core_engine as q
from market_fetcher import INTERVAL_MS, FetchError, fetch_candles, fetch_price
from notifier import Notifier, load_env_file
from state_manager import StateManager

log = logging.getLogger("bot_runner")

@dataclass
class RunnerConfig:
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    interval: str = "1h"
    db_path: str = "data/trading_state.db"
    initial_balance: float = 10000.0
    fee_pct: float = 0.08
    candles_limit: int = 500          # >= 400 para que la EMA200 converja
    watch_seconds: float = 15.0
    close_delay_seconds: float = 5.0  # margen tras el cierre para que el exchange publique la vela
    max_price_gap_pct: float = 5.0    # si el precio en vivo se aleja más que esto del cierre, se usa el cierre


def _iso(ms: int) -> str:
    return pd.Timestamp(ms, unit="ms", tz="UTC").isoformat()


class BotRunner:
    def __init__(self, cfg: RunnerConfig, params: q.Params | None = None, notifier: Notifier | None = None,
                 candle_source: Callable[[str, str, int], pd.DataFrame] | None = None,
                 price_source: Callable[[str], float] | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        if cfg.interval not in INTERVAL_MS:
            raise ValueError(f"Temporalidad no soportada: {cfg.interval}")
        self.cfg = cfg
        self.p = params or q.Params()
        self.notifier = notifier or Notifier.from_env()
        self.candle_source = candle_source or self._default_candles
        self.price_source = price_source or (lambda s: fetch_price(s)[0])
        self.clock, self.sleep = clock, sleep
        self.step = INTERVAL_MS[cfg.interval]
        self.max_price_gap = cfg.max_price_gap_pct / 100.0
        self.sm = StateManager(cfg.db_path, cfg.initial_balance, self.p, cfg.fee_pct)
        self._seen_transitions = 0
        self._stop = False

    # ── Utilidades ──────────────────────────────────────────────────────────
    def _default_candles(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        return fetch_candles(symbol, interval, limit)[0]

    def last_closed_bar_open_ms(self, now_s: float | None = None) -> int:
        now_ms = int((self.clock() if now_s is None else now_s) * 1000)
        return (now_ms // self.step) * self.step - self.step

    def stop(self, *_):
        self._stop = True

    def _flush_status_alerts(self) -> list[dict]:
        new = self.sm.status_transitions[self._seen_transitions:]
        self._seen_transitions = len(self.sm.status_transitions)
        acc = self.sm.account()
        for t in new:
            log.info("Circuit Breaker %s -> %s (%s)", t["from"], t["to"], "; ".join(t["reasons"]))
            self.notifier.status_changed(t, acc)
        return new

    def _handle_events(self, events: list[dict]) -> None:
        for ev in events:
            if ev["event"] == "BREAK_EVEN":
                log.info("Break-Even posición %s -> SL %s", ev["position_id"], ev["new_stop_loss"])
                self.notifier.break_even(ev["position"], ev.get("price"))
            elif ev["event"] == "CLOSE":
                log.info("Cerrada %s %s %s PnL %.2f", ev["symbol"], ev["side"], ev["result"], ev["pnl_usd"])
                self.notifier.position_closed(ev, self.sm.account())

    # ── Ciclo a) cierre de vela ─────────────────────────────────────────────
    def candle_cycle(self, now_s: float | None = None) -> dict:
        bar_open = self.last_closed_bar_open_ms(now_s)
        bar_close_iso = _iso(bar_open + self.step)
        last = self.sm.meta_get("last_bar_open_ms")
        if last is not None and int(last) >= bar_open:
            return {"skipped": True, "bar_open": _iso(bar_open)}

        # Una vela cerrada = un tick del enfriamiento (si se perdieron velas, se cuentan todas).
        missed = 1 if last is None else max(1, min((bar_open - int(last)) // self.step, 10_000))
        for k in range(missed):
            self.sm.on_new_bar(_iso(bar_open - (missed - 1 - k) * self.step))
        self._flush_status_alerts()

        summary = {"skipped": False, "bar_open": _iso(bar_open), "bars_ticked": missed,
                   "status": self.sm.account()["status"], "symbols": {}}
        for symbol in self.cfg.symbols:
            try:
                summary["symbols"][symbol] = self._process_symbol(symbol, bar_open, bar_close_iso)
            except FetchError as exc:
                log.error("Sin velas para %s: %s", symbol, exc)
                summary["symbols"][symbol] = {"error": f"FetchError: {exc}"}
            except Exception as exc:          # un par con problemas no detiene a los demás
                log.exception("Error procesando %s", symbol)
                summary["symbols"][symbol] = {"error": f"{type(exc).__name__}: {exc}"}
        self._flush_status_alerts()
        self.sm.meta_set("last_bar_open_ms", str(bar_open))
        summary["status"] = self.sm.account()["status"]
        summary["processed_at"] = pd.Timestamp(self.clock(), unit="s", tz="UTC").isoformat()
        self.sm.meta_set("last_cycle", json.dumps(summary, ensure_ascii=False, default=str))
        return summary

    def _process_symbol(self, symbol: str, bar_open: int, bar_close_iso: str) -> dict:
        df = self.candle_source(symbol, self.cfg.interval, self.cfg.candles_limit)
        df = df[df["time"] <= pd.Timestamp(bar_open, unit="ms", tz="UTC")].reset_index(drop=True)
        if df.empty:
            return {"decision": "ERROR", "error": "sin velas"}
        last = df.iloc[-1]
        expected = pd.Timestamp(bar_open, unit="ms", tz="UTC")
        if pd.Timestamp(last["time"]) != expected:
            # Nunca operar con datos viejos (p. ej. si sin red se cayó al CSV local desactualizado).
            log.warning("%s: la última vela es %s y se esperaba %s; no se opera", symbol, last["time"], expected)
            return {"decision": "STALE_DATA", "last_candle": str(last["time"]), "expected": expected.isoformat()}

        # Red de seguridad: la vela cerrada pudo tocar SL/TP/BE entre dos sondeos de 15 s.
        for pos in self.sm.positions(symbol):
            if pd.Timestamp(last["time"]) >= pd.Timestamp(pos["opened_at"]):
                self._handle_events(self.sm.process_candle(symbol, last["time"], float(last["open"]),
                                                           float(last["high"]), float(last["low"]),
                                                           float(last["close"])))
        self._flush_status_alerts()

        if self.sm.positions(symbol):
            return {"decision": "HOLD", "reason": "ya hay posición abierta en el par"}

        try:
            entry = float(self.price_source(symbol))
        except Exception as exc:
            log.warning("Sin precio en vivo para %s (%s); se usa el cierre", symbol, exc)
            entry = None
        close = float(last["close"])
        if entry is not None and abs(entry / close - 1) > self.max_price_gap:
            log.warning("%s: precio en vivo %.4f se aleja %.1f%% del cierre %.4f; se usa el cierre",
                        symbol, entry, abs(entry / close - 1) * 100, close)
            entry = None
        result = q.run_engine(self.sm.engine_account(), candles=df, symbol=symbol,
                              timeframe=self.cfg.interval, entry_price=entry, params=self.p)
        out = {"decision": result["decision"], "regime": result["layer_2_market_regime"].get("regime")}
        if result["decision"] not in ("LONG", "SHORT"):
            out["blocking"] = result["layer_2_market_regime"].get("blocking_filters")
            return out

        acc = self.sm.account()
        capacity = acc["balance"] * self.p.max_allocation_pct / 100.0 * self.p.max_leverage
        needed = result["order"]["notional_usd"]
        if self.sm.open_notional() + needed > capacity + 0.01:
            out.update(decision="SKIPPED", reason=f"capital comprometido: {self.sm.open_notional():.2f} + "
                                                  f"{needed:.2f} > {capacity:.2f} USD")
            log.info("%s: señal %s omitida (%s)", symbol, result["decision"], out["reason"])
            return out

        pid = self.sm.open_from_engine(result, opened_at=bar_close_iso)
        pos = next(p for p in self.sm.positions(symbol) if p["id"] == pid)
        log.info("Abierta %s %s @ %.4f (SL %.4f TP %.4f)", symbol, pos["side"], pos["entry_price"],
                 pos["stop_loss"], pos["take_profit"])
        self.notifier.position_opened(pos, self.sm.account())
        out["position_id"] = pid
        return out

    # ── Ciclo b) vigilancia rápida ──────────────────────────────────────────
    def watch_cycle(self, now_s: float | None = None) -> list[dict]:
        at = pd.Timestamp((self.clock() if now_s is None else now_s), unit="s", tz="UTC").isoformat()
        events: list[dict] = []
        for symbol in sorted({p["symbol"] for p in self.sm.positions()}):
            try:
                price = float(self.price_source(symbol))
            except Exception as exc:
                log.warning("Vigilancia: sin precio para %s (%s)", symbol, exc)
                continue
            ev = self.sm.update_price(symbol, price, at)
            self._handle_events(ev)
            events.extend(ev)
        self._flush_status_alerts()
        return events

    # ── Bucle principal ─────────────────────────────────────────────────────
    def run_forever(self, max_loops: int | None = None) -> None:
        log.info("Bot iniciado: %s en %s, vigilancia cada %ss, base %s",
                 ",".join(self.cfg.symbols), self.cfg.interval, self.cfg.watch_seconds, self.cfg.db_path)
        loops = 0
        while not self._stop and (max_loops is None or loops < max_loops):
            now = self.clock()
            try:
                self.watch_cycle(now)
                since_close = (now * 1000 - (self.last_closed_bar_open_ms(now) + self.step)) / 1000
                if since_close >= self.cfg.close_delay_seconds:
                    s = self.candle_cycle(now)
                    if not s["skipped"]:
                        log.info("Vela %s: %s", s["bar_open"], json.dumps(s["symbols"], ensure_ascii=False))
            except Exception:
                log.exception("Error en el ciclo; se reintenta en el siguiente")
            loops += 1
            if not self._stop and (max_loops is None or loops < max_loops):
                self.sleep(self.cfg.watch_seconds)
        log.info("Bot detenido")

    def close(self) -> None:
        self.sm.close()


def parse_params(raw: str | None, no_short: bool = False) -> q.Params:
    """Params desde un JSON (cadena o ruta de archivo), igual que --params en el resto de scripts."""
    p = q.Params()
    if raw:
        src = Path(raw)
        for k, v in json.loads(src.read_text(encoding="utf-8") if src.exists() else raw).items():
            if not hasattr(p, k):
                raise SystemExit(f"Parámetro desconocido: {k}")
            setattr(p, k, type(getattr(p, k))(v))
    if no_short:
        p.allow_short = False
    return p


def settings_from_env(symbols: str | None = None, interval: str | None = None, db: str | None = None,
                      balance: float | None = None, params: str | None = None,
                      no_short: bool = False) -> tuple[RunnerConfig, q.Params]:
    """Una sola fuente de configuración para bot_runner.py y web_server.py:
    argumento > variable del .env (BOT_SYMBOLS, BOT_INTERVAL, BOT_DB, BOT_BALANCE, BOT_PARAMS, BOT_NO_SHORT) > defecto."""
    env = os.environ.get
    syms = symbols or env("BOT_SYMBOLS") or "BTCUSDT,ETHUSDT,SOLUSDT"
    cfg = RunnerConfig(
        [s.strip().upper() for s in syms.split(",") if s.strip()],
        interval or env("BOT_INTERVAL") or "1h",
        db or env("BOT_DB") or "data/trading_state.db",
        float(balance if balance is not None else env("BOT_BALANCE") or 10000.0),
    )
    no_short = no_short or env("BOT_NO_SHORT", "").strip().lower() in ("1", "true", "yes", "si", "sí")
    return cfg, parse_params(params or env("BOT_PARAMS"), no_short)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bot 24/7 en paper trading. Sin argumentos usa el .env.")
    ap.add_argument("--symbols", help="Por defecto BOT_SYMBOLS o BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--interval", choices=["1h", "4h"], help="Por defecto BOT_INTERVAL o 1h")
    ap.add_argument("--db", help="Por defecto BOT_DB o data/trading_state.db")
    ap.add_argument("--balance", type=float, help="Balance inicial (solo si la base es nueva); BOT_BALANCE")
    ap.add_argument("--fee", type=float, default=0.08)
    ap.add_argument("--watch-seconds", type=float, default=15.0)
    ap.add_argument("--params", help="JSON con parámetros a sobrescribir (ver Params); BOT_PARAMS")
    ap.add_argument("--no-short", action="store_true")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--log-file")
    ap.add_argument("--once", action="store_true", help="Un ciclo de vela + uno de vigilancia y termina")
    args = ap.parse_args(argv)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    load_env_file(args.env_file)

    cfg, p = settings_from_env(args.symbols, args.interval, args.db, args.balance, args.params, args.no_short)
    if cfg.interval not in ("1h", "4h"):
        raise SystemExit(f"BOT_INTERVAL debe ser 1h o 4h (recibido {cfg.interval})")
    cfg.fee_pct, cfg.watch_seconds = args.fee, args.watch_seconds
    bot = BotRunner(cfg, p)
    if not bot.notifier.enabled:
        log.warning("Sin WEBHOOK_URL ni TELEGRAM_*: las alertas solo irán al log")
    signal.signal(signal.SIGINT, bot.stop)
    signal.signal(signal.SIGTERM, bot.stop)
    try:
        if args.once:
            print(json.dumps(bot.candle_cycle(), ensure_ascii=False, indent=2, default=str))
            bot.watch_cycle()
        else:
            bot.run_forever()
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
