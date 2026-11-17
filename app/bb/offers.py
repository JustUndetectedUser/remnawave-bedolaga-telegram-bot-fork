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
Сообщения админам и приветствие группы шлёт сам guest-key. Бот пишет сам после /start offer_<код> одно сообщение:
ссылку уже взял другой аккаунт (409) или она умерла (404) - короткое «ссылка уже использована» / «больше не действует»
(after_start, до меню); предложение записано (204) - «Вам доступен уникальный тариф» с кнопкой «Подключить» или
«Заявка на рассмотрении» (send_offer, после приветствия или меню, последним в чате), при любом BB_CHAT_MENU.
Пустой BB_OFFERS_URL: механизма нет, ни одного запроса.

Сбой связи (таймаут, нет соединения, 5xx, неожиданный ответ) не значит «предложения нет»: при нажатии это
Unavailable («попробуйте ещё раз», кнопка остаётся), и кэш он не трогает. «Предложения нет» говорят только 404/409
и успешный GET без него.
"""

import hashlib
import html
import re
import time
from enum import StrEnum
from typing import Any

import aiohttp
import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from app.bb.texts import bb_text
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
# Кнопка «Подключить» в сообщении после /start offer_<код>: своя, нажатие узнаётся по ней, а не по сообщению
MESSAGE_CALLBACK_PREFIX = 'bb_offer_msg:'
WAIT_CALLBACK = 'bb_offer_wait'
STATES = frozenset({'open', 'pending'})

_START_RE = re.compile(r'offer_([A-Za-z0-9_-]{10,40})')
_CACHE_LIMIT = 4096

# telegram_id -> (до какого момента time.monotonic() верить, предложения)
_offers_cache: dict[int, tuple[float, list[dict]]] = {}
# telegram_id -> код, записанный after_start этого /start: сообщение о нём шлёт send_offer после меню (один раз)
_announce: dict[int, str] = {}
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class Unavailable(Exception):
    """guest-key не ответил или ответил не по договору; о самих предложениях это ничего не говорит."""


class Registered(StrEnum):
    """Итог записи кода ссылки (POST /register)."""

    OK = 'ok'  # 204: записано (или уже было); правду о кнопке говорит GET
    TAKEN = 'taken'  # 409: одноразовое приглашение уже у другого аккаунта
    DEAD = 'dead'  # 404: ссылка не живая (отозвана, истекла, нет такой)
    UNAVAILABLE = 'unavailable'  # сеть, таймаут, 401, 5xx: код стоит прислать ещё раз
    SKIPPED = 'skipped'  # механизм выключен (пустой BB_OFFERS_URL) или нет аккаунта: ничего не спрошено


# Что сказать человеку после /start offer_<код>: только когда ссылка ему уже не достанется
_NOTICES = {Registered.TAKEN: 'offer_taken', Registered.DEAD: 'offer_dead'}


def offer_code(payload: str | None) -> str | None:
    """Код из аргумента /start вида offer_<код>, иначе None (аргумент для Bedolaga)."""
    match = _START_RE.fullmatch(payload or '')
    return match.group(1) if match else None


def offer_hash(offer_id: str) -> str:
    return hashlib.sha256(offer_id.encode('utf-8')).hexdigest()[:16]


def offer_callback(offer_id: str) -> str:
    """callback_data кнопки: id предложения бывает длиннее 64 байт, поэтому хэш."""
    return CALLBACK_PREFIX + offer_hash(offer_id)


def message_callback(offer_id: str) -> str:
    """callback_data кнопки «Подключить» в сообщении (offer_message): тот же хэш, свой префикс."""
    return MESSAGE_CALLBACK_PREFIX + offer_hash(offer_id)


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


async def register_offer(user, code: str) -> Registered:
    """Код ссылки промогруппы -> предложение этого аккаунта. Без сообщений человеку: итог видно по fetch_offers.

    204 не обещает открытого предложения (например, решение по заявке уже было): правду говорит GET.
    """
    return await _register(user, code)


async def _register(user, code: str) -> Registered:
    """POST /register. UNAVAILABLE: guest-key не ответил или ответил неожиданно, код стоит прислать ещё раз."""
    if not _ready(user):
        return Registered.SKIPPED
    if not _START_RE.fullmatch(f'offer_{code}'):
        return Registered.DEAD
    try:
        status, body = await _request('POST', f'{_url()}/register', user, {'code': code})
        if status == 204:
            logger.info('BlackBird: предложение промогруппы записано', user_id=user.id)
            return Registered.OK
        if status in {404, 409}:
            # not_found: ссылка не живая; taken: одноразовое приглашение уже у другого аккаунта.
            logger.info('BlackBird: предложение не записано', status=status, error=_error_code(body), user_id=user.id)
            return Registered.TAKEN if status == 409 else Registered.DEAD
        logger.warning(
            'BlackBird: guest-key не записал предложение', status=status, error=_error_code(body), user_id=user.id
        )
        return Registered.UNAVAILABLE
    except Exception as error:
        logger.warning('BlackBird: guest-key недоступен, предложение не записано', error=repr(error), user_id=user.id)
        return Registered.UNAVAILABLE
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


async def after_start(user, bot=None) -> Registered | None:
    """Человек зарегистрирован: записать предложение по коду из /start offer_<код>, если код ждёт.

    None: кода нет (обычный /start). Не зависит от меню переписки: предложение показывает и кабинет.
    TAKEN или DEAD: ссылка этому аккаунту уже не достанется, бот пишет одно короткое сообщение (bot - бот этого
    обновления; без меню и без ссылки на кабинет, тоже при любом BB_CHAT_MENU). Вызовы в app/handlers/start.py стоят
    до приветствия и меню: сообщение приходит перед ними, меню с кнопками остаётся последним.
    OK: записано; сообщение с кнопкой «Подключить» шлёт send_offer, его вызовы стоят после приветствия или меню.
    UNAVAILABLE: guest-key не ответил, код снова ждёт (следующий /start), чтобы человеку не пришлось искать ссылку
    ещё раз; молча.
    """
    if not _ready(user):
        return None
    # Прошлый /start записал код, но до send_offer не дошёл (путь упал): то сообщение уже не шлём.
    _announce.pop(user.telegram_id, None)
    key = _code_key(user.telegram_id)
    try:
        code = await cache.getdel(key)
        if not code:
            return None
        outcome = await _register(user, str(code))
        if outcome is Registered.UNAVAILABLE:
            await cache.set(key, str(code), expire=CODE_TTL)
    except Exception as error:
        logger.warning('BlackBird: код предложения не обработан', error=repr(error), user_id=user.id)
        return None
    if outcome is Registered.OK:
        _announce[user.telegram_id] = str(code)
    await _tell(bot, user, outcome)
    return outcome


async def _tell(bot, user, outcome: Registered) -> None:
    """«Эта ссылка уже использована другим аккаунтом.» / «Ссылка больше не действует.»: простой текст в чат."""
    key = _NOTICES.get(outcome)
    if key is None or bot is None:
        return
    try:
        await bot.send_message(user.telegram_id, bb_text(getattr(user, 'language', None), key))
    except Exception as error:
        logger.warning(
            'BlackBird: сообщение о ссылке не отправлено', outcome=str(outcome), error=repr(error), user_id=user.id
        )


def offer_message(offer: dict, language: str | None) -> tuple[str, InlineKeyboardMarkup | None]:
    """Сообщение о предложении (HTML; название и текст сервера экранированы, как в меню переписки).

    open: «🎁 Вам доступен уникальный тариф», название, текст и кнопка «Подключить» (message_callback; нажатие
    в app/bb/main_menu.py, on_offer); pending: «⏳ Заявка на рассмотрении» и название, без кнопки; любое другое
    (нажатие привело в группу или выдало тариф): «✅ Готово» и название, без кнопки.
    """
    title = html.escape(offer.get('title') or '')
    state = offer.get('state')
    if state == 'open':
        head, body = bb_text(language, 'offer_available'), html.escape(offer.get('text') or '')
        button = InlineKeyboardButton(
            text=bb_text(language, 'btn_offer_connect'), callback_data=message_callback(offer['id'])
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[button]])
    else:
        head, body, markup = bb_text(language, 'offer_pending' if state == 'pending' else 'offer_done'), '', None
    block = '\n'.join(part for part in (f'<b>{title}</b>' if title else '', body) if part)
    return (f'{head}\n\n{block}' if block else head), markup


async def send_offer(user, bot) -> None:
    """После приветствия или меню /start offer_<код>: сообщение о предложении этой ссылки, последним в чате.

    Только если after_start этого /start записал код (204), и только предложение с id «<kind>:<код>»: открытое -
    с кнопкой «Подключить», pending - «Заявка на рассмотрении». Его нет в списке (решение уже было, ссылка умерла)
    или guest-key не ответил: молча. При любом BB_CHAT_MENU (это не меню переписки). Один раз: второй вызов ничего
    не шлёт. Исключений нет: ошибка только в лог, /start и регистрация идут дальше.
    """
    code = _announce.pop(getattr(user, 'telegram_id', None), None)
    if code is None or bot is None:
        return
    try:
        offers = await fetch_offers(user)
        offer = next((o for o in offers if o['id'].rpartition(':')[2] == code), None)
        if offer is None:
            logger.info('BlackBird: предложения этой ссылки нет в списке, сообщения нет', user_id=user.id)
            return
        text, markup = offer_message(offer, getattr(user, 'language', None))
        await bot.send_message(
            user.telegram_id, text, reply_markup=markup, parse_mode='HTML', link_preview_options=_NO_PREVIEW
        )
    except Exception as error:
        logger.warning('BlackBird: сообщение о предложении не отправлено', error=repr(error), user_id=user.id)
