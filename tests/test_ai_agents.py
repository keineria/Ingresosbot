"""Agentes de IA (ai_core.py + ai_agents.py) con proveedores, feeds, Telegram y reloj simulados: nada sale a internet."""
import json
import logging
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_agents as ag  # noqa: E402
import quant_core_engine as q  # noqa: E402
from ai_core import (AIClient, AIConfig, AIStore, Answer, AnthropicProvider, ProviderError,  # noqa: E402
                     SchemaError, api_schema, iso, validate)
from bot_runner import BotRunner, RunnerConfig  # noqa: E402
from notifier import Notifier  # noqa: E402

logging.disable(logging.CRITICAL)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
BULL = q.demo_bull_trend()
CHOP = q.demo_chop_range()


class Clock:
    def __init__(self, dt):
        self.t = dt.timestamp()

    def __call__(self):
        return self.t


class FakeProvider:
    """Devuelve las respuestas de la lista en orden (un dict o una excepción)."""

    def __init__(self, name, replies, cost=0.001):
        self.name, self.replies, self.cost, self.calls = name, list(replies), cost, []

    def ask(self, model, system, user, schema, effort, max_tokens):
        self.calls.append({"model": model, "system": system, "user": user, "effort": effort})
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return Answer(r, self.name, model, 100, 50, self.cost)


class FakeTelegram(ag.Telegram):
    def __init__(self, chat_id="42", updates=None):
        self.sent, self.answers, self._updates = [], [], list(updates or [])
        super().__init__("TOKEN", chat_id, post=self._post)

    def _post(self, url, payload):
        method = url.rsplit("/", 1)[-1]
        if method == "sendMessage":
            self.sent.append(payload)
            return {"ok": True}
        if method == "getUpdates":
            ups, self._updates = self._updates, []
            return {"ok": True, "result": ups}
        if method == "answerCallbackQuery":
            self.answers.append(payload["text"])
        return {"ok": True}

    def texts(self):
        return [p["text"] for p in self.sent]


def client(store, *providers, budget=2.0, enabled=True):
    cfg = AIConfig(enabled=enabled, anthropic_key="x", monthly_budget_usd=budget)
    return AIClient(cfg, store, list(providers), clock=Clock(NOW))


def rss(items):
    body = "".join(f"<item><title>{t}</title><pubDate>{NOW.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
                   "</item>" for t in items)
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>"


def vigia_reply(nivel, titulares):
    return {"nivel": nivel, "categoria": "hackeo_exchange", "exchanges": ["Binance"], "titulares": titulares,
            "resumen": "Hackeo en Binance confirmado."}


VOTE_OK = {"voto": "OK", "confianza": "media", "motivo": "Nada raro."}
VOTE_VETO = {"voto": "VETO", "confianza": "alta", "motivo": "Exchange hackeado."}


