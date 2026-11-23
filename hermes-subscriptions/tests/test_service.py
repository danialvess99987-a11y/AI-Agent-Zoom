import os
import sqlite3
import stat
import threading

import pytest

from conftest import ADMIN, OTHER, FakeClock, make_user
from subscriptions import db
from subscriptions.models import (
    InvalidTransition,
    PendingActionError,
    PermissionDenied,
    Status,
    TelegramUserInfo,
    UserNotFound,
    ValidationError,
)
from subscriptions.service import PENDING_TTL_SECONDS, SubscriptionService
from subscriptions.timeutil import DAY, HOUR, Duration


# --- 1, 3: yangi foydalanuvchi va tasdiqlanmagan foydalanuvchi ---------------------------------
def test_register_new_user_is_pending(svc):
    user, is_new = svc.register_start(TelegramUserInfo(42, "Dilshod", "Karimov", "dilshod"))
    assert is_new and user.status is Status.PENDING
    again, is_new2 = svc.register_start(TelegramUserInfo(42, "Dilshod", "K.", "dilshod_new"))
    assert not is_new2 and again.last_name == "K." and again.username == "dilshod_new"
    assert again.created_at == user.created_at


def test_access_denied_until_subscription(svc):
    make_user(svc, 42)
    for tid, reason in ((42, "PENDING"), (999, "NOT_REGISTERED"), (None, "NOT_REGISTERED"), (-3, "NOT_REGISTERED")):
        decision = svc.check_access(tid)
        assert not decision.allowed and decision.reason == reason


def test_admin_always_allowed(svc):
    decision = svc.check_access(ADMIN)
    assert decision.allowed and decision.reason == "ADMIN"


# --- 5, 6: ikki soatlik obuna va muddat tugashi --------------------------------------------------
def test_two_hour_subscription_and_expiry(svc, clock):
    make_user(svc, 42)
    result = svc.add_time(42, Duration(2, "hour"), ADMIN)
    assert result.mode == "new" and result.after.status is Status.ACTIVE
    assert result.after.sub_expires_at == clock.now + 2 * HOUR
    assert svc.check_access(42).allowed
    clock.advance(2 * HOUR - 1)
    assert svc.check_access(42).allowed
    clock.advance(1)
    decision = svc.check_access(42)
    assert not decision.allowed and decision.reason == "EXPIRED"
    expired = svc.expire_due()
    assert [u.telegram_id for u in expired] == [42] and expired[0].status is Status.EXPIRED
    assert svc.expire_due() == []  # takroriy chaqiruv hech narsa qilmaydi
    assert svc.history(42)[0]["action"] == "EXPIRE"


# --- 7: uzaytirish qoidasi -----------------------------------------------------------------------
def test_extend_active_adds_to_expiry_not_now(svc, clock):
    make_user(svc, 42)
    first = svc.add_time(42, Duration(5, "day"), ADMIN).after.sub_expires_at
    clock.advance(DAY)
    result = svc.add_time(42, Duration(3, "hour"), ADMIN)
    assert result.mode == "extend" and result.after.sub_expires_at == first + 3 * HOUR
    assert result.delta_seconds == 3 * HOUR


def test_add_after_expiry_starts_from_now(svc, clock):
    make_user(svc, 42)
    svc.add_time(42, Duration(1, "hour"), ADMIN)
    clock.advance(5 * HOUR)  # expire_due hali ishlamagan
    result = svc.add_time(42, Duration(2, "day"), ADMIN)
    assert result.mode == "new" and result.after.sub_expires_at == clock.now + 2 * DAY
    actions = [h["action"] for h in svc.history(42)]
    assert actions[:2] == ["ADD_TIME", "EXPIRE"]  # avval tugash qayd etildi


def test_suspend_freezes_and_resume_restores(svc, clock):
    make_user(svc, 42)
    svc.add_time(42, Duration(10, "hour"), ADMIN)
    clock.advance(4 * HOUR)
    suspended = svc.suspend(42, ADMIN, reason="tekshiruv").after
    assert suspended.status is Status.SUSPENDED and suspended.suspended_remaining == 6 * HOUR
    assert not svc.check_access(42).allowed
    clock.advance(3 * DAY)  # to'xtatilgan vaqt sarflanmaydi
    assert svc.expire_due() == []
    svc.add_time(42, Duration(1, "hour"), ADMIN)  # to'xtatilgan holatda qo'shish
    assert svc.get_user(42).suspended_remaining == 7 * HOUR
    resumed = svc.resume(42, ADMIN).after
    assert resumed.status is Status.ACTIVE and resumed.sub_expires_at == clock.now + 7 * HOUR


