import sqlite3
import os
import threading
import time
from logger import setup_logger

logger = setup_logger('database')

# ЗДЕСЬ ДВА ПОДЧЕРКИВАНИЯ С КАЖДОЙ СТОРОНЫ ОТ file
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.db")


class Database:
    # ЗДЕСЬ ДВА ПОДЧЕРКИВАНИЯ С КАЖДОЙ СТОРОНЫ ОТ init
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

        # Кэш для хранения ID, по которым недавно пришел 'stop' (внешний ID -> timestamp)
        self.recent_stops = {}

        self._init_db()
        logger.info("База данных инициализирована")

    def _init_db(self):
        with self._lock:
            # Добавлено поле forced_close_time для обработки рассинхронизации АСистем
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    equipment TEXT DEFAULT '',
                    info TEXT DEFAULT '',
                    status TEXT DEFAULT 'open',
                    datetime_start REAL NOT NULL,
                    datetime_stop REAL,
                    forced_close_time REAL, 
                    created_at REAL DEFAULT (strftime('%s', 'now'))
                )
            """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_status_type ON calls(status, type)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_external_id ON calls(external_id)")

            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS types (
                    type TEXT PRIMARY KEY,
                    description TEXT DEFAULT '',
                    live_time_min INTEGER DEFAULT 0,
                    repeat_interval_min INTEGER DEFAULT 5,
                    should_speak INTEGER DEFAULT 1
                )
            """)

            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS warehouses (
                    key TEXT PRIMARY KEY,
                    name TEXT,
                    capacity INTEGER,
                    orange_threshold INTEGER,
                    red_threshold INTEGER,
                    repeat_interval_min INTEGER,
                    current_value INTEGER DEFAULT 0,
                    updated_at REAL
                )
            """)

            # Словарь произношений и ударений для TTS.
            # source — что ищем в тексте (регистр игнорируется).
            # replacement — на что заменяем.
            # kind: 'replace' (замена, латиница → кириллица) или
            #       'stress'  (расстановка ударений через +).
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS pronunciation (
                    source TEXT PRIMARY KEY,
                    replacement TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'replace',
                    created_at REAL DEFAULT (strftime('%s', 'now'))
                )
            """)

            # Шаблоны TTS для озвучки вызовов.
            # Для каждого типа вызова можно задать свой текст для:
            #   * new_call_template — озвучка нового вызова;
            #   * reminder_template — озвучка напоминания.
            # Пустой шаблон = использовать дефолтный из tts_service.py.
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS tts_templates (
                    type TEXT PRIMARY KEY,
                    new_call_template TEXT DEFAULT '',
                    reminder_template TEXT DEFAULT '',
                    updated_at REAL DEFAULT (strftime('%s', 'now'))
                )
            """)
            self._conn.commit()
            self._init_default_types()
            self._init_default_pronunciation()
            self._init_default_templates()

    def _init_default_types(self):
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM types").fetchone()[0]
            if count == 0:
                default_types = [
                    ("manager", "Вызов менеджера", 0, 5, 1),
                    ("mechanic", "Вызов механика", 30, 3, 1),
                    ("newstamp", "Новая штамповка", 0, 5, 1),
                    ("otk", "Вызов ОТК", 0, 5, 1),
                    ("master_press", "Мастер пресса", 0, 5, 1),
                    ("master_postpress", "Мастер постпресса", 0, 5, 1),
                    ("technolog", "Вызов технолога", 0, 5, 1),
                    ("repair_stamp", "Ремонт штампа", 60, 10, 1),
                    ("printplate", "Печатная форма", 0, 5, 1),
                    ("warehouse", "Складское оповещение", 0, 240, 1)
                ]
                for t in default_types:
                    self._conn.execute("""
                        INSERT OR IGNORE INTO types (type, description, live_time_min, repeat_interval_min, should_speak)
                        VALUES (?, ?, ?, ?, ?)
                    """, t)
                self._conn.commit()
                logger.info(f"Добавлено {len(default_types)} стандартных типов вызовов")
                
    def _init_default_pronunciation(self):
        """Заполняет словарь произношений значениями из старого хардкода,
        если таблица пуста (первый запуск после обновления).
        """
        with self._lock:
            count = self._conn.execute("SELECT COUNT(*) FROM pronunciation").fetchone()[0]
            if count > 0:
                return

            # Миграция из старого LATIN_ABBR_DICT и STRESS_DICTIONARY.
            # Ключи в БД нормализуем в нижний регистр — сравнение в TTS
            # будет через re.IGNORECASE, так что регистр в БД не важен,
            # но для единообразия храним в lower.
            defaults = [
                # --- Замены (латиница, аббревиатуры) ---
                ("xl",     "ИксЭль",   "replace"),
                ("id",     "АйДи",     "replace"),
                ("pc",     "ПэКа",     "replace"),
                ("bobst",  "Бобст",    "replace"),
                # --- Ударения (кириллица) ---
                ("иванов",     "ива+нов",     "stress"),
                ("петров",     "петро+в",     "stress"),
                ("сидоров",    "си+доров",    "stress"),
                ("звонит",     "зво+нит",     "stress"),
                ("кладовщик",  "кла+довщик",  "stress"),
                ("каталог",    "катало+г",    "stress"),
                ("документ",   "докуме+нт",   "stress"),
                ("станок",     "стано+к",     "stress"),
                ("станки",     "станки+",     "stress"),
                ("паллет",     "палле+т",     "stress"),
                ("отгрузка",   "отгру+зка",   "stress"),
                ("погрузка",   "погру+зка",   "stress"),
                ("разгрузка",  "разгру+зка",  "stress"),
            ]
            for source, replacement, kind in defaults:
                self._conn.execute("""
                    INSERT OR IGNORE INTO pronunciation (source, replacement, kind)
                    VALUES (?, ?, ?)
                """, (source, replacement, kind))
            self._conn.commit()
            logger.info(f"Добавлено {len(defaults)} записей словаря произношений (миграция из хардкода)")

    def _init_default_templates(self):
        """Создаёт пустые шаблоны для всех существующих типов вызовов.

        Пустой шаблон = использовать дефолтный (зашитый в tts_service.py).
        Админ может потом заполнить любой из них через веб-интерфейс.
        """
        with self._lock:
            # Создаём пустые записи для всех типов, у которых ещё нет шаблона
            self._conn.execute("""
                INSERT OR IGNORE INTO tts_templates (type, new_call_template, reminder_template)
                SELECT type, '', '' FROM types
            """)
            self._conn.commit()
            # Считаем, сколько всего шаблонов
            total = self._conn.execute("SELECT COUNT(*) FROM tts_templates").fetchone()[0]
            logger.info(f"Шаблонов TTS в БД: {total}")
            
    def register_recent_stop(self, external_id: str):
        """Запоминаем, что по этому ID недавно был stop."""
        now = time.time()
        self.recent_stops[str(external_id)] = now
        # Чистим кэш от записей старше 5 минут (300 сек)
        self.recent_stops = {k: v for k, v in self.recent_stops.items() if (now - v) < 300}

    def is_recent_stop(self, external_id: str) -> bool:
        """Проверяем, был ли stop за последние 5 минут."""
        ts = self.recent_stops.get(str(external_id), 0)
        return (time.time() - ts) < 300

    def create_call(self, external_id: str, type: str, name: str = "", equipment: str = "", info: str = "",
                    forced_close_time: float = None):
        with self._lock:
            existing = self._conn.execute("SELECT id FROM calls WHERE external_id = ? AND status = 'open'",
                                          (str(external_id),)).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE calls SET type = ?, name = ?, equipment = ?, info = ?, datetime_start = ?, forced_close_time = ? WHERE id = ?",
                    (type, name, equipment, info, time.time(), forced_close_time, existing['id']))
            else:
                self._conn.execute(
                    "INSERT INTO calls (external_id, type, name, equipment, info, status, datetime_start, forced_close_time) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                    (str(external_id), type, name, equipment, info, time.time(), forced_close_time))
            self._conn.commit()
        logger.info(f"Вызов создан/обновлён: external_id={external_id}, type={type}, name={name}")

    def close_call(self, external_id: str):
        with self._lock:
            self._conn.execute(
                "UPDATE calls SET status = 'closed', datetime_stop = ? WHERE external_id = ? AND status = 'open'",
                (time.time(), str(external_id)))
            self._conn.commit()
        logger.info(f"Вызов закрыт: external_id={external_id}")

    def get_open_calls(self, types_filter: list = None):
        now = time.time()
        with self._lock:
            # ------------------------------------------------------------------
            # 1) Автоматическое закрытие по forced_close_time
            #    (это «ручной» таймер из админки — приоритетнее live_time)
            # ------------------------------------------------------------------
            self._conn.execute(
                "UPDATE calls SET status = 'closed', datetime_stop = ? "
                "WHERE status = 'open' AND forced_close_time IS NOT NULL "
                "AND forced_close_time <= ?",
                (now, now)
            )

            # ------------------------------------------------------------------
            # 2) Автоматическое закрытие по live_time_min из types
            #    Условия:
            #      * live_time_min > 0 (иначе — «ждать stop», не трогаем)
            #      * datetime_start + live_time_min * 60 <= now
            #    Если для типа не нашлось записи в types — не закрываем
            #    (защита от «висячих» вызовов с неизвестным типом).
            # ------------------------------------------------------------------
            self._conn.execute("""
                UPDATE calls
                SET status = 'closed', datetime_stop = ?
                WHERE status = 'open'
                  AND datetime_start + (
                        SELECT live_time_min * 60
                        FROM types WHERE types.type = calls.type
                      ) <= ?
                  AND (SELECT live_time_min FROM types WHERE types.type = calls.type) > 0
            """, (now, now))

            self._conn.commit()

            if types_filter and len(types_filter) > 0:
                placeholders = ','.join('?' * len(types_filter))
                query = f"SELECT * FROM calls WHERE status = 'open' AND type IN ({placeholders}) ORDER BY datetime_start ASC"
                rows = self._conn.execute(query, types_filter).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM calls WHERE status = 'open' ORDER BY datetime_start ASC").fetchall()

            return [{
                "id": row["id"], "external_id": row["external_id"], "type": row["type"],
                "name": row["name"], "equipment": row["equipment"], "info": row["info"],
                "status": row["status"], "datetime_start": row["datetime_start"],
                "datetime_stop": row["datetime_stop"]
            } for row in rows]

    def get_warehouses(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM warehouses").fetchall()
            return [dict(row) for row in rows]

    def get_all_types(self):
        with self._lock:
            rows = self._conn.execute("SELECT * FROM types").fetchall()
            return [dict(row) for row in rows]

    def update_warehouse_value(self, key: str, value: int):
        with self._lock:
            self._conn.execute("UPDATE warehouses SET current_value = ?, updated_at = ? WHERE key = ?",
                               (value, time.time(), key))
            self._conn.commit()

    def upsert_warehouse(self, data: dict):
        with self._lock:
            self._conn.execute("""
                INSERT INTO warehouses (key, name, capacity, orange_threshold, red_threshold, repeat_interval_min, current_value, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0)
                ON CONFLICT(key) DO UPDATE SET name=excluded.name, capacity=excluded.capacity, orange_threshold=excluded.orange_threshold, red_threshold=excluded.red_threshold, repeat_interval_min=excluded.repeat_interval_min
            """, (data["key"], data["name"], data["capacity"], data["orange_threshold"], data["red_threshold"],
                  data["repeat_interval_min"]))
            self._conn.commit()

    def delete_warehouse(self, key: str):
        with self._lock:
            self._conn.execute("DELETE FROM warehouses WHERE key = ?", (key,))
            self._conn.commit()

    def get_call_by_id(self, call_id: int):
        """Возвращает один вызов по внутреннему ID (для админки)."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "external_id": row["external_id"],
                "type": row["type"],
                "name": row["name"],
                "equipment": row["equipment"],
                "info": row["info"],
                "status": row["status"],
                "datetime_start": row["datetime_start"],
                "datetime_stop": row["datetime_stop"],
                "forced_close_time": row["forced_close_time"],
            }

    def close_call_by_id(self, call_id: int) -> bool:
        """Закрывает вызов по внутреннему ID. Возвращает True, если что-то закрылось."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE calls SET status = 'closed', datetime_stop = ? WHERE id = ? AND status = 'open'",
                (time.time(), call_id)
            )
            self._conn.commit()
            changed = cur.rowcount > 0
        if changed:
            logger.info(f"Вызов id={call_id} закрыт через админку")
        return changed

    def upsert_type(self, type_key: str, description: str = "", live_time_min: int = 0,
                    repeat_interval_min: int = 5, should_speak: int = 1) -> None:
        """Создаёт или обновляет тип вызова."""
        with self._lock:
            self._conn.execute("""
                INSERT INTO types (type, description, live_time_min, repeat_interval_min, should_speak)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(type) DO UPDATE SET
                    description = excluded.description,
                    live_time_min = excluded.live_time_min,
                    repeat_interval_min = excluded.repeat_interval_min,
                    should_speak = excluded.should_speak
            """, (type_key, description, live_time_min, repeat_interval_min, should_speak))
            self._conn.commit()
        logger.info(f"Тип вызова сохранён: {type_key}")

    def delete_type(self, type_key: str) -> tuple[bool, str]:
        """Удаляет тип. Возвращает (успех, сообщение об ошибке).

        Запрещает удаление, если по этому типу есть активные (open) вызовы.
        """
        with self._lock:
            active = self._conn.execute(
                "SELECT COUNT(*) FROM calls WHERE type = ? AND status = 'open'",
                (type_key,)
            ).fetchone()[0]
            if active > 0:
                return False, f"Нельзя удалить тип: есть {active} активных вызовов"

            cur = self._conn.execute("DELETE FROM types WHERE type = ?", (type_key,))
            self._conn.commit()
            if cur.rowcount == 0:
                return False, "Тип не найден"
        logger.info(f"Тип вызова удалён: {type_key}")
        return True, "ok"

    def update_warehouse_params(self, key: str, name: str = None,
                                capacity: int = None,
                                orange_threshold: int = None,
                                red_threshold: int = None,
                                repeat_interval_min: int = None):
        """Обновляет параметры склада (кроме current_value). None-поля не трогаются."""
        with self._lock:
            fields = []
            values = []
            if name is not None:
                fields.append("name = ?");
                values.append(name)
            if capacity is not None:
                fields.append("capacity = ?");
                values.append(capacity)
            if orange_threshold is not None:
                fields.append("orange_threshold = ?");
                values.append(orange_threshold)
            if red_threshold is not None:
                fields.append("red_threshold = ?");
                values.append(red_threshold)
            if repeat_interval_min is not None:
                fields.append("repeat_interval_min = ?");
                values.append(repeat_interval_min)

            if not fields:
                return False

            fields.append("updated_at = ?");
            values.append(time.time())
            values.append(key)

            cur = self._conn.execute(
                f"UPDATE warehouses SET {', '.join(fields)} WHERE key = ?",
                values
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ========================================================================
    # Словарь произношений (TTS)
    # ========================================================================

    def get_pronunciations(self) -> list:
        """Возвращает все записи словаря произношений."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, replacement, kind, created_at FROM pronunciation ORDER BY kind, source"
            ).fetchall()
            return [dict(row) for row in rows]

    def upsert_pronunciation(self, source: str, replacement: str, kind: str = 'replace') -> bool:
        """Создаёт или обновляет запись словаря.

        source/replacement обрезаются; source приводится к нижнему регистру
        (сравнение в TTS идёт через re.IGNORECASE).
        kind должен быть 'replace' или 'stress'.
        """
        source = (source or '').strip().lower()
        replacement = (replacement or '').strip()
        if not source or not replacement:
            return False
        if kind not in ('replace', 'stress'):
            return False

        with self._lock:
            self._conn.execute("""
                INSERT INTO pronunciation (source, replacement, kind)
                VALUES (?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    replacement = excluded.replacement,
                    kind = excluded.kind
            """, (source, replacement, kind))
            self._conn.commit()
        logger.info(f"Словарь произношений сохранён: {source} → {replacement} ({kind})")
        return True

    def delete_pronunciation(self, source: str) -> bool:
        """Удаляет запись словаря по source. Возвращает True, если что-то удалилось."""
        source = (source or '').strip().lower()
        if not source:
            return False
        with self._lock:
            cur = self._conn.execute("DELETE FROM pronunciation WHERE source = ?", (source,))
            self._conn.commit()
            deleted = cur.rowcount > 0
        if deleted:
            logger.info(f"Словарь произношений удалён: {source}")
        return deleted

    # ========================================================================
    # Шаблоны TTS
    # ========================================================================

    def get_templates(self) -> dict:
        """Возвращает ВСЕ шаблоны в виде словаря {type: {new_call_template, reminder_template}}.

        Формат возврата — словарь для быстрого доступа по типу в sync-цикле.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT type, new_call_template, reminder_template FROM tts_templates"
            ).fetchall()
            return {
                row["type"]: {
                    "new_call_template": row["new_call_template"] or "",
                    "reminder_template": row["reminder_template"] or "",
                }
                for row in rows
            }

    def get_template(self, type_key: str) -> dict:
        """Возвращает шаблоны для одного типа."""
        with self._lock:
            row = self._conn.execute(
                "SELECT type, new_call_template, reminder_template FROM tts_templates WHERE type = ?",
                (type_key,)
            ).fetchone()
            if not row:
                return {"type": type_key, "new_call_template": "", "reminder_template": ""}
            return {
                "type": row["type"],
                "new_call_template": row["new_call_template"] or "",
                "reminder_template": row["reminder_template"] or "",
            }

    def upsert_template(self, type_key: str,
                        new_call_template: str = "",
                        reminder_template: str = "") -> bool:
        """Создаёт или обновляет шаблоны для типа."""
        type_key = (type_key or "").strip()
        if not type_key:
            return False

        # Гарантируем, что тип существует — это защита от создания «висячих» шаблонов
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM types WHERE type = ?", (type_key,)
            ).fetchone()
            if not exists:
                return False

            self._conn.execute("""
                INSERT INTO tts_templates (type, new_call_template, reminder_template, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(type) DO UPDATE SET
                    new_call_template = excluded.new_call_template,
                    reminder_template = excluded.reminder_template,
                    updated_at = excluded.updated_at
            """, (type_key, new_call_template or "", reminder_template or "", time.time()))
            self._conn.commit()
        logger.info(f"Шаблон TTS сохранён для типа: {type_key}")
        return True

    def close(self):
        if self._conn:
            self._conn.close()
            logger.info("Соединение с БД закрыто")