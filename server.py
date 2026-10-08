import time
import os
import threading

from database import Database
from tts_service import TTSService
from warehouse_manager import WarehouseManager
from http_server import HTTPServerThread
from config import ServerConfig, AdminConfig, migrate_config_if_needed
from logger import setup_logger

logger = setup_logger('server')


class ServerCacheCleanerThread(threading.Thread):
    """Поток фоновой круглосуточной очистки сгенерированного аудио-кэша на сервере по TTL."""

    def __init__(self, config, cache_dir):
        super().__init__(daemon=True, name="ServerCacheCleaner")
        self.config = config
        self.cache_dir = cache_dir

    def run(self):
        logger.info("Фоновый поток очистки кэша сервера успешно запущен.")
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
                            f"📁 Автоочистка сервера: удалено {cleaned_count} WAV старше {ttl_days} дн.")
            except Exception as e:
                logger.error(f"Ошибка в потоке автоочистки кэша сервера: {e}")

            time.sleep(6 * 60 * 60)


def main():
    logger.info("=== Запуск сервера CallNotifier ===")

    # 1) Разовая миграция старого config.json
    migrate_config_if_needed()

    # 2) Серверный конфиг + админский конфиг
    config = ServerConfig()
    admin_config = AdminConfig()

    host = config.get("http_server", "host", default="0.0.0.0")
    port = config.get("http_server", "port", default=5499)

    db = Database()
    tts_service = TTSService()

    # Менеджер складов
    warehouse_manager = WarehouseManager(db, config=config, tts_service=tts_service)
    warehouse_manager.start()
    logger.info("Менеджер складов запущен")

    # Уборщик тяжелого кэша Silero TTS
    server_cleaner = ServerCacheCleanerThread(config, tts_service.cache_dir)
    server_cleaner.start()

    # HTTP-сервер Flask
    http_server = HTTPServerThread(
        host=host,
        port=port,
        db=db,
        audio_manager=None,
        warehouse_manager=warehouse_manager,
        tts_service=tts_service,
        admin_config=admin_config
    )
    http_server.start()
    logger.info(f"HTTP-сервер запущен на {host}:{port}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Остановка сервера...")
        warehouse_manager.stop()
        http_server.shutdown()
        db.close()
        logger.info("=== Сервер остановлен ===")


if __name__ == "__main__":
    main()