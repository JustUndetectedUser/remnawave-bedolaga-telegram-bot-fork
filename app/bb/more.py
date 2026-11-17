"""Экран «Ещё»: устройства, автопродление, история, поддержка, вопросы и ответы.

Каждая кнопка ведёт на готовый экран Bedolaga по его callback_data, своей логики здесь нет. Устройства и
автопродление работают только с подпиской, поэтому у человека без подписки этих двух кнопок нет.
"""

from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bb.main_menu import bb_edit_main_menu
from app.bb.renew import _show
from app.bb.rollout import chat_menu_enabled
from app.bb.texts import bb_text


def more_message(user, language: str) -> tuple[str, InlineKeyboardMarkup]:
    buttons = []
    if getattr(user, 'subscription', None) is not None:
        buttons += [('btn_devices', 'subscription_manage_devices'), ('btn_autopay', 'subscription_autopay')]
    buttons += [
        ('btn_history', 'balance_history'),
        ('btn_support', 'menu_support'),
        ('btn_faq', 'menu_faq'),
        ('btn_back', 'back_to_menu'),
    ]
    rows = [[InlineKeyboardButton(text=bb_text(language, key), callback_data=data)] for key, data in buttons]
    return bb_text(language, 'more_title'), InlineKeyboardMarkup(inline_keyboard=rows)


async def on_more(callback: types.CallbackQuery, db_user, db: AsyncSession) -> None:
    if not chat_menu_enabled(db_user):
        await bb_edit_main_menu(callback, db_user, db)
        await callback.answer()
        return
    text, keyboard = more_message(db_user, getattr(db_user, 'language', None) or 'ru')
    await _show(callback, text, keyboard)
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(on_more, F.data == 'bb_more')
