"""«Продлить или сменить тариф»: тариф, устройства, дата, баланс и продление на 30 дней.

Продлевает и берёт деньги Bedolaga (app/handlers/subscription/tariff_purchase.py), своих списаний здесь нет.
Денег хватает: кнопка tariff_ext_confirm:<подписка>:<тариф>:<дни> продлевает сразу. Не хватает:
tariff_extend:<подписка>:<тариф>:<дни> сохраняет корзину и показывает оплату ровно недостающей суммы
(AUTO_PURCHASE_AFTER_TOPUP_ENABLED), после оплаты подписка продлевается сама. Пополнения не хватило:
notify_renew_shortfall и notify_purchase_shortfall пишут, сколько ещё нужно.
"""

import html
from contextvars import ContextVar

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bb import tariffs as bb_tariffs
from app.bb.main_menu import NO_PREVIEW, bb_edit_main_menu, display_name, format_day, is_unlimited
from app.bb.rollout import chat_menu_enabled
from app.bb.texts import bb_count, bb_text
from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.localization.texts import Texts
from app.services.pricing_engine import pricing_engine


logger = structlog.get_logger(__name__)

RENEW_DAYS = 30
RENEWABLE_STATES = frozenset({'active', 'limited', 'expired'})

# Пополнение, после которого идёт автопокупка из корзины: сумма (для итога «Пополнение 300 ₽») и признак,
# что итог уже написан (тогда Stars не шлёт своё «Платеж успешно обработан»). Ставит
# payment.common.send_cart_notification_after_topup; живёт в контексте обработки одного платежа.
_topup_amount: ContextVar[int | None] = ContextVar('bb_topup_amount', default=None)
_topup_reported: ContextVar[bool] = ContextVar('bb_topup_reported', default=False)


def renewable(subscription) -> bool:
    """Действующая, с исчерпанным трафиком или закончившаяся; отключённую админом не продлеваем."""
    return (getattr(subscription, 'actual_status', '') or '').lower() in RENEWABLE_STATES


def renew_period(tariff) -> int | None:
    """30 дней, если у тарифа есть такая цена, иначе самый короткий платный период."""
    periods = []
    for period, price in (getattr(tariff, 'period_prices', None) or {}).items():
        try:
            if int(price) > 0:
                periods.append(int(period))
        except (TypeError, ValueError):
            continue
    if not periods:
        return None
    return RENEW_DAYS if RENEW_DAYS in periods else min(periods)


def _devices(subscription, tariff) -> int:
    # Как в продлении Bedolaga: лимит подписки (с докупленными устройствами), иначе лимит тарифа.
    return getattr(subscription, 'device_limit', None) or tariff.device_limit


async def renew_price(tariff, period: int, subscription, user) -> int:
    """Цена тем же расчётом, которым продление Bedolaga потом списывает деньги (скидки, лишние устройства)."""
    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff, period, device_limit=_devices(subscription, tariff), user=user
    )
    return result.final_total


