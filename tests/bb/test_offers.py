"""Уникальный тариф промогруппы в меню переписки: /start offer_<код>, строка в меню, нажатие.

Сервер (guest-key) здесь настоящий HTTP-сервер на 127.0.0.1: проверяются и запросы бота (адрес, токен кабинета,
тело), и то, что меню не ждёт упавший сервер дольше таймаута.
"""

import asyncio
import hashlib
import inspect
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.bb import main_menu, offers
from app.bb.main_menu import bb_main_menu
from app.config import settings
from app.handlers import start
from app.middlewares import channel_checker


CODE = 'Ab3_k9-ZzQ1x'
OFFER_ID = 'group:mcksp:' + 'x' * 70  # длиннее 64 байт callback_data
SECRET = 'test-cabinet-secret-of-at-least-32-bytes'


def _user(*, language='ru', telegram_id=7, subscription=None, status='active'):
    return SimpleNamespace(
        id=42,
        telegram_id=telegram_id,
        language=language,
        balance_kopeks=12000,
        auth_type='telegram',
        is_trial_already_used=lambda: True,
        subscription=subscription,
        status=status,
    )


def _sub(status='active', *, is_trial=False, tag='PAID_PRO', name='PRO'):
    from datetime import UTC, datetime

    tariff = SimpleNamespace(id=3, name=name, panel_tag=tag)
    return SimpleNamespace(
        actual_status=status,
        is_trial=is_trial,
        tariff=tariff,
        tariff_id=tariff.id,
        end_date=datetime(datetime.now(UTC).year, 12, 16, 12, tzinfo=UTC),
    )


def _callbacks(kb):
    return [[b.callback_data for b in row] for row in kb.inline_keyboard]


def _labels(kb):
    return [[b.text for b in row] for row in kb.inline_keyboard]


OPEN = {'id': OFFER_ID, 'title': 'MCKSP <группа> & друзья', 'text': 'Ещё один сервер', 'state': 'open'}
PENDING = {'id': OFFER_ID, 'title': 'Unlimited Pro', 'text': 'Полный безлимит', 'state': 'pending'}


class FakeCache:
    """app.utils.cache: get/set/getdel/delete поверх словаря."""

    def __init__(self):
        self.data = {}
        self.ttl = {}

    async def set(self, key, value, expire=None):
        self.data[key] = value
        self.ttl[key] = expire
        return True

    async def get(self, key):
        return self.data.get(key)

    async def getdel(self, key):
        return self.data.pop(key, None)

    async def delete(self, key):
        return self.data.pop(key, None) is not None