# ════════════════════════════════════════════════════════════════════════════
class TestSchemaAndClient(unittest.TestCase):
    def test_validate_and_api_schema(self):
        validate(VOTE_OK, ag.ANALISTA_SCHEMA)
        with self.assertRaises(SchemaError):
            validate({"voto": "COMPRAR", "confianza": "alta", "motivo": "x"}, ag.ANALISTA_SCHEMA)
        with self.assertRaises(SchemaError):
            validate(VOTE_OK | {"extra": 1}, ag.ANALISTA_SCHEMA)
        with self.assertRaises(SchemaError):
            validate(VOTE_OK | {"motivo": "x" * 301}, ag.ANALISTA_SCHEMA)
        sent = json.dumps(api_schema(ag.VIGIA_SCHEMA))
        self.assertNotIn("maxLength", sent)
        self.assertNotIn("maxItems", sent)
        self.assertIn("maxLength", json.dumps(ag.VIGIA_SCHEMA))      # el original no se toca

    def test_fallback_chain_and_schema_errors_are_billed(self):
        store = AIStore(":memory:")
        bad = FakeProvider("anthropic", [{"voto": "QUIZAS"}], cost=0.01)
        down = FakeProvider("groq", [ProviderError("HTTP 503")])
        good = FakeProvider("gemini", [VOTE_OK])
        c = client(store, bad, down, good)
        ans = c.ask("analista", "s", "u", ag.ANALISTA_SCHEMA)
        self.assertEqual(ans.provider, "gemini")
        self.assertEqual(len(c.last_errors), 2)
        self.assertEqual((bad.calls[0]["model"], bad.calls[0]["effort"]), ("claude-opus-5-5", "high"))
        self.assertAlmostEqual(store.month_cost(NOW), 0.01)              # la respuesta inválida también costó
        self.assertEqual(store.decisions()[0]["decision"], "ERROR_ESQUEMA")

    def test_budget_cap_and_disabled(self):
        store = AIStore(":memory:")
        store.record("vigia", iso(NOW), decision="OK", answer=Answer({}, "anthropic", "m", 1, 1, 2.5))
        p = FakeProvider("anthropic", [VOTE_OK])
        c = client(store, p)
        self.assertIsNone(c.ask("x", "s", "u", ag.ANALISTA_SCHEMA))
        self.assertEqual(p.calls, [])
        self.assertIn("presupuesto", c.last_errors[0])
        self.assertIsNone(client(AIStore(":memory:"), p, enabled=False).ask("x", "s", "u", ag.ANALISTA_SCHEMA))
        self.assertEqual(p.calls, [])

    def test_anthropic_payload(self):
        seen = {}

        def post(url, headers, payload, timeout):
            seen.update(url=url, headers=headers, payload=payload)
            return {"model": payload["model"], "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": json.dumps(VOTE_OK)}],
                    "usage": {"input_tokens": 1000, "output_tokens": 100}}

        ans = AnthropicProvider("k", post=post).ask("claude-sonnet-5-5", "s", "u", ag.ANALISTA_SCHEMA, "medium", 8000)
        self.assertEqual(ans.data, VOTE_OK)
        self.assertAlmostEqual(ans.cost_usd, (1000 * 2 + 100 * 10) / 1e6)
        self.assertEqual(seen["headers"]["anthropic-version"], "2026-10-21")
        self.assertEqual(seen["payload"]["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(seen["payload"]["fallbacks"], "default")
        self.assertNotIn("temperature", seen["payload"])
        AnthropicProvider("k", post=post).ask("claude-haiku-5-5", "s", "u", ag.ANALISTA_SCHEMA, "low", 100)
        self.assertNotIn("fallbacks", seen["payload"])

        with self.assertRaises(ProviderError):
            AnthropicProvider("k", post=lambda *a: {"stop_reason": "refusal", "content": []}).ask(
                "claude-haiku-5-5", "s", "u", ag.ANALISTA_SCHEMA, "low", 100)


# ════════════════════════════════════════════════════════════════════════════
class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = AIStore(self.dir / "ai.db")
        self.tg = FakeTelegram()
        self.feeds = {"https://a.com/rss": rss(["Binance hackeado: retiros pausados", "BTC sube 2%"]),
                      "https://b.org/rss": rss(["Binance confirma un hackeo de 300 millones"])}
        self.settings = ag.AgentSettings(bots=[ag.BotRef(str(self.dir / "trend_state.db"), "", "1h")],
                                         feeds=tuple(self.feeds), lab_symbols=["BTCUSDT"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def service(self, *providers, enabled=True, prices=None):
        self.client = client(self.store, *providers, enabled=enabled)
        return ag.AgentService(self.client.cfg, self.settings, self.store, self.client, self.tg,
                               get_text=lambda url: self.feeds[url],
                               price_source=lambda s: (prices or {}).get(s, 1.0),
                               candle_source=lambda s, i, n: BULL, clock=Clock(NOW), run_lab_in_thread=False)


class TestVigia(ServiceCase):
    def test_single_source_cannot_pause(self):
        svc = self.service(FakeProvider("anthropic", [vigia_reply("PAUSA_SUGERIDA", [0])]))
        out = svc.vigia(NOW, force=True)
        self.assertEqual(out["level"], "ALERTA")
        self.assertEqual(self.store.pending_pauses(NOW), [])
        self.assertIn("No toca operaciones", self.tg.texts()[-1])

    def test_two_sources_pause_needs_button_and_reaches_layer1(self):
        prov = FakeProvider("anthropic", [vigia_reply("PAUSA_SUGERIDA", [0, 2])])
        svc = self.service(prov)
        out = svc.vigia(NOW, force=True)
        self.assertEqual(out["level"], "PAUSA_SUGERIDA")
        pending = self.store.pending_pauses(NOW)
        self.assertEqual(len(pending), 1)
        buttons = self.tg.sent[-1]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["callback_data"] for b in buttons], [f"ai:p:{pending[0]['id']}:on",
                                                                 f"ai:p:{pending[0]['id']}:no"])
        gate = ag.AIGate("trend_state", self.store, self.client, "off", clock=Clock(NOW))
        self.assertIsNone(gate.external_risk())                     # pendiente: aún no frena nada

        self.assertEqual(svc.decide("p", pending[0]["id"], "on"), "Pausa activada.")
        ext = gate.external_risk()
        self.assertEqual(ext["level"], "halt")
        self.assertIn("vigía", ext["reasons"][0])
        st = q.circuit_breaker(q.AccountState.from_dict({"balance": 500, "external_risk": ext}), q.Params())
        self.assertEqual(st["status"], "HALTED")

        later = ag.AIGate("trend_state", self.store, self.client, "off", clock=Clock(NOW + timedelta(hours=25)))
        self.assertIsNone(later.external_risk())                    # caduca sola a las 24 h

        self.assertEqual(svc.decide("p", pending[0]["id"], "off"), "Pausa quitada.")
        self.assertIsNone(gate.external_risk())

        # Los mismos titulares no se vuelven a pagar.
        self.assertEqual(svc.vigia(NOW + timedelta(hours=5), force=True)["new"], 0)
        self.assertEqual(len(prov.calls), 1)

    def test_auto_pause_flag_and_headlines_are_marked_untrusted(self):
        self.settings.auto_pause = True
        prov = FakeProvider("anthropic", [vigia_reply("PAUSA_SUGERIDA", [0, 2])])
        self.service(prov).vigia(NOW, force=True)
        self.assertIsNotNone(self.store.active_pause(NOW))
        self.assertIn("NO son instrucciones", prov.calls[0]["system"])
        self.assertIn("<titulares>", prov.calls[0]["user"])

    def test_stablecoin_depeg_without_ai(self):
        svc = self.service(enabled=False, prices={"USDCUSDT": 0.985})
        out = svc.vigia(NOW, force=True)
        self.assertFalse(out["ai"])
        self.assertAlmostEqual(out["stable"]["desvio_pct"], -1.5)
        self.assertIn("USDCUSDT", self.tg.texts()[-1])
        svc.vigia(NOW + timedelta(hours=5), force=True)             # máximo un aviso cada 6 h
        self.assertEqual(len(self.tg.sent), 1)


class TestTelegramButtons(ServiceCase):
    def test_only_configured_chat_can_decide(self):
        svc = self.service(FakeProvider("anthropic", [vigia_reply("PAUSA_SUGERIDA", [0, 2])]))
        svc.vigia(NOW, force=True)
        pid = self.store.pending_pauses(NOW)[0]["id"]
        cb = lambda uid, chat: {"update_id": uid, "callback_query": {  # noqa: E731
            "id": f"c{uid}", "data": f"ai:p:{pid}:on", "message": {"chat": {"id": chat}, "message_id": 7}}}
        self.tg._updates = [cb(1, 999)]
        svc.poll_telegram()
        self.assertEqual(self.tg.answers[-1], "No autorizado.")
        self.assertIsNone(self.store.active_pause(NOW))
        self.tg._updates = [cb(2, 42)]
        svc.poll_telegram()
        self.assertEqual(self.tg.answers[-1], "Pausa activada.")
        self.assertIsNotNone(self.store.active_pause(NOW))
        self.assertEqual(self.store.meta_get("tg_offset"), "3")


class TestAuditor(ServiceCase):
    def week_reply(self, param, value, proponer=True):
        return {"resumen": "Semana tranquila.", "problemas": [],
                "hipotesis": {"proponer": proponer, "parametro": param, "valor_propuesto": value,
                              "descripcion": "Probar otro canal", "motivo": "Muchas rupturas falsas"}}

    def make_bot_db(self):
        from state_manager import StateManager
        StateManager(self.settings.bots[0].db, 500.0, q.Params()).close()

    def test_hypothesis_limits(self):
        self.make_bot_db()
        p = q.Params()
        svc = self.service(FakeProvider("anthropic", [self.week_reply("sl_atr_mult", p.sl_atr_mult * 2)]))
        svc.weekly_audit(NOW, force=True)
        self.assertEqual(self.store.hypotheses(), [])               # +100%: descartada
        self.assertIn("50%", self.tg.texts()[-1])

        svc = self.service(FakeProvider("anthropic", [self.week_reply("sl_atr_mult", p.sl_atr_mult * 1.2)]))
        svc.weekly_audit(NOW, force=True)
        hyps = self.store.hypotheses("propuesta")
        self.assertEqual(len(hyps), 1)
        self.assertEqual(self.tg.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         f"ai:h:{hyps[0]['id']}:ok")

        svc.weekly_audit(NOW + timedelta(days=3), force=True)       # mismo mes: máximo una
        self.assertEqual(len(self.store.hypotheses()), 1)

        with self.assertRaises(SchemaError):                        # el riesgo ni siquiera está en el esquema
            validate(self.week_reply("risk_base_pct", 3.0), ag.AUDITOR_WEEK_SCHEMA)

    def test_weekly_schedule(self):
        self.make_bot_db()
        svc = self.service(FakeProvider("anthropic", [self.week_reply("ninguno", 0, proponer=False)]))
        sunday = datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(sunday.weekday(), 6)
        self.assertTrue(svc.weekly_audit(sunday - timedelta(days=1))["skipped"])
        self.assertIn("trend_state", svc.weekly_audit(sunday))
        self.assertTrue(svc.weekly_audit(sunday + timedelta(hours=2))["skipped"])     # una vez por semana

    def test_trade_audit_starts_from_new_trades(self):
        from state_manager import StateManager
        self.make_bot_db()
        prov = FakeProvider("anthropic", [{"veredicto": "REVISAR", "problemas": [{"tipo": "comisiones",
                                                                                     "detalle": "altas"}],
                                           "resumen": "Comisión rara."}])
        svc = self.service(prov)
        self.assertEqual(svc.audit_trades(NOW), 0)                  # primera vez: solo marca dónde empieza
        with StateManager(self.settings.bots[0].db, 500.0, q.Params()) as sm:
            res = q.run_engine({"balance": 500}, candles=BULL, symbol="BTCUSDT", timeframe="1h")
            pid = sm.open_from_engine(res, opened_at=iso(NOW))
            sl = sm.positions("BTCUSDT")[0]["stop_loss"]
            sm.close_position(pid, sl, "STOP_LOSS", iso(NOW + timedelta(hours=1)))
        self.assertEqual(svc.audit_trades(NOW), 1)
        self.assertIn("AUDITOR", self.tg.texts()[-1])
        self.assertEqual(svc.audit_trades(NOW), 0)


class TestCoordinator(ServiceCase):
    def test_summary_without_ai_has_exact_figures(self):
        from state_manager import StateManager
        StateManager(self.settings.bots[0].db, 500.0, q.Params()).close()
        svc = self.service(enabled=False)
        late = NOW.replace(hour=2)
        self.assertFalse(svc.daily_summary(late)["ai"])
        text = self.tg.texts()[-1]
        self.assertIn("RESUMEN DEL DÍA", text)
        self.assertIn("500.00 USD", text)
        self.assertTrue(svc.daily_summary(late + timedelta(hours=1))["skipped"])

    def test_summary_with_ai_keeps_code_figures(self):
        from state_manager import StateManager
        StateManager(self.settings.bots[0].db, 500.0, q.Params()).close()
        svc = self.service(FakeProvider("anthropic", [{"texto": "Día tranquilo."}]))
        self.assertTrue(svc.daily_summary(NOW, force=True)["ai"])
        self.assertIn("Día tranquilo.", self.tg.texts()[-1])
        self.assertIn("500.00 USD", self.tg.texts()[-1])


# ════════════════════════════════════════════════════════════════════════════
class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append(payload)
        return 200


class TestBotRunnerGate(unittest.TestCase):
    """La IA dentro de bot_runner: solo puede impedir una entrada, nunca crearla ni cambiar su tamaño."""

    def run_bot(self, gate):
        first_close = BULL.time.iloc[-1] + pd.Timedelta(hours=1)
        clock = lambda: first_close.timestamp() + 10                # noqa: E731
        data = {"BTCUSDT": BULL, "ETHUSDT": CHOP}
        bot = BotRunner(RunnerConfig(["BTCUSDT", "ETHUSDT"], "1h", ":memory:", 500.0), None,
                        Notifier("https://hook", post=Rec()), candle_source=lambda s, i, n: data[s],
                        price_source=lambda s: float(data[s].close.iloc[-1]), clock=clock, ai_gate=gate)
        return bot, bot.candle_cycle()

    def gate(self, mode, *replies, store=None, lab=False):
        store = store or AIStore(":memory:")
        prov = FakeProvider("anthropic", list(replies) or [VOTE_OK])
        settings = ag.AgentSettings(bots=[], analyst_mode=mode, lab=False)
        return ag.AIGate("trading_state", store, client(store, prov), mode, FakeTelegram(),
                         clock=lambda: NOW.timestamp(), interval="1h", profile="oficial_1h", settings=settings,
                         lab=lab, sleep=lambda s: None, wait_s=4), prov

    @staticmethod
    def grant(store, permission):
        row = store.permission("oficial_1h", permission, at=iso(NOW))
        store.set_permission(row["id"], "activo", iso(NOW), "prueba")

    def test_without_ai_nothing_changes(self):
        bot, s = self.run_bot(ag.AIGate("trading_state"))
        self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "LONG")
        self.assertNotIn("ai_vote", s["symbols"]["BTCUSDT"])

    def test_shadow_veto_does_not_block(self):
        gate, prov = self.gate("sombra", VOTE_VETO)
        bot, s = self.run_bot(gate)
        self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "LONG")
        self.assertEqual(s["symbols"]["BTCUSDT"]["ai_vote"], "VETO")
        self.assertEqual(len(bot.sm.positions("BTCUSDT")), 1)
        self.assertEqual(len(prov.calls), 1)                        # solo se consulta cuando hay señal
        vote = gate.store.decisions("analista")[0]
        self.assertEqual((vote["decision"], vote["mode"]), ("VETO", "sombra"))
        self.assertIn("BTCUSDT|", vote["ref"])

    def test_veto_only_with_the_permission(self):
        gate, _ = self.gate("escalera", VOTE_VETO)
        bot, s = self.run_bot(gate)
        self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "LONG")   # sin el permiso, sigue en sombra
        self.assertIn("en sombra", gate.telegram.texts()[-1])

        gate, _ = self.gate("escalera", VOTE_VETO)
        self.grant(gate.store, "veto")
        bot, s = self.run_bot(gate)
        self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "VETOED")
        self.assertEqual(bot.sm.positions(), [])
        self.assertIn("aplicado", gate.telegram.texts()[-1])

        gate, _ = self.gate("sombra", VOTE_VETO)                      # modo sombra: nunca aplica
        self.grant(gate.store, "veto")
        self.assertEqual(self.run_bot(gate)[1]["symbols"]["BTCUSDT"]["decision"], "LONG")

        gate, _ = self.gate("escalera", VOTE_VETO, lab=True)          # el laboratorio nunca aplica
        self.grant(gate.store, "veto")
        self.assertEqual(self.run_bot(gate)[1]["symbols"]["BTCUSDT"]["decision"], "LONG")
        self.assertEqual(gate.telegram.sent, [])

    def test_reduce_halves_size_only_with_permission(self):
        base, _ = self.run_bot(ag.AIGate("trading_state"))
        full = base.sm.positions("BTCUSDT")[0]
        gate, _ = self.gate("escalera", {"voto": "REDUCIR", "confianza": "media", "motivo": "Volátil."})
        bot, s = self.run_bot(gate)
        self.assertEqual(bot.sm.positions("BTCUSDT")[0]["position_units"], full["position_units"])
        gate, _ = self.gate("escalera", {"voto": "REDUCIR", "confianza": "media", "motivo": "Volátil."})
        self.grant(gate.store, "reducir")
        bot, s = self.run_bot(gate)
        half = bot.sm.positions("BTCUSDT")[0]
        self.assertEqual(s["symbols"]["BTCUSDT"]["ai_size_factor"], 0.5)
        self.assertAlmostEqual(half["position_units"], full["position_units"] / 2, places=6)
        self.assertAlmostEqual(half["risk_usd"], full["risk_usd"] / 2, delta=0.02)
        self.assertEqual((half["stop_loss"], half["entry_price"]), (full["stop_loss"], full["entry_price"]))

    def test_one_vote_per_signal_between_lab_and_real_bot(self):
        store = AIStore(":memory:")
        lab, prov = self.gate("escalera", VOTE_VETO, store=store, lab=True)
        self.run_bot(lab)
        real, prov2 = self.gate("escalera", VOTE_OK, store=store)
        bot, s = self.run_bot(real)
        self.assertEqual(s["symbols"]["BTCUSDT"]["ai_vote"], "VETO")   # reutiliza el voto, no pregunta otra vez
        self.assertEqual(prov2.calls, [])
        self.assertEqual(len(store.decisions("analista")), 1)

        store = AIStore(":memory:")                                   # otro proceso la está consultando
        ref = f"BTCUSDT|{(BULL.time.iloc[-1] + pd.Timedelta(hours=1)).isoformat()}"
        store.take_vote_lock("oficial_1h", ref, iso(NOW))
        waiting, prov3 = self.gate("escalera", VOTE_VETO, store=store)
        bot, s = self.run_bot(waiting)
        self.assertEqual(prov3.calls, [])
        self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "LONG")   # sin voto a tiempo: sigue la regla

    def test_ai_exit_is_executed_by_the_bot(self):
        gate, _ = self.gate("escalera")
        bot, s = self.run_bot(gate)
        pos = bot.sm.positions("BTCUSDT")[0]
        gate.store.request_exit(iso(NOW), "trading_state", "BTCUSDT", pos["opened_at"], "agotamiento")
        gate.store.request_exit(iso(NOW), "trading_state", "ETHUSDT", "2020-01-01", "no existe")
        ev = bot.watch_cycle()
        self.assertEqual([e["exit_reason"] for e in ev], ["AI_EXIT"])
        self.assertEqual(bot.sm.positions(), [])
        st = {r["symbol"]: r["status"] for r in gate.store.conn.execute("SELECT * FROM ai_exit_requests")}
        self.assertEqual(st, {"BTCUSDT": "hecha", "ETHUSDT": "caducada"})
        lab, _ = self.gate("escalera", lab=True)
        self.assertEqual(lab.pending_exits(), [])

    def test_approved_pause_halts_layer1(self):
        gate, prov = self.gate("sombra")
        gate.store.add_pause(iso(NOW), "hackeo", ["a", "b"], iso(NOW + timedelta(hours=24)), "activa")
        bot, s = self.run_bot(gate)
        self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "HALTED")
        self.assertEqual(bot.sm.positions(), [])
        self.assertEqual(prov.calls, [])
        self.assertEqual(bot.sm.account()["status"], "ACTIVE")       # la pausa no queda grabada en la cuenta

    def test_ai_errors_never_stop_the_bot(self):
        gate, _ = self.gate("activo", ProviderError("caído"))
        bot, s = self.run_bot(gate)
        self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "LONG")

        class Broken(ag.AIGate):
            def external_risk(self):
                return None

            def review_signal(self, *a, **k):
                return super().review_signal(*a, **k)

        broken = Broken("trading_state", AIStore(":memory:"), None, "sombra")
        broken.client = object()                                    # sin .ask: explota dentro y se ignora
        bot, s = self.run_bot(broken)
        self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "LONG")


