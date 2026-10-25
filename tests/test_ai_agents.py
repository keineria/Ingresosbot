"""Agentes de IA (ai_core.py + ai_agents.py) con proveedores, feeds, Telegram y reloj simulados: nada sale a internet."""
import json
import logging
import sys
import tempfile
import unittest
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
        ans = c.ask("analista", "s", "u", ag.ANALISTA_SCHEMA, tier="smart")
        self.assertEqual(ans.provider, "gemini")
        self.assertEqual(len(c.last_errors), 2)
        self.assertEqual(bad.calls[0]["model"], "claude-sonnet-5-5")
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

    def gate(self, mode, *replies, store=None):
        store = store or AIStore(":memory:")
        prov = FakeProvider("anthropic", list(replies) or [VOTE_OK])
        return ag.AIGate("trading_state", store, client(store, prov), mode, FakeTelegram(),
                         clock=lambda: NOW.timestamp(), interval="1h"), prov

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

    def test_active_needs_track_record(self):
        gate, _ = self.gate("activo", VOTE_VETO)
        self.assertEqual(gate.effective_mode()[0], "sombra")
        bot, s = self.run_bot(gate)
        self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "LONG")   # sin historial, sigue en sombra

        store = AIStore(":memory:")
        for i in range(30):                                         # 30 votos: los vetos rindieron peor
            vid = store.record("analista", iso(NOW), bot="trading_state", ref=f"X|{i}",
                               decision="VETO" if i < 6 else "OK", mode="sombra")
            store.resolve_vote(vid, -1.0 if i < 6 else 0.5, iso(NOW))
        gate, _ = self.gate("activo", VOTE_VETO, store=store)
        self.assertEqual(gate.effective_mode()[0], "activo")
        bot, s = self.run_bot(gate)
        self.assertEqual(s["symbols"]["BTCUSDT"]["decision"], "VETOED")
        self.assertEqual(bot.sm.positions(), [])

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
                             clock=lambda: NOW.timestamp(), interval="1h")
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
            stats = store.vote_stats("trend_state")
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


if __name__ == "__main__":
    unittest.main()


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
                store.close()
                app._ai_svc.store.close()
