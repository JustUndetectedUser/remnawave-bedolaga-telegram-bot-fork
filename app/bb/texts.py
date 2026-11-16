"""Тексты BlackBird для бота. Язык: ru/en, по умолчанию ru."""

OFFER_URL = 'https://blackbirdnet.com/offer'
PRIVACY_URL = 'https://blackbirdnet.com/privacy'

TEXTS = {
    'ru': {
        'btn_bot': 'Вызвать бота',
        'btn_cabinet': 'В веб-кабинет',
        'keyboard_note': 'Кнопки «Вызвать бота» и «В веб-кабинет» всегда над строкой ввода.',
        'cabinet_note': 'Веб-кабинет откроется уже с входом в ваш аккаунт.',
        'cabinet_open': 'Открыть веб-кабинет',
        # Главное меню
        'btn_connect': '🔗 Подключить устройство',
        'btn_renew': '💳 Продлить или сменить тариф',
        'btn_topup': '💰 Пополнить баланс',
        'btn_more': 'Ещё',
        'btn_trial': '🎁 Попробовать Pro бесплатно',
        'btn_tariffs': 'Платные тарифы',
        'menu_status': 'Подписка {tariff} до {date} · Баланс {balance}',
        'menu_status_expired': 'Подписка {tariff} закончилась {date} · Баланс {balance}',
        'menu_status_inactive': 'Подписка {tariff} не активна · Баланс {balance}',
        'menu_status_unlimited': '{tariff} · бессрочно',
        'menu_no_tariff': 'VPN',
        # Первый визит
        'welcome_trial': (
            'Добро пожаловать в BlackBird. Попробуйте Pro бесплатно на {days}: до {devices}, работает даже '
            'при белых списках мобильного интернета, без карты и автосписаний.'
        ),
        'welcome_no_trial': 'Добро пожаловать в BlackBird. Выберите тариф, чтобы подключиться.',
        'trial_over': 'Пробный период закончился. Выберите тариф, чтобы продолжить.',
        'legal': (
            f'Продолжая, вы принимаете <a href="{OFFER_URL}">оферту</a> и '
            f'<a href="{PRIVACY_URL}">политику конфиденциальности</a>.'
        ),
        # Платные тарифы
        'tariffs_title': '<b>Платные тарифы</b>',
        'tariffs_empty': 'Сейчас нет тарифов для покупки.',
        'tariff_popular': 'самый популярный',
        'tariff_price': '{price} за {period}',
    },
    'en': {
        'btn_bot': 'Open bot menu',
        'btn_cabinet': 'To web account',
        'keyboard_note': 'The buttons are always above the message field.',
        'cabinet_note': 'The web account opens already signed in.',
        'cabinet_open': 'Open web account',
        # Main menu
        'btn_connect': '🔗 Connect a device',
        'btn_renew': '💳 Renew or change plan',
        'btn_topup': '💰 Top up balance',
        'btn_more': 'More',
        'btn_trial': '🎁 Try Pro for free',
        'btn_tariffs': 'Paid plans',
        'menu_status': '{tariff} subscription until {date} · Balance {balance}',
        'menu_status_expired': '{tariff} subscription ended on {date} · Balance {balance}',
        'menu_status_inactive': '{tariff} subscription is not active · Balance {balance}',
        'menu_status_unlimited': '{tariff} · no end date',
        'menu_no_tariff': 'VPN',
        # First visit
        'welcome_trial': (
            'Welcome to BlackBird. Try Pro for free for {days}: up to {devices}, works even when mobile '
            'internet is limited to whitelists, no card and no automatic charges.'
        ),
        'welcome_no_trial': 'Welcome to BlackBird. Choose a plan to get connected.',
        'trial_over': 'Your trial has ended. Choose a plan to continue.',
        'legal': (
            f'By continuing, you accept the <a href="{OFFER_URL}">terms of service</a> and the '
            f'<a href="{PRIVACY_URL}">privacy policy</a>.'
        ),
        # Paid plans
        'tariffs_title': '<b>Paid plans</b>',
        'tariffs_empty': 'There are no plans to buy right now.',
        'tariff_popular': 'most popular',
        'tariff_price': '{price} for {period}',
    },
}

# Формы слов по числу: ru (1, 2-4, 5+), en (1, много).
_PLURALS = {
    'ru': {
        'days': ('день', 'дня', 'дней'),
        'devices': ('устройство', 'устройства', 'устройств'),
        # после «до»: до 1 устройства, до 2 устройств, до 5 устройств
        'devices_upto': ('устройства', 'устройств', 'устройств'),
    },
    'en': {
        'days': ('day', 'days', 'days'),
        'devices': ('device', 'devices', 'devices'),
        'devices_upto': ('device', 'devices', 'devices'),
    },
}


def _lang(language: str | None) -> str:
    lang = (language or 'ru').split('-')[0].lower()
    return lang if lang in TEXTS else 'ru'


def bb_text(language: str | None, key: str) -> str:
    return TEXTS[_lang(language)][key]


def bb_count(language: str | None, number: int, word: str) -> str:
    """«30 дней», «2 устройства», «1 device»."""
    lang = _lang(language)
    one, few, many = _PLURALS[lang][word]
    n = abs(int(number)) % 100
    if lang != 'ru':
        form = one if abs(int(number)) == 1 else many
    elif 11 <= n <= 14:
        form = many
    elif n % 10 == 1:
        form = one
    elif 2 <= n % 10 <= 4:
        form = few
    else:
        form = many
    return f'{number} {form}'
