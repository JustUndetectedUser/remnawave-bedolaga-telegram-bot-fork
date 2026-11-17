"""Вход в бот: нажатия двух постоянных кнопок над строкой ввода (сама клавиатура в app.bb.keyboard)."""

import structlog
from aiogram import Dispatcher, F, types
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from sqlalchemy.ext.asyncio import AsyncSession

from app.bb import connect as bb_connect, more as bb_more, renew as bb_renew, tariffs as bb_tariffs
from app.bb.rollout import chat_menu_enabled
from app.bb.texts import TEXTS, bb_text
from app.config import settings
from app.handlers.menu import build_main_menu_keyboard
from app.localization.texts import get_texts
from app.utils.rich_menu import try_answer_rich_main_menu


logger = structlog.get_logger(__name__)

BB_BTN_BOT = TEXTS['ru']['btn_bot']
BB_BTN_CABINET = TEXTS['ru']['btn_cabinet']
BB_BTN_BOT_EN = TEXTS['en']['btn_bot']
BB_BTN_CABINET_EN = TEXTS['en']['btn_cabinet']


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


def register_handlers(dp: Dispatcher) -> None:
    """Все хендлеры app.bb (bot.py зовёт только эту функцию)."""
    dp.message.register(on_bot_button, F.text.in_({BB_BTN_BOT, BB_BTN_BOT_EN}))
    dp.message.register(on_cabinet_button, F.text.in_({BB_BTN_CABINET, BB_BTN_CABINET_EN}))
    bb_tariffs.register_handlers(dp)
    bb_renew.register_handlers(dp)
    bb_connect.register_handlers(dp)
    bb_more.register_handlers(dp)