class TestVoteResolution(unittest.TestCase):
    def test_votes_get_the_real_r(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "trend_state.db")
            store = AIStore(Path(d) / "ai.db")
            gate = ag.AIGate("trend_state", store, client(store, FakeProvider("anthropic", [VOTE_OK])), "sombra",
                             clock=lambda: NOW.timestamp(), interval="1h", profile="oficial_1h")
            first_close = BULL.time.iloc[-1] + pd.Timedelta(hours=1)
            price = {"v": float(BULL.close.iloc[-1])}
            bot = BotRunner(RunnerConfig(["BTCUSDT"], "1h", db, 500.0), None, Notifier("https://hook", post=Rec()),
                            candle_source=lambda s, i, n: BULL, price_source=lambda s: price["v"],
                            clock=lambda: first_close.timestamp() + 10, ai_gate=gate)
            bot.candle_cycle()
            pos = bot.sm.positions("BTCUSDT")[0]
            price["v"] = pos["stop_loss"] * 0.999
            bot.watch_cycle()
            bot.close()
            settings = ag.AgentSettings(bots=[ag.BotRef(db, "", "1h")])
            svc = ag.AgentService(gate.client.cfg, settings, store, gate.client, FakeTelegram(),
                                  price_source=lambda s: 1.0, candle_source=lambda *a: BULL,
                                  clock=lambda: NOW.timestamp())
            self.assertEqual(svc.resolve_votes(NOW), 1)
            stats = store.vote_stats("oficial_1h")
            self.assertEqual(stats["resolved"], 1)
            self.assertLess(stats["OK"]["avg_r"], -0.9)
            self.assertEqual(svc.resolve_votes(NOW), 0)
            store.close()


