import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import subscriptions
from conftest import ADMIN, make_user
from fakes import (
    FAKE_PTB,
    FAKE_PTB_EXT,
    FakeAdapter,
    FakeApp,
    FakeBot,
    FakeCtx,
    FakeGateway,
    FakePairingStore,
    FakeQuery,
    Platform,
    tg_event,
)
from subscriptions import hermes_compat
from subscriptions.admin_tools import SCHEMAS, AdminTools
from subscriptions.gate import Gate, make_tool_guard
from subscriptions.models import Status
from subscriptions.settings import Settings, SettingsError, from_mapping
from subscriptions.telegram_ui import TelegramUI
from subscriptions.timeutil import DAY, HOUR, Duration


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def settings(db_path):
    return Settings(role="gate", admin_id=ADMIN, db_path=db_path, daily_limit=3)


@pytest.fixture
def gate(settings, svc):
    return Gate(settings, svc)


def skipped(result):
    return isinstance(result, dict) and result.get("action") == "skip"


# ------------------------------------------------------------------ 1, 2: yangi foydalanuvchi /start
def test_new_user_start_registers_notifies_admin_and_welcomes(gate, svc):
    gw = FakeGateway()
    result = run(gate.on_pre_gateway_dispatch(tg_event(500, "/start", first="Dilshod", username="dilshod_x"), gw))
    assert skipped(result) and result["reason"] == "subscriptions:new_user"
    user = svc.get_user(500)
    assert user.status is Status.PENDING and user.first_name == "Dilshod" and user.username == "dilshod_x"
    assert len(gw.adapter.sent) == 1 and "administrator ruxsati" in gw.adapter.sent[0][1]
    outbox = svc.fetch_outbox()
    assert [(m.chat_id, m.kind, m.payload["telegram_id"]) for m in outbox] == [(ADMIN, "new_user", 500)]
    assert "ID: <code>500</code>" in outbox[0].text and "@dilshod_x" in outbox[0].text
    # Qayta /start: yangi xabar adminga takrorlanmaydi
    run(gate.on_pre_gateway_dispatch(tg_event(500, "/start", first="Dilshod"), gw))
    assert len(svc.fetch_outbox()) == 1
    assert not gw.store.is_approved("telegram", "500")


# ------------------------------------------------------------------ 3: tasdiqlanmagan foydalanuvchi
def test_pending_user_blocked_and_reply_throttled(gate, svc, clock):
    gw = FakeGateway()
    make_user(svc, 500)
    results = [run(gate.on_pre_gateway_dispatch(tg_event(500, "salom"), gw)) for _ in range(3)]
    assert all(skipped(r) and r["reason"] == "subscriptions:status:PENDING" for r in results)
    assert len(gw.adapter.sent) == 1  # 10 daqiqada bir marta javob
    clock.advance(601)
    run(gate.on_pre_gateway_dispatch(tg_event(500, "salom"), gw))
    assert len(gw.adapter.sent) == 2


def test_first_message_without_start_also_registers(gate, svc):
    gw = FakeGateway()
    result = run(gate.on_pre_gateway_dispatch(tg_event(600, "rasmni tahlil qil", first="Ali"), gw))
    assert skipped(result) and svc.get_user(600).status is Status.PENDING


# ------------------------------------------------------------------ 5, 6: faol obuna, limit va tugash
def test_active_user_allowed_quota_and_pairing(gate, svc, clock):
    gw = FakeGateway()
    make_user(svc, 500)
    svc.add_time(500, Duration(2, "hour"), ADMIN)
    results = [run(gate.on_pre_gateway_dispatch(tg_event(500, f"savol {i}"), gw)) for i in range(4)]
    assert results[:3] == [{"action": "allow"}] * 3
    assert skipped(results[3]) and results[3]["reason"] == "subscriptions:daily_limit"
    assert gw.store.is_approved("telegram", "500") and not gw.store.approve_outside_lock
    assert "limitingiz tugadi" in gw.adapter.sent[-1][1]
    clock.advance(2 * HOUR)  # obuna tugadi
    expired = run(gate.on_pre_gateway_dispatch(tg_event(500, "yana"), gw))
    assert skipped(expired) and expired["reason"] == "subscriptions:status:EXPIRED"
    assert not gw.store.is_approved("telegram", "500")  # pairing'dan chiqarildi


