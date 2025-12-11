import threading
import queue
import tkinter as tk
from PIL import Image, ImageTk
import os
import math


class VoiceGUI:
    def __init__(self):
        self._thread = None
        self._q = queue.Queue()
        self._running = False

        # display state
        # show Atro directly (menu removed)
        self.active_view = "atrobot"  # 'home' or 'atrobot'
        self.last_level = 0.0
        self.display_level = 0.0
        self._last_source = None
        # AI speaking / pulse state
        self._ai_speaking = False
        self._ai_pulse_phase = 0.0
        # selection callbacks called with a string: 'atrobot'|'planetas'|'cohetes'
        self._selection_callbacks = []

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self.root = tk.Tk()
        self.root.title("CaneIA - Interacción Voz")
        self._running = True

        # Try to load assets
        assets_dir = os.path.join(os.path.dirname(__file__), "images")
        self.bg_image = None
        self.bg_photo = None
        self.atrobot_img = None
        self.opt_planets = None
        self.opt_rockets = None
        try:
            bg_svg_path = os.path.join(assets_dir, "background_homce.svg")
            bg_png_path = os.path.join(assets_dir, "background_homce.png")

            # Prefer PNG background if available. If only SVG exists, try to convert with cairosvg (optional dep).
            if os.path.exists(bg_png_path):
                try:
                    im = Image.open(bg_png_path)
                    self.bg_image = im.copy()
                except Exception:
                    self.bg_image = None
            elif os.path.exists(bg_svg_path):
                try:
                    # try cairosvg if installed to convert svg -> png in memory
                    import cairosvg
                    png_data = cairosvg.svg2png(url=bg_svg_path)
                    from io import BytesIO
                    im = Image.open(BytesIO(png_data))
                    self.bg_image = im.copy()
                except Exception:
                    # conversion not available; leave bg_image None
                    self.bg_image = None

            # load PNG icons
            def _load(img_name, size=None):
                path = os.path.join(assets_dir, img_name)
                if not os.path.exists(path):
                    return None
                try:
                    im = Image.open(path).convert("RGBA")
                    if size:
                        im = im.resize(size, Image.LANCZOS)
                    return ImageTk.PhotoImage(im)
                except Exception:
                    return None

            self.atrobot_img = _load("AtroBot.png", size=(180, 180))
            self.opt_planets = _load("options_planets.png", size=(100, 100))
            self.opt_rockets = _load("options_rockets.png", size=(100, 100))
        except Exception:
            self.bg_image = None
            self.atrobot_img = None
            self.opt_planets = None
            self.opt_rockets = None

        # Top: main canvas (will be used to draw background/home/atrobot)
        self.canvas = tk.Canvas(self.root, width=800, height=420, bg="#151515", highlightthickness=0)
        self.canvas.pack(padx=12, pady=12)
        self.root.update_idletasks()
        self._canvas_width = int(self.canvas['width'])
        self._canvas_height = int(self.canvas['height'])

        # (Menu-only UI) no control bar, no message area, no spectrum — only canvas + options

        # schedule updates
        self._update_ui()

        try:
            self.root.mainloop()
        finally:
            self._running = False

    def _process_queue(self):
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break

            typ = item[0]
            if typ == "level":
                # store last level (ignored in menu-only UI but kept for compatibility)
                try:
                    _, level, source = item
                    self.last_level = float(level)
                    self._last_source = source
                except Exception:
                    pass

            elif typ == "message":
                # store last message (ignored in menu-only UI)
                try:
                    _, text, source = item
                    # could be shown in a future message area
                    self._last_message = (text, source)
                except Exception:
                    pass
            elif typ == "ai_speaking":
                try:
                    _, flag = item
                    self._ai_speaking = bool(flag)
                    if not self._ai_speaking:
                        # reset pulse phase when stopping
                        self._ai_pulse_phase = 0.0
                except Exception:
                    pass
            elif typ == "set_source":
                try:
                    _, src = item
                    # map 'ai' -> atrobot, others -> home
                    if src in ('ai', 'atrobot'):
                        self.active_view = 'atrobot'
                    else:
                        self.active_view = 'home'
                except Exception:
                    pass

    def _draw_home(self):
        # draw background (image if available)
        w = int(self.canvas.winfo_width())
        h = int(self.canvas.winfo_height())
        self.canvas.delete("all")
        if self.bg_image:
            # scale bg_image to canvas keeping aspect
            try:
                im = self.bg_image.copy()
                im_ratio = im.width / im.height
                canvas_ratio = w / h
                if canvas_ratio > im_ratio:
                    # canvas wider -> fit width
                    new_w = w
                    new_h = int(w / im_ratio)
                else:
                    new_h = h
                    new_w = int(h * im_ratio)
                im = im.resize((new_w, new_h), Image.LANCZOS)
                self.bg_photo = ImageTk.PhotoImage(im)
                # center
                self.canvas.create_image(w//2, h//2, image=self.bg_photo)
            except Exception:
                self.canvas.create_rectangle(0, 0, w, h, fill="#0b1630", outline="")
        else:
            self.canvas.create_rectangle(0, 0, w, h, fill="#0b1630", outline="")

        cx, cy = w // 2, h // 2 - 10
        # draw AtroBot image centered (clickable)
        if self.atrobot_img:
            # place as a canvas image and keep reference
            self._atrobot_canvas_id = self.canvas.create_image(cx, cy - 10, image=self.atrobot_img)
            # bind click
            self.canvas.tag_bind(self._atrobot_canvas_id, "<Button-1>", lambda e: self._on_atrobot_clicked())
        else:
            self._atrobot_canvas_id = self.canvas.create_oval(cx-60, cy-60, cx+60, cy+60, fill="#6b4bff", outline="")
            self.canvas.tag_bind(self._atrobot_canvas_id, "<Button-1>", lambda e: self._on_atrobot_clicked())

        # greeting text
        self.canvas.create_text(cx, cy + 80, text="¡Hola, explorador!", fill="#ffffff", font=("Helvetica", 16, "bold"))
        self.canvas.create_text(cx, cy + 108, text="¿Qué quieres aprender hoy?", fill="#dfe7ff", font=("Helvetica", 11))

        # Menu removed: Atro is the primary interaction. No option buttons.

    def _on_option_selected(self, option: str):
        # display choice in message area and mark active
        if option == 'planetas':
            try:
                # notify selection callbacks
                for cb in list(self._selection_callbacks):
                    try:
                        cb('planetas')
                    except Exception:
                        pass
            except Exception:
                pass
        else:
            try:
                for cb in list(self._selection_callbacks):
                    try:
                        cb('cohetes')
                    except Exception:
                        pass
            except Exception:
                pass

    def _on_atrobot_clicked(self):
        # switch to AtroBot-only view and notify callbacks
        self.active_view = 'atrobot'
        try:
            for cb in list(self._selection_callbacks):
                try:
                    cb('atrobot')
                except Exception:
                    pass
        except Exception:
            pass

    def _draw_atrobot_view(self):
        try:
            w = int(self.canvas.winfo_width())
            h = int(self.canvas.winfo_height())
            self.canvas.delete("all")
            if self.bg_image:
                im = self.bg_image.copy()
                im_ratio = im.width / im.height
                canvas_ratio = w / h
                if canvas_ratio > im_ratio:
                    new_w = w
                    new_h = int(w / im_ratio)
                else:
                    new_h = h
                    new_w = int(h * im_ratio)
                im = im.resize((new_w, new_h), Image.LANCZOS)
                self.bg_photo = ImageTk.PhotoImage(im)
                self.canvas.create_image(w//2, h//2, image=self.bg_photo)
            else:
                self.canvas.create_rectangle(0, 0, w, h, fill="#0b1630", outline="")

            # draw AtroBot centered larger
            if self.atrobot_img:
                # optionally draw a pulsing ring behind Atro when AI is speaking
                if getattr(self, '_ai_speaking', False):
                    # compute pulse size
                    phase = self._ai_pulse_phase
                    intensity = (math.sin(phase) + 1.0) / 2.0  # 0..1
                    r = 90 + int(16 * intensity)
                    x0 = w//2 - r
                    y0 = h//2 - r
                    x1 = w//2 + r
                    y1 = h//2 + r
                    # pulsating outline
                    color = '#8b6bff'
                    self.canvas.create_oval(x0, y0, x1, y1, outline=color, width=6, tags=('pulse',))
                self.canvas.create_image(w//2, h//2, image=self.atrobot_img)
            else:
                self.canvas.create_oval(w//2-80, h//2-80, w//2+80, h//2+80, fill="#6b4bff", outline="")
        except Exception:
            pass

    def register_selection_callback(self, cb):
        """Register a callback(cb) called with arg in ('atrobot','planetas','cohetes')."""
        try:
            if callable(cb):
                self._selection_callbacks.append(cb)
        except Exception:
            pass

    def set_active_source(self, source: str):
        """Directly set the active view: 'home' or 'atrobot'."""
        try:
            if source == 'ai' or source == 'atrobot':
                self.active_view = 'atrobot'
            else:
                self.active_view = 'home'
        except Exception:
            pass


    # circle/spectrum drawing removed in menu-only mode

    def _update_ui(self):
        # process queue messages
        self._process_queue()

        # advance pulse phase if speaking
        try:
            if getattr(self, '_ai_speaking', False):
                self._ai_pulse_phase += 0.36
            else:
                # decay towards 0
                if self._ai_pulse_phase > 0.0:
                    self._ai_pulse_phase = max(0.0, self._ai_pulse_phase - 0.36)
        except Exception:
            pass

        # Always show menu/home unless user clicked AtroBot (which switches to atrobot view)
        if not getattr(self, '_home_shown', False):
            try:
                self._draw_home()
                self._home_shown = True
            except Exception:
                self._home_shown = True
        else:
            if self.active_view == 'home':
                try:
                    self._draw_home()
                except Exception:
                    pass
            elif self.active_view == 'atrobot':
                try:
                    self._draw_atrobot_view()
                except Exception:
                    pass

        if self._running:
            self.root.after(40, self._update_ui)

    # API methods
    def send_level(self, level, source="user"):
        try:
            self._q.put(("level", level, source))
        except Exception:
            pass

    def send_message(self, text, source="user"):
        try:
            self._q.put(("message", text, source))
        except Exception:
            pass

    def set_active_source(self, source: str):
        try:
            self._q.put(("set_source", source))
        except Exception:
            pass

    def _on_toggle_mute(self):
        # toggle mute state
        self.muted = not getattr(self, 'muted', False)

    def set_muted(self, value: bool):
        self.muted = bool(value)

    def is_muted(self) -> bool:
        return bool(getattr(self, 'muted', False))

    def set_ai_speaking(self, flag: bool):
        """Request GUI show/hide AI speaking animation."""
        try:
            self._q.put(("ai_speaking", bool(flag)))
        except Exception:
            pass

    # compatibility: external callers expect update_audio_level/display_message names
    def update_audio_level(self, level, source="user"):
        return self.send_level(level, source)

    def display_message(self, text, source="user"):
        return self.send_message(text, source)


# Singleton instance
_gui_instance = None


def start_gui():
    global _gui_instance
    if _gui_instance is None:
        _gui_instance = VoiceGUI()
        _gui_instance.start()


def update_audio_level(level, source="user"):
    if _gui_instance:
        _gui_instance.send_level(level, source)


def display_message(text, source="user"):
    if _gui_instance:
        _gui_instance.send_message(text, source)


def set_active_source(source: str):
    if _gui_instance:
        try:
            _gui_instance.set_active_source(source)
        except Exception:
            pass


def register_selection_callback(cb):
    if _gui_instance:
        try:
            _gui_instance.register_selection_callback(cb)
        except Exception:
            pass


def toggle_mute():
    if _gui_instance:
        _gui_instance._on_toggle_mute()


def is_muted() -> bool:
    if _gui_instance:
        return _gui_instance.is_muted()
    return False


def set_ai_speaking(flag: bool):
    if _gui_instance:
        try:
            _gui_instance.set_ai_speaking(flag)
        except Exception:
            pass
