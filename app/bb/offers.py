"""Уникальный тариф промогруппы: клиент guest-key и /start offer_<код>.

Ссылка промогруппы (страница guest-key) ведёт незарегистрированного в бот: /start offer_<код>. Bedolaga этот
аргумент не видит (stash_start_payload: хуки в app/handlers/start.py и app/middlewares/channel_checker.py), код
ждёт в Redis сутки; FSM для этого не годится, пути регистрации его чистят. После регистрации (или сразу, если
человек уже зарегистрирован) after_start записывает предложение на сервере, и кнопка появляется в кабинете и в меню
переписки (строка в app/bb/main_menu.py).

guest-key: внутренний адрес BB_OFFERS_URL (http://guest-key:8091/invite/offers), токен кабинета этого человека.
  GET  {url}           -> 200 {"offers": [{"id", "title", "text", "state": "open" | "pending"}]}
  POST {url}/register  {"code"} -> 204; 404 not_found; 409 taken
  POST {url}/claim     {"id"}   -> 200 {"state": "pending" | "member" | "granted"}; 404 not_found; 409 decided
Сообщения админам и приветствие группы шлёт сам guest-key, бот своих не пишет. Пустой BB_OFFERS_URL: механизма нет,
ни одного запроса.

Сбой связи (таймаут, нет соединения, 5xx, неожиданный ответ) не значит «предложения нет»: при нажатии это
Unavailable («попробуйте ещё раз», кнопка остаётся), и кэш он не трогает. «Предложения нет» говорят только 404/409
и успешный GET без него.
"""

import hashlib
import re
import time
from typing import Any

import aiohttp
import structlog

from app.cabinet.auth.jwt_handler import create_access_token
from app.config import settings
from app.utils.cache import cache


logger = structlog.get_logger(__name__)

TIMEOUT = 2.0  # секунды на весь запрос: меню и регистрация не ждут упавший guest-key дольше
# Нажатие: guest-key до ответа спрашивает кабинет и шлёт сообщения админам и приветствие группы.
CLAIM_TIMEOUT = 10.0
CACHE_TTL = 60  # предложения человека в памяти процесса; запись и нажатие сбрасывают
CODE_TTL = 24 * 3600  # код из /start offer_<код> ждёт регистрации сутки
CALLBACK_PREFIX = 'bb_offer:'
WAIT_CALLBACK = 'bb_offer_wait'
STATES = frozenset({'open', 'pending'})

_START_RE = re.compile(r'offer_([A-Za-z0-9_-]{10,40})')
_CACHE_LIMIT = 4096

# telegram_id -> (до какого момента time.monotonic() верить, предложения)
_offers_cache: dict[int, tuple[float, list[dict]]] = {}


class Unavailable(Exception):
    """guest-key не ответил или ответил не по договору; о самих предложениях это ничего не говорит."""


def offer_code(payload: str | None) -> str | None:
    """Код из аргумента /start вида offer_<код>, иначе None (аргумент для Bedolaga)."""
    match = _START_RE.fullmatch(payload or '')
    return match.group(1) if match else None


def offer_hash(offer_id: str) -> str:
    return hashlib.sha256(offer_id.encode('utf-8')).hexdigest()[:16]


def offer_callback(offer_id: str) -> str:
    """callback_data кнопки: id предложения бывает длиннее 64 байт, поэтому хэш."""
    return CALLBACK_PREFIX + offer_hash(offer_id)


def _url() -> str:
    return str(getattr(settings, 'BB_OFFERS_URL', '') or '').strip().rstrip('/')


def _code_key(telegram_id: int) -> str:
    return f'bb_offer_code:{telegram_id}'


def _forget(user) -> None:
    _offers_cache.pop(getattr(user, 'telegram_id', None), None)


def _remember(telegram_id: int, offers: list[dict]) -> None:
    now = time.monotonic()
    if len(_offers_cache) >= _CACHE_LIMIT:
        for key in [key for key, (until, _) in _offers_cache.items() if until <= now]:
            del _offers_cache[key]
    _offers_cache[telegram_id] = (now + CACHE_TTL, offers)


def _error_code(body: Any) -> str | None:
    return body.get('error') if isinstance(body, dict) else None


async def _request(
    method: str, url: str, user, payload: dict | None = None, *, timeout: float = TIMEOUT
) -> tuple[int, Any]:
    """Статус и JSON ответа (None, если тела нет или это не JSON). timeout на весь запрос."""
    token = create_access_token(user.id, user.telegram_id)
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session,
        session.request(method, url, headers=headers, json=payload) as response,
    ):
        try:
            body = await response.json(content_type=None)
        except Exception:
            body = None
        return response.status, body


def _parse(body: Any) -> list[dict] | None:
    """None: ответ не по договору (нет списка offers). Непонятные элементы списка пропускаются."""
    items = body.get('offers') if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None
    offers = []
    for item in items:
        if not isinstance(item, dict):
            continue
        offer_id, state = item.get('id'), item.get('state')
        if not isinstance(offer_id, str) or not offer_id or not isinstance(state, str) or state not in STATES:
            continue
        offers.append(
            {
                'id': offer_id,
                'title': str(item.get('title') or ''),
                'text': str(item.get('text') or ''),
                'state': state,
            }
        )
    return offers


def _ready(user) -> bool:
    return bool(_url()) and bool(getattr(user, 'telegram_id', None)) and getattr(user, 'id', None) is not None


