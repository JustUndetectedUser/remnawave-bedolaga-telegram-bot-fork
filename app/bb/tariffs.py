"""«Платные тарифы»: новым сообщением, кнопки ведут в покупку Bedolaga (tariff_select:<id>)."""

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bb.main_menu import NO_PREVIEW, is_unlimited
from app.bb.texts import bb_count, bb_text
from app.database.crud.tariff import get_tariffs_for_user
from app.localization.texts import Texts


logger = structlog.get_logger(__name__)

STAR = '⭐ '


def paid_tariffs(tariffs) -> list:
    """Активные тарифы для покупки: без пробного и без Unlimited Pro, по display_order."""
    chosen = [t for t in tariffs if not getattr(t, 'is_trial_available', False) and not is_unlimited(t)]
    return sorted(chosen, key=lambda t: (getattr(t, 'display_order', 0) or 0, t.id))


def _shortest_price(tariff) -> tuple[int, int] | None:
    prices = []
    for period, price in (getattr(tariff, 'period_prices', None) or {}).items():
        try:
            if int(price) > 0:
                prices.append((int(period), int(price)))
        except (TypeError, ValueError):
            continue
    return min(prices) if prices else None


def tariffs_message(tariffs, language: str) -> tuple[str, InlineKeyboardMarkup | None]:
    lines = [bb_text(language, 'tariffs_title')]
    buttons = []
    for tariff in tariffs:
        highlighted = bool(getattr(tariff, 'is_highlighted', False))
        name = html.escape(tariff.name)
        title = f'{STAR if highlighted else ""}<b>{name}</b>'
        if highlighted:
            title += f' · {bb_text(language, "tariff_popular")}'
        details = [bb_count(language, tariff.device_limit, 'devices')]
        price = _shortest_price(tariff)
        if price:
            period, kopeks = price
            details.append(
                bb_text(language, 'tariff_price').format(
                    price=Texts.format_price(kopeks), period=bb_count(language, period, 'days')
                )
            )
        block = [title, ' · '.join(details)]
        if getattr(tariff, 'description', None):
            block.append(html.escape(tariff.description))
        lines.append('\n'.join(block))
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{STAR if highlighted else ""}{tariff.name}', callback_data=f'tariff_select:{tariff.id}'
                )
            ]
        )
    if not buttons:
        lines.append(bb_text(language, 'tariffs_empty'))
        return '\n\n'.join(lines), None
    return '\n\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def on_tariffs(callback: types.CallbackQuery, db_user, db: AsyncSession) -> None:
    language = getattr(db_user, 'language', None) or 'ru'
    tariffs = paid_tariffs(await get_tariffs_for_user(db, getattr(db_user, 'promo_group_id', None)))
    text, keyboard = tariffs_message(tariffs, language)
    # Новое сообщение: приветствие или меню остаётся выше.
    await callback.bot.send_message(
        callback.message.chat.id, text, reply_markup=keyboard, parse_mode='HTML', link_preview_options=NO_PREVIEW
    )
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(on_tariffs, F.data == 'bb_tariffs')
