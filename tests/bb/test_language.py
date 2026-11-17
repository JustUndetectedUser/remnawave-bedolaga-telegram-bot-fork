"""Смена языка в меню переписки: клавиатура над строкой ввода приходит заново на новом языке.

Telegram меняет клавиатуру над строкой ввода только новым сообщением, поэтому после смены языка бот присылает её
сам, затем меню (оно должно быть последним) и только потом убирает экран выбора языка.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from app.bb import keyboard as bb_keyboard, main_menu
from app.config import settings


def _user(language='en'):
    return SimpleNamespace(id=42, telegram_id=7, language=language, subscription=None)


def _callback(order):
    callback = MagicMock()
    callback.message.chat.id = 7

    async def send_message(chat_id, text, **kwargs):
        order.append(('send', text, kwargs.get('reply_markup')))

    async def delete():
        order.append(('delete', None, None))

    callback.bot.send_message = AsyncMock(side_effect=send_message)
    callback.bot.set_chat_menu_button = AsyncMock()
    callback.message.delete = AsyncMock(side_effect=delete)
    return callback


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    monkeypatch.setattr(bb_keyboard.cache, 'set', AsyncMock(return_value=True))
    monkeypatch.setattr(bb_keyboard.cache, 'setnx', AsyncMock(return_value=False))
    monkeypatch.setattr(bb_keyboard.cache, 'exists', AsyncMock(return_value=True))
    menu_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='x', callback_data='bb_more')]])
    monkeypatch.setattr(main_menu, 'build_bb_main_menu', AsyncMock(return_value=('MENU', menu_markup)))
    return menu_markup


@pytest.mark.asyncio
async def test_language_change_resends_keyboard_in_new_language(patched):
    order = []
    callback = _callback(order)
    assert await main_menu.bb_after_language_change(callback, _user('en'), MagicMock()) is True
    kinds = [kind for kind, _, _ in order]
    assert kinds == ['send', 'send', 'delete'], order
    _, _, markup = order[0]
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert [b.text for b in markup.keyboard[0]] == ['Open bot menu', 'To web account']
    assert order[1][1] == 'MENU'


@pytest.mark.asyncio
async def test_language_change_back_to_russian(patched):
    order = []
    await main_menu.bb_after_language_change(_callback(order), _user('ru'), MagicMock())
    assert [b.text for b in order[0][2].keyboard[0]] == ['Вызвать бота', 'В веб-кабинет']


@pytest.mark.asyncio
async def test_language_change_off_leaves_bedolaga_flow(patched, monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'off')
    order = []
    assert await main_menu.bb_after_language_change(_callback(order), _user('en'), MagicMock()) is False
    assert order == []


@pytest.mark.asyncio
async def test_language_change_menu_failure_keeps_language_screen(patched, monkeypatch):
    monkeypatch.setattr(main_menu, 'build_bb_main_menu', AsyncMock(side_effect=RuntimeError('db')))
    order = []
    assert await main_menu.bb_after_language_change(_callback(order), _user('en'), MagicMock()) is False
    assert ('delete', None, None) not in order


@pytest.mark.asyncio
async def test_process_language_change_uses_bb_flow(patched, monkeypatch):
    from app.handlers import menu

    monkeypatch.setattr(settings, 'LANGUAGE_SELECTION_ENABLED', True)
    monkeypatch.setattr(settings, 'AVAILABLE_LANGUAGES', 'ru,en')
    user = _user('ru')
    updated = _user('en')
    monkeypatch.setattr(menu, 'update_user', AsyncMock(return_value=updated))
    show = AsyncMock()
    monkeypatch.setattr(menu, 'show_main_menu', show)
    after = AsyncMock(return_value=True)
    monkeypatch.setattr(main_menu, 'bb_after_language_change', after)
    callback = _callback([])
    callback.data = 'language_select:en'
    callback.answer = AsyncMock()
    await menu.process_language_change(callback, user, MagicMock())
    after.assert_awaited_once()
    assert after.await_args.args[1] is updated
    show.assert_not_awaited()
    callback.answer.assert_awaited_once()
