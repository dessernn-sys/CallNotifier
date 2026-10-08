import json
import os
import shutil
import sys
from logger import setup_logger

# ============================================================================
# Версия приложения
# ============================================================================

__version__ = "1.1.0"

logger = setup_logger('config')

# ============================================================================
# Определение путей
# ============================================================================

def _is_frozen() -> bool:
    """True, если запущено из PyInstaller .exe."""
    return getattr(sys, 'frozen', False)


def _app_dir() -> str:
    """Папка, где лежит код (при разработке) или .exe (при сборке).

    - Разработка: папка, где находится этот файл (config.py).
    - PyInstaller onefile: папка, где лежит .exe (sys.executable).
    - PyInstaller onedir: sys.executable тоже указывает на .exe.
    """
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _user_data_dir() -> str:
    """Папка для пользовательских данных клиента (логи, кэш).

    - Разработка: рядом с config.py (как раньше).
    - .exe: %APPDATA%\\CallNotifier\\
    """
    if _is_frozen():
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        path = os.path.join(base, 'CallNotifier')
        os.makedirs(path, exist_ok=True)
        return path
    return os.path.dirname(os.path.abspath(__file__))

def _meipass_dir() -> str | None:
    """Временная папка, куда PyInstaller распаковывает --add-data. None, если не .exe."""
    return getattr(sys, '_MEIPASS', None)


def _ensure_client_config_present():
    """При первом запуске .exe рядом с ним может не быть config_client.json.
    Пытаемся скопировать дефолт из _MEIPASS. Если и там нет — ClientConfig создаст дефолт."""
    if not _is_frozen():
        return
    target = os.path.join(_app_dir(), "config_client.json")
    if os.path.exists(target):
        return
    meipass = _meipass_dir()
    if not meipass:
        return
    bundled = os.path.join(meipass, "config_client.json")
    if os.path.exists(bundled):
        try:
            import shutil as _shutil
            _shutil.copyfile(bundled, target)
        except Exception:
            pass

# Папка, где лежат пользовательские данные (клиентская сторона)
USER_DATA_DIR = _user_data_dir()

# Пути к конфигам
#   - Клиентский и серверный конфиг читаются из папки приложения (_app_dir).
#     При .exe это папка рядом с .exe, чтобы пользователь мог их редактировать.
#   - config_admin.json тоже лежит в папке приложения (только для сервера).
APP_DIR = _app_dir()
CLIENT_CONFIG_PATH = os.path.join(APP_DIR, "config_client.json")
SERVER_CONFIG_PATH = os.path.join(APP_DIR, "config_server.json")
ADMIN_CONFIG_PATH  = os.path.join(APP_DIR, "config_admin.json")
LEGACY_CONFIG_PATH = os.path.join(APP_DIR, "config.json")

# ============================================================================
# Дефолтные шаблоны
# ============================================================================

DEFAULT_CLIENT_CONFIG = {
    "http_server": {
        "host": "192.168.51.176",
        "port": 5499
    },
    "display": {
        "max_calls_on_screen": 15,
        "font_scale_mode": "auto",
        "green_threshold_minutes": 5,
        "orange_threshold_minutes": 15,
        "color_green": "#2e7d32",
        "color_orange": "#f57c00",
        "color_red": "#c62828",
        "color_bright_green": "#00c853",
        "color_background": "#1e1e1e",
        "color_text": "#ffffff",
        "logo_path": "",
        "cache_ttl_days": 3,
        "allowed_types": []
    }
}

DEFAULT_SERVER_CONFIG = {
    "http_server": {
        "host": "0.0.0.0",
        "port": 5499
    },
    "display": {
        "cache_ttl_days": 3
    },
    "warehouse": {
        "enabled": True,
        "warehouses": []
    }
}

DEFAULT_ADMIN_CONFIG = {
    "admin_password": "admin",
    "dispatcher_password": "dispatcher"
}

# ============================================================================
# Базовый менеджер конфига
# ============================================================================

class _BaseConfig:
    """Общая логика загрузки/сохранения для клиентского и серверного конфигов."""

    #: путь к файлу конфига — переопределяется в наследниках
    path: str = ""
    #: дефолтный шаблон — переопределяется в наследниках
    defaults: dict = {}

    def __init__(self):
        # Глубокая копия через JSON, чтобы не мутировать DEFAULT_*
        import copy
        self.data = copy.deepcopy(self.defaults)
        self.load()

    # ---------- Файловые операции ----------

    def load(self):
        if not os.path.exists(self.path):
            logger.warning(f"{os.path.basename(self.path)} не найден, создаю с дефолтными значениями")
            self.save()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self._update_recursive(self.data, loaded)
        except Exception as e:
            logger.error(f"Ошибка загрузки {os.path.basename(self.path)}: {e}, используются дефолты")

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Ошибка сохранения {os.path.basename(self.path)}: {e}")

    @staticmethod
    def _update_recursive(d: dict, u: dict):
        """Рекурсивный deep-merge: любые ключи из файла попадают в data,
        включая те, которых нет в дефолтном шаблоне."""
        for k, v in u.items():
            if k in d and isinstance(d[k], dict) and isinstance(v, dict):
                _BaseConfig._update_recursive(d[k], v)
            else:
                d[k] = v

    # ---------- Доступ ----------

    def get(self, *keys, default=None):
        result = self.data
        for k in keys:
            if isinstance(result, dict) and k in result:
                result = result[k]
            else:
                return default
        return result

    def set(self, value, *keys):
        d = self.data
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value
        self.save()


