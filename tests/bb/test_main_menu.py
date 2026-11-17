from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bb import main_menu, tariffs
from app.bb.main_menu import bb_main_menu, format_day
from app.bb.texts import TEXTS
from app.config import settings
from app.database.models import User


THIS_YEAR = datetime.now(UTC).year


def _user(*, language='ru', balance=12000, trial_used=False, subscription=None):
    return SimpleNamespace(
        id=42,
        telegram_id=7,
        language=language,
        balance_kopeks=balance,
        auth_type='telegram',
        is_trial_already_used=lambda: trial_used,
        subscription=subscription,
    )


def _tariff(name='PRO', tag='PAID_PRO', tariff_id=3, **extra):
    return SimpleNamespace(id=tariff_id, name=name, panel_tag=tag, **extra)


def _sub(status='active', *, is_trial=False, tariff=None, end=None):
    tariff = tariff or _tariff()
    return SimpleNamespace(
        actual_status=status,
        is_trial=is_trial,
        tariff=tariff,
        tariff_id=tariff.id,
        end_date=end or datetime(THIS_YEAR, 12, 16, 12, tzinfo=UTC),
    )


def _callbacks(kb):
    return [[b.callback_data for b in row] for row in kb.inline_keyboard]


def _labels(kb):
    return [[b.text for b in row] for row in kb.inline_keyboard]


@pytest.fixture
def trial_on(monkeypatch):
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 30)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')


def test_menu_active(monkeypatch):
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC')
    text, kb = bb_main_menu(_user(), _sub(), 'ru')
    assert text == 'Подписка Pro до 16 декабря · Баланс 120 ₽'
    assert _callbacks(kb) == [['subscription_connect'], ['bb_renew', 'balance_topup'], ['bb_more']]
    assert _labels(kb) == [
        ['🔗 Подключить устройство'],
        ['💳 Продлить или сменить тариф', '💰 Пополнить баланс'],
        ['Ещё'],
    ]
    text_en, _ = bb_main_menu(_user(language='en'), _sub(), 'en')
    assert text_en == 'Pro subscription until 16 December · Balance 120 ₽'

    # Пробный и «трафик исчерпан» показываются как действующая подписка.
    for status in ('trial', 'limited'):
        _, kb = bb_main_menu(_user(), _sub(status, is_trial=status == 'trial'), 'ru')
        assert _callbacks(kb) == [['subscription_connect'], ['bb_renew', 'balance_topup'], ['bb_more']]

    # Названия в базе заглавными (LITE, MAX): показываем «Lite», «Max».
    text, _ = bb_main_menu(_user(), _sub(tariff=_tariff('LITE', None, 1)), 'ru')
    assert text == 'Подписка Lite до 16 декабря · Баланс 120 ₽'
    text, _ = bb_main_menu(_user(), _sub(tariff=_tariff('MAX', None, 4)), 'ru')
    assert text.startswith('Подписка Max до ')


def test_menu_active_trial_status(monkeypatch):
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC')
    sub = _sub('active', is_trial=True, tariff=_tariff('TRIAL PRO', None, 5))
    text, kb = bb_main_menu(_user(), sub, 'ru')
    assert text == 'Пробный Pro до 16 декабря · Баланс 120 ₽'
    assert _callbacks(kb) == [['subscription_connect'], ['bb_renew', 'balance_topup'], ['bb_more']]
    text_en, _ = bb_main_menu(_user(language='en'), sub, 'en')
    assert text_en == 'Pro trial until 16 December · Balance 120 ₽'


def test_format_day_year_only_when_not_current():
    today = date(2026, 11, 16)
    assert format_day(datetime(2026, 12, 16, 12, tzinfo=UTC), 'ru', today=today) == '16 декабря'
    assert format_day(datetime(2027, 1, 16, 12, tzinfo=UTC), 'ru', today=today) == '16 января 2027'
    assert format_day(datetime(2026, 12, 16, 12, tzinfo=UTC), 'en', today=today) == '16 December'
    assert format_day(datetime(2027, 1, 16, 12, tzinfo=UTC), 'en', today=today) == '16 January 2027'


def test_menu_unlimited_pro_has_no_renew():
    sub = _sub(tariff=_tariff('Unlimited Pro', 'ENDLESS_PRO', 9), end=datetime(THIS_YEAR + 10, 1, 1, tzinfo=UTC))
    text, kb = bb_main_menu(_user(), sub, 'ru')
    assert text == 'Unlimited Pro · бессрочно'
    assert _callbacks(kb) == [['subscription_connect'], ['bb_more']]
    flat = {cb for row in _callbacks(kb) for cb in row}
    assert 'bb_renew' not in flat and 'balance_topup' not in flat

    # Даже закончившийся бесплатный тариф не предлагают продлить.
    _, kb = bb_main_menu(_user(), _sub('expired', tariff=_tariff('Unlimited Pro', 'ENDLESS_PRO', 9)), 'ru')
    flat = {cb for row in _callbacks(kb) for cb in row}
    assert 'bb_renew' not in flat and 'balance_topup' not in flat


