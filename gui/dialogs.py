import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import requests
from logger import setup_logger

logger = setup_logger("gui.dialogs")


class SettingsDialog:
    def __init__(self, parent, config, db=None, audio=None, http_server=None, warehouse_manager=None, main_app=None):
        self.parent = parent
        self.config = config
        self.db = db
        self.audio = audio
        self.http_server = http_server
        self.warehouse_manager = warehouse_manager
        self.main_app = main_app

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Настройки клиента")
        self.dialog.geometry("600x550")
        self.dialog.resizable(False, False)
        self.dialog.transient(parent)
        self.dialog.grab_set()

        # Словарь для хранения переменных чекбоксов типов вызовов {type_key: BooleanVar}
        self.type_variables = {}

        # Создаем вкладки (Notebook)
        self.notebook = ttk.Notebook(self.dialog)
        self.notebook.pack(expand=True, fill="both", padx=10, pady=10)

        # Вкладка 1: Аудио и Звук
        self.audio_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.audio_frame, text="Звук и связь")
        self._build_audio_tab()

        # Вкладка 2: Фильтр типов уведомлений
        self.filter_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.filter_frame, text="Фильтр вызовов")
        self._build_filter_tab()

        # Вкладка 3: Администрирование (показывается только на сервере)
        if self.db is not None:
            self.admin_frame = ttk.Frame(self.notebook)
            self.notebook.add(self.admin_frame, text="Администрирование")
            self._build_admin_tab()

        # Панель кнопок внизу окна
        btn_frame = ttk.Frame(self.dialog)
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btn_frame, text="Сохранить и применить", command=self._save_and_close).pack(side="right", padx=5)
        ttk.Button(btn_frame, text="Отмена", command=self.dialog.destroy).pack(side="right")

        # Загружаем текущие настройки в элементы интерфейса
        self._load_current_settings()

    def _build_audio_tab(self):
        frame = self.audio_frame

        # IP-адрес сервера
        ttk.Label(frame, text="IP-адрес сервера:").grid(row=0, column=0, padx=10, pady=10, sticky="w")
        self.host_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.host_var, width=25).grid(row=0, column=1, padx=10, pady=10, sticky="w")

        # Порт сервера
        ttk.Label(frame, text="Порт сервера:").grid(row=1, column=0, padx=10, pady=10, sticky="w")
        self.port_var = tk.IntVar()
        ttk.Spinbox(frame, from_=1024, to=65535, textvariable=self.port_var, width=10).grid(
            row=1, column=1, padx=10, pady=10, sticky="w"
        )

        # Ползунок TTL времени жизни кэша файлов
        ttk.Label(frame, text="Срок хранения кэша (дней):").grid(row=2, column=0, padx=10, pady=10, sticky="w")
        self.ttl_label_var = tk.StringVar(value="3 дня")
        self.ttl_var = tk.IntVar(value=3)

        ttl_scale = ttk.Scale(frame, from_=1, to=30, variable=self.ttl_var, orient="horizontal",
                              command=self._update_ttl_label)
        ttl_scale.grid(row=2, column=1, padx=10, pady=10, sticky="ew")

        self.ttl_label = ttk.Label(frame, textvariable=self.ttl_label_var)
        self.ttl_label.grid(row=2, column=2, padx=5, pady=10, sticky="w")

        frame.columnconfigure(1, weight=1)


    def _update_ttl_label(self, event=None):
        val = int(self.ttl_var.get())
        if val == 1:
            self.ttl_label_var.set("1 день")
        elif 2 <= val <= 4:
            self.ttl_label_var.set(f"{val} дня")
        else:
            self.ttl_label_var.set(f"{val} дней")

    def _build_filter_tab(self):
        frame = self.filter_frame
        ttk.Label(
            frame,
            text="Выберите типы вызовов, которые должны озвучиваться и отображаться на этом терминале:",
            wraplength=550,
            foreground="gray"
        ).pack(anchor="w", padx=10, pady=10)

        list_container = ttk.Frame(frame)
        list_container.pack(expand=True, fill="both", padx=10, pady=5)

        canvas = tk.Canvas(list_container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        self.scrollable_frame = ttk.Frame(canvas)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._populate_types_checkboxes()

    def _populate_types_checkboxes(self):
        """Запрашивает список типов и строит чекбоксы на основе сохраненного config.json"""
        types_list = []
        if self.audio and hasattr(self.audio, 'server_url'):
            try:
                response = requests.get(f"{self.audio.server_url}/api/v1/types", timeout=2)
                if response.status_code == 200:
                    types_list = response.json()
            except Exception as e:
                logger.warning(f"Не удалось получить типы с сервера для фильтра: {e}")

        if not types_list and self.db:
            try:
                types_list = self.db.get_all_types()
            except Exception as e:
                logger.error(f"Ошибка чтения типов из локальной БД: {e}")

        if types_list:
            # Получаем сохраненный список из конфига. Жестко приводим к типу list.
            raw_allowed = self.config.get("display", "allowed_types")
            if raw_allowed is None:
                allowed_types_config = []
            elif isinstance(raw_allowed, list):
                allowed_types_config = [str(x) for x in raw_allowed]
            elif isinstance(raw_allowed, str):
                allowed_types_config = [x.strip() for x in raw_allowed.split(",") if x.strip()]
            else:
                allowed_types_config = []

            logger.info(f"Загруженные из config_client.json типы для фильтра: {allowed_types_config}")

            for item in types_list:
                t_key = item.get("type")
                t_desc = item.get("description", t_key)

                # ЛОГИКА ОПРЕДЕЛЕНИЯ ГАЛОЧКИ:
                # Если в конфиге пусто [] — значит фильтр не настроен, показываем все галочки (True)
                # Если в конфиге есть элементы — галочка ставится ТОЛЬКО для тех, кто есть в списке
                if len(allowed_types_config) == 0:
                    is_checked = True
                else:
                    is_checked = (t_key in allowed_types_config)

                # ИСПРАВЛЕНИЕ: Жестко привязываем BooleanVar к self.dialog,
                # чтобы сборщик мусора Python не уничтожал объект переменной в памяти!
                var = tk.BooleanVar(master=self.dialog)
                var.set(is_checked)
                self.type_variables[t_key] = var

                # Рисуем чекбокс с явной привязкой к долгоживущей переменной
                chk = ttk.Checkbutton(self.scrollable_frame, text=f"{t_desc} ({t_key})", variable=var)
                chk.pack(anchor="w", padx=10, pady=3)

                # Дополнительно синхронизируем визуальное состояние виджета
                if is_checked:
                    chk.state(['selected'])
                else:
                    chk.state(['!selected'])

            logger.info("Чекбоксы фильтров успешно построены и защищены от сборщика мусора.")
        else:
            lbl = ttk.Label(self.scrollable_frame,
                            text="⚠ Нет связи с сервером.\nПодключитесь к сети, чтобы настроить фильтры.",
                            foreground="red")
            lbl.pack(pady=20, padx=20)

    def _build_admin_tab(self):
        frame = self.admin_frame
        ttk.Label(
            frame,
            text="Внимание!\nПолное управление конфигурацией базы данных, удаление типов,\n"
                 "а также принудительное закрытие карточек вызовов осуществляется\n"
                 "исключительно через расширенную Web-панель администратора сервера.",
            foreground="blue", justify="center", font=("Arial", 11, "bold"), wraplength=450
        ).pack(pady=30)

        if self.audio and hasattr(self.audio, 'server_url'):
            url = f"{self.audio.server_url}/admin"
            lbl_link = ttk.Label(frame, text=f"Адрес панели: {url}", font=("Arial", 12, "underline"), cursor="hand2", foreground="green")
            lbl_link.pack(pady=10)
            import webbrowser
            lbl_link.bind("<Button-1>", lambda e: webbrowser.open(url))

    def _load_current_settings(self):
        self.host_var.set(self.config.get("http_server", "host", default="127.0.0.1"))
        self.port_var.set(self.config.get("http_server", "port", default=5499))

        current_ttl = self.config.get("display", "cache_ttl_days", default=3)
        self.ttl_var.set(current_ttl)
        self._update_ttl_label()

    def _save_and_close(self):
        old_host = self.config.get("http_server", "host")
        old_port = self.config.get("http_server", "port")

        new_host = self.host_var.get().strip()
        new_port = self.port_var.get()
        new_ttl = int(self.ttl_var.get())

        self.config.set(new_host, "http_server", "host")
        self.config.set(new_port, "http_server", "port")
        self.config.set(new_ttl, "display", "cache_ttl_days")

        # Собираем только те ключи типов, напротив которых стоит галочка True
        selected_types = [t_key for t_key, var in self.type_variables.items() if var.get()]

        # Если выбраны абсолютно ВСЕ чекбоксы — сохраняем пустой массив (эквивалент "принимать всё подряд")
        if len(selected_types) == len(self.type_variables):
            self.config.set([], "display", "allowed_types")
        else:
            self.config.set(selected_types, "display", "allowed_types")

        self.config.save()
        logger.info("Конфигурация успешно сохранена локально в config_client.json.")

        if self.main_app and hasattr(self.main_app, 'update_display'):
            self.main_app.update_display()

        self.dialog.destroy()