"""Тексты после оплаты в меню в переписке (F4): «ровно N ⭐», итог автопродления и покупки, продление сразу."""

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.bb import renew
from app.config import settings
from tests.bb.test_renew import _patch_auto_purchase, _sub, _tariff


THIS_YEAR = datetime.now(UTC).year


def _flat(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _labels(kb):
    return [[b.text for b in row] for row in kb.inline_keyboard]


def _button(kb, prefix):
    return next(b for row in kb.inline_keyboard for b in row if (b.callback_data or '').startswith(prefix))


@pytest.fixture
def payments_on(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 1.0)
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC')
    monkeypatch.setattr(settings, 'ENABLE_NOTIFICATIONS', True)


# --- «⭐ Telegram Stars · ровно N ⭐» на экранах нехватки ---


@pytest.mark.parametrize('mode', ['all', 'admins'])
def test_exact_stars_label_when_amount_known(monkeypatch, payments_on, mode):
    from app.handlers.subscription import tariff_purchase
    from app.keyboards.inline import get_payment_methods_keyboard

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', mode)
    kb = get_payment_methods_keyboard(23000, 'ru')
    assert _button(kb, 'topup_amount|stars|').text == '⭐ Telegram Stars · ровно 230 ⭐'
    assert _button(kb, 'topup_amount|tribute|').text == '💳 Банковская карта (Tribute)'
    en = get_payment_methods_keyboard(23000, 'en')
    assert _button(en, 'topup_amount|stars|').text == '⭐ Telegram Stars · exactly 230 ⭐'
    assert _button(en, 'topup_amount|tribute|').text == '💳 Bank card (Tribute)'

    # Экраны нехватки Bedolaga (продление и покупка) строятся из той же клавиатуры.
    extend = tariff_purchase.get_tariff_extend_insufficient_balance_keyboard(3, 11, 30, 'ru', missing_kopeks=23000)
    assert _button(extend, 'topup_amount|stars|').text == '⭐ Telegram Stars · ровно 230 ⭐'
    purchase = tariff_purchase.get_tariff_insufficient_balance_keyboard(3, 30, 'ru', missing_kopeks=5000)
    assert _button(purchase, 'topup_amount|stars|').text == '⭐ Telegram Stars · ровно 50 ⭐'

    # Курс не 1:1: звёзд столько, сколько выставит счёт Bedolaga (settings.rubles_to_stars).
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_RATE_RUB', 1.5)
    kb = get_payment_methods_keyboard(23000, 'ru')
    assert _button(kb, 'topup_amount|stars|').text == f'⭐ Telegram Stars · ровно {settings.rubles_to_stars(230)} ⭐'


def test_stars_label_untouched_without_amount_or_when_off(monkeypatch, payments_on):
    from app.keyboards.inline import get_payment_methods_keyboard

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    no_amount = get_payment_methods_keyboard(0, 'ru')
    assert 'ровно' not in _button(no_amount, 'topup_stars').text

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'off')
    kb = get_payment_methods_keyboard(23000, 'ru')
    assert 'ровно' not in _button(kb, 'topup_amount|stars|').text


# --- Итог автопродления и автопокупки после пополнения ---


def _auto_user(balance):
    user = MagicMock()
    user.id = 42
    user.telegram_id = 7
    user.language = 'ru'
    user.email = None
    user.balance_kopeks = balance
    user.restriction_subscription = False
    user.get_primary_promo_group = MagicMock(return_value=None)
    user.promo_offer_discount_percent = 0
    user.promo_offer_discount_expires_at = None
    return user


def _patch_success(monkeypatch, user):
    module = 'app.services.subscription_auto_purchase_service'

    async def subtract(db, target, amount, *args, **kwargs):
        target.balance_kopeks -= amount
        return True

    async def extend(db, subscription, days, **kwargs):
        subscription.end_date = subscription.end_date + timedelta(days=days)
        return subscription

    async def admin(handler):
        return None

    monkeypatch.setattr(f'{module}.subtract_user_balance', subtract)
    monkeypatch.setattr('app.database.crud.user.subtract_user_balance', subtract)
    monkeypatch.setattr(f'{module}.extend_subscription', extend)
    monkeypatch.setattr('app.database.crud.subscription.extend_subscription', extend)
    monkeypatch.setattr(f'{module}.create_transaction', AsyncMock(return_value=MagicMock(id=1)))
    monkeypatch.setattr('app.database.crud.transaction.create_transaction', AsyncMock(return_value=MagicMock(id=1)))
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    service = SimpleNamespace(update_remnawave_user=AsyncMock(), create_remnawave_user=AsyncMock())
    monkeypatch.setattr(f'{module}.SubscriptionService', lambda: service)
    monkeypatch.setattr(f'{module}.user_cart_service.delete_processed_cart', AsyncMock())
    monkeypatch.setattr(f'{module}.clear_subscription_checkout_draft', AsyncMock())
    monkeypatch.setattr(f'{module}._notify_email_user_auto_purchase', AsyncMock())
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', admin)
    monkeypatch.setattr('app.cabinet.routes.websocket.notify_user_subscription_renewed', AsyncMock())
    monkeypatch.setattr('app.cabinet.routes.websocket.notify_user_subscription_activated', AsyncMock())
    monkeypatch.setattr('app.services.checkout_squad_validation.validate_checkout_squads', AsyncMock())