def test_menu_expired_renew_first():
    sub = _sub('expired', end=datetime.now(UTC) - timedelta(days=2))
    text, kb = bb_main_menu(_user(), sub, 'ru')
    assert text.startswith('Подписка Pro закончилась ')
    assert text.endswith(' · Баланс 120 ₽')
    assert _callbacks(kb) == [['bb_renew'], ['subscription_connect'], ['balance_topup'], ['bb_more']]
    assert kb.inline_keyboard[0][0].text == '💳 Продлить или сменить тариф'


def test_menu_disabled_offers_nothing_impossible():
    text, kb = bb_main_menu(_user(), _sub('disabled'), 'ru')
    assert text == 'Подписка Pro не активна · Баланс 120 ₽'
    assert _callbacks(kb) == [['bb_more']]


def test_first_visit_trial_offer(trial_on):
    text, kb = bb_main_menu(_user(), None, 'ru', trial_days=30, trial_devices=8)
    assert text.startswith(
        'Добро пожаловать в BlackBird. Попробуйте Pro бесплатно на 30 дней: до 8 устройств, работает даже '
        'при белых списках мобильного интернета, без карты и автосписаний.'
    )
    assert 'Продолжая, вы принимаете' in text
    assert 'href="https://blackbirdnet.com/offer"' in text
    assert 'href="https://blackbirdnet.com/privacy"' in text
    assert _callbacks(kb) == [['trial_activate'], ['bb_tariffs']]
    assert _labels(kb) == [['🎁 Попробовать Pro бесплатно'], ['Платные тарифы']]

    text_en, kb_en = bb_main_menu(_user(language='en'), None, 'en', trial_days=3, trial_devices=1)
    assert 'for 3 days: up to 1 device,' in text_en
    assert _callbacks(kb_en) == [['trial_activate'], ['bb_tariffs']]


def test_first_visit_trial_used_only_tariffs(monkeypatch, trial_on):
    text, kb = bb_main_menu(_user(trial_used=True), None, 'ru')
    assert _callbacks(kb) == [['bb_tariffs']]
    assert _labels(kb) == [['Платные тарифы']]
    assert 'бесплатно' not in text
    assert 'https://blackbirdnet.com/offer' in text

    # Пробный выключен в настройках: тоже только «Платные тарифы».
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'all')
    _, kb = bb_main_menu(_user(), None, 'ru')
    assert _callbacks(kb) == [['bb_tariffs']]

    # Пробный закончился или его отключил сторож: сразу «Платные тарифы», под ними «Ещё» (поддержка).
    for status in ('expired', 'disabled'):
        text, kb = bb_main_menu(_user(trial_used=True), _sub(status, is_trial=True), 'ru')
        assert _callbacks(kb) == [['bb_tariffs'], ['bb_more']]
        assert _labels(kb) == [['Платные тарифы'], ['Ещё']]
        assert 'Пробный период закончился' in text


@pytest.mark.asyncio
async def test_trial_numbers_from_trial_tariff(monkeypatch):
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: True)
    trial = SimpleNamespace(device_limit=8, trial_duration_days=30)
    monkeypatch.setattr(main_menu, 'get_trial_tariff', AsyncMock(return_value=trial))
    assert await main_menu.trial_numbers(MagicMock()) == (30, 8)


def _tariffs_db_rows():
    return [
        _tariff(
            'MAX',
            None,
            4,
            display_order=4,
            description='Max text',
            is_highlighted=False,
            is_trial_available=False,
            period_prices={'30': 50000, '90': 135000},
            device_limit=12,
        ),
        _tariff(
            'PRO',
            'PAID_PRO',
            3,
            display_order=3,
            description='Pro text',
            is_highlighted=True,
            is_trial_available=False,
            period_prices={'30': 35000},
            device_limit=8,
        ),
        _tariff(
            'LITE',
            None,
            1,
            display_order=1,
            description='Lite text',
            is_highlighted=False,
            is_trial_available=False,
            period_prices={'30': 10000},
            device_limit=2,
        ),
        _tariff(
            'TRIAL PRO',
            None,
            5,
            display_order=0,
            description='',
            is_highlighted=False,
            is_trial_available=True,
            period_prices={},
            device_limit=8,
        ),
        _tariff(
            'Unlimited Pro',
            'ENDLESS_PRO',
            9,
            display_order=9,
            description='',
            is_highlighted=False,
            is_trial_available=False,
            period_prices={'3650': 0},
            device_limit=8,
        ),
        _tariff(
            'BASE',
            None,
            2,
            display_order=2,
            description='Base text',
            is_highlighted=False,
            is_trial_available=False,
            period_prices={'30': 20000},
            device_limit=4,
        ),
    ]