class TestLab(unittest.TestCase):
    def test_compare_params_windows(self):
        rng = np.random.default_rng(7)
        n = 6 * 600                                                 # 600 días de velas de 4h
        t = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")

        def series(drift):
            close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
            return pd.DataFrame({"time": t, "open": close, "high": close * 1.005, "low": close * 0.995,
                                 "close": close, "volume": 1000.0})

        data = {"AAA": series(0.0006), "BBB": series(0.0002)}
        base = q.Params(strategy="trend", daily_sma_len=50)
        same = ag.compare_params(data, base, base, warmup_days=200)
        self.assertGreaterEqual(len(same["ventanas"]), 4)
        self.assertEqual(same["veredicto"], "no mejora")            # empatar no basta
        other = ag.compare_params(data, base, ag.with_param(base, "donchian_len", 30.4), warmup_days=200)
        self.assertIn(other["veredicto"], ("mejora", "no mejora"))
        self.assertEqual(ag.with_param(base, "donchian_len", 30.4).donchian_len, 30)


# ════════════════════════════════════════════════════════════════════════════
import ai_autonomy as au  # noqa: E402

OPUS = "claude-opus-5-5"


def add_votes(store, profile, pairs, agent="analista", model=OPUS, start=0):
    """pairs: [(voto, R real, R si cerraba)]: votos ya resueltos."""
    for i, (vote, r, alt) in enumerate(pairs, start):
        vid = store.record(agent, iso(NOW), bot=profile, ref=f"S{i}|t", decision=vote,
                           answer=Answer({"motivo": "m"}, "anthropic", model, 10, 10, 0.0))
        store.resolve_vote(vid, r, iso(NOW))
        if alt is not None:
            store.conn.execute("UPDATE ai_decisions SET alt_r = ? WHERE id = ?", (alt, vid))
    store.conn.commit()


