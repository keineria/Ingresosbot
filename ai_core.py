#!/usr/bin/env python3
"""
ai_core.py — Base común de los agentes de IA (solo librería estándar: urllib + sqlite3).

La IA de Ingresosbot ASESORA; nunca decide. Tres garantías viven en este archivo:
  1. Respuesta cerrada: cada agente pide JSON con un esquema fijo (opciones cerradas). Si la respuesta no cumple
     el esquema, se descarta y se prueba el siguiente proveedor. Ningún número de la IA llega a una orden.
  2. Cadena de respaldo: Claude (Anthropic) -> Groq -> Gemini -> sin IA. Si todos fallan, el bot sigue con
     sus reglas como si la IA no existiera.
  3. Tope de gasto: cada llamada se apunta con su costo en data/ai_state.db; al llegar a AI_MONTHLY_BUDGET_USD
     (2 USD por defecto) la IA se apaga sola hasta el mes siguiente.

Variables del .env (todas opcionales; sin AI_ENABLED=true no se llama a ninguna API):
  AI_ENABLED=true                 interruptor general
  ANTHROPIC_API_KEY=sk-ant-...    clave de console.anthropic.com
  GROQ_API_KEY / GEMINI_API_KEY   respaldos opcionales (planes gratis)
  AI_MONTHLY_BUDGET_USD=2         tope mensual en USD
  AI_MODEL_FAST=claude-haiku-5-5  vigía, coordinador
  AI_MODEL_SMART=claude-sonnet-5-5  analista y auditor
  AI_DB=data/ai_state.db
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("ai_core")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2026-10-21"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
# Modelos de Anthropic que admiten el respaldo del servidor ante un rechazo ("fallbacks": "default").
SERVER_FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}

# USD por millón de tokens (entrada, salida). Un modelo que no esté aquí se cobra como Sonnet, por prudencia.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-4-8": (5.00, 25.00),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "gemini-3.5-flash-lite": (0.30, 2.50),
}
UNKNOWN_PRICE = (2.00, 10.00)
TRUE = ("1", "true", "yes", "si", "sí", "on")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ai_decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    agent         TEXT NOT NULL,             -- vigia | analista | auditor | coordinador | estable
    bot           TEXT,                      -- trend_state, trading_state... (None = global)
    ref           TEXT,                      -- analista: SYMBOL|opened_at · auditor: id del trade
    decision      TEXT,                      -- OK / ALERTA / PAUSA_SUGERIDA / VETO / REVISAR / ...
    mode          TEXT,                      -- analista: sombra | activo
    provider      TEXT,
    model         TEXT,
    input_summary TEXT,
    output_json   TEXT,
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0,
    outcome_r     REAL,                      -- analista: R real de la operación (cuando cierra)
    resolved_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_ai_decisions_agent ON ai_decisions (agent, at);
CREATE TABLE IF NOT EXISTS ai_pauses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    sources     TEXT,
    expires_at  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('pendiente', 'activa', 'rechazada', 'cancelada')),
    decided_at  TEXT,
    decided_by  TEXT
);
CREATE TABLE IF NOT EXISTS ai_hypotheses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    bot            TEXT,
    description    TEXT NOT NULL,
    parameter      TEXT NOT NULL,
    current_value  TEXT,
    proposed_value TEXT NOT NULL,
    rationale      TEXT,
    status         TEXT NOT NULL CHECK (status IN ('propuesta', 'aprobada', 'rechazada')),
    decided_at     TEXT
);
CREATE TABLE IF NOT EXISTS ai_news_seen (
    hash     TEXT PRIMARY KEY,
    source   TEXT,
    title    TEXT,
    seen_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utc_now(clock: Callable[[], float] = time.time) -> datetime:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None or not v.strip() else v.strip().lower() in TRUE


def price_of(model: str) -> tuple[float, float]:
    return PRICES.get(model, UNKNOWN_PRICE)


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = price_of(model)
    return round((tokens_in * pin + tokens_out * pout) / 1_000_000, 6)


# ════════════════════════════════════════════════════════════════════════════
# Validación de esquemas (subconjunto de JSON Schema: object, array, string, integer, number, boolean, enum)
# ════════════════════════════════════════════════════════════════════════════
class SchemaError(ValueError):
    pass


def validate(value: Any, schema: dict, path: str = "$") -> None:
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: {value!r} no está en {schema['enum']}")
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    if types:
        ok = any(
            (tt == "object" and isinstance(value, dict)) or (tt == "array" and isinstance(value, list))
            or (tt == "string" and isinstance(value, str)) or (tt == "boolean" and isinstance(value, bool))
            or (tt == "integer" and isinstance(value, int) and not isinstance(value, bool))
            or (tt == "number" and isinstance(value, (int, float)) and not isinstance(value, bool))
            or (tt == "null" and value is None)
            for tt in types)
        if not ok:
            raise SchemaError(f"{path}: se esperaba {types}, llegó {type(value).__name__}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                raise SchemaError(f"{path}: falta '{k}'")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise SchemaError(f"{path}: campos no permitidos {sorted(extra)}")
        for k, v in value.items():
            if k in props:
                validate(v, props[k], f"{path}.{k}")
    elif isinstance(value, list) and "items" in schema:
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(f"{path}: más de {schema['maxItems']} elementos")
        for i, v in enumerate(value):
            validate(v, schema["items"], f"{path}[{i}]")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        raise SchemaError(f"{path}: texto de más de {schema['maxLength']} caracteres")


def api_schema(schema: dict) -> dict:
    """Copia del esquema sin las restricciones que la API de Anthropic no acepta (maxLength, maxItems);
    esas se validan aquí, en el cliente."""
    if isinstance(schema, dict):
        return {k: api_schema(v) for k, v in schema.items() if k not in ("maxLength", "maxItems")}
    if isinstance(schema, list):
        return [api_schema(v) for v in schema]
    return schema


def extract_json(text: str) -> Any:
    """JSON de la respuesta; tolera un bloque ```json ... ``` (los proveedores de respaldo a veces lo añaden)."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        s = s.rsplit("```", 1)[0]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        a, b = s.find("{"), s.rfind("}")
        if a >= 0 and b > a:
            return json.loads(s[a:b + 1])
        raise


# ════════════════════════════════════════════════════════════════════════════
# Proveedores
# ════════════════════════════════════════════════════════════════════════════
class ProviderError(RuntimeError):
    pass


def _http_post(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "Ingresosbot-ai/1.0",
                                          **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise ProviderError(f"HTTP {exc.code}: {body}") from None


@dataclass
class Answer:
    data: dict
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, post: Callable[..., dict] = _http_post, timeout: float = 60.0):
        self.api_key, self.post, self.timeout = api_key, post, timeout

    def ask(self, model: str, system: str, user: str, schema: dict, effort: str, max_tokens: int) -> Answer:
        payload: dict[str, Any] = {
            "model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": api_schema(schema)}},
        }
        headers = {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION}
        if model in SERVER_FALLBACK_MODELS:
            # Si el modelo rechaza la petición por sus filtros de seguridad, el servidor la repite en otro modelo.
            payload["fallbacks"] = "default"
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        r = self.post(ANTHROPIC_URL, headers, payload, self.timeout)
        if r.get("stop_reason") == "refusal":
            raise ProviderError("el modelo rechazó la petición")
        if r.get("stop_reason") == "max_tokens":
            raise ProviderError("respuesta cortada (max_tokens)")
        text = next((b.get("text", "") for b in r.get("content", []) if b.get("type") == "text"), "")
        u = r.get("usage", {})
        tin = int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0) or 0) \
            + int(u.get("cache_read_input_tokens", 0) or 0)
        tout = int(u.get("output_tokens", 0))
        served = r.get("model") or model
        return Answer(extract_json(text), self.name, served, tin, tout, cost_usd(served, tin, tout))


class OpenAICompatProvider:
    """Groq y Gemini exponen la misma API de chat; se les pide JSON y el esquema se valida aquí."""

    def __init__(self, name: str, url: str, api_key: str, model: str,
                 post: Callable[..., dict] = _http_post, timeout: float = 45.0):
        self.name, self.url, self.api_key, self.model = name, url, api_key, model
        self.post, self.timeout = post, timeout

    def ask(self, model: str, system: str, user: str, schema: dict, effort: str, max_tokens: int) -> Answer:
        sys_msg = (system + "\n\nResponde SOLO con un objeto JSON que cumpla este esquema, sin texto alrededor:\n"
                   + json.dumps(schema, ensure_ascii=False))
        payload = {"model": self.model, "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}],
                   "response_format": {"type": "json_object"}}
        r = self.post(self.url, {"Authorization": f"Bearer {self.api_key}"}, payload, self.timeout)
        try:
            text = r["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError(f"respuesta inesperada: {str(r)[:200]}") from None
        u = r.get("usage", {})
        tin, tout = int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        return Answer(extract_json(text), self.name, self.model, tin, tout, cost_usd(self.model, tin, tout))


# ════════════════════════════════════════════════════════════════════════════
# Almacén (data/ai_state.db)
# ════════════════════════════════════════════════════════════════════════════
class AIStore:
    def __init__(self, path: str | Path = "data/ai_state.db"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")      # lo comparten bot_runner, el servicio de IA y el panel
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM ai_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO ai_meta (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.conn.commit()

    def record(self, agent: str, at: str, *, bot: str | None = None, ref: str | None = None,
               decision: str | None = None, mode: str | None = None, answer: Answer | None = None,
               input_summary: str = "", output: Any = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO ai_decisions (at, agent, bot, ref, decision, mode, provider, model, input_summary, "
            "output_json, tokens_in, tokens_out, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (at, agent, bot, ref, decision, mode, answer.provider if answer else None,
             answer.model if answer else None, input_summary[:2000],
             json.dumps(output if output is not None else (answer.data if answer else None), ensure_ascii=False),
             answer.tokens_in if answer else 0, answer.tokens_out if answer else 0,
             answer.cost_usd if answer else 0.0))
        self.conn.commit()
        return int(cur.lastrowid)

    def month_cost(self, now: datetime) -> float:
        start = iso(now.replace(day=1, hour=0, minute=0, second=0))
        return float(self.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM ai_decisions WHERE at >= ?",
                                       (start,)).fetchone()[0])

    def decisions(self, agent: str | None = None, limit: int = 20) -> list[dict]:
        sql, args = "SELECT * FROM ai_decisions", []
        if agent:
            sql, args = sql + " WHERE agent = ?", [agent]
        rows = self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [dict(r) for r in rows]

    # ── Pausas sugeridas por el vigía ──────────────────────────────────────
    def add_pause(self, created_at: str, reason: str, sources: list[str], expires_at: str, status: str) -> int:
        cur = self.conn.execute("INSERT INTO ai_pauses (created_at, reason, sources, expires_at, status) "
                                "VALUES (?, ?, ?, ?, ?)", (created_at, reason, json.dumps(sources), expires_at,
                                                           status))
        self.conn.commit()
        return int(cur.lastrowid)

    def pause(self, pause_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM ai_pauses WHERE id = ?", (pause_id,)).fetchone()
        return dict(row) if row else None

    def decide_pause(self, pause_id: int, status: str, at: str, by: str) -> dict | None:
        self.conn.execute("UPDATE ai_pauses SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                          (status, at, by, pause_id))
        self.conn.commit()
        return self.pause(pause_id)

    def active_pause(self, now: datetime) -> dict | None:
        row = self.conn.execute("SELECT * FROM ai_pauses WHERE status = 'activa' AND expires_at > ? "
                                "ORDER BY id DESC LIMIT 1", (iso(now),)).fetchone()
        return dict(row) if row else None

    def pending_pauses(self, now: datetime) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM ai_pauses WHERE status = 'pendiente' AND expires_at > ? "
                                 "ORDER BY id", (iso(now),))
        return [dict(r) for r in rows]

    # ── Hipótesis del auditor ──────────────────────────────────────────────
    def add_hypothesis(self, created_at: str, bot: str, h: dict, current_value: Any) -> int:
        cur = self.conn.execute(
            "INSERT INTO ai_hypotheses (created_at, bot, description, parameter, current_value, proposed_value, "
            "rationale, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'propuesta')",
            (created_at, bot, h["descripcion"], h["parametro"], json.dumps(current_value),
             json.dumps(h["valor_propuesto"]), h.get("motivo", "")))
        self.conn.commit()
        return int(cur.lastrowid)

    def hypotheses(self, status: str | None = None, since: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM ai_hypotheses WHERE 1=1", []
        if status:
            sql, args = sql + " AND status = ?", args + [status]
        if since:
            sql, args = sql + " AND created_at >= ?", args + [since]
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def decide_hypothesis(self, hyp_id: int, status: str, at: str) -> dict | None:
        self.conn.execute("UPDATE ai_hypotheses SET status = ?, decided_at = ? WHERE id = ?", (status, at, hyp_id))
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM ai_hypotheses WHERE id = ?", (hyp_id,)).fetchone()
        return dict(row) if row else None

    # ── Votos del analista ─────────────────────────────────────────────────
    def unresolved_votes(self, bot: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM ai_decisions WHERE agent = 'analista' AND bot = ? "
                                 "AND outcome_r IS NULL AND decision IN ('OK', 'VETO')", (bot,))
        return [dict(r) for r in rows]

    def resolve_vote(self, vote_id: int, r_multiple: float, at: str) -> None:
        self.conn.execute("UPDATE ai_decisions SET outcome_r = ?, resolved_at = ? WHERE id = ?",
                          (r_multiple, at, vote_id))
        self.conn.commit()

    def vote_stats(self, bot: str | None = None) -> dict:
        """Resultado real (R) de las operaciones según el voto del analista. Base para pasar de 'sombra' a 'activo'."""
        sql = ("SELECT decision, COUNT(*) n, AVG(outcome_r) avg_r, SUM(outcome_r) sum_r FROM ai_decisions "
               "WHERE agent = 'analista' AND outcome_r IS NOT NULL")
        args: list = []
        if bot:
            sql, args = sql + " AND bot = ?", [bot]
        out = {"OK": {"n": 0, "avg_r": None, "sum_r": 0.0}, "VETO": {"n": 0, "avg_r": None, "sum_r": 0.0}}
        for r in self.conn.execute(sql + " GROUP BY decision", args):
            if r["decision"] in out:
                out[r["decision"]] = {"n": r["n"], "avg_r": round(r["avg_r"], 3), "sum_r": round(r["sum_r"], 3)}
        out["resolved"] = out["OK"]["n"] + out["VETO"]["n"]
        return out


# ════════════════════════════════════════════════════════════════════════════
# Cliente con cadena de respaldo y tope de gasto
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class AIConfig:
    enabled: bool = False
    anthropic_key: str = ""
    groq_key: str = ""
    gemini_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"
    gemini_model: str = "gemini-3.5-flash-lite"
    model_fast: str = "claude-haiku-5-5"
    model_smart: str = "claude-sonnet-5-5"
    effort_fast: str = "low"
    effort_smart: str = "medium"
    monthly_budget_usd: float = 2.0
    db_path: str = "data/ai_state.db"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "AIConfig":
        e = os.environ.get
        return cls(
            enabled=env_flag("AI_ENABLED"),
            anthropic_key=e("ANTHROPIC_API_KEY", "").strip(),
            groq_key=e("GROQ_API_KEY", "").strip(),
            gemini_key=e("GEMINI_API_KEY", "").strip(),
            groq_model=e("AI_GROQ_MODEL", "").strip() or cls.groq_model,
            gemini_model=e("AI_GEMINI_MODEL", "").strip() or cls.gemini_model,
            model_fast=e("AI_MODEL_FAST", "").strip() or cls.model_fast,
            model_smart=e("AI_MODEL_SMART", "").strip() or cls.model_smart,
            effort_fast=e("AI_EFFORT_FAST", "").strip() or cls.effort_fast,
            effort_smart=e("AI_EFFORT_SMART", "").strip() or cls.effort_smart,
            monthly_budget_usd=float(e("AI_MONTHLY_BUDGET_USD", "").strip() or cls.monthly_budget_usd),
            db_path=e("AI_DB", "").strip() or cls.db_path,
        )


class AIClient:
    """Una sola función para todos los agentes: ask(tarea, sistema, usuario, esquema, nivel) -> Answer | None.
    None significa "sin IA" (apagada, sin claves, sin presupuesto o todos los proveedores fallaron)."""

    def __init__(self, cfg: AIConfig, store: AIStore, providers: list | None = None,
                 clock: Callable[[], float] = time.time):
        self.cfg, self.store, self.clock = cfg, store, clock
        if providers is None:
            providers = []
            if cfg.anthropic_key:
                providers.append(AnthropicProvider(cfg.anthropic_key))
            if cfg.groq_key:
                providers.append(OpenAICompatProvider("groq", GROQ_URL, cfg.groq_key, cfg.groq_model))
            if cfg.gemini_key:
                providers.append(OpenAICompatProvider("gemini", GEMINI_URL, cfg.gemini_key, cfg.gemini_model))
        self.providers = providers
        self.last_errors: list[str] = []

    @property
    def available(self) -> bool:
        return self.cfg.enabled and bool(self.providers)

    def budget_left(self) -> float:
        return self.cfg.monthly_budget_usd - self.store.month_cost(utc_now(self.clock))

    def ask(self, task: str, system: str, user: str, schema: dict, tier: str = "fast",
            max_tokens: int | None = None) -> Answer | None:
        self.last_errors = []
        if not self.available:
            return None
        left = self.budget_left()
        if left <= 0:
            self.last_errors.append(f"presupuesto mensual agotado ({self.cfg.monthly_budget_usd:.2f} USD)")
            log.warning("IA: %s; %s no se ejecuta", self.last_errors[-1], task)
            return None
        model = self.cfg.model_smart if tier == "smart" else self.cfg.model_fast
        effort = self.cfg.effort_smart if tier == "smart" else self.cfg.effort_fast
        max_tokens = max_tokens or (8000 if tier == "smart" else 4000)
        for prov in self.providers:
            ans = None
            try:
                ans = prov.ask(model, system, user, schema, effort, max_tokens)
                validate(ans.data, schema)
                log.info("IA %s: %s/%s %d+%d tokens %.5f USD", task, ans.provider, ans.model,
                         ans.tokens_in, ans.tokens_out, ans.cost_usd)
                return ans
            except (ProviderError, SchemaError, json.JSONDecodeError, urllib.error.URLError, OSError,
                    ValueError, KeyError, TypeError) as exc:
                msg = f"{getattr(prov, 'name', '?')}: {type(exc).__name__}: {str(exc)[:200]}"
                self.last_errors.append(msg)
                log.warning("IA %s falló con %s", task, msg)
                if ans is not None:           # respondió pero no sirvió: sus tokens también cuentan para el tope
                    self.store.record(task, iso(utc_now(self.clock)), decision="ERROR_ESQUEMA", answer=ans,
                                      output={"error": msg})
        return None