@pytest.mark.asyncio
async def test_tariffs_message_is_new_message(monkeypatch):
    monkeypatch.setattr(tariffs, 'get_tariffs_for_user', AsyncMock(return_value=_tariffs_db_rows()))
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message.chat.id = 7
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.bot.send_message = AsyncMock()
    user = SimpleNamespace(language='ru', promo_group_id=None)

    await tariffs.on_tariffs(callback, user, MagicMock())

    callback.message.edit_text.assert_not_awaited()
    callback.bot.send_message.assert_awaited_once()
    callback.answer.assert_awaited_once()
    args, kwargs = callback.bot.send_message.await_args
    assert args[0] == 7
    text = args[1]
    kb = kwargs['reply_markup']
    assert kwargs['parse_mode'] == 'HTML'
    assert _labels(kb) == [['Lite'], ['Base'], ['⭐ Pro'], ['Max']]
    # Один период: сразу подтверждение Bedolaga (tariff_period), без экрана с одной кнопкой. Два периода: выбор.
    assert _callbacks(kb) == [
        ['tariff_period:1:30'],
        ['tariff_period:2:30'],
        ['tariff_period:3:30'],
        ['tariff_select:4'],
    ]
    assert text.count('самый популярный') == 1
    assert text.index('Lite text') < text.index('Base text') < text.index('Pro text') < text.index('Max text')
    assert '350 ₽' in text and '8 устройств' in text
    assert 'TRIAL PRO' not in text and 'Unlimited' not in text
    assert '<b>Lite</b>' in text and '<b>Pro</b>' in text and 'LITE' not in text and 'PRO' not in text


def test_single_period_with_custom_options_keeps_selection():
    """Свои дни или трафик и суточный тариф: экран Bedolaga после tariff_select нужен, его не пропускаем."""
    rows = [
        _tariff('LITE', None, 1, period_prices={'30': 10000}, device_limit=2, can_purchase_custom_days=lambda: True),
        _tariff('BASE', None, 2, period_prices={'30': 20000}, device_limit=4, can_purchase_custom_traffic=lambda: True),
        _tariff('PRO', None, 3, period_prices={'30': 35000}, device_limit=8, is_daily=True),
    ]
    _, kb = tariffs.tariffs_message(rows, 'ru')
    assert _callbacks(kb) == [['tariff_select:1'], ['tariff_select:2'], ['tariff_select:3']]


@pytest.mark.asyncio
async def test_render_hooks_use_bb_menu(monkeypatch):
    from app.utils import rich_menu

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    monkeypatch.setattr(main_menu, 'get_tariff_by_id', AsyncMock(return_value=_tariff()))
    # Клавиатуру этому человеку уже присылали (первый показ проверяет tests/bb/test_entry.py).
    monkeypatch.setattr(main_menu, 'maybe_send_bb_keyboard_once', AsyncMock())
    user = _user(subscription=_sub())

    # /start, «Вызвать бота»: новое сообщение, без картинки и rich-меню.
    bot = MagicMock()
    bot.send_message = AsyncMock()
    assert await rich_menu.try_send_rich_main_menu(bot, 7, user, MagicMock(), MagicMock(), 'BEDOLAGA_KB') is True
    args, kwargs = bot.send_message.await_args
    assert args[0] == 7 and args[1].startswith('Подписка Pro до ')
    assert kwargs['parse_mode'] == 'HTML'
    assert _callbacks(kwargs['reply_markup'])[0] == ['subscription_connect']

    # Кнопка «Назад» в меню (callback): правка того же сообщения.
    callback = MagicMock()
    callback.bot.edit_message_text = AsyncMock()
    callback.bot.send_message = AsyncMock()
    callback.message.text = 'старое меню'
    callback.message.photo = None
    callback.message.chat.id = 7
    callback.message.message_id = 99
    assert await rich_menu.try_edit_rich_main_menu(callback, user, MagicMock(), MagicMock(), 'BEDOLAGA_KB') is True
    kwargs = callback.bot.edit_message_text.await_args.kwargs
    assert kwargs['chat_id'] == 7 and kwargs['message_id'] == 99
    assert kwargs['text'].startswith('Подписка Pro до ')
    callback.bot.send_message.assert_not_awaited()

    # Старое меню с логотипом: удалить и прислать текстом.
    callback.bot.edit_message_text.reset_mock()
    callback.message.text = None
    callback.message.photo = ['logo']
    callback.message.delete = AsyncMock()
    assert await rich_menu.try_edit_rich_main_menu(callback, user, MagicMock(), MagicMock(), 'BEDOLAGA_KB') is True
    callback.message.delete.assert_awaited_once()
    callback.bot.send_message.assert_awaited_once()
    callback.bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_render_hooks_untouched_when_off(monkeypatch):
    from app.utils import rich_menu

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'off')
    monkeypatch.setattr(settings, 'MAIN_MENU_RICH_ENABLED', False)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    assert await rich_menu.try_send_rich_main_menu(bot, 7, _user(), MagicMock(), MagicMock(), 'KB') is False
    callback = MagicMock()
    callback.bot.edit_message_text = AsyncMock()
    assert await rich_menu.try_edit_rich_main_menu(callback, _user(), MagicMock(), MagicMock(), 'KB') is False
    bot.send_message.assert_not_awaited()
    callback.bot.edit_message_text.assert_not_awaited()