def strategy_votes(veto_hits, veto_misses, oks=40, ok_win_rate=0.36):
    """Trend Rider: pierde -0.8 R el 64% y gana +2.3 R el 36%. Los vetos aciertan veto_hits de veto_hits+misses."""
    rows = [("VETO", -0.8, None)] * veto_hits + [("VETO", 2.3, None)] * veto_misses
    wins = int(oks * ok_win_rate)
    return rows + [("OK", 2.3, None)] * wins + [("OK", -0.8, None)] * (oks - wins)


class TestLadderRules(unittest.TestCase):
    def ev(self, permission, pairs, agent="analista"):
        store = AIStore(":memory:")
        add_votes(store, "p", pairs, agent)
        return au.evaluate(permission, store.resolved_votes("p", agent, model=OPUS), OPUS)

    def test_random_vetoes_do_not_qualify(self):
        ev = self.ev("veto", strategy_votes(8, 4))            # 67% de acierto = el azar: pierde dinero
        self.assertFalse(ev.qualifies)
        self.assertLess(ev.benefit_r, 0)
        self.assertAlmostEqual(ev.base_rate, (8 + 26) / 52, places=3)

    def test_skilled_vetoes_qualify_and_need_enough_cases(self):
        ev = self.ev("veto", strategy_votes(11, 1))           # 92% de acierto, ahorra 6.5 R
        self.assertTrue(ev.qualifies, ev.why)
        self.assertGreater(ev.boot_low_r, 0)
        small = self.ev("veto", strategy_votes(6, 0, oks=10))
        self.assertFalse(small.qualifies)
        self.assertIn("faltan", small.why)

    def test_hit_rate_must_beat_chance(self):
        # Ahorra dinero por suerte de una gran perdedora, pero acierta menos que el azar + 10 puntos.
        rows = [("VETO", -6.0, None)] + [("VETO", -0.8, None)] * 6 + [("VETO", 0.5, None)] * 5
        ev = self.ev("veto", rows + [("OK", -0.8, None)] * 30 + [("OK", 2.3, None)] * 10)
        self.assertGreater(ev.benefit_r, 0)
        self.assertFalse(ev.qualifies)
        self.assertIn("acierto", ev.why)

    def test_reduce_and_exit_benefits(self):
        self.assertEqual(au.benefits("reducir", [{"decision": "REDUCIR", "outcome_r": -1.0}]), [0.5])
        rows = [("SALIR", 0.5, 2.0)] * 18 + [("SALIR", 3.0, 1.5)] * 4 + [("MANTENER", 1.0, 1.2)] * 5
        ev = self.ev("salida", rows, agent="salida")
        self.assertEqual(ev.cases, 22)
        self.assertAlmostEqual(ev.benefit_r, 18 * 1.5 - 4 * 1.5)
        self.assertTrue(ev.qualifies, ev.why)

    def test_other_models_do_not_count(self):
        store = AIStore(":memory:")
        add_votes(store, "p", strategy_votes(11, 1), model="claude-sonnet-5-5")
        self.assertEqual(store.resolved_votes("p", model=OPUS), [])

    def test_demotion_and_revoked_stays(self):
        good = self.ev("veto", strategy_votes(11, 1))
        self.assertEqual(au.next_status("sombra", good, True)[0], "activo")
        self.assertIsNone(au.next_status("sombra", good, False))        # techo de AI_AUTONOMIA_MAX
        self.assertIsNone(au.next_status("revocado", good, True))
        bad = self.ev("veto", strategy_votes(11, 1) + [("VETO", 2.3, None)] * 6)
        self.assertEqual(au.next_status("activo", bad, True)[0], "sombra")
        self.assertEqual(au.next_status("activo", good, False)[0], "sombra")