async def load_offers(user) -> list[dict]:
    """Предложения человека мимо кэша (нажатие кнопки). Успешный ответ запоминается на CACHE_TTL.

    Unavailable: сбой связи или ответ не по договору; кэш при этом не меняется.
    """
    if not _ready(user):
        return []
    try:
        status, body = await _request('GET', _url(), user)
    except Exception as error:
        logger.warning('BlackBird: guest-key недоступен, предложения не получены', error=repr(error), user_id=user.id)
        raise Unavailable from error
    offers = _parse(body) if status == 200 else None
    if offers is None:
        logger.warning(
            'BlackBird: guest-key не отдал предложения', status=status, error=_error_code(body), user_id=user.id
        )
        raise Unavailable(f'GET {status}')
    _remember(user.telegram_id, offers)
    return list(offers)


async def fetch_offers(user) -> list[dict]:
    """Предложения для меню: из кэша (CACHE_TTL), иначе с сервера. Таймаут TIMEOUT, исключений нет.

    Сбой ничего не запоминает: меню получает последний известный ответ этого человека, а если его нет, [] (меню
    без строки).
    """
    if not _ready(user):
        return []
    cached = _offers_cache.get(user.telegram_id)
    if cached and cached[0] > time.monotonic():
        return list(cached[1])
    try:
        return await load_offers(user)
    except Unavailable:
        return list(cached[1]) if cached else []


async def register_offer(user, code: str) -> None:
    """Код ссылки промогруппы -> предложение этого аккаунта. Молча: итог видно по fetch_offers.

    204 не обещает открытого предложения (например, решение по заявке уже было): правду говорит GET.
    """
    await _register(user, code)


async def _register(user, code: str) -> bool:
    """False: guest-key не ответил или ответил неожиданно (сеть, таймаут, 401, 5xx): код стоит прислать ещё раз."""
    if not _ready(user) or not _START_RE.fullmatch(f'offer_{code}'):
        return True
    try:
        status, body = await _request('POST', f'{_url()}/register', user, {'code': code})
        if status == 204:
            logger.info('BlackBird: предложение промогруппы записано', user_id=user.id)
            return True
        if status in {404, 409}:
            # not_found: ссылка не живая; taken: одноразовое приглашение уже у другого аккаунта.
            logger.info('BlackBird: предложение не записано', status=status, error=_error_code(body), user_id=user.id)
            return True
        logger.warning(
            'BlackBird: guest-key не записал предложение', status=status, error=_error_code(body), user_id=user.id
        )
        return False
    except Exception as error:
        logger.warning('BlackBird: guest-key недоступен, предложение не записано', error=repr(error), user_id=user.id)
        return False
    finally:
        _forget(user)


async def claim_offer(user, offer_id: str) -> str | None:
    """Нажатие «Вам доступен уникальный тариф»: pending, member или granted; None: предложения нет (404, 409).

    Unavailable: сбой связи или ответ не по договору. guest-key мог нажатие и выполнить, поэтому кэш остаётся,
    а правду покажет следующий запрос.
    """
    if not _ready(user):
        return None
    try:
        status, body = await _request('POST', f'{_url()}/claim', user, {'id': offer_id}, timeout=CLAIM_TIMEOUT)
    except Exception as error:
        logger.warning('BlackBird: guest-key недоступен, нажатие не дошло', error=repr(error), user_id=user.id)
        raise Unavailable from error
    state = body.get('state') if isinstance(body, dict) else None
    if status == 200 and isinstance(state, str) and state:
        _forget(user)
        logger.info('BlackBird: предложение нажато', state=state, user_id=user.id)
        return state
    if status in {404, 409}:
        _forget(user)
        logger.info('BlackBird: предложение не принято', status=status, error=_error_code(body), user_id=user.id)
        return None
    logger.warning('BlackBird: guest-key не принял нажатие', status=status, error=_error_code(body), user_id=user.id)
    raise Unavailable(f'claim {status}')


async def stash_start_payload(telegram_id: int | None, payload: str | None) -> bool:
    """/start offer_<код>: True, и Bedolaga этот аргумент не видит; код ждёт регистрации в Redis.

    Код не пропуск: при закрытой регистрации (INVITE_ONLY) Bedolaga решает без него.
    """
    code = offer_code(payload)
    if code is None:
        return False
    if _url() and telegram_id:
        try:
            if not await cache.set(_code_key(telegram_id), code, expire=CODE_TTL):
                logger.warning('BlackBird: код предложения не сохранён, Redis недоступен', telegram_id=telegram_id)
        except Exception as error:
            logger.warning('BlackBird: код предложения не сохранён', telegram_id=telegram_id, error=repr(error))
    return True


async def after_start(user) -> None:
    """Человек зарегистрирован: записать предложение по коду из /start offer_<код>, если код ждёт. Молча.

    Не зависит от меню переписки: предложение показывает и кабинет. guest-key не ответил: код снова ждёт
    (следующий /start), чтобы человеку не пришлось искать ссылку ещё раз.
    """
    if not _ready(user):
        return
    key = _code_key(user.telegram_id)
    try:
        code = await cache.getdel(key)
        if code and not await _register(user, str(code)):
            await cache.set(key, str(code), expire=CODE_TTL)
    except Exception as error:
        logger.warning('BlackBird: код предложения не обработан', error=repr(error), user_id=user.id)
