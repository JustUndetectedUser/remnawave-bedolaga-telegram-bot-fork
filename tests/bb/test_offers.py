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
        return web.json_response({'offers': self.offers})

    async def register(self, request):
        body = await request.json()
        self._record(request, body)
        if self.register_status == 204:
            self.offers = [dict(OPEN)]
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
        for key in ('btn_offer', 'offer_pending', 'offer_gone', 'offer_retry'):
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
        new_user = source.rindex('await bb_offers.after_start(user)')
        # После создания пользователя и state.clear(), до приветствия или меню (оба пути).
        assert source.rindex('await state.clear()') < new_user < source.index('get_welcome_text_for_user')
        assert source.index('await bb_offers.after_start(existing_user)') < source.index(
            'await maybe_send_bb_keyboard('
        )

    cmd_start = _source(start.cmd_start)
    assert cmd_start.count('bb_offers.stash_start_payload(') == 1
    assert cmd_start.index('bb_offers.stash_start_payload(') < cmd_start.index('_evaluate_telegram_registration_access')
    assert cmd_start.count('await bb_offers.after_start(user)') == 1

    channel_check = _source(start.required_sub_channel_check)
    assert channel_check.count('await bb_offers.after_start(user)') == 1
    assert channel_check.index('await bb_offers.after_start(user)') < channel_check.index('try_send_rich_main_menu(')


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
