"""Кнопка «Веб-кабинет»: одноразовая ссылка на кабинет в браузере с входом (app/bb/browser_login.py)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from fastapi import HTTPException

from app.bb import browser_login, entry
from app.cabinet.auth.jwt_handler import JWT_ALGORITHM, create_access_token, create_auto_login_token
from app.config import settings


SECRET = 'test-cabinet-secret-0123456789abcdef'


class _FakeCache:
    def __init__(self, connected=True):
        self.connected = connected
        self.data = {}

    async def setnx(self, key, value, expire=None):
        if not self.connected or key in self.data:
            return False
        self.data[key] = (value, expire)
        return True


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', SECRET)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cab.example/')
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    fake = _FakeCache()
    monkeypatch.setattr(browser_login, 'cache', fake)
    return fake


def _token_from(url: str) -> str:
    parts = urlsplit(url)
    assert f'{parts.scheme}://{parts.netloc}{parts.path}' == 'https://cab.example/auto-login'
    return parse_qs(parts.query)['token'][0]


@pytest.mark.asyncio
async def test_web_button_sends_protected_one_time_link():
    msg = MagicMock()
    msg.answer = AsyncMock()
    msg.delete = AsyncMock()

    await entry.on_web_button(msg, SimpleNamespace(id=42, telegram_id=7, language='ru'))

    msg.delete.assert_awaited_once()
    args, kwargs = msg.answer.await_args
    assert args[0] == (
        'Кабинет откроется в браузере, уже с входом в ваш аккаунт. Ссылка одноразовая и действует 10 минут.'
    )
    assert kwargs['protect_content'] is True
    rows = kwargs['reply_markup'].inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1
    button = rows[0][0]
    assert button.text == 'Открыть в браузере'
    assert button.web_app is None
    payload = jwt.decode(_token_from(button.url), SECRET, algorithms=[JWT_ALGORITHM])
    assert payload['sub'] == '42'
    assert payload['type'] == 'bb_browser_login'
    assert payload['jti']
    assert payload['exp'] - payload['iat'] == 600


@pytest.mark.asyncio
async def test_web_button_english():
    msg = MagicMock()
    msg.answer = AsyncMock()
    msg.delete = AsyncMock()
    await entry.on_web_button(msg, SimpleNamespace(id=42, telegram_id=7, language='en'))
    args, kwargs = msg.answer.await_args
    assert args[0] == 'Your account opens in the browser, already signed in. The link works once, for 10 minutes.'
    assert kwargs['reply_markup'].inline_keyboard[0][0].text == 'Open in browser'


def test_each_link_is_new():
    assert browser_login.browser_login_url(42) != browser_login.browser_login_url(42)


def test_link_falls_back_to_miniapp_url(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_URL', '')
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cab.example')
    _token_from(browser_login.browser_login_url(42))


@pytest.mark.asyncio
async def test_redeem_once(_env):
    token = browser_login.create_browser_login_token(42)
    payload = await browser_login.redeem_browser_login_token(token)
    assert payload is not None and payload['sub'] == '42'
    assert await browser_login.redeem_browser_login_token(token) is None
    # Метка живёт дольше самого токена
    ((_, expire),) = _env.data.values()
    assert expire > browser_login.TTL_SECONDS


@pytest.mark.asyncio
async def test_redeem_refuses_without_redis(monkeypatch):
    monkeypatch.setattr(browser_login, 'cache', _FakeCache(connected=False))
    token = browser_login.create_browser_login_token(42)
    assert await browser_login.redeem_browser_login_token(token) is None


@pytest.mark.asyncio
async def test_redeem_refuses_other_tokens():
    now = datetime.now(UTC)
    expired = jwt.encode(
        {
            'sub': '42',
            'type': 'bb_browser_login',
            'jti': 'x',
            'iat': now - timedelta(hours=1),
            'exp': now - timedelta(minutes=1),
        },
        SECRET,
        algorithm=JWT_ALGORITHM,
    )
    no_jti = jwt.encode(
        {'sub': '42', 'type': 'bb_browser_login', 'iat': now, 'exp': now + timedelta(minutes=5)},
        SECRET,
        algorithm=JWT_ALGORITHM,
    )
    foreign = jwt.encode(
        {'sub': '42', 'type': 'bb_browser_login', 'jti': 'y', 'iat': now, 'exp': now + timedelta(minutes=5)},
        'another-secret-0123456789abcdef0123',
        algorithm=JWT_ALGORITHM,
    )
    for token in (expired, no_jti, foreign, create_auto_login_token(42), create_access_token(42, 7)):
        assert await browser_login.redeem_browser_login_token(token) is None


# --- POST /cabinet/auth/login/auto ---


def _patch_route(monkeypatch, *, admin: bool):
    from app.cabinet.routes import auth as routes
    from app.database.models import UserStatus

    user = SimpleNamespace(id=42, telegram_id=7, status=UserStatus.ACTIVE.value, cabinet_last_login=None)
    monkeypatch.setattr(routes.RateLimitCache, 'is_ip_rate_limited', AsyncMock(return_value=False))
    monkeypatch.setattr(routes, 'get_client_ip', lambda request: '203.0.113.5')
    monkeypatch.setattr(routes, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(routes, 'is_user_admin_by_env', lambda u: SimpleNamespace(is_admin=admin))
    response = SimpleNamespace(refresh_token='refresh')
    monkeypatch.setattr(routes, '_create_auth_response', AsyncMock(return_value=response))
    monkeypatch.setattr(routes, '_store_refresh_token', AsyncMock())
    db = MagicMock()
    db.commit = AsyncMock()
    return routes, db, response


@pytest.mark.asyncio
@pytest.mark.parametrize('admin', [False, True])
async def test_endpoint_accepts_bot_link_once(monkeypatch, admin):
    routes, db, response = _patch_route(monkeypatch, admin=admin)
    token = browser_login.create_browser_login_token(42)
    request = routes.AutoLoginRequest(token=token)

    assert await routes.auto_login(request, MagicMock(), db) is response
    routes.get_user_by_id.assert_awaited_once_with(db, 42)

    with pytest.raises(HTTPException) as error:
        await routes.auto_login(request, MagicMock(), db)
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_endpoint_still_blocks_admin_guest_purchase_token(monkeypatch):
    routes, db, _ = _patch_route(monkeypatch, admin=True)
    request = routes.AutoLoginRequest(token=create_auto_login_token(42))
    with pytest.raises(HTTPException) as error:
        await routes.auto_login(request, MagicMock(), db)
    assert error.value.status_code == 403
