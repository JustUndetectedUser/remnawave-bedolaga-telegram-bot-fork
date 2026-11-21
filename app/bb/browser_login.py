"""Одноразовая ссылка «Веб-кабинет»: кабинет в браузере телефона сразу с входом.

Бот кладёт в ссылку JWT своего типа (bb_browser_login) на TTL_SECONDS. Страница кабинета /auto-login отдаёт его
в POST /cabinet/auth/login/auto (тот же путь, что у ссылок после гостевой покупки), там его принимает
redeem_browser_login_token. Ссылка срабатывает один раз: номер токена (jti) помечается в Redis при первом входе.

Админам вход по такой ссылке разрешён, в отличие от токенов гостевой покупки: те выдаются по @username без
доказательства, что аккаунт свой, а эту ссылку бот присылает только в личный чат владельца аккаунта
(сообщение нельзя переслать: protect_content).
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.cabinet.auth.jwt_handler import JWT_ALGORITHM, get_token_payload
from app.config import settings
from app.utils.cache import cache


TOKEN_TYPE = 'bb_browser_login'
TTL_SECONDS = 10 * 60


def _used_key(jti: str) -> str:
    return f'bb_browser_login_used:{jti}'


def create_browser_login_token(user_id: int) -> str:
    now = datetime.now(UTC)
    payload = {
        'sub': str(user_id),
        'type': TOKEN_TYPE,
        'jti': secrets.token_urlsafe(16),
        'iat': now,
        'exp': now + timedelta(seconds=TTL_SECONDS),
    }
    return jwt.encode(payload, settings.get_cabinet_jwt_secret(), algorithm=JWT_ALGORITHM)


def browser_login_url(user_id: int) -> str:
    base = settings._normalized_cabinet_url() or (settings.MINIAPP_CUSTOM_URL or '').strip().rstrip('/')
    return f'{base}/auto-login?token={create_browser_login_token(user_id)}'


async def redeem_browser_login_token(token: str) -> dict[str, Any] | None:
    """Payload, если токен наш, не истёк и ещё не использован; иначе None.

    Без Redis setnx отвечает False, и вход по ссылке не проходит: лучше отказать, чем пустить дважды.
    """
    payload = get_token_payload(token, expected_type=TOKEN_TYPE)
    if not payload:
        return None
    jti = payload.get('jti')
    if not isinstance(jti, str) or not jti:
        return None
    if not await cache.setnx(_used_key(jti), 1, expire=TTL_SECONDS + 60):
        return None
    return payload
