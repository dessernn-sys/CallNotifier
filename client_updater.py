"""
Модуль автообновления клиента CallNotifier.

Логика:
  1. При старте и раз в N часов — запрашивает /api/v1/version.
  2. Если серверная версия строго больше текущей — предлагает обновиться.
     В headless-режиме — обновляется молча.
  3. Скачивает новый .exe во временную папку.
  4. Проверяет sha256 (до N попыток перескачивания).
  5. Генерирует batch-скрипт, который:
     - ждёт завершения текущего клиента,
     - делает резервную копию старого .exe,
     - копирует новый .exe поверх,
     - перезапускает клиент,
     - удаляет себя.
  6. Запускает batch и завершает текущий процесс.
  7. При сбое копирования — batch восстанавливает старый .exe.

Всё логирование — через logger.

ВАЖНО: модуль НЕ работает, если приложение запущено из-под Python
(`sys.frozen == False`). Обновление имеет смысл только для .exe.

ВАЖНО (v2, после фикса "Failed to load Python DLL"):
  PyInstaller в onefile-режиме читает переменные `_MEIPASS2` / `_MEIPASS`
  из окружения процесса. Если их унаследует дочерний .exe — он решит,
  что распаковываться не надо, и попытается загрузить `python311.dll`
  из УЖЕ УДАЛЁННОЙ папки `%TEMP%\_MEIxxxxxx`. Результат:
      "Failed to load Python DLL".
  Поэтому:
    * updater перед `Popen(batch)` передаёт ЯВНО ОЧИЩЕННОЕ env;
    * сам batch НЕ использует `setlocal` (иначе очистка не долетит
      до дочернего процесса);
    * запуск нового .exe идёт через `cmd /c "set _MEIPASS2= && ... && start"`.
"""

import os
import sys
import time
import hashlib
import subprocess
import threading
import requests

from logger import setup_logger

logger = setup_logger('updater')


class _InvalidResponse(Exception):
    pass


