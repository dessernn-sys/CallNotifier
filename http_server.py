import threading
import re
import os
import time
from collections import deque
from functools import wraps

from flask import Flask, request, jsonify, send_file, abort, Response
from werkzeug.serving import make_server

from database import Database
from tts_service import TTSService
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

        # Трекер последних напоминаний: {(client_id, external_id): unix_timestamp}
        # Хранится строго в памяти процесса. Каждый клиент имеет СВОЙ трекер,
        # чтобы напоминания озвучивались независимо на всех рабочих местах.
        # Ключ — кортеж (client_id, external_id).
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

            result_calls = []
            reminders_due = {}
            calls_by_type = {}
            now = time.time()

            # Множество external_id активных вызовов — для очистки трекера напоминаний
            active_external_ids = set()

            for call in calls:
                # ---------- Озвучка НОВОГО вызова (как раньше) ----------
                tts_text = f"Внимание! Новый вызов! {call['name']}, пройдите к {call['equipment']}."
                if self.tts_service and not self.tts_service.is_cached(tts_text):
                    threading.Thread(target=self.tts_service.generate_chunk, args=(tts_text,), daemon=True).start()

                audio_filename = self.tts_service.get_filename(tts_text) if self.tts_service else None
                audio_filepath = os.path.join(self.tts_service.cache_dir, audio_filename) if audio_filename else None
                audio_size = os.path.getsize(audio_filepath) if audio_filepath and os.path.exists(audio_filepath) else 0

                call['audio_filename'] = audio_filename
                call['audio_size'] = audio_size
                result_calls.append(call)

                active_external_ids.add(call['external_id'])

                # ---------- Решение о НАПОМИНАНИИ (per-client) ----------
                # 1) Сколько минут повтор для этого типа
                type_cfg = self.db._conn.execute(
                    "SELECT repeat_interval_min FROM types WHERE type = ?", (call['type'],)
                ).fetchone()
                repeat_min = type_cfg['repeat_interval_min'] if type_cfg else 5
                repeat_sec = max(60, repeat_min * 60)  # защита от нулевых интервалов

                # 2) Прошло ли достаточно с момента старта вызова
                elapsed_since_start = now - call['datetime_start']
                if elapsed_since_start < repeat_sec:
                    continue  # ещё рано напоминать, только «новый вызов» сыграли

                # 3) Прошло ли достаточно с момента ПОСЛЕДНЕГО напоминания
                #    ИМЕННО ЭТОМУ КЛИЕНТУ по этому вызову
                tracker_key = (client_id, call['external_id'])
                last_sent = self._last_reminder_sent.get(tracker_key, 0.0)
                if last_sent > 0 and (now - last_sent) < repeat_sec:
                    continue  # этому клиенту напоминали недавно — молчим

                # 4) Пора напоминать — добавляем в группу по типу
                calls_by_type.setdefault(call['type'], []).append(call)

                # 5) Помечаем «отправлено этому клиенту сейчас»
                with self._reminder_lock:
                    self._last_reminder_sent[tracker_key] = now

            # ---------- Сборка напоминаний по группам типов ----------
            if self.tts_service and calls_by_type:
                for ctype, type_calls in calls_by_type.items():
                    phrases = ["Напоминаю, список ожидания!"]
                    for c in type_calls:
                        phrases.append(f"{c['name']}, пройдите к {c['equipment']}.")
                    reminder_file = self.tts_service.build_reminder_audio(phrases, pause_sec=0.5)
                    if reminder_file:
                        reminders_due[ctype] = reminder_file

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

        Удаляются записи:
          * по вызовам, которых больше нет в active_external_ids (вызов закрыт);
          * старше 1 часа — клиент, вероятно, отключился и больше не вернётся
            (за час он бы точно синхронизировался).
        """
        ONE_HOUR = 3600.0
        with self._reminder_lock:
            stale_keys = [
                key for key, ts in self._last_reminder_sent.items()
                if key[1] not in active_external_ids or (now - ts) > ONE_HOUR
            ]
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