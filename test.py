# opensteaminjector.py
import os
import sys
import platform
import subprocess
import shutil
import webbrowser
import threading
import json
import time
import re

import customtkinter as ctk
from tkinter import filedialog, messagebox

import pystray
from PIL import Image, ImageDraw, Image

# =========================
# Resource loader (PyInstaller-friendly)
# =========================
def resource_path(rel_path: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel_path)

# =========================
# Settings persistence
# =========================
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".opensteaminjector_settings.json")

def load_settings() -> dict:
    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

# =========================
# Steam helpers
# =========================
def is_steam_running() -> bool:
    try:
        if platform.system() == "Windows":
            out = subprocess.check_output("tasklist", shell=True).decode(errors="ignore").lower()
            return "steam.exe" in out
        else:
            subprocess.check_output(["pgrep", "steam"])
            return True
    except Exception:
        return False

def kill_steam():
    try:
        if platform.system() == "Windows":
            subprocess.call("taskkill /F /IM steam.exe", shell=True)
        else:
            subprocess.call(["pkill", "steam"])
    except Exception:
        pass

def launch_steam(steam_path: str | None = None):
    try:
        if platform.system() == "Windows":
            exe = None
            if steam_path:
                cand = os.path.join(steam_path, "Steam.exe")
                if os.path.exists(cand):
                    exe = cand
            if not exe:
                fallback = r"C:\Program Files (x86)\Steam\Steam.exe"
                if os.path.exists(fallback):
                    exe = fallback
            if exe:
                subprocess.Popen([exe], shell=True)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", "-a", "Steam"])
        else:
            subprocess.Popen(["steam"])
    except Exception:
        pass

def restart_steam(steam_path: str | None = None):
    if is_steam_running():
        kill_steam()
        time.sleep(0.5)
    launch_steam(steam_path)

def detect_steam_path() -> str | None:
    sysname = platform.system()
    if sysname == "Windows":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                val, _ = winreg.QueryValueEx(key, "SteamPath")
                val = os.path.normpath(val)
                if os.path.isdir(val):
                    return val
        except Exception:
            pass
        for d in ["C", "D", "E", "F"]:
            for cand in [fr"{d}:\Program Files (x86)\Steam", fr"{d}:\Program Files\Steam"]:
                if os.path.isdir(cand):
                    return cand
    elif sysname == "Darwin":
        cand = os.path.expanduser("~/Library/Application Support/Steam")
        if os.path.isdir(cand):
            return cand
    else:
        for cand in [os.path.expanduser("~/.local/share/Steam"),
                     os.path.expanduser("~/.steam/steam"),
                     "/usr/lib/steam"]:
            if os.path.isdir(cand):
                return cand
    return None

# =========================
# Injection routing
# =========================
def derive_destinations(steam_path: str) -> dict[str, str]:
    return {
        "manifest": os.path.join(steam_path, "depotcache"),
        "lua": os.path.join(steam_path, "config", "stplug-in"),
        "vdf": os.path.join(steam_path, "config"),
    }

def classify_file(path: str) -> str | None:
    name = os.path.basename(path).lower()
    ext = os.path.splitext(name)[1]
    if "manifest" in name or ext in [".manifest", ".acf"]:
        return "manifest"
    if ext == ".lua":
        return "lua"
    if ext == ".vdf":
        return "vdf"
    return None

# =========================
# App name extraction helpers
# =========================
def parse_appname_from_manifest(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        for pat in (r'"name"\s*"([^"]+)"', r'"name"\s*:\s*"([^"]+)"'):
            m = re.search(pat, content)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None

def parse_appid_from_text(content: str) -> str | None:
    for pat in (r'"appid"\s*"(\d+)"',
                r'"appid"\s*:\s*"(\d+)"',
                r'\bappid\s*=\s*(\d+)\b'):
        m = re.search(pat, content, re.IGNORECASE)
        if m:
            return m.group(1)
    m = re.search(r'\b(\d{5,7})\b', content)  # best-effort fallback
    return m.group(1) if m else None

def find_appname_by_appid(steam_path: str, appid: str) -> str | None:
    depot = os.path.join(steam_path, "depotcache")
    if not os.path.isdir(depot):
        return None
    for fn in os.listdir(depot):
        fp = os.path.join(depot, fn)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            if appid in txt:
                name = parse_appname_from_manifest(fp)
                if name:
                    return name
                return os.path.splitext(fn)[0]
        except Exception:
            continue
    return None

def guess_program_name_for_file(path: str, steam_path: str | None) -> str:
    kind = classify_file(path)
    if kind == "manifest":
        name = parse_appname_from_manifest(path)
        if name:
            return name
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        appid = parse_appid_from_text(content)
        if appid and steam_path:
            name = find_appname_by_appid(steam_path, appid)
            if name:
                return name
    except Exception:
        pass
    return os.path.splitext(os.path.basename(path))[0]

# =========================
# Inject file
# =========================
def inject_file(path: str, steam_path: str, move: bool = False) -> tuple[bool, str, str]:
    kind = classify_file(path)
    base = os.path.basename(path)
    appname = guess_program_name_for_file(path, steam_path)
    if not kind:
        return False, f"Unsupported file: {base}", appname
    if not steam_path or not os.path.isdir(steam_path):
        return False, "Steam path not set or invalid.", appname

    dests = derive_destinations(steam_path)
    target_dir = dests[kind]
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, base)

    try:
        if move:
            shutil.copy2(path, target)
            os.remove(path)
            return True, f"Moved {base} into Steam", appname
        else:
            shutil.copy2(path, target)
            return True, f"Injected {base} into Steam", appname
    except Exception as e:
        return False, f"Error injecting {base}: {e}", appname

