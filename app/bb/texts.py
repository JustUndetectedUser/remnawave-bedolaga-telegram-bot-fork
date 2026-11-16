"""Тексты BlackBird для бота. Язык: ru/en, по умолчанию ru."""

TEXTS = {
    'ru': {
        'btn_bot': 'Вызвать бота',
        'btn_cabinet': 'В веб-кабинет',
        'keyboard_note': 'Кнопки «Вызвать бота» и «В веб-кабинет» всегда над строкой ввода.',
        'cabinet_note': 'Веб-кабинет откроется уже с входом в ваш аккаунт.',
        'cabinet_open': 'Открыть веб-кабинет',
    },
    'en': {
        'btn_bot': 'Open bot menu',
        'btn_cabinet': 'To web account',
        'keyboard_note': 'The buttons are always above the message field.',
        'cabinet_note': 'The web account opens already signed in.',
        'cabinet_open': 'Open web account',
    },
}


def bb_text(language: str | None, key: str) -> str:
    lang = (language or 'ru').split('-')[0].lower()
    return TEXTS.get(lang, TEXTS['ru'])[key]
