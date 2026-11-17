"""Экран «Подключить устройство» и установка Happ (меню в переписке).

Кнопка «📲 Открыть в Happ» ведёт на страницу кабинета BB_HAPP_REDIRECT_URL?u=<ссылка подписки>,
она открывает happ://add/<ссылка>. «Скопировать ссылку» кладёт ссылку в буфер (copy_text, до 256 символов).
Число подключённых устройств берём из панели так же, как управление устройствами Bedolaga, с коротким
таймаутом: экран не должен зависать из-за панели.
"""

import asyncio
import html
from urllib.parse import quote

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bb.main_menu import format_day, trial_numbers
from app.bb.renew import _show
from app.bb.texts import bb_count, bb_text
from app.config import settings
from app.utils.subscription_utils import get_display_subscription_link


logger = structlog.get_logger(__name__)

COPY_TEXT_LIMIT = 256
DEVICES_TIMEOUT = 3.0
_INSTALL_ROW = 3


def _button(text: str, **kwargs) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, **kwargs)


def happ_open_url(link: str) -> str | None:
    base = (settings.BB_HAPP_REDIRECT_URL or '').strip()
    if not base or not link:
        return None
    return f'{base}?u={quote(link, safe="")}'


def bb_connect_screen(
    user,
    subscription,
    language: str | None,
    *,
    intro: str | None = None,
    devices_used: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    language = language or 'ru'
    back = [_button(bb_text(language, 'btn_back'), callback_data='back_to_menu')]
    link = get_display_subscription_link(subscription)
    head = f'{intro}\n\n' if intro else ''
    if not link:
        return head + bb_text(language, 'connect_no_link'), InlineKeyboardMarkup(inline_keyboard=[back])

    lines = [bb_text(language, 'connect_text')]
    limit = getattr(subscription, 'device_limit', None)
    if devices_used is not None and limit:
        lines.append(bb_text(language, 'connect_devices').format(used=devices_used, limit=limit))
    text = head + '\n'.join(lines)

    rows: list[list[InlineKeyboardButton]] = []
    open_url = happ_open_url(link)
    if open_url:
        rows.append([_button(bb_text(language, 'btn_open_happ'), url=open_url)])
    if len(link) <= COPY_TEXT_LIMIT:
        rows.append([_button(bb_text(language, 'btn_copy_link'), copy_text=CopyTextButton(text=link))])
    else:
        text += f'\n\n{bb_text(language, "connect_link_text")}\n<code>{html.escape(link)}</code>'
    rows.append([_button(bb_text(language, 'btn_install_happ'), callback_data='bb_install')])
    rows.append(back)
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def fetch_devices_used(user, subscription) -> int | None:
    """Сколько устройств в списке HWID панели; при ошибке или таймауте None (строка не показывается)."""
    try:
        # Локально: модуль хендлеров подписки тянет за собой почти весь бот.
        from app.handlers.subscription.devices import _get_panel_user_id
        from app.services.remnawave_service import RemnaWaveService

        panel_user_id = _get_panel_user_id(subscription, user)
        if not panel_user_id:
            return None

        async def _fetch():
            async with RemnaWaveService().get_api_client() as api:
                return await api.get_user_devices(panel_user_id)

        info = await asyncio.wait_for(_fetch(), timeout=DEVICES_TIMEOUT)
        if isinstance(info, dict):
            total = info.get('total')
            if total is None:
                total = len(info.get('devices') or [])
            return int(total)
    except Exception as error:
        logger.warning('BlackBird: не удалось получить число устройств', error=repr(error))
    return None


async def trial_intro(db: AsyncSession, user, subscription) -> str | None:
    """«✅ Pro на 3 дня, до 19 ноября. Осталось подключить устройство.» для экрана после пробного."""
    end = getattr(subscription, 'end_date', None)
    if end is None:
        return None
    language = getattr(user, 'language', None) or 'ru'
    try:
        days, _devices = await trial_numbers(db)
        return bb_text(language, 'connect_trial_intro').format(
            days=bb_count(language, days, 'days'), date=format_day(end, language)
        )
    except Exception:
        # Шапка не должна ломать экран подключения после активации пробного.
        logger.exception('BlackBird: не удалось собрать шапку после пробного')
        return None


async def show_connect(callback: types.CallbackQuery, user, subscription, *, intro: str | None = None) -> None:
    language = getattr(user, 'language', None) or 'ru'
    used = await fetch_devices_used(user, subscription) if get_display_subscription_link(subscription) else None
    text, keyboard = bb_connect_screen(user, subscription, language, intro=intro, devices_used=used)
    await _show(callback, text, keyboard)
    await callback.answer()


def install_links() -> list[tuple[str, str]]:
    """(ключ подписи, ссылка) только для заданных ссылок."""
    candidates = [
        ('btn_ios', settings.get_happ_download_link('ios')),
        ('btn_android', settings.get_happ_download_link('android')),
        ('btn_windows', settings.get_happ_download_link('windows')),
        ('btn_macos', settings.get_happ_download_link('macos')),
        ('btn_tv', (settings.BB_HAPP_LINK_TV or '').strip()),
        ('btn_apk', (settings.BB_HAPP_LINK_APK or '').strip()),
    ]
    return [(key, link) for key, link in candidates if link]


def bb_install_screen(language: str | None) -> tuple[str, InlineKeyboardMarkup]:
    language = language or 'ru'
    buttons = [_button(bb_text(language, key), url=link) for key, link in install_links()]
    rows = [buttons[i : i + _INSTALL_ROW] for i in range(0, len(buttons), _INSTALL_ROW)]
    rows.append([_button(bb_text(language, 'btn_back'), callback_data='subscription_connect')])
    return bb_text(language, 'install_text'), InlineKeyboardMarkup(inline_keyboard=rows)


async def on_install(callback: types.CallbackQuery, db_user) -> None:
    text, keyboard = bb_install_screen(getattr(db_user, 'language', None))
    await _show(callback, text, keyboard)
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(on_install, F.data == 'bb_install')
