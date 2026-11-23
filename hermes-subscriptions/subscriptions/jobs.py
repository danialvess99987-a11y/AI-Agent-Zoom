"""Fon tsikli va CLI uchun umumiy ishlar (bir xil dedupe kalitlari — bildirishnoma takrorlanmaydi)."""

from __future__ import annotations

import logging
from typing import Callable

from . import texts
from .service import SubscriptionService

logger = logging.getLogger("hermes_plugins.subscriptions.jobs")


def expire_and_notify(service: SubscriptionService, admin_id: int, revoke: Callable[[int], object]) -> list[int]:
    """Muddati o'tgan obunalarni EXPIRED qiladi, foydalanuvchi va adminga xabar navbatga qo'yadi, pairing'dan chiqaradi."""
    expired_ids = []
    for user in service.expire_due():
        key = f"{user.telegram_id}:{user.sub_expires_at}"
        service.enqueue_message(user.telegram_id, "expired", texts.user_expired(), f"expired_user:{key}")
        service.enqueue_message(admin_id, "expired_admin", texts.admin_expired(user), f"expired_admin:{key}")
        try:
            revoke(user.telegram_id)
        except Exception:
            logger.exception("subscriptions: pairing'dan chiqarib bo'lmadi (id=%s)", user.telegram_id)
        expired_ids.append(user.telegram_id)
    service.expire_stale_pending()
    return expired_ids