def test_media_message_counts_and_passes(gate, svc):
    gw = FakeGateway()
    make_user(svc, 500)
    svc.add_time(500, Duration(1, "day"), ADMIN)
    event = tg_event(500, "")
    event.message_type = "PHOTO"
    assert run(gate.on_pre_gateway_dispatch(event, gw)) == {"action": "allow"}
    assert svc.messages_today(500) == 1


@pytest.mark.parametrize("status_call,reason", [
    (lambda s: s.ban(500, ADMIN), "BANNED"),
    (lambda s: s.reject(500, ADMIN), "REJECTED"),
    (lambda s: (s.add_time(500, Duration(1, "day"), ADMIN), s.suspend(500, ADMIN)), "SUSPENDED"),
    (lambda s: s.approve(500, ADMIN), "APPROVED"),
])
def test_non_active_statuses_blocked(gate, svc, status_call, reason):
    gw = FakeGateway()
    make_user(svc, 500)
    status_call(svc)
    gw.store._approve_user("telegram", "500")  # eski ruxsat qolib ketgan holat
    result = run(gate.on_pre_gateway_dispatch(tg_event(500, "salom"), gw))
    assert skipped(result) and result["reason"] == f"subscriptions:status:{reason}"
    assert not gw.store.is_approved("telegram", "500")


# ------------------------------------------------------------------ 12: chetlab o'tish urinishlari
def test_disallowed_commands_and_groups_and_bots(gate, svc):
    gw = FakeGateway()
    make_user(svc, 500)
    svc.add_time(500, Duration(1, "day"), ADMIN)
    assert run(gate.on_pre_gateway_dispatch(tg_event(500, "/new"), gw)) == {"action": "allow"}
    model = run(gate.on_pre_gateway_dispatch(tg_event(500, "/model gpt-5"), gw))
    assert skipped(model) and model["reason"] == "subscriptions:command:model"
    assert skipped(run(gate.on_pre_gateway_dispatch(tg_event(500, "/model@AnyBot x"), gw)))
    group = run(gate.on_pre_gateway_dispatch(tg_event(500, "salom", chat_type="group"), gw))
    assert skipped(group) and group["reason"] == "subscriptions:not_dm"
    bot = run(gate.on_pre_gateway_dispatch(tg_event(501, "salom", is_bot=True), gw))
    assert skipped(bot) and svc.get_user(501) is None
    no_id = tg_event(500, "salom")
    no_id.source.user_id = None
    assert skipped(run(gate.on_pre_gateway_dispatch(no_id, gw)))
    spoof = tg_event(500, "salom")
    spoof.source.user_id = "500abc"
    assert skipped(run(gate.on_pre_gateway_dispatch(spoof, gw)))


def test_admin_and_other_platforms_pass_through(gate, svc):
    gw = FakeGateway()
    assert run(gate.on_pre_gateway_dispatch(tg_event(ADMIN, "/model"), gw)) == {"action": "allow"}
    assert run(gate.on_pre_gateway_dispatch(tg_event(500, "hi", platform=Platform.DISCORD), gw)) is None
    internal = tg_event(500, "x")
    internal.internal = True
    assert run(gate.on_pre_gateway_dispatch(internal, gw)) is None


# ------------------------------------------------------------------ xatolarda fail-closed
def test_fail_closed_on_db_error(gate, svc, monkeypatch):
    gw = FakeGateway()
    make_user(svc, 500)
    svc.add_time(500, Duration(1, "day"), ADMIN)
    monkeypatch.setattr(svc, "check_access", lambda tid: (_ for _ in ()).throw(RuntimeError("disk")))
    result = run(gate.on_pre_gateway_dispatch(tg_event(500, "salom"), gw))
    assert skipped(result) and result["reason"] == "subscriptions:error:RuntimeError"
    assert not gw.store.is_approved("telegram", "500")


