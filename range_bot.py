#!/usr/bin/env python3
"""
range_bot.py — Bot "Rupturas de rango" en paper trading 24/7 (la regla de range_lab.py que pasó el 2-nov-2026).

Idea de Keiner: "siempre veo laterales". El laboratorio mostró que los laterales se rompen más de lo que rebotan,
así que este bot NO compra el piso: espera un lateral confirmado y entra A FAVOR de la ruptura.

Regla (deploy/rango_reglas.json, mismos nombres que range_lab.Variant; deploy/rango_params.json es solo
el {"allow_short": true} que lee el panel, como en los demás bots):
  1. Al cierre de cada vela de 4 h, en cada moneda libre: techo = máximo y piso = mínimo de las últimas 90 velas
     (15 días). Es un lateral si el ancho está entre 6% y 25%, el precio tocó la zona del piso Y la del techo al
     menos 2 veces (zona = 15% del ancho), el rango mide ≥ 2 ATR y el cierre sigue dentro. Entonces el bot "arma"
     ese rango y le reserva dinero (Capa 3: si sale mal, se pierde como mucho 1,5% del saldo; máx. 30% del saldo
     por rango, 85% en total y 3 rangos a la vez entre armados y abiertos).
  2. Cada 15 min revisa los rangos armados:
       cierre sobre techo + 0,5 ATR  → COMPRA (largo) al precio actual.
       precio toca piso − 1,5 ATR    → VENDE EN CORTO ahí mismo (solo papel: con dinero real exige Futuros 1x).
                                       Con "brk_short": false, el rango simplemente se descarta.
       pasaron 30 días (2 × 90 velas) sin ruptura → se descarta.
  3. La posición arranca con el stop en la mitad del rango y luego un trailing a 3 ATR del mejor precio (solo se
     mueve a favor). Sale como mucho a los 30 días. Sin take profit fijo.
  4. Capa 1 igual que los demás bots (3 pérdidas → 15 velas de 4 h sin armar rangos; 2 → riesgo a la mitad; caída
     del día ≥ 3% → HALTED) y apagado solo si el saldo cae 20% desde su máximo.
Costos como en el laboratorio: 0,1% de comisión por orden y 0,05% de deslizamiento por lado.

Backtest (docs/RANGO.md, 41 monedas + PAXG, 2024-01 → 2026-10, 500 USD): +17,6% con caída máx. 5,4%, PF 1,37,
191 rangos, 16 de 20 grupos de monedas positivos; 2024 +3,4%, 2025 +12,9%, 2026 −0,4%.

Panel: base SQLite propia (data/rango_state.db); en deploy/bots.json la ficha lleva "dynamic_symbols": true y el
último ciclo publica en "watching" las monedas con rango armado o posición.

Uso en el VPS (servicio deploy/ingresosbot-rango.service):
  python range_bot.py --env-file .env --log-file logs/bot-rango.log
  python range_bot.py --once          # un ciclo y termina (pruebas)
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import urllib.parse
from dataclasses import fields
from pathlib import Path
from typing import Callable

import pandas as pd

import market_fetcher as mf
import quant_core_engine as q
import range_lab as rl
from notifier import Notifier, load_env_file
from state_manager import StateManager

log = logging.getLogger("range_bot")

M15 = 900_000
SLIP = rl.SLIP
MIN_NOTIONAL = 5.0
KILL_DD_PCT = 20.0
HEARTBEAT_S = 60.0
LABEL = "Rupturas de rango"
DEFAULT_PARAMS = "deploy/rango_reglas.json"

K_RANGES, K_POS, K_PEAK, K_KILLED = "armed_ranges", "range_positions", "peak_balance", "range_killed"


def _iso(ms: int) -> str:
    return pd.Timestamp(ms, unit="ms", tz="UTC").isoformat()


def _ms(ts) -> int:
    return int(pd.Timestamp(ts).timestamp() * 1000)


def load_variant(path: str | Path | None) -> rl.Variant:
    """Variant desde un JSON con los mismos nombres de campo (rango_params_candidato.json del laboratorio)."""
    data = {}
    if path and Path(path).exists():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    names = {f.name for f in fields(rl.Variant)}
    unknown = sorted(set(data) - names - {"symbols", "note"})
    if unknown:
        raise ValueError(f"Campos desconocidos en {path}: {unknown}")
    return rl.Variant(**{k: v for k, v in data.items() if k in names})


def _hosts() -> list[str]:
    by_name = {"binance": mf.BINANCE_HOSTS[0], "binance_vision": mf.BINANCE_HOSTS[1]}
    hosts = [by_name[n] for n in mf.configured_sources() if n in by_name]
    return hosts or list(mf.BINANCE_HOSTS)


class RangeBot:
    def __init__(self, v: rl.Variant, symbols: list[str], db_path: str = "data/rango_state.db",
                 balance: float = 500.0, fee_pct: float = rl.FEE_MARKET * 100, notifier: Notifier | None = None,
                 get: Callable[[str], object] | None = None, price_source: Callable[[str], float] | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 hosts: list[str] | None = None, watch_seconds: float = 15.0, close_delay_seconds: float = 10.0):
        if v.levels != 0 or not v.brk_long:
            raise ValueError("range_bot solo opera rupturas (levels = 0, brk_long = true)")
        self.v = v
        self.symbols = symbols
        self.det_ms = rl.BAR_MIN[v.det_tf] * 60_000
        self.life_ms = int(v.life_mult * v.lookback) * self.det_ms
        self.sm = StateManager(db_path, balance, q.Params(allow_short=v.brk_short), fee_pct)
        self.db_path = db_path
        self.notifier = notifier or Notifier.from_env(label=LABEL)
        self.http = get or (lambda url: mf._http_get_json(url, timeout=20))
        self.hosts = hosts or _hosts()
        self.price_source = price_source or (lambda s: mf.fetch_price(s)[0])
        self.clock, self.sleep = clock, sleep
        self.watch_seconds, self.close_delay = watch_seconds, close_delay_seconds
        self._seen_transitions = 0
        self._last_beat = 0.0
        self._stop = False

    # ── Utilidades ──────────────────────────────────────────────────────────
    def _get(self, path: str):
        last = None
        for host in self.hosts:
            try:
                return self.http(f"{host}{path}")
            except Exception as exc:
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

    def klines(self, symbol: str, interval: str, now_ms: int, start_ms: int | None = None,
               limit: int = 1000) -> pd.DataFrame:
        """Velas CERRADAS (las que terminan antes de now_ms)."""
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        data = self._get(f"/api/v3/klines?{urllib.parse.urlencode(params)}")
        if not isinstance(data, list):
            raise mf.FetchError(f"{symbol}: respuesta inesperada {str(data)[:120]}")
        if not data:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        df = mf._to_frame([list(r[:7]) for r in data], now_ms)
        step = mf.INTERVAL_MS[interval]
        return df[df["time"].map(_ms) + step <= now_ms].reset_index(drop=True)

    @staticmethod
    def last_closed_open(now_s: float, step_ms: int) -> int:
        return (int(now_s * 1000) // step_ms) * step_ms - step_ms

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
        log.info("Cerrada %s %s %s %s PnL %.2f", trade["symbol"], trade["side"], trade["exit_reason"],
                 trade["result"], trade["pnl_usd"])
        self.notifier.position_closed(trade, self.sm.account())
        pos = self._meta(K_POS, {})
        pos.pop(str(trade["position_id"]), None)
        self._set_meta(K_POS, pos)
        return {"event": "CLOSE", **trade}

    def used_alloc(self) -> float:
        r = sum(x["alloc"] for x in self._meta(K_RANGES, {}).values())
        p = sum(x["alloc"] for x in self._meta(K_POS, {}).values())
        return r + p

    def busy_symbols(self) -> set[str]:
        return set(self._meta(K_RANGES, {})) | {p["symbol"] for p in self.sm.positions()}

    # ── 1) Armar rangos al cierre de cada vela de 4 h ───────────────────────
    def scan(self, det_open: int, now_ms: int) -> dict:
        out: dict = {}
        acc = self.sm.account()
        killed = self.sm.meta_get(K_KILLED)
        blocked = ("apagado desde el panel" if acc["manual_halt"] else "Capa 1 en HALTED"
                   if acc["status"] == "HALTED" else "apagado por el -20%" if killed else "")
        ranges = self._meta(K_RANGES, {})
        busy = self.busy_symbols()
        need = self.v.lookback + 230                       # EMA 200 / ATR necesitan historia
        for sym in self.symbols:
            if sym in busy:
                continue
            try:
                d = self.klines(sym, self.v.det_tf, now_ms, limit=min(1000, need))
            except Exception as exc:
                out[sym] = {"decision": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
                continue
            if len(d) <= self.v.lookback + 30 or _ms(d["time"].iloc[-1]) != det_open:
                out[sym] = {"decision": "STALE_DATA", "reason": "Binance aún no publica la última vela de 4 h"}
                continue
            ok, lo, hi, atr = rl.range_mask(d, self.v)
            if not bool(ok[-1]):
                continue
            lo, hi, atr = float(lo[-1]), float(hi[-1]), float(atr[-1])
            width = (hi / lo - 1) * 100
            if blocked:
                out[sym] = {"decision": "WAIT / NO_TRADE", "reason": f"lateral de {width:.1f}%, pero {blocked}"}
                continue
            if len(ranges) + len(self.sm.positions()) >= self.v.max_open:
                out[sym] = {"decision": "WAIT / NO_TRADE",
                            "reason": f"lateral de {width:.1f}%, pero ya hay {self.v.max_open} rangos en juego"}
                continue
            bal = acc["balance"]
            rp = self.v.risk_pct / 2 if acc["status"] == "THROTTLED" else self.v.risk_pct
            worst = max(rl.breakout_worst(lo, hi, atr, self.v), 1e-4)
            alloc = min(bal * self.v.max_alloc_pct / 100, bal * rp / 100 / worst, bal * 0.85 - self.used_alloc())
            if alloc < MIN_NOTIONAL:
                out[sym] = {"decision": "WAIT / NO_TRADE", "reason": "saldo libre insuficiente para el mínimo"}
                continue
            ranges[sym] = {"lo": lo, "hi": hi, "atr": atr, "armed_at": det_open + self.det_ms,
                           "expires": det_open + self.det_ms + self.life_ms, "last_bar": det_open + self.det_ms - M15,
                           "alloc": alloc}
            self._set_meta(K_RANGES, ranges)
            up = hi + self.v.brk_atr * atr
            down = lo - self.v.stop_atr * atr
            short_txt = (f"si toca {down:.6g} vende en corto (papel)" if self.v.brk_short
                         else f"si toca {down:.6g} lo descarta")
            log.info("Rango armado %s: piso %.6g techo %.6g (%.1f%%), ATR %.6g, reserva %.2f USD",
                     sym, lo, hi, width, atr, alloc)
            self.notifier.send("range_armed", {"symbol": sym, "floor": lo, "ceiling": hi, "alloc_usd": alloc},
                               f"📐 LATERAL CONFIRMADO {sym}\nPiso {lo:.6g} · techo {hi:.6g} (ancho {width:.1f}%)\n"
                               f"Si cierra sobre {up:.6g} compra; {short_txt}.\nReserva {alloc:,.2f} USD")
            out[sym] = {"decision": "ARMED", "reason": f"lateral {lo:.6g}–{hi:.6g} ({width:.1f}%): compra sobre "
                                                     f"{up:.6g}; {short_txt}"}
        return out

    # ── 2) Vigilar rangos armados (velas de 15 min) ─────────────────────────
    def _open(self, sym: str, side: str, entry: float, r: dict, bar_ms: int, at: str) -> dict:
        mid = (r["lo"] + r["hi"]) / 2
        units = r["alloc"] / entry
        pid = self.sm.open_position(sym, side, entry, mid, 0.0, 0.0, 0.0, units, units * abs(entry - mid), at,
                                    regime="RANGO", risk_mode=f"reserva {r['alloc']:.2f} USD")
        self.sm.conn.execute("UPDATE open_positions SET trail_peak = ? WHERE id = ?", (entry, pid))
        self.sm.conn.commit()
        pos = self._meta(K_POS, {})
        pos[str(pid)] = {"entry_bar": bar_ms, "last_bar": bar_ms - M15, "atr": r["atr"], "alloc": r["alloc"],
                         "expires": bar_ms + self.life_ms, "lo": r["lo"], "hi": r["hi"]}
        self._set_meta(K_POS, pos)
        word = "COMPRA" if side == "LONG" else "VENTA EN CORTO"
        why = "rompió el techo" if side == "LONG" else "rompió el piso"
        log.info("%s %s @ %.8g (%.2f USD, SL %.8g)", word, sym, entry, r["alloc"], mid)
        self.notifier.send("position_opened", {"symbol": sym, "side": side, "entry_price": entry, "stop_loss": mid,
                                               "units": units, "notional_usd": r["alloc"]},
                           f"{'🟢' if side == 'LONG' else '🔴'} {word} (papel) {sym}\n"
                           f"El lateral {r['lo']:.6g}–{r['hi']:.6g} {why}. Entrada: {entry:.6g}\n"
                           f"Monto: {r['alloc']:,.2f} USD · SL: {mid:.6g} (mitad del rango)\n"
                           f"Trailing a {self.v.trail_atr:g} ATR del mejor precio · máx. 30 días")
        return {"decision": side, "position_id": pid, "reason": f"{why}: {word.lower()} a {entry:.6g}"}

    def watch_range(self, sym: str, r: dict, bar15: int, now_ms: int, at: str) -> dict:
        df = self.klines(sym, "15m", now_ms, start_ms=r["last_bar"] + M15)
        df = df[df["time"].map(_ms) <= bar15].reset_index(drop=True)
        down = r["lo"] - self.v.stop_atr * r["atr"]
        up = r["hi"] + self.v.brk_atr * r["atr"]
        ranges = self._meta(K_RANGES, {})
        for k, row in enumerate(df.itertuples(index=False)):
            t = _ms(row.time)
            if row.low <= down:
                ranges.pop(sym, None)
                self._set_meta(K_RANGES, ranges)
                if not self.v.brk_short:
                    return {"decision": "SKIPPED", "reason": f"rompió el piso ({down:.6g}): rango descartado"}
                if not self._can_open():
                    return {"decision": "SKIPPED", "reason": "rompió el piso, pero la cuenta no puede abrir"}
                entry = min(row.open, down) * (1 - SLIP)
                d = self._open(sym, "SHORT", entry, r, t, at)
                mid = (r["lo"] + r["hi"]) / 2
                if row.high >= mid:                            # misma vela: peor caso, también tocó su stop
                    trade = self.sm.close_position(d["position_id"], mid * (1 + SLIP), "STOP_LOSS", _iso(t + M15))
                    self._closed(trade)
                    return {"decision": "SKIPPED", "reason": f"corto cerrado en la misma vela ({trade['pnl_usd']:+.2f})"}
                self._advance(d["position_id"], t)
                return d
            if row.close > up:
                ranges.pop(sym, None)
                self._set_meta(K_RANGES, ranges)
                if not self._can_open():
                    return {"decision": "SKIPPED", "reason": "rompió el techo, pero la cuenta no puede abrir"}
                if k + 1 < len(df):                             # vela vieja (recuperando): entra en la apertura siguiente
                    nxt = df.iloc[k + 1]
                    entry, t_in = float(nxt["open"]) * (1 + SLIP), _ms(nxt["time"])
                else:
                    entry, t_in = float(self.price_source(sym)) * (1 + SLIP), t + M15
                return self._open(sym, "LONG", entry, r, t_in, at)
            if t + M15 >= r["expires"]:
                ranges.pop(sym, None)
                self._set_meta(K_RANGES, ranges)
                return {"decision": "SKIPPED", "reason": "30 días sin ruptura: rango descartado"}
            r["last_bar"] = t
        ranges[sym] = r
        self._set_meta(K_RANGES, ranges)
        return {"decision": "WAIT / NO_TRADE",
                "reason": f"lateral {r['lo']:.6g}–{r['hi']:.6g}: compra sobre {up:.6g}"
                          + (f", corto bajo {down:.6g}" if self.v.brk_short else f", descarta bajo {down:.6g}")}

    def _can_open(self) -> bool:
        a = self.sm.account()
        return not a["manual_halt"] and a["status"] != "HALTED"

    def _advance(self, pid: int, t: int) -> None:
        pos = self._meta(K_POS, {})
        if str(pid) in pos:
            pos[str(pid)]["last_bar"] = t
            self._set_meta(K_POS, pos)

    # ── 3) Posiciones abiertas: stop en la mitad del rango y trailing ───────
    def manage(self, pos: dict, bar15: int, now_ms: int) -> dict:
        meta = self._meta(K_POS, {}).get(str(pos["id"]))
        if meta is None:
            return {"decision": "HOLD", "reason": "posición sin ficha: solo se cierra a mano"}
        df = self.klines(pos["symbol"], "15m", now_ms, start_ms=meta["last_bar"] + M15)
        df = df[df["time"].map(_ms) <= bar15]
        long = pos["side"] == "LONG"
        stop, best = pos["stop_loss"], pos["trail_peak"] or pos["entry_price"]
        trail = self.v.trail_atr * meta["atr"]
        for row in df.itertuples(index=False):
            t = _ms(row.time)
            if t < meta["entry_bar"]:
                continue
            px = why = None
            if long and row.low <= stop:
                px = min(row.open, stop) * (1 - SLIP)
            elif not long and row.high >= stop:
                px = max(row.open, stop) * (1 + SLIP)
            elif t + M15 >= meta["expires"]:
                px, why = row.close * (1 - SLIP if long else 1 + SLIP), "TIEMPO"
            if px is not None:
                why = why or ("STOP_LOSS" if stop == pos["initial_stop_loss"] else "TRAILING_STOP")
                trade = self.sm.close_position(pos["id"], px, why, _iso(t + M15))
                self._closed(trade)
                return {"decision": "SKIPPED", "reason": f"cerrada: {why} ({trade['pnl_usd']:+.2f} USD)"}
            if long:
                best = max(best, row.high)
                stop = max(stop, best - trail)
            else:
                best = min(best, row.low)
                stop = min(stop, best + trail)
            meta["last_bar"] = t
        self.sm.conn.execute("UPDATE open_positions SET stop_loss = ?, trail_peak = ? WHERE id = ?",
                             (stop, best, pos["id"]))
        self.sm.conn.commit()
        allm = self._meta(K_POS, {})
        allm[str(pos["id"])] = meta
        self._set_meta(K_POS, allm)
        return {"decision": "HOLD", "reason": f"SL {stop:.6g} (mejor precio {best:.6g})"}

    # ── Kill switch: -20% desde el máximo del saldo ─────────────────────────
    def kill_check(self) -> None:
        acc = self.sm.account()
        bal = acc["balance"]
        killed = self.sm.meta_get(K_KILLED)
        if killed and not acc["manual_halt"]:
            self.sm.conn.execute("DELETE FROM runner_meta WHERE key = ?", (K_KILLED,))
            self.sm.conn.commit()
            self.sm.meta_set(K_PEAK, str(bal))
            return
        peak = max(float(self.sm.meta_get(K_PEAK) or acc["initial_balance"]), bal)
        self.sm.meta_set(K_PEAK, str(peak))
        dd = (1 - bal / peak) * 100 if peak > 0 else 0.0
        if dd >= KILL_DD_PCT and not acc["manual_halt"]:
            reason = f"el saldo cayó {dd:.1f}% desde su máximo ({peak:,.2f} → {bal:,.2f} USD)"
            res = self.sm.set_manual_halt(True, reason, source="range_bot")
            self.sm.meta_set(K_KILLED, json.dumps({"at": _iso(int(self.clock() * 1000)), "reason": reason}))
            self._set_meta(K_RANGES, {})
            self.notifier.kill_switch(res | {"open_positions": len(self.sm.positions())}, self.sm.account(),
                                      reason, "range_bot (-20%)")

    # ── Ciclo de 15 min ─────────────────────────────────────────────────────
    def candle_cycle(self, now_s: float | None = None) -> dict:
        now_s = self.clock() if now_s is None else now_s
        now_ms = int(now_s * 1000)
        bar15 = self.last_closed_open(now_s, M15)
        last = self.sm.meta_get("last_bar_open_ms")
        if last is not None and int(last) >= bar15:
            return {"skipped": True, "bar_open": _iso(bar15)}
        det_open = self.last_closed_open(now_s, self.det_ms)
        last_det = self.sm.meta_get("last_det_open_ms")
        new_det = last_det is None or int(last_det) < det_open
        if new_det:                                          # la Capa 1 cuenta velas de 4 h, como el laboratorio
            missed = 1 if last_det is None else max(1, min((det_open - int(last_det)) // self.det_ms, 1000))
            for k in range(missed):
                self.sm.on_new_bar(_iso(det_open - (missed - 1 - k) * self.det_ms))
            self._flush_status_alerts()
        at = _iso(now_ms)
        summary: dict = {"skipped": False, "bar_open": _iso(bar15), "symbols": {}}
        for pos in self.sm.positions():
            try:
                summary["symbols"][pos["symbol"]] = self.manage(pos, bar15, now_ms)
            except Exception as exc:
                log.exception("Error revisando %s", pos["symbol"])
                summary["symbols"][pos["symbol"]] = {"decision": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
        for sym, r in list(self._meta(K_RANGES, {}).items()):
            try:
                summary["symbols"][sym] = self.watch_range(sym, r, bar15, now_ms, at)
            except Exception as exc:
                log.warning("Error vigilando %s: %s", sym, exc)
                summary["symbols"][sym] = {"decision": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
        self.kill_check()
        if new_det:
            summary["symbols"].update(self.scan(det_open, now_ms))
            self.sm.meta_set("last_det_open_ms", str(det_open))
        self._flush_status_alerts()
        holding = {p["symbol"] for p in self.sm.positions()}
        summary["watching"] = sorted(holding | set(self._meta(K_RANGES, {})))
        summary["status"] = self.sm.account()["status"]
        summary["processed_at"] = _iso(int(self.clock() * 1000))
        self.sm.meta_set("last_bar_open_ms", str(bar15))
        self.sm.meta_set("last_cycle", json.dumps(summary, ensure_ascii=False, default=str))
        return summary

    # ── "Apagar y cerrar" del panel ─────────────────────────────────────────
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
                px = price * (1 - SLIP if pos["side"] == "LONG" else 1 + SLIP)
                events.append(self._closed(self.sm.close_position(pos["id"], px, "MANUAL_CLOSE", at)))
            if not pending:
                self._set_meta(K_RANGES, {})
                self.sm.cancel_close_all()
        self._flush_status_alerts()
        return events

    def run_forever(self, max_loops: int | None = None) -> None:
        log.info("Bot Rupturas de rango iniciado (%s, %d monedas, base %s): %s", self.v.det_tf, len(self.symbols),
                 self.db_path, self.v.label())
        loops = 0
        while not self._stop and (max_loops is None or loops < max_loops):
            now = self.clock()
            try:
                self.watch_cycle(now)
                self.heartbeat(now)
                since_close = (now * 1000 - (self.last_closed_open(now, M15) + M15)) / 1000
                if since_close >= self.close_delay:
                    s = self.candle_cycle(now)
                    if not s["skipped"]:
                        log.info("Vela %s: vigila %s; %s", s["bar_open"], ",".join(s["watching"]) or "nada",
                                 json.dumps({k: v.get("decision") for k, v in s["symbols"].items()},
                                            ensure_ascii=False))
            except Exception:
                log.exception("Error en el ciclo; se reintenta en el siguiente")
            loops += 1
            if not self._stop and (max_loops is None or loops < max_loops):
                self.sleep(self.watch_seconds)
        log.info("Bot detenido")

    def close(self) -> None:
        self.sm.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bot Rupturas de rango en paper trading.")
    ap.add_argument("--params", default=DEFAULT_PARAMS)
    ap.add_argument("--db", default="data/rango_state.db")
    ap.add_argument("--balance", type=float, default=500.0, help="Balance inicial (solo si la base es nueva)")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--log-file")
    ap.add_argument("--once", action="store_true", help="Un ciclo y termina")
    args = ap.parse_args(argv)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    load_env_file(args.env_file)
    raw = json.loads(Path(args.params).read_text(encoding="utf-8")) if Path(args.params).exists() else {}
    symbols = raw.get("symbols") or list(rl.UNIVERSE)
    bot = RangeBot(load_variant(args.params), symbols, args.db, args.balance)
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
