"""Hermes v0.23.3 va python-telegram-bot obyektlarining test modellari.

Har bir model manba koddagi imzoga moslab yozilgan (izohlarda havola). Bular haqiqiy Hermes bilan sinov o'rnini
bosmaydi — faqat plugin mantig'ini tekshiradi.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
from typing import Any, Optional


class Platform(Enum):  # gateway/config.py:239
    TELEGRAM = "telegram"
    DISCORD = "discord"


@dataclass
class SessionSource:  # gateway/session.py:69-97
    platform: Platform = Platform.TELEGRAM
    chat_id: str = "1"
    chat_type: str = "dm"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    is_bot: bool = False
    profile: Optional[str] = "subscribers"


@dataclass
class MessageEvent:  # gateway/platforms/event.py:66-217
    text: str
    source: SessionSource
    raw_message: Any = None
    internal: bool = False
    allow_gateway_control: bool = True
    message_type: str = "TEXT"

    def _command_text(self) -> str:
        return self.text or ""

    def is_command(self) -> bool:
        return self.allow_gateway_control and self._command_text().startswith("/")

    def get_command(self) -> Optional[str]:
        if not self.is_command():
            return None
        raw = self._command_text().split(maxsplit=1)[0][1:].lower().split("@", 1)[0]
        return None if "/" in raw else raw


def tg_event(user_id: int, text: str = "salom", *, first: str = "Ali", last: str = "", username=None,
             chat_type: str = "dm", platform: Platform = Platform.TELEGRAM, is_bot: bool = False) -> MessageEvent:
    sender = SimpleNamespace(first_name=first, last_name=last, username=username)
    source = SessionSource(platform=platform, chat_id=str(user_id), chat_type=chat_type, user_id=str(user_id),
                           user_name=f"{first} {last}".strip(), is_bot=is_bot)
    return MessageEvent(text=text, source=source, raw_message=SimpleNamespace(from_user=sender))


@dataclass
class SendResult:  # gateway/platforms/base.py:1570
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


class FakeAdapter:  # TelegramAdapter.send(chat_id: str, content: str, reply_to=None, metadata=None)
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        if self.fail:
            raise ConnectionError("network")
        self.sent.append((chat_id, content))
        return SendResult(True, "1")


class FakePairingStore:  # gateway/pairing.py:345-488
    def __init__(self) -> None:
        self.approved: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.locked_calls = 0
        self.approve_outside_lock = False
        self._in_lock = False

    @contextlib.contextmanager
    def _locked(self):
        with self._lock:
            self._in_lock = True
            self.locked_calls += 1
            try:
                yield
            finally:
                self._in_lock = False

    def is_approved(self, platform: str, user_id: str) -> bool:
        return f"{platform}:{user_id}" in self.approved

    def _approve_user(self, platform: str, user_id: str, user_name: str = "") -> None:
        if not self._in_lock:
            self.approve_outside_lock = True
        self.approved[f"{platform}:{user_id}"] = {"user_name": user_name}

    def revoke(self, platform: str, user_id: str) -> bool:
        return self.approved.pop(f"{platform}:{user_id}", None) is not None


class FakeGateway:  # GatewayRunner: _adapter_for_source (run.py:3298), _pairing_store_for (authz_mixin.py:628)
    def __init__(self, adapter: Optional[FakeAdapter] = None, store: Any = "default") -> None:
        self.adapter = adapter or FakeAdapter()
        self.store = FakePairingStore() if store == "default" else store

    def _adapter_for_source(self, source):
        return self.adapter

    def _pairing_store_for(self, source):
        return self.store


class FakeCtx:  # hermes_cli/plugins_context.py
    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.hooks: dict[str, list] = {}
        self.tools: dict[str, dict] = {}
        self.telegram_factories: list = []
        self.spawned: list = []

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def register_hook(self, hook_name: str, callback, *, hold_stream: bool = False):
        self.hooks.setdefault(hook_name, []).append(callback)

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False,
                      description="", emoji="", override=False):
        assert isinstance(schema, dict) and isinstance(schema.get("parameters"), dict)  # tools/registry.py:854
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}
        return object()

    def register_telegram_handler(self, factory) -> None:
        self.telegram_factories.append(factory)

    def spawn_task(self, coro, *, name=None):
        self.spawned.append(name)
        coro.close()  # testda tsiklni ishga tushirmaymiz
        return None


# ---------------------------------------------------------------- python-telegram-bot 22.x modellari
@dataclass
class InlineKeyboardButton:
    text: str
    callback_data: Optional[str] = None
    url: Optional[str] = None


@dataclass
class InlineKeyboardMarkup:
    inline_keyboard: list


@dataclass
class CallbackQueryHandler:
    callback: Any
    pattern: Any = None


FAKE_PTB = SimpleNamespace(InlineKeyboardButton=InlineKeyboardButton, InlineKeyboardMarkup=InlineKeyboardMarkup)
FAKE_PTB_EXT = SimpleNamespace(CallbackQueryHandler=CallbackQueryHandler)


class FakeBot:
    def __init__(self, fail_for: set | None = None) -> None:
        self.messages: list[dict] = []
        self.fail_for = fail_for or set()

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kw):
        if chat_id in self.fail_for:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.messages.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "markup": reply_markup})


@dataclass
class FakeApp:
    bot: FakeBot = field(default_factory=FakeBot)
    handlers: list = field(default_factory=list)

    def add_handler(self, handler, group: int = 0):
        self.handlers.append(handler)


class FakeQuery:
    def __init__(self, user_id: int, data: str) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = SimpleNamespace(message_id=77)
        self.answers: list[tuple[Optional[str], bool]] = []
        self.edits: list[str] = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.edits.append(text)
