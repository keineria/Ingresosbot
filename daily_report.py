#!/usr/bin/env python3
"""
daily_report.py — Informe diario de todos los bots de deploy/bots.json (paper trading), para seguirlos con el tiempo.

Escribe un JSON por día (reports/AAAA-MM-DD.json) y un resumen legible (reports/latest.md) con, por bot:
balance, retorno, estado de la Capa 1, métricas, operaciones cerradas en las últimas 24 h, posiciones abiertas
y la última decisión de cada par. No contiene claves ni datos de la cuenta real.

Con --push-dir lo guarda en un clon del repositorio (rama bot-reports) y hace commit + push, para que Claude
lo lea desde GitHub sin entrar al VPS. Lo lanza ingresosbot-report.timer una vez al día (ver docs/ALTERNATIVAS.md).

Uso:
  python daily_report.py                                  # imprime el resumen
  python daily_report.py --out-dir reports                # guarda reports/<fecha>.json y reports/latest.md
  python daily_report.py --push-dir /opt/ingresosbot-reports/repo --ssh-key /opt/ingresosbot-reports/deploy_key
  python daily_report.py --init-repo /opt/ingresosbot-reports/repo --ssh-key ... --remote git@github.com:keineria/Ingresosbot.git
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

from bot_runner import bot_settings, load_bots
from state_manager import StateManager

REPORT_BRANCH = "bot-reports"


def bot_report(bot: dict, now: pd.Timestamp) -> dict:
    """Foto de un bot leída de su base SQLite. Si la base no existe, el bot aún no se ha encendido."""
    cfg, p = bot_settings(bot)
    head = {"id": bot["id"], "name": bot["name"], "interval": cfg.interval, "symbols": cfg.symbols,
            "strategy": p.strategy, "db": cfg.db_path}
    if not Path(cfg.db_path).exists():
        return head | {"running": False, "note": "sin base de datos: el servicio no se ha encendido"}
    since = (now - pd.Timedelta(days=1)).isoformat()
    with StateManager(cfg.db_path, cfg.initial_balance, p) as sm:
        acc = sm.account()
        metrics = sm.metrics()
        trades = [t for t in sm.trades() if str(t.get("closed_at", "")) >= since]
        positions = sm.positions()
        cycle = sm.meta_get("last_cycle")
        day = now.normalize()
        periods = {"24h": sm.pnl_since(since),
                   "week": sm.pnl_since((day - pd.Timedelta(days=day.dayofweek)).isoformat()),
                   "month": sm.pnl_since(day.replace(day=1).isoformat())}
    cycle = json.loads(cycle) if cycle else None
    if bot.get("dynamic_symbols"):          # bot de nuevos listados: los pares son los que vigila ahora
        head["symbols"] = list((cycle or {}).get("watching") or [])
    keep_pos = ("symbol", "side", "entry_price", "stop_loss", "initial_stop_loss", "position_units", "risk_usd",
                "opened_at", "trail_atr_mult")
    keep_tr = ("symbol", "side", "result", "exit_reason", "entry_price", "exit_price", "pnl_usd", "pnl_pct",
               "fees_usd", "opened_at", "closed_at")
    decisions = {}
    if cycle and isinstance(cycle.get("symbols"), dict):
        for s, d in cycle["symbols"].items():
            if isinstance(d, dict):
                decisions[s] = {k: d.get(k) for k in ("decision", "regime", "reason", "error") if d.get(k) is not None}
    init = acc["initial_balance"]
    return head | {
        "running": True,
        "status": acc["status"], "manual_halt": bool(acc["manual_halt"]),
        "consecutive_losses": acc["consecutive_losses"], "cooldown_bars_remaining": acc["cooldown_bars_remaining"],
        "balance": round(acc["balance"], 2), "initial_balance": init,
        "return_pct": round((acc["balance"] / init - 1) * 100, 2) if init else None,
        "metrics": metrics, "periods": periods,
        "trades_24h": [{k: t.get(k) for k in keep_tr} for t in trades],
        "open_positions": [{k: x.get(k) for k in keep_pos} for x in positions],
        "last_cycle_at": cycle.get("processed_at") if cycle else None,
        "last_decisions": decisions,
    }


def build_report(bots: list[dict], now: pd.Timestamp | None = None) -> dict:
    now = now or pd.Timestamp.now(tz="UTC")
    out = []
    for b in bots:
        try:
            out.append(bot_report(b, now))
        except Exception as exc:                       # un bot roto no debe tapar el informe de los demás
            out.append({"id": b.get("id"), "name": b.get("name"), "error": f"{type(exc).__name__}: {exc}"})
    return {"generated_at": now.isoformat(), "date": now.strftime("%Y-%m-%d"), "paper_trading": True, "bots": out}


def markdown(rep: dict) -> str:
    def f(v, suf=""):
        return "—" if v is None else (f"{v:+.2f}{suf}" if isinstance(v, (int, float)) else str(v))

    lines = [f"# Informe diario de los bots · {rep['date']}", "",
             f"Generado {rep['generated_at']} (UTC). Todo en paper trading: dinero simulado.", "",
             "| Bot | Estado | Balance | Retorno | Operaciones | Win rate | Últimas 24 h | Posiciones abiertas |",
             "|---|---|---|---|---|---|---|---|"]
    for b in rep["bots"]:
        if b.get("error"):
            lines.append(f"| {b.get('name')} | error: {b['error']} | | | | | | |")
        elif not b.get("running"):
            lines.append(f"| {b['name']} | sin encender | | | | | | |")
        else:
            m, p = b["metrics"], b["periods"]["24h"]
            wr = "—" if m["win_rate_pct"] is None else f"{m['win_rate_pct']:.1f}%"
            lines.append(f"| {b['name']} ({b['interval']}) | {b['status']}{' · kill switch' if b['manual_halt'] else ''}"
                         f" | {b['balance']:.2f} | {f(b['return_pct'], '%')} | {m['trades']} | "
                         f"{wr} | "
                         f"{f(p['pnl_usd'])} USD ({p['trades']} cierres) | {len(b['open_positions'])} |")
    for b in rep["bots"]:
        if not b.get("running"):
            continue
        lines += ["", f"## {b['name']}", ""]
        for t in b["trades_24h"]:
            lines.append(f"- Cerrada {t['symbol']} {t['side']} {t['result']} ({t['exit_reason']}): "
                         f"{f(t['pnl_usd'])} USD, {t['entry_price']} → {t['exit_price']}")
        for x in b["open_positions"]:
            lines.append(f"- Abierta {x['symbol']} {x['side']} desde {x['opened_at']}: entrada {x['entry_price']}, "
                         f"SL {x['stop_loss']}")
        dec = ", ".join(f"{s.replace('USDT', '')} {d.get('decision', '?')}" for s, d in b["last_decisions"].items())
        lines.append(f"- Última vela procesada: {b['last_cycle_at'] or '—'}" + (f" · {dec}" if dec else ""))
    return "\n".join(lines) + "\n"


def write_report(rep: dict, folder: str | Path) -> list[Path]:
    folder = Path(folder) / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    day = folder / f"{rep['date']}.json"
    day.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    latest = folder / "latest.md"
    latest.write_text(markdown(rep), encoding="utf-8")
    return [day, latest]


def _git(repo: Path, *args: str, ssh_key: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if ssh_key:
        known = Path(ssh_key).resolve().parent / "known_hosts"
        env["GIT_SSH_COMMAND"] = (f"ssh -i {ssh_key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
                                  f"-o UserKnownHostsFile={known}")
    return subprocess.run(["git", "-C", str(repo), *args], env=env, check=check, capture_output=True, text=True)


def init_repo(repo: str | Path, remote: str, ssh_key: str | None = None) -> None:
    """Crea (una vez) el clon local de la rama bot-reports; si la rama no existe en GitHub, la crea vacía."""
    repo = Path(repo)
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _git(repo, "init", "-q")
        _git(repo, "remote", "add", "origin", remote)
    _git(repo, "config", "user.name", "Ingresosbot VPS")
    _git(repo, "config", "user.email", "ingresosbot@localhost")
    if _git(repo, "fetch", "-q", "origin", REPORT_BRANCH, ssh_key=ssh_key, check=False).returncode == 0:
        _git(repo, "checkout", "-q", "-B", REPORT_BRANCH, "FETCH_HEAD")
    else:
        _git(repo, "checkout", "-q", "--orphan", REPORT_BRANCH)
        (repo / "README.md").write_text("# Informes diarios de los bots (paper trading)\n\n"
                                        "Los escribe daily_report.py desde el VPS. Ver reports/latest.md.\n",
                                        encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-q", "-m", "Inicio de la rama de informes")
        _git(repo, "push", "-q", "-u", "origin", REPORT_BRANCH, ssh_key=ssh_key)


def push_report(rep: dict, repo: str | Path, ssh_key: str | None = None, retries: int = 3) -> str:
    repo = Path(repo)
    _git(repo, "pull", "-q", "--rebase", "origin", REPORT_BRANCH, ssh_key=ssh_key, check=False)
    files = write_report(rep, repo)
    _git(repo, "add", *[str(f.relative_to(repo)) for f in files])
    if _git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return "sin cambios"
    _git(repo, "commit", "-q", "-m", f"Informe {rep['date']}")
    for i in range(retries):
        r = _git(repo, "push", "-q", "origin", REPORT_BRANCH, ssh_key=ssh_key, check=False)
        if r.returncode == 0:
            return "publicado"
        time.sleep(2 ** (i + 1))
    raise SystemExit(f"git push falló: {r.stderr.strip()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Informe diario de los bots (paper trading).")
    ap.add_argument("--bots-file", help="Por defecto deploy/bots.json")
    ap.add_argument("--out-dir", help="Guarda <dir>/reports/<fecha>.json y latest.md")
    ap.add_argument("--push-dir", help="Clon de la rama bot-reports: guarda, hace commit y push")
    ap.add_argument("--init-repo", help="Crea el clon de la rama bot-reports en esta carpeta y termina")
    ap.add_argument("--remote", default="git@github.com:keineria/Ingresosbot.git")
    ap.add_argument("--ssh-key", help="Deploy key con permiso de escritura (solo para --push-dir / --init-repo)")
    args = ap.parse_args(argv)

    if args.init_repo:
        init_repo(args.init_repo, args.remote, args.ssh_key)
        print(f"Listo: {args.init_repo} sigue la rama {REPORT_BRANCH}")
        return 0
    bots = load_bots(args.bots_file)
    if not bots:
        raise SystemExit("No hay bots en deploy/bots.json")
    rep = build_report(bots)
    if args.out_dir:
        print("\n".join(str(p) for p in write_report(rep, args.out_dir)))
    if args.push_dir:
        print(push_report(rep, args.push_dir, args.ssh_key))
    if not (args.out_dir or args.push_dir):
        print(markdown(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
