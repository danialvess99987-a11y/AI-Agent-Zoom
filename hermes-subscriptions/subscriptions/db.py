"""SQLite ulanishi, sxema va migratsiyalar.

* WAL rejimi, synchronous=FULL, foreign_keys=ON, busy_timeout=30s.
* Har bir yozish `BEGIN IMMEDIATE` tranzaksiyasida bajariladi (parallel yozuvlar navbatga turadi).
* Sxema versiyasi `PRAGMA user_version` da. Baza koddan yangiroq bo'lsa — ishlash rad etiladi.
* Baza fayli 0600 ruxsati bilan yaratiladi.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

_STATUS_CHECK = "('PENDING','APPROVED','ACTIVE','EXPIRED','SUSPENDED','REJECTED','BANNED')"

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        f"""
        CREATE TABLE users (
            telegram_id         INTEGER PRIMARY KEY CHECK (telegram_id > 0),
            first_name          TEXT    NOT NULL DEFAULT '',
            last_name           TEXT    NOT NULL DEFAULT '',
            username            TEXT,
            search_key          TEXT    NOT NULL DEFAULT '',
            status              TEXT    NOT NULL CHECK (status IN {_STATUS_CHECK}),
            created_at          INTEGER NOT NULL,
            updated_at          INTEGER NOT NULL,
            last_seen_at        INTEGER,
            sub_started_at      INTEGER,
            sub_expires_at      INTEGER,
            suspended_remaining INTEGER CHECK (suspended_remaining IS NULL OR suspended_remaining > 0),
            status_reason       TEXT,
            approved_by         INTEGER,
            message_count       INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0)
        )
        """,
        "CREATE INDEX idx_users_status_exp ON users (status, sub_expires_at)",
        "CREATE INDEX idx_users_username ON users (username COLLATE NOCASE)",
        "CREATE INDEX idx_users_created ON users (created_at)",
        "CREATE INDEX idx_users_last_seen ON users (last_seen_at)",
        """
        CREATE TABLE subscription_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id    INTEGER NOT NULL REFERENCES users (telegram_id),
            action         TEXT    NOT NULL,
            old_status     TEXT,
            new_status     TEXT    NOT NULL,
            old_expires_at INTEGER,
            new_expires_at INTEGER,
            delta_seconds  INTEGER,
            actor_id       INTEGER,
            reason         TEXT,
            request_id     TEXT,
            created_at     INTEGER NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX idx_events_request ON subscription_events (request_id, telegram_id) "
        "WHERE request_id IS NOT NULL",
        "CREATE INDEX idx_events_user ON subscription_events (telegram_id, created_at)",
        """
        CREATE TABLE activity_daily (
            telegram_id INTEGER NOT NULL REFERENCES users (telegram_id),
            day         TEXT    NOT NULL,
            messages    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (telegram_id, day)
        )
        """,
        "CREATE INDEX idx_activity_day ON activity_daily (day)",
        """
        CREATE TABLE audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL,
            actor_id   INTEGER,
            action     TEXT    NOT NULL,
            target_id  INTEGER,
            details    TEXT    NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX idx_audit_created ON audit_log (created_at)",
        """
        CREATE TABLE pending_actions (
            id          TEXT    PRIMARY KEY,
            created_at  INTEGER NOT NULL,
            expires_at  INTEGER NOT NULL,
            actor_id    INTEGER NOT NULL,
            kind        TEXT    NOT NULL,
            payload     TEXT    NOT NULL,
            summary     TEXT    NOT NULL,
            state       TEXT    NOT NULL CHECK (state IN ('pending','confirmed','cancelled','expired','failed')),
            resolved_at INTEGER,
            result      TEXT
        )
        """,
        "CREATE INDEX idx_pending_state ON pending_actions (state, expires_at)",
        """
        CREATE TABLE notifications (
            key        TEXT    PRIMARY KEY,
            created_at INTEGER NOT NULL
        )
        """,
    ),
}


class SchemaTooNew(RuntimeError):
    """Baza sxemasi koddan yangiroq — eski kod bilan ishlatish xavfli."""


def _ensure_file(path: Path) -> None:
    """Katalog (0700) va baza faylini (0600) yaratadi, agar ular hali yo'q bo'lsa."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)


def connect(path: str | os.PathLike) -> sqlite3.Connection:
    """Sozlangan ulanish qaytaradi. isolation_level=None: tranzaksiyalar qo'lda boshqariladi."""
    db_path = Path(path)
    _ensure_file(db_path)
    con = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 30000")
        con.execute("PRAGMA foreign_keys = ON")
        mode = con.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise sqlite3.OperationalError(f"WAL rejimini yoqib bo'lmadi (journal_mode={mode})")
        con.execute("PRAGMA synchronous = FULL")
    except Exception:
        con.close()
        raise
    return con


@contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT; istisno bo'lsa ROLLBACK."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def schema_version(con: sqlite3.Connection) -> int:
    return int(con.execute("PRAGMA user_version").fetchone()[0])


def migrate(con: sqlite3.Connection) -> int:
    """Kerakli migratsiyalarni bajaradi va joriy versiyani qaytaradi."""
    current = schema_version(con)
    if current > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"Baza sxemasi v{current}, kod esa v{SCHEMA_VERSION} ni biladi. Kodni yangilang."
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        with transaction(con):
            # Boshqa jarayon shu orada migratsiya qilgan bo'lishi mumkin — qayta tekshiramiz.
            if schema_version(con) >= version:
                continue
            for statement in MIGRATIONS[version]:
                con.execute(statement)
            con.execute(f"PRAGMA user_version = {int(version)}")
    return schema_version(con)


def integrity_ok(con: sqlite3.Connection) -> bool:
    return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