class GuestKey:
    """Поддельный guest-key: GET /invite/offers, POST /register, POST /claim."""

    def __init__(self):
        self.offers = []
        self.requests = []
        self.register_status = 204
        self.claim_reply = (200, {'state': 'pending'})
        self.claim_delay = 0.0  # guest-key долго спрашивает кабинет и шлёт сообщения
        self.on_register = [OPEN]  # предложения человека после записи кода (204)
        self.list_status = 200  # GET /invite/offers: не 200 - сбой guest-key
        self.server = None

    def _record(self, request, body):
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        self.requests.append(
            {
                'method': request.method,
                'path': request.path,
                'body': body,
                'content_type': request.headers.get('Content-Type'),
                'token': jwt.decode(token, SECRET, algorithms=['HS256']),
            }
        )

    async def list_offers(self, request):
        self._record(request, None)
        if self.list_status != 200:
            return web.json_response({'error': 'internal'}, status=self.list_status)
        return web.json_response({'offers': self.offers})

    async def register(self, request):
        body = await request.json()
        self._record(request, body)
        if self.register_status == 204:
            self.offers = [dict(offer) for offer in self.on_register]
            return web.Response(status=204)
        return web.json_response({'error': 'not_found'}, status=self.register_status)

    async def claim(self, request):
        body = await request.json()
        self._record(request, body)
        await asyncio.sleep(self.claim_delay)
        status, reply = self.claim_reply
        if status == 200:
            self.offers = [dict(o, state=reply['state']) for o in self.offers if o['id'] == body['id']]
        return web.json_response(reply, status=status)

    async def __aenter__(self):
        app = web.Application()
        app.router.add_get('/invite/offers', self.list_offers)
        app.router.add_post('/invite/offers/register', self.register)
        app.router.add_post('/invite/offers/claim', self.claim)
        self.server = TestServer(app)
        await self.server.start_server()
        return self

    async def __aexit__(self, *exc):
        await self.server.close()

    @property
    def url(self):
        return str(self.server.make_url('/invite/offers'))

    def calls(self, method=None, path=None):
        return [
            r
            for r in self.requests
            if (method is None or r['method'] == method) and (path is None or r['path'] == path)
        ]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', SECRET)
    monkeypatch.setattr(settings, 'BB_OFFERS_URL', '')
    fake = FakeCache()
    tariff = SimpleNamespace(id=3, name='PRO', panel_tag='PAID_PRO')
    monkeypatch.setattr(main_menu, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(offers, 'cache', fake)
    offers._offers_cache.clear()
    yield fake
    offers._offers_cache.clear()


# --- Меню -----------------------------------------------------------------------------------------------------


def test_menu_shows_offer_first_when_open():
    """Открытое предложение: первой строкой кнопка, над статусом название и строка о том, что даёт группа."""
    cases = [
        (None, 'Добро пожаловать в BlackBird.'),  # первый визит
        (_sub('expired', is_trial=True), 'Пробный период закончился.'),
        (_sub(tag='ENDLESS_PRO', name='Unlimited Pro'), 'Unlimited Pro · бессрочно'),
        (_sub(), 'Подписка Pro до 16 декабря'),
        (_sub('expired'), 'Подписка Pro закончилась'),
        (_sub('disabled'), 'Подписка Pro не активна'),
    ]
    for subscription, status_text in cases:
        plain_text, plain_kb = bb_main_menu(_user(subscription=subscription), subscription, 'ru')
        text, kb = bb_main_menu(_user(subscription=subscription), subscription, 'ru', offers=[OPEN])
        assert _labels(kb)[0] == ['🎁 Вам доступен уникальный тариф'], status_text
        assert _callbacks(kb)[0] == ['bb_offer:' + hashlib.sha256(OFFER_ID.encode()).hexdigest()[:16]]
        assert len(_callbacks(kb)[0][0].encode()) <= 64
        # Остальные кнопки меню на месте, под строкой предложения.
        assert _callbacks(kb)[1:] == _callbacks(plain_kb)
        # Текст сервера экранирован; блок над строкой статуса, потом пустая строка.
        assert text == '<b>MCKSP &lt;группа&gt; &amp; друзья</b>\nЕщё один сервер\n\n' + plain_text
        assert status_text in text

    _, kb_en = bb_main_menu(_user(language='en'), _sub(), 'en', offers=[OPEN])
    assert _labels(kb_en)[0] == ['🎁 A special plan is available to you']

    # Без предложений меню прежнее.
    assert bb_main_menu(_user(), _sub(), 'ru', offers=()) == bb_main_menu(_user(), _sub(), 'ru')


async def test_menu_shows_pending_row(env):
    text, kb = bb_main_menu(_user(), _sub(), 'ru', offers=[PENDING])
    assert _labels(kb)[0] == ['⏳ Заявка на рассмотрении']
    assert _callbacks(kb)[0] == ['bb_offer_wait']
    assert text.startswith('<b>Unlimited Pro</b>\n\nПодписка Pro до ')
    _, kb_en = bb_main_menu(_user(language='en'), _sub(), 'en', offers=[PENDING])
    assert _labels(kb_en)[0] == ['⏳ Request under review']

    # Нажатие на «Заявка на рассмотрении» только отвечает, без запросов и без нового меню.
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.data = 'bb_offer_wait'
    await main_menu.on_offer_wait(callback, _user())
    callback.answer.assert_awaited_once_with('⏳ Заявка на рассмотрении')


def test_texts_have_no_em_dash():
    from app.bb.texts import TEXTS

    for language in ('ru', 'en'):
        for key in (
            'btn_offer',
            'offer_pending',
            'offer_gone',
            'offer_retry',
            'offer_taken',
            'offer_dead',
            'offer_available',
            'btn_offer_connect',
        ):
            assert '—' not in TEXTS[language][key]


# --- Нажатие --------------------------------------------------------------------------------------------------


def _menu_callback(data):
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.chat.id = 7
    callback.message.message_id = 99
    callback.message.text = 'старое меню'
    callback.message.photo = None
    callback.bot.edit_message_text = AsyncMock()
    callback.bot.send_message = AsyncMock()
    return callback


async def test_claim_calls_api_and_rerenders_menu(env, monkeypatch):
    monkeypatch.setattr(main_menu, 'maybe_send_bb_keyboard_once', AsyncMock())
    user = _user(subscription=None)
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.offers = [dict(OPEN)]

        text, kb = await main_menu.build_bb_main_menu(user, MagicMock())
        assert _labels(kb)[0] == ['🎁 Вам доступен уникальный тариф']

        callback = _menu_callback(_callbacks(kb)[0][0])
        await main_menu.on_offer(callback, user, MagicMock())

        claims = server.calls('POST', '/invite/offers/claim')
        assert [c['body'] for c in claims] == [{'id': OFFER_ID}]
        assert claims[0]['content_type'] == 'application/json'
        assert claims[0]['token']['type'] == 'access'
        assert claims[0]['token']['telegram_id'] == 7
        assert claims[0]['token']['sub'] == '42'
        callback.answer.assert_awaited_once_with('⏳ Заявка на рассмотрении')
        # Меню на месте того же сообщения, уже со строкой «Заявка на рассмотрении».
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        assert kwargs['message_id'] == 99
        assert _callbacks(kwargs['reply_markup'])[0] == ['bb_offer_wait']
        # Бот сам ничего не пишет: сообщения админам и приветствие группы шлёт guest-key.
        callback.bot.send_message.assert_not_awaited()

        # Группа с автоодобрением: человек сразу в группе, строка исчезает, ответ без текста.
        server.offers = [dict(OPEN)]
        server.claim_reply = (200, {'state': 'member'})
        callback = _menu_callback(offers.offer_callback(OFFER_ID))
        await main_menu.on_offer(callback, user, MagicMock())
        callback.answer.assert_awaited_once_with()
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        assert all(not cb.startswith('bb_offer') for row in _callbacks(kwargs['reply_markup']) for cb in row)


async def test_claim_of_gone_offer_answers_and_rerenders(env, monkeypatch):
    monkeypatch.setattr(main_menu, 'maybe_send_bb_keyboard_once', AsyncMock())
    user = _user(subscription=None)
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.offers = [dict(OPEN)]
        await main_menu.build_bb_main_menu(user, MagicMock())  # в кэше ещё открытое предложение
        server.offers = []  # ссылку отозвали

        callback = _menu_callback(offers.offer_callback(OFFER_ID))
        await main_menu.on_offer(callback, user, MagicMock())
        assert server.calls('POST') == []
        callback.answer.assert_awaited_once_with('Предложение больше не доступно')
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        assert all(not cb.startswith('bb_offer') for row in _callbacks(kwargs['reply_markup']) for cb in row)

        # Решение уже принято (409 decided) или предложения нет (404 not_found): тот же короткий ответ.
        for reply in ((409, {'error': 'decided'}), (404, {'error': 'not_found'})):
            server.offers = [dict(OPEN)]
            server.claim_reply = reply
            callback = _menu_callback(offers.offer_callback(OFFER_ID))
            await main_menu.on_offer(callback, user, MagicMock())
            callback.answer.assert_awaited_once_with('Предложение больше не доступно')


def _has_offer_button(markup) -> bool:
    return _callbacks(markup)[0] == [offers.offer_callback(OFFER_ID)]


async def test_claim_failure_says_try_again_and_keeps_button(env, monkeypatch):
    """Сбой связи при нажатии не «предложение пропало»: guest-key мог и выполнить нажатие (MCKSP: человек уже
    в группе). Ответ «Не получилось, попробуйте ещё раз», кнопка остаётся, кэш не пустеет."""
    monkeypatch.setattr(main_menu, 'maybe_send_bb_keyboard_once', AsyncMock())
    monkeypatch.setattr(offers, 'CLAIM_TIMEOUT', 0.2)
    user = _user(subscription=None)
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        cases = [
            ('timeout', (200, {'state': 'member'}), 0.6),
            ('5xx', (500, {'error': 'internal'}), 0),
            ('unparseable', (200, {'oops': 1}), 0),
            ('401', (401, {'error': 'unauthorized'}), 0),
        ]
        for name, reply, delay in cases:
            server.offers = [dict(OPEN)]
            server.claim_reply, server.claim_delay = reply, delay
            callback = _menu_callback(offers.offer_callback(OFFER_ID))
            await main_menu.on_offer(callback, user, MagicMock())
            assert server.calls('POST', '/invite/offers/claim'), name
            callback.answer.assert_awaited_once_with('Не получилось, попробуйте ещё раз')
            kwargs = callback.bot.edit_message_text.await_args.kwargs
            assert _has_offer_button(kwargs['reply_markup']), name
            assert offers._offers_cache[7][1] == [OPEN], name
        await asyncio.sleep(0.5)  # медленный claim дорабатывает на сервере

    callback = _menu_callback(offers.offer_callback(OFFER_ID))
    await main_menu.on_offer(callback, _user(language='en', subscription=None), MagicMock())
    callback.answer.assert_awaited_once_with('Something went wrong, please try again')


async def test_late_answer_failure_still_rerenders_menu(env, monkeypatch):
    """Нажатие шло долго (до CLAIM_TIMEOUT), Telegram уже не принимает ответ («query is too old»): ошибка ответа
    в лог, меню всё равно перерисовано."""
    monkeypatch.setattr(main_menu, 'maybe_send_bb_keyboard_once', AsyncMock())
    user = _user(subscription=None)
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.offers = [dict(OPEN)]
        callback = _menu_callback(offers.offer_callback(OFFER_ID))
        callback.answer.side_effect = RuntimeError('Bad Request: query is too old and response timeout expired')
        await main_menu.on_offer(callback, user, MagicMock())
        assert len(server.calls('POST', '/invite/offers/claim')) == 1
        callback.answer.assert_awaited_once_with('⏳ Заявка на рассмотрении')
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        assert _callbacks(kwargs['reply_markup'])[0] == ['bb_offer_wait']


async def test_click_when_guest_key_unreachable_keeps_cached_offer(env, monkeypatch):
    """Свежий запрос при нажатии не прошёл (порт закрыт): «попробуйте ещё раз», открытое предложение из кэша
    остаётся и в кэше, и в меню; сбой ничего не запоминает."""
    monkeypatch.setattr(main_menu, 'maybe_send_bb_keyboard_once', AsyncMock())
    user = _user(subscription=None)
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.offers = [dict(OPEN)]
        await main_menu.build_bb_main_menu(user, MagicMock())
    entry = offers._offers_cache[7]

    probe = await asyncio.start_server(lambda r, w: None, '127.0.0.1', 0)
    closed_port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    monkeypatch.setattr(settings, 'BB_OFFERS_URL', f'http://127.0.0.1:{closed_port}/invite/offers')

    callback = _menu_callback(offers.offer_callback(OFFER_ID))
    await main_menu.on_offer(callback, user, MagicMock())
    callback.answer.assert_awaited_once_with('Не получилось, попробуйте ещё раз')
    assert _has_offer_button(callback.bot.edit_message_text.await_args.kwargs['reply_markup'])
    assert offers._offers_cache[7] is entry

    # Минута прошла, guest-key всё ещё недоступен: меню показывает последний известный ответ, кэш не трогает.
    stale = (time.monotonic() - 1, entry[1])
    offers._offers_cache[7] = stale
    callback = _menu_callback(offers.offer_callback(OFFER_ID))
    await main_menu.on_offer(callback, user, MagicMock())
    callback.answer.assert_awaited_once_with('Не получилось, попробуйте ещё раз')
    assert _has_offer_button(callback.bot.edit_message_text.await_args.kwargs['reply_markup'])
    assert offers._offers_cache[7] is stale


async def test_offers_cached_for_a_minute_and_dropped_by_claim(env, monkeypatch):
    user = _user()
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.offers = [dict(OPEN)]
        assert await offers.fetch_offers(user) == [OPEN]
        assert await offers.fetch_offers(user) == [OPEN]
        assert len(server.calls('GET')) == 1
        # Ответ живёт минуту; потом снова спрашивает сервер.
        until, cached = offers._offers_cache[7]
        assert offers.CACHE_TTL - 5 < until - time.monotonic() <= offers.CACHE_TTL == 60
        offers._offers_cache[7] = (time.monotonic() - 0.001, cached)
        await offers.fetch_offers(user)
        assert len(server.calls('GET')) == 2
        # После нажатия следующее меню берёт новое состояние.
        assert await offers.claim_offer(user, OFFER_ID) == 'pending'
        assert await offers.fetch_offers(user) == [dict(OPEN, state='pending')]
        assert len(server.calls('GET')) == 3


# --- guest-key недоступен -------------------------------------------------------------------------------------


async def _hanging_server():
    """Принимает соединение и молчит: guest-key завис."""
    release = asyncio.Event()

    async def handle(reader, writer):
        await release.wait()
        writer.close()

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    return server, release, f'http://127.0.0.1:{port}/invite/offers'


async def test_guest_key_down_menu_without_offer_under_2s(env, monkeypatch):
    assert offers.TIMEOUT <= 2
    user = _user(subscription=_sub())

    # Порт закрыт: ответ сразу.
    probe = await asyncio.start_server(lambda r, w: None, '127.0.0.1', 0)
    closed_port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    monkeypatch.setattr(settings, 'BB_OFFERS_URL', f'http://127.0.0.1:{closed_port}/invite/offers')
    started = time.monotonic()
    text, kb = await main_menu.build_bb_main_menu(user, AsyncMock())
    assert time.monotonic() - started < 2
    assert text.startswith('Подписка Pro до ')
    assert all(not cb.startswith('bb_offer') for row in _callbacks(kb) for cb in row)

    # Сервер завис: меню ждёт не дольше таймаута (2 с), без исключений.
    offers._offers_cache.clear()
    server, release, url = await _hanging_server()
    monkeypatch.setattr(settings, 'BB_OFFERS_URL', url)
    try:
        started = time.monotonic()
        text, kb = await main_menu.build_bb_main_menu(user, AsyncMock())
        elapsed = time.monotonic() - started
    finally:
        release.set()
        server.close()
        await server.wait_closed()
    assert elapsed < offers.TIMEOUT + 0.5
    assert text.startswith('Подписка Pro до ')
    assert all(not cb.startswith('bb_offer') for row in _callbacks(kb) for cb in row)

    # Сбой ничего не запоминает: следующее меню снова спросит guest-key.
    assert offers._offers_cache == {}


async def test_bad_answers_give_no_offers(env, monkeypatch):
    async def broken(request):
        return web.json_response({'error': 'unauthorized'}, status=401)

    async def garbage(request):
        return web.Response(text='<html>', content_type='text/html')

    async def odd(request):
        return web.json_response(
            {
                'offers': [
                    {'id': 'a', 'state': 'unknown'},
                    {'id': 'b', 'state': []},
                    {'title': 'no id', 'state': 'open'},
                    'x',
                    dict(OPEN),
                ]
            }
        )

    for handler, expected in ((broken, []), (garbage, []), (odd, [OPEN])):
        app = web.Application()
        app.router.add_get('/invite/offers', handler)
        server = TestServer(app)
        await server.start_server()
        try:
            monkeypatch.setattr(settings, 'BB_OFFERS_URL', str(server.make_url('/invite/offers')))
            offers._offers_cache.clear()
            assert await offers.fetch_offers(_user()) == expected
            # Ошибочный ответ не запоминается, разобранный запоминается.
            assert (7 in offers._offers_cache) == bool(expected)
        finally:
            await server.close()


# --- /start offer_<код> ---------------------------------------------------------------------------------------


class _StopStart(BaseException):
    """Останавливаем /start после проверяемого места, до меню Bedolaga."""


def _start_message(text, telegram_id=7):
    return SimpleNamespace(text=text, from_user=SimpleNamespace(id=telegram_id, username='person'), bot=object())


def _start_mocks(monkeypatch):
    access = AsyncMock(return_value=SimpleNamespace(allowed=True, reason=None))
    campaign = AsyncMock(return_value=None)
    monkeypatch.setattr(start, 'get_pending_payload_from_redis', AsyncMock(return_value=None))
    monkeypatch.setattr(start, '_evaluate_telegram_registration_access', access)
    monkeypatch.setattr(start, 'get_campaign_by_start_parameter', campaign)
    monkeypatch.setattr(start, 'get_user_by_referral_code', AsyncMock(return_value=None))
    return access, campaign


@pytest.mark.parametrize('chat_menu', ['all', 'off'])
async def test_start_offer_for_registered_user_registers_offer(env, monkeypatch, chat_menu):
    """Уже зарегистрированный: предложение записано сразу (и при выключенном меню переписки: его покажет кабинет),
    Bedolaga код не видит: ни кампании, ни реферального кода, ни «вы уже зарегистрированы»."""
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', chat_menu)
    access, campaign = _start_mocks(monkeypatch)
    # Дальше /start идёт обычным путём к меню; там и остановимся.
    monkeypatch.setattr(start, 'find_phantom_user_by_username', AsyncMock(side_effect=_StopStart))
    state = SimpleNamespace(get_data=AsyncMock(return_value={}), update_data=AsyncMock(), set_data=AsyncMock())
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        with pytest.raises(_StopStart):
            await start.cmd_start(_start_message(f'/start offer_{CODE}'), state, AsyncMock(), db_user=_user())

        registers = server.calls('POST', '/invite/offers/register')
        assert [r['body'] for r in registers] == [{'code': CODE}]
        assert registers[0]['token']['telegram_id'] == 7
    assert access.await_args.kwargs['start_parameter'] is None
    campaign.assert_not_awaited()
    state.update_data.assert_not_awaited()  # ни referral_code, ни registration_invite_payload
    assert env.data == {}  # код использован


async def test_start_offer_for_new_user_registers_after_registration(env, monkeypatch):
    access, campaign = _start_mocks(monkeypatch)
    monkeypatch.setattr(start, 'get_user_by_telegram_id', AsyncMock(side_effect=[None, _StopStart]))
    save_referral = AsyncMock()
    monkeypatch.setattr(start, 'save_pending_referral', save_referral)
    state = SimpleNamespace(get_data=AsyncMock(return_value={}), update_data=AsyncMock(), set_data=AsyncMock())
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        with pytest.raises(_StopStart):
            await start.cmd_start(_start_message(f'/start offer_{CODE}'), state, AsyncMock())

        # До конца регистрации сервер не трогаем; код ждёт в Redis сутки (FSM чистится на путях регистрации).
        assert server.requests == []
        assert env.data == {'bb_offer_code:7': CODE}
        assert env.ttl['bb_offer_code:7'] == 24 * 3600
        # Bedolaga код не видит: в закрытой регистрации он не пропуск.
        assert access.await_args.kwargs['start_parameter'] is None
        campaign.assert_not_awaited()
        save_referral.assert_not_awaited()
        state.update_data.assert_not_awaited()

        # Регистрация закончилась (хук в app/handlers/start.py): предложение записано, кнопка в меню.
        user = _user(subscription=None)
        await offers.after_start(user)
        assert [r['body'] for r in server.calls('POST', '/invite/offers/register')] == [{'code': CODE}]
        assert env.data == {}
        _, kb = await main_menu.build_bb_main_menu(user, AsyncMock())
        assert _labels(kb)[0] == ['🎁 Вам доступен уникальный тариф']

        # Второй раз записывать нечего.
        await offers.after_start(user)
        assert len(server.calls('POST', '/invite/offers/register')) == 1


async def test_code_waits_again_when_guest_key_does_not_answer(env, monkeypatch):
    """guest-key лежал в момент регистрации: код снова ждёт (следующий /start), ссылку искать не нужно."""
    user = _user()
    probe = await asyncio.start_server(lambda r, w: None, '127.0.0.1', 0)
    closed_port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    monkeypatch.setattr(settings, 'BB_OFFERS_URL', f'http://127.0.0.1:{closed_port}/invite/offers')
    env.data['bb_offer_code:7'] = CODE
    await offers.after_start(user)
    assert env.data == {'bb_offer_code:7': CODE}
    assert env.ttl['bb_offer_code:7'] == 24 * 3600

    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        await offers.after_start(user)
        assert [r['body'] for r in server.calls('POST', '/invite/offers/register')] == [{'code': CODE}]
        assert env.data == {}

        # Ссылка не живая (404) или приглашение у другого (409): код больше не нужен.
        server.register_status = 409
        env.data['bb_offer_code:7'] = CODE
        await offers.after_start(user)
        assert env.data == {}


def _bot():
    return SimpleNamespace(send_message=AsyncMock())


@pytest.mark.parametrize('chat_menu', ['all', 'off'])
@pytest.mark.parametrize(
    ('status', 'language', 'expected'),
    [
        (409, 'ru', 'Эта ссылка уже использована другим аккаунтом.'),
        (404, 'ru', 'Ссылка больше не действует.'),
        (409, 'en', 'This link has already been used by another account.'),
        (404, 'en', 'This link is no longer valid.'),
    ],
    ids=['409-ru', '404-ru', '409-en', '404-en'],
)
async def test_taken_or_dead_link_is_told_once(env, monkeypatch, chat_menu, status, language, expected):
    """Одноразовое приглашение уже у другого аккаунта (409) или ссылка умерла до Start (404): одно короткое
    сообщение без ссылки на кабинет, и при выключенном меню переписки тоже."""
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', chat_menu)
    user, bot = _user(language=language), _bot()
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.register_status = status
        env.data['bb_offer_code:7'] = CODE
        outcome = await offers.after_start(user, bot)
        assert outcome == ('taken' if status == 409 else 'dead')
        bot.send_message.assert_awaited_once_with(7, expected)
        assert env.data == {}
        # Код использован: следующий /start ничего не пишет.
        await offers.after_start(user, bot)
        bot.send_message.assert_awaited_once()
        assert len(server.calls('POST', '/invite/offers/register')) == 1


async def test_recorded_offer_sends_nothing(env, monkeypatch):
    """204: предложение записано; сам after_start ничего не пишет: «Подключить» шлёт send_offer, после меню."""
    user, bot = _user(), _bot()
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        env.data['bb_offer_code:7'] = CODE
        assert await offers.after_start(user, bot) == 'ok'
    bot.send_message.assert_not_awaited()


async def test_unavailable_sends_nothing_and_keeps_code(env, monkeypatch):
    """guest-key не ответил по договору (5xx) или не ответил вовсе: молча, код снова ждёт в Redis."""
    user, bot = _user(), _bot()
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.register_status = 500
        env.data['bb_offer_code:7'] = CODE
        assert await offers.after_start(user, bot) == 'unavailable'
        assert env.data == {'bb_offer_code:7': CODE}
    probe = await asyncio.start_server(lambda r, w: None, '127.0.0.1', 0)
    closed_port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    monkeypatch.setattr(settings, 'BB_OFFERS_URL', f'http://127.0.0.1:{closed_port}/invite/offers')
    assert await offers.after_start(user, bot) == 'unavailable'
    assert env.data == {'bb_offer_code:7': CODE}
    assert env.ttl['bb_offer_code:7'] == 24 * 3600
    bot.send_message.assert_not_awaited()


async def test_notice_failure_does_not_break_start(env, monkeypatch):
    """Бот заблокирован или Telegram не принял сообщение: /start идёт дальше, код не возвращается."""
    user, bot = _user(), _bot()
    bot.send_message.side_effect = RuntimeError('Forbidden: bot was blocked by the user')
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.register_status = 409
        env.data['bb_offer_code:7'] = CODE
        assert await offers.after_start(user, bot) == 'taken'
    bot.send_message.assert_awaited_once()
    assert env.data == {}


def test_claim_waits_longer_than_menu():
    """Нажатие: guest-key до ответа спрашивает кабинет и шлёт сообщения; меню ждёт не дольше 2 с."""
    assert offers.TIMEOUT == 2
    assert offers.TIMEOUT < offers.CLAIM_TIMEOUT <= 10


def _source(function) -> str:
    return inspect.getsource(function)


def test_after_start_hooks_in_registration_paths():
    """Запись предложения стоит там, где регистрация закончена, и там, где человек уже зарегистрирован."""
    for function in (start.complete_registration_from_callback, start.complete_registration):
        source = _source(function)
        # Уже активный (регистрация закончилась в другом месте) и только что зарегистрированный.
        assert source.count('await bb_offers.after_start(') == 2, function.__name__
        # Каждый вызов получает бота этого обновления: «ссылка уже использована» уходит человеку в чат.
        bot = 'callback.bot' if 'callback' in function.__name__ else 'message.bot'
        new_user = source.rindex(f'await bb_offers.after_start(user, {bot})')
        # После создания пользователя и state.clear(), до приветствия или меню (оба пути): сообщение о ссылке
        # приходит перед ними, меню с кнопками остаётся последним.
        assert source.rindex('await state.clear()') < new_user < source.index('get_welcome_text_for_user')
        assert source.index(f'await bb_offers.after_start(existing_user, {bot})') < source.index(
            'await maybe_send_bb_keyboard('
        )

    cmd_start = _source(start.cmd_start)
    assert cmd_start.count('bb_offers.stash_start_payload(') == 1
    assert cmd_start.index('bb_offers.stash_start_payload(') < cmd_start.index('_evaluate_telegram_registration_access')
    assert cmd_start.count('await bb_offers.after_start(') == 1
    assert cmd_start.count('await bb_offers.after_start(user, message.bot)') == 1

    channel_check = _source(start.required_sub_channel_check)
    assert channel_check.count('await bb_offers.after_start(') == 1
    after = channel_check.index('await bb_offers.after_start(user, query.bot)')
    assert after < channel_check.index('try_send_rich_main_menu(')


async def test_channel_check_does_not_keep_offer_as_bedolaga_payload(env, monkeypatch):
    """Подписка на канал обязательна: middleware не сохраняет offer_<код> как pending_start_payload Bedolaga
    (иначе после проверки он стал бы реферальным кодом), код ждёт в Redis до регистрации."""
    monkeypatch.setattr(settings, 'BB_OFFERS_URL', 'http://guest-key:8091/invite/offers')
    saved = AsyncMock()
    monkeypatch.setattr(channel_checker, 'save_pending_payload_to_redis', saved)
    state = SimpleNamespace(get_data=AsyncMock(return_value={}), set_data=AsyncMock())
    event = MagicMock(spec=channel_checker.Message)
    event.text = f'/start offer_{CODE}'
    event.from_user = SimpleNamespace(id=7)
    middleware = channel_checker.ChannelCheckerMiddleware()
    await middleware._capture_start_payload(state, event, None)
    state.set_data.assert_not_awaited()
    saved.assert_not_awaited()
    assert env.data == {'bb_offer_code:7': CODE}

    # Обычный payload Bedolaga сохраняется как раньше.
    event.text = '/start ref_partner'
    await middleware._capture_start_payload(state, event, None)
    saved.assert_awaited_once_with(7, 'ref_partner')


def test_offer_code_format():
    assert offers.offer_code(f'offer_{CODE}') == CODE
    assert offers.offer_code('offer_' + 'a' * 40) == 'a' * 40
    for payload in (None, '', 'ref_partner', 'offer_short', 'offer_' + 'a' * 41, f'offer_{CODE}!', f'xoffer_{CODE}'):
        assert offers.offer_code(payload) is None, payload


# --- Сообщение «Подключить» после /start offer_<код> -----------------------------------------------------------

MINE_ID = f'group:mcksp:{CODE}'  # предложение именно этой ссылки: id «<kind>:<code>»
MINE = dict(OPEN, id=MINE_ID)
OTHER = dict(OPEN, id='invite:Zz9_other-code', title='Unlimited Pro', text='Полный безлимит')


@pytest.mark.parametrize('chat_menu', ['all', 'admins', 'off'])
@pytest.mark.parametrize(
    ('language', 'head', 'button'),
    [
        ('ru', '🎁 Вам доступен уникальный тариф', 'Подключить'),
        ('en', '🎁 A special plan is available to you', 'Activate'),
    ],
    ids=['ru', 'en'],
)
async def test_recorded_offer_message_with_connect_button(env, monkeypatch, chat_menu, language, head, button):
    """Предложение записано (204) и открыто: после меню одно сообщение с названием и текстом группы (как в меню
    переписки, экранированы) и кнопкой «Подключить», при любом BB_CHAT_MENU. Другие предложения человека не в счёт."""
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', chat_menu)
    user, bot = _user(language=language), _bot()
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.on_register = [OTHER, MINE]
        env.data['bb_offer_code:7'] = CODE
        assert await offers.after_start(user, bot) == 'ok'
        bot.send_message.assert_not_awaited()  # до меню ничего
        await offers.send_offer(user, bot)
        bot.send_message.assert_awaited_once()
        args, kwargs = bot.send_message.await_args
        assert args == (7, f'{head}\n\n<b>MCKSP &lt;группа&gt; &amp; друзья</b>\nЕщё один сервер')
        assert kwargs['parse_mode'] == 'HTML'
        assert _labels(kwargs['reply_markup']) == [[button]]
        assert _callbacks(kwargs['reply_markup']) == [[offers.offer_callback(MINE_ID)]]

        # Одно сообщение на /start: второй вызов ничего не шлёт и guest-key не спрашивает.
        requests = len(server.requests)
        await offers.send_offer(user, bot)
        bot.send_message.assert_awaited_once()
        assert len(server.requests) == requests


async def test_pending_offer_message_without_button(env, monkeypatch):
    """Заявка по этой ссылке уже ждёт админа: «⏳ Заявка на рассмотрении» и название группы, без кнопки."""
    user, bot = _user(), _bot()
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.on_register = [dict(MINE, state='pending')]
        env.data['bb_offer_code:7'] = CODE
        assert await offers.after_start(user, bot) == 'ok'
        await offers.send_offer(user, bot)
    bot.send_message.assert_awaited_once()
    args, kwargs = bot.send_message.await_args
    assert args == (7, '⏳ Заявка на рассмотрении\n\n<b>MCKSP &lt;группа&gt; &amp; друзья</b>')
    assert kwargs.get('reply_markup') is None


@pytest.mark.parametrize('listed', [[], [OTHER], [OPEN]], ids=['empty', 'other-offer', 'same-group-other-code'])
async def test_no_message_when_offer_of_this_code_is_missing(env, monkeypatch, listed):
    """204, но предложения этой ссылки в списке нет (решение уже было, ссылка умерла, у группы раньше записан
    другой код): ничего."""
    user, bot = _user(), _bot()
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.on_register = listed
        env.data['bb_offer_code:7'] = CODE
        assert await offers.after_start(user, bot) == 'ok'
        await offers.send_offer(user, bot)
        assert len(server.calls('GET')) == 1
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize('status', [409, 404, 500])
async def test_no_offer_message_unless_recorded(env, monkeypatch, status):
    """409/404: только прежнее короткое сообщение (до меню); 5xx: молча, код ждёт. Списка не спрашиваем."""
    user, bot = _user(), _bot()
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.offers = [dict(MINE)]
        server.register_status = status
        env.data['bb_offer_code:7'] = CODE
        await offers.after_start(user, bot)
        sent = bot.send_message.await_count
        await offers.send_offer(user, bot)
        assert bot.send_message.await_count == sent
        assert server.calls('GET') == []


async def test_plain_start_sends_no_offer_message(env, monkeypatch):
    """Обычный /start (кода нет): ничего, даже при открытом предложении. Путь упал до меню и send_offer не дошёл:
    на следующем /start старое сообщение не всплывает."""
    user, bot = _user(), _bot()
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.on_register = [MINE]
        env.data['bb_offer_code:7'] = CODE
        assert await offers.after_start(user, bot) == 'ok'
        assert await offers.after_start(user, bot) is None  # следующий /start, без кода
        await offers.send_offer(user, bot)
        assert server.calls('GET') == []
    bot.send_message.assert_not_awaited()


async def test_offer_message_failures_are_silent(env, monkeypatch):
    """guest-key не отдал список (5xx, порт закрыт), другая ошибка или Telegram не принял сообщение: /start и
    регистрация идут дальше, человеку ничего."""
    user, bot = _user(), _bot()
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.on_register = [MINE]

        server.list_status = 500
        env.data['bb_offer_code:7'] = CODE
        assert await offers.after_start(user, bot) == 'ok'
        await offers.send_offer(user, bot)
        bot.send_message.assert_not_awaited()

        server.list_status = 200
        bot.send_message.side_effect = RuntimeError('Forbidden: bot was blocked by the user')
        env.data['bb_offer_code:7'] = CODE
        assert await offers.after_start(user, bot) == 'ok'
        await offers.send_offer(user, bot)
        bot.send_message.assert_awaited_once()
        bot.send_message.side_effect = None
        bot.send_message.reset_mock()

        with monkeypatch.context() as patch:
            patch.setattr(offers, 'fetch_offers', AsyncMock(side_effect=RuntimeError('boom')))
            env.data['bb_offer_code:7'] = CODE
            assert await offers.after_start(user, bot) == 'ok'
            await offers.send_offer(user, bot)

        env.data['bb_offer_code:7'] = CODE
        assert await offers.after_start(user, bot) == 'ok'
    await offers.send_offer(user, bot)  # guest-key уже не слушает порт
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    ('status', 'expected'),
    [
        (204, ['keyboard', 'menu', 'pinned', '🎁 Вам доступен уникальный тариф']),
        (409, ['Эта ссылка уже использована другим аккаунтом.', 'keyboard', 'menu', 'pinned']),
    ],
    ids=['recorded', 'taken'],
)
async def test_start_offer_message_comes_after_menu(env, monkeypatch, status, expected):
    """Зарегистрированный, /start offer_<код>: «Подключить» последним, после клавиатуры, меню и закреплённого
    сообщения (оно после меню); «ссылка уже использована» как раньше, до меню."""
    _start_mocks(monkeypatch)
    order = []

    def step(name):
        async def record(*args, **kwargs):
            order.append(name)
            return True

        return record

    monkeypatch.setattr(start, 'find_phantom_user_by_username', AsyncMock(return_value=None))
    for name in (
        '_activate_pending_gift_after_registration',
        '_redeem_pending_coupon',
        '_activate_pending_trial',
        '_persist_pending_subid_after_registration',
    ):
        monkeypatch.setattr(start, name, AsyncMock())
    pinned = SimpleNamespace(send_before_menu=False)
    monkeypatch.setattr(start, 'get_active_pinned_message', AsyncMock(return_value=pinned))
    monkeypatch.setattr(start, 'SupportSettingsService', SimpleNamespace(is_moderator=lambda telegram_id: False))
    buttons = SimpleNamespace(get_buttons_for_user=AsyncMock(return_value=[]))
    monkeypatch.setattr(start, 'MainMenuButtonService', buttons)
    monkeypatch.setattr(start, 'get_main_menu_keyboard_async', AsyncMock())
    monkeypatch.setattr(start, 'maybe_send_bb_keyboard', step('keyboard'))
    monkeypatch.setattr(start, 'try_answer_rich_main_menu', step('menu'))
    monkeypatch.setattr(start, '_send_pinned_message', step('pinned'))
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=lambda chat_id, text, **kw: order.append(text.split('\n')[0]))
    )
    person = SimpleNamespace(id=7, username='person', first_name='P', last_name=None)
    message = SimpleNamespace(
        text=f'/start offer_{CODE}', from_user=person, bot=bot, chat=SimpleNamespace(id=7), answer=AsyncMock()
    )
    user = SimpleNamespace(
        **vars(_user()),
        username='person',
        first_name='P',
        last_name=None,
        referred_by_id=None,
        subscriptions=[],
        has_had_paid_subscription=False,
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={}), update_data=AsyncMock(), set_data=AsyncMock(), clear=AsyncMock()
    )
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.on_register = [MINE]
        server.register_status = status
        await start.cmd_start(message, state, AsyncMock(), db_user=user)
    assert order == expected
    state.clear.assert_awaited_once()


