from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.dispatcher.event.bases import SkipHandler

from app.bb import entry, keyboard, rollout
from app.bb.entry import BB_BTN_BOT, BB_BTN_CABINET
from app.bb.keyboard import bb_reply_keyboard
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
    await keyboard.send_bb_keyboard(bot, 5, 'en')
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


# --- Кнопка слева от поля ввода (F1) ---


@pytest.mark.asyncio
async def test_send_bb_keyboard_sets_commands_button_for_chat():
    """MenuButtonDefault для чата значит «как у бота», то есть снова «Кабинет»; MenuButtonCommands без команд пуст."""
    from aiogram.types import MenuButtonCommands

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.set_chat_menu_button = AsyncMock()
    await keyboard.send_bb_keyboard(bot, 5, 'ru')
    kwargs = bot.set_chat_menu_button.await_args.kwargs
    assert kwargs['chat_id'] == 5
    assert isinstance(kwargs['menu_button'], MenuButtonCommands)


@pytest.mark.asyncio
@pytest.mark.parametrize(('mode', 'reset'), [('all', True), ('admins', True), ('off', False)])
async def test_startup_resets_bot_wide_menu_button(monkeypatch, mode, reset):
    """MENU_BUTTON_WEBAPP_ENABLED=false: при меню в переписке кнопка бота «Кабинет» сбрасывается на старте."""
    from aiogram.types import MenuButtonDefault

    from app.utils.chat_menu_button import configure_chat_menu_button

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', mode)
    monkeypatch.setattr(settings, 'MENU_BUTTON_WEBAPP_ENABLED', False)
    bot = MagicMock()
    bot.set_chat_menu_button = AsyncMock()

    assert await configure_chat_menu_button(bot) is False

    if not reset:
        bot.set_chat_menu_button.assert_not_awaited()
        return
    bot.set_chat_menu_button.assert_awaited_once()
    kwargs = bot.set_chat_menu_button.await_args.kwargs
    assert kwargs.get('chat_id') is None
    assert isinstance(kwargs['menu_button'], MenuButtonDefault)

    # Ошибка Telegram не роняет старт.
    bot.set_chat_menu_button = AsyncMock(side_effect=RuntimeError('telegram down'))
    assert await configure_chat_menu_button(bot) is False


@pytest.mark.asyncio
async def test_startup_keeps_webapp_button_when_enabled(monkeypatch):
    from aiogram.types import MenuButtonWebApp

    from app.utils.chat_menu_button import configure_chat_menu_button

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    monkeypatch.setattr(settings, 'MENU_BUTTON_WEBAPP_ENABLED', True)
    monkeypatch.setattr(settings, 'MENU_BUTTON_WEBAPP_URL', 'https://cab.example')
    bot = MagicMock()
    bot.set_chat_menu_button = AsyncMock()
    assert await configure_chat_menu_button(bot) is True
    assert isinstance(bot.set_chat_menu_button.await_args.kwargs['menu_button'], MenuButtonWebApp)


# --- Кнопки над строкой ввода у тех, кто уже в боте (F2) ---


class _FakeCache:
    """Как app.utils.cache.cache: без Redis setnx и exists отвечают False."""

    def __init__(self, connected=True):
        self.connected = connected
        self.data = {}

    async def setnx(self, key, value, expire=None):
        if not self.connected or key in self.data:
            return False
        self.data[key] = (value, expire)
        return True

    async def set(self, key, value, expire=None):
        if not self.connected:
            return False
        self.data[key] = (value, expire)
        return True

    async def exists(self, key):
        return self.connected and key in self.data


def _menu_user():
    return SimpleNamespace(
        id=42,
        telegram_id=7,
        language='ru',
        balance_kopeks=12000,
        auth_type='telegram',
        is_trial_already_used=lambda: True,
        subscription=None,
    )


def _sent_reply_keyboards(bot):
    from aiogram.types import ReplyKeyboardMarkup

    return [
        c for c in bot.send_message.await_args_list if isinstance(c.kwargs.get('reply_markup'), ReplyKeyboardMarkup)
    ]


