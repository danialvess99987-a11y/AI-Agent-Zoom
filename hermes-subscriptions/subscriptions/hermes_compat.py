"""Hermes Agent v0.23.3 ichki API'lariga yagona kirish nuqtasi.

Hermes'ning barcha ichki (pastki chiziqli) atributlariga murojaat faqat shu faylda. Hermes yangilansa,
`check_pairing_api()` mos kelmaslikni aniqlaydi va plugin obunachilarga ruxsat bermaydi (fail-closed).
Manba havolalari: gateway/pairing.py:345-488, gateway/authz_mixin.py:429,628, gateway/session_context.py:230.
"""

from __future__ import annotations

import inspect
from typing import Any, Optional

from .models import TelegramUserInfo, ValidationError

PLATFORM = "telegram"


def platform_value(source: Any) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "")


def parse_user_id(raw: Any) -> Optional[int]:
    """Telegram user_id (Hermes'da str) -> int; noto'g'ri bo'lsa None."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        return value if value > 0 else None
    return None


def user_info_from_event(event: Any) -> TelegramUserInfo:
    """MessageEvent'dan foydalanuvchi ma'lumoti (PTB Message.from_user bo'lsa — undan)."""
    source = getattr(event, "source", None)
    user_id = parse_user_id(getattr(source, "user_id", None))
    if user_id is None:
        raise ValidationError("Xabarda foydalanuvchi ID yo'q")
    sender = getattr(getattr(event, "raw_message", None), "from_user", None)
    first = getattr(sender, "first_name", None)
    last = getattr(sender, "last_name", None)
    username = getattr(sender, "username", None)
    if not isinstance(first, str) or not first:
        first = getattr(source, "user_name", None) or ""
    return TelegramUserInfo(
        telegram_id=user_id,
        first_name=first if isinstance(first, str) else "",
        last_name=last if isinstance(last, str) else "",
        username=username if isinstance(username, str) else None,
    )


def check_pairing_api(store: Any) -> Optional[str]:
    """PairingStore kutilgan interfeysga egami? Mos bo'lsa None, aks holda sabab."""
    if store is None:
        return "pairing store topilmadi"
    for name in ("is_approved", "revoke", "_approve_user", "_locked"):
        if not callable(getattr(store, name, None)):
            return f"PairingStore.{name} topilmadi (Hermes versiyasi mos emas)"
    try:
        params = list(inspect.signature(store._approve_user).parameters)
    except (TypeError, ValueError):
        return "PairingStore._approve_user imzosini o'qib bo'lmadi"
    if params[:2] != ["platform", "user_id"]:
        return f"PairingStore._approve_user imzosi kutilmagan: {params}"
    return None


class PairingAccess:
    """Hermes pairing store ustidan xavfsiz o'ram (faqat telegram platformasi)."""

    def __init__(self, store: Any) -> None:
        problem = check_pairing_api(store)
        if problem:
            raise RuntimeError(problem)
        self._store = store

    def is_approved(self, telegram_id: int) -> bool:
        return bool(self._store.is_approved(PLATFORM, str(telegram_id)))

    def approve(self, telegram_id: int, user_name: str = "") -> None:
        # Hermes talabi: _approve_user faqat _locked() ichida chaqiriladi (gateway/pairing.py:443).
        with self._store._locked():
            self._store._approve_user(PLATFORM, str(telegram_id), user_name[:64])

    def revoke(self, telegram_id: int) -> bool:
        return bool(self._store.revoke(PLATFORM, str(telegram_id)))


def pairing_for_source(gateway: Any, source: Any) -> Optional[PairingAccess]:
    """Xabar yo'naltirilgan profilning pairing store'i (gateway/authz_mixin.py:628)."""
    getter = getattr(gateway, "_pairing_store_for", None)
    store = getter(source) if callable(getter) else getattr(gateway, "pairing_store", None)
    if store is None or check_pairing_api(store):
        return None
    return PairingAccess(store)


def pairing_for_profile(profile: str) -> PairingAccess:
    """Profil nomi bo'yicha pairing store (gateway/pairing.py:345: PairingStore(profile=...))."""
    from gateway.pairing import PairingStore  # Hermes ichida ishlaganda mavjud

    return PairingAccess(PairingStore(profile=profile))


async def send_text(gateway: Any, source: Any, text: str) -> bool:
    """Xabar kelgan chatga oddiy matn yuborish (TelegramAdapter.send, gateway/authz_mixin.py:429)."""
    getter = getattr(gateway, "_adapter_for_source", None) or getattr(gateway, "_delivery_adapter_for", None)
    adapter = getter(source) if callable(getter) else None
    if adapter is None:
        return False
    result = await adapter.send(str(getattr(source, "chat_id", "")), text)
    return bool(getattr(result, "success", False))


def session_actor_id() -> Optional[int]:
    """Joriy turn'ni yuborgan Telegram foydalanuvchisi (HERMES_SESSION_ACTOR_ID). Boshqa platforma — None."""
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return None
    if get_session_env("HERMES_SESSION_PLATFORM", "") != PLATFORM:
        return None
    return parse_user_id(get_session_env("HERMES_SESSION_ACTOR_ID", ""))
