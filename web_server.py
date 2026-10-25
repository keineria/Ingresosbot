#!/usr/bin/env python3
"""
web_server.py — Panel y API de control del bot (solo librería estándar: http.server).

Escucha en 127.0.0.1:8050 por defecto (WEB_HOST / WEB_PORT en .env). Pensado para quedar detrás del
proxy inverso de OpenLiteSpeed con SSL (ver deploy/CYBERPANEL_SETUP.md), sin abrir puertos en firewalld.

Autenticación: DASHBOARD_ACCESS_KEY en .env, enviada en la cabecera `X-Access-Key`
(o `?key=` solo en peticiones GET). Sin clave configurada el servidor no arranca.

Varios bots: si existe deploy/bots.json (o WEB_BOTS_FILE), el panel muestra cada bot de esa lista con su nombre
y su estrategia, cada uno con su propia base SQLite. Las rutas /api/* aceptan ?bot=<id> (por defecto el primero).
El archivo se vuelve a leer cuando cambia: un bot nuevo aparece en el panel sin reiniciar ingresosbot-web.

Endpoints:
  GET  /                 dashboard (static/index.html; la página pide la clave)
  GET  /healthz          {"ok": true} sin clave, para monitoreo
  GET  /api/bots         lista de bots: nombre, estrategia explicada y resumen (balance, retorno, estado)
  GET  /api/overview     todos los bots en vivo a la vez: cuenta y Capa 1, posiciones con precio actual,
                         últimas operaciones, curva, decisión de la última vela y si el proceso está vivo
  GET  /api/state        cuenta, exposición, runner_meta, posiciones (con precio actual), últimos 20 trades,
                         PnL de hoy/semana/mes (UTC), curva de balance y precio actual de cada par
  GET  /api/binance      tu cuenta real de Binance, SOLO LECTURA (binance_account.py): saldo por moneda y billetera,
                         total en USD y permisos de la llave. Sin BINANCE_API_KEY en .env responde configured=false
  POST /api/scan-now     diagnóstico de las 3 capas en cada par configurado (no abre órdenes)
  POST /api/kill-switch  {"action": "halt" | "resume" | "toggle", "reason": "..."}
  GET  /api/ai           agentes de IA: gasto del mes, pausa activa, sugerencias pendientes, últimas decisiones
  POST /api/ai/decide    {"kind": "pause" | "hypothesis" | "permission", "id": N, "action": "on" | "no" | "off" | "ok"}

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
import re
import shutil
import subprocess
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
from binance_account import AccountView
from bot_runner import BOTS_FILE, RunnerConfig, bot_settings, load_bots, settings_from_env
from market_fetcher import fetch_candles, fetch_price
from notifier import Notifier, load_env_file
from state_manager import StateManager

log = logging.getLogger("web_server")

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 16 * 1024
PRICE_TTL_S = 10.0
MIN_KEY_LEN = 16
BOT_TEXT_KEYS = ("trader", "summary", "how", "backtest", "service", "color")
INTERVAL_S = {"1h": 3_600, "4h": 14_400, "1d": 86_400}
HEARTBEAT_STALE_S = 300.0     # bot_runner deja un latido cada 60 s; sin latido en 5 min, algo pasa
SERVICE_TTL_S = 30.0
_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,80}$")
_service_cache: dict[str, tuple[float, str | None]] = {}
_service_lock = threading.Lock()


def systemd_state(service: str | None, clock: Callable[[], float] = time.time) -> str | None:
    """`systemctl is-active <servicio>` (active, inactive, failed…), en caché 30 s. None si no se puede saber."""
    if not service or not _SERVICE_RE.match(service) or not shutil.which("systemctl"):
        return None
    now = clock()
    with _service_lock:
        hit = _service_cache.get(service)
        if hit and now - hit[0] < SERVICE_TTL_S:
            return hit[1]
    try:
        out = subprocess.run(["systemctl", "is-active", service], capture_output=True, text=True, timeout=3)
        state = out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("systemctl is-active %s: %s", service, exc)
        state = None
    with _service_lock:
        _service_cache[service] = (now, state)
    return state


def _age_s(iso: str | None, now: float) -> float | None:
    if not iso:
        return None
    try:
        return max(0.0, now - pd.Timestamp(iso).timestamp())
    except (ValueError, TypeError):
        return None


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
                 clock: Callable[[], float] = time.time, bot: dict | None = None,
                 service_state: Callable[[str | None], str | None] | None = None):
        if not access_key:
            raise ValueError("DASHBOARD_ACCESS_KEY vacío: define una clave en .env antes de arrancar el panel.")
        self.cfg, self.p, self.access_key = cfg, params, access_key
        bot = bot or {}
        # Las alertas de los bots paralelos llevan su nombre delante, como las de bot_runner.py.
        label = bot.get("name") if bot.get("id", "oficial") != "oficial" else None
        self.notifier = notifier or Notifier.from_env(label=label)
        self.candle_source = candle_source or (lambda s, i, n: fetch_candles(s, i, n)[0])
        self.price_source = price_source or (lambda s: fetch_price(s)[0])
        self.clock = clock
        self.service_state = service_state or (lambda svc: systemd_state(svc, clock))
        self.bot = {"id": bot.get("id", "oficial"), "name": bot.get("name", "Bot Oficial"),
                    **{k: bot[k] for k in BOT_TEXT_KEYS if k in bot}}
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

        prices = self._with_prices(positions)
        bar_ms = int(last_bar) if last_bar else None
        market = {s: {"price": prices[s] if s in prices else self._price(s)} for s in self.cfg.symbols}
        return {
            "server_time": pd.Timestamp(self.clock(), unit="s", tz="UTC").isoformat(),
            "bot": self.bot,
            "config": self._config(),
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

    def _with_prices(self, positions: list[dict]) -> dict[str, float | None]:
        """Añade a cada posición el precio actual y su PnL latente; devuelve los precios usados."""
        prices: dict[str, float | None] = {}
        for pos in positions:
            d = 1 if pos["side"] == "LONG" else -1
            price = prices.setdefault(pos["symbol"], self._price(pos["symbol"]))
            notional = pos["position_units"] * pos["entry_price"]
            pnl = pos["position_units"] * (price - pos["entry_price"]) * d if price is not None else None
            pos["current_price"] = price
            pos["unrealized_pnl_usd"] = round(pnl, 2) if pnl is not None else None
            pos["unrealized_pct"] = round(pnl / notional * 100, 2) if pnl is not None and notional else None
            pos["notional_usd"] = round(notional, 2)
        return prices

    # ── GET /api/overview (un bot, todo lo que cabe en su tarjeta en vivo) ──
    def live(self) -> dict:
        now = self.clock()
        day = pd.Timestamp(now, unit="s", tz="UTC").normalize()
        with self._sm() as sm:
            acc = sm.account()
            positions = sm.positions()
            trades = sm.trades(last=5)
            metrics = sm.metrics()
            curve = sm.balance_curve()
            today = sm.pnl_since(day.isoformat())
            last_cycle = sm.meta_get("last_cycle")
            beat = sm.meta_get("heartbeat")
            started = [t for t in (sm.meta_get("started_at"), sm.first_trade_at()) if t]
        self._with_prices(positions)
        cycle = json.loads(last_cycle) if last_cycle else None
        unreal = sum(p["unrealized_pnl_usd"] or 0.0 for p in positions)
        init, bal = acc["initial_balance"], acc["balance"]
        equity = bal + unreal

        # ¿Sigue vivo el proceso? El latido (cada 60 s) manda; si no hay, se pregunta a systemd.
        beat_age = _age_s(beat, now)
        svc = self.service_state(self.bot.get("service"))
        if beat_age is not None and beat_age <= HEARTBEAT_STALE_S:
            health = "running"
        elif svc == "active":
            health = "running"
        elif svc in ("inactive", "failed", "deactivating"):
            health = "stopped"
        elif beat is None and cycle is None:
            health = "never"
        else:
            health = "stale"
        step = INTERVAL_S.get(self.cfg.interval)
        next_close = (int(now // step) + 1) * step if step else None
        decisions = {}
        for sym, d in ((cycle or {}).get("symbols") or {}).items():
            if isinstance(d, dict):
                decisions[sym] = {k: d[k] for k in ("decision", "regime", "blocking", "reason", "error") if d.get(k)}
        return self.bot | {
            "interval": self.cfg.interval, "symbols": self.cfg.symbols, "strategy": self.p.strategy,
            "account": {k: acc[k] for k in ("status", "balance", "initial_balance", "daily_drawdown_pct",
                                            "consecutive_losses", "cooldown_bars_remaining")}
                       | {"manual_halt": bool(acc["manual_halt"])},
            "limits": {"halt_losses": self.p.halt_losses, "throttle_losses": self.p.throttle_losses,
                       "halt_dd_pct": self.p.halt_dd_pct, "cooldown_candles": self.p.cooldown_candles},
            "equity": round(equity, 2),
            "unrealized_pnl_usd": round(unreal, 2),
            "return_pct": round((equity / init - 1) * 100, 2) if init else None,
            "realized_return_pct": round((bal / init - 1) * 100, 2) if init else None,
            "metrics": {k: metrics.get(k) for k in ("trades", "wins", "losses", "win_rate_pct", "profit_factor",
                                                     "net_pnl_usd", "total_fees_usd")},
            "today": today,
            "open_positions": [{k: p.get(k) for k in (
                "symbol", "side", "entry_price", "stop_loss", "initial_stop_loss", "take_profit", "trail_atr_mult",
                "is_at_breakeven", "opened_at", "current_price", "unrealized_pnl_usd", "unrealized_pct",
                "notional_usd")} for p in positions],
            "recent_trades": [{k: t.get(k) for k in ("symbol", "side", "result", "exit_reason", "pnl_usd", "pnl_pct",
                                                      "r_multiple", "opened_at", "closed_at")} for t in trades],
            "balance_curve": curve,
            "started_at": min(started) if started else None,
            "runner": {"health": health, "service_state": svc, "heartbeat_at": beat,
                       "heartbeat_age_s": round(beat_age) if beat_age is not None else None,
                       "last_cycle_at": cycle.get("processed_at") if cycle else None,
                       "last_bar_open": cycle.get("bar_open") if cycle else None,
                       "next_bar_close": pd.Timestamp(next_close, unit="s", tz="UTC").isoformat() if next_close else None,
                       "decisions": decisions},
        }

    def _config(self) -> dict:
        p = self.p
        cfg = {"symbols": self.cfg.symbols, "interval": self.cfg.interval,
               "max_leverage": p.max_leverage, "max_allocation_pct": p.max_allocation_pct,
               "allow_short": p.allow_short if p.strategy != "trend" else p.trend_allow_short,
               "strategy": p.strategy, "risk_base_pct": p.risk_base_pct}
        if p.strategy == "trend":
            cfg |= {"donchian_len": p.donchian_len, "trend_sl_atr_mult": p.trend_sl_atr_mult,
                    "trail_atr_mult": p.trail_atr_mult, "daily_sma_len": p.daily_sma_len, "trend_entry": p.trend_entry}
        else:
            cfg |= {"sl_atr_mult": p.sl_atr_mult, "tp_atr_mult": p.tp_atr_mult, "be_atr_mult": p.be_atr_mult}
        return cfg

    # ── GET /api/bots (resumen de un bot, sin precios en vivo) ──
    def summary(self) -> dict:
        with self._sm() as sm:
            acc = sm.account()
            metrics = sm.metrics()
            n_open = len(sm.positions())
            last_cycle = sm.meta_get("last_cycle")
        cycle = json.loads(last_cycle) if last_cycle else None
        init = acc["initial_balance"]
        return self.bot | {
            "interval": self.cfg.interval, "symbols": self.cfg.symbols, "strategy": self.p.strategy,
            "status": acc["status"], "manual_halt": bool(acc["manual_halt"]), "balance": acc["balance"],
            "initial_balance": init,
            "return_pct": round((acc["balance"] / init - 1) * 100, 2) if init else None,
            "trades": metrics.get("trades"), "win_rate_pct": metrics.get("win_rate_pct"),
            "open_positions": n_open, "last_cycle_at": cycle.get("processed_at") if cycle else None,
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
                                                          "short_checks", "blocking_filters", "strategy",
                                                          "entry_trigger", "breakout_long_checks")}
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
    # ── Agentes de IA (ai_agents.py) ───────────────────────────────────────
    def _ai_service(self):
        import ai_agents
        from ai_core import AIConfig
        if getattr(self, "_ai_svc", None) is None:
            self._ai_svc = ai_agents.AgentService(AIConfig.from_env(), ai_agents.AgentSettings.from_env(),
                                                  clock=self.clock, run_lab_in_thread=False)
        return self._ai_svc

    def ai_state(self) -> dict:
        from ai_core import AIConfig
        cfg = AIConfig.from_env()
        if not cfg.enabled and not Path(cfg.db_path).exists():
            return {"enabled": False}
        import ai_agents
        svc = self._ai_service()
        return ai_agents.ai_summary(svc.store, cfg, svc.s, svc.now())

    def ai_decide(self, body: dict, source: str = "api") -> dict:
        kind = {"pause": "p", "hypothesis": "h", "permission": "a"}.get(str(body.get("kind", "")))
        action = str(body.get("action", ""))
        try:
            item = int(body.get("id"))
        except (TypeError, ValueError):
            item = None
        allowed = {"p": {"on", "no", "off"}, "h": {"ok", "no"}, "a": {"on", "off"}}
        if kind is None or item is None or action not in allowed[kind]:
            raise ApiError(HTTPStatus.BAD_REQUEST,
                           'usa {"kind": "pause"|"hypothesis"|"permission", "id": N, "action": ...}')
        with self._db_lock:
            msg = self._ai_service().decide(kind, item, action, by=source)
        log.warning("IA: %s #%s %s por %s -> %s", body.get("kind"), item, action, source, msg)
        return {"ok": True, "message": msg}

    def kill_switch(self, body: dict, source: str = "api") -> dict:
        action = str(body.get("action", "toggle")).lower()
        if action not in ("halt", "resume", "toggle"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "action debe ser halt, resume o toggle")
        reason = str(body.get("reason", ""))[:200]
        power = bool(body.get("power"))            # botón "Apagar / Encender" de las tarjetas: mismo interruptor
        with self._db_lock, self._sm() as sm:
            current = bool(sm.account()["manual_halt"])
            on = {"halt": True, "resume": False, "toggle": not current}[action]
            result = sm.set_manual_halt(on, reason or ("Apagado desde el panel" if power and on else ""), source)
            n_open = len(sm.positions())
            if on and body.get("close_positions") and n_open:
                sm.request_close_all(reason or "Apagado desde el panel", source)   # las cierra el propio bot
                result["close_requested"] = n_open
            elif not on:
                sm.cancel_close_all()
            acc = sm.account()
        result["power"], result["open_positions"] = power, n_open
        log.warning("Kill switch %s por %s: %s -> %s (%s)%s", "ON" if on else "OFF", source,
                    result["status_before"], result["status_after"], reason,
                    f"; cierre pedido de {n_open} posiciones" if result.get("close_requested") else "")
        result["notified"] = self.notifier.kill_switch(result, acc, reason, source)
        result["account"] = {"status": acc["status"], "manual_halt": bool(acc["manual_halt"]),
                             "cooldown_bars_remaining": acc["cooldown_bars_remaining"]}
        return result


class BotRegistry:
    """Varios DashboardApp (uno por bot de deploy/bots.json) detrás del mismo servidor y la misma clave."""

    def __init__(self, apps: list[DashboardApp]):
        self._set_apps(apps)
        self._source: tuple[Path, str, dict] | None = None   # (bots.json, clave, kw) si se recarga solo
        self._mtime: float | None = None
        self._reload_lock = threading.Lock()

    def _set_apps(self, apps: list[DashboardApp]) -> None:
        if not apps:
            raise ValueError("No hay bots que mostrar")
        prices: dict[str, tuple[float, float]] = {}
        for a in apps:                         # un solo precio por par aunque varios bots lo operen
            a._price_cache = prices
        self.apps = {a.bot["id"]: a for a in apps}
        self.default = apps[0].bot["id"]

    @staticmethod
    def _build(bots: list[dict], access_key: str, old: dict[str, DashboardApp] | None = None, **kw) -> list[DashboardApp]:
        apps = []
        for b in bots:
            prev = (old or {}).get(b["id"])
            if prev is not None and getattr(prev, "_def", None) == b:
                apps.append(prev)              # sin cambios: conserva su estado (bloqueos, caché)
                continue
            cfg, p = bot_settings(b)
            app = DashboardApp(cfg, p, access_key, bot=b, **kw)
            app._def = json.loads(json.dumps(b))
            apps.append(app)
        return apps

    @classmethod
    def from_bots(cls, bots: list[dict], access_key: str, **kw) -> "BotRegistry":
        return cls(cls._build(bots, access_key, **kw))

    @classmethod
    def from_file(cls, path: str | Path, access_key: str, **kw) -> "BotRegistry":
        """Como from_bots, pero vuelve a leer el archivo cuando cambia (añadir un bot = editar bots.json)."""
        path = Path(path)
        reg = cls.from_bots(load_bots(path), access_key, **kw)
        reg._source = (path, access_key, kw)
        reg._mtime = path.stat().st_mtime
        return reg

    def maybe_reload(self) -> bool:
        if not self._source:
            return False
        path, key, kw = self._source
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        with self._reload_lock:
            if mtime == self._mtime:
                return False
            self._mtime = mtime
            try:
                apps = self._build(load_bots(path), key, old=self.apps, **kw)
                self._set_apps(apps)
            except (Exception, SystemExit) as exc:     # un bots.json mal escrito no tumba el panel
                log.error("No se pudo recargar %s (se mantienen los bots anteriores): %s", path, exc)
                return False
        log.info("Bots recargados desde %s: %s", path, ", ".join(self.apps))
        return True

    def check_key(self, provided: str | None) -> bool:
        return self.apps[self.default].check_key(provided)

    def get(self, bot_id: str | None) -> DashboardApp:
        self.maybe_reload()
        if not bot_id:
            return self.apps[self.default]
        if bot_id not in self.apps:
            raise ApiError(HTTPStatus.NOT_FOUND, f"bot desconocido: {bot_id}")
        return self.apps[bot_id]

    def bots(self) -> dict:
        self.maybe_reload()
        out = []
        for a in self.apps.values():
            try:
                out.append(a.summary())
            except Exception as exc:
                log.warning("Resumen de %s: %s", a.bot["id"], exc)
                out.append(a.bot | {"error": f"{type(exc).__name__}: {exc}"})
        return {"default": self.default, "bots": out}

    def overview(self) -> dict:
        self.maybe_reload()
        out = []
        for a in list(self.apps.values()):
            try:
                out.append(a.live())
            except Exception as exc:
                log.warning("Vista en vivo de %s: %s", a.bot["id"], exc)
                out.append(a.bot | {"interval": a.cfg.interval, "symbols": a.cfg.symbols,
                                    "error": f"{type(exc).__name__}: {exc}"})
        now = next(iter(self.apps.values())).clock()
        return {"server_time": pd.Timestamp(now, unit="s", tz="UTC").isoformat(), "default": self.default,
                "bots": out}


def make_handler(app: DashboardApp | BotRegistry, binance: AccountView | None = None) -> type[BaseHTTPRequestHandler]:
    registry = app if isinstance(app, BotRegistry) else BotRegistry([app])
    binance = binance or AccountView(None)

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
            if registry.check_key(key):
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

        def _bot(self, body: dict | None = None) -> DashboardApp:
            q_bot = (parse_qs(urlsplit(self.path).query).get("bot") or [None])[0]
            return registry.get(q_bot or (body or {}).get("bot"))

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
                        self._json(HTTPStatus.OK, self._bot().state())
                elif path == "/api/ai":                    # la IA es una sola para todos los bots
                    if self._authorized(allow_query=True):
                        self._json(HTTPStatus.OK, registry.get(None).ai_state())
                elif path == "/api/bots":
                    if self._authorized(allow_query=True):
                        self._json(HTTPStatus.OK, registry.bots())
                elif path == "/api/overview":
                    if self._authorized(allow_query=True):
                        self._json(HTTPStatus.OK, registry.overview())
                elif path == "/api/binance":
                    if self._authorized(allow_query=False):
                        self._json(HTTPStatus.OK, binance.snapshot())
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
                if path not in ("/api/scan-now", "/api/kill-switch", "/api/ai/decide"):
                    self._json(HTTPStatus.NOT_FOUND, {"error": "no encontrado"})
                    return
                if not self._authorized(allow_query=False):   # en POST solo cabecera (evita CSRF por URL)
                    return
                body = self._body()
                bot = self._bot(body)
                if path == "/api/scan-now":
                    self._json(HTTPStatus.OK, bot.scan_now())
                elif path == "/api/ai/decide":
                    self._json(HTTPStatus.OK, registry.get(None).ai_decide(body, source=f"web:{self._source()}"))
                else:
                    self._json(HTTPStatus.OK, bot.kill_switch(body, source=f"web:{self._source()}"))
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})
            except Exception:
                log.exception("Error en POST %s", path)
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "error interno"})

    return Handler


def build_server(app: DashboardApp | BotRegistry, host: str = "127.0.0.1", port: int = 8050,
                 binance: AccountView | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app, binance))
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

    bots_file = Path(os.environ.get("WEB_BOTS_FILE") or BOTS_FILE)
    if load_bots(bots_file):
        registry = BotRegistry.from_file(bots_file, key)
    else:
        cfg, params = settings_from_env()
        registry = BotRegistry([DashboardApp(cfg, params, key)])
    binance = AccountView.from_env()
    server = build_server(registry, host, port, binance)
    log.info("Cuenta de Binance (solo lectura): %s",
             f"llave {binance.client.key_hint}" if binance.client else "sin configurar")
    log.info("Panel en http://%s:%d con %d bot(s): %s", host, port, len(registry.apps),
             ", ".join(f"{a.bot['name']} ({a.cfg.db_path})" for a in registry.apps.values()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
