#!/usr/bin/env python3
"""
notifier.py — Alertas del bot a un webhook (n8n) y/o Telegram, solo con urllib.

Configuración por variables de entorno (o un archivo .env con --env-file en bot_runner.py):
  WEBHOOK_URL          URL que recibe un POST JSON por cada evento (p. ej. un Webhook de n8n)
  TELEGRAM_BOT_TOKEN   token de @BotFather
  TELEGRAM_CHAT_ID     chat o grupo que recibe los mensajes

Eventos:
  position_opened   nueva posición abierta
  break_even        Stop-Loss protegido en Break-Even
  position_closed   posición cerrada (WIN / LOSS / BREAKEVEN) con balance y racha
  status_changed    el Circuit Breaker pasa a THROTTLED o HALTED

Un fallo de red nunca detiene el bot: se registra en el log y send() devuelve False.

Prueba rápida:
  python notifier.py --test
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("notifier")

TELEGRAM_API = "https://api.telegram.org"
STATUS_ALERTS = ("THROTTLED", "HALTED")


def _http_post_json(url: str, payload: dict, timeout: float = 10.0) -> int:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "Ingresosbot-notifier/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def load_env_file(path: str | Path) -> None:
    """Carga KEY=VALUE de un archivo .env sin pisar variables ya definidas."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _fmt(x: float | None, nd: int = 2) -> str:
    return "-" if x is None else f"{x:,.{nd}f}"


class Notifier:
    def __init__(self, webhook_url: str | None = None, telegram_token: str | None = None,
                 telegram_chat_id: str | None = None, post: Callable[[str, dict], int] = _http_post_json,
                 retries: int = 1):
        self.webhook_url = webhook_url or None
        self.telegram_token = telegram_token or None
        self.telegram_chat_id = telegram_chat_id or None
        self.post = post
        self.retries = retries

    @classmethod
    def from_env(cls, **kw) -> "Notifier":
        return cls(os.environ.get("WEBHOOK_URL"), os.environ.get("TELEGRAM_BOT_TOKEN"),
                   os.environ.get("TELEGRAM_CHAT_ID"), **kw)

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url or (self.telegram_token and self.telegram_chat_id))

    # ── Envío ───────────────────────────────────────────────────────────────
    def _post(self, url: str, payload: dict, label: str) -> bool:
        for attempt in range(self.retries + 1):
            try:
                status = self.post(url, payload)
                if 200 <= int(status) < 300:
                    return True
                log.warning("%s respondió HTTP %s", label, status)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.warning("%s falló (intento %d): %s", label, attempt + 1, exc)
        return False

    def send(self, event: str, data: dict, text: str) -> bool:
        """Envía a todos los destinos configurados. True si al menos uno lo recibió."""
        if not self.enabled:
            log.info("[sin destino] %s: %s", event, text.replace("\n", " | "))
            return False
        ok = []
        if self.webhook_url:
            ok.append(self._post(self.webhook_url, {
                "event": event, "data": data, "text": text,
                "sent_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}, "webhook"))
        if self.telegram_token and self.telegram_chat_id:
            ok.append(self._post(f"{TELEGRAM_API}/bot{self.telegram_token}/sendMessage",
                                 {"chat_id": self.telegram_chat_id, "text": text,
                                  "disable_web_page_preview": True}, "telegram"))
        return any(ok)

    # ── Los 4 eventos ───────────────────────────────────────────────────────
    def position_opened(self, pos: dict, account: dict) -> bool:
        text = (f"🟢 NUEVA POSICIÓN {pos['side']} {pos['symbol']}\n"
                f"Entrada: {_fmt(pos['entry_price'])}\n"
                f"SL: {_fmt(pos['stop_loss'])} · TP: {_fmt(pos['take_profit'])}\n"
                f"Break-Even al tocar {_fmt(pos['breakeven_trigger'])} → SL {_fmt(pos['breakeven_stop_price'])}\n"
                f"Unidades: {pos['position_units']} · Riesgo: {_fmt(pos['risk_usd'])} USD ({pos.get('risk_mode')})\n"
                f"Régimen: {pos.get('regime')} · Balance: {_fmt(account['balance'])} USD")
        return self.send("position_opened", {"position": pos, "account": account}, text)

    def break_even(self, pos: dict, price: float | None = None) -> bool:
        text = (f"🛡️ SL PROTEGIDO EN BREAK-EVEN {pos['side']} {pos['symbol']}\n"
                f"Precio tocó {_fmt(price if price is not None else pos['breakeven_trigger'])} "
                f"(trigger {_fmt(pos['breakeven_trigger'])})\n"
                f"Nuevo SL: {_fmt(pos['breakeven_stop_price'])} (entrada {_fmt(pos['entry_price'])})")
        return self.send("break_even", {"position": pos, "price": price}, text)

    def position_closed(self, trade: dict, account: dict) -> bool:
        icon = {"WIN": "✅", "LOSS": "🔴", "BREAKEVEN": "⚪"}.get(trade["result"], "•")
        text = (f"{icon} POSICIÓN CERRADA {trade['result']} {trade['side']} {trade['symbol']}\n"
                f"Motivo: {trade['exit_reason']} · {_fmt(trade['entry_price'])} → {_fmt(trade['exit_price'])}\n"
                f"PnL: {trade['pnl_usd']:+,.2f} USD ({trade['pnl_pct']:+.2f}%) · R: {trade.get('r_multiple')}\n"
                f"Balance: {_fmt(account['balance'])} USD · Racha de pérdidas: {account['consecutive_losses']}\n"
                f"DD diario: {account['daily_drawdown_pct']:.2f}% · Estado: {account['status']}")
        return self.send("position_closed", {"trade": trade, "account": account}, text)

    def status_changed(self, transition: dict, account: dict) -> bool:
        if transition["to"] not in STATUS_ALERTS:
            return False
        icon = "🟡" if transition["to"] == "THROTTLED" else "⛔"
        extra = ("Riesgo reducido a 0.75% por operación." if transition["to"] == "THROTTLED"
                 else f"Trading detenido. Enfriamiento: {account['cooldown_bars_remaining']} velas.")
        text = (f"{icon} CIRCUIT BREAKER: {transition['from']} → {transition['to']}\n"
                f"Motivo: {'; '.join(transition.get('reasons', []))}\n{extra}\n"
                f"Balance: {_fmt(account['balance'])} USD · Racha: {account['consecutive_losses']} · "
                f"DD diario: {account['daily_drawdown_pct']:.2f}%")
        return self.send("status_changed", {"transition": transition, "account": account}, text)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Prueba de alertas.")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--test", action="store_true", help="Envía un mensaje de prueba a los destinos configurados")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env_file(args.env_file)
    n = Notifier.from_env()
    if args.test:
        ok = n.send("test", {"ok": True}, "🔔 Ingresosbot: prueba de alertas correcta.")
        print("enviado" if ok else "no se pudo enviar (revisa WEBHOOK_URL / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
