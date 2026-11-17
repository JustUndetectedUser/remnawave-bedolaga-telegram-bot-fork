"""«Продлить или сменить тариф»: тариф, устройства, дата, баланс и продление на 30 дней.

Продлевает и берёт деньги Bedolaga (app/handlers/subscription/tariff_purchase.py), своих списаний здесь нет.
Денег хватает: кнопка tariff_ext_confirm:<подписка>:<тариф>:<дни> продлевает сразу. Не хватает:
tariff_extend:<подписка>:<тариф>:<дни> сохраняет корзину и показывает оплату ровно недостающей суммы
(AUTO_PURCHASE_AFTER_TOPUP_ENABLED), после оплаты подписка продлевается сама. Пополнения не хватило:
notify_renew_shortfall и notify_purchase_shortfall пишут, сколько ещё нужно.
"""

import html

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bb import tariffs as bb_tariffs
from app.bb.main_menu import NO_PREVIEW, bb_edit_main_menu, display_name, format_day, is_unlimited
from app.bb.rollout import chat_menu_enabled
from app.bb.texts import bb_count, bb_text
from app.database.crud.tariff import get_tariff_by_id
from app.localization.texts import Texts
from app.services.pricing_engine import pricing_engine


logger = structlog.get_logger(__name__)

RENEW_DAYS = 30
RENEWABLE_STATES = frozenset({'active', 'limited', 'expired'})


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


async def _notify_shortfall(bot: Bot | None, user, missing_kopeks: int, text_key: str, keyboard) -> None:
    """Пополнения не хватило на покупку из корзины: сколько ещё нужно, под ним оплата остатка.

    Bedolaga в этом случае только пишет в лог. Корзина и метка намерения остаются, так что оплата остатка
    кнопками Bedolaga (keyboard(tariff_purchase, language)) доведёт покупку до конца сама. bot есть только у
    автопокупки после пополнения: при покупке кнопкой «Подтвердить» (_confirm_tariff_checkout) его нет и
    сообщения нет. Ошибки только в лог: автопокупка не должна падать из-за сообщения.
    """
    chat_id = getattr(user, 'telegram_id', None)
    if bot is None or not chat_id or missing_kopeks <= 0 or not chat_menu_enabled(user):
        return
    language = getattr(user, 'language', None) or 'ru'
    try:
        # Локальный импорт: модуль хендлеров тянет за собой почти весь бот.
        from app.handlers.subscription import tariff_purchase

        text = bb_text(language, text_key).format(missing=Texts.format_price(missing_kopeks))
        await bot.send_message(chat_id, text, reply_markup=keyboard(tariff_purchase, language))
    except Exception:
        logger.exception('BlackBird: не удалось сообщить о нехватке после пополнения', chat_id=chat_id)


async def notify_renew_shortfall(
    bot: Bot | None, user, *, missing_kopeks: int, tariff_id: int | None, subscription_id: int, period: int
) -> None:
    """Продление из корзины (subscription_auto_purchase_service._auto_extend_subscription)."""
    await _notify_shortfall(
        bot,
        user,
        missing_kopeks,
        'renew_topup_short',
        lambda module, language: module.get_tariff_extend_insufficient_balance_keyboard(
            tariff_id, subscription_id, period, language, missing_kopeks=missing_kopeks
        ),
    )


async def notify_purchase_shortfall(bot: Bot | None, user, *, missing_kopeks: int, tariff_id: int, period: int) -> None:
    """Покупка тарифа из корзины, в том числе пробный -> платный (_auto_purchase_tariff)."""
    await _notify_shortfall(
        bot,
        user,
        missing_kopeks,
        'purchase_topup_short',
        lambda module, language: module.get_tariff_insufficient_balance_keyboard(
            tariff_id, period, language, missing_kopeks=missing_kopeks
        ),
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(on_renew, F.data == 'bb_renew')
