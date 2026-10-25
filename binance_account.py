#!/usr/bin/env python3
"""
binance_account.py — Saldo real de tu cuenta de Binance, SOLO LECTURA (librería estándar: urllib + hmac).

Qué hace: con una API key de Binance que solo tenga "Enable Reading", lee lo que tienes en Spot, en Earn
(Simple Earn flexible y bloqueado) y en la billetera Funding, y lo valora en USD con los precios públicos.

Qué NO puede hacer: crear, cambiar o cancelar órdenes, mover fondos ni retirar. El módulo solo conoce las
llamadas de READ_ONLY_CALLS y se niega a cualquier otra antes de tocar la red. Además, antes de leer el saldo
pregunta a Binance qué permisos tiene la llave: si permite operar, retirar o transferir, no la usa.
Los bots siguen en paper; nada de este archivo se conecta con bot_runner.py.

Configuración (.env):
  BINANCE_API_KEY=...       API Key de la llave de solo lectura
  BINANCE_API_SECRET=...    Secret Key de esa llave
  BINANCE_API_URL=          opcional; por defecto https://api.binance.com
Guía paso a paso: docs/BINANCE_SOLO_LECTURA.md

Uso (prueba desde el VPS, no imprime la clave):
  python binance_account.py --env-file .env
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

log = logging.getLogger("binance_account")

API_URL = "https://api.binance.com"
USER_AGENT = "Ingresosbot-readonly/1.0"
RECV_WINDOW_MS = 10_000
CACHE_TTL_S = 30.0
DUST_USD = 1.0

# Las únicas llamadas que este módulo puede hacer. Ninguna crea órdenes, mueve fondos ni retira.
# get-funding-asset es una consulta de saldo; Binance la definió como POST, pero solo lee.
READ_ONLY_CALLS = frozenset({
    ("GET", "/api/v3/time"),                                # hora del servidor (para firmar)
    ("GET", "/api/v3/ticker/price"),                        # precios públicos, para valorar en USD
    ("GET", "/sapi/v1/account/apiRestrictions"),            # qué permisos tiene la llave
    ("GET", "/api/v3/account"),                             # saldo Spot
    ("GET", "/sapi/v1/simple-earn/flexible/position"),      # Earn flexible
    ("GET", "/sapi/v1/simple-earn/locked/position"),        # Earn bloqueado
    ("POST", "/sapi/v1/asset/get-funding-asset"),           # billetera Funding (P2P, depósitos)
})
SIGNED = frozenset(p for _, p in READ_ONLY_CALLS) - {"/api/v3/time", "/api/v3/ticker/price"}

# Permisos que una llave de solo lectura NO debe tener. Si Binance dice que alguno está activo, no se usa.
DANGEROUS_PERMISSIONS = {
    "enableSpotAndMarginTrading": "operar en Spot y Margin",
    "enableWithdrawals": "retirar fondos",
    "enableInternalTransfer": "transferir entre cuentas",
    "permitsUniversalTransfer": "transferencias universales",
    "enableMargin": "Margin",
    "enableFutures": "Futuros",
    "enableVanillaOptions": "Opciones",
    "enablePortfolioMarginTrading": "Portfolio Margin",
    "enableFixApiTrade": "operar por FIX API",
}
STABLES = ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USDP", "DAI")
WALLETS = ("spot", "earn", "funding")

HttpFn = Callable[[str, str, dict, float], tuple[int, object]]


class BinanceError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status, self.code = status, code


def _http(method: str, url: str, headers: dict, timeout: float) -> tuple[int, object]:
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None,
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"msg": raw[:200]}


def explain_error(status: int | None, code: int | None, msg: str) -> str:
    """Mensaje claro en español para los errores de Binance que más se ven al conectar una llave."""
    if status == 451:
        return "Binance bloquea las consultas desde el país de este servidor (HTTP 451)."
    if code == -2015:
        return ("Binance rechazó la llave: revisa que copiaste completa la API Key, que la llave no está borrada "
                "y que la IP del VPS está en su lista de IP permitidas.")
    if code in (-1022, -2014):
        return "La Secret Key no corresponde a la API Key: vuelve a copiarla en el .env sin espacios."
    if code == -1021:
        return "El reloj del servidor no coincide con el de Binance."
    if status == 418 or status == 429 or code == -1003:
        return "Binance pidió esperar: demasiadas consultas seguidas. Se reintenta solo en unos minutos."
    return f"Binance respondió {status}{f' (código {code})' if code is not None else ''}: {msg}"


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


class BinanceReadOnly:
    """Cliente mínimo de solo lectura. `http` se inyecta en las pruebas para no tocar la red."""

    def __init__(self, api_key: str, api_secret: str, base_url: str = API_URL,
                 http: HttpFn = _http, clock: Callable[[], float] = time.time, timeout: float = 10.0):
        if not api_key or not api_secret:
            raise ValueError("Faltan BINANCE_API_KEY y BINANCE_API_SECRET.")
        self._key, self._secret = api_key.strip(), api_secret.strip().encode()
        self.base_url = base_url.rstrip("/")
        self.http, self.clock, self.timeout = http, clock, timeout
        self._offset_ms: int | None = None

    @classmethod
    def from_env(cls, **kw) -> "BinanceReadOnly | None":
        key, secret = os.environ.get("BINANCE_API_KEY", ""), os.environ.get("BINANCE_API_SECRET", "")
        if not key.strip() or not secret.strip():
            return None
        return cls(key, secret, os.environ.get("BINANCE_API_URL", "").strip() or API_URL, **kw)

    @property
    def key_hint(self) -> str:
        """Últimos 4 caracteres de la API Key (la parte pública), para reconocer qué llave está puesta."""
        return "···" + self._key[-4:]

    def _now_ms(self) -> int:
        return int(self.clock() * 1000) + (self._offset_ms or 0)

    def _sync_time(self) -> None:
        local = int(self.clock() * 1000)
        server = self._call("GET", "/api/v3/time")
        self._offset_ms = int(server["serverTime"]) - local if isinstance(server, dict) and "serverTime" in server else 0

    def _call(self, method: str, path: str, params: dict | None = None, _retry: bool = True):
        if (method, path) not in READ_ONLY_CALLS:
            raise PermissionError(f"{method} {path} no está permitido: este módulo solo lee.")
        params = dict(params or {})
        headers = {}
        if path in SIGNED:
            if self._offset_ms is None:
                self._sync_time()
            params |= {"recvWindow": RECV_WINDOW_MS, "timestamp": self._now_ms()}
            query = urllib.parse.urlencode(params)
            sig = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
            query += "&signature=" + sig
            headers["X-MBX-APIKEY"] = self._key
        else:
            query = urllib.parse.urlencode(params)
        url = self.base_url + path + ("?" + query if query else "")
        try:
            status, data = self.http(method, url, headers, self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BinanceError(f"No se pudo conectar con Binance: {getattr(exc, 'reason', exc)}") from None
        if status == 200:
            return data
        code = data.get("code") if isinstance(data, dict) else None
        msg = str(data.get("msg", "")) if isinstance(data, dict) else str(data)[:200]
        if code == -1021 and _retry and path in SIGNED:      # reloj desfasado: sincroniza y reintenta una vez
            self._offset_ms = None
            return self._call(method, path, params={k: v for k, v in params.items()
                                                    if k not in ("timestamp", "recvWindow")}, _retry=False)
        raise BinanceError(explain_error(status, code, msg), status, code)

    # ── lecturas ──
    def permissions(self) -> dict:
        r = self._call("GET", "/sapi/v1/account/apiRestrictions")
        risky = [label for k, label in DANGEROUS_PERMISSIONS.items() if r.get(k)]
        return {"reading": bool(r.get("enableReading")), "ip_restricted": bool(r.get("ipRestrict")),
                "risky": risky, "safe": bool(r.get("enableReading")) and not risky}

    def prices(self) -> dict[str, float]:
        return {row["symbol"]: _f(row["price"]) for row in self._call("GET", "/api/v3/ticker/price")
                if isinstance(row, dict) and "symbol" in row}

    def spot(self) -> list[tuple[str, float, float]]:
        r = self._call("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        return [(b["asset"], _f(b.get("free")), _f(b.get("locked"))) for b in r.get("balances", [])]

    def earn(self) -> list[tuple[str, float]]:
        out = []
        for path, field in (("/sapi/v1/simple-earn/flexible/position", "totalAmount"),
                            ("/sapi/v1/simple-earn/locked/position", "amount")):
            r = self._call("GET", path, {"size": 100})
            out += [(row["asset"], _f(row.get(field))) for row in (r or {}).get("rows", [])]
        return out

    def funding(self) -> list[tuple[str, float, float]]:
        r = self._call("POST", "/sapi/v1/asset/get-funding-asset")
        return [(row["asset"], _f(row.get("free")), _f(row.get("locked")) + _f(row.get("freeze"))
                 + _f(row.get("withdrawing"))) for row in (r or [])]


def price_usd(asset: str, prices: dict[str, float]) -> float | None:
    if asset == "USDT":
        return 1.0
    if asset + "USDT" in prices:
        return prices[asset + "USDT"]
    if asset in STABLES:
        return 1.0
    if asset + "BTC" in prices and "BTCUSDT" in prices:
        return prices[asset + "BTC"] * prices["BTCUSDT"]
    if prices.get("USDT" + asset):                              # monedas fiat: USDTBRL, USDTARS…
        return 1.0 / prices["USDT" + asset]
    return None


def _earn_token(asset: str, prices: dict[str, float]) -> str | None:
    """Binance muestra Earn flexible en Spot como LD<moneda> (LDUSDT). Ojo: LDO es una moneda real."""
    if asset.startswith("LD") and len(asset) > 2 and price_usd(asset, prices) is None:
        base = asset[2:]
        return base if price_usd(base, prices) is not None else None
    return None


def build_snapshot(client: BinanceReadOnly, now: Callable[[], float] = time.time) -> dict:
    """Todo lo que muestra el panel. Si la llave tiene permisos de más, no lee el saldo."""
    out: dict = {"configured": True, "ok": False, "key_hint": client.key_hint,
                 "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())), "notes": []}
    perms = client.permissions()
    out["key"] = perms
    if perms["risky"]:
        out["blocked"] = True
        out["message"] = ("Por seguridad no se usa esta llave: tiene permiso para " + ", ".join(perms["risky"])
                          + ". Crea una llave nueva con solo «Enable Reading» y pon esa en el .env.")
        return out
    if not perms["reading"]:
        out["message"] = "La llave no tiene activado «Enable Reading»: actívalo en Binance o crea una nueva."
        return out

    prices = client.prices()
    holdings: dict[str, dict[str, list[float]]] = {}      # asset -> wallet -> [libre, bloqueado]

    def add(asset: str, wallet: str, free: float, locked: float = 0.0) -> None:
        if free or locked:
            slot = holdings.setdefault(asset, {}).setdefault(wallet, [0.0, 0.0])
            slot[0] += free
            slot[1] += locked

    try:
        earn = client.earn()
    except BinanceError as exc:
        earn = None
        out["notes"].append("Earn: " + str(exc))
    for asset, free, locked in client.spot():
        base = _earn_token(asset, prices)
        if base is None:
            add(asset, "spot", free, locked)
        elif earn is None:                                   # sin el detalle de Earn, usa el LD<moneda> de Spot
            add(base, "earn", free + locked)
    for asset, amount in earn or []:
        add(asset, "earn", amount)
    try:
        for asset, free, locked in client.funding():
            add(asset, "funding", free, locked)
    except BinanceError as exc:
        out["notes"].append("Funding: " + str(exc))

    rows, by_wallet, unpriced = [], dict.fromkeys(WALLETS, 0.0), []
    for asset, wallets in holdings.items():
        px = price_usd(asset, prices)
        amount = sum(f + lk for f, lk in wallets.values())
        row = {"asset": asset, "amount": amount, "price_usd": px,
               "value_usd": None if px is None else amount * px,
               "wallets": {w: {"free": f, "locked": lk} for w, (f, lk) in wallets.items()}}
        if px is None:
            unpriced.append(asset)
        else:
            for w, (f, lk) in wallets.items():
                by_wallet[w] += (f + lk) * px
        rows.append(row)
    total = sum(by_wallet.values())
    for row in rows:
        v = row["value_usd"]
        row["share_pct"] = None if v is None or total <= 0 else round(100 * v / total, 2)
        row["dust"] = v is not None and v < DUST_USD
    rows.sort(key=lambda r: (r["value_usd"] is None, -(r["value_usd"] or 0), r["asset"]))
    if unpriced:
        out["notes"].append("Sin precio en USDT (no suman al total): " + ", ".join(sorted(unpriced)))
    if not perms["ip_restricted"]:
        out["notes"].append("La llave no está restringida a la IP del VPS: en Binance, edítala y deja solo esa IP.")
    out.update(ok=True, total_usd=round(total, 2), wallets={w: round(v, 2) for w, v in by_wallet.items()},
               assets=rows, btc_usd=prices.get("BTCUSDT"))
    return out


class AccountView:
    """Lo que consulta el panel: guarda la última lectura CACHE_TTL_S segundos para no pedir a Binance en cada visita."""

    def __init__(self, client: BinanceReadOnly | None, clock: Callable[[], float] = time.time,
                 ttl_s: float = CACHE_TTL_S):
        self.client, self.clock, self.ttl_s = client, clock, ttl_s
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None

    @classmethod
    def from_env(cls) -> "AccountView":
        return cls(BinanceReadOnly.from_env())

    def snapshot(self) -> dict:
        if self.client is None:
            return {"configured": False, "ok": False,
                    "message": "Aún no hay llave de Binance en el .env (BINANCE_API_KEY y BINANCE_API_SECRET)."}
        with self._lock:
            now = self.clock()
            if self._cache and now - self._cache[0] < self.ttl_s:
                return self._cache[1]
            try:
                snap = build_snapshot(self.client, self.clock)
            except BinanceError as exc:
                log.warning("Binance (solo lectura): %s", exc)
                snap = {"configured": True, "ok": False, "key_hint": self.client.key_hint, "message": str(exc)}
            self._cache = (now, snap)
            return snap


def main(argv: list[str] | None = None) -> int:
    from notifier import load_env_file

    ap = argparse.ArgumentParser(description="Muestra el saldo de tu cuenta de Binance (solo lectura).")
    ap.add_argument("--env-file", default=".env")
    args = ap.parse_args(argv)
    load_env_file(args.env_file)
    snap = AccountView.from_env().snapshot()
    if not snap.get("ok"):
        print("No se pudo leer la cuenta:", snap.get("message"))
        for n in snap.get("notes", []):
            print(" -", n)
        return 1
    k = snap["key"]
    print(f"Llave {snap['key_hint']}: solo lectura OK · IP restringida: {'sí' if k['ip_restricted'] else 'NO'}")
    print(f"Total estimado: {snap['total_usd']:,.2f} USD  (Spot {snap['wallets']['spot']:,.2f} · "
          f"Earn {snap['wallets']['earn']:,.2f} · Funding {snap['wallets']['funding']:,.2f})")
    for r in snap["assets"]:
        if not r["dust"]:
            v = "sin precio" if r["value_usd"] is None else f"{r['value_usd']:,.2f} USD"
            print(f"  {r['asset']:<8} {r['amount']:>18,.8f}   {v}")
    for n in snap["notes"]:
        print(" -", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