# =========================
# Hub window (hidden at startup, includes file selection inject)
# =========================
class TrailHub(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("OpenSteamInjector")
        self.geometry("720x560")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        s = load_settings()
        self.steam_path = s.get("steam_path") or detect_steam_path()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        ctk.CTkLabel(self, text="OpenSteamInjector", font=ctk.CTkFont(size=20, weight="bold")).pack(pady=14)

        grid = ctk.CTkFrame(self)
        grid.pack(fill="x", padx=10, pady=6)

        ctk.CTkButton(grid, text="Restart Steam", command=lambda: restart_steam(self.steam_path)).grid(row=0, column=0, padx=8, pady=8, sticky="ew")
        ctk.CTkButton(grid, text="Close Steam", command=kill_steam).grid(row=0, column=1, padx=8, pady=8, sticky="ew")
        ctk.CTkButton(grid, text="Inject files", command=self.inject_files).grid(row=1, column=0, padx=8, pady=8, sticky="ew")
        ctk.CTkButton(grid, text="Open SteamDB", command=lambda: webbrowser.open("https://steamdb.info/")).grid(row=1, column=1, padx=8, pady=8, sticky="ew")
        ctk.CTkButton(grid, text="Open SteamML", command=lambda: webbrowser.open("https://steamml.vercel.app/")).grid(row=2, column=0, padx=8, pady=8, sticky="ew")
        for i in range(2):
            grid.grid_columnconfigure(i, weight=1)

        path_frame = ctk.CTkFrame(self)
        path_frame.pack(fill="x", padx=10, pady=10)
        self.path_label = ctk.CTkLabel(path_frame, text=f"Steam path: {self.steam_path or 'Not found'}")
        self.path_label.grid(row=0, column=0, sticky="w", padx=6, pady=6)
        ctk.CTkButton(path_frame, text="Set Steam Path", command=self.set_steam_path).grid(row=0, column=1, sticky="e", padx=6, pady=6)

        info = ctk.CTkTextbox(self, height=360)
        info.pack(fill="both", expand=True, padx=10, pady=10)
        info.configure(state="normal")
        info.insert("end", "Features:\n")
        info.insert("end", " • Tray-only start; open hub from tray.\n")
        info.insert("end", " • Drag injection: independent PyQt5 cube (semi-transparent, draggable, right-click Exit).\n")
        info.insert("end", " • Inject files: select .lua/.acf/.manifest/.vdf from the hub.\n")
        info.insert("end", " • Injected Apps Remover: shows friendly names; delete selected.\n\n")
        info.insert("end", "Routing:\n")
        info.insert("end", " • .acf/.manifest → Steam/depotcache\n")
        info.insert("end", " • .lua → Steam/config/stplug-in\n")
        info.insert("end", " • .vdf → Steam/config\n")
        info.configure(state="disabled")

    def _on_close(self):
        self.withdraw()

    def set_steam_path(self):
        folder = filedialog.askdirectory()
        if folder:
            self.steam_path = folder
            self.path_label.configure(text=f"Steam path: {self.steam_path}")
            data = load_settings()
            data["steam_path"] = self.steam_path
            save_settings(data)
            messagebox.showinfo("OpenSteamInjector", "Steam path saved.")

    def inject_files(self):
        files = filedialog.askopenfilenames(filetypes=[("Supported", "*.lua *.manifest *.acf *.vdf")])
        if not files:
            return
        if not self.steam_path or not os.path.isdir(self.steam_path):
            messagebox.showerror("OpenSteamInjector", "Steam path not set")
            return
        results = []
        for f in files:
            ok, msg, appname = inject_file(f, self.steam_path)
            results.append(("✔ " if ok else "❌ ") + (f" Injected {appname}" if ok else msg))
        messagebox.showinfo("Injection results", "\n".join(results))

# =========================
# Injected Apps Remover
# =========================
class InjectedAppsRemover(ctk.CTkToplevel):
    def __init__(self, master, steam_path):
        super().__init__(master)
        self.steam_path = steam_path
        self.title("Injected Apps Remover")
        self.geometry("700x500")
        self.attributes("-topmost", True)
        ctk.set_appearance_mode("dark")

        ctk.CTkLabel(self, text="Select injected items to remove (manifests show app names)").pack(pady=8)

        import tkinter as tk
        self.listbox = tk.Listbox(self, selectmode=tk.MULTIPLE)
        self.listbox.pack(fill="both", expand=True, padx=10, pady=10)

        self.label_to_path: dict[str, str] = {}

        btn_frame = ctk.CTkFrame(self)
        btn_frame.pack(fill="x", padx=10, pady=8)
        ctk.CTkButton(btn_frame, text="Refresh", command=self.refresh).pack(side="left", padx=6)
        ctk.CTkButton(btn_frame, text="Remove selected", command=self.remove_selected).pack(side="right", padx=6)

        self.refresh()

    def refresh(self):
        self.listbox.delete(0, "end")
        self.label_to_path.clear()
        if not self.steam_path or not os.path.isdir(self.steam_path):
            self.listbox.insert("end", "Steam path not set")
            return

        targets = [
            (os.path.join(self.steam_path, "config", "stplug-in"), [".lua"]),
            (os.path.join(self.steam_path, "config"), [".vdf"]),
            (os.path.join(self.steam_path, "depotcache"), [".acf", ".manifest"]),
        ]

        for folder, exts in targets:
            if os.path.isdir(folder):
                for f in sorted(os.listdir(folder)):
                    fp = os.path.join(folder, f)
                    if not os.path.isfile(fp):
                        continue
                    if not any(f.lower().endswith(ext) for ext in exts):
                        continue

                    if f.lower().endswith((".acf", ".manifest")):
                        appname = parse_appname_from_manifest(fp) or os.path.splitext(f)[0]
                        label = f"Manifest — {appname}"
                    else:
                        appname = guess_program_name_for_file(fp, self.steam_path) or os.path.splitext(f)[0]
                        if f.lower().endswith(".lua"):
                            label = f"Lua — {appname}"
                        elif f.lower().endswith(".vdf"):
                            label = f"VDF — {appname}"
                        else:
                            label = f"{f}"

                    base_label = label
                    n = 1
                    while label in self.label_to_path:
                        n += 1
                        label = f"{base_label} ({n})"
                    self.label_to_path[label] = fp
                    self.listbox.insert("end", label)

    def remove_selected(self):
        selections = self.listbox.curselection()
        if not selections:
            messagebox.showinfo("Injected Apps Remover", "No items selected.")
            return
        removed = 0
        errors = 0
        for idx in selections:
            label = self.listbox.get(idx)
            fp = self.label_to_path.get(label)
            if not fp:
                continue
            try:
                os.remove(fp)
                removed += 1
            except Exception:
                errors += 1
        messagebox.showinfo("Injected Apps Remover", f"Removed: {removed}, Errors: {errors}")
        self.refresh()

# =========================
# PyQt5 drag injection cube (independent process, semi-transparent, draggable, right-click Exit)
# =========================
def run_qt_cube(steam_path: str | None):
    from PyQt5 import QtWidgets, QtCore, QtGui

    class Cube(QtWidgets.QWidget):
        def __init__(self, steam_path=None):
            super().__init__()
            self.steam_path = steam_path

            # Frameless, always on top, tool window (no taskbar entry)
            self.setWindowFlags(
                QtCore.Qt.FramelessWindowHint |
                QtCore.Qt.WindowStaysOnTopHint |
                QtCore.Qt.Tool
            )

            # Make the whole window semi-transparent
            self.setWindowOpacity(0.85)

            # Box look with rounded corners
            self.setStyleSheet("""
                QWidget#CubeRoot {
                    background-color: #1E1E1E;
                    border-radius: 12px;
                }
                QLabel#CubeLabel {
                    color: white;
                    font-size: 14pt;
                }
            """)
            self.setObjectName("CubeRoot")
            self.setFixedSize(320, 140)
            self.move(120, 120)

            # Content
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(8)

            self.label = QtWidgets.QLabel("Drop files here")
            self.label.setObjectName("CubeLabel")
            self.label.setAlignment(QtCore.Qt.AlignCenter)
            layout.addWidget(self.label)

            # Accept drops
            self.setAcceptDrops(True)

            # Right-click context menu for Exit
            self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.customContextMenuRequested.connect(self.show_context_menu)

            # Enable dragging by left mouse
            self._drag_pos = None

        # Draggable by left mouse
        def mousePressEvent(self, event: QtGui.QMouseEvent):
            if event.button() == QtCore.Qt.LeftButton:
                self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
                event.accept()

        def mouseMoveEvent(self, event: QtGui.QMouseEvent):
            if self._drag_pos and event.buttons() & QtCore.Qt.LeftButton:
                self.move(event.globalPos() - self._drag_pos)
                event.accept()

        def mouseReleaseEvent(self, event: QtGui.QMouseEvent):
            if event.button() == QtCore.Qt.LeftButton:
                self._drag_pos = None

        # Right-click menu with Exit
        def show_context_menu(self, pos: QtCore.QPoint):
            menu = QtWidgets.QMenu(self)
            exit_action = menu.addAction("Exit")
            action = menu.exec_(self.mapToGlobal(pos))
            if action == exit_action:
                QtWidgets.QApplication.quit()

        # Drag-and-drop handling
        def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
            if event.mimeData().hasUrls():
                event.acceptProposedAction()

        def dropEvent(self, event: QtGui.QDropEvent):
            last = ""
            if not self.steam_path or not os.path.isdir(self.steam_path):
                self.label.setText("Set Steam path in Show App")
                return
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                ok, msg, appname = inject_file(path, self.steam_path)
                last = f"✔ Injected {appname} into Steam" if ok else f"❌ {msg}"
            if last:
                self.label.setText(last)

    app = QtWidgets.QApplication(sys.argv)
    cube = Cube(steam_path)
    cube.show()
    sys.exit(app.exec_())

# =========================
# Tray controller (uses your logo.png)
# =========================
def make_icon_image():
    try:
        img = Image.open(resource_path("logo.png"))
        if max(img.size) > 64:
            img = img.resize((64, 64))
        return img
    except Exception:
        img = Image.new("RGB", (64, 64), "black")
        draw = ImageDraw.Draw(img)
        draw.ellipse((8, 8, 56, 56), outline=(24, 166, 255), width=3)
        draw.ellipse((28, 28, 36, 36), fill=(24, 166, 255))
        return img

class TrayController:
    def __init__(self, hub: TrailHub):
        self.hub = hub
        self.icon = pystray.Icon("OpenSteamInjector", make_icon_image(), "OpenSteamInjector")
        self.icon.menu = pystray.Menu(
            pystray.MenuItem("Show App", self._menu_show_app),
            pystray.MenuItem("Drag injection", self._menu_drag_injection),
            pystray.MenuItem("Injected Apps Remover", self._menu_remove),
            pystray.MenuItem("Restart Steam", self._menu_restart),
            pystray.MenuItem("Close Steam", self._menu_close),
            pystray.MenuItem("Open SteamDB", lambda icon, item: webbrowser.open("https://steamdb.info/")),
            pystray.MenuItem("Open SteamML", lambda icon, item: webbrowser.open("https://steamml.vercel.app/")),
            pystray.MenuItem("Quit", self._menu_quit),
        )

    def _menu_show_app(self, icon, item):
        threading.Thread(target=self._show_app, daemon=True).start()

    def _menu_drag_injection(self, icon, item):
        # Launch PyQt5 cube as independent process of this same script
        threading.Thread(
            target=lambda: subprocess.Popen([sys.executable, resource_path(os.path.basename(__file__)), "--cube", self.hub.steam_path or ""]),
            daemon=True
        ).start()

    def _menu_remove(self, icon, item):
        threading.Thread(target=lambda: InjectedAppsRemover(self.hub, self.hub.steam_path), daemon=True).start()

    def _menu_restart(self, icon, item):
        threading.Thread(target=lambda: restart_steam(self.hub.steam_path), daemon=True).start()

    def _menu_close(self, icon, item):
        threading.Thread(target=kill_steam, daemon=True).start()

    def _menu_quit(self, icon, item):
        try:
            self.icon.stop()
        except Exception:
            pass
        os._exit(0)

    def _show_app(self):
        try:
            self.hub.deiconify()
            self.hub.focus_force()
        except Exception:
            pass

    def run(self):
        self.icon.run()

# =========================
# Entry point
# =========================
if __name__ == "__main__":
    # If called with --cube, run the independent PyQt5 drag injection cube
    if len(sys.argv) >= 2 and sys.argv[1] == "--cube":
        steam_path_arg = sys.argv[2] if len(sys.argv) >= 3 else None
        run_qt_cube(steam_path_arg if steam_path_arg else None)
        sys.exit(0)

    # Normal tray-driven app
    hub = TrailHub()
    hub.withdraw()  # start hidden

    tray = TrayController(hub)
    t = threading.Thread(target=tray.run, daemon=True)
    t.start()

    hub.mainloop()
