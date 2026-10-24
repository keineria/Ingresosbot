#!/usr/bin/env python3
"""
state_manager.py — Persistencia del bot en SQLite (solo librería estándar).

Archivo por defecto: data/trading_state.db, con 3 tablas:
  account_state   1 fila: balance, racha, drawdown diario, enfriamiento y estado del Circuit Breaker
  open_positions  posiciones abiertas con SL/TP/Break-Even y la marca is_at_breakeven
  trade_history   cada trade cerrado (WIN / LOSS / BREAKEVEN) con PnL en USD y %

Lógica automática:
  * Precio >= breakeven_trigger (LONG) -> stop_loss = breakeven_stop_price, is_at_breakeven = 1.
  * Cierre por TP / SL / BE -> actualiza balance, consecutive_losses, daily_drawdown_pct,
    enfriamiento de 15 velas y status (reglas de SKILL_3, vía quant_core_engine).

Uso típico (en vivo o en paper trading):
  sm = StateManager("data/trading_state.db", initial_balance=10000)
  sm.on_new_bar(candle_time)                       # una vez por vela cerrada
  sm.process_candle("BTCUSDT", t, o, h, l, c)      # gestiona SL/TP/BE con la vela
  result = run_engine(sm.engine_account(), candles=df, symbol="BTCUSDT")
  sm.open_from_engine(result, opened_at=t)         # guarda la orden si hubo señal
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import quant_core_engine as q

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_state (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    balance                 REAL    NOT NULL,
    initial_balance         REAL    NOT NULL,
    consecutive_losses      INTEGER NOT NULL DEFAULT 0,
    daily_drawdown_pct      REAL    NOT NULL DEFAULT 0,
    cooldown_bars_remaining INTEGER NOT NULL DEFAULT 0,
    status                  TEXT    NOT NULL DEFAULT 'ACTIVE'
                            CHECK (status IN ('ACTIVE', 'THROTTLED', 'HALTED')),
    day_peak_equity         REAL    NOT NULL,
    current_day             TEXT,
    updated_at              TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS open_positions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                TEXT    NOT NULL,
    side                  TEXT    NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    entry_price           REAL    NOT NULL,
    stop_loss             REAL    NOT NULL,
    take_profit           REAL    NOT NULL,
    breakeven_trigger     REAL    NOT NULL,
    breakeven_stop_price  REAL    NOT NULL,
    position_units        REAL    NOT NULL,
    initial_stop_loss     REAL    NOT NULL,
    risk_usd              REAL    NOT NULL,
    is_at_breakeven       INTEGER NOT NULL DEFAULT 0 CHECK (is_at_breakeven IN (0, 1)),
    regime                TEXT,
    risk_mode             TEXT,
    opened_at             TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT    NOT NULL,
    side              TEXT    NOT NULL,
    result            TEXT    NOT NULL CHECK (result IN ('WIN', 'LOSS', 'BREAKEVEN')),
    exit_reason       TEXT    NOT NULL,
    entry_price       REAL    NOT NULL,
    exit_price        REAL    NOT NULL,
    position_units    REAL    NOT NULL,
    was_at_breakeven  INTEGER NOT NULL,
    pnl_usd           REAL    NOT NULL,
    pnl_pct           REAL    NOT NULL,
    fees_usd          REAL    NOT NULL,
    r_multiple        REAL,
    balance_after     REAL    NOT NULL,
    regime            TEXT,
    risk_mode         TEXT,
    opened_at         TEXT    NOT NULL,
    closed_at         TEXT    NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ts(t: Any) -> str:
    return t.isoformat() if hasattr(t, "isoformat") else str(t)


def check_exit(side: str, stop_loss: float, take_profit: float, is_at_breakeven: bool,
               o: float, h: float, l: float) -> tuple[float, str] | None:
    """(precio_salida, motivo) si la vela cierra la posición, o None.

    Conservador: si la vela toca SL y TP, se asume que tocó primero el SL.
    Si abre más allá de un nivel (gap), sale al open.
    """
    sl_reason = "BREAK_EVEN" if is_at_breakeven else "STOP_LOSS"
    if side == "LONG":
        if o <= stop_loss:
            return o, sl_reason + "_GAP"
        if o >= take_profit:
            return o, "TAKE_PROFIT_GAP"
        if l <= stop_loss:
            return stop_loss, sl_reason
        if h >= take_profit:
            return take_profit, "TAKE_PROFIT"
    else:
        if o >= stop_loss:
            return o, sl_reason + "_GAP"
        if o <= take_profit:
            return o, "TAKE_PROFIT_GAP"
        if h >= stop_loss:
            return stop_loss, sl_reason
        if l <= take_profit:
            return take_profit, "TAKE_PROFIT"
    return None


def break_even_touched(side: str, trigger: float, h: float, l: float) -> bool:
    return h >= trigger if side == "LONG" else l <= trigger


class StateManager:
    def __init__(self, db_path: str | Path = "data/trading_state.db", initial_balance: float = 10000.0,
                 params: q.Params | None = None, fee_pct: float = 0.08):
        self.p = params or q.Params()
        self.fee = fee_pct / 100.0
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        if self.conn.execute("SELECT COUNT(*) FROM account_state").fetchone()[0] == 0:
            self.conn.execute(
                "INSERT INTO account_state (id, balance, initial_balance, day_peak_equity, updated_at) "
                "VALUES (1, ?, ?, ?, ?)", (initial_balance, initial_balance, initial_balance, _now()))
            self.conn.commit()
        self.status_transitions: list[dict] = []   # historial en memoria de cambios de estado

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "StateManager":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # ── Cuenta ──────────────────────────────────────────────────────────────
    def account(self) -> dict:
        return dict(self.conn.execute("SELECT * FROM account_state WHERE id = 1").fetchone())

    def engine_account(self) -> dict:
        """Estado en el formato que consume quant_core_engine.run_engine()."""
        a = self.account()
        return {"balance": a["balance"], "consecutive_losses": a["consecutive_losses"],
                "daily_drawdown_pct": a["daily_drawdown_pct"],
                "cooldown_candles_remaining": a["cooldown_bars_remaining"]}

    def _save_account(self, s: dict, at: Any = None) -> None:
        cb = q.circuit_breaker(q.AccountState.from_dict({
            "balance": s["balance"], "consecutive_losses": s["consecutive_losses"],
            "daily_drawdown_pct": s["daily_drawdown_pct"],
            "cooldown_candles_remaining": s["cooldown_bars_remaining"]}), self.p)
        prev = self.account()["status"]
        if cb["status"] != prev:
            self.status_transitions.append({"at": _ts(at) if at is not None else _now(),
                                            "from": prev, "to": cb["status"], "reasons": cb["reasons"]})
        self.conn.execute(
            "UPDATE account_state SET balance=?, consecutive_losses=?, daily_drawdown_pct=?, "
            "cooldown_bars_remaining=?, status=?, day_peak_equity=?, current_day=?, updated_at=? WHERE id=1",
            (s["balance"], s["consecutive_losses"], s["daily_drawdown_pct"], s["cooldown_bars_remaining"],
             cb["status"], s["day_peak_equity"], s.get("current_day"), _now()))
        self.conn.commit()

    def on_new_bar(self, bar_time: Any) -> None:
        """Llamar una vez por vela cerrada: reinicio diario (00:00 UTC) y cuenta atrás del enfriamiento."""
        s = self.account()
        day = _ts(bar_time)[:10]
        if day != s["current_day"]:
            s["current_day"] = day
            s["day_peak_equity"] = s["balance"]
            s["daily_drawdown_pct"] = 0.0
        ticked = q.tick_cooldown({"consecutive_losses": s["consecutive_losses"],
                                  "daily_drawdown_pct": s["daily_drawdown_pct"],
                                  "cooldown_candles_remaining": s["cooldown_bars_remaining"]}, self.p)
        s["consecutive_losses"] = ticked["consecutive_losses"]
        s["cooldown_bars_remaining"] = ticked["cooldown_candles_remaining"]
        self._save_account(s, bar_time)

    # ── Posiciones ──────────────────────────────────────────────────────────
    def open_position(self, symbol: str, side: str, entry_price: float, stop_loss: float, take_profit: float,
                      breakeven_trigger: float, breakeven_stop_price: float, position_units: float,
                      risk_usd: float, opened_at: Any, regime: str | None = None,
                      risk_mode: str | None = None) -> int:
        if self.account()["status"] == "HALTED":
            raise RuntimeError("Cuenta en HALTED: no se pueden abrir posiciones.")
        cur = self.conn.execute(
            "INSERT INTO open_positions (symbol, side, entry_price, stop_loss, take_profit, breakeven_trigger, "
            "breakeven_stop_price, position_units, initial_stop_loss, risk_usd, regime, risk_mode, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, side, entry_price, stop_loss, take_profit, breakeven_trigger, breakeven_stop_price,
             position_units, stop_loss, risk_usd, regime, risk_mode, _ts(opened_at)))
        self.conn.commit()
        return int(cur.lastrowid)

    def open_from_engine(self, result: dict, opened_at: Any) -> int | None:
        """Guarda la orden de un JSON de run_engine(). Devuelve el id o None si no hubo orden."""
        order = result.get("order")
        if not order:
            return None
        rk = result["layer_3_atr_risk_guardian"]
        return self.open_position(
            symbol=order["symbol"], side=rk["side"], entry_price=rk["entry_price"],
            stop_loss=rk["stop_loss"], take_profit=rk["take_profit"],
            breakeven_trigger=rk["break_even_trigger"], breakeven_stop_price=rk["break_even_new_sl"],
            position_units=rk["quantity"], risk_usd=rk["risk_usd_effective"], opened_at=opened_at,
            regime=result["layer_2_market_regime"]["regime"], risk_mode=order["risk_mode"])

    def positions(self, symbol: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM open_positions", ()
        if symbol:
            sql, args = sql + " WHERE symbol = ?", (symbol,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def _move_to_break_even(self, pos: dict) -> None:
        self.conn.execute("UPDATE open_positions SET stop_loss = ?, is_at_breakeven = 1 WHERE id = ?",
                          (pos["breakeven_stop_price"], pos["id"]))
        self.conn.commit()

    @staticmethod
    def _be_event(pos: dict, price: float | None, at: Any) -> dict:
        moved = dict(pos, stop_loss=pos["breakeven_stop_price"], is_at_breakeven=1)
        return {"event": "BREAK_EVEN", "position_id": pos["id"], "new_stop_loss": pos["breakeven_stop_price"],
                "price": price, "at": _ts(at), "position": moved}

    def open_notional(self) -> float:
        """Nocional comprometido en posiciones abiertas (a precio de entrada)."""
        row = self.conn.execute("SELECT COALESCE(SUM(position_units * entry_price), 0) FROM open_positions").fetchone()
        return float(row[0])

    def process_candle(self, symbol: str, bar_time: Any, o: float, h: float, l: float, c: float,
                       slippage_pct: float = 0.0) -> list[dict]:
        """Gestiona SL/TP/BE con una vela cerrada. El nuevo SL de BE rige desde la vela siguiente."""
        events = []
        slip = slippage_pct / 100.0
        for pos in self.positions(symbol):
            hit = check_exit(pos["side"], pos["stop_loss"], pos["take_profit"], bool(pos["is_at_breakeven"]), o, h, l)
            if hit:
                price, reason = hit
                if not reason.startswith("TAKE_PROFIT"):   # stop a mercado: deslizamiento en contra
                    price *= (1 - slip) if pos["side"] == "LONG" else (1 + slip)
                events.append({"event": "CLOSE", **self.close_position(pos["id"], price, reason, bar_time)})
            elif not pos["is_at_breakeven"] and break_even_touched(pos["side"], pos["breakeven_trigger"], h, l):
                self._move_to_break_even(pos)
                events.append(self._be_event(pos, None, bar_time))
        return events

    def update_price(self, symbol: str, price: float, at: Any = None) -> list[dict]:
        """Versión en vivo: un precio puntual (último trade o ticker)."""
        at = at if at is not None else _now()
        events = []
        for pos in self.positions(symbol):
            hit = check_exit(pos["side"], pos["stop_loss"], pos["take_profit"], bool(pos["is_at_breakeven"]),
                             price, price, price)
            if hit:
                events.append({"event": "CLOSE", **self.close_position(pos["id"], hit[0], hit[1].replace("_GAP", ""), at)})
            elif not pos["is_at_breakeven"] and break_even_touched(pos["side"], pos["breakeven_trigger"], price, price):
                self._move_to_break_even(pos)
                events.append(self._be_event(pos, price, at))
        return events

    def close_position(self, position_id: int, exit_price: float, reason: str, closed_at: Any) -> dict:
        row = self.conn.execute("SELECT * FROM open_positions WHERE id = ?", (position_id,)).fetchone()
        if row is None:
            raise KeyError(f"Posición {position_id} no existe")
        pos = dict(row)
        d = 1 if pos["side"] == "LONG" else -1
        units = pos["position_units"]
        fees = units * pos["entry_price"] * self.fee + units * exit_price * self.fee
        pnl = units * (exit_price - pos["entry_price"]) * d - fees
        cost = units * pos["entry_price"]
        if reason.startswith("BREAK_EVEN") or pnl == 0:
            result = "BREAKEVEN"
        else:
            result = "WIN" if pnl > 0 else "LOSS"

        s = self.account()
        new_balance = s["balance"] + pnl
        # BREAKEVEN es neutro para la racha: ni suma pérdida ni la reinicia.
        streak_pnl = 0.0 if result == "BREAKEVEN" else pnl
        audited = q.postmortem_update(
            {"consecutive_losses": s["consecutive_losses"], "cooldown_candles_remaining": s["cooldown_bars_remaining"]},
            streak_pnl, new_balance, s["day_peak_equity"], self.p)
        s.update(balance=new_balance, consecutive_losses=audited["consecutive_losses"],
                 daily_drawdown_pct=audited["daily_drawdown_pct"], day_peak_equity=audited["day_peak_equity"],
                 cooldown_bars_remaining=audited["cooldown_candles_remaining"])

        trade = {
            "symbol": pos["symbol"], "side": pos["side"], "result": result, "exit_reason": reason,
            "entry_price": pos["entry_price"], "exit_price": round(exit_price, 8), "position_units": units,
            "was_at_breakeven": int(pos["is_at_breakeven"]), "pnl_usd": round(pnl, 2),
            "pnl_pct": round(pnl / cost * 100, 4) if cost else 0.0, "fees_usd": round(fees, 2),
            "r_multiple": round(pnl / pos["risk_usd"], 4) if pos["risk_usd"] > 0 else None,
            "balance_after": round(new_balance, 2), "regime": pos["regime"], "risk_mode": pos["risk_mode"],
            "opened_at": pos["opened_at"], "closed_at": _ts(closed_at),
        }
        cols = ", ".join(trade)
        self.conn.execute(f"INSERT INTO trade_history ({cols}) VALUES ({', '.join('?' * len(trade))})",
                          tuple(trade.values()))
        self.conn.execute("DELETE FROM open_positions WHERE id = ?", (position_id,))
        self._save_account(s, closed_at)   # hace commit de todo junto
        return {"position_id": position_id, **trade}

    # ── Consultas ───────────────────────────────────────────────────────────
    def trades(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM trade_history ORDER BY id")]

    def equity(self, prices: dict[str, float]) -> float:
        """Balance + PnL latente de las posiciones abiertas a los precios dados."""
        eq = self.account()["balance"]
        for pos in self.positions():
            if pos["symbol"] in prices:
                d = 1 if pos["side"] == "LONG" else -1
                eq += pos["position_units"] * (prices[pos["symbol"]] - pos["entry_price"]) * d
        return eq


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Consulta el estado guardado del bot.")
    ap.add_argument("--db", default="data/trading_state.db")
    ap.add_argument("--init-balance", type=float, default=10000.0)
    args = ap.parse_args()
    sm = StateManager(args.db, args.init_balance)
    print(json.dumps({"account_state": sm.account(), "open_positions": sm.positions(),
                      "trade_history_last_10": sm.trades()[-10:]}, ensure_ascii=False, indent=2))
