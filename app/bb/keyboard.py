"""Три постоянные кнопки над строкой ввода и кнопка слева от поля ввода.

Модуль лёгкий (без хендлеров): его зовут и главное меню, и старт бота.
"""

import structlog
from aiogram import Bot
from aiogram.types import (
    InlineKeyboardButton,
    KeyboardButton,
    MenuButtonCommands,
    MenuButtonDefault,
    ReplyKeyboardMarkup,
)

from app.bb.rollout import chat_menu_enabled, chat_menu_rolled_out
from app.bb.texts import bb_text
from app.config import settings
from app.utils.cache import cache


logger = structlog.get_logger(__name__)

# Метка «клавиатуру уже присылали»: тем, кто давно в боте, она придёт с первым же меню, а не только после /start.
KEYBOARD_FLAG_TTL = 30 * 24 * 3600


def _flag_key(telegram_id) -> str:
    # bb_kb3: клавиатура из трёх кнопок (21.11). Новое имя метки, чтобы у тех, кому приходили две кнопки
    # (bb_kb_sent), новая клавиатура пришла с первым же меню, а не только после /start.
    return f'bb_kb3_sent:{telegram_id}'


def bb_reply_keyboard(language: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=bb_text(language, 'btn_bot')),
                KeyboardButton(text=bb_text(language, 'btn_web')),
                KeyboardButton(text=bb_text(language, 'btn_miniapp')),
            ]
        ],
        is_persistent=True,
        resize_keyboard=True,
    )


async def send_bb_keyboard(bot: Bot, chat_id: int, language: str) -> None:
    """Короткое сообщение с клавиатурой и пустая кнопка слева для чата. Ошибки только в лог.

    MenuButtonDefault для чата значит «как у бота», то есть снова «Кабинет», если он выставлен боту
    (MENU_BUTTON_WEBAPP_ENABLED). MenuButtonCommands без зарегистрированных команд ничего не показывает.
    """
    try:
        await bot.send_message(chat_id, bb_text(language, 'keyboard_note'), reply_markup=bb_reply_keyboard(language))
    except Exception as error:
        logger.warning('BlackBird: не удалось отправить клавиатуру', chat_id=chat_id, error=str(error))
    try:
        await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonCommands())
    except Exception as error:
        logger.warning('BlackBird: не удалось убрать кнопку меню', chat_id=chat_id, error=str(error))


async def maybe_send_bb_keyboard(bot: Bot, chat_id: int, user) -> None:
    """/start и регистрация: клавиатура всегда (вернуть, если скрыли) и метка, чтобы меню её не дублировало."""
    if not chat_menu_enabled(user):
        return
    await send_bb_keyboard(bot, chat_id, getattr(user, 'language', None) or 'ru')
    await cache.set(_flag_key(user.telegram_id), 1, expire=KEYBOARD_FLAG_TTL)


async def _first_time(user) -> bool:
    key = _flag_key(user.telegram_id)
    if await cache.setnx(key, 1, expire=KEYBOARD_FLAG_TTL):
        return True
    # Метка уже есть или Redis недоступен: без Redis лучше прислать клавиатуру лишний раз.
    return not await cache.exists(key)


async def maybe_send_bb_keyboard_once(bot: Bot, chat_id: int, user) -> None:
    """Первое меню в переписке у того, кто пришёл до включения: прислать клавиатуру один раз (метка на 30 дней)."""
    if not chat_menu_enabled(user) or not getattr(user, 'telegram_id', None):
        return
    try:
        first = await _first_time(user)
    except Exception as error:
        logger.warning('BlackBird: не удалось проверить метку клавиатуры', error=str(error))
        first = True
    if first:
        await send_bb_keyboard(bot, chat_id, getattr(user, 'language', None) or 'ru')


async def reset_bot_menu_button(bot: Bot) -> None:
    """Старт бота с MENU_BUTTON_WEBAPP_ENABLED=false: убрать «Кабинет», который бот выставил раньше.

    Telegram помнит кнопку бота, пока её не сбросят; одно лишь выключение настройки её не убирает.
    Только при меню в переписке (admins, all): при off бот ведёт себя как Bedolaga.
    """
    if settings.MENU_BUTTON_WEBAPP_ENABLED or not chat_menu_rolled_out():
        return
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        logger.info('BlackBird: кнопка меню бота сброшена на обычную')
    except Exception as error:
        logger.warning('BlackBird: не удалось сбросить кнопку меню бота', error=str(error))


def label_exact_amount(keyboard: list[list[InlineKeyboardButton]], amount_kopeks: int, language: str) -> None:
    """Кнопки оплаты недостающей суммы: «⭐ Telegram Stars · ровно N ⭐» и «💳 Банковская карта (Tribute)».

    Хук в get_payment_methods_keyboard: сумма там известна только на экранах нехватки денег. Клавиатура
    не знает человека, поэтому подпись работает при admins и all для всех. N тем же расчётом, что у счёта
    Stars (settings.rubles_to_stars).
    """
    if amount_kopeks <= 0 or not chat_menu_rolled_out():
        return
    labels = {'tribute': bb_text(language, 'btn_pay_tribute')}
    try:
        stars = settings.rubles_to_stars(amount_kopeks / 100)
        labels['stars'] = bb_text(language, 'btn_pay_stars_exact').format(stars=stars)
    except ValueError as error:
        logger.warning('BlackBird: не удалось пересчитать сумму в звёзды', error=str(error))
    for row in keyboard:
        for index, button in enumerate(row):
            parts = (button.callback_data or '').split('|')
            if len(parts) == 3 and parts[0] == 'topup_amount' and parts[1] in labels:
                row[index] = button.model_copy(update={'text': labels[parts[1]]})
