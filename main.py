#!/usr/bin/env python3
"""
Min Launcher for macOS
-----------------------
A fullscreen, keyboard-driven app launcher for macOS (tested on macOS 26 "Tahoe"),
ported from the original Omarchy/Quickshell "min-launcher" plugin.

Native apps are discovered from the standard macOS Applications folders and
grouped by their LSApplicationCategoryType (Apple's equivalent of freedesktop
categories on Linux). A "Web Apps" section lets you pin URLs that open in your
default browser, persisted to disk, just like the original plugin.

Run:
    pip install -r requirements.txt
    python3 main.py

Toggle the launcher with the global hotkey (default: Option+Space) or by
clicking the menu-bar icon. See README.md for packaging it as a real .app
and starting it automatically at login.
"""

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

from PyQt6.QtCore import Qt, QObject, pyqtSignal, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QKeyEvent, QFont
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QScrollArea, QPushButton, QDialog, QFormLayout,
    QSystemTrayIcon, QMenu, QFrame
)

# ----------------------------------------------------------------------------
# Config / paths
# ----------------------------------------------------------------------------

APP_ID = "min-launcher"
CONFIG_DIR = Path.home() / "Library" / "Application Support" / "MinLauncher"
WEB_APPS_PATH = CONFIG_DIR / "web-apps.json"
ICON_CACHE_DIR = Path(tempfile.gettempdir()) / "min-launcher-icons"

GLOBAL_HOTKEY = "<alt>+<space>"  # Option+Space; change to taste (see README)

APP_DIRS = [
    "/Applications",
    "/System/Applications",
    "/System/Applications/Utilities",
    str(Path.home() / "Applications"),
]

# Apple's LSApplicationCategoryType values, grouped the same way the Linux
# version grouped freedesktop categories.
CATEGORY_MAP = {
    "public.app-category.developer-tools": "Development",
    "public.app-category.graphics-design": "Graphics",
    "public.app-category.photography": "Graphics",
    "public.app-category.social-networking": "Internet",
    "public.app-category.news": "Internet",
    "public.app-category.business": "Office",
    "public.app-category.productivity": "Office",
    "public.app-category.finance": "Office",
    "public.app-category.education": "Office",
    "public.app-category.reference": "Utility",
    "public.app-category.healthcare-fitness": "Utility",
    "public.app-category.medical": "Utility",
    "public.app-category.lifestyle": "Utility",
    "public.app-category.travel": "Utility",
    "public.app-category.utilities": "Utility",
    "public.app-category.weather": "Utility",
    "public.app-category.music": "Multimedia",
    "public.app-category.video": "Multimedia",
    "public.app-category.entertainment": "Multimedia",
    "public.app-category.sports": "Games",
    "public.app-category.games": "Games",
    "public.app-category.action-games": "Games",
    "public.app-category.adventure-games": "Games",
    "public.app-category.arcade-games": "Games",
    "public.app-category.board-games": "Games",
    "public.app-category.card-games": "Games",
    "public.app-category.casino-games": "Games",
    "public.app-category.dice-games": "Games",
    "public.app-category.educational-games": "Games",
    "public.app-category.family-games": "Games",
    "public.app-category.kids-games": "Games",
    "public.app-category.music-games": "Games",
    "public.app-category.puzzle-games": "Games",
    "public.app-category.racing-games": "Games",
    "public.app-category.role-playing-games": "Games",
    "public.app-category.simulation-games": "Games",
    "public.app-category.sports-games": "Games",
    "public.app-category.strategy-games": "Games",
    "public.app-category.trivia-games": "Games",
    "public.app-category.word-games": "Games",
}
SECTION_ORDER = ["Development", "Graphics", "Internet", "Office", "Multimedia",
                  "System", "Utility", "Games", "Apps", "Web Apps"]

ACCENT_PALETTE = ["#60a5fa", "#a78bfa", "#f472b6", "#34d399", "#fbbf24",
                   "#fb7185", "#22d3ee", "#c084fc", "#4ade80", "#f97316"]