class TestLadderService(ServiceCase):
    def test_promotion_button_and_revoke(self):
        self.settings.bots = [ag.BotRef(str(self.dir / "trend_state.db"), "deploy/trend_params.json", "4h")]
        self.settings.lab = False
        svc = self.service()
        profile = "trend_params_4h"
        add_votes(self.store, profile, strategy_votes(11, 1))
        changes = svc.ladder(NOW, force=True)
        self.assertEqual([(c["permiso"], c["estado"]) for c in changes], [("veto", "activo")])
        self.assertTrue(self.store.is_active(profile, "veto"))
        msg = self.tg.sent[-1]
        self.assertIn("ganó el permiso de vetar", msg["text"])
        pid = self.store.permission(profile, "veto")["id"]
        self.assertEqual(msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"ai:a:{pid}:off")

        self.assertEqual(svc.decide("a", pid, "off"), "Permiso quitado.")
        self.assertEqual(svc.ladder(NOW, force=True), [])               # revocado: no vuelve solo
        self.assertFalse(self.store.is_active(profile, "veto"))
        svc.decide("a", pid, "on")
        self.assertEqual(svc.ladder(NOW, force=True)[0]["estado"], "activo")

        self.settings.autonomy_max = 0                                  # Keiner baja el techo
        self.assertEqual(svc.ladder(NOW, force=True)[0]["estado"], "sombra")
        rows = svc.ladder_rows()
        self.assertEqual({r["permiso"] for r in rows}, set(au.PERMISSIONS))
        self.assertIn("vetar entradas", svc.ladder_text(rows))