def test_offer_message_hooks_after_welcome_or_menu():
    """«Подключить» последним: на всех 6 путях с after_start send_offer стоит после приветствия или меню (и после
    закреплённого сообщения, если оно идёт после меню), по одному разу на путь."""
    for function in (start.complete_registration_from_callback, start.complete_registration):
        source = _source(function)
        bot = 'callback.bot' if 'callback' in function.__name__ else 'message.bot'
        assert source.count('await bb_offers.send_offer(') == 2, function.__name__
        # Уже активный: после меню, закреплённого и запасного «Добро пожаловать», до выхода из ветки.
        branch_end = source.index('await state.clear()\n        return')
        existing = source.index(f'await bb_offers.send_offer(existing_user, {bot})')
        assert source.index(f'await bb_offers.after_start(existing_user, {bot})') < existing < branch_end
        for step in ('answer_menu_with_media(', "'WELCOME_FALLBACK'"):
            assert source.index(step) < existing, (function.__name__, step)
        assert source.rindex('_send_pinned_message(', 0, branch_end) < existing
        # Только что зарегистрированный: в самом конце, после приветствия или меню (оба пути) и закреплённого.
        new_user = source.index(f'await bb_offers.send_offer(user, {bot})')
        for step in (
            'get_post_registration_keyboard(',
            'answer_menu_with_media(',
            '_send_pinned_message(',
            "'WELCOME_FALLBACK'",
        ):
            assert source.rindex(step) < new_user, (function.__name__, step)

    cmd_start = _source(start.cmd_start)
    assert cmd_start.count('await bb_offers.send_offer(') == 1
    after = cmd_start.index('await bb_offers.after_start(user, message.bot)')
    send = cmd_start.index('await bb_offers.send_offer(user, message.bot)')
    branch_end = cmd_start.index('await state.clear()\n        return', after)
    menu = cmd_start.index('answer_menu_with_media(', after)
    assert menu < cmd_start.rindex('_send_pinned_message(', after, branch_end) < send < branch_end

    channel_check = _source(start.required_sub_channel_check)
    assert channel_check.count('await bb_offers.send_offer(') == 1
    send = channel_check.index('await bb_offers.send_offer(user, query.bot)')
    branch_end = channel_check.index('get_rules_keyboard')
    assert channel_check.index('send_menu_with_media(') < send < branch_end
    assert channel_check.rindex('_send_pinned_message(', 0, branch_end) < send


