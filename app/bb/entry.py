"""Вход в бот: нажатия трёх постоянных кнопок над строкой ввода (сама клавиатура в app.bb.keyboard)."""

import structlog
from aiogram import Dispatcher, F, types
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from sqlalchemy.ext.asyncio import AsyncSession

from app.bb import (
    browser_login as bb_browser_login,
    connect as bb_connect,
    main_menu as bb_menu,
    more as bb_more,
    renew as bb_renew,
    tariffs as bb_tariffs,
)
from app.bb.keyboard import maybe_send_bb_keyboard
from app.bb.rollout import chat_menu_enabled
from app.bb.texts import TEXTS, bb_text
from app.config import settings
from app.database.models import UserStatus
from app.handlers.menu import build_main_menu_keyboard
from app.localization.texts import get_texts
from app.utils.rich_menu import try_answer_rich_main_menu


logger = structlog.get_logger(__name__)

BB_BTN_BOT = TEXTS['ru']['btn_bot']
BB_BTN_WEB = TEXTS['ru']['btn_web']
BB_BTN_MINIAPP = TEXTS['ru']['btn_miniapp']
BB_BTN_BOT_EN = TEXTS['en']['btn_bot']
BB_BTN_WEB_EN = TEXTS['en']['btn_web']
BB_BTN_MINIAPP_EN = TEXTS['en']['btn_miniapp']
# Подписи клавиатуры из двух кнопок (до 21.11): у кого она ещё на экране, кнопки работают как раньше.
# «В веб-кабинет» тогда открывал Mini App
BB_LEGACY_BOT = {'Вызвать бота', 'Open bot menu'}
BB_LEGACY_MINIAPP = {'В веб-кабинет', 'To web account'}


async def _delete_incoming(message: types.Message) -> None:
    try:
        await message.delete()
    except Exception as error:
        logger.warning('BlackBird: не удалось удалить сообщение с кнопкой', error=str(error))


async def on_bot_button(message: types.Message, state: FSMContext, db: AsyncSession, db_user=None) -> None:
    if not chat_menu_enabled(db_user):
        raise SkipHandler
    await state.clear()
    await _delete_incoming(message)
    # Локальный импорт: start.py сам импортирует этот модуль (цикл).
    from app.handlers.start import answer_menu_with_media, get_main_menu_text

    # Путь как в cmd_start для Message: rich-меню, иначе текст/видео через answer_menu_with_media.
    texts = get_texts(db_user.language)
    keyboard = await build_main_menu_keyboard(db_user, db)
    if not await try_answer_rich_main_menu(message, db_user, texts, db, keyboard):
        menu_text = await get_main_menu_text(db_user, texts, db)
        await answer_menu_with_media(message, menu_text, keyboard, db)


async def on_cabinet_button(message: types.Message, db_user=None) -> None:
    if not chat_menu_enabled(db_user):
        raise SkipHandler
    await _delete_incoming(message)
    language = getattr(db_user, 'language', None)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=bb_text(language, 'cabinet_open'),
                    web_app=WebAppInfo(url=settings.MINIAPP_CUSTOM_URL),
                )
            ]
        ]
    )
    await message.answer(bb_text(language, 'cabinet_note'), reply_markup=markup)


async def on_web_button(message: types.Message, db_user=None) -> None:
    """«Веб-кабинет»: одноразовая ссылка на кабинет в браузере, сразу с входом (app.bb.browser_login).

    Ссылка новая на каждое нажатие. protect_content: сообщение с ней нельзя переслать.
    """
    if not chat_menu_enabled(db_user):
        raise SkipHandler
    await _delete_incoming(message)
    language = getattr(db_user, 'language', None)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=bb_text(language, 'web_open'),
                    url=bb_browser_login.browser_login_url(db_user.id),
                )
            ]
        ]
    )
    note = bb_text(language, 'web_note').format(minutes=bb_browser_login.TTL_SECONDS // 60)
    await message.answer(note, reply_markup=markup, protect_content=True)


async def on_any_message(message: types.Message, db_user=None, db: AsyncSession | None = None) -> bool:
    """Сообщение, которое бот не понял (хук в начале app.handlers.common.handle_unknown_message).

    Кто пишет боту вместо нажатия кнопок, скорее всего их не видит. Например, переписка началась не с /start: бот
    написал первым после входа в кабинет через Telegram, и кнопки «Старт» в чате нет. Поэтому вместо «Не понимаю эту
    команду» он получает то же, что на /start: три кнопки над строкой ввода (всегда, вернуть скрытые) и меню.
    Сообщение человека не удаляется: это не нажатие кнопки.

    False: меню переписки выключено, человек неизвестен или не активен, чат не личный, меню не ушло. Тогда отвечает
    Bedolaga.
    """
    if not chat_menu_enabled(db_user) or db is None or message.chat.type != 'private':
        return False
    if getattr(db_user, 'status', None) != UserStatus.ACTIVE.value:
        return False
    await maybe_send_bb_keyboard(message.bot, message.chat.id, db_user)
    return await bb_menu.bb_send_main_menu(message.bot, message.chat.id, db_user, db)


def register_handlers(dp: Dispatcher) -> None:
    """Все хендлеры app.bb (bot.py зовёт только эту функцию)."""
    dp.message.register(on_bot_button, F.text.in_({BB_BTN_BOT, BB_BTN_BOT_EN, *BB_LEGACY_BOT}))
    dp.message.register(on_web_button, F.text.in_({BB_BTN_WEB, BB_BTN_WEB_EN}))
    dp.message.register(on_cabinet_button, F.text.in_({BB_BTN_MINIAPP, BB_BTN_MINIAPP_EN, *BB_LEGACY_MINIAPP}))
    bb_tariffs.register_handlers(dp)
    bb_renew.register_handlers(dp)
    bb_connect.register_handlers(dp)
    bb_more.register_handlers(dp)
    bb_menu.register_handlers(dp)