def test_fail_closed_on_db_timeout(settings, svc, monkeypatch):
    fast = Settings(role="gate", admin_id=ADMIN, db_path=settings.db_path, daily_limit=3, db_timeout=1.0)
    gate = Gate(fast, svc)
    make_user(svc, 500)
    svc.add_time(500, Duration(1, "day"), ADMIN)
    monkeypatch.setattr(svc, "check_access", lambda tid: time.sleep(3))

    async def timed():
        started = time.monotonic()
        result = await gate.on_pre_gateway_dispatch(tg_event(500, "salom"), FakeGateway())
        return result, time.monotonic() - started  # hook qaytgan payt (asyncio.run yopilishini kutmasdan)

    result, elapsed = run(timed())
    assert skipped(result) and "TimeoutError" in result["reason"] and elapsed < 2.0


def test_fail_closed_when_pairing_api_missing(gate, svc):
    make_user(svc, 500)
    svc.add_time(500, Duration(1, "day"), ADMIN)
    gw = FakeGateway(store=object())  # Hermes API mos emas
    result = run(gate.on_pre_gateway_dispatch(tg_event(500, "salom"), gw))
    assert skipped(result) and result["reason"] == "subscriptions:pairing_unavailable"


def test_reply_failure_does_not_change_decision(gate, svc):
    gw = FakeGateway(adapter=FakeAdapter(fail=True))
    result = run(gate.on_pre_gateway_dispatch(tg_event(500, "/start"), gw))
    assert skipped(result) and svc.get_user(500).status is Status.PENDING


def test_tool_guard():
    guard = make_tool_guard(frozenset({"web_search", "clarify"}))
    assert guard(tool_name="web_search", args={}) is None
    for name in ("terminal", "read_file", "write_file", "execute_code", "memory", "skill_manage", "", None):
        result = guard(tool_name=name, args={})
        assert result["action"] == "block" and result["message"]


# ------------------------------------------------------------------ 4, 9: admin tool'lari
def tools_for(svc, settings, actor):
    return AdminTools(settings, svc, lambda: actor)


def test_admin_tools_reject_non_admin(svc, settings):
    make_user(svc, 500, "Dilshod")
    for actor in (None, 500, 999):
        tools = tools_for(svc, settings, actor)
        for name, handler in tools.handlers().items():
            out = json.loads(handler({"query": "Dilshod", "telegram_id": 500, "filter": "all",
                                      "action": "add_time", "amount": 1, "unit": "day"}))
            assert out == {"success": False, "error": "Bu vosita faqat administrator uchun."}, name
    assert svc.fetch_outbox() == [] and svc.get_user(500).status is Status.PENDING


def test_natural_language_flow_two_hours(svc, settings, clock):
    """«Dilshodga 2 soatlik obuna ber» -> model: find -> request_action -> admin tugmani bosadi."""
    make_user(svc, 500, "Dilshod", "Karimov", "dilshod_k")
    tools = tools_for(svc, settings, ADMIN)
    found = json.loads(tools.find_users({"query": "Dilshod"}))
    assert found["count"] == 1 and found["users"][0]["telegram_id"] == 500
    req = json.loads(tools.request_action({"action": "add_time", "telegram_id": 500, "amount": 2, "unit": "hour"}))
    assert req["status"] == "awaiting_confirmation" and "yangi obuna 2 soat" in req["summary"]
    assert svc.get_user(500).status is Status.PENDING  # hali bajarilmagan
    confirm = [m for m in svc.fetch_outbox() if m.kind == "confirm"][0]
    ui = TelegramUI(settings, svc, spawn_task=lambda *a, **k: None, revoke_pairing=lambda tid: True,
                    ptb=lambda: FAKE_PTB, ptb_ext=lambda: FAKE_PTB_EXT)
    ui._app = FakeApp()
    query = FakeQuery(ADMIN, f"subs:ok:{confirm.payload['pending_id']}")
    run(ui.on_callback(SimpleNamespace(callback_query=query)))
    user = svc.get_user(500)
    assert user.status is Status.ACTIVE and user.sub_expires_at == clock.now + 2 * HOUR
    assert "Bajarildi" in query.edits[0]
    assert any(m.kind == "activated" and m.chat_id == 500 for m in svc.fetch_outbox())