# --- Нажатие «Подключить» в сообщении ---------------------------------------------------------------------------


def _message_callback(markup):
    """Нажатие «Подключить» в сообщении бота после /start offer_<код>: у сообщения одна кнопка."""
    callback = _menu_callback(markup.inline_keyboard[0][0].callback_data)
    callback.message.reply_markup = markup
    callback.message.text = 'сообщение с кнопкой'
    callback.bot.edit_message_reply_markup = AsyncMock()
    return callback


@pytest.mark.parametrize('chat_menu', ['off', 'admins', 'all'])
async def test_connect_from_message_claims_and_edits_that_message(env, monkeypatch, chat_menu):
    """«Подключить» в сообщении: то же нажатие, что в меню переписки (заявка; сообщения админам шлёт guest-key),
    при любом BB_CHAT_MENU; потом правится само это сообщение, без кнопки; меню переписки не рисуется. Второе
    нажатие: «Заявка на рассмотрении», без второго запроса."""
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', chat_menu)
    edit_menu = AsyncMock()
    monkeypatch.setattr(main_menu, 'bb_edit_main_menu', edit_menu)
    user = _user(subscription=None)
    _, markup = offers.offer_message(MINE, 'ru')
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.offers = [dict(MINE)]
        for press in (1, 2):
            callback = _message_callback(markup)
            await main_menu.on_offer(callback, user, MagicMock())
            assert [c['body'] for c in server.calls('POST', '/invite/offers/claim')] == [{'id': MINE_ID}], press
            callback.answer.assert_awaited_once_with('⏳ Заявка на рассмотрении')
            kwargs = callback.bot.edit_message_text.await_args.kwargs
            assert (kwargs['chat_id'], kwargs['message_id']) == (7, 99)
            assert kwargs['text'] == '⏳ Заявка на рассмотрении\n\n<b>MCKSP &lt;группа&gt; &amp; друзья</b>'
            assert kwargs.get('reply_markup') is None
            assert kwargs['parse_mode'] == 'HTML'
            callback.bot.send_message.assert_not_awaited()
    edit_menu.assert_not_awaited()