class ClientUpdater:
    """Менеджер автообновления клиента.

    Атрибуты:
        config — ClientConfig, читается update-секция.
        server_url — базовый URL сервера (http://host:port).
        current_version — текущая версия клиента (из config.__version__).
        version_callback — callback для GUI: (latest_version, download_url) -> None.
            Вызывается при обнаружении новой версии, ДО начала скачивания.
            Может вернуть True (разрешить обновление) или False (отложить).
        state_callback — callback для GUI: (state, payload) -> None.
            state: 'downloading', 'ready_to_restart', 'error'.
    """

    def __init__(self, config, server_url: str, current_version: str,
                 version_callback=None, state_callback=None):
        self.config = config
        self.server_url = server_url.rstrip('/')
        self.current_version = current_version
        self.version_callback = version_callback
        self.state_callback = state_callback

        # Путь к текущему .exe (или None, если работаем из-под Python)
        self.exe_path = self._resolve_exe_path()
        self.is_frozen = self.exe_path is not None

        # Папка для временных файлов обновления
        from config import USER_DATA_DIR
        self.update_dir = os.path.join(USER_DATA_DIR, "update")
        os.makedirs(self.update_dir, exist_ok=True)

        self._stop_event = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------------

    def start(self):
        """Запускает фоновый поток проверки обновлений."""
        if not self.is_frozen:
            logger.info("Updater: приложение не .exe, автообновление отключено")
            return
        if not self._is_enabled():
            logger.info("Updater: автообновление отключено в конфиге")
            return
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ClientUpdater")
        self._thread.start()
        logger.info("Updater: фоновый поток автообновления запущен")

    def stop(self):
        """Останавливает фоновый поток."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        logger.info("Updater: остановлен")

    def check_now(self):
        """Ручной запуск проверки (например, из F1). Возвращает None или dict с версией."""
        return self._check_and_maybe_update(manual=True)

    # ------------------------------------------------------------------------
    # Внутренняя логика
    # ------------------------------------------------------------------------

    def _resolve_exe_path(self):
        """Возвращает путь к текущему .exe, если запущено через PyInstaller.
        Иначе — None.
        """
        if not getattr(sys, 'frozen', False):
            return None
        exe = os.path.abspath(sys.executable)
        # Иногда sys.executable в PyInstaller — это путь к Python, а не к .exe.
        # Но при --onefile с sys.frozen=True это именно наш .exe.
        if not exe.lower().endswith(".exe"):
            return None
        return exe

    def _is_enabled(self) -> bool:
        try:
            return bool(self.config.get("update", "enabled", default=True))
        except Exception:
            return True

    def _is_headless(self) -> bool:
        try:
            return bool(self.config.get("update", "headless_mode", default=False))
        except Exception:
            return False

    def _check_interval_seconds(self) -> int:
        try:
            hours = int(self.config.get("update", "check_interval_hours", default=6))
        except Exception:
            hours = 6
        hours = max(1, hours)
        return hours * 3600

    def _download_retries(self) -> int:
        try:
            n = int(self.config.get("update", "download_retries", default=3))
        except Exception:
            n = 3
        return max(1, min(n, 10))

    def _loop(self):
        """Основной цикл: проверка при старте, потом раз в N часов."""
        # Проверка при старте с небольшой задержкой, чтобы GUI успел подняться
        self._stop_event.wait(5)

        while not self._stop_event.is_set():
            try:
                self._check_and_maybe_update(manual=False)
            except Exception as e:
                logger.error(f"Updater: непредвиденная ошибка: {e}", exc_info=True)

            # Ждём N часов
            interval = self._check_interval_seconds()
            self._stop_event.wait(interval)

    def _check_and_maybe_update(self, manual=False):
        """Проверяет версию и, если нужно, обновляется. Возвращает dict или None."""
        try:
            resp = requests.get(f"{self.server_url}/api/v1/version", timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"Updater: не удалось получить версию сервера: {e}")
            return None

        if not isinstance(data, dict):
            logger.warning("Updater: невалидный ответ /api/v1/version")
            return None

        latest = str(data.get("latest", "")).strip()
        url = str(data.get("url", "")).strip()
        expected_sha = str(data.get("sha256", "")).strip()

        if not latest or not url:
            logger.info("Updater: сервер не отдаёт информации о релизе")
            return None

        if self._compare_versions(latest, self.current_version) <= 0:
            logger.info(f"Updater: версия актуальна ({self.current_version})")
            return None

        logger.info(f"Updater: доступна новая версия {latest} (текущая {self.current_version})")

        # Готовим полный URL
        if url.startswith("http://") or url.startswith("https://"):
            full_url = url
        else:
            full_url = self.server_url + "/" + url.lstrip("/")

        # Спрашиваем GUI / headless-логику
        allow = self._request_permission(latest, full_url)
        if not allow:
            logger.info("Updater: обновление отложено пользователем")
            return None

        # Скачиваем
        new_exe_path = os.path.join(self.update_dir, f"CallNotifierClient-{latest}.new.exe")
        if not self._download_with_retry(full_url, new_exe_path, expected_sha):
            logger.error("Updater: не удалось скачать новый .exe")
            self._emit_state("error", {"message": "Не удалось скачать обновление"})
            return None

        # Готовим batch и запускаем
        batch_path = self._write_batch(self.exe_path, new_exe_path)
        if not batch_path:
            logger.error("Updater: не удалось подготовить batch-скрипт")
            self._emit_state("error", {"message": "Не удалось подготовить обновление"})
            return None

        # --- Формируем АБСОЛЮТНО чистое окружение для batch --------------------
        # Это критично. Если updater сам запущен из .exe, в os.environ могут
        # болтаться _MEIPASS2 / _MEIPASS. Если их унаследует batch, а затем
        # и новый .exe — PyInstaller решит, что распаковываться не надо,
        # и попытается загрузить python311.dll из УЖЕ УДАЛЁННОЙ папки.
        env = os.environ.copy()
        for var in (
            "_MEIPASS2",
            "_MEIPASS",
            "_PYI_APPLICATION_HOME_DIR",
            "_PYI_ARCHIVE_FILE",
            "_PYI_PARENT_PROCESS_LEVEL",
        ):
            env.pop(var, None)

        # Явно задаём чистый TEMP/TMP (в %LOCALAPPDATA%\Temp)
        local_temp = os.path.join(
            os.environ.get("LOCALAPPDATA")
            or os.path.join(os.path.expanduser("~"), "AppData", "Local"),
            "Temp",
        )
        if os.path.isdir(local_temp):
            env["TEMP"] = local_temp
            env["TMP"] = local_temp

        logger.info(
            "Updater: env для batch подготовлен "
            f"(_MEIPASS2={env.get('_MEIPASS2', '<unset>')}, "
            f"_MEIPASS={env.get('_MEIPASS', '<unset>')}, "
            f"TEMP={env.get('TEMP')})"
        )
        logger.info(f"Updater: запуск обновления через {batch_path}")

        self._emit_state("ready_to_restart", {"version": latest, "batch_path": batch_path})

        try:
            # Запускаем batch как отдельный процесс.
            # CREATE_NO_WINDOW — скрыть окно cmd от пользователя.
            # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP — чтобы batch не умер вместе с нами.
            # env=env — ГЛАВНОЕ: batch получает чистое окружение без _MEIPASS2.
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                ["cmd.exe", "/c", batch_path],
                creationflags=(
                    subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                    | CREATE_NO_WINDOW
                ),
                close_fds=True,
                cwd=self.update_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except Exception as e:
            logger.error(f"Updater: ошибка запуска batch: {e}")
            self._emit_state("error", {"message": f"Не удалось запустить обновление: {e}"})
            return None

        # Дать batch время на старт и освободить файл .exe
        time.sleep(1.0)

        # Завершаем текущий процесс — batch дальше сам справится.
        # ВАЖНО: os._exit, чтобы не запускать atexit-обработчики Tkinter.
        logger.info("Updater: завершение текущего процесса для применения обновления")
        os._exit(0)

    def _compare_versions(self, v1: str, v2: str) -> int:
        """Сравнивает две версии. Возвращает -1, 0, 1."""
        def _key(v):
            try:
                return tuple(int(p) for p in v.split('.'))
            except Exception:
                return (0,)
        k1, k2 = _key(v1), _key(v2)
        if k1 < k2:
            return -1
        if k1 > k2:
            return 1
        return 0

    def _request_permission(self, latest: str, download_url: str) -> bool:
        """Спрашивает пользователя через GUI (если есть callback) или headless-правило."""
        if self._is_headless():
            logger.info("Updater: headless-режим — обновление разрешено автоматически")
            return True

        if self.version_callback is None:
            # Callback не задан — не обновляемся без явного разрешения
            logger.info("Updater: version_callback не задан, обновление отменено")
            return False

        try:
            return bool(self.version_callback(latest, download_url))
        except Exception as e:
            logger.error(f"Updater: ошибка в version_callback: {e}")
            return False

    def _download_with_retry(self, url: str, target_path: str, expected_sha: str) -> bool:
        """Скачивает .exe с проверкой sha256. До N попыток."""
        retries = self._download_retries()

        for attempt in range(1, retries + 1):
            logger.info(f"Updater: попытка {attempt}/{retries} скачать {url}")
            if self._download_one(url, target_path, expected_sha):
                return True
            # При неудаче — удаляем файл, если он остался
            if os.path.exists(target_path):
                try:
                    os.remove(target_path)
                except OSError:
                    pass

        return False

    def _download_one(self, url: str, target_path: str, expected_sha: str) -> bool:
        """Одна попытка скачивания. Возвращает True при успехе и совпадении sha256."""
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0) or 0)
                downloaded = 0
                self._emit_state("downloading", {"total": total, "downloaded": 0})

                with open(target_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        self._emit_state("downloading", {"total": total, "downloaded": downloaded})

                    # Гарантируем запись на диск до закрытия
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                    except OSError:
                        pass

            # Проверяем sha256
            actual_sha = self._sha256(target_path)
            if expected_sha and actual_sha.lower() != expected_sha.lower():
                logger.warning(
                    f"Updater: sha256 не совпал (ожидался {expected_sha[:12]}..., "
                    f"получен {actual_sha[:12]}...)"
                )
                return False

            logger.info(f"Updater: файл скачан, sha256 = {actual_sha[:12]}...")
            return True
        except Exception as e:
            logger.warning(f"Updater: ошибка скачивания: {e}")
            return False

    def _write_batch(self, old_exe: str, new_exe: str) -> str:
        """Создаёт batch-скрипт для замены .exe и перезапуска.

        КРИТИЧНО: НЕ используем `setlocal`. Он изолирует переменные окружения
        и ломает очистку `_MEIPASS2`/`_MEIPASS` для дочернего процесса,
        запускаемого через `start`. Без `setlocal` наследование работает
        предсказуемо, а очистка долетает до нового .exe.

        Дополнительно: запуск нового .exe делаем через
        `cmd /c "set _MEIPASS2= && set _MEIPASS= && start ..."` — это
        двойная страховка на случай, если грязное окружение всё же
        просочилось в текущую cmd-сессию.
        """
        batch_path = os.path.join(self.update_dir, "apply_update.bat")
        old_backup = old_exe + ".old"
        log_file = os.path.join(self.update_dir, "update.log")

        content = f"""@echo off
