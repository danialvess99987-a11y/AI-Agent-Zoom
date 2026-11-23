"""Holatlar, amallar, holatlar orasidagi o'tish qoidalari va ma'lumot obyektlari."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SubscriptionError(Exception):
    """Obuna tizimidagi barcha kutilgan xatolar uchun asos."""


class UserNotFound(SubscriptionError):
    pass


class InvalidTransition(SubscriptionError):
    pass


class PermissionDenied(SubscriptionError):
    pass


class ValidationError(SubscriptionError):
    pass


class PendingActionError(SubscriptionError):
    pass


class Status(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"
    REJECTED = "REJECTED"
    BANNED = "BANNED"

    @property
    def label_uz(self) -> str:
        return STATUS_LABEL_UZ[self]


STATUS_LABEL_UZ = {
    Status.PENDING: "tasdiq kutilmoqda",
    Status.APPROVED: "tasdiqlangan, obunasi yo'q",
    Status.ACTIVE: "faol obuna",
    Status.EXPIRED: "obuna tugagan",
    Status.SUSPENDED: "vaqtincha to'xtatilgan",
    Status.REJECTED: "rad etilgan",
    Status.BANNED: "bloklangan",
}


class Action(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    ADD_TIME = "ADD_TIME"      # yangi obuna yoki uzaytirish
    SUSPEND = "SUSPEND"        # qolgan vaqt muzlatiladi
    RESUME = "RESUME"          # muzlatilgan vaqt qayta ishga tushadi
    REVOKE = "REVOKE"          # obuna hozir tugatiladi
    BAN = "BAN"
    UNBAN = "UNBAN"
    EXPIRE = "EXPIRE"          # faqat tizim (muddat tugashi)


# Ruxsat etilgan o'tishlar: joriy holat -> {amal: yangi holat}. Ro'yxatda yo'q juftlik taqiqlangan.
TRANSITIONS: dict[Status, dict[Action, Status]] = {
    Status.PENDING: {
        Action.APPROVE: Status.APPROVED,
        Action.REJECT: Status.REJECTED,
        Action.ADD_TIME: Status.ACTIVE,
        Action.BAN: Status.BANNED,
    },
    Status.APPROVED: {
        Action.ADD_TIME: Status.ACTIVE,
        Action.REJECT: Status.REJECTED,
        Action.BAN: Status.BANNED,
    },
    Status.ACTIVE: {
        Action.ADD_TIME: Status.ACTIVE,
        Action.SUSPEND: Status.SUSPENDED,
        Action.REVOKE: Status.APPROVED,
        Action.BAN: Status.BANNED,
        Action.EXPIRE: Status.EXPIRED,
    },
    Status.EXPIRED: {
        Action.ADD_TIME: Status.ACTIVE,
        Action.BAN: Status.BANNED,
    },
    Status.SUSPENDED: {
        Action.ADD_TIME: Status.SUSPENDED,
        Action.RESUME: Status.ACTIVE,
        Action.REVOKE: Status.APPROVED,
        Action.BAN: Status.BANNED,
    },
    Status.REJECTED: {
        Action.APPROVE: Status.APPROVED,
        Action.ADD_TIME: Status.ACTIVE,
        Action.BAN: Status.BANNED,
    },
    Status.BANNED: {
        Action.UNBAN: Status.APPROVED,
    },
}


def next_status(current: Status, action: Action) -> Status:
    """O'tish ruxsat etilgan bo'lsa yangi holatni, aks holda InvalidTransition qaytaradi."""
    allowed = TRANSITIONS.get(current, {})
    if action not in allowed:
        raise InvalidTransition(
            f"'{current.label_uz}' holatidagi foydalanuvchi uchun {action.value} amali ruxsat etilmagan"
        )
    return allowed[action]


_APOSTROPHES = "ʻʼ’‘`´ʹ′"
_SPACE_RE = re.compile(r"\s+")


def normalize_name(text: Optional[str]) -> str:
    """Qidiruv uchun normallashtirish: kichik harf, apostroflar bir xil, @ olib tashlanadi, bo'shliqlar siqiladi."""
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text)
    for ch in _APOSTROPHES:
        value = value.replace(ch, "'")
    value = value.replace("@", " ")
    value = _SPACE_RE.sub(" ", value).strip()
    return value.casefold()


@dataclass(frozen=True)
class TelegramUserInfo:
    """Telegram'dan kelgan, hali tekshirilmagan foydalanuvchi ma'lumoti."""

    telegram_id: int
    first_name: str = ""
    last_name: str = ""
    username: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.telegram_id, bool) or not isinstance(self.telegram_id, int) or self.telegram_id <= 0:
            raise ValidationError("Telegram ID musbat butun son bo'lishi kerak")
        for field_name in ("first_name", "last_name"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValidationError(f"{field_name} matn bo'lishi kerak")
            if len(value) > 256:
                object.__setattr__(self, field_name, value[:256])
        if self.username is not None:
            if not isinstance(self.username, str):
                raise ValidationError("username matn bo'lishi kerak")
            cleaned = self.username.strip().lstrip("@")[:64]
            object.__setattr__(self, "username", cleaned or None)


@dataclass(frozen=True)
class User:
    telegram_id: int
    first_name: str
    last_name: str
    username: Optional[str]
    status: Status
    created_at: int
    updated_at: int
    last_seen_at: Optional[int]
    sub_started_at: Optional[int]
    sub_expires_at: Optional[int]
    suspended_remaining: Optional[int]
    status_reason: Optional[str]
    approved_by: Optional[int]
    message_count: int

    @property
    def display_name(self) -> str:
        name = " ".join(part for part in (self.first_name, self.last_name) if part).strip()
        return name or (f"@{self.username}" if self.username else str(self.telegram_id))

    def effective_status(self, now: int) -> Status:
        """ACTIVE bo'lsa-yu, muddat o'tgan bo'lsa — EXPIRED (scheduler hali yozmagan bo'lsa ham)."""
        if self.status is Status.ACTIVE and (self.sub_expires_at is None or self.sub_expires_at <= now):
            return Status.EXPIRED
        return self.status

    def has_access(self, now: int) -> bool:
        return self.effective_status(now) is Status.ACTIVE

    def remaining_seconds(self, now: int) -> Optional[int]:
        status = self.effective_status(now)
        if status is Status.ACTIVE and self.sub_expires_at is not None:
            return self.sub_expires_at - now
        if status is Status.SUSPENDED:
            return self.suspended_remaining
        return None


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str  # ADMIN | ACTIVE | NOT_REGISTERED | PENDING | APPROVED | EXPIRED | SUSPENDED | REJECTED | BANNED
    user: Optional[User]


@dataclass(frozen=True)
class ChangeResult:
    telegram_id: int
    action: Action
    before: User
    after: User
    mode: Optional[str]          # ADD_TIME uchun: "new" | "extend" | "suspended_extend"
    delta_seconds: Optional[int]
    replayed: bool               # True — shu request_id avval bajarilgan, qayta qo'llanmadi


@dataclass(frozen=True)
class PendingAction:
    id: str
    kind: str
    payload: dict
    summary: str
    actor_id: int
    created_at: int
    expires_at: int
    state: str
