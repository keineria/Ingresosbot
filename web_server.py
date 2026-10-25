#!/usr/bin/env python3
"""
web_server.py — Panel y API de control del bot (solo librería estándar: http.server).

Escucha en 127.0.0.1:8050 por defecto (WEB_HOST / WEB_PORT en .env). Pensado para quedar detrás del
proxy inverso de OpenLiteSpeed con SSL (ver deploy/CYBERPANEL_SETUP.md), sin abrir puertos en firewalld.

Autenticación: DASHBOARD_ACCESS_KEY en .env, enviada en la cabecera `X-Access-Key`
(o `?key=` solo en peticiones GET). Sin clave configurada el servidor no arranca.

Endpoints:
  GET  /                 dashboard (static/index.html; la página pide la clave)
  GET  /healthz          {"ok": true} sin clave, para monitoreo
  GET  /api/state        cuenta, exposición, runner_meta, posiciones (con precio actual), últimos 20 trades,
                         PnL de hoy/semana/mes (UTC), curva de balance y precio actual de cada par
  POST /api/scan-now     diagnóstico de las 3 capas en cada par configurado (no abre órdenes)
  POST /api/kill-switch  {"action": "halt" | "resume" | "toggle", "reason": "..."}

Uso:
  python web_server.py                      # lee .env
  python web_server.py --port 8050 --env-file .env
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import pandas as pd

import quant_core_engine as q
from bot_runner import RunnerConfig, settings_from_env
from market_fetcher import fetch_candles, fetch_price
from notifier import Notifier, load_env_file
from state_manager import StateManager

log = logging.getLogger("web_server")

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 16 * 1024
PRICE_TTL_S = 10.0
MIN_KEY_LEN = 16


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class DashboardApp:
    """Lógica del panel, independiente de HTTP (así se prueba sin sockets)."""

    def __init__(self, cfg: RunnerConfig, params: q.Params, access_key: str,
                 notifier: Notifier | None = None,
                 candle_source: Callable[[str, str, int], pd.DataFrame] | None = None,
                 price_source: Callable[[str], float] | None = None,
                 clock: Callable[[], float] = time.time):
        if not access_key:
            raise ValueError("DASHBOARD_ACCESS_KEY vacío: define una clave en .env antes de arrancar el panel.")
        self.cfg, self.p, self.access_key = cfg, params, access_key
        self.notifier = notifier or Notifier.from_env()
        self.candle_source = candle_source or (lambda s, i, n: fetch_candles(s, i, n)[0])
        self.price_source = price_source or (lambda s: fetch_price(s)[0])
        self.clock = clock
        self._price_cache: dict[str, tuple[float, float]] = {}
        self._scan_lock = threading.Lock()
        self._db_lock = threading.Lock()      # una escritura a la vez desde este proceso
        # Crea la base (o la migra) con el balance configurado si aún no existe.
        StateManager(cfg.db_path, cfg.initial_balance, params).close()

    def _sm(self) -> StateManager:
        return StateManager(self.cfg.db_path, self.cfg.initial_balance, self.p, self.cfg.fee_pct)

    def check_key(self, provided: str | None) -> bool:
        return bool(provided) and hmac.compare_digest(provided.encode(), self.access_key.encode())

    def _price(self, symbol: str) -> float | None:
        now = self.clock()
        hit = self._price_cache.get(symbol)
        if hit and now - hit[0] < PRICE_TTL_S:
            return hit[1]
        try:
            price = float(self.price_source(symbol))
        except Exception as exc:
            log.warning("Sin precio para %s: %s", symbol, exc)
            return None
        self._price_cache[symbol] = (now, price)
        return price

    # ── GET /api/state ──────────────────────────────────────────────────────
    def state(self) -> dict:
        with self._sm() as sm:
            acc = sm.account()
            positions = sm.positions()
            cap = acc["balance"] * self.p.max_allocation_pct / 100.0 * self.p.max_leverage
            used = sm.open_notional()
            last_bar = sm.meta_get("last_bar_open_ms")
            last_cycle = sm.meta_get("last_cycle")
            last_scan = sm.meta_get("last_scan")
            trades = sm.trades(last=20)
            metrics = sm.metrics()
            control = sm.control_log(10)
            now = pd.Timestamp(self.clock(), unit="s", tz="UTC")
            day = now.normalize()
            starts = {"today": day, "week": day - pd.Timedelta(days=day.dayofweek), "month": day.replace(day=1)}
            periods = {k: sm.pnl_since(v.isoformat()) | {"since": v.isoformat()} for k, v in starts.items()}
            curve = sm.balance_curve()

        prices = {}
        for pos in positions:
            d = 1 if pos["side"] == "LONG" else -1
            price = prices.setdefault(pos["symbol"], self._price(pos["symbol"]))
            pos["current_price"] = price
            pos["unrealized_pnl_usd"] = (round(pos["position_units"] * (price - pos["entry_price"]) * d, 2)
                                         if price is not None else None)
            pos["notional_usd"] = round(pos["position_units"] * pos["entry_price"], 2)
        bar_ms = int(last_bar) if last_bar else None
        market = {s: {"price": prices[s] if s in prices else self._price(s)} for s in self.cfg.symbols}
        return {
            "server_time": pd.Timestamp(self.clock(), unit="s", tz="UTC").isoformat(),
            "config": {"symbols": self.cfg.symbols, "interval": self.cfg.interval,
                       "max_leverage": self.p.max_leverage, "max_allocation_pct": self.p.max_allocation_pct,
                       "allow_short": self.p.allow_short},
            "account": {k: acc[k] for k in ("status", "balance", "initial_balance", "daily_drawdown_pct",
                                            "consecutive_losses", "cooldown_bars_remaining", "day_peak_equity",
                                            "updated_at")} | {"manual_halt": bool(acc["manual_halt"])},
            "exposure": {"open_notional_usd": round(used, 2), "cap_usd": round(cap, 2),
                         "used_pct_of_cap": round(used / cap * 100, 2) if cap > 0 else None,
                         "formula": f"balance × {self.p.max_allocation_pct:g}% × {self.p.max_leverage:g}"},
            "runner": {"last_bar_open_ms": bar_ms,
                       "last_bar_open": pd.Timestamp(bar_ms, unit="ms", tz="UTC").isoformat() if bar_ms else None,
                       "last_cycle": json.loads(last_cycle) if last_cycle else None},
            "metrics": metrics,
            "periods": periods,
            "balance_curve": curve,
            "market": market,
            "open_positions": positions,
            "recent_trades": trades,
            "last_scan": json.loads(last_scan) if last_scan else None,
            "control_log": control,
        }

    # ── POST /api/scan-now ──────────────────────────────────────────────────
    def scan_now(self) -> dict:
        if not self._scan_lock.acquire(blocking=False):
            raise ApiError(HTTPStatus.CONFLICT, "Ya hay un escaneo en curso")
        try:
            with self._sm() as sm:
                account = sm.engine_account()
                open_syms = {p["symbol"] for p in sm.positions()}
            results = {}
            for symbol in self.cfg.symbols:
                try:
                    df = self.candle_source(symbol, self.cfg.interval, 500)
                    r = q.run_engine(account, candles=df, symbol=symbol, timeframe=self.cfg.interval,
                                     params=self.p)
                    mr, rk = r["layer_2_market_regime"], r["layer_3_atr_risk_guardian"]
                    results[symbol] = {
                        "decision": r["decision"],
                        "candle_time": r["candle_time"],
                        "closes": [round(float(c), 8) for c in q.normalize_ohlcv(df)["close"].tail(48)],
                        "position_open": symbol in open_syms,
                        "would_open": r["decision"] in ("LONG", "SHORT") and symbol not in open_syms,
                        "layer_1": {k: r["layer_1_circuit_breaker"][k] for k in ("status", "risk_pct", "reasons")},
                        "layer_2": ({k: mr.get(k) for k in ("regime", "signal", "indicators", "long_checks",
                                                          "short_checks", "blocking_filters")}
                                    if mr.get("evaluated") else {"evaluated": False, "reason": mr.get("reason")}),
                        "layer_3": ({k: rk.get(k) for k in ("side", "entry_price", "stop_loss", "take_profit",
                                                          "break_even_trigger", "break_even_new_sl",
                                                          "position_size_usd", "quantity", "risk_usd_effective",
                                                          "sl_distance_pct", "reward_risk_ratio")}
                                    if rk.get("evaluated") else {"evaluated": False, "reason": rk.get("reason")}),
                    }
                except Exception as exc:
                    log.warning("scan-now %s: %s", symbol, exc)
                    results[symbol] = {"decision": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
            out = {"scanned_at": pd.Timestamp(self.clock(), unit="s", tz="UTC").isoformat(),
                   "diagnostic_only": True, "symbols": results}
            with self._db_lock, self._sm() as sm:
                sm.meta_set("last_scan", json.dumps(out, ensure_ascii=False, default=str))
            return out
        finally:
            self._scan_lock.release()

    # ── POST /api/kill-switch ───────────────────────────────────────────────
    def kill_switch(self, body: dict, source: str = "api") -> dict:
        action = str(body.get("action", "toggle")).lower()
        if action not in ("halt", "resume", "toggle"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "action debe ser halt, resume o toggle")
        reason = str(body.get("reason", ""))[:200]
        with self._db_lock, self._sm() as sm:
            current = bool(sm.account()["manual_halt"])
            on = {"halt": True, "resume": False, "toggle": not current}[action]
            result = sm.set_manual_halt(on, reason, source)
            acc = sm.account()
        log.warning("Kill switch %s por %s: %s -> %s (%s)", "ON" if on else "OFF", source,
                    result["status_before"], result["status_after"], reason)
        result["notified"] = self.notifier.kill_switch(result, acc, reason, source)
        result["account"] = {"status": acc["status"], "manual_halt": bool(acc["manual_halt"]),
                             "cooldown_bars_remaining": acc["cooldown_bars_remaining"]}
        return result


def make_handler(app: DashboardApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Ingresosbot"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        # ── utilidades ──
        def _headers(self, status: int, ctype: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")

        def _json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(data))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self, allow_query: bool) -> bool:
            key = self.headers.get("X-Access-Key")
            if not key and allow_query:
                key = (parse_qs(urlsplit(self.path).query).get("key") or [None])[0]
            if app.check_key(key):
                return True
            time.sleep(0.3)                               # frena intentos de adivinar la clave
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "clave de acceso inválida o ausente"})
            return False

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "cuerpo demasiado grande")
            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return {}
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                raise ApiError(HTTPStatus.BAD_REQUEST, "JSON inválido") from None
            if not isinstance(data, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "se esperaba un objeto JSON")
            return data

        def _source(self) -> str:
            return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()

        # ── rutas ──
        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path in ("/", "/index.html"):
                    data = (STATIC_DIR / "index.html").read_bytes()
                    self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(data))
                    self.send_header("Content-Security-Policy",
                                     "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                     "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                                     "frame-ancestors 'none'")
                    self.end_headers()
                    self.wfile.write(data)
                elif path == "/healthz":
                    self._json(HTTPStatus.OK, {"ok": True})
                elif path == "/api/state":
                    if self._authorized(allow_query=True):
                        self._json(HTTPStatus.OK, app.state())
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "no encontrado"})
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})
            except Exception:
                log.exception("Error en GET %s", path)
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "error interno"})

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                if path not in ("/api/scan-now", "/api/kill-switch"):
                    self._json(HTTPStatus.NOT_FOUND, {"error": "no encontrado"})
                    return
                if not self._authorized(allow_query=False):   # en POST solo cabecera (evita CSRF por URL)
                    return
                body = self._body()
                if path == "/api/scan-now":
                    self._json(HTTPStatus.OK, app.scan_now())
                else:
                    self._json(HTTPStatus.OK, app.kill_switch(body, source=f"web:{self._source()}"))
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})
            except Exception:
                log.exception("Error en POST %s", path)
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "error interno"})

    return Handler


def build_server(app: DashboardApp, host: str = "127.0.0.1", port: int = 8050) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Panel web y API de control del bot.")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--host", help="Por defecto WEB_HOST o 127.0.0.1")
    ap.add_argument("--port", type=int, help="Por defecto WEB_PORT o 8050")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env_file(args.env_file)

    key = os.environ.get("DASHBOARD_ACCESS_KEY", "")
    if not key:
        log.error("Falta DASHBOARD_ACCESS_KEY en %s; el panel no arranca sin clave.", args.env_file)
        return 2
    if len(key) < MIN_KEY_LEN:
        log.warning("DASHBOARD_ACCESS_KEY tiene menos de %d caracteres: usa una más larga.", MIN_KEY_LEN)
    host = args.host or os.environ.get("WEB_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("WEB_PORT", "8050"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("Escuchando en %s: el panel queda expuesto fuera del proxy inverso.", host)

    cfg, params = settings_from_env()
    server = build_server(DashboardApp(cfg, params, key), host, port)
    log.info("Panel en http://%s:%d (base %s, pares %s)", host, port, cfg.db_path, ",".join(cfg.symbols))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