class TestExitGuardian(ServiceCase):
    def setUp(self):
        super().setUp()
        from state_manager import StateManager
        self.lab_db = str(self.dir / "sombra" / "oficial_1h.db")
        self.real_db = str(self.dir / "trend_state.db")
        Path(self.lab_db).parent.mkdir()
        self.res = q.run_engine({"balance": 500}, candles=BULL, symbol="BTCUSDT", timeframe="1h")
        self.opened = iso(NOW - timedelta(days=3))
        for db in (self.lab_db, self.real_db):
            with StateManager(db, 500.0, q.Params()) as sm:
                sm.open_from_engine(self.res, opened_at=self.opened)
        self.settings.bots = [ag.BotRef(self.real_db, "", "1h")]
        self.settings.lab_profiles = [ag.LabProfile("", "1h", ["BTCUSDT"])]
        self.settings.lab_dir = str(self.dir / "sombra")
        rk = self.res["layer_3_atr_risk_guardian"]
        self.entry, self.risk_px = rk["entry_price"], rk["entry_price"] - rk["stop_loss"]

    def test_shadow_exit_vote_is_measured(self):
        up = self.entry + 2 * self.risk_px                              # +2 R aprox.
        prov = FakeProvider("anthropic", [{"voto": "SALIR", "confianza": "media", "motivo": "Agotamiento."}])
        svc = self.service(prov, prices={"BTCUSDT": up})
        out = svc.exit_votes(NOW)
        self.assertEqual(out["asked"], 1)                               # la misma posición en 2 bases: 1 voto
        self.assertEqual(out["exit_requests"], [])                      # sin permiso: no pide cerrar
        vote = self.store.decisions("salida")[0]
        self.assertEqual(vote["bot"], "oficial_1h")
        self.assertGreater(vote["alt_r"], 1.8)
        self.assertEqual(prov.calls[0]["model"], "claude-sonnet-5-5")   # potencia equilibrada
        self.assertIn("en sombra", self.tg.texts()[-1])
        self.assertEqual(svc.exit_votes(NOW + timedelta(days=2))["asked"], 0)   # ya pidió salir: no repite

        from state_manager import StateManager                          # el laboratorio sigue y cierra peor
        with StateManager(self.lab_db, 500.0, q.Params()) as sm:
            sm.close_position(sm.positions()[0]["id"], self.entry + 0.5 * self.risk_px, "TRAILING_STOP",
                              iso(NOW + timedelta(days=1)))
        self.assertEqual(svc.resolve_votes(NOW), 1)
        ev = svc.evidence("oficial_1h", "salida")
        self.assertEqual(ev.cases, 1)
        self.assertGreater(ev.benefit_r, 1.0)

    def test_exit_needs_profit_and_permission(self):
        prov = FakeProvider("anthropic", [{"voto": "SALIR", "confianza": "alta", "motivo": "Hackeo."}])
        svc = self.service(prov, prices={"BTCUSDT": self.entry})
        self.assertEqual(svc.exit_votes(NOW)["asked"], 0)               # sin ganancia de 1 R no se pregunta
        svc.price_source = lambda s: self.entry + 2 * self.risk_px
        row = self.store.permission("oficial_1h", "salida", at=iso(NOW))
        self.store.set_permission(row["id"], "activo", iso(NOW), "prueba")
        out = svc.exit_votes(NOW)
        self.assertEqual(out["exit_requests"], ["trend_state:BTCUSDT"])
        self.assertEqual(self.store.pending_exits("trend_state")[0]["opened_at"], self.opened)
        self.assertEqual(self.store.pending_exits("oficial_1h"), [])     # el laboratorio nunca cierra


class TestShadowLab(unittest.TestCase):
    def test_lab_takes_every_signal_without_risking_anything(self):
        import ai_lab
        with tempfile.TemporaryDirectory() as d:
            settings = ag.AgentSettings(bots=[], lab_dir=str(Path(d) / "sombra"),
                                        lab_profiles=[ag.LabProfile("", "1h", ["BTCUSDT", "ETHUSDT"])])
            cfg = AIConfig(enabled=True, anthropic_key="x", db_path=str(Path(d) / "ai.db"))
            store = AIStore(cfg.db_path)
            prov = FakeProvider("anthropic", [VOTE_VETO])
            first_close = BULL.time.iloc[-1] + pd.Timedelta(hours=1)
            data = {"BTCUSDT": BULL, "ETHUSDT": CHOP}
            runner = ai_lab.make_runner(settings.lab_profiles[0], cfg, settings,
                                        candle_source=lambda s, i, n: data[s],
                                        price_source=lambda s: float(data[s].close.iloc[-1]),
                                        clock=lambda: first_close.timestamp() + 10, store=store,
                                        client=AIClient(cfg, store, [prov], clock=lambda: NOW.timestamp()))
            self.assertEqual(runner.p.halt_losses, ai_lab.NO_LIMIT)
            self.assertEqual(runner.p.risk_base_pct, ai_lab.LAB_RISK_PCT)
            s = runner.candle_cycle()
            self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "LONG")      # el VETO no bloquea aquí
            pos = runner.sm.positions("BTCUSDT")[0]
            self.assertLess(pos["risk_usd"], 60)
            self.assertEqual(store.decisions("analista")[0]["mode"], "laboratorio")
            self.assertTrue(Path(settings.lab_dir, "oficial_1h.db").exists())
            runner.close()
            store.close()


