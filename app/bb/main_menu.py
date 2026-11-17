"""Главное меню в переписке (BB_CHAT_MENU): строка статуса и три ряда кнопок, без картинок.

Рисуется вместо rich-меню и классического меню Bedolaga на всех путях показа главного меню:
хуки в app/utils/rich_menu.py (try_send_rich_main_menu и try_edit_rich_main_menu).
"""

import html
from datetime import date, datetime

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.bb.texts import bb_count, bb_text
from app.config import settings
from app.database.crud.tariff import get_tariff_by_id, get_trial_tariff
from app.localization.texts import Texts
from app.utils.timezone import local_date


logger = structlog.get_logger(__name__)

ENDLESS_TAG = 'ENDLESS_PRO'
ACTIVE_STATES = frozenset({'active', 'trial', 'limited'})
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

_MONTHS = {
    'ru': (
        'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
        'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
    ),
    'en': (
        'January', 'February', 'March', 'April', 'May', 'June',
        'July', 'August', 'September', 'October', 'November', 'December',
    ),
}  # fmt: skip


def format_day(moment: datetime, language: str | None, *, today: date | None = None) -> str:
    """«16 декабря» / «16 December»; год только если он не текущий. Дата в зоне TIMEZONE."""
    day = local_date(moment)
    today = today or local_date()
    lang = 'en' if (language or 'ru').lower().startswith('en') else 'ru'
    text = f'{day.day} {_MONTHS[lang][day.month - 1]}'
    return text if day.year == today.year else f'{text} {day.year}'


def display_name(name: str | None) -> str:
    """Название тарифа для людей: слова заглавными в базе (PRO, TRIAL PRO) с заглавной буквы, «Unlimited Pro» как есть."""
    return ' '.join(word.capitalize() if word.isupper() else word for word in (name or '').split())


def trial_display_name(name: str | None) -> str:
    """«TRIAL PRO» -> «Pro»: название пробного тарифа без слова TRIAL."""
    return display_name(' '.join(word for word in (name or '').split() if word.lower() != 'trial'))


def is_unlimited(tariff) -> bool:
    return str(getattr(tariff, 'panel_tag', None) or '').strip().upper() == ENDLESS_TAG


def trial_available(user) -> bool:
    """Те же проверки, что у ссылки на пробный в rich-меню Bedolaga (rich_menu._trial_offer_link)
    и у activate_trial: длительность, TRIAL_DISABLED_FOR, тип входа, пробный уже брали."""
    if settings.TRIAL_DURATION_DAYS <= 0 or settings.TRIAL_DISABLED_FOR == 'all':
        return False
    if settings.is_trial_disabled_for_user(getattr(user, 'auth_type', None)):
        return False
    try:
        return not user.is_trial_already_used()
    except Exception as error:
        logger.debug('BlackBird: не удалось проверить доступность пробного', error=str(error))
        return False


def _button(language: str, key: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=bb_text(language, key), callback_data=callback_data)


def _keyboard(language: str, *rows: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button(language, key, data) for key, data in row] for row in rows])


CONNECT = ('btn_connect', 'subscription_connect')
RENEW = ('btn_renew', 'bb_renew')
TOPUP = ('btn_topup', 'balance_topup')
MORE = ('btn_more', 'bb_more')
TRIAL = ('btn_trial', 'trial_activate')
TARIFFS = ('btn_tariffs', 'bb_tariffs')


def _first_visit(user, language: str, trial_days: int | None, trial_devices: int | None):
    legal = bb_text(language, 'legal')
    if not trial_available(user):
        return f'{bb_text(language, "welcome_no_trial")}\n\n{legal}', _keyboard(language, (TARIFFS,))
    days = trial_days if trial_days is not None else settings.TRIAL_DURATION_DAYS
    devices = trial_devices if trial_devices is not None else settings.TRIAL_DEVICE_LIMIT
    welcome = bb_text(language, 'welcome_trial').format(
        days=bb_count(language, days, 'days'),
        devices=bb_count(language, devices, 'devices_upto'),
    )
    return f'{welcome}\n\n{legal}', _keyboard(language, (TRIAL,), (TARIFFS,))


