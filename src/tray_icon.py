import threading
import pystray
from PIL import Image, ImageDraw
from paths import resource_path

class TrayController:
    def __init__(self, capture, on_exit, on_show=None):
        self.capture = capture
        self.on_exit = on_exit
        self.on_show = on_show
        icon_path = resource_path("assets/stealth_copilot.png")
        try:
            image = Image.open(icon_path).convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        except OSError:
            image = Image.new("RGB", (64, 64), "#171717")
            draw = ImageDraw.Draw(image)
            draw.ellipse((12, 12, 52, 52), fill="#E5E5E5")
        self.icon = pystray.Icon("Stealth Audio Bridge", image, "Stealth Audio Bridge", menu=pystray.Menu(
            # Default item: double-clicking the tray icon reopens the window.
            pystray.MenuItem("Show window", self.show, default=True),
            pystray.MenuItem("Pause / Resume Capture", self.toggle),
            pystray.MenuItem("Exit", self.exit),
        ))

    def run(self):
        self.icon.run()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def show(self, _icon=None, _item=None):
        if self.on_show:
            self.on_show()

    def toggle(self, _icon=None, _item=None):
        self.capture.paused = not self.capture.paused
        self.icon.title = "Capture paused" if self.capture.paused else "Stealth Audio Bridge"

    def exit(self, _icon=None, _item=None):
        self.capture.running = False
        self.on_exit()
        self.icon.stop()

    def stop(self):
        self.icon.stop()