def test_extend_summary_and_ambiguous_names(svc, settings):
    make_user(svc, 1, "Jasur", "Aliyev")
    make_user(svc, 2, "Jasur", "Karimov")
    svc.add_time(2, Duration(1, "day"), ADMIN)
    tools = tools_for(svc, settings, ADMIN)
    found = json.loads(tools.find_users({"query": "Jasur"}))
    assert found["count"] == 2 and found["note"]
    req = json.loads(tools.request_action({"action": "add_time", "telegram_id": 2, "amount": 2, "unit": "day"}))
    assert "uzaytirish" in req["summary"] and "→ yangi" in req["summary"]


def test_request_action_validation(svc, settings):
    make_user(svc, 500)
    tools = tools_for(svc, settings, ADMIN)
    for args in ({"action": "delete_all"}, {"action": "add_time", "telegram_id": 500, "amount": 0, "unit": "day"},
                 {"action": "add_time", "telegram_id": 500, "amount": 2, "unit": "year"},
                 {"action": "add_time", "amount": 2, "unit": "day"},
                 {"action": "suspend", "telegram_id": 999}, {"action": "resume", "telegram_id": 500}):
        assert json.loads(tools.request_action(args))["success"] is False, args
    assert svc.fetch_outbox() == []


def test_list_and_stats_tools(svc, settings, clock):
    for tid in (1, 2, 3):
        make_user(svc, tid, f"U{tid}")
    svc.add_time(1, Duration(3, "hour"), ADMIN)
    svc.add_time(2, Duration(10, "day"), ADMIN)
    tools = tools_for(svc, settings, ADMIN)
    assert json.loads(tools.list_users({"filter": "expiring_today"}))["count"] == 1
    assert json.loads(tools.list_users({"filter": "active"}))["count"] == 2
    assert json.loads(tools.list_users({"filter": "pending"}))["users"][0]["telegram_id"] == 3
    assert json.loads(tools.stats({}))["stats"]["active_subscriptions"] == 2
    info = json.loads(tools.user_info({"telegram_id": 1}))["user"]
    assert info["status"] == "ACTIVE" and info["recent_history"][0]["action"] == "ADD_TIME"
    assert json.loads(tools.list_users({"filter": "nope"}))["success"] is False


def test_schemas_are_valid_function_schemas():
    for name, schema in SCHEMAS.items():
        assert schema["name"] == name and schema["description"]
        assert schema["parameters"]["type"] == "object" and schema["parameters"]["additionalProperties"] is False


# ------------------------------------------------------------------ tugmalar va fon tsikli
def make_ui(svc, settings, revoked=None):
    revoked = revoked if revoked is not None else []
    ui = TelegramUI(settings, svc, spawn_task=lambda *a, **k: None, revoke_pairing=revoked.append,
                    ptb=lambda: FAKE_PTB, ptb_ext=lambda: FAKE_PTB_EXT)
    ui._app = FakeApp()
    return ui, revoked


def test_callback_rejects_non_admin(svc, settings):
    make_user(svc, 500)
    pending = svc.create_pending("approve", {"target": 500}, "x", ADMIN)
    ui, _ = make_ui(svc, settings)
    for data in (f"subs:ok:{pending.id}", "subs:ap:500", "subs:in:500"):
        query = FakeQuery(500, data)  # foydalanuvchining o'zi bosmoqchi
        run(ui.on_callback(SimpleNamespace(callback_query=query)))
        assert query.answers == [("Bu tugma faqat administrator uchun.", True)] and query.edits == []
    assert svc.get_user(500).status is Status.PENDING and svc.get_pending(pending.id).state == "pending"