def bb_main_menu(
    user,
    subscription,
    language: str,
    *,
    tariff=None,
    trial_days: int | None = None,
    trial_devices: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст (HTML) и кнопки главного меню. tariff: тариф подписки, если уже загружен отдельно."""
    if subscription is None:
        return _first_visit(user, language, trial_days, trial_devices)

    status = (getattr(subscription, 'actual_status', '') or '').lower()
    active = status in ACTIVE_STATES

    # Пробный закончился или его отключил сторож: сразу к платным тарифам.
    if getattr(subscription, 'is_trial', False) and not active:
        return bb_text(language, 'trial_over'), _keyboard(language, (TARIFFS,), (MORE,))

    if tariff is None:
        tariff = getattr(subscription, 'tariff', None)
    raw_name = getattr(tariff, 'name', None)
    name = html.escape(display_name(raw_name) or bb_text(language, 'menu_no_tariff'))
    balance = Texts.format_price(getattr(user, 'balance_kopeks', 0) or 0)
    end_date = getattr(subscription, 'end_date', None)
    day = format_day(end_date, language) if end_date else ''

    # Unlimited Pro бесплатный: не предлагать ни продление, ни пополнение.
    if is_unlimited(tariff):
        if active:
            return bb_text(language, 'menu_status_unlimited').format(tariff=name), _keyboard(
                language, (CONNECT,), (MORE,)
            )
        return bb_text(language, 'menu_status_inactive').format(tariff=name, balance=balance), _keyboard(
            language, (MORE,)
        )

    if active:
        if getattr(subscription, 'is_trial', False):
            trial_name = html.escape(trial_display_name(raw_name) or bb_text(language, 'menu_no_tariff'))
            text = bb_text(language, 'menu_status_trial').format(tariff=trial_name, date=day, balance=balance)
        else:
            text = bb_text(language, 'menu_status').format(tariff=name, date=day, balance=balance)
        return text, _keyboard(language, (CONNECT,), (RENEW, TOPUP), (MORE,))

    if status == 'expired':
        text = bb_text(language, 'menu_status_expired').format(tariff=name, date=day, balance=balance)
        return text, _keyboard(language, (RENEW,), (CONNECT,), (TOPUP,), (MORE,))

    # Отключена (админом) или в непонятном состоянии: ни продления, ни подключения.
    return bb_text(language, 'menu_status_inactive').format(tariff=name, balance=balance), _keyboard(language, (MORE,))


async def trial_numbers(db: AsyncSession) -> tuple[int, int]:
    """Дни и устройства пробного так же, как их выдаёт activate_trial: из пробного тарифа, иначе из настроек."""
    days = settings.TRIAL_DURATION_DAYS
    devices = None
    if not settings.is_devices_selection_enabled():
        devices = settings.get_disabled_mode_device_limit()
    if settings.is_tariffs_mode():
        try:
            trial_tariff = await get_trial_tariff(db)
            if not trial_tariff and settings.get_trial_tariff_id() > 0:
                trial_tariff = await get_tariff_by_id(db, settings.get_trial_tariff_id())
            if trial_tariff:
                devices = trial_tariff.device_limit
                days = getattr(trial_tariff, 'trial_duration_days', None) or days
        except Exception as error:
            logger.error('BlackBird: не удалось получить пробный тариф', error=str(error))
    if devices is None:
        devices = settings.TRIAL_DEVICE_LIMIT
    return days, devices


async def build_bb_main_menu(db_user, db: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    language = getattr(db_user, 'language', None) or 'ru'
    subscription = getattr(db_user, 'subscription', None)
    tariff = None
    trial_days = trial_devices = None
    if subscription is not None and getattr(subscription, 'tariff_id', None):
        tariff = await get_tariff_by_id(db, subscription.tariff_id, with_promo_groups=False)
    if subscription is None and trial_available(db_user):
        trial_days, trial_devices = await trial_numbers(db)
    return bb_main_menu(
        db_user, subscription, language, tariff=tariff, trial_days=trial_days, trial_devices=trial_devices
    )


async def bb_send_main_menu(bot: Bot, chat_id: int, db_user, db: AsyncSession) -> bool:
    """Новое сообщение с меню. False: не получилось, Bedolaga рисует своё меню."""
    try:
        text, keyboard = await build_bb_main_menu(db_user, db)
        await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode='HTML', link_preview_options=NO_PREVIEW)
        return True
    except TelegramForbiddenError:
        logger.warning('BlackBird: меню не отправлено, бот заблокирован', chat_id=chat_id)
        return True
    except Exception:
        logger.exception('BlackBird: не удалось отправить меню', chat_id=chat_id)
        return False


async def bb_edit_main_menu(callback: CallbackQuery, db_user, db: AsyncSession) -> bool:
    """Меню на месте сообщения с кнопкой; картинку или rich-сообщение заменяет новым текстом."""
    message, bot = callback.message, callback.bot
    if message is None or bot is None:
        return False
    chat_id = message.chat.id
    try:
        text, keyboard = await build_bb_main_menu(db_user, db)
    except Exception:
        logger.exception('BlackBird: не удалось собрать меню', chat_id=chat_id)
        return False
    try:
        if not isinstance(message, InaccessibleMessage) and message.text is not None and not message.photo:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode='HTML',
                    link_preview_options=NO_PREVIEW,
                )
                return True
            except TelegramBadRequest as error:
                if 'message is not modified' in str(error).lower():
                    return True
                logger.debug('BlackBird: меню не отредактировалось, пришлю новое', error=str(error))
        if not isinstance(message, InaccessibleMessage):
            try:
                await message.delete()
            except Exception as error:
                logger.debug('BlackBird: не удалось удалить старое меню', error=str(error))
        await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode='HTML', link_preview_options=NO_PREVIEW)
        return True
    except TelegramForbiddenError:
        logger.warning('BlackBird: меню не показано, бот заблокирован', chat_id=chat_id)
        return True
    except Exception:
        logger.exception('BlackBird: не удалось показать меню', chat_id=chat_id)
        return False
