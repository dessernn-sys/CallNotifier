import tkinter as tk
import time
from datetime import datetime
import os
import math
from .cards import RoundedCard
from .warehouse_display import WarehouseDisplay
from .dialogs import SettingsDialog
from logger import setup_logger

logger = setup_logger("gui.main")

class CallInfo:
    """Универсальная обертка: безопасно принимает и словарь (от сервера), и объект."""
    def __init__(self, data):
        if isinstance(data, dict):
            self.id = data.get("id")
            self.external_id = data.get("external_id")
            self.type = data.get("type", "unknown")
            self.name = data.get("name", "")
            self.equipment = data.get("equipment", "")
            self.info = data.get("info", "")
            self.status = data.get("status", "open")
            self.datetime_start = data.get("datetime_start", time.time())
            self.datetime_stop = data.get("datetime_stop")
        else:
            self.id = getattr(data, 'id', None)
            self.external_id = getattr(data, 'external_id', None)
            self.type = getattr(data, 'type', "unknown")
            self.name = getattr(data, 'name', "")
            self.equipment = getattr(data, 'equipment', "")
            self.info = getattr(data, 'info', "")
            self.status = getattr(data, 'status', "open")
            self.datetime_start = getattr(data, 'datetime_start', time.time())
            self.datetime_stop = getattr(data, 'datetime_stop', None)