def test_revoke_ban_unban(svc):
    make_user(svc, 42)
    svc.add_time(42, Duration(1, "day"), ADMIN)
    assert svc.revoke(42, ADMIN, reason="so'rov bo'yicha").after.status is Status.APPROVED
    assert not svc.check_access(42).allowed
    svc.add_time(42, Duration(1, "day"), ADMIN)
    assert svc.ban(42, ADMIN).after.status is Status.BANNED
    assert not svc.check_access(42).allowed
    with pytest.raises(InvalidTransition):
        svc.add_time(42, Duration(1, "day"), ADMIN)
    assert svc.unban(42, ADMIN).after.status is Status.APPROVED


def test_approve_reject(svc):
    make_user(svc, 42)
    assert svc.reject(42, ADMIN, reason="spam").after.status is Status.REJECTED
    assert svc.approve(42, ADMIN).after.status is Status.APPROVED
    assert not svc.check_access(42).allowed  # tasdiqlangan, lekin obunasi yo'q
    with pytest.raises(InvalidTransition):
        svc.resume(42, ADMIN)


def test_cannot_ban_admin(svc):
    make_user(svc, ADMIN, "Admin")
    with pytest.raises(ValidationError):
        svc.ban(ADMIN, ADMIN)


def test_unknown_user_raises(svc):
    with pytest.raises(UserNotFound):
        svc.add_time(404, Duration(1, "day"), ADMIN)


# --- 9: oddiy foydalanuvchi admin amallarini bajara olmaydi --------------------------------------
@pytest.mark.parametrize("call", [
    lambda s: s.add_time(42, Duration(1, "day"), OTHER),
    lambda s: s.add_time(42, Duration(1, "day"), None),
    lambda s: s.add_time(42, Duration(1, "day"), True),
    lambda s: s.approve(42, OTHER), lambda s: s.reject(42, OTHER), lambda s: s.suspend(42, OTHER),
    lambda s: s.resume(42, OTHER), lambda s: s.revoke(42, OTHER), lambda s: s.ban(42, OTHER),
    lambda s: s.unban(42, OTHER), lambda s: s.add_time_bulk(Duration(1, "day"), OTHER, request_id="x"),
    lambda s: s.create_pending("approve", {"target": 42}, "x", OTHER),
    lambda s: s.confirm_pending("abc", OTHER), lambda s: s.cancel_pending("abc", OTHER),
    lambda s: s.add_time(42, Duration(1, "day"), 42),  # o'ziga o'zi obuna bera olmaydi
])
def test_non_admin_cannot_mutate(svc, call):
    make_user(svc, 42)
    with pytest.raises(PermissionDenied):
        call(svc)
    assert svc.get_user(42).status is Status.PENDING


def test_admin_ids_validated(db_path):
    for bad in ([], [0], [-1], [True], ["111"]):
        with pytest.raises(ValidationError):
            SubscriptionService(db_path, bad)


# --- 8: bir xil ismlar ---------------------------------------------------------------------------
def test_find_users_disambiguation(svc):
    make_user(svc, 1, "Jasur", "Aliyev", "jasur_a")
    make_user(svc, 2, "Jasur", "Karimov", None)
    make_user(svc, 3, "Alisher", "", "alisher")
    make_user(svc, 4, "Ali", "Valiyev", None)
    make_user(svc, 5, "Gʻayrat", "", None)
    assert sorted(u.telegram_id for u in svc.find_users("Jasur")) == [1, 2]
    assert [u.telegram_id for u in svc.find_users("jasur karimov")] == [2]
    assert [u.telegram_id for u in svc.find_users("@jasur_a")] == [1]
    assert [u.telegram_id for u in svc.find_users("2")] == [2]
    assert [u.telegram_id for u in svc.find_users("Ali")] == [4]  # "Alisher" emas — so'z to'liq mos
    assert [u.telegram_id for u in svc.find_users("G'ayrat")] == [5]
    assert svc.find_users("Nodir") == []
    assert svc.find_users("100%_") == []  # LIKE belgilari xavfsiz
    with pytest.raises(ValidationError):
        svc.find_users("  ")


# --- idempotentlik va tasdiqlash ----------------------------------------------------------------
def test_request_id_idempotent(svc, clock):
    make_user(svc, 42)
    first = svc.add_time(42, Duration(1, "day"), ADMIN, request_id="req-1")
    second = svc.add_time(42, Duration(1, "day"), ADMIN, request_id="req-1")
    assert second.replayed and second.after.sub_expires_at == first.after.sub_expires_at
    assert len(svc.history(42)) == 1


