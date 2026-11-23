"""Plugin sozlamalari: `plugins.entries.subscriptions.settings.*` (har bir profilning config.yaml'ida).

Administrator ID faqat shu yerdan olinadi — suhbatdan yoki foydalanuvchi ismidan hech qachon aniqlanmaydi.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

ROLE_ADMIN = "admin"   # default profil: admin tool'lari, tasdiqlash tugmalari, xabar yuborish, muddat nazorati
ROLE_GATE = "gate"     # obunachilar profili: har bir xabarda obuna nazorati, tool qo'riqchisi

DEFAULT_DB_PATH = "~/.local/share/hermes-subscriptions/subs.db"
DEFAULT_DAILY_LIMIT = 50
DEFAULT_SUBSCRIBER_PROFILE = "subscribers"
# Obunachilar uchun ruxsat etilgan tool'lar (Hermes v0.23.3 nomlari, toolsets.py).
DEFAULT_ALLOWED_TOOLS = ("web_search", "web_extract", "vision_analyze", "clarify", "todo_list")
# Obunachilar uchun ruxsat etilgan slash-buyruqlar (qolganlari, masalan /model, bloklanadi).
DEFAULT_ALLOWED_COMMANDS = ("start", "new", "reset", "help", "stop")


class SettingsError(ValueError):
    """Sozlamalar noto'g'ri — plugin ishga tushmaydi."""


@dataclass(frozen=True)
class Settings:
    role: str
    admin_id: int
    db_path: Path
    daily_limit: int = DEFAULT_DAILY_LIMIT
    subscriber_profile: str = DEFAULT_SUBSCRIBER_PROFILE
    allowed_tools: frozenset = field(default_factory=lambda: frozenset(DEFAULT_ALLOWED_TOOLS))
    allowed_commands: frozenset = field(default_factory=lambda: frozenset(DEFAULT_ALLOWED_COMMANDS))
    outbox_interval: float = 5.0
    expiry_interval: float = 60.0
    db_timeout: float = 10.0


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise SettingsError(f"{name} butun son bo'lishi kerak")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or value <= 0:
        raise SettingsError(f"{name} musbat butun son bo'lishi kerak")
    return value


def _name_list(name: str, value: Any, default: tuple) -> frozenset:
    if value is None:
        return frozenset(default)
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v.strip() for v in value):
        raise SettingsError(f"{name} matnlar ro'yxati bo'lishi kerak")
    return frozenset(v.strip().lower() if name == "allowed_commands" else v.strip() for v in value)


def _number(name: str, value: Any, default: float, low: float, high: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= float(value) <= high:
        raise SettingsError(f"{name} {low} va {high} oralig'ida bo'lishi kerak")
    return float(value)


def from_mapping(data: Mapping[str, Any]) -> Settings:
    role = data.get("role")
    if role not in (ROLE_ADMIN, ROLE_GATE):
        raise SettingsError("role 'admin' yoki 'gate' bo'lishi kerak")
    admin_id = _positive_int("admin_telegram_id", data.get("admin_telegram_id"))
    raw_path = data.get("db_path") or DEFAULT_DB_PATH
    if not isinstance(raw_path, str):
        raise SettingsError("db_path matn bo'lishi kerak")
    db_path = Path(os.path.expanduser(raw_path))
    if not db_path.is_absolute():
        raise SettingsError("db_path absolyut yo'l bo'lishi kerak")
    limit = data.get("daily_limit", DEFAULT_DAILY_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise SettingsError("daily_limit manfiy bo'lmagan butun son bo'lishi kerak (0 — cheklanmagan)")
    profile = data.get("subscriber_profile") or DEFAULT_SUBSCRIBER_PROFILE
    if not isinstance(profile, str) or not profile.replace("-", "").replace("_", "").isalnum() or profile == "default":
        raise SettingsError("subscriber_profile noto'g'ri profil nomi")
    return Settings(
        role=role,
        admin_id=admin_id,
        db_path=db_path,
        daily_limit=limit,
        subscriber_profile=profile,
        allowed_tools=_name_list("allowed_tools", data.get("allowed_tools"), DEFAULT_ALLOWED_TOOLS),
        allowed_commands=_name_list("allowed_commands", data.get("allowed_commands"), DEFAULT_ALLOWED_COMMANDS),
        outbox_interval=_number("outbox_interval", data.get("outbox_interval"), 5.0, 1.0, 300.0),
        expiry_interval=_number("expiry_interval", data.get("expiry_interval"), 60.0, 10.0, 3600.0),
        db_timeout=_number("db_timeout", data.get("db_timeout"), 10.0, 1.0, 60.0),
    )


_KEYS = ("role", "admin_telegram_id", "db_path", "daily_limit", "subscriber_profile", "allowed_tools",
         "allowed_commands", "outbox_interval", "expiry_interval", "db_timeout")


def from_plugin_context(get_config: Callable[[str, Any], Any]) -> Settings:
    """`ctx.get_config(key, default)` orqali o'qiydi."""
    return from_mapping({key: get_config(key, None) for key in _KEYS})