async def test_connect_from_message_gone_member_retry(env, monkeypatch):
    """Предложения уже нет (пропало из списка, 409, 404): «Предложение больше не доступно» вместо текста; сразу
    в группе (member): кнопка убрана, приветствие шлёт guest-key; сбой связи: «попробуйте ещё раз», сообщение
    с кнопкой не трогается; Telegram не дал поправить сообщение: без исключений."""
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'off')
    user = _user()
    _, markup = offers.offer_message(MINE, 'ru')
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        cases = [([], (200, {'state': 'pending'})), ([MINE], (409, {'error': 'decided'})), ([MINE], (404, {}))]
        for listed, reply in cases:
            server.offers, server.claim_reply = [dict(o) for o in listed], reply
            callback = _message_callback(markup)
            await main_menu.on_offer(callback, user, MagicMock())
            callback.answer.assert_awaited_once_with('Предложение больше не доступно')
            kwargs = callback.bot.edit_message_text.await_args.kwargs
            assert kwargs['text'] == 'Предложение больше не доступно'
            assert kwargs.get('reply_markup') is None

        server.offers, server.claim_reply = [dict(MINE)], (200, {'state': 'member'})
        callback = _message_callback(markup)
        await main_menu.on_offer(callback, user, MagicMock())
        callback.answer.assert_awaited_once_with()
        callback.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=7, message_id=99, reply_markup=None)
        callback.bot.edit_message_text.assert_not_awaited()

        server.offers, server.claim_reply = [dict(MINE)], (500, {'error': 'internal'})
        callback = _message_callback(markup)
        await main_menu.on_offer(callback, user, MagicMock())
        callback.answer.assert_awaited_once_with('Не получилось, попробуйте ещё раз')
        callback.bot.edit_message_text.assert_not_awaited()
        callback.bot.edit_message_reply_markup.assert_not_awaited()

        server.offers, server.claim_reply = [dict(MINE)], (200, {'state': 'pending'})
        callback = _message_callback(markup)
        callback.bot.edit_message_text.side_effect = RuntimeError('Bad Request: message is not modified')
        await main_menu.on_offer(callback, user, MagicMock())
        callback.answer.assert_awaited_once_with('⏳ Заявка на рассмотрении')