chcp 65001 >nul
REM =====================================================================
REM  CallNotifier updater (v2)
REM
REM  Ключевые моменты:
REM    * НЕТ setlocal — иначе очистка _MEIPASS2/_MEIPASS не долетит
REM      до дочернего процесса, запускаемого через `start`.
REM    * Запуск нового .exe — через `cmd /c "set ... && start ..."`,
REM      чтобы гарантированно получить чистое окружение.
REM    * Проверка размера после копирования — ловит недописанный файл
REM      (антивирус держит хэндл).
REM =====================================================================

REM --- 1. Сбрасываем PyInstaller-переменные ---
set "_MEIPASS2="
set "_MEIPASS="
set "_PYI_APPLICATION_HOME_DIR="
set "_PYI_ARCHIVE_FILE="
set "_PYI_PARENT_PROCESS_LEVEL="

REM --- 2. Чистый TEMP/TMP ---
set "TEMP=%USERPROFILE%\\AppData\\Local\\Temp"
set "TMP=%USERPROFILE%\\AppData\\Local\\Temp"

set "OLD_EXE={old_exe}"
set "NEW_EXE={new_exe}"
set "OLD_BAK={old_backup}"
set "LOG={log_file}"

REM --- 3. Диагностика на входе ---
echo [%date% %time%] Update started >> "%LOG%"
echo   OLD_EXE=%OLD_EXE% >> "%LOG%"
echo   NEW_EXE=%NEW_EXE% >> "%LOG%"
echo   TEMP=%TEMP% >> "%LOG%"
echo   _MEIPASS2=[%_MEIPASS2%] >> "%LOG%"
echo   _MEIPASS=[%_MEIPASS%] >> "%LOG%"

