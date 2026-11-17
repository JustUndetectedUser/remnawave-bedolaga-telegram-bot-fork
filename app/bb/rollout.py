"""Переключатель BB_CHAT_MENU: кому показывать меню в переписке."""

from app.config import settings


def chat_menu_mode() -> str:
    """off, admins или all; любое неизвестное значение считается off."""
    mode = str(getattr(settings, 'BB_CHAT_MENU', 'off') or 'off').strip().lower()
    return mode if mode in {'admins', 'all'} else 'off'


def chat_menu_rolled_out() -> bool:
    """Меню в переписке включено хоть для кого-то (admins или all): для мест, где человек неизвестен."""
    return chat_menu_mode() != 'off'


def chat_menu_enabled(user) -> bool:
    """off (и любое неизвестное значение) -> False; admins -> только админы; all -> все."""
    if user is None:
        return False
    mode = chat_menu_mode()
    if mode == 'all':
        return True
    if mode == 'admins':
        return bool(settings.is_admin(user.telegram_id))
    return False
