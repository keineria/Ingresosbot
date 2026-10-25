#!/usr/bin/env python3
"""
ai_lab.py — Laboratorio en sombra: el mismo motor de 3 capas corre sobre más pares, sin dinero, para que la
escalera de autonomía (ai_autonomy.py) junte pruebas más rápido.

Cada perfil de AI_LAB_PERFILES es un BotRunner normal con su propia base (data/sombra/<perfil>.db), un balance
ficticio grande y un riesgo por operación diminuto (así ninguna señal se queda fuera por capital), y la Capa 1
sin frenos por rachas (aquí no hay dinero que proteger y se quiere el resultado de TODAS las señales). Los
resultados se miden en R, que no depende del tamaño. El analista vota cada señal; el laboratorio nunca veta,
reduce ni cierra nada, así cada voto tiene su resultado limpio. No envía alertas.

Lo arranca `python ai_agents.py run` (servicio ingresosbot-ai). Para probarlo solo:
  python ai_lab.py --once                # un ciclo de vela en cada perfil y termina
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pandas as pd

from ai_agents import AgentSettings, AIGate, LabProfile, Telegram, parse_params_file
from ai_core import AIClient, AIConfig, AIStore
from bot_runner import BotRunner, RunnerConfig
from notifier import Notifier, load_env_file

log = logging.getLogger("ai_lab")

LAB_BALANCE = 100_000.0
LAB_RISK_PCT = 0.05                     # 50 USD de riesgo ficticio: ~1.000 USD por posición, caben las 20
NO_LIMIT = 10 ** 6


def lab_params(params_path: str):
    p = parse_params_file(params_path)
    return replace(p, risk_base_pct=LAB_RISK_PCT, risk_throttled_pct=LAB_RISK_PCT, throttle_losses=NO_LIMIT,
                   halt_losses=NO_LIMIT, throttle_dd_pct=1e9, halt_dd_pct=1e9)


def make_runner(lp: LabProfile, cfg: AIConfig, settings: AgentSettings,
                candle_source: Callable[[str, str, int], pd.DataFrame] | None = None,
                price_source: Callable[[str], float] | None = None,
                clock: Callable[[], float] = time.time, store: AIStore | None = None,
                client: AIClient | None = None) -> BotRunner:
    db = lp.db(settings.lab_dir)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    gate = None
    if cfg.enabled:
        store = store or AIStore(cfg.db_path)
        gate = AIGate(Path(db).stem, store, client or AIClient(cfg, store, clock=clock),
                      settings.analyst_mode, Telegram(None, None), clock, lp.interval, profile=lp.profile,
                      settings=settings, lab=True)
    else:
        gate = AIGate(Path(db).stem, lab=True)
    runner = BotRunner(RunnerConfig(list(lp.symbols), lp.interval, db, LAB_BALANCE, watch_seconds=60.0),
                       lab_params(lp.params_path), Notifier(), candle_source=candle_source,
                       price_source=price_source, clock=clock, ai_gate=gate)
    runner.notifier.label = "Laboratorio"
    return runner


class ShadowLab:
    def __init__(self, cfg: AIConfig, settings: AgentSettings, clock: Callable[[], float] = time.time, **kw):
        self.runners = [make_runner(lp, cfg, settings, clock=clock, **kw) for lp in settings.lab_profiles]
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        for r in self.runners:
            t = threading.Thread(target=r.run_forever, name=f"lab-{Path(r.cfg.db_path).stem}", daemon=True)
            t.start()
            self.threads.append(t)
            log.info("Laboratorio en sombra: %s en %s con %d pares", Path(r.cfg.db_path).stem, r.cfg.interval,
                     len(r.cfg.symbols))

    def stop(self) -> None:
        for r in self.runners:
            r.stop()
        for t in self.threads:
            t.join(timeout=30)
        for r in self.runners:
            r.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Laboratorio en sombra de los agentes de IA (sin dinero).")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--once", action="store_true", help="un ciclo de vela en cada perfil y termina")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env_file(args.env_file)
    lab = ShadowLab(AIConfig.from_env(), AgentSettings.from_env())
    if args.once:
        for r in lab.runners:
            print(Path(r.cfg.db_path).stem, r.candle_cycle())
            r.close()
        return 0
    lab.start()
    try:
        while any(t.is_alive() for t in lab.threads):
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    lab.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
