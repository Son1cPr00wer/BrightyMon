#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Brightness & Blue-Light Tray Controller
=======================================
"""

import sys
import shutil
import math
import ctypes
import subprocess
import threading
import json
import os
import webbrowser

import tkinter as tk
from tkinter import ttk
from tkinter import colorchooser

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit(
        "Не найдены зависимости. Установите их командой:\n"
        "    pip install pystray Pillow"
    )

APP_TITLE = "BrightyMon"
MIN_LEVEL = 20
MAX_LEVEL = 100
MIN_BLUE = 80
MAX_BLUE = 100
DEFAULT_BRIGHTNESS = 100
DEFAULT_BLUE = 100

CONFIG_FILE = "brightymon_settings.json"

# Цвета по умолчанию из вашей версии + дефолтные значения пресетов
DEFAULT_SETTINGS = {
    "bg_color": "#A100FF",
    "fg_color": "#FFFFFF",
    "btn_bg": "#B6007F",
    "btn_active_bg": "#57007F",
    "btn_pressed_bg": "#A100FF",
    "btn_border": "#FFFFFF",
    "slider_trough": "#B6007F",
    "slider_color": "#57007F",
    "slider_border": "#FFFFFF",
    "preset_care_b": 70,
    "preset_care_bl": 80,
    "preset_read_b": 80,
    "preset_read_bl": 90
}

def load_settings():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_SETTINGS, **json.load(f)}
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(settings):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=4)

# ---------------------------------------------------------------------------
# Платформенные контроллеры
# ---------------------------------------------------------------------------

class _GammaRamp(ctypes.Structure):
    _fields_ = [("r", ctypes.c_ushort * 256),
                ("g", ctypes.c_ushort * 256),
                ("b", ctypes.c_ushort * 256)]

class WindowsController:
    name = "Windows (gamma)"

    def __init__(self):
        self._brightness = DEFAULT_BRIGHTNESS
        self._blue = DEFAULT_BLUE
        self._user32 = ctypes.windll.user32
        self._gdi32 = ctypes.windll.gdi32
        self._SetDeviceGammaRamp = self._gdi32.SetDeviceGammaRamp
        self._GetDC = self._user32.GetDC
        self._ReleaseDC = self._user32.ReleaseDC

    def _apply_gamma(self):
        b = max(MIN_LEVEL, min(MAX_LEVEL, self._brightness)) / 100.0
        bl = max(MIN_BLUE, min(MAX_BLUE, self._blue)) / 100.0

        ramp = _GammaRamp()
        for i in range(256):
            base = int((i / 255.0) * 65535)
            ramp.r[i] = min(65535, int(base * b))
            ramp.g[i] = min(65535, int(base * b))
            ramp.b[i] = min(65535, int(base * b * bl))

        hdc = self._GetDC(None)
        self._SetDeviceGammaRamp(hdc, ctypes.byref(ramp))
        self._ReleaseDC(None, hdc)

    def set_brightness(self, percent: int):
        self._brightness = int(max(MIN_LEVEL, min(MAX_LEVEL, percent)))
        self._apply_gamma()

    def set_blue(self, percent: int):
        self._blue = int(max(MIN_BLUE, min(MAX_BLUE, percent)))
        self._apply_gamma()

    def reset(self):
        self._brightness = MAX_LEVEL
        self._blue = MAX_BLUE
        self._apply_gamma()

class LinuxController:
    name = "Linux (xrandr gamma)"

    def __init__(self):
        self._brightness = DEFAULT_BRIGHTNESS
        self._blue = DEFAULT_BLUE
        self._has_xrandr = shutil.which("xrandr") is not None
        self._outputs = self._list_outputs() if self._has_xrandr else []

    def _list_outputs(self):
        try:
            out = subprocess.check_output(["xrandr"], stderr=subprocess.DEVNULL).decode()
            return [line.split()[0] for line in out.splitlines() if " connected" in line]
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
                subprocess.run(["xrandr", "--output", output, "--gamma", gamma_str],
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
    name = "macOS (best effort)"

    def __init__(self):
        self._brightness = DEFAULT_BRIGHTNESS
        self._blue = DEFAULT_BLUE
        self._has_brighter = shutil.which("brighter") is not None

    def set_brightness(self, percent: int):
        self._brightness = percent
        if self._has_brighter:
            try:
                subprocess.run(["brighter", "-A", str(percent)], check=False, capture_output=True)
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
# UI: Главная панель
# ---------------------------------------------------------------------------

class SliderPanel:
    def __init__(self, root, controller, settings):
        self.controller = controller
        self.root = root
        self.settings = settings
        self.suppress = False

        root.title(APP_TITLE)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.resizable(False, False)

        def start_move(event):
            root.x = event.x
            root.y = event.y
        def stop_move(event):
            root.x = None
            root.y = None
        def do_move(event):
            deltax = event.x - root.x
            deltay = event.y - root.y
            x = root.winfo_x() + deltax
            y = root.winfo_y() + deltay
            root.geometry(f"+{x}+{y}")
            
        title_bar = ttk.Frame(root, height=20, cursor="fleur")
        title_bar.grid(row=0, column=0, sticky="ew")

        self.lbl_title = ttk.Label(title_bar, text="BrightyMon", font=("Segoe UI", 10, "bold italic"), cursor="fleur")
        self.lbl_title.pack(side="left", padx=5)

        for widget in (title_bar, self.lbl_title):
            widget.bind("<ButtonPress-1>", start_move)
            widget.bind("<ButtonRelease-1>", stop_move)
            widget.bind("<B1-Motion>", do_move)

        self.style = ttk.Style(root)

        main = ttk.Frame(root, padding=2)
        main.grid(row=1, column=0, sticky="nsew")
        root.grid_rowconfigure(1, weight=1)
        root.grid_columnconfigure(0, weight=1)
        
        main.grid_columnconfigure(0, weight=1, uniform="col")
        main.grid_columnconfigure(1, weight=1, uniform="col")

        self.lbl_head_b = ttk.Label(main, text="Bright", anchor="center")
        self.lbl_head_bl = ttk.Label(main, text="Blue", anchor="center")
        self.lbl_head_b.grid(row=0, column=0, padx=10)
        self.lbl_head_bl.grid(row=0, column=1, padx=10)

        self.var_b = tk.IntVar(value=DEFAULT_BRIGHTNESS)
        self.var_bl = tk.IntVar(value=DEFAULT_BLUE)

        # showvalue=0 убирает встроенные цифры
        self.s_b = tk.Scale(
            main, from_=MAX_LEVEL, to=MIN_LEVEL, orient="vertical",
            variable=self.var_b, command=lambda *_: self.on_change(),
            width=25, sliderlength=30, highlightthickness=1, bd=0, showvalue=0
        )
        self.s_bl = tk.Scale(
            main, from_=MAX_BLUE, to=MIN_BLUE, orient="vertical",
            variable=self.var_bl, command=lambda *_: self.on_change(),
            width=25, sliderlength=30, highlightthickness=1, bd=0, showvalue=0
        )
        self.s_b.grid(row=1, column=0, pady=(4, 4), sticky="ns")
        self.s_bl.grid(row=1, column=1, pady=(4, 4), sticky="ns")

        self.lbl_b = ttk.Label(main, text=f"{DEFAULT_BRIGHTNESS}%", anchor="center")
        self.lbl_bl = ttk.Label(main, text=f"{DEFAULT_BLUE}%", anchor="center")
        self.lbl_b.grid(row=2, column=0, padx=10, pady=(0, 8), sticky="ew")
        self.lbl_bl.grid(row=2, column=1, padx=10, pady=(0, 8), sticky="ew")

        presets = ttk.Frame(main)
        presets.grid(row=3, column=0, columnspan=2, sticky="ew")
        inner = ttk.Frame(presets)
        inner.pack(anchor="center")
        
        # Берем значения пресетов из настроек
        for text, cmd in [
            ("Care",  lambda: self.apply_preset(self.settings["preset_care_b"], self.settings["preset_care_bl"])),
            ("Read",  lambda: self.apply_preset(self.settings["preset_read_b"], self.settings["preset_read_bl"])),
            ("Reset", self.reset_all),
        ]:
            ttk.Button(inner, text=text, command=cmd, width=6, takefocus=False).pack(side="left", padx=4)

        root.bind_class("Tk", "<FocusOut>", lambda e: self.hide(), add="+")
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.bind_all("<Escape>", lambda e: self.hide(), add="+")

        self.visible = False
        self.apply_theme()

    def apply_theme(self):
        s = self.settings
        try:
            self.style.theme_use("clam")
            
            self.style.configure("TFrame", background=s["bg_color"])
            self.style.configure("TLabel", background=s["bg_color"], foreground=s["fg_color"])
            self.root.configure(bg=s["bg_color"])
            
            self.style.configure("TButton", 
                                 background=s["btn_bg"], 
                                 foreground=s["fg_color"],
                                 bordercolor=s["btn_border"],
                                 focuscolor=s["btn_bg"],
                                 padding=(6, 2))
            self.style.map("TButton", 
                           background=[("pressed", s["btn_pressed_bg"]), ("active", s["btn_active_bg"])],
                           bordercolor=[("pressed", s["btn_border"]), ("active", s["btn_border"])])
            
            for scale in (self.s_b, self.s_bl):
                scale.config(
                    bg=s["bg_color"],
                    fg=s["fg_color"], # Цвет текста (если появится)
                    troughcolor=s["slider_trough"],
                    activebackground=s["slider_color"],
                    highlightbackground=s["slider_border"],
                    highlightcolor=s["slider_border"]
                )
        except tk.TclError:
            pass

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
        self.suppress = True
        try:
            self.var_b.set(brightness)
            self.var_bl.set(blue)
        finally:
            self.suppress = False
        self.lbl_b.config(text=f"{brightness}%")
        self.lbl_bl.config(text=f"{blue}%")
        self.controller.set_brightness(brightness)
        self.controller.set_blue(blue)

    def reset_all(self):
        self.apply_preset(DEFAULT_BRIGHTNESS, DEFAULT_BLUE)
        self.controller.reset()

    def position_near_tray(self):
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
# UI: Окно настроек
# ---------------------------------------------------------------------------

class SettingsWindow:
    def __init__(self, root, settings, on_save_callback):
        self.root = root
        self.settings = settings
        self.on_save_callback = on_save_callback
        
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        
        bg = self.settings["bg_color"]
        fg = self.settings["fg_color"]
        self.win.configure(bg=bg)

        title_bar = tk.Frame(self.win, bg=bg, height=25)
        title_bar.pack(fill="x")
        
        lbl = tk.Label(title_bar, text="Настройки", bg=bg, fg=fg, font=("Segoe UI", 9, "bold"))
        lbl.pack(side="left", padx=5)
        
        btn_close = tk.Label(title_bar, text="✕", bg=bg, fg=fg, font=("Segoe UI", 10, "bold"), cursor="hand2")
        btn_close.pack(side="right", padx=5)
        btn_close.bind("<Button-1>", lambda e: self.win.destroy())

        def start_move(event):
            self.win.x = event.x
            self.win.y = event.y
        def do_move(event):
            deltax = event.x - self.win.x
            deltay = event.y - self.win.y
            x = self.win.winfo_x() + deltax
            y = self.win.winfo_y() + deltay
            self.win.geometry(f"+{x}+{y}")

        for w in (title_bar, lbl):
            w.bind("<ButtonPress-1>", start_move)
            w.bind("<B1-Motion>", do_move)

        content = tk.Frame(self.win, bg=bg, padx=10, pady=10)
        content.pack(fill="both", expand=True)

        self.color_labels = {}
        fields = [
            ("bg_color", "Фон окна"),
            ("fg_color", "Цвет текста"),
            ("btn_bg", "Фон кнопки"),
            ("btn_active_bg", "Фон кнопки (наведение)"),
            ("btn_pressed_bg", "Фон кнопки (нажатие)"),
            ("btn_border", "Рамка кнопки"),
            ("slider_trough", "Фон слайдера"),
            ("slider_color", "Ползунок слайдера"),
            ("slider_border", "Рамка слайдера")
        ]

        for i, (key, text) in enumerate(fields):
            tk.Label(content, text=text, bg=bg, fg=fg).grid(row=i, column=0, sticky="w", pady=2, padx=(0, 10))
            btn = tk.Button(content, width=10, bg=self.settings[key], command=lambda k=key: self.pick_color(k))
            btn.grid(row=i, column=1, pady=2)
            self.color_labels[key] = btn

        # --- Секция пресетов ---
        row = len(fields)
        tk.Label(content, text="--- Пресеты ---", bg=bg, fg=fg).grid(row=row, column=0, columnspan=2, pady=(10, 5))
        
        self.var_care_b = tk.IntVar(value=self.settings["preset_care_b"])
        self.var_care_bl = tk.IntVar(value=self.settings["preset_care_bl"])
        self.var_read_b = tk.IntVar(value=self.settings["preset_read_b"])
        self.var_read_bl = tk.IntVar(value=self.settings["preset_read_bl"])

        tk.Label(content, text="Care: Ярк / Син", bg=bg, fg=fg).grid(row=row+1, column=0, sticky="w", pady=2)
        frame_care = tk.Frame(content, bg=bg)
        frame_care.grid(row=row+1, column=1, sticky="w")
        tk.Spinbox(frame_care, from_=MIN_LEVEL, to=MAX_LEVEL, textvariable=self.var_care_b, width=4).pack(side="left")
        tk.Label(frame_care, text="/", bg=bg, fg=fg).pack(side="left", padx=2)
        tk.Spinbox(frame_care, from_=MIN_BLUE, to=MAX_BLUE, textvariable=self.var_care_bl, width=4).pack(side="left")

        tk.Label(content, text="Read: Ярк / Син", bg=bg, fg=fg).grid(row=row+2, column=0, sticky="w", pady=2)
        frame_read = tk.Frame(content, bg=bg)
        frame_read.grid(row=row+2, column=1, sticky="w")
        tk.Spinbox(frame_read, from_=MIN_LEVEL, to=MAX_LEVEL, textvariable=self.var_read_b, width=4).pack(side="left")
        tk.Label(frame_read, text="/", bg=bg, fg=fg).pack(side="left", padx=2)
        tk.Spinbox(frame_read, from_=MIN_BLUE, to=MAX_BLUE, textvariable=self.var_read_bl, width=4).pack(side="left")

        btn_frame = tk.Frame(content, bg=bg)
        btn_frame.grid(row=row+3, column=0, columnspan=2, pady=(15, 0))
        
        tk.Button(btn_frame, text="Применить", command=self.apply_changes, width=10, 
                  bg=self.settings["btn_bg"], fg=fg, activebackground=self.settings["btn_active_bg"]).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Отмена", command=self.win.destroy, width=10,
                  bg=self.settings["btn_bg"], fg=fg, activebackground=self.settings["btn_active_bg"]).pack(side="left", padx=5)

        self.win.update_idletasks()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")

    def pick_color(self, key):
        color = colorchooser.askcolor(initialcolor=self.settings[key], title="Выберите цвет")
        if color[1]:
            self.settings[key] = color[1]
            self.color_labels[key].config(bg=color[1])

    def apply_changes(self):
        self.settings["preset_care_b"] = self.var_care_b.get()
        self.settings["preset_care_bl"] = self.var_care_bl.get()
        self.settings["preset_read_b"] = self.var_read_b.get()
        self.settings["preset_read_bl"] = self.var_read_bl.get()
        
        save_settings(self.settings)
        self.on_save_callback(self.settings)
        self.win.destroy()

# ---------------------------------------------------------------------------
# Иконка трея и приложение
# ---------------------------------------------------------------------------

def make_icon_image(dimmed=False):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
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

class TrayApp:
    def __init__(self):
        self.controller = make_controller()
        self.settings = load_settings()
        self.root = tk.Tk()
        self.root.withdraw()
        
        self.panel = SliderPanel(self.root, self.controller, self.settings)
        
        self.icon = pystray.Icon(
            name=APP_TITLE,
            icon=make_icon_image(),
            title=APP_TITLE,
            menu=self._build_menu(),
            default_action=self.on_icon_click,
        )

    def _open_url(self, icon=None, item=None):
        # Переносим открытие браузера в главный поток Tkinter
        self.root.after(0, lambda: webbrowser.open("https://donate.stream/son1cpr00wer"))

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("Показать/скрыть панель", self.on_icon_click, default=True),
            pystray.MenuItem("Макс. яркость", lambda: self._quick(100)),
            pystray.MenuItem("Ночной режим", lambda: self._night()),
            pystray.MenuItem("Сброс настроек экрана", lambda: self._reset()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Поддержать автора", self._open_url),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Настройки", self._open_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self.on_exit),
        )

    def _open_settings(self, icon=None, item=None):
        self.root.after(0, self._show_settings_window)

    def _show_settings_window(self):
        SettingsWindow(self.root, self.settings, self._on_settings_saved)

    def _on_settings_saved(self, new_settings):
        self.settings = new_settings
        self.panel.settings = new_settings
        self.panel.apply_theme()
        self.panel.show()

    def _quick(self, value):
        self.root.after(0, lambda: self.panel.apply_preset(value, int(round(self.panel.var_bl.get()))))

    def _night(self):
        self.root.after(0, lambda: self.panel.apply_preset(self.settings["preset_care_b"], self.settings["preset_care_bl"]))

    def _reset(self):
        self.root.after(0, self.panel.reset_all)

    def on_icon_click(self, icon=None, item=None):
        self.root.after(0, self.panel.toggle)

    def on_exit(self, icon=None, item=None):
        try:
            icon.stop()
        finally:
            self.root.after(0, self.root.quit)

def main():
    app = TrayApp()
    t = threading.Thread(target=app.icon.run, daemon=True)
    t.start()
    try:
        app.root.mainloop()
    finally:
        app.icon.stop()

if __name__ == "__main__":
    main()
