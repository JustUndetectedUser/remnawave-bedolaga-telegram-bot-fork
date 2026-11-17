from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest

from app.bb import connect
from app.config import settings


LINK = 'https://sub.example.com/abc123'
REDIRECT = 'https://cab.example.com/guest/happ'


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, 'BB_HAPP_REDIRECT_URL', REDIRECT)
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC')


def _sub(link=LINK, devices=3):
    return SimpleNamespace(
        subscription_url=link,
        subscription_crypto_link=None,
        device_limit=devices,
        remnawave_id=None,
        end_date=datetime(2026, 12, 16, 12, tzinfo=UTC),
    )


def _user(language='ru'):
    return SimpleNamespace(id=1, telegram_id=7, language=language, remnawave_id=None)


def _labels(kb):
    return [[b.text for b in row] for row in kb.inline_keyboard]


def test_connect_buttons_order():
    text, kb = connect.bb_connect_screen(_user(), _sub(), 'ru', devices_used=1)
    assert _labels(kb) == [['📲 Открыть в Happ'], ['📋 Скопировать ссылку'], ['⬇️ Установить Happ'], ['← Назад']]
    assert kb.inline_keyboard[0][0].url == f'{REDIRECT}?u={quote(LINK, safe="")}'
    assert kb.inline_keyboard[2][0].callback_data == 'bb_install'
    assert kb.inline_keyboard[3][0].callback_data == 'back_to_menu'
    assert text == (
        '<b>Подключение устройства</b>\n1. Установите Happ\n'
        '2. Нажмите «Открыть в Happ», подписка добавится сама.\n3. Включите VPN в Happ\n'
        'Устройств подключено: 1 из 3'
    )
    assert '—' not in text


def test_connect_english_and_no_devices_line():
    text, kb = connect.bb_connect_screen(_user('en'), _sub(), 'en')
    assert text.startswith('<b>Connect a device</b>\n1. Install Happ')
    assert 'Devices connected' not in text
    assert _labels(kb)[0] == ['📲 Open in Happ']
    text, _ = connect.bb_connect_screen(_user('en'), _sub(), 'en', devices_used=2)
    assert text.endswith('Devices connected: 2 of 3')


def test_no_redirect_url_hides_open_button(monkeypatch):
    monkeypatch.setattr(settings, 'BB_HAPP_REDIRECT_URL', '')
    _, kb = connect.bb_connect_screen(_user(), _sub(), 'ru')
    assert _labels(kb) == [['📋 Скопировать ссылку'], ['⬇️ Установить Happ'], ['← Назад']]


def test_copy_button_has_link():
    _, kb = connect.bb_connect_screen(_user(), _sub(), 'ru')
    assert kb.inline_keyboard[1][0].copy_text.text == LINK


def test_link_over_256_chars_shown_as_text():
    long_link = 'https://sub.example.com/' + 'a' * 300
    text, kb = connect.bb_connect_screen(_user(), _sub(long_link), 'ru')
    assert all('Скопировать' not in label for row in _labels(kb) for label in row)
    assert f'<code>{long_link}</code>' in text
    assert _labels(kb)[0] == ['📲 Открыть в Happ']


def test_no_link_no_crash():
    text, kb = connect.bb_connect_screen(_user(), _sub(None), 'ru')
    assert text == 'Ссылка ещё готовится, попробуйте через минуту.'
    assert _labels(kb) == [['← Назад']]
    text, kb = connect.bb_connect_screen(_user('en'), _sub(None), 'en', intro='hi')
    assert text == 'hi\n\nThe link is not ready yet, try again in a minute.'


def test_intro_goes_first():
    text, _ = connect.bb_connect_screen(_user(), _sub(), 'ru', intro='✅ шапка')
    assert text.startswith('✅ шапка\n\n<b>Подключение устройства</b>')


@pytest.mark.asyncio
async def test_trial_intro(monkeypatch):
    monkeypatch.setattr(connect, 'trial_numbers', AsyncMock(return_value=(3, 2)))
    ru = await connect.trial_intro(MagicMock(), _user(), _sub())
    assert ru == '✅ Pro на 3 дня, до 16 декабря. Осталось подключить устройство.'
    en = await connect.trial_intro(MagicMock(), _user('en'), _sub())
    assert en == '✅ Pro for 3 days, until 16 December. Now connect a device.'


