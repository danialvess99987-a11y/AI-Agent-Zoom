"""Administrator profili: Telegram tugmalari, xabarlar navbati (outbox) va muddat nazorati.

* `factory(app, adapter)` Hermes tomonidan Telegram adapteri ulanganda chaqiriladi (gateway/platforms/base.py:2188;
  plugins/platforms/telegram/adapter.py:2956). `app` — python-telegram-bot 22.x `Application`.
* Tugmalar `subs:` prefiksli callback_data bilan; boshqa (core) tugmalarga tegilmaydi.
* Hermes plugin handler'lari uchun avtorizatsiya qilmaydi — shuning uchun har bir bosishda admin ID tekshiriladi.
* Fon tsikli: outbox xabarlarini yuboradi, muddati o'tgan obunalarni EXPIRED qiladi va pairing'dan chiqaradi.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Optional

from . import texts
from .jobs import expire_and_notify
from .models import PendingActionError, PermissionDenied, SubscriptionError
from .service import SubscriptionService
from .settings import Settings

logger = logging.getLogger("hermes_plugins.subscriptions.telegram_ui")

CALLBACK_PATTERN = r"^subs:"
_DATA_RE = re.compile(r"^subs:(ok|no|ap|rj|in):([A-Za-z0-9_\-]{1,32})$")


def _ptb() -> Any:
    import telegram  # python-telegram-bot (Hermes venv'ida mavjud)

    return telegram


def _ptb_ext() -> Any:
    import telegram.ext

    return telegram.ext


class TelegramUI:
    def __init__(
        self,
        settings: Settings,
        service: SubscriptionService,
        *,
        spawn_task: Callable[..., Any],
        revoke_pairing: Callable[[int], Any],
        ptb: Callable[[], Any] = _ptb,
        ptb_ext: Callable[[], Any] = _ptb_ext,
    ) -> None:
        self._settings = settings
        self._service = service
        self._spawn_task = spawn_task
        self._revoke_pairing = revoke_pairing
        self._ptb = ptb
        self._ptb_ext = ptb_ext
        self._app: Any = None
        self._loop_started = False
        self._last_expiry_run = 0.0

    # ------------------------------------------------------------------ Hermes factory
    def factory(self, app: Any, adapter: Any = None) -> None:
        ext = self._ptb_ext()
        app.add_handler(ext.CallbackQueryHandler(self.on_callback, pattern=CALLBACK_PATTERN))
        self._app = app  # qayta ulanishda yangi Application bilan almashadi
        if not self._loop_started:
            self._loop_started = True
            self._spawn_task(self.run_loop(), name="subscriptions-loop")

    # ------------------------------------------------------------------ tugmalar
    def _keyboard(self, kind: str, payload: dict) -> Any:
        tg = self._ptb()
        button = tg.InlineKeyboardButton
        if kind == "confirm" and isinstance(payload.get("pending_id"), str):
            pid = payload["pending_id"]
            return tg.InlineKeyboardMarkup([[button("✅ Tasdiqlash", callback_data=f"subs:ok:{pid}"),
                                             button("❌ Bekor qilish", callback_data=f"subs:no:{pid}")]])
        if kind == "new_user" and isinstance(payload.get("telegram_id"), int):
            tid = payload["telegram_id"]
            rows = [[button("✅ Ruxsat berish", callback_data=f"subs:ap:{tid}"),
                     button("❌ Rad etish", callback_data=f"subs:rj:{tid}")],
                    [button("ℹ️ Batafsil", callback_data=f"subs:in:{tid}")]]
            username = payload.get("username")
            if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{4,32}", username):
                rows[1].append(button("💬 Chatni ochish", url=f"https://t.me/{username}"))
            return tg.InlineKeyboardMarkup(rows)
        return None

    async def on_callback(self, update: Any, context: Any = None) -> None:
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        user = getattr(query, "from_user", None)
        if getattr(user, "id", None) != self._settings.admin_id:
            await query.answer("Bu tugma faqat administrator uchun.", show_alert=True)
            return
        match = _DATA_RE.match(str(getattr(query, "data", "") or ""))
        if not match:
            await query.answer("Noma'lum tugma.")
            return
        verb, value = match.groups()
        admin = self._settings.admin_id
        try:
            if verb == "ok":
                results = await asyncio.to_thread(self._service.confirm_pending, value, admin)
                now = self._service.now()
                for result in results:
                    await asyncio.to_thread(self._after_change, result)
                await query.answer("Bajarildi")
                await query.edit_message_text(texts.admin_done(results, now), parse_mode="HTML")
            elif verb == "no":
                cancelled = await asyncio.to_thread(self._service.cancel_pending, value, admin)
                await query.answer("Bekor qilindi" if cancelled else "Allaqachon yakunlangan")
                await query.edit_message_text("🚫 Bekor qilindi." if cancelled else "Bu so'rov allaqachon yakunlangan.")
            elif verb in ("ap", "rj"):
                if not value.isdigit():
                    raise SubscriptionError("Noto'g'ri ID")
                tid = int(value)
                call = self._service.approve if verb == "ap" else self._service.reject
                request_id = f"button:{verb}:{tid}:{getattr(getattr(query, 'message', None), 'message_id', '')}"
                result = await asyncio.to_thread(call, tid, admin, request_id=request_id)
                await asyncio.to_thread(self._after_change, result)
                await query.answer("Bajarildi")
                note = ("\n\nObuna berish uchun yozing, masalan: «… ga 7 kunlik obuna ber»." if verb == "ap" else "")
                await query.edit_message_text(texts.admin_done([result], self._service.now()) + note,
                                              parse_mode="HTML")
            else:  # "in" — batafsil ma'lumot
                if not value.isdigit():
                    raise SubscriptionError("Noto'g'ri ID")
                tid = int(value)
                user_obj = await asyncio.to_thread(self._service.get_user, tid)
                if user_obj is None:
                    raise SubscriptionError("Foydalanuvchi topilmadi")
                today = await asyncio.to_thread(self._service.messages_today, tid)
                limit = user_obj.daily_limit if user_obj.daily_limit is not None else self._settings.daily_limit
                await query.answer()
                await self._app.bot.send_message(
                    chat_id=admin, text=texts.admin_user_card(user_obj, self._service.now(), today, limit),
                    parse_mode="HTML")
        except (PendingActionError, PermissionDenied, SubscriptionError) as exc:
            await query.answer(str(exc)[:190], show_alert=True)
        except Exception:
            logger.exception("subscriptions: tugmani qayta ishlashda xato")
            await query.answer("Xatolik yuz berdi, loglarni tekshiring.", show_alert=True)

    def _after_change(self, result: Any) -> None:
        """O'zgarishdan keyingi ishlar: kirish yopilsa — pairing'dan chiqarish; faollashsa — foydalanuvchiga xabar."""
        if result.replayed:
            return
        now = self._service.now()
        after = result.after
        if after.has_access(now):
            if not result.before.has_access(now):
                self._service.enqueue_message(after.telegram_id, "activated", texts.user_activated(after),
                                              f"activated:{after.telegram_id}:{after.sub_expires_at}")
        else:
            try:
                self._revoke_pairing(after.telegram_id)
            except Exception:
                logger.exception("subscriptions: pairing'dan chiqarib bo'lmadi (gate keyingi xabarda yopadi)")

    # ------------------------------------------------------------------ fon tsikli
    async def run_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("subscriptions: fon tsiklida xato")
            await asyncio.sleep(self._settings.outbox_interval)

    async def tick(self) -> None:
        now = self._service.now()
        if now - self._last_expiry_run >= self._settings.expiry_interval:
            self._last_expiry_run = now
            await asyncio.to_thread(self.process_expiry)
        await self.deliver_outbox()

    def process_expiry(self) -> int:
        return len(expire_and_notify(self._service, self._settings.admin_id, self._revoke_pairing))

    async def deliver_outbox(self) -> int:
        if self._app is None or getattr(self._app, "bot", None) is None:
            return 0
        sent = 0
        for message in await asyncio.to_thread(self._service.fetch_outbox, 20):
            try:
                await self._app.bot.send_message(
                    chat_id=message.chat_id, text=message.text, parse_mode="HTML",
                    reply_markup=self._keyboard(message.kind, message.payload),
                )
            except Exception as exc:
                await asyncio.to_thread(self._service.mark_outbox_failed, message.id, f"{type(exc).__name__}: {exc}")
                logger.warning("subscriptions: xabar yuborilmadi (id=%s): %s", message.id, type(exc).__name__)
                continue
            await asyncio.to_thread(self._service.mark_outbox_sent, message.id)
            sent += 1
        return sent


def make_revoker(profile: str) -> Callable[[int], bool]:
    """Obunachilar profilining pairing store'idan foydalanuvchini chiqaradi (default kontekstdan xavfsiz:
    revoke faqat o'chiradi)."""
    from .hermes_compat import pairing_for_profile

    def revoke(telegram_id: int) -> bool:
        return pairing_for_profile(profile).revoke(telegram_id)

    return revoke


__all__ = ["TelegramUI", "make_revoker", "CALLBACK_PATTERN"]
