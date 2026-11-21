"""Описание бота: текст, который Telegram показывает в пустом чате над своей кнопкой «Старт» (setMyDescription).

Пока человек не нажал эту кнопку, бот не может ему ничего прислать: ни меню, ни три кнопки над строкой ввода. В пустом
чате есть только описание и кнопка Telegram, поэтому описание подсказывает, что нажать. Подпись кнопки зависит от
языка приложения, в тексте она не названа.

Выставляется при запуске бота и только если отличается от текущего. При BB_CHAT_MENU=off бот описание не трогает
(ведёт себя как Bedolaga).
"""

import structlog
from aiogram import Bot

from app.bb.rollout import chat_menu_rolled_out
from app.bb.texts import bb_text


logger = structlog.get_logger(__name__)

# (language_code для Telegram, язык текста). '': описание по умолчанию, для всех языков без своего: по-русски.
DESCRIPTION_LANGUAGES = (('', 'ru'), ('en', 'en'))


async def configure_bb_description(bot: Bot) -> None:
    """Ошибки Telegram только в лог: старт бота из-за описания не падает."""
    if not chat_menu_rolled_out():
        return
    for language_code, language in DESCRIPTION_LANGUAGES:
        text = bb_text(language, 'bot_description')
        code = language_code or None
        try:
            current = await bot.get_my_description(language_code=code)
            previous = getattr(current, 'description', None) or ''
            if previous == text:
                continue
            await bot.set_my_description(description=text, language_code=code)
            logger.info(
                'BlackBird: описание бота обновлено', language_code=language_code or 'default', previous=previous
            )
        except Exception as error:
            logger.warning(
                'BlackBird: не удалось выставить описание бота',
                language_code=language_code or 'default',
                error=str(error),
            )
