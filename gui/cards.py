import tkinter as tk


class RoundedCard:
    def __init__(self, canvas, x, y, width, height, radius=25, **kwargs):
        self.canvas = canvas
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.radius = radius
        self.rect_items = []
        self.text_items = []
        self._draw(**kwargs)

    def _draw(self, fill="#2e7d32", outline="#555555", lines=None, fonts=None, text_color="#ffffff"):
        if lines is None:
            lines = []
        if fonts is None:
            fonts = [("Arial", 30, "bold")] * len(lines)

        for item in self.rect_items + self.text_items:
            self.canvas.delete(item)
        self.rect_items = []
        self.text_items = []

        r = self.radius
        x1, y1 = self.x, self.y
        x2, y2 = self.x + self.width, self.y + self.height

        # Рисуем скругленные углы и основу
        self.rect_items.append(
            self.canvas.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90, style="pieslice", fill=fill,
                                   outline=fill))
        self.rect_items.append(
            self.canvas.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90, style="pieslice", fill=fill,
                                   outline=fill))
        self.rect_items.append(
            self.canvas.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90, style="pieslice", fill=fill,
                                   outline=fill))
        self.rect_items.append(
            self.canvas.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90, style="pieslice", fill=fill,
                                   outline=fill))
        self.rect_items.append(self.canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline=fill))
        self.rect_items.append(self.canvas.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline=fill))

        # Рисуем текст
        line_heights = [f[1] + 6 for f in fonts]
        total_text_height = sum(line_heights)
        start_y = (y1 + y2) / 2 - total_text_height / 2
        current_y = start_y

        for i, (line, font) in enumerate(zip(lines, fonts)):
            text_id = self.canvas.create_text(
                (x1 + x2) // 2, current_y,
                text=line, fill=text_color, font=font, anchor="n"
            )
            self.text_items.append(text_id)
            current_y += line_heights[i]

    def update_fill(self, fill):
        for item in self.rect_items:
            if self.canvas.type(item) in ("arc", "rectangle"):
                self.canvas.itemconfig(item, fill=fill, outline=fill)

    def update_lines(self, lines, fonts):
        for i, (line, font) in enumerate(zip(lines, fonts)):
            if i < len(self.text_items):
                self.canvas.itemconfig(self.text_items[i], text=line, font=font)

    def delete(self):
        for item in self.rect_items + self.text_items:
            self.canvas.delete(item)