def test_callback_double_press_and_cancel(svc, settings):
    make_user(svc, 500)
    ui, _ = make_ui(svc, settings)
    pending = svc.create_pending("add_time", {"target": 500, "amount": 1, "unit": "day"}, "x", ADMIN)
    q1, q2 = FakeQuery(ADMIN, f"subs:ok:{pending.id}"), FakeQuery(ADMIN, f"subs:ok:{pending.id}")
    run(ui.on_callback(SimpleNamespace(callback_query=q1)))
    run(ui.on_callback(SimpleNamespace(callback_query=q2)))
    assert q1.answers == [("Bajarildi", False)] and q2.answers[0][1] is True
    assert len(svc.history(500)) == 1
    p2 = svc.create_pending("revoke", {"target": 500}, "x", ADMIN)
    q3 = FakeQuery(ADMIN, f"subs:no:{p2.id}")
    run(ui.on_callback(SimpleNamespace(callback_query=q3)))
    assert svc.get_user(500).status is Status.ACTIVE and q3.edits == ["🚫 Bekor qilindi."]


def test_callback_quick_buttons_and_revoke_on_ban(svc, settings):
    make_user(svc, 500, "Ali")
    ui, revoked = make_ui(svc, settings)
    q = FakeQuery(ADMIN, "subs:ap:500")
    run(ui.on_callback(SimpleNamespace(callback_query=q)))
    assert svc.get_user(500).status is Status.APPROVED and revoked == [500]  # kirish yo'q -> pairing tozalanadi
    q_info = FakeQuery(ADMIN, "subs:in:500")
    run(ui.on_callback(SimpleNamespace(callback_query=q_info)))
    assert "Foydalanuvchi" in ui._app.bot.messages[-1]["text"]
    for bad in ("subs:ap:abc", "subs:zz:1", "other:ok:1", "subs:ok:" + "x" * 40):
        qb = FakeQuery(ADMIN, bad)
        run(ui.on_callback(SimpleNamespace(callback_query=qb)))
        assert qb.edits == []


def test_factory_registers_scoped_handler_and_single_loop(svc, settings):
    spawned = []
    ui = TelegramUI(settings, svc, spawn_task=lambda coro, name=None: (spawned.append(name), coro.close()),
                    revoke_pairing=lambda tid: True, ptb=lambda: FAKE_PTB, ptb_ext=lambda: FAKE_PTB_EXT)
    app1, app2 = FakeApp(), FakeApp()
    ui.factory(app1, None)
    ui.factory(app2, None)  # qayta ulanish
    assert spawned == ["subscriptions-loop"]
    assert app1.handlers[0].pattern == "^subs:" and ui._app is app2


def test_outbox_delivery_with_buttons_and_failures(svc, settings):
    make_user(svc, 500, "Ali", username="ali_99")
    svc.enqueue_message(ADMIN, "new_user", "yangi", "new_user:500", {"telegram_id": 500, "username": "ali_99"})
    svc.enqueue_message(500, "activated", "faol", "a:500")
    svc.enqueue_message(600, "expired", "tugadi", "e:600")
    ui, _ = make_ui(svc, settings)
    ui._app = FakeApp(bot=FakeBot(fail_for={600}))
    assert run(ui.deliver_outbox()) == 2
    admin_msg = ui._app.bot.messages[0]
    rows = admin_msg["markup"].inline_keyboard
    assert [b.callback_data for b in rows[0]] == ["subs:ap:500", "subs:rj:500"]
    assert rows[1][1].url == "https://t.me/ali_99" and admin_msg["parse_mode"] == "HTML"
    assert ui._app.bot.messages[1]["markup"] is None
    assert [m.chat_id for m in svc.fetch_outbox()] == [600]  # xato bo'lgani qayta urinadi


