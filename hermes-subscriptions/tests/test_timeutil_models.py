from datetime import datetime

import pytest

from conftest import T0
from subscriptions.models import (
    Action,
    InvalidTransition,
    Status,
    TRANSITIONS,
    TelegramUserInfo,
    ValidationError,
    next_status,
    normalize_name,
)
from subscriptions.timeutil import (
    DAY,
    HOUR,
    LOCAL_TZ,
    Duration,
    DurationError,
    format_local,
    format_remaining,
    local_day,
    local_day_end,
    local_day_start,
)


def test_duration_hours_days_weeks():
    assert Duration(2, "hour").add_to(T0) == T0 + 2 * HOUR
    assert Duration(7, "day").add_to(T0) == T0 + 7 * DAY
    assert Duration(1, "week").add_to(T0) == T0 + 7 * DAY
    assert Duration(30, "minute").add_to(T0) == T0 + 1800


def test_duration_month_clamps_end_of_month():
    # T0 = 2026-01-31 12:00 Toshkent; +1 oy -> 2026-02-28 12:00 Toshkent
    result = datetime.fromtimestamp(Duration(1, "month").add_to(T0), LOCAL_TZ)
    assert (result.year, result.month, result.day, result.hour) == (2026, 2, 28, 12)
    result12 = datetime.fromtimestamp(Duration(12, "month").add_to(T0), LOCAL_TZ)
    assert (result12.year, result12.month, result12.day) == (2027, 1, 31)


@pytest.mark.parametrize("amount,unit,expected", [
    (2, "soat", ("hour", 2)), ("7", "kun", ("day", 7)), (1.0, "hafta", ("week", 1)),
    (3, "oy", ("month", 3)), (5, "Daqiqa", ("minute", 5)), (2, "hours", ("hour", 2)),
    (2, "часа", ("hour", 2)),
])
def test_duration_parse_aliases(amount, unit, expected):
    d = Duration.parse(amount, unit)
    assert (d.unit, d.amount) == expected


@pytest.mark.parametrize("amount,unit", [
    (0, "day"), (-1, "day"), (True, "day"), (1.5, "day"), ("2x", "day"), (2, "year"),
    (2, None), (None, "day"), (100_001, "minute"), (11 * 366, "day"), (200, "month"),
])
def test_duration_rejects_invalid(amount, unit):
    with pytest.raises(DurationError):
        Duration.parse(amount, unit)


def test_duration_label():
    assert Duration(2, "hour").label_uz() == "2 soat"
    assert Duration(1, "month").label_uz() == "1 oy"


def test_format_local_is_tashkent():
    assert format_local(T0) == "2026-01-31 12:00"
    assert format_local(None) == "—"


def test_format_remaining():
    assert format_remaining(2 * DAY + 3 * HOUR + 5 * 60) == "2 kun 3 soat 5 daqiqa"
    assert format_remaining(0) == "tugagan"
    assert format_remaining(30) == "1 daqiqadan kam"
    assert format_remaining(HOUR) == "1 soat"


def test_local_day_boundaries():
    assert local_day(T0) == "2026-01-31"
    start, end = local_day_start(T0), local_day_end(T0)
    assert format_local(start) == "2026-01-31 00:00"
    assert end - start == DAY


def test_transition_table_rules():
    assert next_status(Status.PENDING, Action.ADD_TIME) is Status.ACTIVE
    assert next_status(Status.ACTIVE, Action.SUSPEND) is Status.SUSPENDED
    assert next_status(Status.SUSPENDED, Action.ADD_TIME) is Status.SUSPENDED
    assert next_status(Status.BANNED, Action.UNBAN) is Status.APPROVED
    for action in Action:
        if action is not Action.UNBAN:
            with pytest.raises(InvalidTransition):
                next_status(Status.BANNED, action)
    # EXPIRE faqat ACTIVE holatdan
    assert [s for s, acts in TRANSITIONS.items() if Action.EXPIRE in acts] == [Status.ACTIVE]
    assert set(TRANSITIONS) == set(Status)


def test_normalize_name_apostrophes():
    assert normalize_name("Gʻayrat  O‘G‘LI") == normalize_name("G'ayrat o'g'li") == "g'ayrat o'g'li"
    assert normalize_name("@Dilshod_99") == "dilshod_99"
    assert normalize_name(None) == ""


def test_telegram_user_info_validation():
    info = TelegramUserInfo(5, "A" * 300, "", "@name")
    assert len(info.first_name) == 256 and info.username == "name"
    for bad in (0, -5, True, "5", None):
        with pytest.raises(ValidationError):
            TelegramUserInfo(bad)  # type: ignore[arg-type]
