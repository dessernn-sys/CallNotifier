import tkinter as tk
from logger import setup_logger

logger = setup_logger("gui.warehouse")


class WarehouseDisplay:
    def __init__(self, parent_frame, config):
        self.parent_frame = parent_frame
        self.config = config
        self.red_canvases = []
        self.wh_widgets = {}  # Словарь для хранения виджетов каждого склада: {key: {canvas, bar_ids, ...}}

    def update(self, warehouses):
        """Обновляет отображение складов без полного пересоздания."""
        # Очищаем список красных канвасов для анимации
        self.red_canvases.clear()

        # Получаем текущие ключи из БД
        current_keys = [wh['key'] for wh in warehouses]

        # 1. Удаляем виджеты складов, которых больше нет в БД
        keys_to_remove = [k for k in self.wh_widgets if k not in current_keys]
        for key in keys_to_remove:
            self._destroy_warehouse_widgets(key)

        # 2. Обновляем или создаем виджеты
        self.parent_frame.update_idletasks()
        total_width = self.parent_frame.winfo_width() - 40
        if total_width < 400: total_width = 1000

        num_wh = len(current_keys)
        if num_wh == 0:
            # Если складов нет, показываем заглушку
            if not hasattr(self, 'empty_label') or not self.empty_label.winfo_exists():
                self.empty_label = tk.Label(self.parent_frame, text="Наполненность склада ГП: нет данных",
                                            bg=self.config.get("display", "color_background"), fg="white",
                                            font=("Arial", 18))
                self.empty_label.pack(expand=True)
            return
        elif hasattr(self, 'empty_label') and self.empty_label.winfo_exists():
            self.empty_label.pack_forget()

        single_width = int(total_width / num_wh) - 20

        for wh in warehouses:
            key = wh['key']
            if key in self.wh_widgets:
                # ОБНОВЛЕНИЕ существующего склада
                self._update_existing_warehouse(key, wh, single_width)
            else:
                # СОЗДАНИЕ нового склада
                self._create_new_warehouse(key, wh, single_width)

    def _create_new_warehouse(self, key, wh, width):
        sub = tk.Frame(self.parent_frame, bg=self.config.get("display", "color_background"))
        sub.pack(side="left", expand=True, fill="both", padx=10)

        title = tk.Label(sub, text=f"{wh['name']}", fg="#e0e0e0",
                         bg=self.config.get("display", "color_background"), font=("Arial", 18, "bold"))
        title.pack(pady=(2, 0))

        bar_width = width
        bar_height = 36
        canvas = tk.Canvas(sub, width=bar_width + 10, height=bar_height + 10,
                           bg=self.config.get("display", "color_background"), highlightthickness=0)
        canvas.pack(pady=(2, 5))

        # Сохраняем ссылки
        self.wh_widgets[key] = {
            'frame': sub,
            'title': title,
            'canvas': canvas,
            'bg_ids': [],  # ID элементов фона
            'progress_ids': [],  # ID элементов прогресса
            'text_id': None,  # ID текста
            'width': bar_width,
            'height': bar_height
        }

        # Рисуем фон один раз
        self._draw_background(canvas, bar_width, bar_height)
        # Рисуем начальный прогресс
        self._draw_progress(key, wh)

    def _update_existing_warehouse(self, key, wh, width):
        widget_data = self.wh_widgets[key]
        canvas = widget_data['canvas']

        # Обновляем заголовок если имя изменилось
        widget_data['title'].config(text=f"{wh['name']}")

        # Перерисовываем прогресс
        self._draw_progress(key, wh)

    def _destroy_warehouse_widgets(self, key):
        if key in self.wh_widgets:
            data = self.wh_widgets[key]
            data['frame'].destroy()
            del self.wh_widgets[key]

    def _draw_background(self, canvas, w, h):
        r = 10
        x1, y1 = 5, 5
        x2, y2 = 5 + w, 5 + h
        bg_color = "#2c3e50"

        ids = []
        ids.append(
            canvas.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90, style="pieslice", fill=bg_color,
                              outline=""))
        ids.append(
            canvas.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90, style="pieslice", fill=bg_color,
                              outline=""))
        ids.append(
            canvas.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90, style="pieslice", fill=bg_color,
                              outline=""))
        ids.append(
            canvas.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90, style="pieslice", fill=bg_color,
                              outline=""))
        ids.append(canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=bg_color, outline=""))
        ids.append(canvas.create_rectangle(x1, y1 + r, x2, y2 - r, fill=bg_color, outline=""))

        # Контур
        border_color = "#555555"
        ids.append(
            canvas.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90, style="arc", outline=border_color,
                              width=2))
        ids.append(
            canvas.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90, style="arc", outline=border_color,
                              width=2))
        ids.append(
            canvas.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90, style="arc", outline=border_color,
                              width=2))
        ids.append(
            canvas.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90, style="arc", outline=border_color,
                              width=2))
        ids.append(canvas.create_line(x1 + r, y1, x2 - r, y1, fill=border_color, width=2))
        ids.append(canvas.create_line(x1 + r, y2, x2 - r, y2, fill=border_color, width=2))
        ids.append(canvas.create_line(x1, y1 + r, x1, y2 - r, fill=border_color, width=2))
        ids.append(canvas.create_line(x2, y1 + r, x2, y2 - r, fill=border_color, width=2))

        self.wh_widgets[list(self.wh_widgets.keys())[-1]][
            'bg_ids'] = ids  # Временное сохранение, потом перезапишется в _draw_progress

    def _draw_progress(self, key, wh):
        data = self.wh_widgets[key]
        canvas = data['canvas']
        w = data['width']
        h = data['height']

        # Удаляем старый прогресс
        for item_id in data.get('progress_ids', []):
            canvas.delete(item_id)
        if data.get('text_id'):
            canvas.delete(data['text_id'])

        value = wh.get("current_value", 0)
        capacity = wh.get("capacity", 1)
        orange = wh.get("orange_threshold", capacity)
        red = wh.get("red_threshold", capacity)

        is_critical = value >= red
        color = "#e74c3c" if is_critical else ("#e67e22" if value >= orange else "#2ecc71")

        fill_ratio = min(value / capacity, 1.0)
        fill_width = int(w * fill_ratio)

        r = 10
        x1, y1 = 5, 5
        x2, y2 = 5 + w, 5 + h
        fx2 = x1 + fill_width

        progress_ids = []
        if fill_width > 0:
            progress_ids.append(
                canvas.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90, style="pieslice", fill=color,
                                  outline="", tags="progress_bar"))
            progress_ids.append(
                canvas.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90, style="pieslice", fill=color,
                                  outline="", tags="progress_bar"))
            progress_ids.append(
                canvas.create_rectangle(x1 + r, y1, fx2, y2, fill=color, outline="", tags="progress_bar"))
            bar_id = canvas.create_rectangle(x1, y1 + r, fx2, y2 - r, fill=color, outline="", tags="progress_bar")
            progress_ids.append(bar_id)

            if fill_width >= w:
                progress_ids.append(
                    canvas.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90, style="pieslice", fill=color,
                                      outline="", tags="progress_bar"))
                progress_ids.append(
                    canvas.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90, style="pieslice",
                                      fill=color, outline="", tags="progress_bar"))

            if is_critical:
                self.red_canvases.append((canvas, bar_id))

        # Текст поверх всего
        text = f"{value} / {capacity}"
        text_id = canvas.create_text((w // 2) + 5, (h // 2) + 5, text=text, fill="#ffffff", font=("Arial", 15, "bold"))

        data['progress_ids'] = progress_ids
        data['text_id'] = text_id