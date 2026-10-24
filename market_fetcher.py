#!/usr/bin/env python3
"""
market_fetcher.py — Velas OHLCV públicas sin API key (solo urllib de la librería estándar).

Orden de fuentes (la primera que responda gana):
  1. Binance        https://api.binance.com/api/v3/klines
  2. Binance mirror https://data-api.binance.vision/api/v3/klines   (solo datos públicos; útil si 1 está bloqueado)
  3. Bybit V5       https://api.bybit.com/v5/market/kline
  4. CSV local      data/candles/{SYMBOL}_{interval}.csv  (o la ruta que pases en fallback_csv)

Siempre descarta la vela que aún no ha cerrado y devuelve el DataFrame normalizado
(time, open, high, low, close, volume) que consume quant_core_engine.py.

Uso:
  python market_fetcher.py --symbol BTCUSDT --interval 1h --limit 1000 --out data/candles/BTCUSDT_1h.csv
  python market_fetcher.py --symbol SOLUSDT --interval 4h --limit 5000      # pagina automáticamente
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

import pandas as pd

import quant_core_engine as q

BINANCE_HOSTS = ("https://api.binance.com", "https://data-api.binance.vision")
BYBIT_HOST = "https://api.bybit.com"
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
               "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000}
BYBIT_INTERVAL = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120",
                  "4h": "240", "6h": "360", "12h": "720", "1d": "D"}
DEFAULT_CANDLE_DIR = Path("data/candles")
USER_AGENT = "Ingresosbot-market-fetcher/1.0"


class FetchError(RuntimeError):
    pass


def _http_get_json(url: str, timeout: float = 10.0) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _check_args(symbol: str, interval: str, limit: int) -> None:
    if interval not in INTERVAL_MS:
        raise ValueError(f"Temporalidad no soportada: {interval}. Usa una de {list(INTERVAL_MS)}")
    if limit < 1:
        raise ValueError("limit debe ser >= 1")
    if not symbol.isalnum():
        raise ValueError("symbol debe ir sin separadores, p. ej. BTCUSDT")


def fetch_binance(symbol: str, interval: str, limit: int, host: str = BINANCE_HOSTS[0],
                  get: Callable[[str], object] = _http_get_json) -> list[list]:
    """Pagina hacia atrás con endTime en bloques de 1000 hasta reunir `limit` velas."""
    rows: list[list] = []
    end_time = None
    while len(rows) < limit:
        qs = {"symbol": symbol.upper(), "interval": interval, "limit": min(1000, limit - len(rows))}
        if end_time is not None:
            qs["endTime"] = end_time
        data = get(f"{host}/api/v3/klines?{urllib.parse.urlencode(qs)}")
        if isinstance(data, dict):
            raise FetchError(f"Binance: {data.get('msg', data)}")
        if not data:
            break
        rows = [list(r[:7]) for r in data] + rows
        end_time = int(data[0][0]) - 1
        if len(data) < qs["limit"]:
            break
    return rows[-limit:]


def fetch_bybit(symbol: str, interval: str, limit: int, category: str = "spot",
                get: Callable[[str], object] = _http_get_json) -> list[list]:
    """Bybit devuelve de la más nueva a la más vieja; se invierte y se pagina con `end`."""
    rows: list[list] = []
    end = None
    step = INTERVAL_MS[interval]
    while len(rows) < limit:
        qs = {"category": category, "symbol": symbol.upper(), "interval": BYBIT_INTERVAL[interval],
              "limit": min(1000, limit - len(rows))}
        if end is not None:
            qs["end"] = end
        data = get(f"{BYBIT_HOST}/v5/market/kline?{urllib.parse.urlencode(qs)}")
        if not isinstance(data, dict) or data.get("retCode") != 0:
            raise FetchError(f"Bybit: {data.get('retMsg') if isinstance(data, dict) else data}")
        chunk = data["result"]["list"]
        if not chunk:
            break
        chunk = sorted(chunk, key=lambda r: int(r[0]))
        # close_time aproximado = start + intervalo - 1 (mismo formato que Binance)
        rows = [[int(r[0]), r[1], r[2], r[3], r[4], r[5], int(r[0]) + step - 1] for r in chunk] + rows
        end = int(chunk[0][0]) - 1
        if len(chunk) < qs["limit"]:
            break
    return rows[-limit:]


def _to_frame(rows: list[list], now_ms: int) -> pd.DataFrame:
    closed = [r for r in rows if int(r[6]) < now_ms]          # fuera la vela en formación
    seen, uniq = set(), []
    for r in closed:
        if int(r[0]) not in seen:
            seen.add(int(r[0]))
            uniq.append(r)
    df = pd.DataFrame([r[:6] for r in uniq], columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = df["time"].astype("int64")
    return q.normalize_ohlcv(df)


def default_csv_path(symbol: str, interval: str, base: Path = DEFAULT_CANDLE_DIR) -> Path:
    return base / f"{symbol.upper()}_{interval}.csv"


def fetch_candles(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 1000,
                  fallback_csv: str | Path | None = None, sources: tuple[str, ...] = ("binance", "binance_vision", "bybit"),
                  get: Callable[[str], object] = _http_get_json, now_ms: int | None = None,
                  verbose: bool = False) -> tuple[pd.DataFrame, dict]:
    """Devuelve (velas, info). info["source"] dice de dónde salieron y info["errors"] qué falló."""
    _check_args(symbol, interval, limit)
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    errors: dict[str, str] = {}
    fetchers = {
        "binance": lambda: fetch_binance(symbol, interval, limit + 1, BINANCE_HOSTS[0], get),
        "binance_vision": lambda: fetch_binance(symbol, interval, limit + 1, BINANCE_HOSTS[1], get),
        "bybit": lambda: fetch_bybit(symbol, interval, limit + 1, get=get),
    }
    for name in sources:
        try:
            rows = fetchers[name]()
            df = _to_frame(rows, now_ms).tail(limit).reset_index(drop=True)
            if len(df):
                return df, {"source": name, "candles": len(df), "errors": errors}
            errors[name] = "respuesta vacía"
        except (urllib.error.URLError, OSError, ValueError, KeyError, FetchError) as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
        if verbose:
            print(f"[market_fetcher] {name} falló: {errors[name]}", file=sys.stderr)

    path = Path(fallback_csv) if fallback_csv else default_csv_path(symbol, interval)
    if path.exists():
        df = q.load_csv(path).tail(limit).reset_index(drop=True)
        return df, {"source": f"csv:{path}", "candles": len(df), "errors": errors}
    raise FetchError(f"Sin red y sin CSV local ({path}). Errores: {errors}")


def save_csv(df: pd.DataFrame, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    if "time" in out:
        out["time"] = out["time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out.to_csv(path, index=False)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Descarga velas públicas (sin API key) con fallback a CSV.")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="1h", choices=list(INTERVAL_MS))
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--fallback-csv")
    ap.add_argument("--out", help="Guarda las velas en CSV (por defecto data/candles/SYMBOL_interval.csv)")
    args = ap.parse_args(argv)
    try:
        df, info = fetch_candles(args.symbol, args.interval, args.limit, args.fallback_csv, verbose=True)
    except FetchError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    if not info["source"].startswith("csv:"):
        info["saved_to"] = str(save_csv(df, args.out or default_csv_path(args.symbol, args.interval)))
    info.update({"from": str(df["time"].iloc[0]), "to": str(df["time"].iloc[-1])})
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
