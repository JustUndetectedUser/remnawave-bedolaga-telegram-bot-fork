from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bb import more
from app.config import settings


def _user(language='ru', subscription=True):
    return SimpleNamespace(
        id=42, telegram_id=7, language=language, subscription=SimpleNamespace(id=1) if subscription else None
    )


def _callback():
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message.text = 'Меню'
    callback.message.photo = None
    callback.message.chat.id = 7
    callback.message.edit_text = AsyncMock()
    callback.bot.send_message = AsyncMock()
    return callback


async def _render(monkeypatch, user):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    callback = _callback()
    await more.on_more(callback, user, MagicMock())
    callback.message.edit_text.assert_awaited_once()
    callback.answer.assert_awaited_once()
    args, kwargs = callback.message.edit_text.await_args
    assert kwargs['parse_mode'] == 'HTML'
    return args[0], [[(b.text, b.callback_data) for b in row] for row in kwargs['reply_markup'].inline_keyboard]


@pytest.mark.asyncio
async def test_more_buttons_exact(monkeypatch):
    text, rows = await _render(monkeypatch, _user())
    assert text == '<b>Ещё</b>'
    assert rows == [
        [('📲 Устройства', 'subscription_manage_devices')],
        [('🔄 Автопродление', 'subscription_autopay')],
        [('📜 История', 'balance_history')],
        [('💬 Поддержка', 'menu_support')],
        [('❓ Вопросы и ответы', 'menu_faq')],
        [('← Назад', 'back_to_menu')],
    ]


@pytest.mark.asyncio
async def test_more_buttons_english(monkeypatch):
    text, rows = await _render(monkeypatch, _user('en'))
    assert text == '<b>More</b>'
    assert [label for [(label, _)] in rows] == [
        '📲 Devices',
        '🔄 Auto-renewal',
        '📜 History',
        '💬 Support',
        '❓ FAQ',
        '← Back',
    ]


@pytest.mark.asyncio
async def test_more_without_subscription_hides_subscription_screens(monkeypatch):
    _, rows = await _render(monkeypatch, _user(subscription=False))
    assert [data for [(_, data)] in rows] == ['balance_history', 'menu_support', 'menu_faq', 'back_to_menu']


@pytest.mark.asyncio
async def test_more_disabled_falls_back_to_main_menu(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'off')
    edit_menu = AsyncMock()
    monkeypatch.setattr(more, 'bb_edit_main_menu', edit_menu)
    callback = _callback()
    user = _user()
    await more.on_more(callback, user, MagicMock())
    edit_menu.assert_awaited_once()
    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once()
