"""Obunachilar profili ("gate" roli): har bir kiruvchi xabarda obuna nazorati.

`pre_gateway_dispatch` hook'i Hermes auth'idan OLDIN ishlaydi (gateway/run_inbound.py:223) va
`{"action": "skip"}` bilan xabarni to'xtatadi. Hermes bu hookni vaqt bilan cheklamaydi, shuning uchun bu yerda
har bir bloklovchi amal `db_timeout` bilan cheklangan. Kutilmagan xatoda xabar TO'XTATILADI (fail-closed).

Ikkinchi himoya qatlami — Hermes pairing store: faqat faol obunachilar u yerda "approved" bo'ladi. Ruxsat
faqat shu profil kontekstida beriladi (`_approve_user` joriy profilning .env allowlist'iga yozishi mumkin,
obunachilar profilida u bo'sh bo'lishi kerak — o'rnatish skripti tekshiradi).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from . import hermes_compat, texts
from .models import Status, SubscriptionError
from .service import SubscriptionService
from .settings import Settings

logger = logging.getLogger("hermes_plugins.subscriptions.gate")

SKIP = "skip"
ALLOW = {"action": "allow"}
REPLY_THROTTLE_SECONDS = 600  # bir xil holat xabari bir foydalanuvchiga 10 daqiqada ko'pi bilan bir marta


def _skip(reason: str) -> dict:
    return {"action": SKIP, "reason": f"subscriptions:{reason}"}


class Gate:
    def __init__(
        self,
        settings: Settings,
        service: SubscriptionService,
        *,
        pairing_for: Callable[[Any, Any], Optional[hermes_compat.PairingAccess]] = hermes_compat.pairing_for_source,
        send_text: Callable[[Any, Any, str], Awaitable[bool]] = hermes_compat.send_text,
    ) -> None:
        self._settings = settings
        self._service = service
        self._pairing_for = pairing_for
        self._send_text = send_text

    async def _db(self, fn: Callable, *args: Any) -> Any:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=self._settings.db_timeout)

    async def _reply(self, gateway: Any, source: Any, text: str) -> None:
        try:
            await asyncio.wait_for(self._send_text(gateway, source, text), timeout=self._settings.db_timeout)
        except Exception as exc:  # xabar yuborilmasa ham nazorat qarori o'zgarmaydi
            logger.warning("subscriptions: javob yuborilmadi: %s", type(exc).__name__)

    async def _reply_throttled(self, gateway: Any, source: Any, user_id: int, key: str, text: str) -> None:
        bucket = self._service.now() // REPLY_THROTTLE_SECONDS
        try:
            first = await self._db(self._service.claim_notification, f"reply:{user_id}:{key}:{bucket}")
        except Exception:
            first = False
        if first:
            await self._reply(gateway, source, text)

    async def _sync_pairing(self, gateway: Any, source: Any, user_id: int, allowed: bool, name: str) -> bool:
        """Pairing store'ni obuna holatiga moslaydi. Ruxsat berib bo'lmasa — False (xabar to'xtatiladi)."""
        access = self._pairing_for(gateway, source)
        if access is None:
            logger.error("subscriptions: pairing store mavjud emas yoki Hermes API mos emas — kirish yopiq")
            return False
        if allowed:
            if not await self._db(access.is_approved, user_id):
                await self._db(access.approve, user_id, name)
            return True
        if await self._db(access.is_approved, user_id):
            await self._db(access.revoke, user_id)
        return True

    async def on_pre_gateway_dispatch(self, event: Any, gateway: Any = None) -> Optional[dict]:
        try:
            return await self._decide(event, gateway)
        except Exception as exc:
            logger.exception("subscriptions: nazoratda kutilmagan xato — xabar to'xtatildi")
            source = getattr(event, "source", None)
            user_id = hermes_compat.parse_user_id(getattr(source, "user_id", None))
            if user_id is not None and source is not None:
                await self._reply_throttled(gateway, source, user_id, "error", texts.temporary_error())
            return _skip(f"error:{type(exc).__name__}")

    async def _decide(self, event: Any, gateway: Any) -> Optional[dict]:
        if getattr(event, "internal", False):
            return None  # Hermes ichki hodisalari (hook umuman chaqirilmaydi, ehtiyot uchun)
        source = getattr(event, "source", None)
        if source is None or hermes_compat.platform_value(source) != hermes_compat.PLATFORM:
            return None  # boshqa platformalar — Hermes'ning o'z nazorati
        user_id = hermes_compat.parse_user_id(getattr(source, "user_id", None))
        if user_id is None:
            return _skip("no_user")
        if self._service.is_admin(user_id):
            return ALLOW
        if getattr(source, "is_bot", False):
            return _skip("bot")
        if str(getattr(source, "chat_type", "dm")) != "dm":
            await self._reply_throttled(gateway, source, user_id, "group", texts.groups_not_supported())
            return _skip("not_dm")

        get_command = getattr(event, "get_command", None)
        command = get_command() if callable(get_command) else None
        info = hermes_compat.user_info_from_event(event)
        user = await self._db(self._service.get_user, user_id)

        if user is None or command == "start":
            user, is_new = await self._db(self._service.register_start, info)
            if is_new:
                history = await self._db(self._service.history, user_id, 1)
                await self._db(self._service.enqueue_message, self._settings.admin_id, "new_user",
                               texts.admin_new_user(user, len(history)), f"new_user:{user_id}",
                               {"telegram_id": user_id, "username": user.username})
                await self._reply(gateway, source, texts.welcome_pending())
                return _skip("new_user")
            now = self._service.now()
            await self._reply(gateway, source, texts.user_status_message(user.effective_status(now), user, now))
            if user.effective_status(now) is not Status.ACTIVE:
                await self._sync_pairing(gateway, source, user_id, False, info.first_name)
            return _skip("start")

        decision = await self._db(self._service.check_access, user_id)
        if not decision.allowed:
            await self._sync_pairing(gateway, source, user_id, False, info.first_name)
            await self._db(self._service.touch_seen, user_id)
            status = Status(decision.reason)
            now = self._service.now()
            await self._reply_throttled(gateway, source, user_id, f"status:{status.value}",
                                        texts.user_status_message(status, decision.user, now))
            return _skip(f"status:{status.value}")

        if command and command not in self._settings.allowed_commands:
            await self._reply(gateway, source, texts.command_not_allowed())
            return _skip(f"command:{command}")

        quota = await self._db(self._service.consume_message, user_id, self._settings.daily_limit)
        if not quota.allowed:
            await self._reply_throttled(gateway, source, user_id, "limit", texts.limit_reached(quota.limit))
            return _skip("daily_limit")

        if not await self._sync_pairing(gateway, source, user_id, True, info.first_name):
            await self._reply_throttled(gateway, source, user_id, "error", texts.temporary_error())
            return _skip("pairing_unavailable")
        return ALLOW


def make_tool_guard(allowed_tools: frozenset) -> Callable[..., Optional[dict]]:
    """Obunachilar profili uchun `pre_tool_call`: ro'yxatda yo'q tool bloklanadi.

    Hermes bu hookni fail-closed ishlatadi (hermes_cli/plugins_dispatch.py:309-323): xato yoki timeout ham blok.
    """
    allowed = frozenset(allowed_tools)

    def guard(tool_name: str = "", **_: Any) -> Optional[dict]:
        if isinstance(tool_name, str) and tool_name in allowed:
            return None
        return {"action": "block", "message": f"'{tool_name}' vositasi bu foydalanuvchi uchun ruxsat etilmagan."}

    return guard


__all__ = ["Gate", "make_tool_guard", "SubscriptionError"]