def test_pending_confirm_once(svc):
    make_user(svc, 42, "Dilshod")
    pending = svc.create_pending("add_time", {"target": 42, "amount": 2, "unit": "soat"}, "Dilshod: +2 soat", ADMIN)
    assert pending.payload == {"target": 42, "amount": 2, "unit": "hour"} and len(pending.id) == 12
    assert svc.get_user(42).status is Status.PENDING  # tasdiqlanmaguncha o'zgarmaydi
    results = svc.confirm_pending(pending.id, ADMIN)
    assert len(results) == 1 and results[0].after.status is Status.ACTIVE
    with pytest.raises(PendingActionError):
        svc.confirm_pending(pending.id, ADMIN)  # ikkinchi bosish
    assert len(svc.history(42)) == 1
    assert svc.get_pending(pending.id).state == "confirmed"


def test_pending_expires_and_cancel(svc, clock):
    make_user(svc, 42)
    p1 = svc.create_pending("approve", {"target": 42}, "x", ADMIN)
    clock.advance(PENDING_TTL_SECONDS)
    with pytest.raises(PendingActionError):
        svc.confirm_pending(p1.id, ADMIN)
    assert svc.get_pending(p1.id).state == "expired"
    p2 = svc.create_pending("approve", {"target": 42}, "x", ADMIN)
    assert svc.cancel_pending(p2.id, ADMIN) is True
    assert svc.cancel_pending(p2.id, ADMIN) is False
    with pytest.raises(PendingActionError):
        svc.confirm_pending(p2.id, ADMIN)
    assert svc.get_user(42).status is Status.PENDING


def test_pending_validation(svc):
    make_user(svc, 42)
    with pytest.raises(ValidationError):
        svc.create_pending("delete_everything", {"target": 42}, "x", ADMIN)
    with pytest.raises(ValidationError):
        svc.create_pending("add_time", {"target": 42, "amount": 0, "unit": "day"}, "x", ADMIN)
    with pytest.raises(ValidationError):
        svc.create_pending("add_time", {"target": "42", "amount": 1, "unit": "day"}, "x", ADMIN)
    with pytest.raises(UserNotFound):
        svc.create_pending("approve", {"target": 777}, "x", ADMIN)
    with pytest.raises(InvalidTransition):
        svc.create_pending("resume", {"target": 42}, "x", ADMIN)


def test_pending_marks_failed_when_state_changed(svc):
    make_user(svc, 42)
    svc.add_time(42, Duration(1, "day"), ADMIN)
    pending = svc.create_pending("suspend", {"target": 42}, "x", ADMIN)
    svc.ban(42, ADMIN)  # tasdiqdan oldin holat o'zgardi
    with pytest.raises(InvalidTransition):
        svc.confirm_pending(pending.id, ADMIN)
    assert svc.get_pending(pending.id).state == "failed"


def test_bulk_extend_only_active(svc, clock):
    for tid in (1, 2, 3, 4):
        make_user(svc, tid)
    svc.add_time(1, Duration(1, "day"), ADMIN)
    svc.add_time(2, Duration(2, "day"), ADMIN)
    svc.add_time(3, Duration(1, "hour"), ADMIN)
    clock.advance(2 * HOUR)  # 3 tugadi, 4 umuman obunasiz
    before = {tid: svc.get_user(tid).sub_expires_at for tid in (1, 2)}
    pending = svc.create_pending("add_time_bulk", {"amount": 3, "unit": "day"}, "Barcha faollarga +3 kun", ADMIN)
    results = svc.confirm_pending(pending.id, ADMIN)
    assert sorted(r.telegram_id for r in results) == [1, 2]
    for tid in (1, 2):
        assert svc.get_user(tid).sub_expires_at == before[tid] + 3 * DAY
    assert svc.get_user(3).effective_status(clock.now) is Status.EXPIRED
    assert svc.get_user(4).status is Status.PENDING


