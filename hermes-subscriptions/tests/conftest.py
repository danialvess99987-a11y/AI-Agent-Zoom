import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subscriptions.models import TelegramUserInfo  # noqa: E402
from subscriptions.service import SubscriptionService  # noqa: E402

ADMIN = 111_111_111
OTHER = 222_222_222
# 2026-01-31 07:00:00 UTC = 12:00 Toshkent
T0 = 1_769_842_800


class FakeClock:
    def __init__(self, start: int = T0) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "data" / "subs.db"


@pytest.fixture
def svc(db_path, clock):
    return SubscriptionService(db_path, [ADMIN], clock=clock)


def make_user(svc, tid, first="Test", last="", username=None):
    user, _ = svc.register_start(TelegramUserInfo(tid, first, last, username))
    return user