class ClientConfig(_BaseConfig):
    path = CLIENT_CONFIG_PATH
    defaults = DEFAULT_CLIENT_CONFIG


class ServerConfig(_BaseConfig):
    path = SERVER_CONFIG_PATH
    defaults = DEFAULT_SERVER_CONFIG


class AdminConfig(_BaseConfig):
    """Отдельный конфиг админских паролей. Плоский, без вложенности."""
    path = ADMIN_CONFIG_PATH
    defaults = DEFAULT_ADMIN_CONFIG

    def get_admin_password(self) -> str:
        return str(self.data.get("admin_password", "admin"))

    def get_dispatcher_password(self) -> str:
        return str(self.data.get("dispatcher_password", "dispatcher"))


# ============================================================================
# Миграция со старого единого config.json
# ============================================================================

def migrate_config_if_needed():
    """Разовая миграция: config.json → config_client.json + config_server.json.

    Срабатывает только если:
      * существует старый config.json,
      * отсутствует хотя бы один из новых конфигов.

    После успешной миграции старый config.json переименовывается в config.json.bak.
    """
    legacy_exists = os.path.exists(LEGACY_CONFIG_PATH)
    client_exists = os.path.exists(CLIENT_CONFIG_PATH)
    server_exists = os.path.exists(SERVER_CONFIG_PATH)

    if not legacy_exists:
        return  # миграция не нужна

    if client_exists and server_exists:
        logger.info("Оба новых конфига уже существуют, миграция не требуется")
        return

    logger.info("Обнаружен старый config.json, запускаю миграцию...")
    try:
        with open(LEGACY_CONFIG_PATH, "r", encoding="utf-8") as f:
            legacy = json.load(f)
    except Exception as e:
        logger.error(f"Не удалось прочитать legacy config.json: {e}")
        return

    # ---------- Собираем клиентский конфиг ----------
    client_data = {
        "http_server": legacy.get("http_server", DEFAULT_CLIENT_CONFIG["http_server"]),
        "audio":       legacy.get("audio",       DEFAULT_CLIENT_CONFIG["audio"]),
        "display":     {},
    }
    legacy_display = legacy.get("display", {})
    for k, v in DEFAULT_CLIENT_CONFIG["display"].items():
        client_data["display"][k] = legacy_display.get(k, v)

    # ---------- Собираем серверный конфиг ----------
    server_data = {
        "http_server": {
            # host для сервера — это «на чём слушать», а не «куда стучаться».
            # Если в старом конфиге был конкретный IP (например 192.168.x.x) —
            # сохраняем его, чтобы сервер слушал именно на этом интерфейсе.
            "host": legacy.get("http_server", {}).get("host", DEFAULT_SERVER_CONFIG["http_server"]["host"]),
            "port": legacy.get("http_server", {}).get("port", DEFAULT_SERVER_CONFIG["http_server"]["port"]),
        },
        "display": {
            "cache_ttl_days": legacy.get("display", {}).get("cache_ttl_days", 3),
        },
        "warehouse": legacy.get("warehouse", DEFAULT_SERVER_CONFIG["warehouse"]),
    }

    # ---------- Пишем новые файлы ----------
    try:
        if not client_exists:
            with open(CLIENT_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(client_data, f, indent=2, ensure_ascii=False)
            logger.info(f"Создан {os.path.basename(CLIENT_CONFIG_PATH)}")
        if not server_exists:
            with open(SERVER_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(server_data, f, indent=2, ensure_ascii=False)
            logger.info(f"Создан {os.path.basename(SERVER_CONFIG_PATH)}")

        # ---------- Убираем legacy ----------
        backup_path = LEGACY_CONFIG_PATH + ".bak"
        # Если .bak уже есть — перезапишем (шумно, но безопасно)
        if os.path.exists(backup_path):
            os.remove(backup_path)
        shutil.move(LEGACY_CONFIG_PATH, backup_path)
        logger.info(f"Старый config.json переименован в {os.path.basename(backup_path)}")

    except Exception as e:
        logger.error(f"Ошибка в процессе миграции конфигов: {e}")


# ============================================================================
# Совместимость со старым кодом (можно будет удалить отдельной итерацией)
# ============================================================================

# Псевдоним, чтобы не ломать импорты «from config import ConfigManager»
# в случайных местах. Возвращает клиентский конфиг — он в 90% случаев
# используется именно клиентом.
ConfigManager = ClientConfig