def test_expiry_processing_notifies_once_and_revokes(svc, settings, clock):
    make_user(svc, 500, "Ali")
    svc.add_time(500, Duration(1, "hour"), ADMIN)
    ui, revoked = make_ui(svc, settings)
    assert ui.process_expiry() == 0
    clock.advance(HOUR)
    assert ui.process_expiry() == 1 and revoked == [500]
    assert ui.process_expiry() == 0
    kinds = sorted((m.chat_id, m.kind) for m in svc.fetch_outbox())
    assert kinds == [(500, "expired"), (ADMIN, "expired_admin")]


def test_tick_runs_expiry_by_interval(svc, settings, clock):
    make_user(svc, 500)
    svc.add_time(500, Duration(1, "hour"), ADMIN)
    ui, revoked = make_ui(svc, settings)
    run(ui.tick())
    clock.advance(HOUR + 30)  # interval (60s) o'tdi
    run(ui.tick())
    assert revoked == [500] and svc.get_user(500).status is Status.EXPIRED


# ------------------------------------------------------------------ register() va sozlamalar
def base_settings(db_path, role):
    return {"role": role, "admin_telegram_id": ADMIN, "db_path": str(db_path), "daily_limit": 50}


def test_register_gate_role(db_path):
    ctx = FakeCtx(base_settings(db_path, "gate"))
    subscriptions.register(ctx)
    assert set(ctx.hooks) == {"pre_gateway_dispatch", "pre_tool_call"}
    assert ctx.tools == {} and ctx.telegram_factories == []
    assert ctx.hooks["pre_tool_call"][0](tool_name="terminal")["action"] == "block"


def test_register_admin_role(db_path):
    ctx = FakeCtx(base_settings(db_path, "admin"))
    subscriptions.register(ctx)
    assert set(ctx.tools) == set(SCHEMAS) and all(t["toolset"] == "subscriptions" for t in ctx.tools.values())
    assert len(ctx.telegram_factories) == 1 and "pre_tool_call" not in ctx.hooks
    guard = ctx.hooks["pre_gateway_dispatch"][0]
    assert run(guard(tg_event(ADMIN, "salom"))) is None
    stranger = run(guard(tg_event(500, "salom")))
    assert stranger["action"] == "skip"
    assert run(guard(tg_event(500, "hi", platform=Platform.DISCORD))) is None


@pytest.mark.parametrize("patch", [
    {"role": "root"}, {"role": None}, {"admin_telegram_id": None}, {"admin_telegram_id": 0},
    {"admin_telegram_id": True}, {"db_path": "relative/subs.db"}, {"daily_limit": -1}, {"daily_limit": "50"},
    {"subscriber_profile": "default"}, {"subscriber_profile": "../x"}, {"allowed_tools": "terminal"},
    {"outbox_interval": 0}, {"db_timeout": 1000},
])
def test_settings_validation(db_path, patch):
    data = {**base_settings(db_path, "gate"), **patch}
    with pytest.raises(SettingsError):
        from_mapping(data)


def test_settings_defaults(db_path):
    s = from_mapping({"role": "gate", "admin_telegram_id": str(ADMIN), "db_path": str(db_path)})
    assert s.admin_id == ADMIN and s.daily_limit == 50 and "web_search" in s.allowed_tools
    assert "terminal" not in s.allowed_tools and "model" not in s.allowed_commands


def test_compat_pairing_check():
    assert hermes_compat.check_pairing_api(FakePairingStore()) is None
    assert hermes_compat.check_pairing_api(None)
    assert hermes_compat.check_pairing_api(object())

    class WrongSig(FakePairingStore):
        def _approve_user(self, uid, plat):  # noqa: D401 - noto'g'ri imzo
            pass

    assert "kutilmagan" in hermes_compat.check_pairing_api(WrongSig())