async def test_chat_menu_button_keeps_menu_behaviour(env, monkeypatch):
    """Кнопка в меню переписки (там строк больше одной) как раньше: меню включено - нажатие и меню на месте того же
    сообщения; меню выключено (старое меню в чате) - только ответ, без запросов."""
    monkeypatch.setattr(main_menu, 'maybe_send_bb_keyboard_once', AsyncMock())
    user = _user(subscription=None)
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.offers = [dict(MINE)]
        _, menu = await main_menu.build_bb_main_menu(user, MagicMock())
        assert len(menu.inline_keyboard) > 1
        callback = _menu_callback(_callbacks(menu)[0][0])
        callback.message.reply_markup = menu
        await main_menu.on_offer(callback, user, MagicMock())
        assert len(server.calls('POST', '/invite/offers/claim')) == 1
        kwargs = callback.bot.edit_message_text.await_args.kwargs
        assert _callbacks(kwargs['reply_markup'])[0] == ['bb_offer_wait']
        assert len(kwargs['reply_markup'].inline_keyboard) == len(menu.inline_keyboard)

        monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'off')
        server.offers = [dict(MINE)]
        callback = _menu_callback(_callbacks(menu)[0][0])
        callback.message.reply_markup = menu
        await main_menu.on_offer(callback, user, MagicMock())
        assert len(server.calls('POST', '/invite/offers/claim')) == 1
        callback.answer.assert_awaited_once_with()
        callback.bot.edit_message_text.assert_not_awaited()


