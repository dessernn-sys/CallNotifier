import logging
import os
import sys
from logging.handlers import RotatingFileHandler


# ============================================================================
# Определение, кто мы: клиент или сервер
# ============================================================================

# Явные имена, по которым узнаём процесс.
# Важно: у клиента имя .exe — "CallNotifierClient.exe", поэтому добавляем
# именно его, а не абстрактный "client.exe".
_CLIENT_SCRIPTS = ("client.py",)
_CLIENT_EXES = ("callnotifierclient.exe", "client.exe")

_SERVER_SCRIPTS = ("server.py",)
_SERVER_EXES = ("callnotifierserver.exe", "server.exe")


def _argv0_basename() -> str:
    """Имя запущенного файла (без пути), в нижнем регистре. Пустая строка, если недоступно."""
    try:
        return os.path.basename(sys.argv[0] or "").lower()
    except Exception:
        return ""


def _is_client_process() -> bool:
    """True, если запущен клиент — скриптом или как .exe."""
    name = _argv0_basename()
    if name in _CLIENT_SCRIPTS or name in _CLIENT_EXES:
        return True
    return False


def _is_server_process() -> bool:
    """True, если запущен сервер — скриптом или как .exe."""
    name = _argv0_basename()
    if name in _SERVER_SCRIPTS or name in _SERVER_EXES:
        return True
    return False


def _app_dir() -> str:
    """Папка рядом с программой (рядом с .exe или со скриптом)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _user_data_dir_client() -> str:
    """%APPDATA%\\CallNotifier для клиента в .exe, папка проекта при разработке."""
    if getattr(sys, 'frozen', False):
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        path = os.path.join(base, 'CallNotifier')
        os.makedirs(path, exist_ok=True)
        return path
    return os.path.dirname(os.path.abspath(__file__))


def _log_path() -> str:
    """Возвращает путь к файлу лога в зависимости от того, кто мы."""
    is_frozen = getattr(sys, 'frozen', False)

    # --- Сервер ---
    if _is_server_process():
        # Сервер всегда пишет рядом с собой
        return os.path.join(_app_dir(), "notifier_server.log")

    # --- Клиент ---
    if _is_client_process():
        return os.path.join(_user_data_dir_client(), "notifier_client.log")

    # --- Замороженное приложение, но имя .exe нам незнакомо ---
    # На всякий случай уводим логи в %APPDATA%, чтобы точно не потерять.
    if is_frozen:
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        base = os.path.join(base, 'CallNotifier')
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "notifier.log")

    # --- Не заморожены и не опознали по имени (тесты, скрипты) ---
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "notifier.log")


LOG_FILE = _log_path()


def setup_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # Защита от повторного добавления хендлеров
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger