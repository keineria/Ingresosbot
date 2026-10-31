#!/usr/bin/env python3
"""
listing_lab.py — Laboratorio de nuevos listados de Binance Spot (última idea de corto plazo tras el Cazador).

Pregunta: ¿hay una forma repetible de ganar comprando monedas en sus primeros días tras listarse en Binance,
DESPUÉS de comisiones y de un deslizamiento alto (los primeros días tienen libro fino)?

Datos: todas las parejas USDT de Binance Spot (también las que hoy están en pausa o retiradas, si Binance aún
las lista en exchangeInfo) cuya primera vela es de 2023 o después; de cada una, sus primeras 1.000 velas de 1 h
(~41 días). Se excluyen stablecoins y tokens apalancados (UP/DOWN/BULL/BEAR). Ojo: algunos "listados" son
cambios de nombre de monedas viejas (p. ej. MATIC → POL); se tratan igual porque no se pueden separar solo con
precios, y las monedas borradas por completo de exchangeInfo no están (algo de sesgo de supervivencia).

Tres familias de reglas, solo compras (Spot):
  hora      comprar en la apertura de la hora H tras el listado y salir por stop, trailing o tiempo.
  caida     esperar a que el precio caiga X% desde el máximo desde el listado (dentro de la 1.ª semana),
            comprar en la vela siguiente y salir por TP, stop o tiempo.
  ruptura   desde el día 2, comprar cuando el cierre supera el máximo de las primeras 24 h; stop, trailing
            o tiempo.
Opcional en cada una: solo si el cierre diario de ayer de BTC está sobre su SMA 200.

Costos: 0,08% de comisión por orden; deslizamiento 0,5% por lado en las primeras 24 h y 0,2% después.

Cuenta: 500 USD, cada operación usa el 10% del saldo, máx. 3 a la vez. Capa 1: 3 pérdidas seguidas →
15 h sin entrar; caída del día ≥ 3% → nada más ese día; caída total ≥ 20% → se apaga.

Método: rejilla pequeña elegida SOLO con la primera mitad de los listados (por fecha); las 5 mejores se miden en
la segunda mitad, en el total, en 20 sorteos de la mitad de las monedas y quitando sus 3 mejores operaciones.
Pasa si: total > 0, validación > 0, ≥ 15 de 20 sorteos > 0, sigue > 0 sin sus 3 mejores operaciones,
caída máx. < 20% y ≥ 40 operaciones.

Uso (en el VPS, como usuario ingresosbot, desde /opt/Ingresosbot):
  python listing_lab.py download --csv-dir data/listados/velas --since 2023-01-01   # ~10-15 min
  python listing_lab.py run --csv-dir data/listados/velas --out-dir data/listados/informes
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

import quant_core_engine as q

FEE = 0.0008
SLIP_EARLY = 0.005            # primeras 24 h: libro fino
SLIP_LATE = 0.002
ALLOC = 0.10                  # 10% del saldo por operación
MAX_OPEN = 3
MIN_NOTIONAL = 5.0
STABLES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "EUR", "EURI", "AEUR", "USDE", "PYUSD", "XUSD",
           "USD1", "BFUSD", "RLUSD", "USDS", "GBP", "TRY", "BRL"}
LEVERAGED = ("UP", "DOWN", "BULL", "BEAR")
# Tickers nuevos de activos viejos (ETH/SOL en staking, oro tokenizado, redenominaciones): no son monedas nuevas.
NOT_NEW = {"BETH", "WBETH", "BNSOL", "XAUT", "PAXG", "1MBABYDOGE"}
# Binance bStocks (acciones y ETF tokenizados, desde junio de 2026): NVDAB, TSLAB, SPYB… No son criptos nuevas.
BSTOCKS_FROM = pd.Timestamp("2026-06-01", tz="UTC")


def is_tokenized_stock(symbol: str, listed: pd.Timestamp) -> bool:
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return len(base) >= 3 and base.endswith("B") and listed >= BSTOCKS_FROM


@dataclass(frozen=True)
class Rule:
    kind: str = "hora"          # hora | caida | ruptura
    h: int = 1                  # hora: hora de entrada tras el listado
    dip: float = 0.3            # caida: caída desde el máximo para entrar
    tp: float = 0.0             # 0 = sin TP
    sl: float = 0.25            # stop desde la entrada
    trail: float = 0.0          # 0 = sin trailing; si no, stop a X% del máximo desde la entrada
    hold: int = 72              # horas máximas en la operación
    window: int = 168           # caida / ruptura: horas desde el listado en que se acepta la entrada
    btc_filter: bool = False

    def label(self) -> str:
        if self.kind == "hora":
            head = f"comprar en la hora {self.h}"
        elif self.kind == "caida":
            head = f"comprar tras caer {self.dip:.0%} del máximo (1.ª semana)"
        else:
            head = f"ruptura del máximo del día 1 (hasta día {self.window // 24})"
        parts = [head, f"SL {self.sl:.0%}"]
        if self.tp:
            parts.append(f"TP {self.tp:.0%}")
        if self.trail:
            parts.append(f"trail {self.trail:.0%}")
        parts.append(f"máx {self.hold} h")
        if self.btc_filter:
            parts.append("solo BTC > SMA200")
        return " · ".join(parts)


# ════════════════════════════════════════════════════════════════════════════
# DATOS
# ════════════════════════════════════════════════════════════════════════════
def usdt_symbols(info: dict) -> list[tuple[str, str]]:
    out = []
    for s in info.get("symbols", []):
        base = s.get("baseAsset", "")
        if s.get("quoteAsset") != "USDT" or base in STABLES or base in NOT_NEW or base.endswith(LEVERAGED):
            continue
        out.append((s["symbol"], s.get("status", "")))
    return out


def download(csv_dir: str | Path, since: str = "2023-01-01", pause: float = 0.12) -> list[str]:
    """Primeras 1.000 velas de 1 h de cada pareja USDT listada desde `since`, más BTC diario.
    Guarda un índice (indice.csv) para no volver a preguntar por monedas viejas en la próxima corrida."""
    import market_fetcher as mf
    d = Path(csv_dir)
    d.mkdir(parents=True, exist_ok=True)
    since_ms = int(pd.Timestamp(since, tz="UTC").timestamp() * 1000)

    def get(path: str):
        last = None
        for host in mf.BINANCE_HOSTS:
            try:
                return mf._http_get_json(f"{host}{path}", timeout=30)
            except Exception as exc:              # 451 en algunos países: se prueba el otro host
                last = exc
        raise RuntimeError(f"ningún host respondió: {last}")

    idx_path = d / "indice.csv"
    idx = pd.read_csv(idx_path) if idx_path.exists() else pd.DataFrame(columns=["symbol", "status", "first_ms"])
    known = dict(zip(idx.symbol, idx.first_ms))
    rows, out, now_ms = [], [], int(time.time() * 1000)
    for sym, status in usdt_symbols(get("/api/v3/exchangeInfo")):
        first = known.get(sym)
        path = d / f"{sym}_1h.csv"
        if first is not None and (first < since_ms or (path.exists() and now_ms - first > 1000 * 3_600_000)):
            rows.append({"symbol": sym, "status": status, "first_ms": first})
            continue
        qs = urllib.parse.urlencode({"symbol": sym, "interval": "1h", "startTime": 0, "limit": 1000})
        try:
            data = get(f"/api/v3/klines?{qs}")
        except RuntimeError as exc:
            out.append(f"{sym}: NO descargada ({exc})")
            continue
        time.sleep(pause)
        if not isinstance(data, list) or not data:
            continue
        first = int(data[0][0])
        rows.append({"symbol": sym, "status": status, "first_ms": first})
        if first >= since_ms:
            mf._to_frame([list(r[:7]) for r in data], now_ms).to_csv(path, index=False)
            out.append(f"{sym}: listada {pd.Timestamp(first, unit='ms', tz='UTC'):%Y-%m-%d} ({status})")
    pd.DataFrame(rows).to_csv(idx_path, index=False)
    btc = get("/api/v3/klines?" + urllib.parse.urlencode({"symbol": "BTCUSDT", "interval": "1d", "limit": 1000}))
    rows_btc = [list(r[:7]) for r in btc]
    if len(rows_btc) == 1000:                    # 1.000 días no alcanzan para SMA200 desde 2023: se pide más
        older = get("/api/v3/klines?" + urllib.parse.urlencode(
            {"symbol": "BTCUSDT", "interval": "1d", "limit": 1000, "endTime": int(btc[0][0]) - 1}))
        rows_btc = [list(r[:7]) for r in older] + rows_btc
    mf._to_frame(rows_btc, now_ms).to_csv(d / "BTCUSDT_1d.csv", index=False)
    out.append(f"{sum(1 for r in rows if r['first_ms'] >= since_ms)} listados desde {since}")
    print("\n".join(out[-5:]), file=sys.stderr)
    return out


def load(csv_dir: str | Path, keep_stocks: bool = False, excluded: list | None = None
         ) -> tuple[dict[str, pd.DataFrame], pd.Series | None]:
    d = Path(csv_dir)
    data = {p.name[:-7]: q.load_csv(p) for p in sorted(d.glob("*_1h.csv"))}
    data = {s: df for s, df in data.items() if len(df) >= 48 and s[:-4] not in NOT_NEW}
    if not keep_stocks:
        stocks = [s for s, df in data.items() if is_tokenized_stock(s, df.time.iloc[0])]
        for s in stocks:
            del data[s]
        if excluded is not None:
            excluded.extend(stocks)
    if not data:
        raise SystemExit(f"No hay velas en {csv_dir}. Corre primero: python listing_lab.py download")
    btc_ok = None
    if (d / "BTCUSDT_1d.csv").exists():
        daily = q.load_csv(d / "BTCUSDT_1d.csv").set_index("time").close
        ok = daily > daily.rolling(200).mean()
        ok.index = ok.index + pd.Timedelta(days=1)          # el cierre de ayer se conoce hoy
        btc_ok = ok
    return data, btc_ok


# ════════════════════════════════════════════════════════════════════════════
# UNA OPERACIÓN POR LISTADO
# ════════════════════════════════════════════════════════════════════════════
def trade(df: pd.DataFrame, r: Rule, btc_ok: pd.Series | None = None) -> dict | None:
    """Simula la regla sobre las velas de un listado (fila 0 = primera hora). None si no hay entrada."""
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(df)
    e = None
    if r.kind == "hora":
        e = r.h if r.h < n else None
    elif r.kind == "caida":
        peak = np.maximum.accumulate(h)
        hit = np.flatnonzero(c[: min(r.window, n - 1)] <= peak[: min(r.window, n - 1)] * (1 - r.dip))
        e = int(hit[0]) + 1 if len(hit) else None
    elif r.kind == "ruptura":
        if n > 25:
            day1 = h[:24].max()
            hit = np.flatnonzero(c[24: min(r.window, n - 1)] > day1)
            e = int(hit[0]) + 25 if len(hit) else None
    if e is None or e >= n:
        return None
    t_in = df.time.iloc[e]
    if r.btc_filter and btc_ok is not None:
        ok = btc_ok.asof(t_in.floor("D")) if len(btc_ok) else False
        if pd.isna(ok) or not bool(ok):
            return None
    slip_in = SLIP_EARLY if e < 24 else SLIP_LATE
    entry = o[e]
    if not entry > 0:
        return None
    sl = entry * (1 - r.sl)
    tp = entry * (1 + r.tp) if r.tp else np.inf
    top, px, why, j = entry, None, None, e
    last = min(n - 1, e + r.hold)
    for j in range(e, last + 1):
        stop = max(sl, top * (1 - r.trail)) if r.trail else sl
        if o[j] <= stop and j > e:
            px, why = o[j], "SL" if stop == sl else "TRAIL"
        elif l[j] <= stop:
            px, why = stop, "SL" if stop == sl else "TRAIL"
        elif h[j] >= tp:
            px, why = (o[j] if o[j] >= tp and j > e else tp), "TP"
        elif j == last:
            px, why = c[j], "TIEMPO" if j == e + r.hold else "FIN"
        if px is not None:
            break
        top = max(top, h[j])
    slip_out = SLIP_EARLY if j < 24 else SLIP_LATE
    ret = (px * (1 - slip_out)) / (entry * (1 + slip_in)) * (1 - FEE) ** 2 - 1
    return {"t_in": t_in, "t_out": df.time.iloc[j], "ret": ret, "reason": why, "entry_bar": e}


def events(data: dict[str, pd.DataFrame], r: Rule, btc_ok=None, symbols=None) -> pd.DataFrame:
    rows = []
    for s in (symbols if symbols is not None else data):
        x = trade(data[s], r, btc_ok)
        if x:
            rows.append({"symbol": s, "listed": data[s].time.iloc[0], **x})
    return pd.DataFrame(rows).sort_values("t_in").reset_index(drop=True) if rows else pd.DataFrame(
        columns=["symbol", "listed", "t_in", "t_out", "ret", "reason", "entry_bar"])


# ════════════════════════════════════════════════════════════════════════════
# CUENTA
# ════════════════════════════════════════════════════════════════════════════
def account(ev: pd.DataFrame, balance: float = 500.0, kill_dd: float = 20.0) -> dict:
    """Recorre las operaciones por hora de entrada: 10% del saldo cada una, máx. 3 abiertas, Capa 1 y apagado."""
    bal = peak = start = balance
    open_: list[tuple] = []                       # (t_out, notional, ret)
    pnls, max_dd, losses = [], 0.0, 0
    pause_until, day, day_peak, day_halt, killed = None, None, bal, False, None

    def close_until(t):
        nonlocal bal, peak, max_dd, losses, pause_until, day_halt, day_peak, killed
        open_.sort(key=lambda x: x[0])
        while open_ and (t is None or open_[0][0] <= t):
            t_out, notional, ret = open_.pop(0)
            pnl = notional * ret
            bal += pnl
            pnls.append(pnl)
            peak = max(peak, bal)
            max_dd = max(max_dd, (peak - bal) / peak * 100)
            losses = 0 if pnl > 0 else losses + 1
            if losses >= 3:
                pause_until, losses = t_out + pd.Timedelta(hours=15), 0
            if day is not None and t_out.floor("D") == day and (day_peak - bal) / day_peak * 100 >= 3:
                day_halt = True
            if killed is None and (peak - bal) / peak * 100 >= kill_dd:
                killed = t_out

    for row in ev.itertuples(index=False):
        close_until(row.t_in)
        if killed is not None:
            break
        d = row.t_in.floor("D")
        if d != day:
            day, day_peak, day_halt = d, bal, False
        day_peak = max(day_peak, bal)
        if day_halt or (pause_until is not None and row.t_in < pause_until) or len(open_) >= MAX_OPEN:
            continue
        notional = bal * ALLOC
        if notional < MIN_NOTIONAL:
            continue
        open_.append((row.t_out, notional, row.ret))
    close_until(None)
    p = np.array(pnls, float)
    wins, loss = p[p > 0].sum(), -p[p < 0].sum()
    return {"return_pct": (bal / start - 1) * 100, "max_dd_pct": max_dd, "trades": len(p),
            "win_rate_pct": float((p > 0).mean() * 100) if len(p) else 0.0,
            "profit_factor": float(wins / loss) if loss > 0 else (float("inf") if wins > 0 else 0.0),
            "killed_at": "" if killed is None else str(killed)[:10]}


def without_top(ev: pd.DataFrame, k: int = 3) -> pd.DataFrame:
    return ev.drop(ev.ret.nlargest(k).index) if len(ev) > k else ev.iloc[0:0]


# ════════════════════════════════════════════════════════════════════════════
# REJILLA, VALIDACIÓN E INFORME
# ════════════════════════════════════════════════════════════════════════════
def grid() -> list[Rule]:
    out = []
    for btc in (False, True):
        for h, hold, sl, tr in itertools.product((1, 4, 24, 72), (24, 72, 168, 336), (0.15, 0.30), (0.0, 0.25)):
            out.append(Rule("hora", h=h, hold=hold, sl=sl, trail=tr, btc_filter=btc))
        for dip, tp, hold, sl in itertools.product((0.2, 0.35, 0.5), (0.15, 0.3), (72, 168), (0.15, 0.3)):
            out.append(Rule("caida", dip=dip, tp=tp, hold=hold, sl=sl, btc_filter=btc))
        for win, tr, hold, sl in itertools.product((168, 336), (0.15, 0.25), (168, 336), (0.15, 0.25)):
            out.append(Rule("ruptura", window=win, trail=tr, hold=hold, sl=sl, btc_filter=btc))
    return out


def score(r: dict) -> float:
    if r["trades"] < 20:
        return -1e9
    return r["return_pct"] / max(r["max_dd_pct"], 5.0)


def evaluate(data, btc_ok, balance: float = 500.0, top: int = 5, rules=None) -> dict:
    listed = pd.Series({s: df.time.iloc[0] for s, df in data.items()}).sort_values()
    split = listed.iloc[len(listed) // 2]
    train_syms = list(listed[listed < split].index)
    valid_syms = list(listed[listed >= split].index)
    rules = rules or grid()
    cache = {r: events(data, r, btc_ok) for r in rules}
    train = []
    for r in rules:
        ev = cache[r]
        train.append((r, account(ev[ev.symbol.isin(train_syms)], balance)))
    train.sort(key=lambda x: -score(x[1]))
    rng = np.random.default_rng(11)
    draws = [set(rng.choice(list(data), max(1, len(data) // 2), replace=False)) for _ in range(20)]
    finals = []
    for r, tr in train[:top]:
        ev = cache[r]
        full = account(ev, balance)
        valid = account(ev[ev.symbol.isin(valid_syms)], balance)
        no_top = account(without_top(ev), balance)
        groups = [account(ev[ev.symbol.isin(dr)], balance)["return_pct"] for dr in draws]
        pos = sum(g > 0 for g in groups)
        passed = (full["return_pct"] > 0 and valid["return_pct"] > 0 and pos >= 15 and no_top["return_pct"] > 0
                  and full["max_dd_pct"] < 20 and full["trades"] >= 40)
        finals.append({"rule": r, "train": tr, "valid": valid, "full": full, "no_top": no_top,
                       "groups_pos": pos, "median_ret_pct": float(ev.ret.median() * 100) if len(ev) else 0.0,
                       "passed": passed})
    hold_all = events(data, Rule("hora", h=1, hold=24 * 30, sl=0.99), None)
    return {"n": len(data), "split": split, "first": listed.iloc[0], "last": listed.iloc[-1], "train_all": train,
            "finals": finals, "cache": cache, "hold30_median": float(hold_all.ret.median() * 100),
            "hold30_pos": float((hold_all.ret > 0).mean() * 100), "hold30_n": len(hold_all)}


def report_md(res: dict, balance: float, excluded: list | None = None) -> str:
    L = ["# Laboratorio de nuevos listados de Binance", ""]
    if excluded:
        L += [f"Se excluyeron {len(excluded)} acciones o ETF tokenizados de Binance (bStocks, desde junio de 2026), "
              f"p. ej. {', '.join(sorted(excluded)[:8])}. No son criptos nuevas.", ""]
    L += [f"{res['n']} monedas USDT listadas entre {res['first']:%Y-%m-%d} y {res['last']:%Y-%m-%d}, con sus primeros "
          f"~41 días en velas de 1 h. Cuenta de {balance:,.0f} USD, 10% del saldo por operación, máx. 3 a la vez, "
          "Capa 1 y apagado al -20%. Comisión 0,08% por orden; deslizamiento 0,5% por lado en las primeras 24 h y "
          "0,2% después.", "",
          f"Referencia: comprar en la 2.ª hora y aguantar 30 días dio una mediana de {res['hold30_median']:+.1f}% por "
          f"moneda, y solo el {res['hold30_pos']:.0f}% de {res['hold30_n']} terminó en positivo.", "",
          f"Elección con los listados anteriores al {res['split']:%Y-%m-%d}; validación con los posteriores. Pasa si: "
          "total > 0, validación > 0, ≥ 15 de 20 sorteos de la mitad de las monedas > 0, sigue > 0 sin sus 3 mejores "
          "operaciones, caída máx. < 20% y ≥ 40 operaciones.", "",
          "| Regla | Entren. % | Valid. % | Total % | Sin 3 mejores % | Caída máx. % | Ops | Acierto % | PF | "
          "Mediana por op. % | Sorteos > 0 | ¿Pasa? |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for f in res["finals"]:
        fu = f["full"]
        off = f" (apagado {fu['killed_at']})" if fu["killed_at"] else ""
        L.append(f"| {f['rule'].label()} | {f['train']['return_pct']:+.1f} | {f['valid']['return_pct']:+.1f} | "
                 f"{fu['return_pct']:+.1f}{off} | {f['no_top']['return_pct']:+.1f} | {fu['max_dd_pct']:.1f} | "
                 f"{fu['trades']} | {fu['win_rate_pct']:.0f} | {fu['profit_factor']:.2f} | "
                 f"{f['median_ret_pct']:+.1f} | {f['groups_pos']}/20 | {'SÍ' if f['passed'] else 'no'} |")
    pos_train = sum(x[1]["return_pct"] > 0 for x in res["train_all"])
    by_kind = {}
    for r, m in res["train_all"]:
        by_kind.setdefault(r.kind, []).append(m["return_pct"] > 0)
    kinds = ", ".join(f"{k}: {sum(v)} de {len(v)}" for k, v in by_kind.items())
    any_pass = any(f["passed"] for f in res["finals"])
    L += ["", f"En entrenamiento, {pos_train} de {len(res['train_all'])} reglas terminaron en positivo ({kinds}).",
          "", "## Veredicto", "",
          "Nota: operaciones_mejor.csv lista todas las señales de la mejor regla, también las que la cuenta no tomó "
          "por el tope de 3 posiciones, la Capa 1 o el apagado.", "",
          ("Al menos una regla pasa todas las condiciones: se puede probar en papel con ella."
           if any_pass else
           "Ninguna regla pasa todas las condiciones: comprar nuevos listados no se convierte en bot. "
           "Se descarta igual que el Cazador, la compra de caídas y la rotación por momentum."), ""]
    return "\n".join(L)


def run(csv_dir: str, out_dir: str, balance: float, keep_stocks: bool = False) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    excluded: list[str] = []
    data, btc_ok = load(csv_dir, keep_stocks, excluded)
    print(f"[run] {len(data)} listados", file=sys.stderr)
    res = evaluate(data, btc_ok, balance)
    rows = [{"label": r.label(), **asdict(r), **m} for r, m in res["train_all"]]
    pd.DataFrame(rows).to_csv(Path(out_dir) / "entrenamiento.csv", index=False)
    if res["finals"]:
        res["cache"][res["finals"][0]["rule"]].to_csv(Path(out_dir) / "operaciones_mejor.csv", index=False)
    md = report_md(res, balance, excluded)
    (Path(out_dir) / "informe_listados.md").write_text(md, encoding="utf-8")
    best = [f for f in res["finals"] if f["passed"]]
    if best:
        (Path(out_dir) / "listados_params_candidato.json").write_text(json.dumps(asdict(best[0]["rule"]), indent=2),
                                                                      encoding="utf-8")
    return md


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Laboratorio de nuevos listados de Binance Spot.")
    ap.add_argument("mode", choices=("download", "run"))
    ap.add_argument("--csv-dir", default="data/listados/velas")
    ap.add_argument("--out-dir", default="data/listados/informes")
    ap.add_argument("--since", default="2023-01-01")
    ap.add_argument("--balance", type=float, default=500.0)
    ap.add_argument("--keep-stocks", action="store_true", help="no excluir las acciones tokenizadas (bStocks)")
    a = ap.parse_args(argv)
    if a.mode == "download":
        print("\n".join(download(a.csv_dir, a.since)))
    else:
        print(run(a.csv_dir, a.out_dir, a.balance, a.keep_stocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