# --- Выключено ------------------------------------------------------------------------------------------------


async def test_chat_menu_off_no_row(env, monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'off')
    async with GuestKey() as server:
        monkeypatch.setattr(settings, 'BB_OFFERS_URL', server.url)
        server.offers = [dict(OPEN)]
        _, kb = await main_menu.build_bb_main_menu(_user(subscription=_sub()), AsyncMock())
        assert server.requests == []
    assert all(not cb.startswith('bb_offer') for row in _callbacks(kb) for cb in row)

    # Старая кнопка в чате после выключения: только ответ, без запросов и без меню.
    callback = _menu_callback(offers.offer_callback(OFFER_ID))
    await main_menu.on_offer(callback, _user(), MagicMock())
    callback.answer.assert_awaited_once_with()
    callback.bot.edit_message_text.assert_not_awaited()


async def test_empty_offers_url_no_calls(env, monkeypatch):
    def no_http(*args, **kwargs):
        raise AssertionError('HTTP-запрос при пустом BB_OFFERS_URL')

    monkeypatch.setattr(offers.aiohttp, 'ClientSession', no_http)
    user = _user(subscription=_sub())
    assert await offers.fetch_offers(user) == []
    assert await offers.claim_offer(user, OFFER_ID) is None
    await offers.register_offer(user, CODE)
    # Код из ссылки Bedolaga всё равно не видит, но и не хранится: механизма нет.
    assert await offers.stash_start_payload(7, f'offer_{CODE}') is True
    assert env.data == {}
    await offers.after_start(user)
    _, kb = await main_menu.build_bb_main_menu(user, AsyncMock())
    assert all(not cb.startswith('bb_offer') for row in _callbacks(kb) for cb in row)


def test_handlers_registered():
    from aiogram import Dispatcher

    from app.bb import entry

    dp = Dispatcher()
    entry.register_handlers(dp)
    callbacks = {handler.callback for handler in dp.callback_query.handlers}
    assert main_menu.on_offer in callbacks
    assert main_menu.on_offer_wait in callbacks