COLORS = dict(
    card_bg="#000000", text_primary="#f0f0f2", text_muted="#8a8a96",
    accent="#7c6af7", hover_bg="#1c1c22", border="#2a2a32", danger="#f87171",
)


# ----------------------------------------------------------------------------
# App discovery
# ----------------------------------------------------------------------------

def _read_plist(app_path: Path):
    plist_path = app_path / "Contents" / "Info.plist"
    if not plist_path.exists():
        return {}
    try:
        with open(plist_path, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return {}


def _icon_png_for(app_path: Path) -> str:
    """Render the app's .icns/.icon resource to a cached PNG via macOS'
    'sips' tool (no extra Python deps needed for this part) and return the
    PNG path, or '' if it couldn't be produced."""
    ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_name = app_path.name.replace("/", "_") + ".png"
    cache_path = ICON_CACHE_DIR / cache_name
    if cache_path.exists():
        return str(cache_path)

    info = _read_plist(app_path)
    icon_file = info.get("CFBundleIconFile") or info.get("CFBundleIconName") or ""
    icon_file = str(icon_file)
    resources = app_path / "Contents" / "Resources"
    candidate = None
    if icon_file:
        stem = icon_file[:-5] if icon_file.endswith(".icns") else icon_file
        for ext in (".icns", ""):
            p = resources / (stem + ext)
            if p.exists():
                candidate = p
                break
    if candidate is None and resources.exists():
        icns_files = sorted(resources.glob("*.icns"))
        if icns_files:
            candidate = icns_files[0]
    if candidate is None:
        return ""

    try:
        subprocess.run(
            ["sips", "-s", "format", "png", str(candidate), "--out", str(cache_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=True,
        )
        return str(cache_path) if cache_path.exists() else ""
    except Exception:
        return ""


def scan_native_apps():
    apps = []
    seen_paths = set()
    for base in APP_DIRS:
        base_path = Path(base)
        if not base_path.exists():
            continue
        # One level deep is enough for the standard layout, plus nested
        # folders like /Applications/Utilities.
        candidates = list(base_path.glob("*.app")) + list(base_path.glob("*/*.app"))
        for app_path in candidates:
            resolved = str(app_path.resolve())
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)

            info = _read_plist(app_path)
            name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or app_path.stem
            category_key = str(info.get("LSApplicationCategoryType", ""))
            section = CATEGORY_MAP.get(category_key, "Apps")
            bundle_id = info.get("CFBundleIdentifier", resolved)

            apps.append({
                "appId": bundle_id,
                "name": str(name),
                "subtext": "",
                "path": resolved,
                "icon": "",  # filled in lazily on first paint
                "isWeb": False,
                "section": section,
            })
    apps.sort(key=lambda a: a["name"].lower())
    return apps


# ----------------------------------------------------------------------------
# Web apps persistence
# ----------------------------------------------------------------------------

def load_web_apps():
    if not WEB_APPS_PATH.exists():
        return []
    try:
        data = json.loads(WEB_APPS_PATH.read_text())
        items = data.get("items", []) if isinstance(data, dict) else data
        out = []
        for it in items:
            name = str(it.get("name", "")).strip()
            url = str(it.get("url", "")).strip()
            if not name or not url:
                continue
            if not (url.startswith("http://") or url.startswith("https://")):
                url = "https://" + url
            out.append({
                "appId": "web." + name.lower().replace(" ", "-"),
                "name": name, "subtext": "Web", "path": "", "icon": "",
                "url": url, "isWeb": True, "section": "Web Apps",
            })
        return out
    except Exception:
        return []


def save_web_apps(items):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"items": [{"name": i["name"], "url": i["url"]} for i in items]}
    WEB_APPS_PATH.write_text(json.dumps(payload, indent=2))


# ----------------------------------------------------------------------------
# Filtering / grouping (ported near-verbatim from Tools.js)
# ----------------------------------------------------------------------------

def filter_apps(apps, query):
    q = (query or "").strip().lower()
    if not q:
        return apps
    tokens = q.split()
    out = []
    for a in apps:
        hay = " ".join([a.get("name", ""), a.get("subtext", ""), a.get("appId", ""),
                         a.get("section", "")]).lower()
        if all(t in hay for t in tokens):
            out.append(a)
    return out


def build_sections(apps):
    buckets = {title: [] for title in SECTION_ORDER}
    for a in apps:
        buckets.setdefault(a.get("section", "Apps"), []).append(a)
    sections = []
    for title in SECTION_ORDER:
        if buckets.get(title):
            sections.append((title, buckets[title]))
    for title, items in buckets.items():
        if title not in SECTION_ORDER and items:
            sections.append((title, items))
    return sections


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

class AppRow(QWidget):
    def __init__(self, app_data, index, launcher):
        super().__init__()
        self.app_data = app_data
        self.index = index
        self.launcher = launcher
        self.setFixedHeight(26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(6)

        icon_lbl = QLabel()
        icon_lbl.setFixedSize(16, 16)
        png = _icon_png_for(Path(app_data["path"])) if app_data.get("path") else ""
        if png:
            pix = QPixmap(png).scaled(16, 16, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
            icon_lbl.setPixmap(pix)
        else:
            color = ACCENT_PALETTE[index % len(ACCENT_PALETTE)]
            icon_lbl.setStyleSheet(f"background-color:{color}; border-radius:3px;")
        layout.addWidget(icon_lbl)

        self.name_lbl = QLabel(app_data["name"][:128])
        self.name_lbl.setFont(QFont("Helvetica Neue", 12))
        layout.addWidget(self.name_lbl, 1)

        if app_data.get("isWeb"):
            self.remove_btn = QLabel("−")
            self.remove_btn.setFixedSize(18, 18)
            self.remove_btn.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.remove_btn.mousePressEvent = self._on_remove
            layout.addWidget(self.remove_btn)
        else:
            self.remove_btn = None

        self.set_selected(False)

    def _on_remove(self, _event):
        self.launcher.remove_web_app(self.app_data)

    def set_selected(self, selected):
        bg = "rgba(125,107,247,0.2)" if selected else "transparent"
        fg = COLORS["text_primary"] if selected else COLORS["text_muted"]
        self.setStyleSheet(f"QWidget {{ background-color:{bg}; border-radius:6px; }}")
        self.name_lbl.setStyleSheet(f"color:{fg}; background:transparent;")
        if self.remove_btn:
            self.remove_btn.setStyleSheet(
                f"color:{COLORS['text_muted']}; background:transparent; border-radius:4px;")

    def enterEvent(self, event):
        self.launcher.select_app(self.app_data)
        super().enterEvent(event)

    def mousePressEvent(self, event):
        self.launcher.launch_app(self.app_data)
        super().mousePressEvent(event)


class AddWebAppDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Add Web App")
        self.setModal(True)
        self.setFixedWidth(360)
        self.setStyleSheet(f"""
            QDialog {{ background-color:#0c0c0e; }}
            QLabel {{ color:{COLORS['text_muted']}; font-size:11px; }}
            QLineEdit {{ background:#121216; color:{COLORS['text_primary']};
                         border:1px solid {COLORS['border']}; border-radius:8px; padding:6px; }}
        """)
        form = QFormLayout(self)
        self.title_edit = QLineEdit()
        self.url_edit = QLineEdit()
        form.addRow("Title", self.title_edit)
        form.addRow("URL", self.url_edit)
        btn_row = QHBoxLayout()
        cancel = QPushButton("Cancel")
        add = QPushButton("Add")
        cancel.clicked.connect(self.reject)
        add.clicked.connect(self.accept)
        btn_row.addStretch(1)
        btn_row.addWidget(cancel)
        btn_row.addWidget(add)
        form.addRow(btn_row)
        self.title_edit.setFocus()

    def values(self):
        return self.title_edit.text().strip(), self.url_edit.text().strip()


class LauncherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setStyleSheet(f"background-color:{COLORS['card_bg']};")

        self.native_apps = []
        self.web_apps = load_web_apps()
        self.all_apps = []
        self.filtered_apps = []
        self.selected_index = 0
        self.row_widgets = []

        self._build_ui()
        self.refresh_native_apps()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 48, 48, 48)
        outer.setSpacing(20)

        header = QVBoxLayout()
        title = QLabel("Apps")
        title.setStyleSheet(f"color:{COLORS['text_primary']}; font-size:26px; font-weight:600;")
        self.count_lbl = QLabel("0 items")
        self.count_lbl.setStyleSheet(f"color:{COLORS['text_muted']}; font-size:13px;")
        header.addWidget(title)
        header.addWidget(self.count_lbl)
        outer.addLayout(header)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter apps…")
        self.search.setFixedHeight(34)
        self.search.setStyleSheet(f"""
            QLineEdit {{ background:#000; color:{COLORS['text_primary']};
                         border:1px solid transparent; border-radius:8px; padding-left:12px;
                         font-size:13px; }}
            QLineEdit:focus {{ border:1px solid {COLORS['accent']}; }}
        """)
        self.search.textChanged.connect(self.apply_filter)
        outer.addWidget(self.search)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.sections_container = QWidget()
        self.sections_container.setStyleSheet("background: transparent;")
        self.sections_grid = QGridLayout(self.sections_container)
        self.sections_grid.setSpacing(14)
        self.scroll.setWidget(self.sections_container)
        outer.addWidget(self.scroll, 1)

        hint = QLabel("\u2191\u2193 navigate  \u00b7  Enter launch  \u00b7  Esc close")
        hint.setStyleSheet(f"color:{COLORS['text_muted']}; font-size:11px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(hint)

        add_row = QHBoxLayout()
        add_row.addStretch(1)
        add_btn = QPushButton("+")
        add_btn.setFixedSize(48, 48)
        add_btn.setStyleSheet(f"""
            QPushButton {{ background:{COLORS['accent']}; color:white; border-radius:24px;
                           font-size:22px; font-weight:600; }}
            QPushButton:hover {{ background:#8f80f9; }}
        """)
        add_btn.clicked.connect(self.open_add_dialog)
        add_row.addWidget(add_btn)
        outer.addLayout(add_row)

    # -- data ----------------------------------------------------------

    def refresh_native_apps(self):
        self.native_apps = scan_native_apps()
        self.rebuild_all_apps()

    def rebuild_all_apps(self):
        self.all_apps = self.native_apps + self.web_apps
        self.apply_filter()

    def apply_filter(self):
        self.filtered_apps = filter_apps(self.all_apps, self.search.text())
        self.selected_index = min(self.selected_index, max(0, len(self.filtered_apps) - 1))
        self.count_lbl.setText(f"{len(self.filtered_apps)} items")
        self._render_sections()

    def _render_sections(self):
        while self.sections_grid.count():
            item = self.sections_grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.row_widgets = []

        sections = build_sections(self.filtered_apps)
        cols = max(2, min(4, max(1, self.width() // 220)))
        flat_index = 0
        for i, (title, items) in enumerate(sections):
            col_widget = QWidget()
            col_layout = QVBoxLayout(col_widget)
            col_layout.setContentsMargins(0, 0, 0, 0)
            col_layout.setSpacing(8)
            heading = QLabel(title)
            heading.setStyleSheet(f"color:{COLORS['text_primary']}; font-size:13px; font-weight:600;")
            col_layout.addWidget(heading)
            for app_data in items:
                row = AppRow(app_data, flat_index, self)
                row.set_selected(flat_index == self.selected_index)
                col_layout.addWidget(row)
                self.row_widgets.append(row)
                flat_index += 1
            col_layout.addStretch(1)
            self.sections_grid.addWidget(col_widget, i // cols, i % cols)

    # -- selection / launching -----------------------------------------

    def select_app(self, app_data):
        for idx, row in enumerate(self.row_widgets):
            if row.app_data is app_data:
                self.selected_index = idx
        self._refresh_selection_styles()

    def _refresh_selection_styles(self):
        for idx, row in enumerate(self.row_widgets):
            row.set_selected(idx == self.selected_index)

    def move_selection(self, delta):
        n = len(self.filtered_apps)
        if n == 0:
            return
        self.selected_index = (self.selected_index + delta) % n
        self._refresh_selection_styles()

    def launch_selected(self):
        if 0 <= self.selected_index < len(self.filtered_apps):
            self.launch_app(self.filtered_apps[self.selected_index])

    def launch_app(self, app_data):
        if app_data.get("isWeb"):
            webbrowser.open(app_data["url"])
        elif app_data.get("path"):
            subprocess.Popen(["open", app_data["path"]])
        self.dismiss()

    # -- web app management ----------------------------------------------

    def open_add_dialog(self):
        dlg = AddWebAppDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name, url = dlg.values()
            if name and url:
                if not (url.startswith("http://") or url.startswith("https://")):
                    url = "https://" + url
                self.web_apps = [w for w in self.web_apps if w["name"] != name]
                self.web_apps.append({
                    "appId": "web." + name.lower().replace(" ", "-"),
                    "name": name, "subtext": "Web", "path": "", "icon": "",
                    "url": url, "isWeb": True, "section": "Web Apps",
                })
                save_web_apps(self.web_apps)
                self.rebuild_all_apps()
        self.search.setFocus()

    def remove_web_app(self, app_data):
        self.web_apps = [w for w in self.web_apps if w["appId"] != app_data["appId"]]
        save_web_apps(self.web_apps)
        self.rebuild_all_apps()

    # -- open / close -----------------------------------------------------

    def open_launcher(self):
        self.search.clear()
        self.selected_index = 0
        self.refresh_native_apps()
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(0, self.search.setFocus)

    def dismiss(self):
        self.hide()

    def toggle(self):
        if self.isVisible():
            self.dismiss()
        else:
            self.open_launcher()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_sections()

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self.search.text():
                self.search.clear()
            else:
                self.dismiss()
        elif key == Qt.Key.Key_Down or key == Qt.Key.Key_Tab:
            self.move_selection(1)
        elif key == Qt.Key.Key_Up or key == Qt.Key.Key_Backtab:
            self.move_selection(-1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.launch_selected()
        else:
            super().keyPressEvent(event)


# ----------------------------------------------------------------------------
# Global hotkey bridge (pynput listener thread -> Qt main thread)
# ----------------------------------------------------------------------------

class HotkeyBridge(QObject):
    triggered = pyqtSignal()


def start_global_hotkey(bridge: HotkeyBridge):
    try:
        from pynput import keyboard
    except ImportError:
        print("pynput not installed; global hotkey disabled. "
              "Use the menu-bar icon instead, or `pip install pynput`.")
        return None
    listener = keyboard.GlobalHotKeys({GLOBAL_HOTKEY: bridge.triggered.emit})
    listener.start()
    return listener


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Hide the Dock icon / app switcher entry, like a background utility.
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:
        pass  # pyobjc not installed; app will just show a normal Dock icon

    window = LauncherWindow()

    bridge = HotkeyBridge()
    bridge.triggered.connect(window.toggle)
    listener = start_global_hotkey(bridge)

    tray = QSystemTrayIcon()
    tray.setIcon(QIcon.fromTheme("application-x-executable"))
    menu = QMenu()
    toggle_action = menu.addAction("Show Min Launcher")
    toggle_action.triggered.connect(window.toggle)
    menu.addSeparator()
    quit_action = menu.addAction("Quit")
    quit_action.triggered.connect(app.quit)
    tray.setContextMenu(menu)
    tray.setToolTip("Min Launcher (Option+Space)")
    tray.activated.connect(lambda reason: window.toggle()
                            if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
