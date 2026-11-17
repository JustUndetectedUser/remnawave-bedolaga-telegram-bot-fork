from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.bb import renew, tariffs
from app.config import settings


THIS_YEAR = datetime.now(UTC).year


def _tariff(name='PRO', tag='PAID_PRO', tariff_id=3, devices=8, prices=None, **extra):
    return SimpleNamespace(
        id=tariff_id,
        name=name,
        panel_tag=tag,
        is_active=True,
        is_daily=False,
        period_prices=prices if prices is not None else {'30': 35000, '90': 90000},
        device_limit=devices,
        device_price_kopeks=None,
        traffic_limit_gb=0,
        allowed_squads=[],
        **extra,
    )


def _sub(status='active', *, is_trial=False, tariff=None, devices=None, end=None):
    tariff = tariff or _tariff()
    return SimpleNamespace(
        id=11,
        actual_status=status,
        status=status,
        is_trial=is_trial,
        tariff_id=tariff.id,
        device_limit=devices if devices is not None else tariff.device_limit,
        end_date=end or datetime(THIS_YEAR, 12, 16, 12, tzinfo=UTC),
        connected_squads=[],
    )


def _user(*, language='ru', balance=12000, subscription=None):
    return SimpleNamespace(
        id=42,
        telegram_id=7,
        language=language,
        balance_kopeks=balance,
        promo_group_id=None,
        promo_offer_discount_percent=0,
        promo_offer_discount_expires_at=None,
        subscription=subscription,
    )


def _callback():
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message.text = 'Подписка Pro до 16 декабря · Баланс 120 ₽'
    callback.message.photo = None
    callback.message.chat.id = 7
    callback.message.edit_text = AsyncMock()
    callback.bot.send_message = AsyncMock()
    return callback


def _callbacks(kb):
    return [[b.callback_data for b in row] for row in kb.inline_keyboard]


def _labels(kb):
    return [[b.text for b in row] for row in kb.inline_keyboard]