def _extend_cart():
    return {
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


async def _topup(user, amount, bot):
    """Как провайдер: зачислили и зовут общую точку после пополнения (там автопокупка из корзины)."""
    from app.services.payment.common import send_cart_notification_after_topup

    user.balance_kopeks += amount
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = user.subscription
    await send_cart_notification_after_topup(user, amount, db, bot)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['all', 'off'])
async def test_auto_renewal_after_topup_reports_amounts(monkeypatch, payments_on, mode):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', mode)
    tariff = _tariff()
    sub = _sub(tariff=tariff, end=datetime(THIS_YEAR, 1, 2, 12, tzinfo=UTC))
    sub.updated_at = None
    sub.is_trial = False
    user = _auto_user(12000)
    user.subscription = sub
    _patch_auto_purchase(monkeypatch, user, sub, tariff, _extend_cart(), 35000)
    _patch_success(monkeypatch, user)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await _topup(user, 30000, bot)

    bot.send_message.assert_awaited_once()
    call = bot.send_message.await_args
    text = call.kwargs.get('text') or call.args[1]
    if mode == 'off':
        assert not text.startswith('✅ Подписка продлена до')
        assert renew.topup_reported() is False
        return
    assert text == '✅ Подписка продлена до 1 февраля. Пополнение 300 ₽, списано 350 ₽. На балансе осталось 70 ₽'
    kb = call.kwargs['reply_markup']
    assert _labels(kb) == [['🔗 Подключить устройство']]
    assert _flat(kb) == ['subscription_connect']
    assert renew.topup_reported() is True


@pytest.mark.asyncio
async def test_auto_renewal_message_en(monkeypatch, payments_on):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    tariff = _tariff()
    sub = _sub(tariff=tariff, end=datetime(THIS_YEAR, 1, 2, 12, tzinfo=UTC))
    sub.updated_at = None
    user = _auto_user(12000)
    user.language = 'en'
    user.subscription = sub
    _patch_auto_purchase(monkeypatch, user, sub, tariff, _extend_cart(), 35000)
    _patch_success(monkeypatch, user)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await _topup(user, 30000, bot)

    call = bot.send_message.await_args
    assert call.args[1] == ('✅ Subscription renewed until 1 February. Top-up 300 ₽, charged 350 ₽. Balance left 70 ₽')
    assert _labels(call.kwargs['reply_markup']) == [['🔗 Connect a device']]


