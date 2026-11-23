"""Foydalanuvchi va administratorga yuboriladigan o'zbekcha matnlar.

* Foydalanuvchiga Hermes adapteri orqali yuboriladigan matnlar — oddiy matn (Markdown belgilarisiz).
* Administratorga PTB orqali yuboriladigan matnlar — HTML; foydalanuvchidan kelgan qiymatlar escape qilinadi.
"""

from __future__ import annotations

from html import escape
from typing import Optional

from .models import ChangeResult, Status, User
from .timeutil import format_local, format_remaining

CAPABILITIES = (
    "— oddiy tilda suhbatlashish va savollarga javob berish;\n"
    "— murakkab topshiriqlarni bosqichlarga ajratib rejalashtirish;\n"
    "— internetda izlanish, sahifalarni o'qish va ma'lumot to'plash;\n"
    "— dasturlash bo'yicha maslahat, kod yozish va xatolarni topishda yordam;\n"
    "— rasmlarni tahlil qilish;\n"
    "— matn, jadval va ma'lumotlarni tahlil qilish;\n"
    "— takroriy ishlarni avtomatlashtirish bo'yicha reja tuzish."
)


def welcome_pending() -> str:
    return (
        "Assalomu alaykum! Bu shaxsiy AI-agent.\n\n"
        "Agent imkoniyatlari:\n" + CAPABILITIES + "\n\n"
        "Agentdan foydalanish uchun administrator ruxsati va faol obuna kerak. "
        "So'rovingiz administratorga yuborildi. Ruxsat berilgach, shu yerga xabar keladi."
    )


def user_status_message(status: Status, user: Optional[User], now: int) -> str:
    if status is Status.PENDING:
        return "So'rovingiz administrator tasdig'ini kutmoqda. Ruxsat berilgach, xabar olasiz."
    if status is Status.APPROVED:
        return "Ruxsatingiz tasdiqlangan, lekin hozir faol obunangiz yo'q. Obuna uchun administratorga murojaat qiling."
    if status is Status.EXPIRED:
        return "Obunangiz muddati tugagan. Davom ettirish uchun administratorga murojaat qiling."
    if status is Status.SUSPENDED:
        left = format_remaining(user.suspended_remaining) if user else "—"
        return f"Obunangiz vaqtincha to'xtatilgan (qolgan vaqt saqlanadi: {left})."
    if status is Status.REJECTED:
        return "Afsuski, so'rovingiz rad etilgan."
    if status is Status.BANNED:
        return "Sizning kirishingiz bloklangan."
    if status is Status.ACTIVE and user is not None:
        return (f"Obunangiz faol. Tugash vaqti: {format_local(user.sub_expires_at)} (Toshkent), "
                f"qolgan: {format_remaining(user.remaining_seconds(now))}.")
    return "Agentdan foydalanish uchun /start buyrug'ini yuboring."


def limit_reached(limit: int) -> str:
    return (f"Bugungi limitingiz tugadi ({limit} ta xabar). "
            "Limit Toshkent vaqti bilan yarim tunda yangilanadi.")


def command_not_allowed() -> str:
    return "Bu buyruq siz uchun mavjud emas. Savolingizni oddiy matn bilan yozing."


def temporary_error() -> str:
    return "Vaqtincha texnik nosozlik. Birozdan keyin qayta urinib ko'ring."


def groups_not_supported() -> str:
    return "Agent faqat shaxsiy chatda ishlaydi."


# ----------------------------------------------------------------------------- administratorga (HTML)
def _user_line(user: User) -> str:
    name = escape(user.display_name)
    username = f" (@{escape(user.username)})" if user.username else ""
    return f'<a href="tg://user?id={user.telegram_id}">{name}</a>{username}, ID: <code>{user.telegram_id}</code>'


def admin_new_user(user: User, history_count: int) -> str:
    lines = [
        "🆕 <b>Yangi foydalanuvchi</b>",
        _user_line(user),
        f"Birinchi kirgan: {format_local(user.created_at)}",
        f"Oxirgi faollik: {format_local(user.last_seen_at)}",
        f"Holat: {escape(user.status.label_uz)}",
        f"Oldingi obunalar tarixi: {history_count} ta yozuv" if history_count else "Oldingi obunalar: yo'q",
    ]
    if not user.username:
        lines.append("<i>Username yo'q — chat havolasi Telegram sozlamalariga qarab ishlamasligi mumkin.</i>")
    return "\n".join(lines)


def admin_user_card(user: User, now: int, today_messages: int, limit: int) -> str:
    status = user.effective_status(now)
    lines = [
        "👤 <b>Foydalanuvchi</b>",
        _user_line(user),
        f"Holat: {escape(status.label_uz)}",
        f"Ro'yxatdan o'tgan: {format_local(user.created_at)}",
        f"Oxirgi faollik: {format_local(user.last_seen_at)}",
        f"Bugungi xabarlar: {today_messages}/{limit if limit else '∞'}",
        f"Jami xabarlar: {user.message_count}",
    ]
    if status is Status.ACTIVE:
        lines.append(f"Obuna tugashi: {format_local(user.sub_expires_at)} "
                     f"(qolgan {format_remaining(user.remaining_seconds(now))})")
    if status is Status.SUSPENDED:
        lines.append(f"Muzlatilgan vaqt: {format_remaining(user.suspended_remaining)}")
    if user.status_reason:
        lines.append(f"Sabab: {escape(user.status_reason)}")
    return "\n".join(lines)


def admin_confirm_request(summary: str) -> str:
    return f"❓ <b>Tasdiqlang</b>\n{escape(summary)}\n\n<i>15 daqiqa ichida tasdiqlanmasa bekor bo'ladi.</i>"


_MODE_TEXT = {"new": "yangi obuna", "extend": "uzaytirildi", "suspended_extend": "muzlatilgan vaqtga qo'shildi"}


def change_line(result: ChangeResult, now: int) -> str:
    after = result.after
    name = escape(after.display_name)
    if result.replayed:
        return f"• {name}: avval bajarilgan, o'zgarish yo'q"
    text = f"• {name}: {escape(after.effective_status(now).label_uz)}"
    if result.mode:
        text += f" ({_MODE_TEXT.get(result.mode, result.mode)})"
    if after.effective_status(now) is Status.ACTIVE:
        text += f", tugash: {format_local(after.sub_expires_at)}"
    if after.status is Status.SUSPENDED:
        text += f", muzlatilgan: {format_remaining(after.suspended_remaining)}"
    return text


def admin_done(results: list[ChangeResult], now: int) -> str:
    if not results:
        return "✅ Tasdiqlandi. O'zgarish talab qiladigan foydalanuvchi topilmadi."
    shown = [change_line(r, now) for r in results[:MAX_RESULT_LINES]]
    if len(results) > MAX_RESULT_LINES:
        shown.append(f"… va yana {len(results) - MAX_RESULT_LINES} ta foydalanuvchi")
    return f"✅ <b>Bajarildi</b> ({len(results)} ta)\n" + "\n".join(shown)


MAX_RESULT_LINES = 30


def admin_expired(user: User) -> str:
    return f"⌛ Obuna tugadi: {_user_line(user)}"


def user_activated(user: User) -> str:
    return (f"✅ Obunangiz faollashtirildi. Tugash vaqti: {format_local(user.sub_expires_at)} (Toshkent). "
            "Endi savolingizni yozishingiz mumkin.")


def user_expired() -> str:
    return "⌛ Obunangiz muddati tugadi. Davom ettirish uchun administratorga murojaat qiling."
