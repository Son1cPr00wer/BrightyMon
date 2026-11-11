#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Brightness & Blue-Light Tray Controller
=======================================

Лёгкое приложение для регулировки яркости монитора и синего канала
("режим чтения / отдых для глаз").

Возможности:
  * Иконка в системном трее.
  * Клик левой кнопкой мыши по иконке открывает/закрывает окно
    с вертикальными слайдерами (яркость, синий).
  * Яркость регулируется программной гаммой от 10% до 100%
    (без WMI-аппаратной яркости — она имеет порог ~50% и «залипала»).
  * Синий канал: только от 70% до 100% (мягкая коррекция, отдых для глаз).
  * Пресеты: «Чтение» (80/90), «Отдых глаз» (90/70), «Сброс» (100/100).

Зависимости:
  pip install pystray Pillow

Запуск:
  python brightness_tray.py
"""

import sys
import shutil
import math
import ctypes
import subprocess
import threading

import tkinter as tk
from tkinter import ttk

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit(
        "Не найдены зависимости. Установите их командой:\n"
        "    pip install pystray Pillow"
    )

APP_TITLE = "Brightness Control"
MIN_LEVEL = 10          # нижняя граница, чтобы не оставить экран совсем чёрным
MAX_LEVEL = 100
MIN_BLUE = 70           # регулировка синего: только от 70% до 100%
MAX_BLUE = 100
DEFAULT_BRIGHTNESS = 100
DEFAULT_BLUE = 100

# Пресеты быстрых кнопок (яркость %, синий %)
PRESET_READ = (80, 90)   # «Чтение»
PRESET_REST = (90, 70)   # «Отдых глаз»


# ---------------------------------------------------------------------------
# Платформенные контроллеры
# ---------------------------------------------------------------------------

class _GammaRamp(ctypes.Structure):
    _fields_ = [("r", ctypes.c_ushort * 256),
                ("g", ctypes.c_ushort * 256),
                ("b", ctypes.c_ushort * 256)]


class WindowsController:
    """Управление яркостью и синим каналом на Windows.

    Программная гамма через GDI SetDeviceGammaRamp — работает для любого
    дисплея, плавно покрывает весь диапазон 10–100 % и позволяет отдельно
    регулировать синий канал (70–100 %).
    """

    name = "Windows (gamma)"

    def __init__(self):
        self._brightness = DEFAULT_BRIGHTNESS
        self._blue = DEFAULT_BLUE
        # GDI handles
        self._user32 = ctypes.windll.user32
        self._gdi32 = ctypes.windll.gdi32
        self._SetDeviceGammaRamp = self._gdi32.SetDeviceGammaRamp
        self._GetDC = self._user32.GetDC
        self._ReleaseDC = self._user32.ReleaseDC

    # --- программная гамма -------------------------------------------------
    def _apply_gamma(self):
        """Ставит гамма-таблицу через GDI (работает всегда)."""
        b = max(MIN_LEVEL, min(MAX_LEVEL, self._brightness)) / 100.0
        bl = max(MIN_BLUE, min(MAX_BLUE, self._blue)) / 100.0

        ramp = _GammaRamp()
        for i in range(256):
            base = int((i / 255.0) * 65535)       # линейная шкала
            ramp.r[i] = min(65535, int(base * b))
            ramp.g[i] = min(65535, int(base * b))
            ramp.b[i] = min(65535, int(base * b * bl))

        hdc = self._GetDC(None)
        self._SetDeviceGammaRamp(hdc, ctypes.byref(ramp))
        self._ReleaseDC(None, hdc)

    def set_brightness(self, percent: int):
        self._brightness = int(max(MIN_LEVEL, min(MAX_LEVEL, percent)))
        # Гамма — основной способ: она плавно работает до MIN_LEVEL.
        # Раньше поверх гаммы вызывалась WmiSetBrightness с процентом UI
        # напрямую; мониторы часто имеют аппаратный минимум ~50%, и ниже
        # него значение просто игнорировалось — из-за этого яркость
        # «залипала» на ~51%. Теперь аппаратная яркость не используется,
        # всё регулирование делает корректная программная гамма.
        self._apply_gamma()

    def set_blue(self, percent: int):
        self._blue = int(max(MIN_BLUE, min(MAX_BLUE, percent)))
        self._apply_gamma()

    def reset(self):
        self._brightness = MAX_LEVEL
        self._blue = MAX_BLUE
        self._apply_gamma()


class LinuxController:
    """Управление через xrandr (гамма). Если xrandr нет — только UI."""

    name = "Linux (xrandr gamma)"

    def __init__(self):
        self._brightness = DEFAULT_BRIGHTNESS
        self._blue = DEFAULT_BLUE
        self._has_xrandr = shutil.which("xrandr") is not None
        self._outputs = self._list_outputs() if self._has_xrandr else []

    def _list_outputs(self):
        try:
            out = subprocess.check_output(
                ["xrandr"], stderr=subprocess.DEVNULL).decode()
            connected = []
            for line in out.splitlines():
                if " connected" in line:
                    connected.append(line.split()[0])
            return connected
        except Exception:
            return []

    def _apply(self):
        if not self._has_xrandr or not self._outputs:
            return
        b = max(MIN_LEVEL, min(MAX_LEVEL, self._brightness)) / 100.0
        bl = max(MIN_BLUE, min(MAX_BLUE, self._blue)) / 100.0
        gamma_str = "%.2f:%.2f:%.2f" % (b, b, b * bl)
        for output in self._outputs:
            try:
                subprocess.run(
                    ["xrandr", "--output", output, "--gamma", gamma_str],
                    check=False, capture_output=True)
            except Exception:
                pass

    def set_brightness(self, percent: int):
        self._brightness = int(max(MIN_LEVEL, min(MAX_LEVEL, percent)))
        self._apply()

    def set_blue(self, percent: int):
        self._blue = int(max(MIN_BLUE, min(MAX_BLUE, percent)))
        self._apply()

    def reset(self):
        self._brightness = MAX_LEVEL
        self._blue = MAX_BLUE
        self._apply()


class MacOSController:
    """macOS: яркость через Utility (osascript/brighterc), синий — f.lux-подобный
    фильтр через встроенную настройку Night Shift недоступна из коробки,
    поэтому используем сторонние утилиты если есть, иначе only-ui."""

    name = "macOS (best effort)"

    def __init__(self):
        self._brightness = DEFAULT_BRIGHTNESS
        self._blue = DEFAULT_BLUE
        self._has_brighter = shutil.which("brighter") is not None

    def set_brightness(self, percent: int):
        self._brightness = percent
        if self._has_brighter:
            try:
                subprocess.run(["brighter", "-A", str(percent)],
                               check=False, capture_output=True)
            except Exception:
                pass

    def set_blue(self, percent: int):
        self._blue = percent

    def reset(self):
        self.set_brightness(100)
        self.set_blue(100)


def make_controller():
    if sys.platform.startswith("win"):
        return WindowsController()
    if sys.platform == "darwin":
        return MacOSController()
    return LinuxController()


# ---------------------------------------------------------------------------
# UI: вертикальные слайдеры
# ---------------------------------------------------------------------------

class SliderPanel:
    def __init__(self, root, controller):
        self.controller = controller
        self.root = root
        self.suppress = False  # защита от рекурсии событий

        root.title(APP_TITLE)
        root.attributes("-topmost", True)
        root.resizable(False, False)

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        main = ttk.Frame(root, padding=12)
        main.grid(row=0, column=0, sticky="nsew")
        root.grid_rowconfigure(0, weight=1)
        root.grid_columnconfigure(0, weight=1)

        # Заголовки
        ttk.Label(main, text="Яркость", anchor="center").grid(
            row=0, column=0, padx=10)
        ttk.Label(main, text="Синий", anchor="center").grid(
            row=0, column=1, padx=10)

        # Вертикальные слайдеры
        self.var_b = tk.IntVar(value=DEFAULT_BRIGHTNESS)
        self.var_bl = tk.IntVar(value=DEFAULT_BLUE)

        self.s_b = ttk.Scale(
            main, from_=MAX_LEVEL, to=MIN_LEVEL, orient="vertical",
            variable=self.var_b,
            command=lambda *_: self.on_change())
        self.s_bl = ttk.Scale(
            main, from_=MAX_BLUE, to=MIN_BLUE, orient="vertical",
            variable=self.var_bl,
            command=lambda *_: self.on_change())
        self.s_b.grid(row=1, column=0, pady=(4, 4), sticky="ns")
        self.s_bl.grid(row=1, column=1, pady=(4, 4), sticky="ns")

        # Значения
        self.lbl_b = ttk.Label(main, text=f"{DEFAULT_BRIGHTNESS}%",
                               anchor="center")
        self.lbl_bl = ttk.Label(main, text=f"{DEFAULT_BLUE}%",
                                anchor="center")
        self.lbl_b.grid(row=2, column=0, padx=10, pady=(0, 8))
        self.lbl_bl.grid(row=2, column=1, padx=10, pady=(0, 8))

        # Быстрые пресеты (яркость + синий)
        presets = ttk.Frame(main)
        presets.grid(row=3, column=0, columnspan=2, sticky="ew")
        ttk.Button(presets, text="Отдых глаз",
                   command=lambda: self.apply_preset(*PRESET_REST)).pack(
                       side="left", expand=True, fill="x", padx=2)
        ttk.Button(presets, text="Чтение",
                   command=lambda: self.apply_preset(*PRESET_READ)).pack(
                       side="left", expand=True, fill="x", padx=2)
        ttk.Button(presets, text="Сброс",
                   command=self.reset_all).pack(
                       side="left", expand=True, fill="x", padx=2)

        # Окно ведёт себя как поповер из трея: прячем при потере фокуса.
        # bind_class, чтобы событие ловилось на любом дочернем виджете.
        root.bind_class("Tk", "<FocusOut>", lambda e: self.hide(), add="+")
        root.protocol("WM_DELETE_WINDOW", self.hide)

        self.visible = False

    # -- callbacks ---------------------------------------------------------
    def on_change(self):
        if self.suppress:
            return
        b = int(round(self.var_b.get()))
        bl = int(round(self.var_bl.get()))
        self.lbl_b.config(text=f"{b}%")
        self.lbl_bl.config(text=f"{bl}%")
        self.controller.set_brightness(b)
        self.controller.set_blue(bl)

    def apply_preset(self, brightness, blue):
        """Применяет пресет: двигает слайдеры и отдаёт значения контроллеру."""
        self.suppress = True
        try:
            self.var_b.set(brightness)
            self.var_bl.set(blue)
        finally:
            self.suppress = False
        self.lbl_b.config(text=f"{brightness}%")
        self.lbl_bl.config(text=f"{blue}%")
        # один вызов на каждый канал — контроллер применит гамму целиком
        self.controller.set_brightness(brightness)
        self.controller.set_blue(blue)

    def reset_all(self):
        self.apply_preset(DEFAULT_BRIGHTNESS, DEFAULT_BLUE)
        self.controller.reset()

    # -- show/hide ---------------------------------------------------------
    def position_near_tray(self):
        """Размещаем окно в правом нижнем углу (рядом с треем)."""
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = sw - w - 20
        y = sh - h - 60
        self.root.geometry(f"+{max(0,x)}+{max(0,y)}")

    def show(self):
        self.position_near_tray()
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        self.visible = True

    def hide(self):
        self.root.withdraw()
        self.visible = False

    def toggle(self):
        if self.visible:
            self.hide()
        else:
            self.show()


# ---------------------------------------------------------------------------
# Иконка трея
# ---------------------------------------------------------------------------

def make_icon_image(dimmed=False):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # солнце: круг + лучи
    cx = cy = size // 2
    r = 14
    color = (255, 200, 60, 255) if not dimmed else (180, 140, 40, 255)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    for ang_deg in range(0, 360, 45):
        a = math.radians(ang_deg)
        x1 = cx + math.cos(a) * (r + 5)
        y1 = cy + math.sin(a) * (r + 5)
        x2 = cx + math.cos(a) * (r + 12)
        y2 = cy + math.sin(a) * (r + 12)
        d.line([x1, y1, x2, y2], fill=color, width=4)
    return img


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------

class TrayApp:
    def __init__(self):
        self.controller = make_controller()
        self.root = tk.Tk()
        self.root.withdraw()  # стартуем скрытыми
        self.panel = SliderPanel(self.root, self.controller)
        self.icon = pystray.Icon(
            name=APP_TITLE,
            icon=make_icon_image(),
            title=APP_TITLE,  # только ASCII: pystray/X11 кодирует имя в latin-1
            menu=self._build_menu(),
            default_action=self.on_icon_click,
        )
        # при выходе восстанавливаем яркость? Нет — оставляем как есть,
        # но добавим пункт Reset в меню.

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("Показать/скрыть панель",
                             self.on_icon_click, default=True),
            pystray.MenuItem("Макс. яркость",
                             lambda: self._quick(100)),
            pystray.MenuItem("Ночной режим",
                             lambda: self._night()),
            pystray.MenuItem("Сброс настроек экрана",
                             lambda: self._reset()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self.on_exit),
        )

    def _quick(self, value):
        self.panel.apply_preset(value, int(round(
            self.panel.var_bl.get())))

    def _night(self):
        self.panel.apply_preset(*PRESET_REST)

    def _reset(self):
        self.panel.reset_all()

    def on_icon_click(self, icon=None, item=None):
        # pystray крутит свой поток; Tk-виджеты обновляем через after()
        self.root.after(0, self.panel.toggle)

    def on_exit(self, icon=None, item=None):
        try:
            icon.stop()
        finally:
            self.root.after(0, self.root.quit)


def main():
    app = TrayApp()
    # трей в отдельном потоке, Tk — главный цикл
    t = threading.Thread(target=app.icon.run, daemon=True)
    t.start()
    try:
        app.root.mainloop()
    finally:
        app.icon.stop()


if __name__ == "__main__":
    main()