# --- 8-bo'lim: statistika ------------------------------------------------------------------------
def test_queries_and_stats(svc, clock):
    for tid in (1, 2, 3):
        make_user(svc, tid, f"U{tid}")
    svc.add_time(1, Duration(10, "hour"), ADMIN)
    svc.add_time(2, Duration(5, "day"), ADMIN)
    for _ in range(5):
        svc.record_activity(1)
    svc.record_activity(2)
    svc.record_activity(999)  # ro'yxatda yo'q — hech narsa yozilmaydi
    assert [u.telegram_id for u in svc.expiring_within(DAY)] == [1]
    assert [(u.telegram_id, n) for u, n in svc.top_users(7)] == [(1, 5), (2, 1)]
    assert svc.get_user(1).message_count == 5
    stats = svc.stats()
    assert stats["total_users"] == 3 and stats["active_subscriptions"] == 2
    assert stats["by_status"]["PENDING"] == 1 and stats["expiring_24h"] == 1 and stats["new_today"] == 3
    clock.advance(8 * DAY)
    assert {u.telegram_id for u in svc.list_users(Status.EXPIRED)} == {1, 2}
    assert svc.top_users(7) == []
    assert len(svc.new_users_since(clock.now - DAY)) == 0
    assert [u.telegram_id for u in svc.list_users(Status.PENDING)] == [3]


def test_inactive_and_seen(svc, clock):
    make_user(svc, 1)
    make_user(svc, 2)
    svc.add_time(1, Duration(30, "day"), ADMIN)
    svc.add_time(2, Duration(30, "day"), ADMIN)
    clock.advance(8 * DAY)
    svc.record_activity(2)
    assert [u.telegram_id for u in svc.inactive_active_since(clock.now - 7 * DAY)] == [1]
    assert [u.telegram_id for u in svc.seen_since(clock.now - DAY)] == [2]


def test_notification_dedupe(svc):
    assert svc.claim_notification("expired:42:1000") is True
    assert svc.claim_notification("expired:42:1000") is False
    with pytest.raises(ValidationError):
        svc.claim_notification("")


def test_audit_written(svc):
    make_user(svc, 42)
    svc.add_time(42, Duration(1, "day"), ADMIN)
    actions = [a["action"] for a in svc.audit_tail()]
    assert actions[:2] == ["SUB_ADD_TIME", "USER_REGISTERED"]


# --- 10: restart'dan keyin ma'lumotlar saqlanadi ---------------------------------------------------
def test_persistence_across_restart(db_path, clock):
    first = SubscriptionService(db_path, [ADMIN], clock=clock)
    make_user(first, 42, "Dilshod")
    first.add_time(42, Duration(2, "hour"), ADMIN)
    del first
    second = SubscriptionService(db_path, [ADMIN], clock=clock)
    user = second.get_user(42)
    assert user.status is Status.ACTIVE and user.sub_expires_at == clock.now + 2 * HOUR
    assert second.check_access(42).allowed


def test_db_file_permissions_and_wal(db_path, svc):
    mode = stat.S_IMODE(os.stat(db_path).st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(os.stat(db_path.parent).st_mode) == 0o700
    con = db.connect(db_path)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.integrity_ok(con)
        assert db.schema_version(con) == db.SCHEMA_VERSION
    finally:
        con.close()


def test_refuses_newer_schema(db_path):
    con = db.connect(db_path)
    con.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    con.close()
    with pytest.raises(db.SchemaTooNew):
        SubscriptionService(db_path, [ADMIN])


def test_check_constraint_blocks_bad_status(db_path, svc):
    con = db.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO users (telegram_id, status, created_at, updated_at) VALUES (5, 'GOD', 0, 0)")
    finally:
        con.close()


# --- 11: bir vaqtda ko'p murojaat ----------------------------------------------------------------
def test_concurrent_writes_are_consistent(db_path):
    clock = FakeClock()
    setup = SubscriptionService(db_path, [ADMIN], clock=clock)
    make_user(setup, 42)
    setup.add_time(42, Duration(1, "hour"), ADMIN)
    start_expiry = setup.get_user(42).sub_expires_at
    errors = []

    def extend_worker():
        local = SubscriptionService(db_path, [ADMIN], clock=clock)
        try:
            for _ in range(25):
                local.add_time(42, Duration(1, "hour"), ADMIN)
        except Exception as exc:  # pragma: no cover - testda xato bo'lsa yig'amiz
            errors.append(exc)

    def register_worker(base):
        local = SubscriptionService(db_path, [ADMIN], clock=clock)
        try:
            for i in range(25):
                local.register_start(TelegramUserInfo(base + i, f"User{base + i}"))
                local.record_activity(base + i)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=extend_worker) for _ in range(8)]
    threads += [threading.Thread(target=register_worker, args=(10_000 + k * 100,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert setup.get_user(42).sub_expires_at == start_expiry + 200 * HOUR
    assert len(setup.history(42, limit=500)) == 201
    assert setup.stats()["total_users"] == 101
    con = db.connect(db_path)
    try:
        assert db.integrity_ok(con)
    finally:
        con.close()
