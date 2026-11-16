"""Переключатель BB_CHAT_MENU: кому показывать меню в переписке."""

from app.config import settings


def chat_menu_enabled(user) -> bool:
    """off (и любое неизвестное значение) -> False; admins -> только админы; all -> все."""
    if user is None:
        return False
    mode = str(getattr(settings, 'BB_CHAT_MENU', 'off') or 'off').strip().lower()
    if mode == 'all':
        return True
    if mode == 'admins':
        return bool(settings.is_admin(user.telegram_id))
    return False