class TestPrice(unittest.TestCase):
    def test_presets_and_measured_costs(self):
        est = ag.estimate_costs(None, NOW)["potencias"]
        self.assertLess(est["economica"]["usd_mes"], est["equilibrada"]["usd_mes"])
        self.assertLess(est["equilibrada"]["usd_mes"], est["maxima"]["usd_mes"])
        self.assertLess(est["maxima"]["usd_mes"], 12)
        store = AIStore(":memory:")
        for day in range(10):                                         # 10 días, 2 votos de analista al día
            for _ in range(2):
                store.record("analista", iso(NOW - timedelta(days=day)), decision="OK",
                             answer=Answer({}, "anthropic", OPUS, 3000, 4000, 0.09))
        got = ag.estimate_costs(store, NOW)
        self.assertEqual(got["medido"], ["analista"])
        row = got["potencias"]["equilibrada"]["tareas"]["analista"]
        self.assertAlmostEqual(row["llamadas_mes"], 60, delta=7)
        cfg = AIConfig.from_env()
        self.assertIn(cfg.potencia, ag.POTENCIA)


class TestPanelApi(unittest.TestCase):
    def test_ai_state_and_pause_buttons(self):
        import os
        from unittest import mock

        import web_server as ws
        with tempfile.TemporaryDirectory() as d:
            env = {"AI_ENABLED": "false", "AI_DB": str(Path(d) / "ai.db"), "TELEGRAM_BOT_TOKEN": "",
                   "AI_BOT_DBS": str(Path(d) / "trend_state.db")}
            with mock.patch.dict(os.environ, env):
                cfg = RunnerConfig(["BTCUSDT"], "1h", str(Path(d) / "bot.db"), 500.0)
                app = ws.DashboardApp(cfg, q.Params(), "k" * 20, Notifier("https://hook", post=Rec()),
                                      price_source=lambda s: 1.0, clock=lambda: NOW.timestamp())
                self.assertEqual(app.ai_state(), {"enabled": False})
                store = AIStore(env["AI_DB"])
                pid = store.add_pause(iso(NOW), "hackeo", ["a", "b"], iso(NOW + timedelta(hours=24)), "pendiente")
                state = app.ai_state()
                self.assertEqual(state["pending_pauses"][0]["id"], pid)
                self.assertEqual(app.ai_decide({"kind": "pause", "id": pid, "action": "on"}, "web:test")["message"],
                                 "Pausa activada.")
                self.assertIsNotNone(store.active_pause(NOW))
                with self.assertRaises(ws.ApiError):
                    app.ai_decide({"kind": "pause", "id": pid, "action": "ok"})
                with self.assertRaises(ws.ApiError):
                    app.ai_decide({"kind": "trade", "id": 1, "action": "on"})
                perm = store.permission("trend_params_4h", "veto", at=iso(NOW))
                store.set_permission(perm["id"], "activo", iso(NOW), "prueba")
                self.assertEqual(app.ai_decide({"kind": "permission", "id": perm["id"], "action": "off"})["message"],
                                 "Permiso quitado.")
                self.assertEqual(store.permission("trend_params_4h", "veto")["status"], "revocado")
                self.assertEqual(app.ai_state()["escalera"][0]["status"], "revocado")
                store.close()
                app._ai_svc.store.close()


class TestBotCoverage(unittest.TestCase):
    """Keiner (Trend Rider) y Celeste (Tortuga 55) con todos los agentes; Nathalia (Oficial) solo en el resumen."""

    def test_defaults_cover_three_bots(self):
        s = ag.AgentSettings()
        self.assertEqual([(b.key, b.full) for b in s.bots],
                         [("trend_state", True), ("tortuga_state", True), ("trading_state", False)])
        self.assertEqual(list(s.profiles()), ["trend_params_4h", "tortuga_params_1d"])
        tortuga_lab = s.lab_profiles[1]
        self.assertEqual((tortuga_lab.interval, len(tortuga_lab.symbols)), ("1d", 21))
        self.assertIn("PAXGUSDT", tortuga_lab.symbols)

    def test_names_come_from_bots_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "bots.json"
            f.write_text(json.dumps([{"db": "data/tortuga_state.db", "name": "Tortuga 55", "trader": "Celeste",
                                      "params": "deploy/tortuga_params.json", "interval": "1d"},
                                     {"db": "data/trading_state.db", "name": "Bot Oficial", "trader": "Nathalia"}]))
            names = ag.bot_names(f)
        self.assertEqual(names["tortuga_state"], "Celeste · Tortuga 55")
        self.assertEqual(names["tortuga_params_1d"], "Celeste · Tortuga 55")
        self.assertEqual(names["oficial_1h"], "Nathalia · Bot Oficial")
        self.assertEqual(ag.bot_names(Path("/no/existe.json")), {})

    def test_summary_only_bot_has_no_analyst(self):
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.dict("os.environ", {
                "AI_ENABLED": "true", "AI_DB": str(Path(tmp) / "ai.db"), "ANTHROPIC_API_KEY": "x",
                "AI_BOT_DBS": "data/trend_state.db|deploy/trend_params.json|4h,data/trading_state.db||1h|solo_resumen"}):
            self.assertEqual(ag.AIGate.from_env("data/trading_state.db", "1h").analyst_mode, "off")
            gate = ag.AIGate.from_env("data/trend_state.db", "4h")
            self.assertEqual((gate.analyst_mode, gate.profile), ("escalera", "trend_params_4h"))
            self.assertIsNotNone(gate.store)            # la pausa del vigía sigue aplicando a todos


if __name__ == "__main__":
    unittest.main()
