"""Administrator profili uchun LLM tool'lari (tabiiy tilda boshqarish).

Model administrator gapini strukturali argumentlarga aylantiradi; qaror deterministik kodda.
* Har bir tool server tomonida HERMES_SESSION_ACTOR_ID == admin ekanini tekshiradi.
* O'zgartiruvchi amallar darhol bajarilmaydi: `subs_request_action` faqat tasdiq so'rovini yaratadi va
  administratorga Telegram tugmalari bilan yuboradi. Tasdiqni faqat tugma bosish (admin ID tekshiriladi) bajaradi.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from . import texts
from .models import Status, SubscriptionError, User
from .service import SubscriptionService
from .settings import Settings
from .timeutil import DAY, Duration, DurationError, format_local, format_remaining, local_day_start

TOOLSET = "subscriptions"

_FILTERS = ("active", "pending", "approved", "expired", "suspended", "rejected", "banned", "expiring_today",
            "expiring_3d", "new_today", "new_7d", "seen_7d", "inactive_7d", "top_7d", "all")
_ACTIONS = ("add_time", "approve", "reject", "suspend", "resume", "revoke", "ban", "unban", "add_time_bulk")
_UNITS = ("minute", "hour", "day", "week", "month")


def _schema(name: str, description: str, properties: dict, required: Optional[list] = None) -> dict:
    params: dict = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


_ID = {"type": "integer", "description": "Foydalanuvchining Telegram ID raqami (subs_find_users natijasidan)."}

SCHEMAS = {
    "subs_find_users": _schema(
        "subs_find_users",
        "Obuna tizimidagi foydalanuvchini ism, familiya, @username yoki Telegram ID bo'yicha qidiradi. "
        "Administrator ism bilan murojaat qilganda HAR DOIM avval shu tool bilan qidiring. "
        "Bir nechta natija chiqsa — taxmin qilmang, ro'yxatni ko'rsatib administratordan tanlashni so'rang.",
        {"query": {"type": "string", "description": "Ism, '@username' yoki Telegram ID."}},
        ["query"],
    ),
    "subs_user_info": _schema(
        "subs_user_info",
        "Bitta foydalanuvchi haqida batafsil ma'lumot: holat, obuna muddati, bugungi xabarlar, oxirgi obuna tarixi.",
        {"telegram_id": _ID}, ["telegram_id"],
    ),
    "subs_list": _schema(
        "subs_list",
        "Foydalanuvchilar ro'yxati filtr bo'yicha: active, pending, approved, expired, suspended, rejected, banned, "
        "expiring_today (bugun tugaydi), expiring_3d, new_today, new_7d, seen_7d (7 kunda kirganlar), "
        "inactive_7d (faol obunasi bor, 7 kunda foydalanmagan), top_7d (eng faollar), all.",
        {"filter": {"type": "string", "enum": list(_FILTERS)},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ["filter"],
    ),
    "subs_stats": _schema("subs_stats", "Umumiy statistika: foydalanuvchilar soni, holatlar bo'yicha, faol obunalar.", {}),
    "subs_history": _schema(
        "subs_history", "Foydalanuvchining obuna tarixi (kim, qachon, qanday amal).",
        {"telegram_id": _ID, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ["telegram_id"],
    ),
    "subs_request_action": _schema(
        "subs_request_action",
        "Obunani o'zgartirish so'rovini yaratadi. Amal DARHOL BAJARILMAYDI: administratorga Telegram'da "
        "'Tasdiqlash/Bekor qilish' tugmalari yuboriladi. Amallar: add_time (obuna berish yoki uzaytirish — faol "
        "bo'lsa tugash vaqtiga qo'shiladi, bo'lmasa hozirdan boshlanadi), approve, reject, suspend (vaqt muzlatiladi), "
        "resume, revoke (obunani hozir tugatish), ban, unban, add_time_bulk (barcha faol obunalarga qo'shish). "
        "telegram_id faqat subs_find_users natijasidan olinsin; muddat faqat administrator aytgan qiymat bo'lsin.",
        {
            "action": {"type": "string", "enum": list(_ACTIONS)},
            "telegram_id": _ID,
            "amount": {"type": "integer", "minimum": 1, "description": "Muddat miqdori (add_time uchun)."},
            "unit": {"type": "string", "enum": list(_UNITS), "description": "Muddat birligi (add_time uchun)."},
            "reason": {"type": "string", "description": "Ixtiyoriy sabab (reject/suspend/revoke/ban)."},
        },
        ["action"],
    ),
}


def _ok(data: Any) -> str:
    return json.dumps({"success": True, **data} if isinstance(data, dict) else {"success": True, "data": data},
                      ensure_ascii=False)


def _err(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def _user_brief(user: User, now: int) -> dict:
    status = user.effective_status(now)
    data = {
        "telegram_id": user.telegram_id,
        "name": user.display_name,
        "username": f"@{user.username}" if user.username else None,
        "status": status.value,
        "status_uz": status.label_uz,
        "registered": format_local(user.created_at),
        "last_seen": format_local(user.last_seen_at),
    }
    if status is Status.ACTIVE:
        data["expires"] = format_local(user.sub_expires_at)
        data["remaining"] = format_remaining(user.remaining_seconds(now))
    if status is Status.SUSPENDED:
        data["frozen_remaining"] = format_remaining(user.suspended_remaining)
    return data


class AdminTools:
    def __init__(self, settings: Settings, service: SubscriptionService,
                 actor_id: Callable[[], Optional[int]]) -> None:
        self._settings = settings
        self._service = service
        self._actor_id = actor_id

    def _guard(self) -> Optional[str]:
        actor = self._actor_id()
        if actor is None or actor != self._settings.admin_id or not self._service.is_admin(actor):
            return _err("Bu vosita faqat administrator uchun.")
        return None

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {
            "subs_find_users": self.find_users,
            "subs_user_info": self.user_info,
            "subs_list": self.list_users,
            "subs_stats": self.stats,
            "subs_history": self.history,
            "subs_request_action": self.request_action,
        }

    # Hermes handler'ni `handler(args, **kwargs)` ko'rinishida chaqiradi (tools/registry.py:1091-1099).
    def find_users(self, args: dict, **_: Any) -> str:
        if (denied := self._guard()):
            return denied
        try:
            users = self._service.find_users(str(args.get("query", "")))
        except SubscriptionError as exc:
            return _err(str(exc))
        now = self._service.now()
        return _ok({"count": len(users), "users": [_user_brief(u, now) for u in users],
                    "note": "Bir nechta natija bo'lsa, administratordan aniq tanlashni so'rang."
                    if len(users) > 1 else None})

    def user_info(self, args: dict, **_: Any) -> str:
        if (denied := self._guard()):
            return denied
        tid = args.get("telegram_id")
        user = self._service.get_user(tid) if isinstance(tid, int) and not isinstance(tid, bool) else None
        if user is None:
            return _err("Foydalanuvchi topilmadi.")
        now = self._service.now()
        limit = user.daily_limit if user.daily_limit is not None else self._settings.daily_limit
        data = _user_brief(user, now)
        data.update({
            "messages_today": self._service.messages_today(tid),
            "daily_limit": limit if limit else "cheklanmagan",
            "messages_total": user.message_count,
            "status_reason": user.status_reason,
            "recent_history": self._history_rows(tid, 5),
        })
        return _ok({"user": data})

    def _history_rows(self, tid: int, limit: int) -> list[dict]:
        rows = []
        for item in self._service.history(tid, limit):
            rows.append({
                "time": format_local(item["created_at"]),
                "action": item["action"],
                "from": item["old_status"],
                "to": item["new_status"],
                "new_expires": format_local(item["new_expires_at"]),
                "by": "administrator" if item["actor_id"] else "tizim",
                "reason": item["reason"],
            })
        return rows

    def list_users(self, args: dict, **_: Any) -> str:
        if (denied := self._guard()):
            return denied
        name = args.get("filter")
        limit = args.get("limit") if isinstance(args.get("limit"), int) else 30
        limit = max(1, min(int(limit), 100))
        svc, now = self._service, self._service.now()
        if name not in _FILTERS:
            return _err("Noma'lum filtr.")
        if name == "top_7d":
            rows = svc.top_users(7, limit)
            return _ok({"count": len(rows), "users": [{**_user_brief(u, now), "messages_7d": n} for u, n in rows]})
        if name in ("active", "pending", "approved", "expired", "suspended", "rejected", "banned"):
            users = svc.list_users(Status(name.upper()), limit)
        elif name == "all":
            users = svc.list_users(None, limit)
        elif name == "expiring_today":
            users = svc.expiring_today()
        elif name == "expiring_3d":
            users = svc.expiring_within(3 * DAY)
        elif name == "new_today":
            users = svc.new_users_since(local_day_start(now))
        elif name == "new_7d":
            users = svc.new_users_since(now - 7 * DAY)
        elif name == "seen_7d":
            users = svc.seen_since(now - 7 * DAY)
        else:  # inactive_7d
            users = svc.inactive_active_since(now - 7 * DAY)
        users = users[:limit]
        return _ok({"filter": name, "count": len(users), "users": [_user_brief(u, now) for u in users]})

    def stats(self, args: dict, **_: Any) -> str:
        if (denied := self._guard()):
            return denied
        data = self._service.stats()
        data["daily_limit"] = self._settings.daily_limit
        return _ok({"stats": data})

    def history(self, args: dict, **_: Any) -> str:
        if (denied := self._guard()):
            return denied
        tid = args.get("telegram_id")
        if not isinstance(tid, int) or isinstance(tid, bool) or self._service.get_user(tid) is None:
            return _err("Foydalanuvchi topilmadi.")
        limit = args.get("limit") if isinstance(args.get("limit"), int) else 20
        return _ok({"telegram_id": tid, "history": self._history_rows(tid, max(1, min(int(limit), 50)))})

    def request_action(self, args: dict, **_: Any) -> str:
        if (denied := self._guard()):
            return denied
        actor = self._settings.admin_id
        action = args.get("action")
        if action not in _ACTIONS:
            return _err("Noma'lum amal.")
        payload: dict = {}
        duration: Optional[Duration] = None
        if action in ("add_time", "add_time_bulk"):
            try:
                duration = Duration.parse(args.get("amount"), args.get("unit"))
            except DurationError as exc:
                return _err(f"Muddat noto'g'ri: {exc}")
            payload.update(duration.to_dict())
        if args.get("reason") is not None:
            payload["reason"] = args.get("reason")
        try:
            if action == "add_time_bulk":
                count = self._service.stats()["active_subscriptions"]
                summary = f"Barcha faol obunalarga (+{duration.label_uz()}) qo'shish. Hozir faol: {count} ta."
            else:
                tid = args.get("telegram_id")
                if not isinstance(tid, int) or isinstance(tid, bool):
                    return _err("telegram_id kerak (avval subs_find_users bilan aniqlang).")
                user = self._service.get_user(tid)
                if user is None:
                    return _err("Foydalanuvchi topilmadi.")
                payload["target"] = tid
                summary = self._summary(action, user, duration, payload.get("reason"))
            pending = self._service.create_pending(action, payload, summary, actor)
            self._service.enqueue_message(actor, "confirm", texts.admin_confirm_request(summary),
                                          f"confirm:{pending.id}", {"pending_id": pending.id})
        except SubscriptionError as exc:
            return _err(str(exc))
        return _ok({"status": "awaiting_confirmation", "summary": summary,
                    "note": "Amal hali bajarilmadi. Administrator Telegram'dagi tugma orqali tasdiqlashi kerak."})

    def _summary(self, action: str, user: User, duration: Optional[Duration], reason: Optional[str]) -> str:
        now = self._service.now()
        who = f"{user.display_name} (ID {user.telegram_id})"
        status = user.effective_status(now)
        if action == "add_time" and duration is not None:
            mode, value = self._service.preview_add_time(user.telegram_id, duration)
            if mode == "extend":
                return (f"{who}: obunani +{duration.label_uz()} uzaytirish. Hozirgi tugash: "
                        f"{format_local(user.sub_expires_at)} → yangi: {format_local(value)} (Toshkent).")
            if mode == "suspended_extend":
                return (f"{who}: to'xtatilgan obunaga +{duration.label_uz()} qo'shish. "
                        f"Muzlatilgan vaqt: {format_remaining(value)}.")
            return (f"{who}: yangi obuna {duration.label_uz()} ({status.label_uz} holatidan). "
                    f"Hozirdan boshlanadi, tugash: {format_local(value)} (Toshkent).")
        labels = {"approve": "ruxsat berish (obunasiz)", "reject": "rad etish", "suspend": "obunani vaqtincha to'xtatish",
                  "resume": "obunani qayta faollashtirish", "revoke": "obunani hozir bekor qilish",
                  "ban": "bloklash", "unban": "blokdan chiqarish"}
        text = f"{who}: {labels[action]}. Hozirgi holat: {status.label_uz}."
        if reason:
            text += f" Sabab: {reason}"
        return text


def register_admin_tools(ctx: Any, tools: AdminTools) -> list[str]:
    registered = []
    for name, handler in tools.handlers().items():
        result = ctx.register_tool(name=name, toolset=TOOLSET, schema=SCHEMAS[name], handler=handler,
                                   description=SCHEMAS[name]["description"])
        if result is not None:
            registered.append(name)
    return registered
