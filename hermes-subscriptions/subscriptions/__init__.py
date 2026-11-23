"""Hermes Agent uchun foydalanuvchilar, ruxsat va obuna boshqaruv plugini.

Bitta plugin kodi ikki profilda, `role` sozlamasi bilan ishlaydi:
* role: admin — default profil (bot egasi): admin tool'lari, tasdiqlash tugmalari, xabarlar va muddat nazorati.
* role: gate  — obunachilar profili: har bir xabarda obuna nazorati, kunlik limit, tool qo'riqchisi.

Sozlamalar: config.yaml -> plugins.entries.subscriptions.settings (settings.py ga qarang).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("hermes_plugins.subscriptions")

__all__ = ["register"]


def register(ctx: Any) -> None:
    from . import hermes_compat
    from .admin_tools import AdminTools, register_admin_tools
    from .gate import Gate, make_tool_guard
    from .service import SubscriptionService
    from .settings import ROLE_GATE, from_plugin_context
    from .telegram_ui import TelegramUI, make_revoker

    settings = from_plugin_context(ctx.get_config)
    service = SubscriptionService(settings.db_path, [settings.admin_id])

    if settings.role == ROLE_GATE:
        gate = Gate(settings, service)
        ctx.register_hook("pre_gateway_dispatch", gate.on_pre_gateway_dispatch)
        ctx.register_hook("pre_tool_call", make_tool_guard(settings.allowed_tools))
        logger.info("subscriptions: gate roli faol (kunlik limit %s)", settings.daily_limit)
        return

    admin_id = settings.admin_id

    async def admin_only(event: Any) -> Any:
        """Himoya: route xato sozlansa ham default profilga Telegram'da faqat administrator kira oladi."""
        source = getattr(event, "source", None)
        if source is None or getattr(event, "internal", False):
            return None
        if hermes_compat.platform_value(source) != hermes_compat.PLATFORM:
            return None
        if hermes_compat.parse_user_id(getattr(source, "user_id", None)) == admin_id:
            return None
        logger.warning("subscriptions: default profilga administrator bo'lmagan Telegram xabari keldi — to'xtatildi")
        return {"action": "skip", "reason": "subscriptions:admin_profile_only"}

    ctx.register_hook("pre_gateway_dispatch", admin_only)
    tools = AdminTools(settings, service, hermes_compat.session_actor_id)
    registered = register_admin_tools(ctx, tools)
    ui = TelegramUI(settings, service, spawn_task=ctx.spawn_task,
                    revoke_pairing=make_revoker(settings.subscriber_profile))
    ctx.register_telegram_handler(ui.factory)
    logger.info("subscriptions: admin roli faol, tool'lar: %s", ", ".join(registered))
