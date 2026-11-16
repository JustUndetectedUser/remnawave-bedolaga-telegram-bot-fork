from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from app.bb import entry, rollout
from app.bb.entry import BB_BTN_BOT, BB_BTN_CABINET, bb_reply_keyboard
from app.config import settings


def _user(language='ru', telegram_id=1):
    return SimpleNamespace(telegram_id=telegram_id, language=language)


def _message(text):
    msg = MagicMock()
    msg.text = text
    msg.answer = AsyncMock()
    msg.delete = AsyncMock()
    msg.bot = MagicMock()
    return msg


@pytest.mark.parametrize(
    ('mode', 'admin', 'expected'),
    [
        ('off', True, False),
        ('off', False, False),
        ('admins', True, True),
        ('admins', False, False),
        ('all', True, True),
        ('all', False, True),
        ('garbage', True, False),
    ],
)
def test_rollout_off_admins_all(monkeypatch, mode, admin, expected):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', mode)
    monkeypatch.setattr(type(settings), 'is_admin', lambda self, telegram_id=None, email=None: admin)
    assert rollout.chat_menu_enabled(_user()) is expected
    assert rollout.chat_menu_enabled(None) is False


def test_reply_keyboard_layout():
    kb = bb_reply_keyboard('ru')
    assert kb.is_persistent is True
    assert kb.resize_keyboard is True
    assert len(kb.keyboard) == 1
    assert [b.text for b in kb.keyboard[0]] == [BB_BTN_BOT, BB_BTN_CABINET]
    en = bb_reply_keyboard('en')
    assert [b.text for b in en.keyboard[0]] == ['Open bot menu', 'To web account']


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')


@pytest.mark.asyncio
async def test_bot_button_clears_state_and_shows_menu(monkeypatch, enabled):
    monkeypatch.setattr(entry, 'build_main_menu_keyboard', AsyncMock(return_value='KB'))
    monkeypatch.setattr(entry, 'try_answer_rich_main_menu', AsyncMock(return_value=False))
    from app.handlers import start

    monkeypatch.setattr(start, 'get_main_menu_text', AsyncMock(return_value='MENU'))
    media = AsyncMock()
    monkeypatch.setattr(start, 'answer_menu_with_media', media)
    state = MagicMock()
    state.clear = AsyncMock()
    msg = _message(BB_BTN_BOT)
    db = MagicMock()

    await entry.on_bot_button(msg, state, db, _user())

    state.clear.assert_awaited_once()
    media.assert_awaited_once_with(msg, 'MENU', 'KB', db)
    msg.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_cabinet_button_sends_webapp_button(monkeypatch, enabled):
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cab.example/app')
    msg = _message(BB_BTN_CABINET)

    await entry.on_cabinet_button(msg, _user())

    msg.delete.assert_awaited_once()
    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.await_args
    assert args[0] == 'Веб-кабинет откроется уже с входом в ваш аккаунт.'
    rows = kwargs['reply_markup'].inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1
    assert rows[0][0].text == 'Открыть веб-кабинет'
    assert rows[0][0].web_app.url == 'https://cab.example/app'


@pytest.mark.asyncio
async def test_buttons_ignored_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'off')
    state = MagicMock()
    state.clear = AsyncMock()
    msg = _message(BB_BTN_BOT)
    with pytest.raises(SkipHandler):
        await entry.on_bot_button(msg, state, MagicMock(), _user())
    with pytest.raises(SkipHandler):
        await entry.on_cabinet_button(_message(BB_BTN_CABINET), _user())
    state.clear.assert_not_awaited()
    msg.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_bb_keyboard_never_raises():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError('x'))
    bot.set_chat_menu_button = AsyncMock(side_effect=RuntimeError('y'))
    await entry.send_bb_keyboard(bot, 5, 'en')
    bot.send_message.assert_awaited_once()
    bot.set_chat_menu_button.assert_awaited_once()


def _real_dispatcher(monkeypatch, calls):
    from aiogram import Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.states import BalanceStates

    dp = Dispatcher(storage=MemoryStorage())
    entry.register_handlers(dp)

    async def amount_handler(message):
        calls.append('amount')

    async def catch_all(message):
        calls.append('catch_all')

    dp.message.register(amount_handler, BalanceStates.waiting_for_amount)
    dp.message.register(catch_all)

    from aiogram import types

    monkeypatch.setattr(types.Message, 'delete', AsyncMock())
    monkeypatch.setattr(entry, 'build_main_menu_keyboard', AsyncMock(return_value='KB'))
    monkeypatch.setattr(entry, 'try_answer_rich_main_menu', AsyncMock(return_value=True))
    return dp


async def _feed(dp, text, state_key=True):
    from datetime import UTC, datetime

    from aiogram import Bot, types
    from aiogram.fsm.storage.base import StorageKey

    from app.states import BalanceStates

    bot = Bot('1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA')
    key = StorageKey(bot_id=1, chat_id=7, user_id=7)
    if state_key:
        await dp.storage.set_state(key, BalanceStates.waiting_for_amount.state)
    msg = types.Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=types.Chat(id=7, type='private'),
        from_user=types.User(id=7, is_bot=False, first_name='t'),
        text=text,
    )
    await dp.feed_update(bot, types.Update(update_id=1, message=msg), db=MagicMock(), db_user=_user())
    return await dp.storage.get_state(key), bot


@pytest.mark.asyncio
async def test_dispatcher_bot_button_beats_fsm_and_catch_all(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    calls = []
    dp = _real_dispatcher(monkeypatch, calls)
    state, bot = await _feed(dp, BB_BTN_BOT)
    await bot.session.close()
    assert state is None
    assert calls == []


@pytest.mark.asyncio
async def test_dispatcher_disabled_falls_through_to_state_handler(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'off')
    calls = []
    dp = _real_dispatcher(monkeypatch, calls)
    state, bot = await _feed(dp, BB_BTN_BOT)
    await bot.session.close()
    assert calls == ['amount']
    assert state is not None

    calls.clear()
    dp = _real_dispatcher(monkeypatch, calls)
    state, bot = await _feed(dp, BB_BTN_BOT, state_key=False)
    await bot.session.close()
    assert calls == ['catch_all']
