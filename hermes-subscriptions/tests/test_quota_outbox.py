import sqlite3
import threading

import pytest

from conftest import ADMIN, OTHER, FakeClock, make_user
from subscriptions import db
from subscriptions.models import PermissionDenied, Status, UserNotFound, ValidationError
from subscriptions.service import SubscriptionService
from subscriptions.timeutil import DAY, Duration


def test_daily_limit_exactly_50(svc):
    make_user(svc, 42)
    svc.add_time(42, Duration(1, "day"), ADMIN)
    results = [svc.consume_message(42, 50) for _ in range(52)]
    assert [r.allowed for r in results] == [True] * 50 + [False, False]
    assert results[49].used == 50 and results[50].used == 50 and results[50].limit == 50
    assert svc.messages_today(42) == 50
    assert svc.get_user(42).message_count == 50


def test_limit_resets_next_local_day(svc, clock):
    make_user(svc, 42)
    for _ in range(3):
        svc.consume_message(42, 3)
    assert not svc.consume_message(42, 3).allowed
    clock.advance(DAY)
    assert svc.consume_message(42, 3).allowed
    assert svc.messages_today(42) == 1


def test_personal_limit_and_unlimited(svc):
    make_user(svc, 42)
    make_user(svc, 43)
    svc.set_daily_limit(42, 2, ADMIN)
    svc.set_daily_limit(43, 0, ADMIN)  # cheklanmagan
    assert [svc.consume_message(42, 50).allowed for _ in range(3)] == [True, True, False]
    assert all(svc.consume_message(43, 1).allowed for _ in range(5))
    svc.set_daily_limit(42, None, ADMIN)  # umumiy limitga qaytdi
    assert svc.consume_message(42, 50).allowed
    with pytest.raises(PermissionDenied):
        svc.set_daily_limit(42, 10, OTHER)
    for bad in (-1, True, 1.5, 100_001):
        with pytest.raises(ValidationError):
            svc.set_daily_limit(42, bad, ADMIN)  # type: ignore[arg-type]


def test_consume_validation(svc):
    with pytest.raises(UserNotFound):
        svc.consume_message(777, 50)
    make_user(svc, 42)
    for bad in (-1, True, "50"):
        with pytest.raises(ValidationError):
            svc.consume_message(42, bad)  # type: ignore[arg-type]


def test_quota_never_exceeded_under_concurrency(db_path):
    clock = FakeClock()
    setup = SubscriptionService(db_path, [ADMIN], clock=clock)
    make_user(setup, 42)
    allowed = []
    lock = threading.Lock()

    def worker():
        local = SubscriptionService(db_path, [ADMIN], clock=clock)
        for _ in range(10):
            ok = local.consume_message(42, 50).allowed
            with lock:
                allowed.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == 80 and sum(allowed) == 50
    assert setup.messages_today(42) == 50


def test_outbox_dedupe_and_retry(svc):
    assert svc.enqueue_message(42, "expired", "Obunangiz tugadi", dedupe_key="exp:42:1") is True
    assert svc.enqueue_message(42, "expired", "Obunangiz tugadi", dedupe_key="exp:42:1") is False
    assert svc.enqueue_message(ADMIN, "admin", "Hisobot") is True
    pending = svc.fetch_outbox()
    assert [m.chat_id for m in pending] == [42, ADMIN]
    svc.mark_outbox_sent(pending[0].id)
    for _ in range(SubscriptionService.OUTBOX_MAX_ATTEMPTS):
        svc.mark_outbox_failed(pending[1].id, "network")
    assert svc.fetch_outbox() == []  # yuborilgan va urinishlari tugagan xabarlar qaytmaydi
    for bad in ((0, "k", "t"), (42, "k", "  "), (42, "", "t"), (True, "k", "t")):
        with pytest.raises(ValidationError):
            svc.enqueue_message(*bad)  # type: ignore[arg-type]


def test_migration_v1_to_v2_keeps_data(db_path, clock):
    # v1 sxemasini qo'lda yaratib, eski ma'lumot bilan to'ldiramiz
    con = db.connect(db_path)
    with db.transaction(con):
        for statement in db.MIGRATIONS[1]:
            con.execute(statement)
        con.execute("PRAGMA user_version = 1")
        con.execute("INSERT INTO users (telegram_id, first_name, status, created_at, updated_at, sub_started_at, "
                    "sub_expires_at) VALUES (42, 'Eski', 'ACTIVE', 1, 1, 1, ?)", (clock.now + DAY,))
    con.close()
    svc = SubscriptionService(db_path, [ADMIN], clock=clock)
    user = svc.get_user(42)
    assert user.first_name == "Eski" and user.status is Status.ACTIVE and user.daily_limit is None
    assert svc.check_access(42).allowed
    con = db.connect(db_path)
    try:
        assert db.schema_version(con) == 2 and db.integrity_ok(con)
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE users SET daily_limit = -5 WHERE telegram_id = 42")
    finally:
        con.close()