@pytest.mark.asyncio
async def test_first_menu_render_sends_keyboard_once(monkeypatch, enabled):
    from app.bb import main_menu

    fake = _FakeCache()
    monkeypatch.setattr(keyboard, 'cache', fake)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.set_chat_menu_button = AsyncMock()
    user = _menu_user()

    assert await main_menu.bb_send_main_menu(bot, 7, user, MagicMock()) is True
    assert len(_sent_reply_keyboards(bot)) == 1
    # Сначала клавиатура, последним сообщение с меню и его кнопками.
    calls = bot.send_message.await_args_list
    assert len(calls) == 2 and calls[0].args[1] == 'Кнопки «Вызвать бота» и «В веб-кабинет» всегда над строкой ввода.'
    assert 'bb_kb_sent:7' in fake.data and fake.data['bb_kb_sent:7'][1] == 30 * 24 * 3600

    bot.send_message.reset_mock()
    assert await main_menu.bb_send_main_menu(bot, 7, user, MagicMock()) is True
    assert _sent_reply_keyboards(bot) == []
    bot.send_message.assert_awaited_once()

    # Правка меню на месте (back_to_menu) у того, кому клавиатуру ещё не присылали.
    other = _FakeCache()
    monkeypatch.setattr(keyboard, 'cache', other)
    callback = MagicMock()
    callback.bot = bot
    bot.send_message.reset_mock()
    bot.edit_message_text = AsyncMock()
    callback.message.text = 'старое меню'
    callback.message.photo = None
    callback.message.chat.id = 7
    callback.message.message_id = 99
    assert await main_menu.bb_edit_main_menu(callback, user, MagicMock()) is True
    assert len(_sent_reply_keyboards(bot)) == 1
    bot.edit_message_text.assert_awaited_once()
    bot.send_message.reset_mock()
    assert await main_menu.bb_edit_main_menu(callback, user, MagicMock()) is True
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_keyboard_sent_anyway_without_redis(monkeypatch, enabled):
    from app.bb import main_menu

    monkeypatch.setattr(keyboard, 'cache', _FakeCache(connected=False))
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.set_chat_menu_button = AsyncMock()
    for _ in range(2):
        bot.send_message.reset_mock()
        assert await main_menu.bb_send_main_menu(bot, 7, _menu_user(), MagicMock()) is True
        assert len(_sent_reply_keyboards(bot)) == 1


@pytest.mark.asyncio
async def test_start_keyboard_marks_flag(monkeypatch, enabled):
    """/start присылает клавиатуру всегда и ставит метку: следующее меню её не дублирует."""
    from app.bb import main_menu

    fake = _FakeCache()
    monkeypatch.setattr(keyboard, 'cache', fake)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.set_chat_menu_button = AsyncMock()
    user = _menu_user()
    fake.data['bb_kb_sent:7'] = (1, 1)

    await keyboard.maybe_send_bb_keyboard(bot, 7, user)
    assert len(_sent_reply_keyboards(bot)) == 1

    del fake.data['bb_kb_sent:7']
    await keyboard.maybe_send_bb_keyboard(bot, 7, user)
    assert 'bb_kb_sent:7' in fake.data
    bot.send_message.reset_mock()
    await main_menu.bb_send_main_menu(bot, 7, user, MagicMock())
    assert _sent_reply_keyboards(bot) == []


@pytest.mark.asyncio
async def test_no_keyboard_when_disabled(monkeypatch):

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'off')
    monkeypatch.setattr(keyboard, 'cache', _FakeCache())
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await keyboard.maybe_send_bb_keyboard_once(bot, 7, _menu_user())
    await keyboard.maybe_send_bb_keyboard(bot, 7, _menu_user())
    bot.send_message.assert_not_awaited()


def test_start_sends_keyboard_before_menu():
    """/start и регистрация: заметка с клавиатурой до меню, последним остаётся меню с его кнопками (F9)."""
    from pathlib import Path

    from app.handlers import start

    lines = Path(start.__file__).read_text(encoding='utf-8').splitlines()
    calls = [i for i, line in enumerate(lines) if 'await maybe_send_bb_keyboard(' in line]
    assert len(calls) == 5
    for i in calls:
        following = lines[i + 1 : i + 4]
        assert any('try_answer_rich_main_menu(' in line for line in following), lines[i - 2 : i + 4]