def _flat(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def _render(monkeypatch, user, tariff):
    monkeypatch.setattr(renew, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    callback = _callback()
    await renew.on_renew(callback, user, MagicMock())
    callback.message.edit_text.assert_awaited_once()
    callback.answer.assert_awaited_once()
    args, kwargs = callback.message.edit_text.await_args
    assert kwargs['parse_mode'] == 'HTML'
    return args[0], kwargs['reply_markup']


@pytest.mark.asyncio
async def test_renew_screen_buttons(monkeypatch):
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC')
    tariff = _tariff()

    # Денег не хватает: кнопка ведёт в выбор периода Bedolaga, он сохранит корзину и покажет оплату.
    text, kb = await _render(monkeypatch, _user(subscription=_sub(tariff=tariff)), tariff)
    assert text == 'Ваш тариф: Pro · 8 устройств · до 16 декабря · Баланс 120 ₽'
    assert _labels(kb) == [['Продлить на 30 дней · 350 ₽'], ['Сменить тариф'], ['← Назад']]
    assert _callbacks(kb) == [['tariff_extend:11:3:30'], ['tariff_switch'], ['back_to_menu']]

    # Денег хватает: сразу списание Bedolaga (tariff_ext_confirm), без экрана подтверждения.
    text, kb = await _render(monkeypatch, _user(balance=50000, subscription=_sub(tariff=tariff)), tariff)
    assert text == 'Ваш тариф: Pro · 8 устройств · до 16 декабря · Баланс 500 ₽'
    assert _callbacks(kb) == [['tariff_ext_confirm:11:3:30'], ['tariff_switch'], ['back_to_menu']]

    # Закончившаяся подписка продлевается с того же экрана.
    expired = _sub('expired', tariff=tariff, end=datetime(THIS_YEAR, 1, 2, tzinfo=UTC))
    text, kb = await _render(monkeypatch, _user(subscription=expired), tariff)
    assert text.startswith('Ваш тариф: Pro · 8 устройств · до 2 января')
    assert _callbacks(kb)[0] == ['tariff_extend:11:3:30']

    text, kb = await _render(monkeypatch, _user(language='en', subscription=_sub(tariff=tariff)), tariff)
    assert text == 'Your plan: Pro · 8 devices · until 16 December · Balance 120 ₽'
    assert _labels(kb) == [['Renew for 30 days · 350 ₽'], ['Change plan'], ['← Back']]


@pytest.mark.parametrize(
    ('devices', 'expected'),
    [(2, '2 устройства'), (4, '4 устройства'), (8, '8 устройств'), (12, '12 устройств')],
)
def test_renew_devices_plural(monkeypatch, devices, expected):
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC')
    tariff = _tariff('MAX', None, 4, devices=devices, prices={'30': 50000})
    sub = _sub(tariff=tariff)
    text, kb = renew.renew_message(_user(subscription=sub), sub, tariff, 50000, 30, 'ru')
    assert text == f'Ваш тариф: Max · {expected} · до 16 декабря · Баланс 120 ₽'
    assert kb.inline_keyboard[0][0].text == 'Продлить на 30 дней · 500 ₽'


@pytest.mark.asyncio
async def test_renew_trial_or_no_subscription_shows_paid_tariffs(monkeypatch):
    shown = AsyncMock()
    monkeypatch.setattr(tariffs, 'on_tariffs', shown)
    monkeypatch.setattr(renew, 'get_tariff_by_id', AsyncMock(return_value=_tariff('TRIAL PRO', None, 5)))
    db = MagicMock()

    for user in (_user(), _user(subscription=_sub('trial', is_trial=True, tariff=_tariff('TRIAL PRO', None, 5)))):
        callback = _callback()
        shown.reset_mock()
        await renew.on_renew(callback, user, db)
        shown.assert_awaited_once_with(callback, user, db)
        callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_renew_unlimited_or_disabled_falls_back_to_main_menu(monkeypatch):
    unlimited = _tariff('Unlimited Pro', 'ENDLESS_PRO', 9, prices={'3650': 0})
    menu = AsyncMock(return_value=True)
    monkeypatch.setattr(renew, 'bb_edit_main_menu', menu)
    cases = [
        (unlimited, _sub(tariff=unlimited, end=datetime(THIS_YEAR + 10, 1, 1, tzinfo=UTC))),
        (_tariff(), _sub('disabled')),
    ]
    for tariff, sub in cases:
        monkeypatch.setattr(renew, 'get_tariff_by_id', AsyncMock(return_value=tariff))
        menu.reset_mock()
        callback = _callback()
        await renew.on_renew(callback, _user(subscription=sub), MagicMock())
        menu.assert_awaited_once()
        callback.message.edit_text.assert_not_awaited()
        callback.answer.assert_awaited_once()


def test_renew_button_is_routed():
    from aiogram import Dispatcher

    from app.bb import entry

    dp = Dispatcher()
    entry.register_handlers(dp)
    assert renew.on_renew in [handler.callback for handler in dp.callback_query.handlers]


def _enable_auto_purchase(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', True)


@pytest.mark.asyncio
async def test_insufficient_shows_exact_missing_methods(monkeypatch):
    """Кнопка «Продлить» при нехватке денег -> экран Bedolaga: оплата ровно на 230 ₽ и корзина для автопокупки."""
    from app.handlers.subscription import tariff_purchase

    _enable_auto_purchase(monkeypatch)
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC')
    tariff = _tariff()
    sub = _sub(tariff=tariff)
    user = _user(subscription=sub)

    _, kb = await _render(monkeypatch, user, tariff)
    renew_data = kb.inline_keyboard[0][0].callback_data

    monkeypatch.setattr(tariff_purchase, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariff_purchase, 'get_subscription_by_id_for_user', AsyncMock(return_value=sub))
    save_cart = AsyncMock()
    monkeypatch.setattr(tariff_purchase.user_cart_service, 'save_user_cart', save_cart)
    callback = _callback()
    callback.data = renew_data
    state = MagicMock()
    state.update_data = AsyncMock()

    await tariff_purchase.select_tariff_extend_period(callback, user, MagicMock(), state)

    cart = save_cart.await_args.args[1]
    assert cart['cart_mode'] == 'extend' and cart['subscription_id'] == 11 and cart['period_days'] == 30
    assert cart['missing_amount'] == 23000 and cart['return_to_cart'] is True
    insufficient = callback.message.edit_text.await_args.kwargs['reply_markup']
    callbacks = _flat(insufficient)
    assert 'topup_amount|stars|23000' in callbacks
    assert 'topup_amount|tribute|23000' in callbacks
    assert 'balance_topup' not in callbacks
    labels = [label for row in _labels(insufficient) for label in row]
    assert any('Tribute' in label for label in labels)

    # Тот же билдер, вызванный напрямую.
    direct = tariff_purchase.get_tariff_extend_insufficient_balance_keyboard(3, 11, 30, 'ru', missing_kopeks=23000)
    assert 'topup_amount|stars|23000' in _flat(direct) and 'topup_amount|tribute|23000' in _flat(direct)


def _patch_auto_purchase(monkeypatch, user, sub, tariff, cart, price):
    module = 'app.services.subscription_auto_purchase_service'
    monkeypatch.setattr(f'{module}.user_cart_service.has_topup_intent', AsyncMock(return_value=True))
    monkeypatch.setattr(f'{module}.user_cart_service.get_user_cart', AsyncMock(return_value=cart))
    monkeypatch.setattr(f'{module}.user_cart_service.get_all_subscription_carts', AsyncMock(return_value=[]))
    subtract = AsyncMock(return_value=True)
    monkeypatch.setattr(f'{module}.subtract_user_balance', subtract)
    monkeypatch.setattr('app.database.crud.transaction.get_user_transactions', AsyncMock(return_value=[]))
    monkeypatch.setattr('app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.database.crud.subscription.get_subscription_by_id_for_user', AsyncMock(return_value=sub))
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff))
    pricing = SimpleNamespace(final_total=price, original_total=price)
    monkeypatch.setattr(
        'app.services.pricing_engine.pricing_engine.calculate_renewal_price', AsyncMock(return_value=pricing)
    )
    return subtract


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['all', 'off'])
async def test_partial_topup_reports_remaining(monkeypatch, mode):
    """Tribute пришло 180 ₽ из 230 ₽: продление не прошло, в новом меню бот пишет, что не хватает ещё 50 ₽.

    Bedolaga в этом случае молчит (только лог), поэтому при выключенном меню сообщения нет.
    """
    from app.services.subscription_auto_purchase_service import auto_purchase_saved_cart_after_topup

    _enable_auto_purchase(monkeypatch)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', mode)
    tariff = _tariff()
    sub = _sub(tariff=tariff)
    sub.updated_at = None
    sub.status = 'active'
    user = MagicMock()
    user.id = 42
    user.telegram_id = 7
    user.language = 'ru'
    user.balance_kopeks = 12000 + 18000
    user.restriction_subscription = False
    user.get_primary_promo_group = MagicMock(return_value=None)
    user.promo_offer_discount_percent = 0
    user.promo_offer_discount_expires_at = None
    cart = {
        'cart_mode': 'extend',
        'tariff_id': 3,
        'subscription_id': 11,
        'period_days': 30,
        'total_price': 35000,
        'missing_amount': 23000,
        'return_to_cart': True,
        'description': 'Продление тарифа PRO на 30 дней',
        'device_limit': 8,
    }
    subtract = _patch_auto_purchase(monkeypatch, user, sub, tariff, cart, 35000)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = sub
    bot = MagicMock()
    bot.send_message = AsyncMock()

    assert await auto_purchase_saved_cart_after_topup(db, user, bot=bot) is False

    subtract.assert_not_awaited()
    if mode == 'off':
        bot.send_message.assert_not_awaited()
        return
    bot.send_message.assert_awaited_once()
    args, kwargs = bot.send_message.await_args
    assert args[0] == 7
    assert args[1] == 'Пополнение получено, но для продления не хватает ещё 50 ₽.'
    # Под сообщением та же оплата недостающего, что на экране нехватки: корзина ещё сохранена.
    callbacks = _flat(kwargs['reply_markup'])
    assert 'topup_amount|stars|5000' in callbacks and 'topup_amount|tribute|5000' in callbacks

    # en
    bot.send_message.reset_mock()
    user.language = 'en'
    await auto_purchase_saved_cart_after_topup(db, user, bot=bot)
    assert bot.send_message.await_args.args[1] == 'Top-up received, but 50 ₽ more is needed to renew.'


@pytest.mark.asyncio
async def test_shortfall_notice_never_raises(monkeypatch):
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError('telegram down'))
    user = _user()
    await renew.notify_renew_shortfall(bot, user, missing_kopeks=5000, tariff_id=3, subscription_id=11, period=30)
    bot.send_message.assert_awaited_once()

    # Без Telegram (вход по почте) и без нехватки: ничего не шлём.
    bot.send_message.reset_mock()
    await renew.notify_renew_shortfall(
        bot,
        SimpleNamespace(**{**vars(user), 'telegram_id': None}),
        missing_kopeks=5000,
        tariff_id=3,
        subscription_id=11,
        period=30,
    )
    await renew.notify_renew_shortfall(bot, user, missing_kopeks=0, tariff_id=3, subscription_id=11, period=30)
    await renew.notify_renew_shortfall(None, user, missing_kopeks=5000, tariff_id=3, subscription_id=11, period=30)
    bot.send_message.assert_not_awaited()
