import threading
import re
import os
import time
from collections import deque
from functools import wraps

from flask import Flask, request, jsonify, send_file, abort, Response
from werkzeug.serving import make_server

from database import Database
from tts_service import TTSService, render_template, DEFAULT_NEW_CALL_TEMPLATE, DEFAULT_REMINDER_TEMPLATE
from logger import setup_logger, LOG_FILE


logger = setup_logger('http_server')


# ============================================================================
# Basic Auth для админки
# ============================================================================

def _check_basic_auth(admin_config) -> bool:
    """Проверяет HTTP Basic Auth по паролю из config_admin.json.

    Логин игнорируется — важен только пароль.
    """
    if admin_config is None:
        return False
    auth = request.authorization
    if not auth or not auth.password:
        return False
    return auth.password == admin_config.get_admin_password()


def _basic_auth_challenge():
    """Возвращает 401 с заголовком WWW-Authenticate, чтобы браузер показал окно логина."""
    return Response(
        "Требуется авторизация", 401,
        {"WWW-Authenticate": 'Basic realm="CallNotifier Admin"'}
    )


class HTTPServerThread(threading.Thread):
    def __init__(self, host: str, port: int, db: Database, audio_manager, warehouse_manager,
                 tts_service: TTSService = None, admin_config=None):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.db = db
        self.audio_manager = audio_manager
        self.warehouse_manager = warehouse_manager
        self.tts_service = tts_service
        self.admin_config = admin_config
        self.started_at = time.time()

        # Трекер последних напоминаний: {(client_id, key): unix_timestamp}
        # Хранится строго в памяти процесса. Каждый клиент имеет СВОЙ трекер,
        # чтобы напоминания озвучивались независимо на всех рабочих местах.
        #
        # Формат ключа:
        #   * (client_id, "all_reminders") — общее напоминание на клиента (текущий формат).
        #     Одно напоминание раз в N минут со ВСЕМИ активными вызовами.
        #   * (client_id, external_id) — старый формат per-call. Может ещё
        #     оставаться в памяти при горячем обновлении — очистится через час.
        self._last_reminder_sent: dict[tuple[str, str], float] = {}
        self._reminder_lock = threading.Lock()

        self.app = Flask(__name__)
        self.server = None
        self._configure_routes()

    # ------------------------------------------------------------------------
    # Декоратор аутентификации админки
    # ------------------------------------------------------------------------
    def _admin_required(self, f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not _check_basic_auth(self.admin_config):
                return _basic_auth_challenge()
            return f(*args, **kwargs)
        return wrapper

    # ------------------------------------------------------------------------
    # Конфигурация маршрутов
    # ------------------------------------------------------------------------
    def _configure_routes(self):
        # ====================================================================
        # Публичное API для клиентов (без аутентификации)
        # ====================================================================

        @self.app.route('/api/v1/incoming', methods=['POST'])
        def handle_incoming():
            logger.info(f"Новый запрос: {request.method} {request.url}")
            params = self._parse_post_params()
            logger.info(f"Параметры: {params}")

            external_id = str(params.get('id', '')).strip()
            status = str(params.get('status', '')).strip().lower()

            warehouses_updated = False
            for i in range(1, 6):
                wh_key = f'wh{i}'
                if wh_key in params:
                    try:
                        value = int(params[wh_key])
                        if self.warehouse_manager:
                            self.warehouse_manager.update_value(wh_key, value)
                        else:
                            self.db.update_warehouse_value(wh_key, value)
                        warehouses_updated = True
                    except ValueError:
                        pass

            if not external_id and not status and warehouses_updated:
                return jsonify({"status": "warehouses_updated"}), 200
            if not external_id or not status:
                return jsonify({"error": "id and status are required"}), 400

            if status == 'stop':
                self.db.close_call(external_id)
                self.db.register_recent_stop(external_id)
                return jsonify({"status": "closed", "id": external_id}), 200

            if status == 'start':
                call_type = str(params.get('type', '')).strip()
                if not call_type:
                    return jsonify({"error": "type is required"}), 400

                name = str(params.get('name', '')).strip()
                equipment = str(params.get('equipment', '')).strip()
                info = str(params.get('info', '')).strip()

                forced_close_time = None
                if self.db.is_recent_stop(external_id):
                    logger.warning(f"Обнаружен опережающий стоп для {external_id}. Вызов будет закрыт через 1 минуту.")
                    forced_close_time = time.time() + 60

                self.db.create_call(external_id, call_type, name, equipment, info,
                                    forced_close_time=forced_close_time)
                return jsonify({"status": "created", "id": external_id}), 201

            return jsonify({"error": "invalid status"}), 400

        @self.app.route('/api/v1/sync', methods=['GET'])
        def sync():
            # ---------- Идентификация клиента ----------
            # Клиент передаёт свой уникальный ID в заголовке X-Client-Id.
            # Формат: "hostname-username". Если заголовка нет — используем "unknown".
            # Это позволяет каждому клиенту получать напоминания НЕЗАВИСИМО.
            client_id = (request.headers.get('X-Client-Id') or 'unknown').strip()[:128]

            types_param = request.args.get('types', '')
            types_filter = [t.strip() for t in types_param.split(',') if t.strip()] if types_param else None

            calls = self.db.get_open_calls(types_filter)
            warehouses = self.db.get_warehouses()

            # Загружаем шаблоны TTS для всех типов (один запрос к БД)
            templates = self.db.get_templates()
            
            # Кеш описаний типов (для {type_desc})
            type_desc_map = {t['type']: t.get('description', '') or t['type']
                             for t in self.db.get_all_types()}
            
            result_calls = []
            reminders_due = {}
            now = time.time()

            # Множество external_id активных вызовов — для очистки трекера напоминаний
            active_external_ids = set()

            # Кандидаты для ОБЩЕГО напоминания на клиента.
            # Сюда попадают все вызовы, которые уже «пересели» свой repeat_sec
            # (то есть по ним пора напоминать).
            reminder_candidates = []

            # Минимальный repeat_sec среди ВСЕХ активных вызовов (нужен для выбора
            # интервала между напоминаниями клиенту).
            min_repeat_sec = None

            for call in calls:
                # ---------- Озвучка НОВОГО вызова (с учётом шаблона типа) ----------
                new_tpl = templates.get(call['type'], {}).get('new_call_template') or ""
                # type_desc нужен для {type_desc} — берём из кеша типов
                type_desc = type_desc_map.get(call['type'], "")

                if new_tpl:
                    tts_text = render_template(new_tpl, call, type_desc)
                else:
                    tts_text = render_template(DEFAULT_NEW_CALL_TEMPLATE, call, type_desc)

                # Если шаблон дал пустую строку — не озвучиваем
                if not tts_text.strip():
                    audio_filename = None
                    audio_size = 0
                else:
                    if self.tts_service and not self.tts_service.is_cached(tts_text):
                        threading.Thread(
                            target=self.tts_service.generate_chunk,
                            args=(tts_text,), daemon=True
                        ).start()

                    audio_filename = self.tts_service.get_filename(tts_text) if self.tts_service else None
                    audio_filepath = os.path.join(self.tts_service.cache_dir, audio_filename) if audio_filename else None
                    audio_size = os.path.getsize(audio_filepath) if audio_filepath and os.path.exists(audio_filepath) else 0

                call['audio_filename'] = audio_filename
                call['audio_size'] = audio_size
                result_calls.append(call)

                active_external_ids.add(call['external_id'])

                # ---------- Проверяем: этот вызов может попасть в общее напоминание? ----------
                type_cfg = self.db._conn.execute(
                    "SELECT repeat_interval_min FROM types WHERE type = ?", (call['type'],)
                ).fetchone()
                repeat_min = type_cfg['repeat_interval_min'] if type_cfg else 5
                repeat_sec = max(60, repeat_min * 60)  # защита от нулевых интервалов

                if min_repeat_sec is None or repeat_sec < min_repeat_sec:
                    min_repeat_sec = repeat_sec

                elapsed_since_start = now - call['datetime_start']
                if elapsed_since_start < repeat_sec:
                    continue  # ещё рано — вызов появился недавно

                # Этот вызов готов к напоминанию — добавляем в общий список
                reminder_candidates.append(call)

            # ---------- Общее напоминание на клиента ----------
            # Одно напоминание раз в N минут со ВСЕМИ активными вызовами.
            # Это устраняет «эхо» и каскады: клиент всегда получает один файл.
            if reminder_candidates and self.tts_service:
                tracker_key = (client_id, "all_reminders")
                last_sent = self._last_reminder_sent.get(tracker_key, 0.0)

                client_repeat_sec = max(60, min_repeat_sec or 300)

                if last_sent == 0.0 or (now - last_sent) >= client_repeat_sec:
                    # Собираем одно напоминание из ВСЕХ кандидатов.
                    # Заголовок оставляем жёстко, фразы — по шаблонам типов.
                    phrases = ["Напоминаю, список ожидания!"]
                    for c in reminder_candidates:
                        tpl = templates.get(c['type'], {}).get('reminder_template') or ""
                        if tpl:
                            phrase = render_template(tpl, c, type_desc_map.get(c['type'], ""))
                        else:
                            phrase = render_template(DEFAULT_REMINDER_TEMPLATE, c, type_desc_map.get(c['type'], ""))
                        # Пустые фразы пропускаем
                        if phrase.strip():
                            phrases.append(phrase)
                    # Если после фильтра остался только заголовок — ничего не напоминаем
                    if len(phrases) > 1:
                        reminder_file = self.tts_service.build_reminder_audio(phrases, pause_sec=0.5)
                        if reminder_file:
                            reminders_due["all"] = reminder_file
                            with self._reminder_lock:
                                self._last_reminder_sent[tracker_key] = now

            # ---------- Очистка трекера ----------
            # Удаляем:
            #   а) записи по закрытым вызовам (которых больше нет в active_external_ids)
            #   б) записи, которым больше часа — считаем клиента «мертвым»
            self._cleanup_reminder_tracker(active_external_ids, now)

            # ---------- Складские алерты и визуализация (per-client + уважение к allowed_types) ----------
            # Клиент получает складские алерты ТОЛЬКО если в его фильтре есть 'warehouse'.
            # Пустой types_filter = клиент хочет ВСЕ типы (как у обычных вызовов).
            wants_warehouse = (not types_filter) or ('warehouse' in types_filter)

            warehouse_alerts = []
            if self.warehouse_manager:
                if wants_warehouse:
                    warehouse_alerts = self.warehouse_manager.get_pending_alerts(client_id)
                else:
                    # Клиент не хочет склад — регистрируем активность и помечаем
                    # все текущие алерты как «уже отправленные ему» (чтобы при
                    # возврате галочки он не получил накопленное).
                    if hasattr(self.warehouse_manager, 'register_client_activity'):
                        self.warehouse_manager.register_client_activity(client_id)

            # Визуализация складов на клиенте — только если клиент хочет warehouse.
            # Если галочка снята — отдаём пустой массив, чтобы WarehouseDisplay
            # показывал «Нет данных».
            warehouses_for_client = warehouses if wants_warehouse else []

            from config import __version__
            return jsonify({
                "server_time": int(time.time()),
                "server_version": __version__,
                "calls": result_calls,
                "warehouses": warehouses_for_client,
                "reminders_due": reminders_due,
                "warehouse_alerts": warehouse_alerts
            }), 200

        @self.app.route('/api/v1/audio/<filename>', methods=['GET'])
        def get_audio(filename):
            if '..' in filename or '/' in filename or '\\' in filename:
                abort(403)
            if not self.tts_service:
                abort(404)
            filepath = os.path.join(self.tts_service.cache_dir, filename)
            if os.path.exists(filepath):
                return send_file(filepath, mimetype='audio/wav')
            abort(404)

        @self.app.route('/api/v1/types', methods=['GET'])
        def get_types():
            return jsonify(self.db.get_all_types()), 200

        @self.app.route('/', methods=['GET'])
        def serve_client():
            static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
            return send_file(os.path.join(static_dir, 'index.html'), mimetype='text/html')

        @self.app.route('/static/<path:filename>', methods=['GET'])
        def serve_static(filename):
            static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
            return send_file(os.path.join(static_dir, filename))

        @self.app.route('/health', methods=['GET'])
        def health():
            return jsonify({"status": "ok"}), 200

        # ====================================================================
        # Админка: отдача статики
        # ====================================================================

        @self.app.route('/admin', methods=['GET'])
        @self._admin_required
        def admin_panel():
            static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'admin')
            return send_file(os.path.join(static_dir, 'admin.html'), mimetype='text/html')

        @self.app.route('/static/admin/<path:filename>', methods=['GET'])
        @self._admin_required
        def serve_admin_static(filename):
            static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'admin')
            return send_file(os.path.join(static_dir, filename))

        # ====================================================================
        # Админка: API — Дашборд
        # ====================================================================

        @self.app.route('/admin/api/status', methods=['GET'])
        @self._admin_required
        def admin_status():
            now = time.time()
            calls = self.db.get_open_calls()
            types = self.db.get_all_types()

            tts_status = self.tts_service.get_status() if self.tts_service and hasattr(self.tts_service, 'get_status') else {"model_loaded": False}
            wh_status = self.warehouse_manager.get_status() if self.warehouse_manager and hasattr(self.warehouse_manager, 'get_status') else {}

            return jsonify({
                "server": {
                    "running": True,
                    "uptime_seconds": int(now - self.started_at),
                    "host": self.host,
                    "port": self.port,
                    "server_time": int(now),
                },
                "tts": tts_status,
                "warehouse_manager": wh_status,
                "db": {
                    "active_calls": len(calls),
                    "types_count": len(types),
                },
            }), 200

        # ====================================================================
        # Админка: API — Вызовы
        # ====================================================================

        @self.app.route('/admin/api/calls', methods=['GET'])
        @self._admin_required
        def admin_get_calls():
            calls = self.db.get_open_calls()
            now = time.time()
            for c in calls:
                c["elapsed_min"] = int((now - c["datetime_start"]) / 60)
            return jsonify(calls), 200

        @self.app.route('/admin/api/calls', methods=['POST'])
        @self._admin_required
        def admin_create_call():
            data = request.get_json(silent=True) or {}
            external_id = str(data.get("external_id", "")).strip()
            call_type = str(data.get("type", "")).strip()
            name = str(data.get("name", "")).strip()
            equipment = str(data.get("equipment", "")).strip()
            info = str(data.get("info", "")).strip()
            forced_close_seconds = data.get("forced_close_seconds")

            if not external_id or not call_type:
                return jsonify({"error": "external_id и type обязательны"}), 400

            forced_close_time = None
            if forced_close_seconds:
                try:
                    forced_close_time = time.time() + int(forced_close_seconds)
                except (ValueError, TypeError):
                    pass

            self.db.create_call(external_id, call_type, name, equipment, info,
                                forced_close_time=forced_close_time)
            logger.info(f"Админ создал вызов: external_id={external_id}, type={call_type}, name={name}")
            return jsonify({"status": "created", "external_id": external_id}), 201

        @self.app.route('/admin/api/calls/<int:call_id>/close', methods=['POST'])
        @self._admin_required
        def admin_close_call(call_id):
            call = self.db.get_call_by_id(call_id)
            if not call:
                return jsonify({"error": "Вызов не найден"}), 404
            if call["status"] != "open":
                return jsonify({"error": "Вызов уже закрыт"}), 409

            ok = self.db.close_call_by_id(call_id)
            if not ok:
                return jsonify({"error": "Не удалось закрыть вызов"}), 500
            return jsonify({"status": "closed", "id": call_id}), 200

        # ====================================================================
        # Админка: API — Типы вызовов
        # ====================================================================

        @self.app.route('/admin/api/types', methods=['GET'])
        @self._admin_required
        def admin_get_types():
            types = self.db.get_all_types()
            # Добавляем счётчик активных вызовов по каждому типу
            with self.db._lock:
                active_by_type = {
                    row["type"]: row["cnt"]
                    for row in self.db._conn.execute(
                        "SELECT type, COUNT(*) AS cnt FROM calls WHERE status='open' GROUP BY type"
                    ).fetchall()
                }
            for t in types:
                t["active_calls"] = active_by_type.get(t["type"], 0)
            return jsonify(types), 200

        @self.app.route('/admin/api/types', methods=['POST'])
        @self._admin_required
        def admin_upsert_type():
            data = request.get_json(silent=True) or {}
            type_key = str(data.get("type", "")).strip()
            if not type_key:
                return jsonify({"error": "type обязателен"}), 400

            self.db.upsert_type(
                type_key=type_key,
                description=str(data.get("description", "")).strip(),
                live_time_min=int(data.get("live_time_min", 0) or 0),
                repeat_interval_min=int(data.get("repeat_interval_min", 5) or 5),
                should_speak=int(data.get("should_speak", 1) or 0),
            )
            return jsonify({"status": "ok", "type": type_key}), 200

        @self.app.route('/admin/api/types/<type_key>', methods=['DELETE'])
        @self._admin_required
        def admin_delete_type(type_key):
            ok, msg = self.db.delete_type(type_key)
            if not ok:
                return jsonify({"error": msg}), 409
            return jsonify({"status": "ok"}), 200

        # ====================================================================
        # Админка: API — Склады
        # ====================================================================

        @self.app.route('/admin/api/warehouses', methods=['GET'])
        @self._admin_required
        def admin_get_warehouses():
            if self.warehouse_manager and hasattr(self.warehouse_manager, 'get_status'):
                return jsonify(self.warehouse_manager.get_status()), 200
            # Fallback, если менеджер недоступен
            return jsonify({
                "running": False,
                "warehouses_count": 0,
                "exceeding_count": 0,
                "warehouses": self.db.get_warehouses(),
            }), 200

        @self.app.route('/admin/api/warehouses/<wh_key>/value', methods=['POST'])
        @self._admin_required
        def admin_set_wh_value(wh_key):
            data = request.get_json(silent=True) or {}
            try:
                value = int(data.get("value"))
            except (ValueError, TypeError):
                return jsonify({"error": "value должно быть числом"}), 400

            if not self.warehouse_manager:
                self.db.update_warehouse_value(wh_key, value)
                return jsonify({"status": "ok", "key": wh_key, "value": value}), 200

            if wh_key not in self.warehouse_manager.warehouses:
                return jsonify({"error": "Склад не найден"}), 404

            self.warehouse_manager.update_value(wh_key, value)
            logger.info(f"Админ установил значение склада {wh_key}={value}")
            return jsonify({"status": "ok", "key": wh_key, "value": value}), 200

        @self.app.route('/admin/api/warehouses/<wh_key>/params', methods=['POST'])
        @self._admin_required
        def admin_set_wh_params(wh_key):
            data = request.get_json(silent=True) or {}

            # Валидация: числа должны быть числами
            def _to_int_or_none(v):
                if v is None or v == "":
                    return None
                try:
                    return int(v)
                except (ValueError, TypeError):
                    return None

            try:
                updated = self.db.update_warehouse_params(
                    key=wh_key,
                    name=data.get("name"),
                    capacity=_to_int_or_none(data.get("capacity")),
                    orange_threshold=_to_int_or_none(data.get("orange_threshold")),
                    red_threshold=_to_int_or_none(data.get("red_threshold")),
                    repeat_interval_min=_to_int_or_none(data.get("repeat_interval_min")),
                )
            except Exception as e:
                logger.error(f"Ошибка обновления параметров склада {wh_key}: {e}")
                return jsonify({"error": str(e)}), 500

            if not updated:
                return jsonify({"error": "Склад не найден или нечего менять"}), 404

            # Перечитываем склад в WarehouseManager, чтобы изменения вступили в силу
            if self.warehouse_manager and hasattr(self.warehouse_manager, 'reload_from_db'):
                self.warehouse_manager.reload_from_db()

            logger.info(f"Админ обновил параметры склада {wh_key}: {data}")
            return jsonify({"status": "ok", "key": wh_key}), 200

        @self.app.route('/admin/api/warehouses/force_check', methods=['POST'])
        @self._admin_required
        def admin_wh_force_check():
            if not self.warehouse_manager or not hasattr(self.warehouse_manager, 'force_check'):
                return jsonify({"error": "Менеджер складов недоступен"}), 500
            self.warehouse_manager.force_check()
            return jsonify({"status": "ok"}), 200

        # ====================================================================
        # Админка: API — Словарь произношений
        # ====================================================================

        @self.app.route('/admin/api/pronunciation', methods=['GET'])
        @self._admin_required
        def admin_get_pronunciation():
            """Возвращает весь словарь произношений/ударений."""
            try:
                rows = self.db.get_pronunciations()
            except Exception as e:
                logger.error(f"Ошибка чтения словаря произношений: {e}")
                return jsonify({"error": str(e)}), 500
            return jsonify(rows), 200

        @self.app.route('/admin/api/pronunciation', methods=['POST'])
        @self._admin_required
        def admin_upsert_pronunciation():
            """Создаёт или обновляет запись словаря."""
            data = request.get_json(silent=True) or {}
            source = str(data.get("source", "")).strip()
            replacement = str(data.get("replacement", "")).strip()
            kind = str(data.get("kind", "replace")).strip().lower()

            if not source or not replacement:
                return jsonify({"error": "source и replacement обязательны"}), 400
            if kind not in ("replace", "stress"):
                return jsonify({"error": "kind должен быть 'replace' или 'stress'"}), 400

            try:
                ok = self.db.upsert_pronunciation(source, replacement, kind)
            except Exception as e:
                logger.error(f"Ошибка сохранения словаря: {e}")
                return jsonify({"error": str(e)}), 500

            if not ok:
                return jsonify({"error": "Не удалось сохранить запись"}), 400

            # Сбрасываем кэш в TTS, чтобы изменения применились мгновенно
            if self.tts_service and hasattr(self.tts_service, '_refresh_pronunciation_cache'):
                try:
                    self.tts_service._refresh_pronunciation_cache()
                except Exception as e:
                    logger.warning(f"Не удалось обновить кэш словаря TTS: {e}")

            return jsonify({"status": "ok", "source": source.lower()}), 200

        @self.app.route('/admin/api/pronunciation/<path:source>', methods=['DELETE'])
        @self._admin_required
        def admin_delete_pronunciation(source):
            """Удаляет запись словаря по source."""
            try:
                ok = self.db.delete_pronunciation(source)
            except Exception as e:
                logger.error(f"Ошибка удаления записи словаря: {e}")
                return jsonify({"error": str(e)}), 500

            if not ok:
                return jsonify({"error": "Запись не найдена"}), 404

            # Сбрасываем кэш TTS
            if self.tts_service and hasattr(self.tts_service, '_refresh_pronunciation_cache'):
                try:
                    self.tts_service._refresh_pronunciation_cache()
                except Exception as e:
                    logger.warning(f"Не удалось обновить кэш словаря TTS: {e}")

            return jsonify({"status": "ok"}), 200

        # ====================================================================
        # Админка: API — Шаблоны TTS
        # ====================================================================

        @self.app.route('/admin/api/templates', methods=['GET'])
        @self._admin_required
        def admin_get_templates():
            """Возвращает шаблоны + описания типов (для UI)."""
            try:
                templates = self.db.get_templates()
                types = self.db.get_all_types()
            except Exception as e:
                logger.error(f"Ошибка чтения шаблонов: {e}")
                return jsonify({"error": str(e)}), 500

            # Собираем единый список: для каждого типа — его шаблоны
            result = []
            for t in types:
                type_key = t['type']
                tpl = templates.get(type_key, {})
                result.append({
                    "type": type_key,
                    "description": t.get('description', ''),
                    "new_call_template": tpl.get('new_call_template', ''),
                    "reminder_template": tpl.get('reminder_template', ''),
                })
            return jsonify(result), 200

        @self.app.route('/admin/api/templates/<type_key>', methods=['POST'])
        @self._admin_required
        def admin_upsert_template(type_key):
            """Сохраняет шаблоны для одного типа."""
            data = request.get_json(silent=True) or {}
            new_call_template = str(data.get("new_call_template", ""))
            reminder_template = str(data.get("reminder_template", ""))

            try:
                ok = self.db.upsert_template(
                    type_key=type_key,
                    new_call_template=new_call_template,
                    reminder_template=reminder_template,
                )
            except Exception as e:
                logger.error(f"Ошибка сохранения шаблона {type_key}: {e}")
                return jsonify({"error": str(e)}), 500

            if not ok:
                return jsonify({"error": "Тип не найден"}), 404

            logger.info(f"Админ сохранил шаблон TTS для {type_key}")
            return jsonify({"status": "ok", "type": type_key}), 200

        @self.app.route('/admin/api/templates/preview', methods=['POST'])
        @self._admin_required
        def admin_preview_template():
            """Возвращает превью отрендеренного шаблона с тестовыми данными.

            Принимает JSON: {template, type, type_desc}.
            """
            data = request.get_json(silent=True) or {}
            template = str(data.get("template", ""))
            type_key = str(data.get("type", ""))
            type_desc = str(data.get("type_desc", ""))

            # Тестовые данные для превью
            test_call = {
                "name": "Иванов Иван",
                "equipment": "XL-75",
                "info": "Подпись тиража",
                "type": type_key,
            }
            result = render_template(template or "", test_call, type_desc)
            return jsonify({"preview": result}), 200

        # ====================================================================
        # Админка: API — Настройки TTS (голос, кэш)
        # ====================================================================

        @self.app.route('/admin/api/tts/speaker', methods=['GET'])
        @self._admin_required
        def admin_get_speaker():
            """Возвращает текущий голос и список доступных."""
            if not self.tts_service:
                return jsonify({"error": "TTS недоступен"}), 500
            current = self.tts_service.get_speaker()
            available = list(getattr(self.tts_service, 'AVAILABLE_SPEAKERS', []))
            return jsonify({
                "current": current,
                "available": available,
            }), 200

        @self.app.route('/admin/api/tts/speaker', methods=['POST'])
        @self._admin_required
        def admin_set_speaker():
            """Меняет голос TTS. Не сбрасывает кэш — это делается отдельным вызовом."""
            if not self.tts_service:
                return jsonify({"error": "TTS недоступен"}), 500
            data = request.get_json(silent=True) or {}
            speaker = str(data.get("speaker", "")).strip().lower()

            ok = self.tts_service.set_speaker(speaker)
            if not ok:
                return jsonify({
                    "error": f"Недопустимый голос. Доступные: {', '.join(getattr(self.tts_service, 'AVAILABLE_SPEAKERS', []))}"
                }), 400

            logger.info(f"Админ переключил голос TTS на: {speaker}")
            return jsonify({"status": "ok", "speaker": speaker}), 200

        @self.app.route('/admin/api/tts/cache/clear', methods=['POST'])
        @self._admin_required
        def admin_clear_tts_cache():
            """Очищает кэш TTS (удаляет все WAV-файлы)."""
            if not self.tts_service:
                return jsonify({"error": "TTS недоступен"}), 500

            removed = self.tts_service.clear_cache()
            logger.info(f"Админ очистил кэш TTS: удалено {removed} файлов")
            return jsonify({"status": "ok", "removed": removed}), 200

        # ====================================================================
        # Админка: API — Логи
        # ====================================================================

        @self.app.route('/admin/api/logs', methods=['GET'])
        @self._admin_required
        def admin_get_logs():
            try:
                tail = int(request.args.get('tail', 50))
            except (ValueError, TypeError):
                tail = 50
            tail = max(1, min(tail, 500))

            lines = self._read_log_tail(tail)
            return jsonify({
                "lines": lines,
                "tail": tail,
                "log_file": LOG_FILE,
            }), 200

        @self.app.route('/admin/api/logs/download', methods=['GET'])
        @self._admin_required
        def admin_download_log():
            if not os.path.exists(LOG_FILE):
                abort(404)
            return send_file(
                LOG_FILE,
                mimetype='text/plain',
                as_attachment=True,
                download_name=f"notifier_{datetime_now_str()}.log"
            )

        @self.app.route('/api/v1/version', methods=['GET'])
        def get_version():
            """Возвращает информацию об актуальной версии клиента.

            Клиент периодически опрашивает этот маршрут, сравнивает
            свою версию с `latest` и показывает бейдж обновления,
            если на сервере появилась более новая версия.
            """
            import hashlib
            from config import __version__
            # Папка с релизными .exe
            release_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "client")

            # Ищем последний по имени файл вида CallNotifierClient-X.Y.Z.exe
            latest_exe = None
            latest_version = __version__  # fallback — версия самого сервера
            if os.path.isdir(release_dir):
                candidates = []
                for fn in os.listdir(release_dir):
                    m = re.match(r'^CallNotifierClient-(\d+\.\d+\.\d+)\.exe$', fn)
                    if m:
                        candidates.append((m.group(1), fn))
                if candidates:
                    # Сортируем по версии (покомпонентно)
                    def version_key(v):
                        return tuple(int(p) for p in v[0].split('.'))
                    candidates.sort(key=version_key, reverse=True)
                    latest_version, latest_filename = candidates[0]
                    latest_exe = latest_filename

            result = {
                "latest": latest_version,
                "min_supported": "1.0.0",
                "url": f"/static/client/{latest_exe}" if latest_exe else None,
                "sha256": None,
            }

            # Считаем sha256 для integrity-проверки (на будущее, если пригодится)
            if latest_exe:
                exe_path = os.path.join(release_dir, latest_exe)
                try:
                    h = hashlib.sha256()
                    with open(exe_path, 'rb') as f:
                        for chunk in iter(lambda: f.read(65536), b''):
                            h.update(chunk)
                    result["sha256"] = h.hexdigest()
                except Exception as e:
                    logger.warning(f"Не удалось посчитать sha256 для {latest_exe}: {e}")

            return jsonify(result), 200
        # ====================================================================
        # Внутренние помощники
        # ====================================================================


    def _cleanup_reminder_tracker(self, active_external_ids: set, now: float):
        """Очищает трекер напоминаний от устаревших записей.

        Два типа ключей:
          * (client_id, "all_reminders") — новый формат (общее напоминание).
            Не привязан к конкретному вызову, удаляется только по времени.
          * (client_id, external_id) — старый формат (per-call).
            Может ещё оставаться в памяти после горячего обновления.
            Удаляется, если вызов закрыт или запись старше часа.

        Общее правило: записи старше часа удаляются всегда — клиент,
        вероятно, отключился и больше не вернётся (за час он бы 100%
        синхронизировался и обновил бы свой трекер).
        """
        ONE_HOUR = 3600.0
        with self._reminder_lock:
            stale_keys = []
            for key, ts in self._last_reminder_sent.items():
                _, second = key
                # Проверка по времени — общая для обоих форматов
                if (now - ts) > ONE_HOUR:
                    stale_keys.append(key)
                    continue
                # Для старого формата дополнительно проверяем, что вызов ещё активен
                if second != "all_reminders" and second not in active_external_ids:
                    stale_keys.append(key)
            for key in stale_keys:
                self._last_reminder_sent.pop(key, None)

    def _parse_post_params(self):
        params = {}
        if request.is_json:
            data = request.get_json(force=True, silent=True)
            if data:
                params.update(data)
        body = request.data.decode('utf-8', errors='ignore').strip()
        if body:
            for key, value in re.findall(r'"([^"]+)"\s*:\s*"([^"]*)"', body):
                params[key] = value
        for key in request.args:
            params[key] = request.args.get(key)
        return params

    def _read_log_tail(self, tail: int):
        """Читает последние `tail` строк из notifier.log.

        Устойчив к ротации: если файла нет — вернёт пустой список.
        Не блокирует: открывает файл на чтение, читает в deque.
        """
        if not os.path.exists(LOG_FILE):
            return []
        try:
            with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
                return list(deque(f, maxlen=tail))
        except Exception as e:
            logger.error(f"Ошибка чтения лога: {e}")
            return []

    def run(self):
        self.server = make_server(self.host, self.port, self.app, threaded=True)
        logger.info(f"HTTP-сервер запущен на {self.host}:{self.port}")
        self.server.serve_forever()

    def shutdown(self):
        if self.server:
            logger.info("Остановка HTTP-сервера...")
            self.server.shutdown()
            logger.info("HTTP-сервер остановлен.")


def datetime_now_str() -> str:
    """YYYYMMDD_HHMMSS для имени скачиваемого файла лога."""
    return time.strftime("%Y%m%d_%H%M%S")