def test_install_skips_empty_links(monkeypatch):
    for name in ('IOS', 'ANDROID', 'WINDOWS', 'MACOS', 'PC'):
        monkeypatch.setattr(settings, f'HAPP_DOWNLOAD_LINK_{name}', None)
    monkeypatch.setattr(settings, 'HAPP_DOWNLOAD_LINK_IOS', 'https://ios')
    monkeypatch.setattr(settings, 'HAPP_DOWNLOAD_LINK_PC', 'https://pc')
    monkeypatch.setattr(settings, 'BB_HAPP_LINK_TV', '')
    monkeypatch.setattr(settings, 'BB_HAPP_LINK_APK', 'https://apk')
    text, kb = connect.bb_install_screen('ru')
    assert text.startswith('<b>Установите Happ</b>\nВыберите своё устройство.')
    assert _labels(kb) == [['iPhone', 'Windows', 'APK'], ['← Назад']]
    assert kb.inline_keyboard[0][1].url == 'https://pc'
    assert kb.inline_keyboard[1][0].callback_data == 'subscription_connect'


def test_install_layout_three_per_row(monkeypatch):
    for name in ('IOS', 'ANDROID', 'WINDOWS', 'MACOS'):
        monkeypatch.setattr(settings, f'HAPP_DOWNLOAD_LINK_{name}', f'https://{name}')
    monkeypatch.setattr(settings, 'BB_HAPP_LINK_TV', 'https://tv')
    monkeypatch.setattr(settings, 'BB_HAPP_LINK_APK', 'https://apk')
    _, kb = connect.bb_install_screen('en')
    assert _labels(kb) == [['iPhone', 'Android', 'Windows'], ['Mac', 'TV', 'APK'], ['← Back']]


@pytest.mark.asyncio
async def test_fetch_devices_failure_and_timeout(monkeypatch):
    from app.services import remnawave_service

    class Api:
        def __init__(self, result):
            self.result = result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get_user_devices(self, uid):
            if isinstance(self.result, Exception):
                raise self.result
            if self.result == 'hang':
                import asyncio

                await asyncio.sleep(10)
            return self.result

    def service(result):
        return lambda: SimpleNamespace(get_api_client=lambda: Api(result))

    sub = _sub()
    sub.remnawave_id = 'uuid'
    monkeypatch.setattr(connect, 'DEVICES_TIMEOUT', 0.05)
    monkeypatch.setattr(remnawave_service, 'RemnaWaveService', service({'total': 2, 'devices': [1, 2]}))
    assert await connect.fetch_devices_used(_user(), sub) == 2
    monkeypatch.setattr(remnawave_service, 'RemnaWaveService', service(RuntimeError('x')))
    assert await connect.fetch_devices_used(_user(), sub) is None
    monkeypatch.setattr(remnawave_service, 'RemnaWaveService', service('hang'))
    assert await connect.fetch_devices_used(_user(), sub) is None


@pytest.mark.asyncio
async def test_trial_intro_failure_is_none(monkeypatch):
    monkeypatch.setattr(connect, 'trial_numbers', AsyncMock(side_effect=RuntimeError('x')))
    assert await connect.trial_intro(MagicMock(), _user(), _sub()) is None


@pytest.mark.asyncio
async def test_handle_connect_uses_bb_screen_when_menu_on(monkeypatch):
    from unittest.mock import patch

    from aiogram.types import CallbackQuery, Message

    from app.handlers.subscription import links

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    callback = AsyncMock(spec=CallbackQuery)
    callback.message = AsyncMock(spec=Message)
    callback.data = 'subscription_connect'
    sub = _sub()
    shown = AsyncMock()
    with (
        patch.object(links, '_resolve_subscription', new=AsyncMock(return_value=(sub, 11))),
        patch('app.bb.connect.show_connect', new=shown),
        patch('app.config.Settings.is_multi_tariff_enabled', return_value=False),
    ):
        await links.handle_connect_subscription(callback, _user(), MagicMock(), bb_intro='I')
    shown.assert_awaited_once()
    assert shown.await_args.kwargs == {'intro': 'I'}


@pytest.mark.parametrize('link', [LINK, 'https://sub.example.com/' + 'a' * 300])
def test_hidden_link_not_shown(monkeypatch, link):
    monkeypatch.setattr(settings, 'HIDE_SUBSCRIPTION_LINK', True)
    text, kb = connect.bb_connect_screen(_user(), _sub(link), 'ru')
    assert link not in text
    assert '<code>' not in text
    assert all(b.copy_text is None for row in kb.inline_keyboard for b in row)
    assert _labels(kb) == [['📲 Открыть в Happ'], ['⬇️ Установить Happ'], ['← Назад']]


def test_hidden_link_off_unchanged(monkeypatch):
    monkeypatch.setattr(settings, 'HIDE_SUBSCRIPTION_LINK', False)
    _, kb = connect.bb_connect_screen(_user(), _sub(), 'ru')
    assert kb.inline_keyboard[1][0].copy_text.text == LINK
