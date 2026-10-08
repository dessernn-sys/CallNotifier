import tkinter as tk
import os
import time
import threading

from config import ClientConfig, migrate_config_if_needed, USER_DATA_DIR, _ensure_client_config_present
from audio import AudioManager
from gui.main_window import FullScreenApp
from logger import setup_logger

logger = setup_logger('client')


class CacheCleanerThread(threading.Thread):
    """Поток фоновой круглосуточной очистки кэша клиента по TTL."""

    def __init__(self, config, cache_dir):
        super().__init__(daemon=True, name="ClientCacheCleaner")
        self.config = config
        self.cache_dir = cache_dir

    def run(self):
        logger.info("Фоновый поток очистки кэша клиента успешно запущен.")
        while True:
            try:
                if os.path.exists(self.cache_dir):
                    ttl_days = self.config.get("display", "cache_ttl_days", default=3)
                    limit_sec = time.time() - (ttl_days * 24 * 60 * 60)

                    cleaned_count = 0
                    for filename in os.listdir(self.cache_dir):
                        if filename.endswith('.wav'):
                            filepath = os.path.join(self.cache_dir, filename)
                            if os.path.getmtime(filepath) < limit_sec:
                                os.remove(filepath)
                                cleaned_count += 1

                    if cleaned_count > 0:
                        logger.info(
                            f"💾 Автоочистка кэша: удалено {cleaned_count} WAV файлов старше {ttl_days} дн.")
            except Exception as e:
                logger.error(f"Ошибка в потоке автоочистки кэша клиента: {e}")

            time.sleep(6 * 60 * 60)


def main():
    logger.info("=== Запуск клиента CallNotifier ===")

    # 1) Разовая миграция старого config.json
    migrate_config_if_needed()
    _ensure_client_config_present()

    # 2) Клиентский конфиг
    config = ClientConfig()
    host = config.get("http_server", "host", default="127.0.0.1")
    port = config.get("http_server", "port", default=5499)
    server_url = f"http://{host}:{port}"

    audio = AudioManager(config, server_url=server_url, tts_service=None)
    logger.info("Аудио-менеджер запущен")

    # 3) Уборщик локального кэша
    cleaner = CacheCleanerThread(config, audio.client_cache_dir)
    cleaner.start()

    root = tk.Tk()
    app = FullScreenApp(
        root=root,
        config=config,
        db=None,
        audio=audio,
        http_server=None,
        warehouse_manager=None
    )
    app.app = app

    try:
        root.mainloop()
    except Exception as e:
        logger.error(f"Ошибка в главном цикле интерфейса: {e}")
    finally:
        logger.info("Завершение работы клиентского приложения...")
        if 'audio' in locals() and audio:
            try:
                audio.shutdown()
            except Exception as e:
                logger.warning(f"Ошибка при остановке AudioManager: {e}")
        if 'root' in locals() and root:
            try:
                root.destroy()
            except tk.TclError:
                pass
        logger.info("=== Клиент CallNotifier успешно остановлен ===")


if __name__ == "__main__":
    main()