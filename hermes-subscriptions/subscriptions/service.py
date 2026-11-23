"""Obuna va foydalanuvchilarni boshqarish biznes mantig'i (Hermes'ga bog'liq emas).

Xavfsizlik qoidalari:
* Holatni o'zgartiruvchi har bir amal administrator ID'si bilan chaqirilishi shart (PermissionDenied).
* Administrator ro'yxati faqat konstruktorga beriladi (serverdagi ishonchli sozlamadan) — hech qachon
  suhbatdan yoki foydalanuvchi ismidan aniqlanmaydi.
* Har bir o'zgarish bitta tranzaksiyada: users + subscription_events + audit_log birga yoziladi.
* request_id bilan takroriy so'rov qayta qo'llanmaydi (idempotentlik).
* Tasdiqlash (pending action) holati va amalning o'zi bitta tranzaksiyada — ikki marta bosish
  ikki marta bajarmaydi.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from . import db
from .models import (
    AccessDecision,
    Action,
    ChangeResult,
    InvalidTransition,
    PendingAction,
    PendingActionError,
    PermissionDenied,
    Status,
    TelegramUserInfo,
    User,
    UserNotFound,
    ValidationError,
    next_status,
    normalize_name,
)
from .timeutil import DAY, Duration, DurationError, local_day, local_day_end, local_day_start, utc_now

PENDING_TTL_SECONDS = 15 * 60
MAX_REASON_LENGTH = 500
SYSTEM_ACTOR = None  # muddat tugashi kabi avtomatik amallar

# Tasdiqlanadigan amal turlari -> Action
_PENDING_KINDS = {
    "add_time": Action.ADD_TIME,
    "approve": Action.APPROVE,
    "reject": Action.REJECT,
    "suspend": Action.SUSPEND,
    "resume": Action.RESUME,
    "revoke": Action.REVOKE,
    "ban": Action.BAN,
    "unban": Action.UNBAN,
    "add_time_bulk": Action.ADD_TIME,
}

_USER_COLUMNS = (
    "telegram_id, first_name, last_name, username, status, created_at, updated_at, last_seen_at, "
    "sub_started_at, sub_expires_at, suspended_remaining, status_reason, approved_by, message_count"
)
_USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{3,31}$")


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        telegram_id=int(row["telegram_id"]),
        first_name=row["first_name"],
        last_name=row["last_name"],
        username=row["username"],
        status=Status(row["status"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        last_seen_at=row["last_seen_at"],
        sub_started_at=row["sub_started_at"],
        sub_expires_at=row["sub_expires_at"],
        suspended_remaining=row["suspended_remaining"],
        status_reason=row["status_reason"],
        approved_by=row["approved_by"],
        message_count=int(row["message_count"]),
    )


def _search_key(first_name: str, last_name: str, username: Optional[str]) -> str:
    return normalize_name(" ".join(part for part in (first_name, last_name, username or "") if part))


def _clean_reason(reason: Optional[str]) -> Optional[str]:
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise ValidationError("Sabab matn bo'lishi kerak")
    text = reason.strip()
    if len(text) > MAX_REASON_LENGTH:
        raise ValidationError(f"Sabab {MAX_REASON_LENGTH} belgidan oshmasligi kerak")
    return text or None


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SubscriptionService:
    def __init__(
        self,
        db_path: str | Path,
        admin_ids: Iterable[int],
        clock: Callable[[], int] = utc_now,
    ) -> None:
        admins = set()
        for value in admin_ids:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValidationError("Administrator ID musbat butun son bo'lishi kerak")
            admins.add(value)
        if not admins:
            raise ValidationError("Kamida bitta administrator ID kerak")
        self._admins = frozenset(admins)
        self._db_path = Path(db_path)
        self._clock = clock
        with closing(db.connect(self._db_path)) as con:
            db.migrate(con)

    # ------------------------------------------------------------------ infrastruktura
    @property
    def db_path(self) -> Path:
        return self._db_path

    def now(self) -> int:
        return int(self._clock())

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with closing(db.connect(self._db_path)) as con:
            yield con

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(db.connect(self._db_path)) as con:
            with db.transaction(con):
                yield con

    def is_admin(self, telegram_id: Optional[int]) -> bool:
        return isinstance(telegram_id, int) and not isinstance(telegram_id, bool) and telegram_id in self._admins

    def _require_admin(self, actor_id: Optional[int]) -> int:
        if not self.is_admin(actor_id):
            raise PermissionDenied("Bu amal faqat administrator uchun")
        return int(actor_id)  # type: ignore[arg-type]

    @staticmethod
    def _fetch_user(con: sqlite3.Connection, telegram_id: int) -> Optional[User]:
        row = con.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return _row_to_user(row) if row else None

    def _require_user(self, con: sqlite3.Connection, telegram_id: int) -> User:
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int):
            raise ValidationError("Telegram ID butun son bo'lishi kerak")
        user = self._fetch_user(con, telegram_id)
        if user is None:
            raise UserNotFound(f"Foydalanuvchi topilmadi: {telegram_id}")
        return user

    @staticmethod
    def _audit(con: sqlite3.Connection, now: int, actor_id: Optional[int], action: str,
               target_id: Optional[int], details: dict) -> None:
        con.execute(
            "INSERT INTO audit_log (created_at, actor_id, action, target_id, details) VALUES (?, ?, ?, ?, ?)",
            (now, actor_id, action, target_id, json.dumps(details, ensure_ascii=False, sort_keys=True)),
        )

    # ------------------------------------------------------------------ kirish (gateway)
    def register_start(self, info: TelegramUserInfo) -> tuple[User, bool]:
        """/start yoki birinchi xabar: yangi bo'lsa PENDING yaratadi. (user, is_new) qaytaradi."""
        if not isinstance(info, TelegramUserInfo):
            raise ValidationError("TelegramUserInfo kutilgan")
        now = self.now()
        key = _search_key(info.first_name, info.last_name, info.username)
        with self._write() as con:
            existing = self._fetch_user(con, info.telegram_id)
            if existing is None:
                con.execute(
                    "INSERT INTO users (telegram_id, first_name, last_name, username, search_key, status, "
                    "created_at, updated_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (info.telegram_id, info.first_name, info.last_name, info.username, key,
                     Status.PENDING.value, now, now, now),
                )
                self._audit(con, now, info.telegram_id, "USER_REGISTERED", info.telegram_id,
                            {"username": info.username})
                return self._fetch_user(con, info.telegram_id), True  # type: ignore[return-value]
            con.execute(
                "UPDATE users SET first_name = ?, last_name = ?, username = ?, search_key = ?, "
                "last_seen_at = ?, updated_at = ? WHERE telegram_id = ?",
                (info.first_name, info.last_name, info.username, key, now, now, info.telegram_id),
            )
            return self._fetch_user(con, info.telegram_id), False  # type: ignore[return-value]

    def record_activity(self, telegram_id: int) -> None:
        """Agentga o'tkazilgan xabar statistikasi (faqat ro'yxatdagi foydalanuvchilar uchun)."""
        now = self.now()
        with self._write() as con:
            cur = con.execute(
                "UPDATE users SET last_seen_at = ?, message_count = message_count + 1 WHERE telegram_id = ?",
                (now, telegram_id),
            )
            if cur.rowcount:
                con.execute(
                    "INSERT INTO activity_daily (telegram_id, day, messages) VALUES (?, ?, 1) "
                    "ON CONFLICT (telegram_id, day) DO UPDATE SET messages = messages + 1",
                    (telegram_id, local_day(now)),
                )

    def touch_seen(self, telegram_id: int) -> None:
        """Ruxsati yo'q foydalanuvchining urinishi ham oxirgi faollik sifatida qayd etiladi."""
        now = self.now()
        with self._write() as con:
            con.execute("UPDATE users SET last_seen_at = ? WHERE telegram_id = ?", (now, telegram_id))

    def check_access(self, telegram_id: Optional[int]) -> AccessDecision:
        """AI agentga xabar o'tkazish mumkinmi? Faqat o'qiydi, tez."""
        if self.is_admin(telegram_id):
            return AccessDecision(True, "ADMIN", None)
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
            return AccessDecision(False, "NOT_REGISTERED", None)
        with self._read() as con:
            user = self._fetch_user(con, telegram_id)
        if user is None:
            return AccessDecision(False, "NOT_REGISTERED", None)
        status = user.effective_status(self.now())
        return AccessDecision(status is Status.ACTIVE, status.value, user)

    # ------------------------------------------------------------------ so'rovlar
    def get_user(self, telegram_id: int) -> Optional[User]:
        with self._read() as con:
            return self._fetch_user(con, telegram_id)

    def find_users(self, query: str, limit: int = 10) -> list[User]:
        """ID, @username yoki ism bo'yicha qidiruv. Bir nechta natija — administrator tanlaydi."""
        if not isinstance(query, str) or not query.strip():
            raise ValidationError("Qidiruv so'rovi bo'sh")
        limit = max(1, min(int(limit), 50))
        text = query.strip()
        with self._read() as con:
            if text.isdigit():
                user = self._fetch_user(con, int(text))
                return [user] if user else []
            if text.startswith("@") and _USERNAME_RE.match(text):
                rows = con.execute(
                    f"SELECT {_USER_COLUMNS} FROM users WHERE username = ? COLLATE NOCASE LIMIT ?",
                    (text.lstrip("@"), limit),
                ).fetchall()
                return [_row_to_user(r) for r in rows]
            needle = normalize_name(text)
            if not needle:
                return []
            rows = con.execute(
                f"SELECT {_USER_COLUMNS}, search_key FROM users WHERE search_key LIKE ? ESCAPE '\\' "
                "ORDER BY created_at",
                (f"%{_like_escape(needle)}%",),
            ).fetchall()
        tokens = needle.split(" ")

        def rank(row: sqlite3.Row) -> tuple:
            words = row["search_key"].split(" ")
            exact_words = all(token in words for token in tokens)
            exact_username = (row["username"] or "").casefold() == needle
            return (not exact_username, not exact_words, row["created_at"])

        ordered = sorted(rows, key=rank)
        # So'z to'liq mos kelganlar bo'lsa — faqat ularni qaytaramiz ("Ali" -> "Alisher" chiqmasin).
        exact = [r for r in ordered if all(t in r["search_key"].split(" ") for t in tokens)]
        chosen = exact or ordered
        return [_row_to_user(r) for r in chosen[:limit]]

    def list_users(self, status: Optional[Status] = None, limit: int = 100, offset: int = 0) -> list[User]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        now = self.now()
        with self._read() as con:
            if status is None:
                rows = con.execute(
                    f"SELECT {_USER_COLUMNS} FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
                ).fetchall()
                return [_row_to_user(r) for r in rows]
            rows = con.execute(f"SELECT {_USER_COLUMNS} FROM users ORDER BY created_at DESC").fetchall()
        users = [u for u in (_row_to_user(r) for r in rows) if u.effective_status(now) is status]
        return users[offset:offset + limit]

    def expiring_within(self, seconds: int) -> list[User]:
        now = self.now()
        with self._read() as con:
            rows = con.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE status = 'ACTIVE' AND sub_expires_at > ? "
                "AND sub_expires_at <= ? ORDER BY sub_expires_at",
                (now, now + int(seconds)),
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    def expiring_today(self) -> list[User]:
        now = self.now()
        return self.expiring_within(local_day_end(now) - now)

    def new_users_since(self, since: int) -> list[User]:
        with self._read() as con:
            rows = con.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE created_at >= ? ORDER BY created_at DESC", (int(since),)
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    def seen_since(self, since: int) -> list[User]:
        with self._read() as con:
            rows = con.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE last_seen_at >= ? ORDER BY last_seen_at DESC", (int(since),)
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    def inactive_active_since(self, since: int) -> list[User]:
        """Faol obunasi bor, lekin `since` dan beri foydalanmaganlar."""
        now = self.now()
        with self._read() as con:
            rows = con.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE status = 'ACTIVE' AND sub_expires_at > ? "
                "AND (last_seen_at IS NULL OR last_seen_at < ?) ORDER BY last_seen_at",
                (now, int(since)),
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    def top_users(self, days: int = 7, limit: int = 10) -> list[tuple[User, int]]:
        days = max(1, min(int(days), 366))
        limit = max(1, min(int(limit), 100))
        first_day = local_day(self.now() - (days - 1) * DAY)
        with self._read() as con:
            rows = con.execute(
                f"SELECT {', '.join('u.' + c.strip() for c in _USER_COLUMNS.split(','))}, SUM(a.messages) AS total "
                "FROM activity_daily a JOIN users u ON u.telegram_id = a.telegram_id "
                "WHERE a.day >= ? GROUP BY a.telegram_id ORDER BY total DESC, u.telegram_id LIMIT ?",
                (first_day, limit),
            ).fetchall()
        return [(_row_to_user(r), int(r["total"])) for r in rows]

    def history(self, telegram_id: int, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._read() as con:
            rows = con.execute(
                "SELECT id, action, old_status, new_status, old_expires_at, new_expires_at, delta_seconds, "
                "actor_id, reason, created_at FROM subscription_events WHERE telegram_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (telegram_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        now = self.now()
        with self._read() as con:
            rows = con.execute(f"SELECT {_USER_COLUMNS} FROM users").fetchall()
        users = [_row_to_user(r) for r in rows]
        by_status = {s.value: 0 for s in Status}
        for user in users:
            by_status[user.effective_status(now).value] += 1
        today_start = local_day_start(now)
        return {
            "total_users": len(users),
            "by_status": by_status,
            "active_subscriptions": by_status[Status.ACTIVE.value],
            "expiring_24h": sum(
                1 for u in users
                if u.effective_status(now) is Status.ACTIVE and u.sub_expires_at is not None
                and u.sub_expires_at <= now + DAY
            ),
            "new_today": sum(1 for u in users if u.created_at >= today_start),
            "seen_7d": sum(1 for u in users if u.last_seen_at is not None and u.last_seen_at >= now - 7 * DAY),
        }

    # ------------------------------------------------------------------ o'zgartirishlar (yadro)
    def _apply(
        self,
        con: sqlite3.Connection,
        telegram_id: int,
        action: Action,
        actor_id: Optional[int],
        *,
        duration: Optional[Duration] = None,
        reason: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> ChangeResult:
        now = self.now()
        user = self._require_user(con, telegram_id)

        if request_id is not None:
            done = con.execute(
                "SELECT action FROM subscription_events WHERE request_id = ? AND telegram_id = ?",
                (request_id, telegram_id),
            ).fetchone()
            if done is not None:
                return ChangeResult(telegram_id, action, user, user, None, None, replayed=True)

        before = user
        # ACTIVE, lekin muddati o'tgan — avval EXPIRE sifatida qayd etamiz.
        if user.status is Status.ACTIVE and user.effective_status(now) is Status.EXPIRED:
            self._write_change(con, user, Status.EXPIRED, now, SYSTEM_ACTOR, Action.EXPIRE,
                               expires_at=user.sub_expires_at, started_at=user.sub_started_at,
                               remaining=None, reason=None, request_id=None, delta=None,
                               approved_by=user.approved_by)
            user = self._fetch_user(con, telegram_id)  # type: ignore[assignment]

        current = user.status
        target = next_status(current, action)
        started_at, expires_at = user.sub_started_at, user.sub_expires_at
        remaining = user.suspended_remaining
        approved_by = user.approved_by
        mode: Optional[str] = None
        delta: Optional[int] = None
        reason = _clean_reason(reason)

        if action is Action.ADD_TIME:
            if duration is None:
                raise ValidationError("Muddat ko'rsatilmagan")
            if current is Status.SUSPENDED:
                delta = duration.seconds_from(now)
                remaining = (remaining or 0) + delta
                mode = "suspended_extend"
            elif current is Status.ACTIVE:
                new_expires = duration.add_to(int(expires_at))  # type: ignore[arg-type]
                delta = new_expires - int(expires_at)  # type: ignore[arg-type]
                expires_at = new_expires
                mode = "extend"
            else:
                started_at = now
                expires_at = duration.add_to(now)
                delta = expires_at - now
                remaining = None
                mode = "new"
                approved_by = approved_by or actor_id
        elif action is Action.SUSPEND:
            left = int(expires_at) - now  # type: ignore[arg-type]
            if left <= 0:
                raise InvalidTransition("Obuna muddati allaqachon tugagan")
            remaining, expires_at = left, None
        elif action is Action.RESUME:
            if not remaining or remaining <= 0:
                raise InvalidTransition("Muzlatilgan vaqt qolmagan")
            expires_at = now + int(remaining)
            delta = int(remaining)
            remaining = None
        elif action is Action.REVOKE:
            if expires_at is not None and expires_at > now:
                expires_at = now
            remaining = None
        elif action is Action.BAN:
            if expires_at is not None and expires_at > now:
                expires_at = now
            remaining = None
        elif action is Action.APPROVE:
            approved_by = actor_id
        elif action in (Action.REJECT, Action.UNBAN, Action.EXPIRE):
            pass
        else:  # pragma: no cover - barcha Action qiymatlari yuqorida qamrab olingan
            raise InvalidTransition(f"Qo'llab-quvvatlanmaydigan amal: {action.value}")

        stored_reason = reason if action in (Action.SUSPEND, Action.REVOKE, Action.BAN, Action.REJECT) else None
        self._write_change(con, user, target, now, actor_id, action, expires_at=expires_at,
                           started_at=started_at, remaining=remaining, reason=stored_reason,
                           request_id=request_id, delta=delta, approved_by=approved_by)
        after = self._fetch_user(con, telegram_id)
        return ChangeResult(telegram_id, action, before, after, mode, delta, replayed=False)  # type: ignore[arg-type]

    def _write_change(self, con: sqlite3.Connection, user: User, target: Status, now: int,
                      actor_id: Optional[int], action: Action, *, expires_at: Optional[int],
                      started_at: Optional[int], remaining: Optional[int], reason: Optional[str],
                      request_id: Optional[str], delta: Optional[int], approved_by: Optional[int]) -> None:
        con.execute(
            "UPDATE users SET status = ?, sub_started_at = ?, sub_expires_at = ?, suspended_remaining = ?, "
            "status_reason = ?, approved_by = ?, updated_at = ? WHERE telegram_id = ?",
            (target.value, started_at, expires_at, remaining, reason, approved_by, now, user.telegram_id),
        )
        con.execute(
            "INSERT INTO subscription_events (telegram_id, action, old_status, new_status, old_expires_at, "
            "new_expires_at, delta_seconds, actor_id, reason, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user.telegram_id, action.value, user.status.value, target.value, user.sub_expires_at,
             expires_at, delta, actor_id, reason, request_id, now),
        )
        self._audit(con, now, actor_id, f"SUB_{action.value}", user.telegram_id, {
            "old_status": user.status.value, "new_status": target.value,
            "old_expires_at": user.sub_expires_at, "new_expires_at": expires_at,
            "delta_seconds": delta, "request_id": request_id,
        })

    # ------------------------------------------------------------------ o'zgartirishlar (ochiq API)
    def add_time(self, telegram_id: int, duration: Duration, actor_id: int, *,
                 request_id: Optional[str] = None, reason: Optional[str] = None) -> ChangeResult:
        """Faol bo'lsa — tugash vaqtiga qo'shadi; aks holda hozirdan yangi obuna boshlaydi."""
        self._require_admin(actor_id)
        if not isinstance(duration, Duration):
            raise ValidationError("Duration kutilgan")
        with self._write() as con:
            return self._apply(con, telegram_id, Action.ADD_TIME, actor_id, duration=duration,
                               reason=reason, request_id=request_id)

    def _simple(self, action: Action, telegram_id: int, actor_id: int, reason: Optional[str],
                request_id: Optional[str]) -> ChangeResult:
        self._require_admin(actor_id)
        with self._write() as con:
            return self._apply(con, telegram_id, action, actor_id, reason=reason, request_id=request_id)

    def approve(self, telegram_id: int, actor_id: int, *, request_id: Optional[str] = None) -> ChangeResult:
        return self._simple(Action.APPROVE, telegram_id, actor_id, None, request_id)

    def reject(self, telegram_id: int, actor_id: int, *, reason: Optional[str] = None,
               request_id: Optional[str] = None) -> ChangeResult:
        return self._simple(Action.REJECT, telegram_id, actor_id, reason, request_id)

    def suspend(self, telegram_id: int, actor_id: int, *, reason: Optional[str] = None,
                request_id: Optional[str] = None) -> ChangeResult:
        return self._simple(Action.SUSPEND, telegram_id, actor_id, reason, request_id)

    def resume(self, telegram_id: int, actor_id: int, *, request_id: Optional[str] = None) -> ChangeResult:
        return self._simple(Action.RESUME, telegram_id, actor_id, None, request_id)

    def revoke(self, telegram_id: int, actor_id: int, *, reason: Optional[str] = None,
               request_id: Optional[str] = None) -> ChangeResult:
        return self._simple(Action.REVOKE, telegram_id, actor_id, reason, request_id)

    def ban(self, telegram_id: int, actor_id: int, *, reason: Optional[str] = None,
            request_id: Optional[str] = None) -> ChangeResult:
        if self.is_admin(telegram_id):
            raise ValidationError("Administratorni bloklab bo'lmaydi")
        return self._simple(Action.BAN, telegram_id, actor_id, reason, request_id)

    def unban(self, telegram_id: int, actor_id: int, *, request_id: Optional[str] = None) -> ChangeResult:
        return self._simple(Action.UNBAN, telegram_id, actor_id, None, request_id)

    def _bulk_targets(self, con: sqlite3.Connection, now: int) -> list[int]:
        rows = con.execute(
            "SELECT telegram_id FROM users WHERE status = 'ACTIVE' AND sub_expires_at > ? ORDER BY telegram_id",
            (now,),
        ).fetchall()
        return [int(r["telegram_id"]) for r in rows]

    def add_time_bulk(self, duration: Duration, actor_id: int, *, request_id: str) -> list[ChangeResult]:
        """Hozir faol bo'lgan barcha obunalarni uzaytiradi (bitta tranzaksiya)."""
        self._require_admin(actor_id)
        if not isinstance(request_id, str) or not request_id:
            raise ValidationError("Ommaviy amal uchun request_id majburiy")
        with self._write() as con:
            return [
                self._apply(con, tid, Action.ADD_TIME, actor_id, duration=duration, request_id=request_id)
                for tid in self._bulk_targets(con, self.now())
            ]

    def expire_due(self) -> list[User]:
        """Muddati o'tgan ACTIVE obunalarni EXPIRED ga o'tkazadi (scheduler chaqiradi)."""
        now = self.now()
        expired: list[User] = []
        with self._write() as con:
            rows = con.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE status = 'ACTIVE' AND "
                "(sub_expires_at IS NULL OR sub_expires_at <= ?)",
                (now,),
            ).fetchall()
            for row in rows:
                user = _row_to_user(row)
                self._write_change(con, user, Status.EXPIRED, now, SYSTEM_ACTOR, Action.EXPIRE,
                                   expires_at=user.sub_expires_at, started_at=user.sub_started_at,
                                   remaining=None, reason=None, request_id=None, delta=None,
                                   approved_by=user.approved_by)
                expired.append(self._fetch_user(con, user.telegram_id))  # type: ignore[arg-type]
        return expired

    # ------------------------------------------------------------------ tasdiqlash (pending)
    def create_pending(self, kind: str, payload: dict, summary: str, actor_id: int) -> PendingAction:
        """O'zgartiruvchi amalni darhol bajarmaydi — administrator tasdig'ini kutadi."""
        self._require_admin(actor_id)
        if kind not in _PENDING_KINDS:
            raise ValidationError(f"Noma'lum amal turi: {kind}")
        if not isinstance(payload, dict):
            raise ValidationError("payload lug'at bo'lishi kerak")
        if not isinstance(summary, str) or not summary.strip():
            raise ValidationError("summary bo'sh bo'lmasligi kerak")
        clean = self._validate_payload(kind, payload)
        now = self.now()
        pending_id = secrets.token_urlsafe(9)  # 12 belgi, Telegram callback_data uchun yetarlicha qisqa
        with self._write() as con:
            if kind != "add_time_bulk":
                user = self._require_user(con, clean["target"])
                # Hozirgi holatda amal mumkinligini oldindan tekshiramiz (aniq xato xabari uchun).
                next_status(user.effective_status(now), _PENDING_KINDS[kind])
            con.execute(
                "INSERT INTO pending_actions (id, created_at, expires_at, actor_id, kind, payload, summary, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (pending_id, now, now + PENDING_TTL_SECONDS, actor_id, kind,
                 json.dumps(clean, ensure_ascii=False, sort_keys=True), summary.strip()[:1000]),
            )
            self._audit(con, now, actor_id, "PENDING_CREATED", clean.get("target"),
                        {"pending_id": pending_id, "kind": kind})
        return PendingAction(pending_id, kind, clean, summary.strip()[:1000], actor_id, now,
                             now + PENDING_TTL_SECONDS, "pending")

    @staticmethod
    def _validate_payload(kind: str, payload: dict) -> dict:
        clean: dict = {}
        if kind != "add_time_bulk":
            target = payload.get("target")
            if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
                raise ValidationError("payload.target musbat Telegram ID bo'lishi kerak")
            clean["target"] = target
        if kind in ("add_time", "add_time_bulk"):
            try:
                duration = Duration.parse(payload.get("amount"), payload.get("unit"))
            except DurationError as exc:
                raise ValidationError(str(exc)) from exc
            clean.update(duration.to_dict())
        if kind in ("reject", "suspend", "revoke", "ban"):
            reason = _clean_reason(payload.get("reason"))
            if reason:
                clean["reason"] = reason
        return clean

    def get_pending(self, pending_id: str) -> Optional[PendingAction]:
        with self._read() as con:
            row = con.execute("SELECT * FROM pending_actions WHERE id = ?", (pending_id,)).fetchone()
        if row is None:
            return None
        return PendingAction(row["id"], row["kind"], json.loads(row["payload"]), row["summary"],
                             int(row["actor_id"]), int(row["created_at"]), int(row["expires_at"]), row["state"])

    def confirm_pending(self, pending_id: str, actor_id: int) -> list[ChangeResult]:
        """Tasdiqlash: holat tekshiruvi va amalning o'zi bitta tranzaksiyada."""
        self._require_admin(actor_id)
        now = self.now()
        try:
            with self._write() as con:
                row = con.execute("SELECT * FROM pending_actions WHERE id = ?", (pending_id,)).fetchone()
                if row is None:
                    raise PendingActionError("Bunday amal topilmadi")
                if row["state"] != "pending":
                    raise PendingActionError(f"Bu amal allaqachon yakunlangan ({row['state']})")
                if int(row["expires_at"]) <= now:
                    con.execute("UPDATE pending_actions SET state = 'expired', resolved_at = ? WHERE id = ?",
                                (now, pending_id))
                    expired_notice = True
                else:
                    expired_notice = False
                    results = self._execute_pending(con, row, actor_id)
                    con.execute(
                        "UPDATE pending_actions SET state = 'confirmed', resolved_at = ?, result = ? WHERE id = ?",
                        (now, json.dumps({"changed": [r.telegram_id for r in results if not r.replayed]}),
                         pending_id),
                    )
                    self._audit(con, now, actor_id, "PENDING_CONFIRMED", None, {"pending_id": pending_id})
            if expired_notice:
                raise PendingActionError("Tasdiqlash muddati o'tgan, amalni qaytadan so'rang")
            return results
        except (InvalidTransition, UserNotFound, ValidationError) as exc:
            with self._write() as con:
                con.execute(
                    "UPDATE pending_actions SET state = 'failed', resolved_at = ?, result = ? "
                    "WHERE id = ? AND state = 'pending'",
                    (now, json.dumps({"error": str(exc)}, ensure_ascii=False), pending_id),
                )
            raise

    def _execute_pending(self, con: sqlite3.Connection, row: sqlite3.Row, actor_id: int) -> list[ChangeResult]:
        kind = row["kind"]
        payload = json.loads(row["payload"])
        request_id = f"pending:{row['id']}"
        action = _PENDING_KINDS[kind]
        if kind == "add_time_bulk":
            duration = Duration(int(payload["amount"]), payload["unit"])
            return [
                self._apply(con, tid, Action.ADD_TIME, actor_id, duration=duration, request_id=request_id)
                for tid in self._bulk_targets(con, self.now())
            ]
        target = int(payload["target"])
        if action is Action.BAN and self.is_admin(target):
            raise ValidationError("Administratorni bloklab bo'lmaydi")
        duration = Duration(int(payload["amount"]), payload["unit"]) if action is Action.ADD_TIME else None
        return [self._apply(con, target, action, actor_id, duration=duration,
                            reason=payload.get("reason"), request_id=request_id)]

    def cancel_pending(self, pending_id: str, actor_id: int) -> bool:
        self._require_admin(actor_id)
        now = self.now()
        with self._write() as con:
            cur = con.execute(
                "UPDATE pending_actions SET state = 'cancelled', resolved_at = ? WHERE id = ? AND state = 'pending'",
                (now, pending_id),
            )
            if cur.rowcount:
                self._audit(con, now, actor_id, "PENDING_CANCELLED", None, {"pending_id": pending_id})
            return bool(cur.rowcount)

    def expire_stale_pending(self) -> int:
        now = self.now()
        with self._write() as con:
            cur = con.execute(
                "UPDATE pending_actions SET state = 'expired', resolved_at = ? WHERE state = 'pending' AND expires_at <= ?",
                (now, now),
            )
            return int(cur.rowcount)

    # ------------------------------------------------------------------ bildirishnomalar
    def claim_notification(self, key: str) -> bool:
        """Bir xil bildirishnoma ikki marta yuborilmasligi uchun: birinchi chaqiruvda True."""
        if not isinstance(key, str) or not key or len(key) > 200:
            raise ValidationError("Bildirishnoma kaliti noto'g'ri")
        with self._write() as con:
            cur = con.execute("INSERT OR IGNORE INTO notifications (key, created_at) VALUES (?, ?)",
                              (key, self.now()))
            return cur.rowcount == 1

    def audit_tail(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._read() as con:
            rows = con.execute(
                "SELECT id, created_at, actor_id, action, target_id, details FROM audit_log "
                "ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