@pytest.mark.asyncio
async def test_auto_purchase_after_topup_reports_amounts(monkeypatch, payments_on):
    """Пробный -> Pro из «Платных тарифов»: после пополнения тариф куплен сам, бот пишет итог с суммами."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    tariff = _tariff()
    tariff.allowed_squads = ['squad-a']
    trial = _sub(
        'trial', is_trial=True, tariff=_tariff('TRIAL PRO', None, 5), end=datetime(THIS_YEAR, 1, 2, tzinfo=UTC)
    )
    trial.updated_at = None
    user = _auto_user(12000)
    user.subscription = trial
    cart = {
        'cart_mode': 'tariff_purchase',
        'tariff_checkout_id': 'checkout-1',
        'tariff_id': 3,
        'period_days': 30,
        'total_price': 35000,
        'missing_amount': 23000,
        'return_to_cart': True,
        'description': 'Покупка тарифа PRO на 30 дней',
        'traffic_limit_gb': 0,
        'device_limit': 8,
        'subscription_id': 11,
    }
    _patch_auto_purchase(monkeypatch, user, trial, tariff, cart, 35000)
    _patch_success(monkeypatch, user)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_and_tariff', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        'app.services.pricing_engine.pricing_engine.calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=35000, promo_offer_discount=0)),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    from app.services.payment.common import send_cart_notification_after_topup

    user.balance_kopeks += 30000
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None  # оплаты этой корзины ещё не было
    await send_cart_notification_after_topup(user, 30000, db, bot)

    bot.send_message.assert_awaited_once()
    call = bot.send_message.await_args
    assert (
        call.args[1] == '✅ Тариф Pro подключён до 1 февраля. Пополнение 300 ₽, списано 350 ₽. На балансе осталось 70 ₽'
    )
    assert _flat(call.kwargs['reply_markup']) == ['subscription_connect']


@pytest.mark.asyncio
async def test_paid_message_needs_known_topup(monkeypatch, payments_on):
    """Без суммы пополнения (автопокупка не из пополнения) своё сообщение не шлём: остаётся сообщение Bedolaga."""
    monkeypatch.setattr(settings, 'BB_CHAT_MENU', 'all')
    bot = MagicMock()
    bot.send_message = AsyncMock()
    user = _auto_user(7000)
    end = datetime(THIS_YEAR, 2, 1, tzinfo=UTC)

    renew._topup_amount.set(None)
    assert await renew.notify_paid_after_topup(bot, user, end_date=end, price_kopeks=35000) is False
    bot.send_message.assert_not_awaited()

    renew.remember_topup(30000)
    assert renew.topup_reported() is False
    assert await renew.notify_paid_after_topup(bot, user, end_date=end, price_kopeks=35000) is True
    assert renew.topup_reported() is True

    # Ошибка Telegram: False, пусть пробует сообщение Bedolaga.
    renew.remember_topup(30000)
    bot.send_message = AsyncMock(side_effect=RuntimeError('telegram down'))
    assert await renew.notify_paid_after_topup(bot, user, end_date=end, price_kopeks=35000) is False
    assert renew.topup_reported() is False


@pytest.mark.asyncio
@pytest.mark.parametrize('reported', [True, False])
async def test_stars_success_message_skipped_after_renewal(monkeypatch, reported):
    """Stars: «Платеж успешно обработан» не шлём, если автопродление уже написало итог этого пополнения."""
    from app.handlers import stars_payments

    user = SimpleNamespace(id=42, telegram_id=7, language='ru')

    async def process(**kwargs):
        renew.remember_topup(30000)
        if reported:
            renew._topup_reported.set(True)
        return True

    service = MagicMock()
    service.process_stars_payment = process
    service.build_topup_success_keyboard = AsyncMock(return_value=None)
    monkeypatch.setattr(stars_payments, 'PaymentService', lambda bot: service)
    monkeypatch.setattr(stars_payments, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    message = MagicMock()
    message.answer = AsyncMock()
    message.from_user.id = 7
    message.chat.id = 7
    message.bot.delete_message = AsyncMock()
    message.successful_payment = SimpleNamespace(
        total_amount=300, invoice_payload='balance_42_30000', telegram_payment_charge_id='charge-123456789'
    )
    state = MagicMock()
    state.get_data = AsyncMock(return_value={})
    state.update_data = AsyncMock()

    await stars_payments.handle_successful_payment(message, MagicMock(), state)

    if reported:
        message.answer.assert_not_awaited()
    else:
        message.answer.assert_awaited_once()


# --- Продление сразу (денег хватило) ---


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['all', 'off'])
async def test_immediate_renewal_short_answer(monkeypatch, mode):
    from app.handlers.subscription import tariff_purchase

    monkeypatch.setattr(settings, 'BB_CHAT_MENU', mode)
    monkeypatch.setattr(settings, 'TIMEZONE', 'UTC')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    tariff = _tariff()
    sub = _sub(tariff=tariff, end=datetime(THIS_YEAR, 1, 2, 12, tzinfo=UTC))
    user = SimpleNamespace(id=42, telegram_id=7, language='ru', balance_kopeks=50000)

    async def extend(db, subscription, days, **kwargs):
        subscription.end_date = subscription.end_date + timedelta(days=days)
        return subscription

    monkeypatch.setattr(tariff_purchase, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(
        'app.services.pricing_engine.pricing_engine.calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=35000, promo_offer_discount=0)),
    )
    monkeypatch.setattr(tariff_purchase, '_reject_unavailable_checkout_squads', AsyncMock(return_value=False))
    monkeypatch.setattr(tariff_purchase, 'subtract_user_balance', AsyncMock(return_value=True))
    monkeypatch.setattr(tariff_purchase, 'extend_subscription', extend)
    monkeypatch.setattr(tariff_purchase, 'create_transaction', AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', AsyncMock())
    monkeypatch.setattr(
        'app.services.payment.lava.shift_lava_next_charge_after_manual_extension', AsyncMock(), raising=False
    )
    monkeypatch.setattr(tariff_purchase, 'should_create_panel_account', AsyncMock(return_value=False))
    monkeypatch.setattr(
        tariff_purchase, 'SubscriptionService', lambda: SimpleNamespace(update_remnawave_user=AsyncMock())
    )
    monkeypatch.setattr(
        tariff_purchase,
        'AdminNotificationService',
        lambda bot: SimpleNamespace(send_subscription_purchase_notification=AsyncMock()),
    )
    monkeypatch.setattr(tariff_purchase.user_cart_service, 'delete_user_cart', AsyncMock())
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = sub
    callback = MagicMock()
    callback.data = 'tariff_ext_confirm:11:3:30'
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    state = MagicMock()
    state.clear = AsyncMock()

    await inspect.unwrap(tariff_purchase.confirm_tariff_extend)(callback, user, db, state)

    callback.message.edit_text.assert_awaited_once()
    args, kwargs = callback.message.edit_text.await_args
    if mode == 'off':
        assert not args[0].startswith('✅ Подписка продлена до')
        return
    assert args[0] == '✅ Подписка продлена до 1 февраля.'
    kb = kwargs['reply_markup']
    assert _labels(kb) == [['🔗 Подключить устройство'], ['← Назад']]
    assert _flat(kb) == ['subscription_connect', 'back_to_menu']