REM --- 4. Ждём завершения старого процесса ---
echo [%date% %time%] Waiting 15 sec for old process cleanup... >> "%LOG%"
ping -n 16 127.0.0.1 >nul
echo [%date% %time%] Waited 15 seconds >> "%LOG%"

REM --- 5. Бэкап ---
if exist "%OLD_EXE%" (
    echo [%date% %time%] Backing up old exe... >> "%LOG%"
    copy /Y "%OLD_EXE%" "%OLD_BAK%" >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo [%date% %time%] ERROR: cannot backup old exe >> "%LOG%"
        goto :fail_backup
    )
) else (
    echo [%date% %time%] WARNING: OLD_EXE does not exist, skip backup >> "%LOG%"
)

REM --- 6. Замена ---
echo [%date% %time%] Replacing exe... >> "%LOG%"
copy /Y "%NEW_EXE%" "%OLD_EXE%" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] ERROR: cannot replace exe >> "%LOG%"
    goto :fail_replace
)

REM --- 7. Пауза, чтобы Defender отпустил хэндл ---
echo [%date% %time%] Waiting 5 sec for AV to release handle... >> "%LOG%"
ping -n 6 127.0.0.1 >nul

REM --- 8. Проверка размера ---
for %%A in ("%OLD_EXE%") do set "SIZE_OLD=%%~zA"
for %%A in ("%NEW_EXE%") do set "SIZE_NEW=%%~zA"
echo   SIZE_OLD=%SIZE_OLD% SIZE_NEW=%SIZE_NEW% >> "%LOG%"
if not "%SIZE_OLD%"=="%SIZE_NEW%" (
    echo [%date% %time%] ERROR: size mismatch after copy >> "%LOG%"
    goto :fail_replace
)

REM --- 9. Запуск НОВОГО exe в чистом окружении ---
echo [%date% %time%] Launching new exe in clean env... >> "%LOG%"
cmd /c "set _MEIPASS2= && set _MEIPASS= && start "" /D "%CD%" "%OLD_EXE%""

REM --- 10. Успех: чистка и выход ---
echo [%date% %time%] Success. Cleanup... >> "%LOG%"
ping -n 4 127.0.0.1 >nul
if exist "%OLD_BAK%" del "%OLD_BAK%" >nul 2>&1
if exist "%NEW_EXE%" del "%NEW_EXE%" >nul 2>&1
goto :cleanup

:fail_backup
echo [%date% %time%] Update aborted: cannot backup old exe >> "%LOG%"
cmd /c "start "" /D "%CD%" "%OLD_EXE%""
goto :cleanup

:fail_replace
echo [%date% %time%] Restoring old exe from backup... >> "%LOG%"
if exist "%OLD_BAK%" (
    copy /Y "%OLD_BAK%" "%OLD_EXE%" >> "%LOG%" 2>&1
    del "%OLD_BAK%" >nul 2>&1
)
echo [%date% %time%] Launching restored exe... >> "%LOG%"
cmd /c "set _MEIPASS2= && set _MEIPASS= && start "" /D "%CD%" "%OLD_EXE%""

:cleanup
echo [%date% %time%] Cleanup >> "%LOG%"
if exist "%NEW_EXE%" del "%NEW_EXE%" >nul 2>&1
(goto) 2>nul & del "%~f0"
"""
        try:
            with open(batch_path, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(content)
            return batch_path
        except Exception as e:
            logger.error(f"Updater: не удалось записать batch: {e}")
            return None

    @staticmethod
    def _sha256(path: str) -> str:
        """Считает sha256 файла."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def _emit_state(self, state: str, payload: dict = None):
        """Отправляет состояние в GUI (если callback задан)."""
        if self.state_callback is None:
            return
        try:
            self.state_callback(state, payload or {})
        except Exception as e:
            logger.warning(f"Updater: ошибка в state_callback ({state}): {e}")