def renew_message(
    user, subscription, tariff, price_kopeks: int, period: int | None, language: str
) -> tuple[str, InlineKeyboardMarkup]:
    balance = getattr(user, 'balance_kopeks', 0) or 0
    end_date = getattr(subscription, 'end_date', None)
    text = bb_text(language, 'renew_status').format(
        tariff=html.escape(display_name(tariff.name)),
        devices=bb_count(language, _devices(subscription, tariff), 'devices'),
        date=format_day(end_date, language) if end_date else '',
        balance=Texts.format_price(balance),
    )
    rows = []
    if period:
        step = 'tariff_ext_confirm' if balance >= price_kopeks else 'tariff_extend'
        label = bb_text(language, 'btn_renew_period').format(
            period=bb_count(language, period, 'days'), price=Texts.format_price(price_kopeks)
        )
        rows.append([InlineKeyboardButton(text=label, callback_data=f'{step}:{subscription.id}:{tariff.id}:{period}')])
    rows.append([InlineKeyboardButton(text=bb_text(language, 'btn_change_tariff'), callback_data='tariff_switch')])
    rows.append([InlineKeyboardButton(text=bb_text(language, 'btn_back'), callback_data='back_to_menu')])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(callback: types.CallbackQuery, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """На месте меню; сообщение с картинкой или недоступное для правки заменяем новым."""
    message = callback.message
    if message is not None and not isinstance(message, InaccessibleMessage) and message.text and not message.photo:
        try:
            await message.edit_text(text, reply_markup=keyboard, parse_mode='HTML', link_preview_options=NO_PREVIEW)
            return
        except TelegramBadRequest as error:
            if 'message is not modified' in str(error).lower():
                return
            logger.debug('BlackBird: экран продления не отредактировался, пришлю новый', error=str(error))
    chat_id = message.chat.id if message is not None else callback.from_user.id
    await callback.bot.send_message(
        chat_id, text, reply_markup=keyboard, parse_mode='HTML', link_preview_options=NO_PREVIEW
    )


async def on_renew(callback: types.CallbackQuery, db_user, db: AsyncSession) -> None:
    language = getattr(db_user, 'language', None) or 'ru'
    subscription = getattr(db_user, 'subscription', None)
    # Подписки нет или она пробная: продлевать нечего, выбор платного тарифа.
    if subscription is None or getattr(subscription, 'is_trial', False):
        await bb_tariffs.on_tariffs(callback, db_user, db)
        return

    tariff_id = getattr(subscription, 'tariff_id', None)
    tariff = await get_tariff_by_id(db, tariff_id) if tariff_id else None
    # Бессрочный Unlimited Pro и отключённая подписка: в меню кнопки нет, на старую кнопку вернуть меню.
    if is_unlimited(tariff) or not renewable(subscription):
        await bb_edit_main_menu(callback, db_user, db)
        await callback.answer()
        return
    # Подписка без тарифа или тариф скрыт: как и Bedolaga, предложить выбрать тариф.
    if tariff is None or not getattr(tariff, 'is_active', False):
        await bb_tariffs.on_tariffs(callback, db_user, db)
        return

    period = renew_period(tariff)
    price = await renew_price(tariff, period, subscription, db_user) if period else 0
    text, keyboard = renew_message(db_user, subscription, tariff, price, period, language)
    await _show(callback, text, keyboard)
    await callback.answer()


def shortfall_keyboard(missing_kopeks: int, language: str) -> InlineKeyboardMarkup:
    """Оплата ровно недостающей суммы (кнопки topup_amount|метод|сумма) и «← Назад» в главное меню.

    Кнопки оплаты те же, что на экране нехватки Bedolaga (get_payment_methods_keyboard), без её навигации.
    Корзина и метка намерения остаются, так что после оплаты покупка или продление пройдут сами.
    """
    # Локальный импорт: клавиатуры Bedolaga тянут за собой половину бота.
    from app.keyboards.inline import get_payment_methods_keyboard

    rows = [
        row
        for row in get_payment_methods_keyboard(missing_kopeks, language).inline_keyboard
        if row and all((button.callback_data or '').startswith('topup_amount|') for button in row)
    ]
    if not rows:
        rows = [[InlineKeyboardButton(text=bb_text(language, 'btn_topup'), callback_data='balance_topup')]]
    rows.append([InlineKeyboardButton(text=bb_text(language, 'btn_back'), callback_data='back_to_menu')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _notify_shortfall(bot: Bot | None, user, missing_kopeks: int, text_key: str) -> None:
    """Пополнения не хватило на покупку из корзины: сколько ещё нужно, под ним оплата остатка.

    Bedolaga в этом случае только пишет в лог. bot есть только у автопокупки после пополнения: при покупке
    кнопкой «Подтвердить» (_confirm_tariff_checkout) его нет и сообщения нет. Ошибки только в лог:
    автопокупка не должна падать из-за сообщения.
    """
    chat_id = getattr(user, 'telegram_id', None)
    if bot is None or not chat_id or missing_kopeks <= 0 or not chat_menu_enabled(user):
        return
    language = getattr(user, 'language', None) or 'ru'
    try:
        text = bb_text(language, text_key).format(missing=Texts.format_price(missing_kopeks))
        await bot.send_message(chat_id, text, reply_markup=shortfall_keyboard(missing_kopeks, language))
    except Exception:
        logger.exception('BlackBird: не удалось сообщить о нехватке после пополнения', chat_id=chat_id)


async def notify_renew_shortfall(
    bot: Bot | None, user, *, missing_kopeks: int, tariff_id: int | None, subscription_id: int, period: int
) -> None:
    """Продление из корзины (subscription_auto_purchase_service._auto_extend_subscription).

    tariff_id, subscription_id и period в сообщении не нужны: оплата остатка идёт по сохранённой корзине.
    """
    await _notify_shortfall(bot, user, missing_kopeks, 'renew_topup_short')


async def notify_purchase_shortfall(bot: Bot | None, user, *, missing_kopeks: int, tariff_id: int, period: int) -> None:
    """Покупка тарифа из корзины, в том числе пробный -> платный (_auto_purchase_tariff)."""
    await _notify_shortfall(bot, user, missing_kopeks, 'purchase_topup_short')


def _connect_keyboard(language: str, *, back: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=bb_text(language, 'btn_connect'), callback_data='subscription_connect')]]
    if back:
        rows.append([InlineKeyboardButton(text=bb_text(language, 'btn_back'), callback_data='back_to_menu')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def renewed_message(subscription, language: str) -> tuple[str, InlineKeyboardMarkup]:
    """Продление сразу (денег хватило, tariff_ext_confirm): «✅ Подписка продлена до …»."""
    end_date = getattr(subscription, 'end_date', None)
    text = bb_text(language, 'renewed').format(date=format_day(end_date, language) if end_date else '')
    return text, _connect_keyboard(language, back=True)


def remember_topup(amount_kopeks: int) -> None:
    """Хук в send_cart_notification_after_topup: сумма пополнения до автопокупки из корзины."""
    _topup_amount.set(amount_kopeks)
    _topup_reported.set(False)


def topup_reported() -> bool:
    """Итог автопокупки по этому пополнению уже отправлен (меню в переписке)."""
    return _topup_reported.get()


async def notify_paid_after_topup(
    bot: Bot | None, user, *, end_date, price_kopeks: int, tariff_name: str | None = None
) -> bool:
    """Итог автопродления (tariff_name None) или покупки тарифа после пополнения с суммами и «Подключить устройство».

    True: сообщение ушло, сообщение Bedolaga не нужно. False (меню выключено, сумма пополнения неизвестна,
    ошибка Telegram): пусть пишет Bedolaga.
    """
    amount = _topup_amount.get()
    chat_id = getattr(user, 'telegram_id', None)
    if bot is None or not chat_id or amount is None or not chat_menu_enabled(user):
        return False
    if not settings.is_notifications_enabled():
        return False
    language = getattr(user, 'language', None) or 'ru'
    values = {
        'date': format_day(end_date, language) if end_date else '',
        'amount': Texts.format_price(amount),
        'price': Texts.format_price(price_kopeks),
        'balance': Texts.format_price(getattr(user, 'balance_kopeks', 0) or 0),
    }
    if tariff_name is None:
        text = bb_text(language, 'renewed_after_topup').format(**values)
    else:
        text = bb_text(language, 'purchased_after_topup').format(
            tariff=html.escape(display_name(tariff_name)), **values
        )
    try:
        await bot.send_message(chat_id, text, reply_markup=_connect_keyboard(language), parse_mode='HTML')
    except Exception:
        logger.exception('BlackBird: не удалось написать итог оплаты', chat_id=chat_id)
        return False
    _topup_reported.set(True)
    return True


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(on_renew, F.data == 'bb_renew')
