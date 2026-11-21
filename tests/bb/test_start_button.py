"""Человеку не нужно писать /start руками (владелец, 21.11.2026).

- Любое непонятое сообщение при меню в переписке: три кнопки над строкой ввода и меню, как на /start.
- /start menu (ссылка на бота с сайта): обычный /start, не реферальный код.
- Описание бота в пустом чате подсказывает, что нажать.
"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bb import description, entry
from app.bb.texts import bb_text
from app.config import settings


def _user(status='active', language='ru'):
    return SimpleNamespace(telegram_id=7, language=language, status=status)


def _message(chat_type='private'):
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=7, type=chat_type)
    msg.answer = AsyncMock()
    msg.delete = AsyncMock()
    msg.bot = MagicMock()
    return msg


@pytest.fixture
def sends(monkeypatch):
    """Порядок отправок: клавиатура, потом меню (меню остаётся последним)."""
    calls = []

    async def keyboard(bot, chat_id, user):
        calls.append(('keyboard', chat_id))

    async def menu(bot, chat_id, user, db):
        calls.append(('menu', chat_id))
        return True

    monkeypatch.setattr(entry, 'maybe_send_bb_keyboard', keyboard)
    monkeypatch.setattr(entry.bb_menu, 'bb_send_main_menu', menu)
    return calls


@pytest.mark.asyncio
async def test_any_message_sends_keyboard_then_menu(monkeypatch, sends):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    msg = _message()
    assert await entry.on_any_message(msg, _user(), MagicMock()) is True
    assert sends == [('keyboard', 7), ('menu', 7)]
    msg.delete.assert_not_awaited()
    msg.answer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('mode', 'user', 'chat_type', 'db'),
    [
        ('off', _user(), 'private', MagicMock()),
        ('all', None, 'private', MagicMock()),
        ('all', _user(status='blocked'), 'private', MagicMock()),
        ('all', _user(status='deleted'), 'private', MagicMock()),
        ('all', _user(), 'group', MagicMock()),
        ('all', _user(), 'supergroup', MagicMock()),
        ('all', _user(), 'private', None),
    ],
)
async def test_any_message_leaves_bedolaga_reply(monkeypatch, sends, mode, user, chat_type, db):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', mode)
    assert await entry.on_any_message(_message(chat_type), user, db) is False
    assert sends == []


@pytest.mark.asyncio
async def test_any_message_menu_failed_falls_back(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    monkeypatch.setattr(entry, 'maybe_send_bb_keyboard', AsyncMock())
    monkeypatch.setattr(entry.bb_menu, 'bb_send_main_menu', AsyncMock(return_value=False))
    assert await entry.on_any_message(_message(), _user(), MagicMock()) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(('mode', 'answered'), [('all', False), ('off', True)])
async def test_unknown_message_handler_hook(monkeypatch, sends, mode, answered):
    from app.handlers.common import handle_unknown_message

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', mode)
    msg = _message()
    await handle_unknown_message(msg, _user(), MagicMock())
    assert msg.answer.await_count == (1 if answered else 0)
    assert sends == ([] if answered else [('keyboard', 7), ('menu', 7)])


async def _feed(dp, **content):
    from aiogram import Bot, types

    bot = Bot('1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA')
    msg = types.Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=types.Chat(id=7, type='private'),
        from_user=types.User(id=7, is_bot=False, first_name='t'),
        **content,
    )
    try:
        await dp.feed_update(bot, types.Update(update_id=1, message=msg), db=MagicMock(), db_user=_user())
    finally:
        await bot.session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'content',
    [
        {'text': 'привет'},
        {'text': 'у меня не работает'},
        {'location': {'latitude': 0.0, 'longitude': 0.0}},
    ],
)
async def test_dispatcher_text_and_media_get_menu(monkeypatch, sends, content):
    """Настоящий порядок регистрации: кнопки app.bb раньше, «непонятое» Bedolaga последним."""
    from aiogram import Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.handlers import common

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    dp = Dispatcher(storage=MemoryStorage())
    entry.register_handlers(dp)
    common.register_handlers(dp)
    await _feed(dp, **content)
    assert sends == [('keyboard', 7), ('menu', 7)]


@pytest.mark.asyncio
async def test_dispatcher_bot_button_not_caught_twice(monkeypatch, sends):
    """«Бот» обрабатывает своя кнопка, хук «непонятого» не срабатывает."""
    from aiogram import Dispatcher, types
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.handlers import common

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    monkeypatch.setattr(types.Message, 'delete', AsyncMock())
    monkeypatch.setattr(entry, 'build_main_menu_keyboard', AsyncMock(return_value='KB'))
    rich = AsyncMock(return_value=True)
    monkeypatch.setattr(entry, 'try_answer_rich_main_menu', rich)
    dp = Dispatcher(storage=MemoryStorage())
    entry.register_handlers(dp)
    common.register_handlers(dp)
    await _feed(dp, text=entry.BB_BTN_BOT)
    rich.assert_awaited_once()
    assert sends == []


def test_start_menu_parameter_is_plain_start():
    """/start menu и сохранённый payload menu обнуляются до проверки доступа, кампании и реферального кода."""
    from app.handlers import start

    source = Path(start.__file__).read_text(encoding='utf-8')
    body = source[source.index('async def cmd_start(') :]
    access = body.index('_evaluate_telegram_registration_access(')
    first = body.index("if msg_start_arg == 'menu':\n        msg_start_arg = None")
    second = body.index("if start_parameter == 'menu':\n        start_parameter = None")
    assert first < second < access
    # msg_start_arg обнуляется до выбора между ним и сохранённым payload: тот не теряется
    assert first < body.index('if pending_start_payload and msg_start_arg')


def _bot(current):
    bot = MagicMock()
    bot.get_my_description = AsyncMock(side_effect=lambda language_code=None: SimpleNamespace(description=current))
    bot.set_my_description = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_description_set_for_default_and_en(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    bot = _bot('')
    await description.configure_bb_description(bot)
    calls = [c.kwargs for c in bot.set_my_description.await_args_list]
    assert calls == [
        {'description': bb_text('ru', 'bot_description'), 'language_code': None},
        {'description': bb_text('en', 'bot_description'), 'language_code': 'en'},
    ]


@pytest.mark.asyncio
async def test_description_same_text_not_set_again(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    texts = {None: bb_text('ru', 'bot_description'), 'en': bb_text('en', 'bot_description')}
    bot = MagicMock()
    bot.get_my_description = AsyncMock(
        side_effect=lambda language_code=None: SimpleNamespace(description=texts[language_code])
    )
    bot.set_my_description = AsyncMock()
    await description.configure_bb_description(bot)
    assert bot.get_my_description.await_count == 2
    bot.set_my_description.assert_not_awaited()


@pytest.mark.asyncio
async def test_description_untouched_when_off(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'off')
    bot = _bot('')
    await description.configure_bb_description(bot)
    bot.get_my_description.assert_not_awaited()
    bot.set_my_description.assert_not_awaited()


@pytest.mark.asyncio
async def test_description_errors_do_not_stop_start(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'admins')
    bot = MagicMock()
    bot.get_my_description = AsyncMock(side_effect=RuntimeError('telegram down'))
    bot.set_my_description = AsyncMock()
    await description.configure_bb_description(bot)
    assert bot.get_my_description.await_count == 2


def test_description_text_fits_and_names_no_button_label():
    """До 512 символов (Telegram). Подпись кнопки зависит от языка приложения, в тексте её нет."""
    for language in ('ru', 'en'):
        text = bb_text(language, 'bot_description')
        assert 0 < len(text) <= 512
        assert 'Старт' not in text and 'Start' not in text and 'Запустить' not in text


def test_startup_calls_description():
    source = Path(__file__).resolve().parents[2].joinpath('main.py').read_text(encoding='utf-8')
    menu = source.index('await configure_chat_menu_button(bot)')
    assert source.index('await configure_bb_description(bot)', menu) > menu
