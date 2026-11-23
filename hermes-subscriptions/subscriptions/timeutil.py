"""Vaqt va muddat yordamchilari.

Qoidalar:
* Barcha vaqtlar bazada UTC epoch soniyalarida (int) saqlanadi.
* Foydalanuvchiga Asia/Tashkent mahalliy vaqtida ko'rsatiladi.
* "Oy" kalendar oyi sifatida mahalliy vaqtda qo'shiladi (31-yanvar + 1 oy = 28/29-fevral).
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

LOCAL_TZ_NAME = "Asia/Tashkent"


def _load_local_tz() -> tzinfo:
    """Asia/Tashkent; tizimda tzdata bo'lmasa — doimiy UTC+5 (1992-yildan beri DST yo'q)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(LOCAL_TZ_NAME)
    except Exception:  # ZoneInfoNotFoundError yoki zoneinfo mavjud emas
        return timezone(timedelta(hours=5), LOCAL_TZ_NAME)


LOCAL_TZ: tzinfo = _load_local_tz()

MINUTE = 60
HOUR = 3600
DAY = 86400
WEEK = 7 * DAY

UNIT_SECONDS = {"minute": MINUTE, "hour": HOUR, "day": DAY, "week": WEEK}
VALID_UNITS = ("minute", "hour", "day", "week", "month")

# Bitta amal bilan qo'shiladigan eng uzun muddat (xato kiritishdan himoya).
MAX_DURATION_SECONDS = 10 * 366 * DAY
MAX_AMOUNT = 100_000

UNIT_ALIASES = {
    "minute": "minute", "minutes": "minute", "min": "minute", "daqiqa": "minute", "minut": "minute",
    "минута": "minute", "минуты": "minute", "минут": "minute",
    "hour": "hour", "hours": "hour", "h": "hour", "soat": "hour",
    "час": "hour", "часа": "hour", "часов": "hour",
    "day": "day", "days": "day", "d": "day", "kun": "day",
    "день": "day", "дня": "day", "дней": "day",
    "week": "week", "weeks": "week", "w": "week", "hafta": "week",
    "неделя": "week", "недели": "week", "недель": "week",
    "month": "month", "months": "month", "oy": "month",
    "месяц": "month", "месяца": "month", "месяцев": "month",
}

UNIT_LABEL_UZ = {"minute": "daqiqa", "hour": "soat", "day": "kun", "week": "hafta", "month": "oy"}


class DurationError(ValueError):
    """Noto'g'ri muddat."""


def utc_now() -> int:
    """Joriy vaqt, UTC epoch soniyalarida."""
    return int(time.time())


def add_months(moment: datetime, months: int) -> datetime:
    """Kalendar oylarini qo'shish; oy oxiri kunlari qisqartiriladi (31-yanvar + 1 oy -> fevral oxiri)."""
    index = moment.month - 1 + months
    year = moment.year + index // 12
    month = index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class Duration:
    amount: int
    unit: str

    def __post_init__(self) -> None:
        if isinstance(self.amount, bool) or not isinstance(self.amount, int):
            raise DurationError("Muddat miqdori butun son bo'lishi kerak")
        if self.amount < 1 or self.amount > MAX_AMOUNT:
            raise DurationError(f"Muddat miqdori 1 dan {MAX_AMOUNT} gacha bo'lishi kerak")
        if self.unit not in VALID_UNITS:
            raise DurationError(f"Noma'lum muddat birligi: {self.unit!r}")
        # Juda uzun muddatni oldindan rad etish (oy ~31 kun deb baholanadi).
        approx = self.amount * (31 * DAY if self.unit == "month" else UNIT_SECONDS[self.unit])
        if approx > MAX_DURATION_SECONDS:
            raise DurationError("Muddat juda uzun (ko'pi bilan 10 yil)")

    @classmethod
    def parse(cls, amount: object, unit: object) -> "Duration":
        """LLM yoki tugmadan kelgan qiymatlarni tekshirib Duration yaratadi."""
        if isinstance(amount, bool):
            raise DurationError("Muddat miqdori butun son bo'lishi kerak")
        if isinstance(amount, float):
            if not amount.is_integer():
                raise DurationError("Muddat miqdori butun son bo'lishi kerak")
            amount = int(amount)
        elif isinstance(amount, str):
            text = amount.strip()
            if not text.isdigit():
                raise DurationError("Muddat miqdori butun son bo'lishi kerak")
            amount = int(text)
        if not isinstance(amount, int):
            raise DurationError("Muddat miqdori butun son bo'lishi kerak")
        if not isinstance(unit, str):
            raise DurationError("Muddat birligi matn bo'lishi kerak")
        key = unit.strip().lower()
        if key not in UNIT_ALIASES:
            raise DurationError(f"Noma'lum muddat birligi: {unit!r}")
        return cls(amount, UNIT_ALIASES[key])

    def add_to(self, start: int) -> int:
        """start (UTC epoch) ga muddatni qo'shib, yangi UTC epoch qaytaradi."""
        if self.unit == "month":
            local = datetime.fromtimestamp(start, LOCAL_TZ)
            result = int(add_months(local, self.amount).timestamp())
        else:
            result = start + self.amount * UNIT_SECONDS[self.unit]
        if result - start > MAX_DURATION_SECONDS:
            raise DurationError("Muddat juda uzun (ko'pi bilan 10 yil)")
        return result

    def seconds_from(self, start: int) -> int:
        """start dan boshlab muddat necha soniya ekanini qaytaradi."""
        return self.add_to(start) - start

    def label_uz(self) -> str:
        return f"{self.amount} {UNIT_LABEL_UZ[self.unit]}"

    def to_dict(self) -> dict:
        return {"amount": self.amount, "unit": self.unit}


def format_local(epoch: int | None) -> str:
    """UTC epoch -> 'YYYY-MM-DD HH:MM' (Toshkent vaqti)."""
    if epoch is None:
        return "—"
    return datetime.fromtimestamp(int(epoch), LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def format_remaining(seconds: int | None) -> str:
    """Qolgan vaqtni o'zbekcha ko'rinishda: '2 kun 3 soat 5 daqiqa'."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds <= 0:
        return "tugagan"
    days, rest = divmod(seconds, DAY)
    hours, rest = divmod(rest, HOUR)
    minutes = rest // MINUTE
    parts = []
    if days:
        parts.append(f"{days} kun")
    if hours:
        parts.append(f"{hours} soat")
    if minutes:
        parts.append(f"{minutes} daqiqa")
    return " ".join(parts) if parts else "1 daqiqadan kam"


def local_day(epoch: int) -> str:
    """Toshkent vaqtidagi sana: 'YYYY-MM-DD'."""
    return datetime.fromtimestamp(int(epoch), LOCAL_TZ).strftime("%Y-%m-%d")


def local_day_start(epoch: int) -> int:
    """epoch tushgan Toshkent kunining boshlanishi (UTC epoch)."""
    local = datetime.fromtimestamp(int(epoch), LOCAL_TZ)
    return int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def local_day_end(epoch: int) -> int:
    """epoch tushgan Toshkent kunining oxiri (keyingi kun boshlanishi, UTC epoch)."""
    local = datetime.fromtimestamp(int(epoch), LOCAL_TZ)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((start + timedelta(days=1)).timestamp())