async def _activate_trial(monkeypatch, mode, connect):
    from app.handlers.subscription import purchase

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', mode)
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 30)
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'guide')
    callback = AsyncMock(spec=CallbackQuery)
    callback.message = AsyncMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    user = MagicMock(spec=User)
    user.restriction_subscription = False
    user.auth_type = 'telegram'
    user.is_trial_already_used.return_value = False
    user.language = 'ru'
    user.id = 42
    user.telegram_id = 7
    db = AsyncMock(spec=AsyncSession)
    service = MagicMock()
    service.return_value.create_remnawave_user = AsyncMock(return_value=MagicMock())
    notifications = MagicMock()
    notifications.return_value.send_trial_activation_notification = AsyncMock()

    with (
        patch('app.services.trial_activation_service.get_trial_activation_charge_amount', return_value=0),
        patch('app.config.Settings.is_trial_disabled_for_user', return_value=False),
        patch('app.config.Settings.is_tariffs_mode', return_value=False),
        patch('app.config.Settings.is_devices_selection_enabled', return_value=True),
        patch('app.database.crud.server_squad.get_random_trial_squad_uuid', new=AsyncMock(return_value='sq')),
        patch('app.services.checkout_squad_validation.validate_checkout_squads', new=AsyncMock()),
        patch.object(purchase, 'create_trial_subscription', new=AsyncMock(return_value=MagicMock())),
        patch.object(purchase, 'charge_trial_activation_if_required', new=AsyncMock(return_value=0)),
        patch.object(purchase, 'SubscriptionService', new=service),
        patch.object(purchase, 'AdminNotificationService', new=notifications),
        patch.object(purchase, 'get_display_subscription_link', return_value='https://sub.example/abc'),
        patch.object(purchase, 'handle_connect_subscription', new=connect),
        patch('app.bb.connect.trial_intro', new=AsyncMock(return_value='INTRO')),
    ):
        await purchase.activate_trial(callback, user, db)
    return callback, user, db


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['all', 'off'])
async def test_trial_activation_lands_on_connect(monkeypatch, mode):
    connect = AsyncMock()
    callback, user, db = await _activate_trial(monkeypatch, mode, connect)

    if mode == 'all':
        connect.assert_awaited_once_with(callback, user, db, bb_intro='INTRO')
        callback.message.edit_text.assert_not_awaited()
    else:
        connect.assert_not_awaited()
        callback.message.edit_text.assert_awaited()


@pytest.mark.asyncio
async def test_trial_connect_error_falls_back_to_success_text(monkeypatch):
    """Экран подключения упал после выдачи пробного: не «ошибка активации», а обычный текст Bedolaga об успехе."""
    from app.localization.texts import get_texts

    connect = AsyncMock(side_effect=RuntimeError('panel down'))
    callback, _, _ = await _activate_trial(monkeypatch, 'all', connect)

    connect.assert_awaited_once()
    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]
    assert text.startswith(get_texts('ru').TRIAL_ACTIVATED)
    assert text != get_texts('ru').ERROR


def test_no_em_dash_in_texts():
    for language, strings in TEXTS.items():
        for key, value in strings.items():
            assert '—' not in value, (language, key)
    for path in Path(main_menu.__file__).parent.glob('*.py'):
        assert '—' not in path.read_text(encoding='utf-8'), path.name