class FullScreenApp:
    def __init__(self, root, config, db=None, audio=None, http_server=None, warehouse_manager=None):
        self.root = root
        self.config = config
        self.db = db
        self.audio = audio
        self.http_server = http_server
        self.warehouse_manager = warehouse_manager
        
        self.current_calls = []
        self._blink_after_id = None
        self.cards = {}
        self._logo_image = None
        self.glow_step = 0

        bg_color = self.config.get("display", "color_background", default="#1e1e1e")
        self.root.title("Call Notifier")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg=bg_color)
        self.root.bind("<Escape>", self._toggle_fullscreen)
        self.root.bind("<F1>", lambda e: self.open_settings())

        # Хедер
        self.header_frame = tk.Frame(self.root, bg="#2b2b2b", height=70)
        self.header_frame.pack(fill="x", padx=10, pady=(10, 0))
        self.header_frame.pack_propagate(False)
        self.header_frame.columnconfigure(0, weight=1)
        self.header_frame.columnconfigure(1, weight=2)
        self.header_frame.columnconfigure(2, weight=1)

        self.logo_label = tk.Label(self.header_frame, bg="#2b2b2b")
        self.logo_label.grid(row=0, column=0, sticky="w", padx=10)
        self._load_logo()

        self.header_title = tk.Label(self.header_frame, text="Ожидание вызовов", fg="white", bg="#2b2b2b", font=("Arial", 22, "bold"))
        self.header_title.grid(row=0, column=1, pady=5)

        right_frame = tk.Frame(self.header_frame, bg="#2b2b2b")
        right_frame.grid(row=0, column=2, sticky="e", padx=10)
        self.indicator_canvas = tk.Canvas(right_frame, width=14, height=14, bg="#2b2b2b", highlightthickness=0)
        self.indicator_canvas.pack(side="top", pady=(5, 0))
        self.indicator_dot = self.indicator_canvas.create_oval(2, 2, 12, 12, fill="#00ff00", outline="")
        self.header_time = tk.Label(right_frame, text="", fg="white", bg="#2b2b2b", font=("Arial", 16))
        self.header_time.pack(side="top")

        # Бейдж «доступно обновление» — живёт в правом фрейме, под часами.
        # По умолчанию скрыт (не пакуем). Появится при обнаружении новой версии.
        self.update_badge = tk.Label(
            right_frame,
            text="",
            fg="#1e1e1e",
            bg="#ffb300",  # янтарный
            font=("Arial", 11, "bold"),
            padx=8, pady=2,
        )

        self._latest_download_url = None

        # Область карточек
        self.main_frame = tk.Frame(self.root, bg=bg_color)
        self.main_frame.pack(expand=True, fill="both", padx=20, pady=(10, 5))
        self.canvas = tk.Canvas(self.main_frame, bg=bg_color, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(self.main_frame, orient="vertical", command=self.canvas.yview)
        self.scroll_frame = tk.Frame(self.canvas, bg=bg_color)
        self.scroll_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # Хедер складов
        self.wh_header_frame = tk.Frame(self.root, bg="#2b2b2b", height=40)
        self.wh_header_frame.pack(fill="x", padx=10, pady=(10, 0))
        self.wh_header_frame.pack_propagate(False)
        self.wh_header_title = tk.Label(self.wh_header_frame, text="Заполняемость складов ГП", fg="white", bg="#2b2b2b", font=("Arial", 18, "bold"))
        self.wh_header_title.pack(expand=True)

        # Панель складов
        self.warehouse_frame = tk.Frame(self.root, bg=bg_color, height=160)
        self.warehouse_frame.pack(fill="x", side="bottom", padx=20, pady=(0, 10))
        self.warehouse_frame.pack_propagate(False)
        self.warehouse_display = WarehouseDisplay(self.warehouse_frame, self.config)

        # Привязка callback-ов
        if self.audio:
            self.audio.set_connection_callback(self._set_connection_status)
            self.audio.set_calls_update_callback(self._on_calls_updated)
            self.audio.set_version_callback(self._on_version_available)
            self._on_calls_updated(self.audio.get_current_calls())
        else:
            self.root.after(1000, self._refresh_calls)

        self.root.after(2000, self._refresh_warehouses)
        self._start_blinking()
        self._update_header_time()
        self._animate_glow()

    def _on_calls_updated(self, calls_data):
        self.current_calls = [CallInfo(c) for c in calls_data]
        self.update_display()

    def _refresh_calls(self):
        if self.db:
            try:
                rows = self.db.get_open_calls()
                self.current_calls = [CallInfo(r) for r in rows]
            except Exception as e:
                logger.error(f"Ошибка чтения вызовов: {e}")
        self.update_display()
        self.root.after(1000, self._refresh_calls)

    def _refresh_warehouses(self):
        if self.audio and hasattr(self.audio, 'get_warehouses'):
            warehouses = self.audio.get_warehouses()
        elif self.warehouse_manager and hasattr(self.warehouse_manager, 'warehouses'):
            warehouses = list(self.warehouse_manager.warehouses.values())
        elif self.db:
            warehouses = self.db.get_warehouses()
        else:
            warehouses = []

        if self.warehouse_display:
            self.warehouse_display.update(warehouses)

        # Скрываем/показываем весь блок в зависимости от наличия данных
        self._update_warehouse_visibility(bool(warehouses))

        self.root.after(2000, self._refresh_warehouses)

    def _update_warehouse_visibility(self, has_warehouses: bool):
        if has_warehouses:
            self.wh_header_frame.configure(height=40)
            self.warehouse_frame.configure(height=160)
            self.wh_header_title.configure(text="Заполняемость складов ГП")
        else:
            self.wh_header_frame.configure(height=1)
            self.warehouse_frame.configure(height=1)
            self.wh_header_title.configure(text="")

    def _toggle_fullscreen(self, event=None):
        current = self.root.attributes("-fullscreen")
        self.root.attributes("-fullscreen", not current)

    def _load_logo(self):
        logo_path = self.config.get("display", "logo_path")
        if logo_path and os.path.exists(logo_path):
            try:
                from PIL import Image, ImageTk
                img = Image.open(logo_path)
                ratio = 70 / img.height
                img = img.resize((int(img.width * ratio), 70), Image.LANCZOS)
                self._logo_image = ImageTk.PhotoImage(img)
                self.logo_label.config(image=self._logo_image, text="")
            except Exception as e:
                logger.error(f"Ошибка загрузки логотипа: {e}")
                self.logo_label.config(image="", text="Логотип")
        else:
            self.logo_label.config(image="", text="")

    def update_display(self):
        if self.current_calls:
            self.header_title.config(text="Список вызовов")
        else:
            self.header_title.config(text="Ожидание вызовов")
            
        for card in self.cards.values():
            card.delete()
        self.cards.clear()
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()
            
        if not self.current_calls:
            return
            
        sorted_calls = sorted(self.current_calls, key=lambda c: c.datetime_start)
        max_calls = self.config.get("display", "max_calls_on_screen")
        num_calls = min(len(sorted_calls), max_calls)
        base_font_size = max(24, 60 - num_calls * 2)
        
        self.root.update_idletasks()
        canvas_width = self.canvas.winfo_width() - 20
        if canvas_width < 400: canvas_width = 800
        
        name_height = base_font_size + 6
        eq_height = int(base_font_size * 0.8) + 6
        time_height = int(base_font_size * 0.5) + 6
        card_height = name_height + eq_height + time_height + 20
        spacing = 15
        
        inner_canvas = tk.Canvas(self.scroll_frame, width=canvas_width, height=num_calls * (card_height + spacing) + 20, bg=self.config.get("display", "color_background"), highlightthickness=0)
        inner_canvas.pack()
        
        for i, call in enumerate(sorted_calls[:num_calls]):
            y = 10 + i * (card_height + spacing)
            lines = [call.name, f"{call.equipment}", f"Ожидание: {int((datetime.now().timestamp() - call.datetime_start) / 60)} мин."]
            fonts = [("Arial", base_font_size, "bold"), ("Arial", int(base_font_size * 0.8), "normal"), ("Arial", int(base_font_size * 0.5), "normal")]
            
            card = RoundedCard(inner_canvas, 10, y, canvas_width - 20, card_height, radius=25, fill=self._get_color_for_call(call), lines=lines, fonts=fonts, text_color="#ffffff")
            self.cards[call.id] = card
            
        if len(sorted_calls) > max_calls:
            tk.Label(self.scroll_frame, text=f"+ ещё {len(sorted_calls) - max_calls} вызовов", fg="gray", bg=self.config.get("display", "color_background"), font=("Arial", 20)).pack()

    def _get_color_for_call(self, call):
        now = datetime.now().timestamp()
        elapsed = (now - call.datetime_start) / 60
        if elapsed < self.config.get("display", "green_threshold_minutes"):
            return self.config.get("display", "color_green")
        elif elapsed < self.config.get("display", "orange_threshold_minutes"):
            return self.config.get("display", "color_orange")
        return self.config.get("display", "color_red")

    def _update_header_time(self):
        self.header_time.config(text=datetime.now().strftime("%H:%M:%S"))
        self.root.after(1000, self._update_header_time)

    def _set_connection_status(self, is_connected: bool):
        if is_connected:
            self.indicator_canvas.itemconfig(self.indicator_dot, fill="#00ff00")
            self.header_title.config(text="Список вызовов" if self.current_calls else "Ожидание вызовов", fg="white")
        else:
            self.indicator_canvas.itemconfig(self.indicator_dot, fill="#ff0000")
            self.header_title.config(text="⚠ Нет соединения с сервером", fg="#ff0000")

    def _start_blinking(self):
        self._blink_reds()
        self._blink_after_id = self.root.after(500, self._start_blinking)

    def _blink_reds(self):
        now = datetime.now().timestamp()
        for call in self.current_calls:
            if call.id in self.cards:
                elapsed = (now - call.datetime_start) / 60
                if elapsed >= self.config.get("display", "orange_threshold_minutes"):
                    card = self.cards[call.id]
                    current_fill = self._get_color_for_call(call)
                    card.update_fill("#ff0000" if int(now * 2) % 2 == 0 else current_fill)

    def _animate_glow(self):
        self.glow_step += 0.15
        amplitude = (math.sin(self.glow_step) + 1) / 2
        r_val = int(231 + (24 - 231) * amplitude)
        g_val = int(76 + (15 - 76) * amplitude)
        b_val = int(60 + (30 - 60) * amplitude)
        glow_color = f"#{r_val:02x}{g_val:02x}{b_val:02x}"
        
        if self.warehouse_display:
            for canvas, bar_id in getattr(self.warehouse_display, 'red_canvases', []):
                try:
                    for item in canvas.find_withtag("progress_bar"):
                        canvas.itemconfig(item, fill=glow_color, outline="")
                except Exception:
                    pass
        self.root.after(60, self._animate_glow)

    def open_settings(self):
        SettingsDialog(self.root, self.config, self.db, self.audio, self.http_server, self.warehouse_manager, self)

    def _on_version_available(self, latest_version: str, download_url: str):
        """Вызывается из AudioManager, когда сервер сообщает о доступной версии.

        Показывает бейдж в правом фрейме хедера, если серверная версия
        СТРОГО больше локальной.
        """
        from config import __version__ as current_version

        # Показываем бейдж только если серверная версия СТРОГО больше локальной
        if self._version_tuple(latest_version) <= self._version_tuple(current_version):
            self._hide_update_badge()
            return

        self.update_badge.configure(
            text=f"🔔 Доступно обновление {latest_version}",
            cursor="hand2",
        )
        self.update_badge.pack(side="top", pady=(4, 0))

        # Запомним URL — пригодится, если сделаем клик
        self._latest_download_url = download_url

    def _hide_update_badge(self):
        """Скрывает бейдж обновления, если он показан."""
        try:
            self.update_badge.pack_forget()
        except Exception:
            pass

    @staticmethod
    def _version_tuple(v: str):
        """'1.1.0' → (1, 1, 0). Для безопасного сравнения версий."""
        try:
            return tuple(int(p) for p in v.split('.'))
        except Exception:
            return (0,)