"""
VIGILANT CENTRAL GUI — Virtual Infrastructure Guard & Intelligent Live Analytics Network Technology.

Same visual identity as the original single-file system monitor (dark sidebar with
team photo, red accent, black panels, arc gauges, trend graphs, footer with Export CSV /
PDF Report), but every number comes from the remote Ubuntu VMs.

Threading model
---------------
  * MonitorEngine (central/monitoring.py) polls the VMs in a background thread.
  * Database writes run in the Database writer thread.
  * Process lists, history queries and exports run in short-lived worker threads.
  * Workers NEVER touch Tk widgets: they post messages on self.ui_queue, which the
    Tk main loop drains every 150 ms (_drain_queue). The GUI never blocks.
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import logging
import os
import queue
import re
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import filedialog, messagebox

import customtkinter as ctk

import config
from central import reports
from central.alarm import AlarmPlayer
from central.screenshot import capture_window, to_jpeg
from central.database import Database, DatabaseError
from central.monitoring import (CONNECTING, OFFLINE, ONLINE, RECOVERED, RETRYING, MonitorEngine,
                                format_duration)
from central.discord_notify import is_valid_webhook_url, store_webhook_url
from central.notifications import CONNECTED, ERROR, KIND_LABELS, KINDS, NOT_CONFIGURED, READY, build_default_manager
from central.severity import (LEVEL_COLORS, LEVEL_NAMES, ThresholdError, ThresholdStore, classify,
                              validate_thresholds)
from central.vm_client import VMFetchError, fetch_processes

log = logging.getLogger("vigilant.gui")

# ----------------------------------------------------------------- VIGILANT palette (graphite + red accent)
BG = "#09090b"
PANEL = "#131316"
PANEL_ALT = "#1c1c21"
PANEL_DEEP = "#0e0e11"
PANEL_HOVER = "#232329"
SIDEBAR = "#0d0d10"
BORDER = "#26262c"
BORDER_STRONG = "#34343c"
TEXT = "#f4f4f5"
MUTED = "#9a9aa5"
FAINT = "#5c5c66"
ACCENT = "#e5323e"
ACCENT_DARK = "#8f1720"
ACCENT_MID = "#c4232f"
ACCENT_HOVER = "#e5323e"
GREY = "#6b6b75"
SEV_GREEN, SEV_YELLOW, SEV_ORANGE, SEV_RED = LEVEL_COLORS
TEAL = "#1fd1a5"
TRACK = "#22222a"

# Fonts (Windows ships all of these; Tk silently falls back elsewhere)
UI_FONT = "Segoe UI"
NUM_FONT = "Bahnschrift"          # clean, technical numerals for gauges and KPI values
MONO_FONT = "Cascadia Mono"
ICON_FONT = "Segoe Fluent Icons"


def blend(color: str, bg: str, alpha: float) -> str:
    """Mix two #rrggbb colours (Tk has no transparency) — used for tinted pills, glows and fills."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * alpha + y * (1 - alpha)):02x}" for x, y in zip(a, b))


def font(size=12, weight="normal", family=UI_FONT):
    return ctk.CTkFont(family=family, size=size, weight=weight)
STATUS_COLORS = {ONLINE: SEV_GREEN, RECOVERED: TEAL, RETRYING: SEV_ORANGE, CONNECTING: GREY,
                 OFFLINE: SEV_RED, "PAUSED": GREY}
SEVERITY_COLORS = {"NORMAL": SEV_GREEN, "ELEVATED": SEV_YELLOW, "WARNING": SEV_ORANGE, "CRITICAL": SEV_RED,
                   "OFFLINE": SEV_RED, "RECOVERED": TEAL, "RETRYING": SEV_ORANGE, "UNKNOWN": GREY,
                   "INFO": MUTED, "TEST": "#c07bff",
                   "DISCORD_NOTIFICATION": "#7f8cff", "DISCORD_NOTIFICATION_FAILED": SEV_ORANGE}
METRIC_LABELS = {"CPU": "CPU", "RAM": "MEMORY", "DISK": "DISK"}
# (label, page, Segoe Fluent icon, page subtitle)
NAV_PAGES = [("Dashboard", "Overview", "", "Live telemetry from the Proxmox VMs"),
             ("Processes", "Processes", "", "Processes reported by the node agent"),
             ("History & Alerts", "History & Alerts", "", "Alerts, events and resource samples from PostgreSQL"),
             ("Sessions", "Sessions", "", "Recorded monitoring sessions and their statistics"),
             ("Threshold Settings", "Threshold Settings", "", "Severity levels, alarm and Discord notifications")]
SEVERITY_SHORT = {"DISCORD_NOTIFICATION": "DISCORD", "DISCORD_NOTIFICATION_FAILED": "DISCORD ✗"}


# ----------------------------------------------------------------- helpers
def load_logo():
    """Team photo from assets/vigilant_team.jpg (None if missing)."""
    try:
        from PIL import Image
        if os.path.isfile(config.LOGO_FILE):
            return Image.open(config.LOGO_FILE).convert("RGB")
    except Exception as exc:
        log.info("Team photo not loaded: %s", exc)
    return None


def hhmmss(value) -> str:
    return value.strftime("%H:%M:%S") if isinstance(value, dt.datetime) else "—"


def stamp(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, dt.datetime) else "—"


def parse_user_datetime(text: str, end_of_day: bool = False):
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            value = dt.datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d" and end_of_day:
                value = value.replace(hour=23, minute=59, second=59)
            return value
        except ValueError:
            continue
    raise ValueError(f"Invalid date '{text}'. Use YYYY-MM-DD or YYYY-MM-DD HH:MM")


def write_textbox(box: ctk.CTkTextbox, lines):
    """lines: iterable of str or (text, tag)."""
    box.configure(state="normal")
    box.delete("1.0", "end")
    for line in lines:
        if isinstance(line, tuple):
            box.insert("end", line[0] + "\n", line[1])
        elif line and set(line) == {"-"}:                # table rule → thin, faint line
            box.insert("end", "─" * len(line) + "\n", "RULE")
        else:
            box.insert("end", line + "\n")
    box.configure(state="disabled")


def add_severity_tags(box: ctk.CTkTextbox):
    for name, color in SEVERITY_COLORS.items():
        box.tag_config(name, foreground=color)
    box.tag_config("HEAD", foreground=MUTED)
    box.tag_config("MUTED", foreground=MUTED)
    box.tag_config("RULE", foreground=BORDER_STRONG)


def table_box(master, size=11, **kw):
    """Monospace read-only table in a bordered panel."""
    box = ctk.CTkTextbox(master, fg_color=PANEL, text_color=TEXT, font=(MONO_FONT, size), wrap="none",
                         border_width=1, border_color=BORDER, corner_radius=12,
                         scrollbar_button_color=BORDER_STRONG, scrollbar_button_hover_color=FAINT, **kw)
    try:
        box._textbox.configure(spacing1=2, spacing3=2, padx=6, pady=4)
    except Exception:
        pass
    add_severity_tags(box)
    return box


def accent_button(master, text, command, width=130, height=32, **kw):
    kw.setdefault("font", font(12, "bold"))
    return ctk.CTkButton(master, text=text, command=command, width=width, height=height, corner_radius=8,
                         fg_color=ACCENT_MID, hover_color=ACCENT_HOVER, **kw)


def ghost_button(master, text, command, width=110, height=32, **kw):
    """Secondary action: dark, outlined, red on hover."""
    kw.setdefault("font", font(12))
    return ctk.CTkButton(master, text=text, command=command, width=width, height=height, corner_radius=8,
                         fg_color=PANEL_ALT, hover_color=PANEL_HOVER, border_width=1, border_color=BORDER_STRONG,
                         text_color=TEXT, **kw)


def option_menu(master, values, width=130, command=None):
    return ctk.CTkOptionMenu(master, values=values, width=width, command=command, fg_color=PANEL_ALT,
                             button_color=PANEL_HOVER, button_hover_color=ACCENT_MID, corner_radius=8,
                             dropdown_fg_color=PANEL, dropdown_hover_color=ACCENT_DARK, text_color=TEXT)


def segmented(master, values, command):
    return ctk.CTkSegmentedButton(master, values=values, command=command, corner_radius=8, height=30,
                                  fg_color=PANEL_ALT, selected_color=ACCENT_MID, selected_hover_color=ACCENT_HOVER,
                                  unselected_color=PANEL_ALT, unselected_hover_color=PANEL_HOVER,
                                  text_color=TEXT, font=font(12, "bold"))


def section_title(master, text, sub=None):
    """Card heading: small red caps title with an optional muted line underneath."""
    ctk.CTkLabel(master, text=text.upper(), text_color=ACCENT, font=font(12, "bold")).pack(
        anchor="w", padx=20, pady=(16, 0 if sub else 8))
    if sub:
        ctk.CTkLabel(master, text=sub, text_color=MUTED, font=font(12), wraplength=880,
                     justify="left").pack(anchor="w", padx=20, pady=(2, 10))


def card(master, **kw):
    return ctk.CTkFrame(master, fg_color=PANEL, corner_radius=14, border_width=1, border_color=BORDER, **kw)


class PageStack(ctk.CTkFrame):
    """Minimal replacement for the hidden-strip CTkTabview: same add/set/get API, no reserved header gap."""

    def __init__(self, master, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self._pages, self._current = {}, None

    def add(self, name):
        page = ctk.CTkFrame(self, fg_color="transparent")
        self._pages[name] = page
        if self._current is None:
            self.set(name)
        return page

    def set(self, name):
        if self._current:
            self._pages[self._current].pack_forget()
        self._pages[name].pack(fill="both", expand=True)
        self._current = name

    def get(self):
        return self._current


# ----------------------------------------------------------------- widgets (VIGILANT style)
class MetricCard(ctk.CTkFrame):
    """KPI tile: coloured accent stripe, small caps title, big value, detail line."""

    def __init__(self, master, title, icon="", **kwargs):
        super().__init__(master, fg_color=PANEL, corner_radius=14, border_width=1, border_color=BORDER, **kwargs)
        self.stripe = ctk.CTkFrame(self, width=4, height=40, corner_radius=2, fg_color=FAINT)
        self.stripe.pack(side="left", fill="y", padx=(10, 0), pady=14)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, padx=(12, 12), pady=(10, 10))
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=title.upper(), text_color=MUTED, height=18, font=font(11, "bold")).pack(side="left")
        if icon:
            ctk.CTkLabel(head, text=icon, text_color=FAINT, height=18, font=font(13, family=ICON_FONT)).pack(side="right")
        self.value = ctk.CTkLabel(body, text="—", text_color=TEXT, height=36, anchor="w",
                                  font=font(27, "bold", NUM_FONT))
        self.value.pack(fill="x")
        self.detail = ctk.CTkLabel(body, text="Waiting for first poll", text_color=MUTED, height=16, anchor="w",
                                   font=font(11))
        self.detail.pack(fill="x")

    def set(self, value, detail="", color=ACCENT):
        self.value.configure(text=value, text_color=color)
        self.detail.configure(text=detail)
        self.stripe.configure(fg_color=color)


class GaugeCard(ctk.CTkFrame):
    """The original 270° arc gauge, coloured by severity, with a soft glow under the value arc."""

    def __init__(self, master, title, unit="%", bg=PANEL_DEEP, height=88):
        super().__init__(master, fg_color=bg, corner_radius=12, border_width=1, border_color=BORDER)
        self.bg = bg
        self.unit, self.color, self.value, self.display = unit, SEV_GREEN, 0.0, "—"
        self.canvas = tk.Canvas(self, height=height, width=90, bg=bg, highlightthickness=0)
        self.canvas.pack(fill="x", padx=6, pady=(8, 0))
        ctk.CTkLabel(self, text=title, text_color=MUTED, height=14, font=font(10, "bold")).pack()
        self.level_label = ctk.CTkLabel(self, text="—", text_color=GREY, height=16, font=font(10, "bold"))
        self.level_label.pack(pady=(0, 8))
        self.canvas.bind("<Configure>", lambda _e: self.draw())

    def set(self, value, color, level_text):
        if value is None:
            self.value, self.display = 0.0, "—"
        else:
            self.value = max(0.0, min(float(value), 100.0))
            self.display = f"{self.value:.1f}"
        self.color = color
        self.level_label.configure(text=level_text, text_color=color)
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 80), max(c.winfo_height(), 70)
        size = max(42, min(w - 16, h - 8))
        x0, y0 = (w - size) / 2, 5
        width = max(7, min(10, size // 9))
        box = (x0, y0, x0 + size, y0 + size)
        c.create_arc(*box, start=135, extent=-270, style="arc", outline=TRACK, width=width)
        if self.value > 0:
            extent = -2.7 * self.value
            c.create_arc(*box, start=135, extent=extent, style="arc", outline=blend(self.color, self.bg, .22),
                         width=width + 6)                                   # glow
            c.create_arc(*box, start=135, extent=extent, style="arc", outline=self.color, width=width)
        c.create_text(w / 2, y0 + size * .47, text=self.display, fill=TEXT,
                      font=(NUM_FONT, max(12, min(19, int(size * .22))), "bold"))
        c.create_text(w / 2, y0 + size * .70, text=self.unit, fill=FAINT, font=(UI_FONT, 8))


class TrendGraph(ctk.CTkFrame):
    """VIGILANT MiniGraph extended to several series: filled area for the first series, scale 0/50/100."""

    def __init__(self, master, title, series, bg=PANEL_DEEP, height=78):
        super().__init__(master, fg_color=bg, corner_radius=12, border_width=1, border_color=BORDER)
        self.bg = bg
        self.series = series          # [(key, label, color)]
        self.data = {k: [] for k, _, _ in series}
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(8, 0))
        ctk.CTkLabel(head, text=title, text_color=MUTED, height=16, font=font(10, "bold")).pack(side="left")
        self.legend = {}
        for key, label, color in reversed(series):
            lbl = ctk.CTkLabel(head, text=f"● {label} —", text_color=color, height=16, font=font(10, "bold"))
            lbl.pack(side="right", padx=(10, 0))
            self.legend[key] = (lbl, label)
        self.canvas = tk.Canvas(self, height=height, bg=bg, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.canvas.bind("<Configure>", lambda _e: self.draw())

    def set_data(self, data: dict, current: dict):
        for key in self.data:
            self.data[key] = list(data.get(key, []))
            lbl, label = self.legend[key]
            v = current.get(key)
            lbl.configure(text=f"● {label} {v:.0f}%" if v is not None else f"● {label} —")
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 40 or h < 20:
            return
        left = 22                                     # room for the scale labels
        y = lambda v: h - 3 - min(max(v, 0) / 100.0, 1.0) * (h - 8)  # noqa: E731
        for pct in (0, 50, 100):
            c.create_line(left, y(pct), w, y(pct), fill=TRACK if pct else BORDER, dash=() if pct == 0 else (2, 4))
            c.create_text(left - 5, y(pct), text=str(pct), anchor="e", fill=FAINT, font=(UI_FONT, 7))
        for i, (key, _label, color) in enumerate(self.series):
            vals = self.data[key]
            if len(vals) < 2:
                continue
            span = max(len(vals) - 1, 1)
            pts = []
            for j, v in enumerate(vals):
                pts.extend((left + j * (w - left - 2) / span + 1, y(v)))
            if i == 0:                                # filled area under the primary series
                c.create_polygon(left + 1, y(0), *pts, pts[-2], y(0), fill=blend(color, self.bg, .16), outline="",
                                 smooth=True)
            c.create_line(*pts, fill=color, width=2, smooth=True)
            c.create_oval(pts[-2] - 3, pts[-1] - 3, pts[-2] + 3, pts[-1] + 3, fill=color, outline="")


class VMPanel(ctk.CTkFrame):
    """One monitored VM: identity, status badge, CPU/RAM/DISK gauges, trend, last update."""

    def __init__(self, master, vm: config.VMConfig):
        super().__init__(master, fg_color=PANEL, corner_radius=16, border_width=2, border_color=BORDER)
        self.vm = vm
        self.alarm = False
        self.base_border = BORDER
        self._current_border = BORDER

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 0))
        ctk.CTkLabel(head, text=vm.vm_id, text_color=TEXT, height=30,
                     font=font(24, "bold", NUM_FONT)).pack(side="left")
        self.pill = ctk.CTkLabel(head, text="● CONNECTING", fg_color=blend(GREY, PANEL, .18), corner_radius=13,
                                 text_color=GREY, width=124, height=26, font=font(11, "bold"))
        self.pill.pack(side="right")
        self.host_label = ctk.CTkLabel(self, text=vm.hostname, text_color=TEXT, anchor="w", height=20,
                                       font=font(13, "bold"))
        self.host_label.pack(fill="x", padx=16, pady=(2, 0))
        ctk.CTkLabel(self, text=f"{vm.ip}:{vm.port}   ·   Proxmox VMID {vm.proxmox_vmid}", text_color=MUTED,
                     anchor="w", height=18, font=font(11)).pack(fill="x", padx=16, pady=(0, 8))

        gauges = ctk.CTkFrame(self, fg_color="transparent")
        gauges.pack(fill="x", padx=12)
        self.gauges = {}
        for col, metric in enumerate(config.METRICS):
            g = GaugeCard(gauges, METRIC_LABELS[metric])
            g.grid(row=0, column=col, sticky="nsew", padx=3, pady=3)
            gauges.grid_columnconfigure(col, weight=1, uniform="g")
            self.gauges[metric] = g

        self.trend = TrendGraph(self, "TREND · 5 MIN", [("CPU", "CPU", ACCENT), ("RAM", "RAM", "#c9c9d1")])
        self.trend.pack(fill="x", padx=15, pady=(6, 6))

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(fill="x", padx=16)
        self.update_label = ctk.CTkLabel(info, text="Waiting for first health check…", text_color=TEXT,
                                         anchor="w", height=18, font=font(11, "bold"))
        self.update_label.pack(fill="x")
        self.extra_label = ctk.CTkLabel(info, text="", text_color=MUTED, anchor="w", height=16, font=font(10))
        self.extra_label.pack(fill="x")
        self.message_label = ctk.CTkLabel(self, text="", text_color=MUTED, anchor="w", justify="left", height=16,
                                          wraplength=300, font=font(10))
        self.message_label.pack(fill="x", padx=16, pady=(0, 12))
        self.bind("<Configure>", lambda e: self.message_label.configure(wraplength=max(200, e.width - 36)))

    def render(self, snap: dict, thresholds: dict, paused: bool = False):
        status = "PAUSED" if paused else snap["status"]
        failures = snap["failures"]
        pill = status
        if status in (RETRYING, CONNECTING) and failures:
            pill = f"{status} {failures}/{config.FAILURE_LIMIT}"
        color = STATUS_COLORS.get(status, GREY)
        self.pill.configure(text=f"● {pill}", text_color=color, fg_color=blend(color, PANEL, .16))

        reported = snap.get("reported_hostname")
        host = self.vm.hostname if not reported or reported == self.vm.hostname \
            else f"{self.vm.hostname}  (reports: {reported})"
        self.host_label.configure(text=host)

        offline = snap["status"] == OFFLINE
        worst = 0
        for metric, gauge in self.gauges.items():
            value = snap["values"].get(metric)
            if offline:
                gauge.set(None, SEV_RED, "OFFLINE")
            elif value is None:
                gauge.set(None, GREY, "NO DATA")
            else:
                lvl = classify(value, thresholds[metric])
                worst = max(worst, lvl)
                gauge.set(value, LEVEL_COLORS[lvl], LEVEL_NAMES[lvl])
        self.trend.set_data(snap["history"], {} if offline else snap["values"])

        last = snap.get("last_update")
        if last:
            age = (dt.datetime.now() - last).total_seconds()
            stale = "  (stale)" if snap["status"] in (RETRYING, OFFLINE) or paused else ""
            self.update_label.configure(text=f"Last update {last:%H:%M:%S}  ·  {format_duration(age)} ago{stale}")
        else:
            self.update_label.configure(text="Waiting for first health check…")
        extra = snap.get("extra") or {}
        parts = []
        if extra.get("uptime_seconds"):
            parts.append(f"Uptime {format_duration(extra['uptime_seconds'])}")
        if extra.get("load_avg") is not None:
            parts.append(f"Load {extra['load_avg']:.2f}")
        if extra.get("process_count"):
            parts.append(f"{int(extra['process_count'])} processes")
        if extra.get("ram_total_gb"):
            parts.append(f"RAM {extra.get('ram_used_gb') or 0:.1f}/{extra['ram_total_gb']:.1f} GB")
        self.extra_label.configure(text="   ·   ".join(parts))

        if offline:
            since = snap.get("offline_since")
            down = format_duration((dt.datetime.now() - since).total_seconds()) if since else "?"
            msg, color = f"⚠ OFFLINE since {hhmmss(since)} ({down})  ·  {snap['last_error']}", SEV_RED
        elif snap["status"] == RETRYING or (snap["status"] == CONNECTING and failures):
            msg, color = (f"Health check failed {failures}/{config.FAILURE_LIMIT} — not offline yet  ·  "
                          f"{snap['last_error']}"), SEV_ORANGE
        elif snap["status"] == RECOVERED:
            msg, color = f"✔ RECOVERED at {hhmmss(snap.get('recovered_at'))}", TEAL
        elif snap.get("source"):
            msg, color = f"Data source  {self.vm.base_url}{snap['source']}", FAINT
        else:
            msg, color = "", MUTED
        self.message_label.configure(text=msg, text_color=color)

        if offline:
            self.base_border = SEV_RED
        elif worst >= 2:
            self.base_border = LEVEL_COLORS[worst]
        else:
            self.base_border = BORDER
        if not self.alarm:
            self._set_border(self.base_border)

    def apply_blink(self, on: bool):
        self._set_border((SEV_RED if on else "#4a0f14") if self.alarm else self.base_border)

    def _set_border(self, color):
        if color != self._current_border:
            self._current_border = color
            self.configure(border_color=color)


class NavItem(ctk.CTkFrame):
    """Sidebar entry: red indicator bar + Fluent icon + label; hover and active states."""

    def __init__(self, master, icon, text, command):
        super().__init__(master, fg_color="transparent", corner_radius=9, height=40)
        self.pack_propagate(False)
        self.command, self.active = command, False
        self.bar = ctk.CTkFrame(self, width=3, height=20, corner_radius=2, fg_color=SIDEBAR)
        self.bar.pack(side="left", padx=(4, 0))
        self.icon = ctk.CTkLabel(self, text=icon, width=30, text_color=MUTED, font=font(15, family=ICON_FONT))
        self.icon.pack(side="left", padx=(8, 4))
        self.label = ctk.CTkLabel(self, text=text, anchor="w", text_color=MUTED, font=font(13))
        self.label.pack(side="left", fill="x", expand=True)
        for w in (self, self.icon, self.label, self.bar):
            w.bind("<Button-1>", lambda _e: self.command())
            w.bind("<Enter>", lambda _e: self._hover(True))
            w.bind("<Leave>", lambda _e: self.after(15, self._check_leave))
            w.configure(cursor="hand2")

    def _hover(self, on):
        if not self.active:
            self.configure(fg_color=PANEL_HOVER if on else "transparent")
            self.label.configure(text_color=TEXT if on else MUTED)

    def _check_leave(self):
        try:
            widget = self.winfo_containing(*self.winfo_pointerxy())
        except (KeyError, tk.TclError):
            widget = None
        while widget is not None:
            if widget is self:
                return
            widget = widget.master
        self._hover(False)

    def set_active(self, active):
        self.active = active
        self.configure(fg_color=blend(ACCENT, SIDEBAR, .14) if active else "transparent")
        self.bar.configure(fg_color=ACCENT if active else SIDEBAR)
        self.icon.configure(text_color=ACCENT if active else MUTED)
        self.label.configure(text_color=TEXT if active else MUTED, font=font(13, "bold" if active else "normal"))


# ----------------------------------------------------------------- main window
class CentralApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(config.APP_NAME)
        self.geometry("1340x860")
        self.minsize(900, 620)
        self.configure(fg_color=BG)

        self.ui_queue: queue.Queue = queue.Queue()
        self.thresholds = ThresholdStore()
        self.db = Database(config.DB_CONFIG, notify=self.ui_queue.put)
        self.notifier = build_default_manager(listener=lambda kind, payload: self.ui_queue.put(("notify", kind, payload)))
        self.notifier.screenshot_provider = self._dashboard_screenshot
        self._shot_cache = None
        self.engine = None
        self.session_id = None
        self.latest = None
        self.recent_events = deque(maxlen=7)
        self.retention_days = config.RETENTION_DAYS
        self._closing = False
        self._starting = False
        self._stopping = False
        self._db_connecting = False
        self._db_write_error = None
        self._blink = False
        self._test_until = 0.0

        self.alarm = AlarmPlayer(self, config.ALARM_WAV_CANDIDATES)
        self.alarm_enabled = True
        self.alarm_muted: set = set()
        self.alarm_items: list = []

        self._proc_vm = config.VMS[0].vm_id
        self._proc_sort = "cpu"
        self._proc_fetching = False
        self._proc_last = 0.0
        self._proc_token = 0

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.after(150, self._drain_queue)
        self.after(600, self._blink_tick)
        self.after(1000, self._process_tick)
        self.after(1000, self._clock_tick)
        self.after(250, lambda: self.connect_database(start_monitoring=True, interactive=True))
        self.after(24 * 3600 * 1000, self.periodic_cleanup)

    # =============================================================== layout
    def _build_ui(self):
        shell = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        shell.pack(fill="both", expand=True)
        self._build_sidebar(shell)

        main = ctk.CTkFrame(shell, fg_color=BG, corner_radius=0)
        main.pack(side="left", fill="both", expand=True, padx=(18, 18))
        self._build_header(main)
        self.tabs = PageStack(main)
        self.tabs.pack(fill="both", expand=True, pady=(0, 12))
        self.overview = self.tabs.add("Overview")
        self.processes_tab = self.tabs.add("Processes")
        self.history_tab = self.tabs.add("History & Alerts")
        self.sessions_tab = self.tabs.add("Sessions")
        self.threshold_tab = self.tabs.add("Threshold Settings")

        self._build_overview()
        self._build_processes_tab()
        self._build_history_tab()
        self._build_sessions_tab()
        self._build_threshold_tab()
        self._build_footer()
        self._highlight_nav("Overview")

    def _build_header(self, main):
        bar = ctk.CTkFrame(main, fg_color="transparent", height=70)
        bar.pack(fill="x", pady=(14, 8))
        titles = ctk.CTkFrame(bar, fg_color="transparent")
        titles.pack(side="left")
        self.page_title = ctk.CTkLabel(titles, text="Dashboard", text_color=TEXT, height=32, anchor="w",
                                       font=font(24, "bold"))
        self.page_title.pack(anchor="w")
        self.page_sub = ctk.CTkLabel(titles, text=NAV_PAGES[0][3], text_color=MUTED, height=18, anchor="w",
                                     font=font(12))
        self.page_sub.pack(anchor="w")
        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.pack(side="right")
        self.live_chip = ctk.CTkLabel(right, text="○  STARTING", text_color=MUTED, fg_color=PANEL_ALT,
                                      corner_radius=14, height=28, width=128, font=font(11, "bold"))
        self.live_chip.pack(side="right")
        self.clock_label = ctk.CTkLabel(right, text="", text_color=MUTED, font=font(14, family=NUM_FONT))
        self.clock_label.pack(side="right", padx=(0, 16))

    def _build_sidebar(self, shell):
        sb = ctk.CTkFrame(shell, fg_color=SIDEBAR, width=236, corner_radius=0)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)
        ctk.CTkFrame(shell, width=1, fg_color=BORDER, corner_radius=0).pack(side="left", fill="y")
        logo = load_logo()
        if logo is not None:
            width = 204
            height = round(width * logo.height / logo.width)
            self.logo_ctk = ctk.CTkImage(light_image=logo, dark_image=logo, size=(width, height))
            ctk.CTkLabel(sb, image=self.logo_ctk, text="", corner_radius=12).pack(pady=(16, 10))
        else:
            ctk.CTkLabel(sb, text="V", text_color=ACCENT, font=font(42, "bold")).pack(pady=(20, 6))
        ctk.CTkLabel(sb, text="V I G I L A N T", text_color=TEXT, height=24,
                     font=font(19, "bold", NUM_FONT)).pack()
        ctk.CTkLabel(sb, text="Virtual Infrastructure Guard &\nIntelligent Live Analytics Network Technology",
                     text_color=FAINT, justify="center", wraplength=210, font=font(9)).pack(pady=(2, 0))
        ctk.CTkLabel(sb, text="PROXMOX  ·  VM MONITOR", text_color=ACCENT, height=18,
                     font=font(10, "bold")).pack(pady=(4, 14))

        self.nav_buttons = {}
        for label, tab_name, icon, _sub in NAV_PAGES:
            item = NavItem(sb, icon, label, lambda n=tab_name: self.navigate(n))
            item.pack(fill="x", padx=12, pady=1)
            self.nav_buttons[tab_name] = item

        box = ctk.CTkFrame(sb, fg_color=PANEL, corner_radius=12, border_width=1, border_color=BORDER)
        box.pack(fill="x", padx=12, pady=(16, 0))
        ctk.CTkLabel(box, text="MONITORING", text_color=FAINT, height=16,
                     font=font(10, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
        self.monitor_btn = accent_button(box, "Starting…", self.toggle_monitoring, width=186, height=34,
                                         state="disabled")
        self.monitor_btn.pack(padx=10, pady=(4, 2))
        self.monitor_status = ctk.CTkLabel(box, text="○ STARTING", text_color=MUTED, height=16,
                                           font=font(10, "bold"))
        self.monitor_status.pack(anchor="w", padx=12)
        ctk.CTkFrame(box, height=1, fg_color=BORDER).pack(fill="x", padx=12, pady=(8, 6))
        ctk.CTkLabel(box, text="ALARM", text_color=FAINT, height=16,
                     font=font(10, "bold")).pack(anchor="w", padx=12)
        self.alarm_switch = ctk.CTkSwitch(box, text="Audible alarm ON", progress_color=ACCENT_MID,
                                          button_color=TEXT, button_hover_color=TEXT, fg_color=BORDER_STRONG,
                                          text_color=TEXT, command=self._toggle_alarm_enabled, font=font(12))
        self.alarm_switch.select()
        self.alarm_switch.pack(anchor="w", padx=12, pady=(4, 4))
        self.mute_btn = ghost_button(box, "Mute alarm", self.mute_alarm, width=186, height=30, state="disabled")
        self.mute_btn.pack(padx=10, pady=2)
        self.alarm_status = ctk.CTkLabel(box, text="ALARM ARMED", text_color=SEV_GREEN, height=16,
                                         font=font(10, "bold"))
        self.alarm_status.pack(anchor="w", padx=12, pady=(0, 10))

        dbbox = ctk.CTkFrame(sb, fg_color="transparent")
        dbbox.pack(side="bottom", fill="x", padx=12, pady=(0, 14))
        self.db_status_label = ctk.CTkLabel(dbbox, text="CONNECTING • POSTGRESQL", text_color=MUTED,
                                            fg_color=PANEL, corner_radius=10, height=28, font=font(10, "bold"))
        self.db_status_label.pack(fill="x")
        self.db_retry_btn = ghost_button(dbbox, "Retry DB connection", lambda: self.connect_database(interactive=True),
                                         width=186, height=26)

    def _build_footer(self):
        footer = ctk.CTkFrame(self, fg_color=SIDEBAR, corner_radius=0, height=36)
        footer.pack(fill="x", side="bottom", before=self.winfo_children()[0])
        footer.pack_propagate(False)
        ctk.CTkFrame(self, height=1, fg_color=BORDER, corner_radius=0).pack(fill="x", side="bottom", before=footer)
        self.footer_status = ctk.CTkLabel(footer, text="Starting VIGILANT central monitor…", text_color=MUTED,
                                          font=font(11, "bold"), anchor="w")
        self.footer_status.pack(side="left", padx=16, fill="x", expand=True)
        actions = ctk.CTkFrame(footer, fg_color="transparent")
        actions.pack(side="right", padx=12)
        ghost_button(actions, "⭳  Export CSV", self.export_csv, width=112, height=26).pack(side="left", padx=4)
        accent_button(actions, "PDF Report", lambda: self.export_pdf(), width=108, height=26).pack(side="left", padx=4)

    # ---------------------------------------------------------------- dashboard
    def _build_overview(self):
        page = ctk.CTkScrollableFrame(self.overview, fg_color="transparent", scrollbar_button_color=BORDER_STRONG,
                                      scrollbar_button_hover_color=FAINT)
        page.pack(fill="both", expand=True)
        cards = ctk.CTkFrame(page, fg_color="transparent")
        cards.pack(fill="x", pady=(0, 6))
        self.card_online = MetricCard(cards, "VMs online", "")
        self.card_alarms = MetricCard(cards, "Active alarms", "")
        self.card_peak = MetricCard(cards, "Highest CPU", "")
        self.card_poll = MetricCard(cards, "Last poll", "")
        for i, tile in enumerate((self.card_online, self.card_alarms, self.card_peak, self.card_poll)):
            tile.grid(row=0, column=i, sticky="nsew", padx=4)
            cards.grid_columnconfigure(i, weight=1, uniform="cards")

        self.vm_grid = ctk.CTkFrame(page, fg_color="transparent")
        self.vm_grid.pack(fill="x")
        self.vm_panels = {vm.vm_id: VMPanel(self.vm_grid, vm) for vm in config.VMS}
        self._vm_cols = 0
        self.vm_grid.bind("<Configure>", self._layout_vm_panels)
        self.after(80, self._layout_vm_panels)

        events = card(page)
        events.pack(fill="x", pady=(8, 4), padx=4)
        head = ctk.CTkFrame(events, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(head, text="RECENT ALERTS & EVENTS", text_color=TEXT, font=font(12, "bold")).pack(side="left")
        ctk.CTkLabel(head, text="● LIVE · LAST 7", text_color=ACCENT, font=font(10, "bold")).pack(side="right")
        self.event_rows = ctk.CTkFrame(events, fg_color="transparent")
        self.event_rows.pack(fill="x", padx=10, pady=(0, 10))
        self._render_recent_events()

    def _layout_vm_panels(self, _event=None):
        width = self.vm_grid.winfo_width()
        cols = min(3, len(self.vm_panels)) if width >= 900 else 1
        if cols == self._vm_cols:
            return
        self._vm_cols = cols
        for col in range(3):
            self.vm_grid.grid_columnconfigure(col, weight=1 if col < cols else 0, uniform="vm" if col < cols else "")
        for i, panel in enumerate(self.vm_panels.values()):
            panel.grid(row=i // cols, column=i % cols, sticky="nsew", padx=4, pady=4)

    def _render_recent_events(self):
        """Coalesce: a burst of events (e.g. 6 alerts in one poll) redraws the list once."""
        if not getattr(self, "_events_dirty", False):
            self._events_dirty = True
            self.after_idle(self._draw_recent_events)

    def _draw_recent_events(self):
        self._events_dirty = False
        if not hasattr(self, "_event_pool"):          # fixed rows, created once and only reconfigured
            fonts = (font(11, family=NUM_FONT), font(11, "bold"), font(10, "bold"), font(11))
            self._event_empty = ctk.CTkLabel(
                self.event_rows, text="No events yet — they appear here live (offline, recovery, thresholds).",
                text_color=MUTED, font=fonts[3])
            self._event_pool = []
            for i in range(self.recent_events.maxlen):
                row = ctk.CTkFrame(self.event_rows, fg_color=PANEL_DEEP if i % 2 == 0 else "transparent",
                                   corner_radius=8, height=30)
                t = ctk.CTkLabel(row, width=70, anchor="w", text_color=MUTED, font=fonts[0])
                t.pack(side="left", padx=(10, 0))
                vm = ctk.CTkLabel(row, width=66, anchor="w", text_color=TEXT, font=fonts[1])
                vm.pack(side="left")
                badge = ctk.CTkLabel(row, width=96, height=22, corner_radius=11, font=fonts[2])
                badge.pack(side="left", padx=(0, 12), pady=4)
                msg = ctk.CTkLabel(row, anchor="w", text_color=TEXT, font=fonts[3])
                msg.pack(side="left", fill="x", expand=True)
                self._event_pool.append((row, t, vm, badge, msg))
        events = list(self.recent_events)
        if events:
            self._event_empty.pack_forget()
        else:
            self._event_empty.pack(anchor="w", padx=6, pady=6)
        for i, (row, t, vm, badge, msg) in enumerate(self._event_pool):
            if i >= len(events):
                row.pack_forget()
                continue
            ev = events[i]
            sev = ev["severity"]
            color = SEVERITY_COLORS.get(sev, MUTED)
            t.configure(text=hhmmss(ev["time"]))
            vm.configure(text=ev.get("vm_id") or "SYSTEM")
            badge.configure(text=SEVERITY_SHORT.get(sev, sev), text_color=color, fg_color=blend(color, PANEL, .16))
            msg.configure(text=ev["message"][:150])
            if not row.winfo_manager():                  # not packed yet (works while the page is hidden)
                row.pack(fill="x", pady=1)

    # ---------------------------------------------------------------- processes
    def _build_processes_tab(self):
        header = ctk.CTkFrame(self.processes_tab, fg_color="transparent")
        header.pack(fill="x", padx=4, pady=(0, 6))
        ctk.CTkLabel(header, text="VM", text_color=MUTED, font=font(12)).pack(side="left", padx=(2, 8))
        self.proc_vm_btn = segmented(header, [vm.vm_id for vm in config.VMS], self._on_proc_vm)
        self.proc_vm_btn.set(self._proc_vm)
        self.proc_vm_btn.pack(side="left")
        accent_button(header, "⟳  Refresh", lambda: self.refresh_processes(force=True), width=100).pack(side="right")
        self.proc_sort_btn = segmented(header, ["CPU %", "Memory %"], self._on_proc_sort)
        self.proc_sort_btn.set("CPU %")
        self.proc_sort_btn.pack(side="right", padx=10)
        ctk.CTkLabel(header, text="Sort by", text_color=MUTED, font=font(12)).pack(side="right")
        self.proc_info = ctk.CTkLabel(self.processes_tab, text="Select a VM — processes are read from the remote node agent.",
                                      text_color=MUTED, anchor="w", font=font(12))
        self.proc_info.pack(fill="x", padx=6, pady=(0, 6))
        self.process_box = table_box(self.processes_tab, size=12)
        self.process_box.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        write_textbox(self.process_box, ["Open this page to load processes from the selected VM…"])

    def _on_proc_vm(self, value):
        self._proc_vm = value
        write_textbox(self.process_box, [f"Loading processes from {value}…"])
        self.refresh_processes(force=True)

    def _on_proc_sort(self, value):
        self._proc_sort = "mem" if value.startswith("Mem") else "cpu"
        self.refresh_processes(force=True)

    def _process_tick(self):
        if self._closing:
            return
        if self.tabs.get() == "Processes" and not self._proc_fetching and \
                time.monotonic() - self._proc_last >= config.PROCESS_REFRESH_SECONDS:
            self.refresh_processes()
        self.after(1000, self._process_tick)

    def refresh_processes(self, force=False):
        if self._proc_fetching and not force:
            return
        vm = next(v for v in config.VMS if v.vm_id == self._proc_vm)
        sort = self._proc_sort
        self._proc_token += 1
        token = self._proc_token
        self._proc_fetching = True
        self._proc_last = time.monotonic()
        path = None
        if self.latest:
            path = self.latest["vms"][vm.vm_id].get("metrics_path")

        def worker():
            try:
                rows, source, total = fetch_processes(vm, sort, config.PROCESS_LIMIT, path)
                result = ("ok", rows, source, total)
            except VMFetchError as exc:
                result = ("error", str(exc))
            except Exception as exc:
                result = ("error", f"{type(exc).__name__}: {exc}")
            self._post(lambda: self._show_processes(token, vm, sort, result))

        threading.Thread(target=worker, name="process-fetch", daemon=True).start()

    def _show_processes(self, token, vm, sort, result):
        if token != self._proc_token or self._closing:
            return
        self._proc_fetching = False
        now = dt.datetime.now()
        if result[0] == "error":
            self.proc_info.configure(text=f"{vm.vm_id} • {vm.hostname} • {vm.ip} — unavailable at {now:%H:%M:%S}",
                                     text_color=SEV_ORANGE)
            write_textbox(self.process_box, [
                (f"Could not read the process list from {vm.vm_id} ({vm.base_url})", "WARNING"), "",
                f"Reason: {result[1]}", "",
                "The VM may be OFFLINE or the node agent is not running.",
                "Check:  python3 monitor.py --mode node   on the VM, and the Dashboard status."])
            return
        _, rows, source, total = result
        th = self.thresholds.all()
        count = f"{total} processes" if total is not None else f"top {len(rows)} processes"
        self.proc_info.configure(
            text=f"{vm.vm_id} • {vm.hostname} • {vm.ip} — {count} • sorted by {'memory' if sort == 'mem' else 'CPU'}"
                 f" • source {source} • updated {now:%H:%M:%S}", text_color=MUTED)
        lines = [(f"{'PID':>7}  {'CPU%':>6}  {'MEM%':>6}  {'RSS MB':>8}  PROCESS", "HEAD"), "-" * 78]
        for p in rows:
            lvl = max(classify(p["cpu"], th["CPU"]), classify(p["mem"], th["RAM"]))
            rss = f"{p['rss_mb']:>8.1f}" if p.get("rss_mb") is not None else f"{'—':>8}"
            text = f"{p['pid']:>7}  {p['cpu']:>6.1f}  {p['mem']:>6.1f}  {rss}  {p['name'][:48]}"
            lines.append((text, LEVEL_NAMES[lvl]) if lvl >= 2 else text)
        if not rows:
            lines.append("The node returned an empty process list.")
        write_textbox(self.process_box, lines)

    # ---------------------------------------------------------------- history
    def _build_history_tab(self):
        filters = card(self.history_tab)
        filters.pack(fill="x", padx=4, pady=(0, 8))
        bar1 = ctk.CTkFrame(filters, fg_color="transparent")
        bar1.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(bar1, text="VM", text_color=MUTED).pack(side="left", padx=(6, 4))
        self.f_vm = option_menu(bar1, ["All"] + [vm.vm_id for vm in config.VMS], width=95)
        self.f_vm.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(bar1, text="Severity", text_color=MUTED).pack(side="left", padx=(0, 4))
        self.f_sev = option_menu(bar1, ["All", "NORMAL", "ELEVATED", "WARNING", "CRITICAL", "OFFLINE",
                                        "RECOVERED", "UNKNOWN", "INFO", "TEST", "DISCORD_NOTIFICATION",
                                        "DISCORD_NOTIFICATION_FAILED"], width=120)
        self.f_sev.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(bar1, text="Event", text_color=MUTED).pack(side="left", padx=(0, 4))
        self.f_type = option_menu(bar1, ["All", "THRESHOLD", "RESOLVED", "OFFLINE", "RECOVERED", "INFO", "TEST"],
                                  width=120)
        self.f_type.pack(side="left")

        bar2 = ctk.CTkFrame(filters, fg_color="transparent")
        bar2.pack(fill="x", padx=10, pady=(4, 10))
        ctk.CTkLabel(bar2, text="From", text_color=MUTED).pack(side="left", padx=(6, 4))
        self.f_from = ctk.CTkEntry(bar2, width=150, placeholder_text="YYYY-MM-DD HH:MM", fg_color=PANEL_DEEP,
                                   border_color=BORDER_STRONG, corner_radius=8)
        self.f_from.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(bar2, text="To", text_color=MUTED).pack(side="left", padx=(0, 4))
        self.f_to = ctk.CTkEntry(bar2, width=150, placeholder_text="YYYY-MM-DD HH:MM", fg_color=PANEL_DEEP,
                                 border_color=BORDER_STRONG, corner_radius=8)
        self.f_to.pack(side="left", padx=(0, 10))
        accent_button(bar2, "Apply filters", self.refresh_history, width=110).pack(side="left", padx=3)
        ghost_button(bar2, "Today", self._filter_today, width=70).pack(side="left", padx=3)
        ghost_button(bar2, "Clear", self._filter_clear, width=70).pack(side="left", padx=3)
        ghost_button(bar2, "Clean old records", self.manual_cleanup, width=140).pack(side="right", padx=4)

        self.history_note = ctk.CTkLabel(self.history_tab, text="Alerts & events (newest first)", text_color=TEXT,
                                         anchor="w", font=font(12, "bold"))
        self.history_note.pack(fill="x", padx=8)
        self.alert_box = table_box(self.history_tab)
        self.alert_box.pack(fill="both", expand=True, padx=4, pady=(4, 10))
        ctk.CTkLabel(self.history_tab, text="Resource samples (newest first)", text_color=TEXT,
                     anchor="w", font=font(12, "bold")).pack(fill="x", padx=8)
        self.sample_box = table_box(self.history_tab)
        self.sample_box.pack(fill="both", expand=True, padx=4, pady=(4, 4))

    def _filter_today(self):
        for entry, value in ((self.f_from, dt.date.today().strftime("%Y-%m-%d 00:00")),
                             (self.f_to, dt.date.today().strftime("%Y-%m-%d 23:59"))):
            entry.delete(0, "end")
            entry.insert(0, value)
        self.refresh_history()

    def _filter_clear(self):
        for menu in (self.f_vm, self.f_sev, self.f_type):
            menu.set("All")
        for entry in (self.f_from, self.f_to):
            entry.delete(0, "end")
        self.refresh_history()

    def refresh_history(self):
        if not self.db.ok:
            msg = self._db_unavailable_lines()
            write_textbox(self.alert_box, msg)
            write_textbox(self.sample_box, msg)
            return
        try:
            since = parse_user_datetime(self.f_from.get())
            until = parse_user_datetime(self.f_to.get(), end_of_day=True)
        except ValueError as exc:
            self.history_note.configure(text=str(exc), text_color=SEV_ORANGE)
            return
        pick = lambda m: None if m.get() == "All" else m.get()  # noqa: E731
        filters = {"vm_id": pick(self.f_vm), "severity": pick(self.f_sev), "since": since, "until": until}
        event_type = pick(self.f_type)
        self.history_note.configure(text="Loading…", text_color=MUTED)

        def worker():
            try:
                events = self.db.query_events(limit=500, event_type=event_type, **filters)
                samples = self.db.query_samples(limit=400, **filters)
                self._post(lambda: self._show_history(events, samples))
            except Exception as exc:
                err = str(exc)
                self._post(lambda: self.history_note.configure(text=f"Could not load history: {err}",
                                                               text_color=SEV_RED))

        threading.Thread(target=worker, name="history", daemon=True).start()

    def _show_history(self, events, samples):
        self.history_note.configure(text=f"Alerts & events (newest first) — {len(events)} shown, max 500",
                                    text_color=MUTED)
        lines = [(f"{'TIMESTAMP':<19}  {'VM':<6} {'IP':<15} {'EVENT':<10} {'METRIC':<6} {'VALUE':>7}  "
                  f"{'SEVERITY':<9} {'STATUS':<10} MESSAGE", "HEAD"), "-" * 140]
        for e in events:
            value = e["metric_value"]
            if e["event_type"] == "RECOVERED" and value is not None:
                vtxt = format_duration(value)
            else:
                vtxt = f"{value:.1f}%" if value is not None else "—"
            lines.append((f"{stamp(e['occurred_at']):<19}  {e['vm_id'] or '—':<6} {e['ip_address'] or '':<15} "
                          f"{e['event_type']:<10} {e['metric'] or '':<6} {vtxt:>7}  "
                          f"{SEVERITY_SHORT.get(e['severity'], e['severity']):<9} "
                          f"{e['vm_status'] or '':<10} {e['message']}", e["severity"]))
        if not events:
            lines.append("No events match these filters.")
        write_textbox(self.alert_box, lines)

        pct = lambda v: f"{v:6.1f}%" if v is not None else "     — "  # noqa: E731
        lines = [(f"{'TIMESTAMP':<19}  {'VM':<6} {'IP':<15} {'CPU':>7} {'RAM':>7} {'DISK':>7}  "
                  f"{'SEVERITY':<9} STATUS", "HEAD"), "-" * 90]
        for s in samples:
            lines.append((f"{stamp(s['sampled_at']):<19}  {s['vm_id']:<6} {s['ip_address'] or '':<15} "
                          f"{pct(s['cpu_percent'])} {pct(s['ram_percent'])} {pct(s['disk_percent'])}  "
                          f"{s['severity']:<9} {s['status']}", s["severity"]))
        if not samples:
            lines.append("No samples match these filters.")
        write_textbox(self.sample_box, lines)

    # ---------------------------------------------------------------- sessions
    def _build_sessions_tab(self):
        top = card(self.sessions_tab)
        top.pack(fill="x", padx=4, pady=(0, 8))
        ghost_button(top, "⟳  Refresh sessions", self.refresh_sessions, width=150).pack(side="left", padx=(10, 5),
                                                                                       pady=10)
        ctk.CTkLabel(top, text="Session", text_color=MUTED, font=font(12)).pack(side="left", padx=(14, 6))
        self.session_menu = option_menu(top, ["—"], width=110)
        self.session_menu.pack(side="left")
        accent_button(top, "View details", self.view_session_details, width=110).pack(side="left", padx=6)
        ghost_button(top, "PDF for this session", self._pdf_selected_session, width=160).pack(side="left", padx=2)
        self.session_info = ctk.CTkLabel(top, text="Current session: —", text_color=MUTED, font=font(12, "bold"))
        self.session_info.pack(side="right", padx=14)
        self.sessions_box = table_box(self.sessions_tab, height=230)
        self.sessions_box.pack(fill="both", expand=True, padx=4, pady=(0, 10))
        ctk.CTkLabel(self.sessions_tab, text="Session details", text_color=TEXT, anchor="w",
                     font=font(12, "bold")).pack(fill="x", padx=8)
        self.session_detail_box = table_box(self.sessions_tab)
        self.session_detail_box.pack(fill="both", expand=True, padx=4, pady=(4, 4))
        write_textbox(self.session_detail_box, [("Pick a session above and press View details.", "MUTED")])

    def refresh_sessions(self):
        if not self.db.ok:
            write_textbox(self.sessions_box, self._db_unavailable_lines())
            return

        def worker():
            try:
                rows = self.db.sessions_summary(200)
                self._post(lambda: self._show_sessions(rows))
            except Exception as exc:
                err = str(exc)
                self._post(lambda: write_textbox(self.sessions_box, [(f"Could not load sessions: {err}", "CRITICAL")]))

        threading.Thread(target=worker, name="sessions", daemon=True).start()

    def _show_sessions(self, rows):
        lines = [(f"{'ID':<6} {'STARTED':<19}  {'ENDED':<19}  {'DURATION':<12} {'ALERTS':>6} {'OFFLINE':>7} "
                  f"{'RECOV.':>6}  VMS MONITORED", "HEAD"), "-" * 150]
        for r in rows:
            end = r["ended_at"]
            running = end is None and r["session_id"] == self.session_id
            dur = format_duration(((end or dt.datetime.now()) - r["started_at"]).total_seconds())
            lines.append((f"{r['session_id']:<6} {stamp(r['started_at']):<19}  "
                          f"{('RUNNING' if running else stamp(end)) if end or running else 'not closed':<19}  "
                          f"{dur:<12} {r['alerts']:>6} {r['offline_events']:>7} {r['recoveries']:>6}  "
                          f"{r['vms_monitored']}", "NORMAL" if running else None))
        if not rows:
            lines.append("No sessions recorded yet. Start monitoring to create one.")
        write_textbox(self.sessions_box, [x if not isinstance(x, tuple) or x[1] else x[0] for x in lines])
        ids = [str(r["session_id"]) for r in rows] or ["—"]
        current = self.session_menu.get()
        self.session_menu.configure(values=ids)
        if current not in ids:
            self.session_menu.set(str(self.session_id) if self.session_id and str(self.session_id) in ids else ids[0])

    def _selected_session_id(self):
        try:
            return int(self.session_menu.get())
        except ValueError:
            return None

    def view_session_details(self):
        sid = self._selected_session_id()
        if sid is None or not self._require_db():
            return
        write_textbox(self.session_detail_box, [f"Loading session #{sid}…"])

        def worker():
            try:
                rep = self.db.session_report(sid, event_limit=300)
                self._post(lambda: self._show_session_detail(rep))
            except Exception as exc:
                err = str(exc)
                self._post(lambda: write_textbox(self.session_detail_box, [(err, "CRITICAL")]))

        threading.Thread(target=worker, name="session-detail", daemon=True).start()

    def _show_session_detail(self, rep):
        s, c = rep["session"], rep["counts"]
        end = s["ended_at"]
        dur = format_duration(((end or dt.datetime.now()) - s["started_at"]).total_seconds())
        lines = [(f"SESSION #{s['session_id']}", "HEAD"),
                 f"Started : {stamp(s['started_at'])}     Ended : {stamp(end) if end else 'running'}     Duration : {dur}",
                 f"Central : {s['central_host']}     Polling : every {s['poll_interval_s']:g}s, offline after "
                 f"{s['failure_limit']} failures",
                 f"VMs     : {s['vms_monitored']}",
                 f"Samples : {rep['total_samples']}   Alerts (warning+critical): {c['alerts']}   Critical: {c['critical']}"
                 f"   Offline events: {c['offline_events']}   Recoveries: {c['recoveries']}", "",
                 (f"{'VM':<6} {'HOSTNAME':<20} {'IP':<15} {'SAMPLES':>7} {'AVAIL':>7} {'AVG CPU':>8} {'MAX CPU':>8} "
                  f"{'AVG RAM':>8} {'MAX RAM':>8} {'MAX DISK':>9}  LAST STATUS", "HEAD")]
        f = lambda v: f"{v:7.1f}%" if v is not None else "      —"  # noqa: E731
        for v in rep["vm_stats"]:
            avail = 100.0 * v["online_samples"] / v["samples"] if v["samples"] else 0
            lines.append(f"{v['vm_id']:<6} {(v['hostname'] or '')[:20]:<20} {v['ip_address'] or '':<15} "
                         f"{v['samples']:>7} {avail:6.1f}% {f(v['avg_cpu'])} {f(v['max_cpu'])} {f(v['avg_ram'])} "
                         f"{f(v['max_ram'])} {f(v['max_disk'])}   {v['last_status']}")
        lines += ["", ("EVENTS", "HEAD")]
        for e in rep["events"]:
            lines.append((f"{stamp(e['occurred_at'])}  {e['vm_id'] or 'SYSTEM':<6} {e['event_type']:<10} "
                          f"{SEVERITY_SHORT.get(e['severity'], e['severity']):<9} {e['message']}", e["severity"]))
        if not rep["events"]:
            lines.append("No events in this session.")
        write_textbox(self.session_detail_box, lines)

    def _pdf_selected_session(self):
        sid = self._selected_session_id()
        if sid is None:
            messagebox.showinfo("PDF report", "Refresh and select a session first.", parent=self)
            return
        self.export_pdf(sid)

    # ---------------------------------------------------------------- thresholds
    def _build_threshold_tab(self):
        page = ctk.CTkScrollableFrame(self.threshold_tab, fg_color="transparent",
                                      scrollbar_button_color=BORDER_STRONG, scrollbar_button_hover_color=FAINT)
        page.pack(fill="both", expand=True)
        panel = card(page)
        panel.pack(fill="x", padx=4, pady=(0, 8))
        section_title(panel, "Alert thresholds",
                      "GREEN below Yellow  ·  YELLOW ≥ Yellow  ·  ORANGE ≥ Orange  ·  RED ≥ Red (critical — triggers "
                      "the audible alarm). Changes apply immediately to all VMs.")
        self.th_entries = {}
        self.th_bands = {}
        for metric in config.METRICS:
            row = ctk.CTkFrame(panel, fg_color=PANEL_DEEP, corner_radius=10, border_width=1, border_color=BORDER)
            row.pack(fill="x", padx=20, pady=4)
            ctk.CTkLabel(row, text=METRIC_LABELS[metric], width=90, anchor="w", text_color=TEXT,
                         font=font(14, "bold")).grid(row=0, column=0, padx=(14, 6), pady=(10, 2))
            entries = []
            for i, (name, color) in enumerate((("Yellow", SEV_YELLOW), ("Orange", SEV_ORANGE), ("Red", SEV_RED))):
                ctk.CTkLabel(row, text=f"● {name}", text_color=color,
                             font=font(12, "bold")).grid(row=0, column=1 + i * 2, padx=(10, 4), pady=(10, 2))
                entry = ctk.CTkEntry(row, width=70, justify="center", fg_color=PANEL, border_color=BORDER_STRONG,
                                     corner_radius=8, font=font(13, "bold", NUM_FONT))
                entry.grid(row=0, column=2 + i * 2, padx=(0, 6), pady=(10, 2))
                entry.bind("<KeyRelease>", lambda _e, m=metric: self._draw_band(m))
                entries.append(entry)
            ctk.CTkLabel(row, text="%", text_color=MUTED).grid(row=0, column=7, padx=(0, 10), pady=(10, 2))
            band = tk.Canvas(row, height=16, bg=PANEL_DEEP, highlightthickness=0)
            band.grid(row=1, column=0, columnspan=9, sticky="ew", padx=14, pady=(4, 10))
            band.bind("<Configure>", lambda _e, m=metric: self._draw_band(m))
            row.grid_columnconfigure(8, weight=1)
            self.th_entries[metric] = entries
            self.th_bands[metric] = band
        buttons = ctk.CTkFrame(panel, fg_color="transparent")
        buttons.pack(fill="x", padx=20, pady=(10, 4))
        accent_button(buttons, "Save settings", self.save_thresholds, width=140, height=36).pack(side="left")
        ghost_button(buttons, "Reset to defaults", self.reset_thresholds, width=150, height=36).pack(side="left",
                                                                                                    padx=8)
        ghost_button(buttons, "▶  Test alarm", self.test_alarm, width=130, height=36).pack(side="left")
        self.th_status = ctk.CTkLabel(panel, text="Defaults: CPU 60/70/85 · RAM 60/70/85 · Disk 60/75/90",
                                      text_color=MUTED, anchor="w", font=font(12))
        self.th_status.pack(fill="x", padx=20, pady=(4, 6))
        ctk.CTkLabel(panel, text=f"Alarm sound: {self.alarm.describe()}", text_color=FAINT, anchor="w",
                     wraplength=820, justify="left", font=font(11)).pack(fill="x", padx=20, pady=(0, 16))
        self._fill_threshold_entries()
        self._build_notifications_panel(page)

        params = card(page)
        params.pack(fill="x", padx=4, pady=8)
        section_title(params, "Monitoring parameters")
        vm_lines = "\n".join(f"{vm.vm_id}   {vm.hostname:<20} {vm.base_url}" for vm in config.VMS)
        ctk.CTkLabel(params, justify="left", anchor="w", text_color=TEXT, font=font(12, family=MONO_FONT),
                     text=(f"Polling interval   {config.POLL_INTERVAL_SECONDS} s\n"
                           f"Request timeout    {config.REQUEST_TIMEOUT_SECONDS} s\n"
                           f"FAILURE_LIMIT      {config.FAILURE_LIMIT} consecutive failed checks → OFFLINE\n"
                           f"Hysteresis         {config.HYSTERESIS_PERCENT} % (prevents alert flapping)\n\n"
                           f"{vm_lines}\n\n(edit config.py to change these values)")
                     ).pack(anchor="w", padx=20, pady=(0, 14))

        retention = card(page)
        retention.pack(fill="x", padx=4, pady=(8, 4))
        section_title(retention, "Data retention",
                      "Samples and events older than this are removed during cleanup; sessions are kept.")
        rrow = ctk.CTkFrame(retention, fg_color="transparent")
        rrow.pack(fill="x", padx=20, pady=(8, 16))
        ctk.CTkLabel(rrow, text="Keep records for", text_color=TEXT,
                     font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        self.retention_entry = ctk.CTkEntry(rrow, width=80, justify="center", fg_color=PANEL_DEEP,
                                            border_color=BORDER_STRONG, corner_radius=8)
        self.retention_entry.insert(0, str(self.retention_days))
        self.retention_entry.pack(side="left", padx=(12, 6))
        ctk.CTkLabel(rrow, text="days", text_color=TEXT).pack(side="left")
        accent_button(rrow, "Save", self.save_retention_days, width=80).pack(side="left", padx=12)
        self.retention_status = ctk.CTkLabel(rrow, text="", text_color=MUTED)
        self.retention_status.pack(side="left")

    def _fill_threshold_entries(self):
        for metric, values in self.thresholds.all().items():
            for entry, v in zip(self.th_entries[metric], values):
                entry.delete(0, "end")
                entry.insert(0, f"{v:g}")
            self._draw_band(metric)

    def _draw_band(self, metric):
        band = self.th_bands[metric]
        band.delete("all")
        w = band.winfo_width()
        if w < 20:
            return
        try:
            y, o, r = validate_thresholds(metric, [e.get() for e in self.th_entries[metric]])
        except ThresholdError:
            band.create_text(4, 8, anchor="w", text="Invalid values: need 0 < Yellow < Orange < Red ≤ 100",
                             fill=SEV_ORANGE, font=("Segoe UI", 9))
            return
        x = lambda p: p / 100.0 * w  # noqa: E731
        for start, end, color in ((0, y, SEV_GREEN), (y, o, SEV_YELLOW), (o, r, SEV_ORANGE), (r, 100, SEV_RED)):
            band.create_rectangle(x(start), 2, x(end), 14, fill=color, outline="")

    def save_thresholds(self):
        try:
            new = {m: validate_thresholds(m, [e.get() for e in self.th_entries[m]]) for m in config.METRICS}
        except ThresholdError as exc:
            self.th_status.configure(text=str(exc), text_color=SEV_ORANGE)
            return
        self._apply_thresholds(new, "Thresholds saved")

    def reset_thresholds(self):
        if not messagebox.askyesno("Reset thresholds", "Restore defaults?\n\nCPU 60/70/85  •  RAM 60/70/85  •  "
                                                       "Disk 60/75/90", parent=self):
            return
        self._apply_thresholds(dict(config.DEFAULT_THRESHOLDS), "Thresholds reset to defaults")

    def _apply_thresholds(self, new, label):
        applied = self.thresholds.update(new)
        self._fill_threshold_entries()
        self._rerender_dashboard()
        if self.engine:
            self.engine.poke()           # re-evaluate alerts/alarm right now
        summary = self.thresholds.describe()
        log.info("%s: %s", label, summary)
        self._log_gui_event("INFO", "INFO", f"{label}: {summary}")
        if not self.db.ok:
            self.th_status.configure(text=f"{label} — applied immediately (not stored: PostgreSQL offline)",
                                     text_color=SEV_ORANGE)
            return
        self.th_status.configure(text=f"{label} — applied immediately, saving…", text_color=MUTED)

        def worker():
            try:
                self.db.save_thresholds(applied)
                self._post(lambda: self.th_status.configure(
                    text=f"{label} — applied immediately and stored in PostgreSQL ({dt.datetime.now():%H:%M:%S})",
                    text_color=SEV_GREEN))
            except Exception as exc:
                err = str(exc)
                self._post(lambda: self.th_status.configure(text=f"Applied, but saving failed: {err}",
                                                            text_color=SEV_RED))

        threading.Thread(target=worker, daemon=True).start()

    def test_alarm(self):
        if not self.alarm.play_test(3.0):
            self.th_status.configure(text="The alarm is already sounding.", text_color=SEV_ORANGE)
            return
        self._test_until = time.monotonic() + 3.0
        self.footer_status.configure(text="▶ ALARM TEST • playing for 3 seconds", text_color="#c07bff")
        self.after(3200, self._update_footer)
        self.th_status.configure(text="Alarm test played (3 s).", text_color=MUTED)
        self._log_gui_event("TEST", "TEST", "Alarm test triggered from Threshold Settings")

    def save_retention_days(self):
        try:
            days = int(self.retention_entry.get().strip())
            if not 1 <= days <= 3650:
                raise ValueError
        except ValueError:
            self.retention_status.configure(text="Enter a whole number from 1 to 3650.", text_color=SEV_ORANGE)
            return
        if not self._require_db():
            return

        def worker():
            try:
                self.db.set_retention_days(days)
                self.retention_days = days
                self._post(lambda: self.retention_status.configure(text="Saved", text_color=SEV_GREEN))
            except Exception as exc:
                err = str(exc)
                self._post(lambda: self.retention_status.configure(text=f"Save failed: {err[:90]}",
                                                                   text_color=SEV_RED))

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------------- Discord notifications
    def _build_notifications_panel(self, page):
        panel = card(page)
        panel.pack(fill="x", padx=4, pady=8)
        section_title(panel, "Discord notifications",
                      "Sent by this central monitor when a metric escalates to ORANGE or RED, a VM goes OFFLINE or "
                      "RECOVERS, or an alert is RESOLVED — never on every poll (the same VM/metric/level at most once "
                      f"per {config.DISCORD_COOLDOWN_SECONDS} s). The pasted webhook URL is masked and saved only to "
                      "your Windows user environment variable VIGILANT_DISCORD_WEBHOOK_URL (never to a file or "
                      "PostgreSQL).")
        row = ctk.CTkFrame(panel, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=4)
        self.discord_switch = ctk.CTkSwitch(row, text="", progress_color=ACCENT_MID, button_color=TEXT,
                                            button_hover_color=TEXT, fg_color=BORDER_STRONG,
                                            text_color=TEXT, command=self._toggle_discord, font=font(13, "bold"))
        if self.notifier.enabled:
            self.discord_switch.select()
        self.discord_switch.configure(text=f"Discord Notifications {'ON' if self.notifier.enabled else 'OFF'}")
        self.discord_switch.pack(side="left")
        ctk.CTkLabel(row, text="Webhook status", text_color=MUTED, font=font(12)).pack(side="left", padx=(30, 8))
        self.discord_status = ctk.CTkLabel(row, text="", height=26, width=140, corner_radius=13,
                                           font=font(11, "bold"))
        self.discord_status.pack(side="left")

        urow = ctk.CTkFrame(panel, fg_color="transparent")
        urow.pack(fill="x", padx=20, pady=(8, 2))
        ctk.CTkLabel(urow, text="Webhook URL", text_color=TEXT, font=font(13, "bold")).pack(side="left",
                                                                                          padx=(0, 10))
        self.discord_url_entry = ctk.CTkEntry(urow, width=470, show="•", fg_color=PANEL_DEEP, height=32,
                                              border_color=BORDER_STRONG, corner_radius=8,
                                              placeholder_text="Paste the Discord webhook URL (Ctrl+V or right-click)")
        self.discord_url_entry.pack(side="left")
        self.discord_url_entry.bind("<Button-3>", self._paste_discord_url)
        self.discord_url_entry.bind("<Return>", lambda _e: self.save_discord_url())
        accent_button(urow, "Save URL", self.save_discord_url, width=100).pack(side="left", padx=(10, 6))
        ghost_button(urow, "Clear", self.clear_discord_url, width=70).pack(side="left")

        flags_row = ctk.CTkFrame(panel, fg_color=PANEL_DEEP, corner_radius=10, border_width=1, border_color=BORDER)
        flags_row.pack(fill="x", padx=20, pady=6)
        flags = self.notifier.flags()
        self.discord_flag_vars = {}
        for kind in KINDS:
            var = tk.BooleanVar(value=flags.get(kind, True))
            ctk.CTkCheckBox(flags_row, text=KIND_LABELS[kind], variable=var, text_color=TEXT, fg_color=ACCENT_MID,
                            hover_color=ACCENT_HOVER, border_color=BORDER_STRONG, corner_radius=6,
                            checkbox_width=20, checkbox_height=20, font=font(12),
                            command=lambda k=kind, v=var: self.notifier.set_flag(k, v.get())
                            ).pack(side="left", padx=(14, 6), pady=10)
            self.discord_flag_vars[kind] = var

        brow = ctk.CTkFrame(panel, fg_color="transparent")
        brow.pack(fill="x", padx=20, pady=(6, 2))
        self.discord_test_btn = ghost_button(brow, "▶  Test Discord Notification", self.test_discord,
                                             width=230, height=36)
        self.discord_test_btn.pack(side="left")
        self.discord_shot_var = tk.BooleanVar(value=self.notifier.attach_screenshot)
        ctk.CTkCheckBox(brow, text="Attach dashboard screenshot", variable=self.discord_shot_var, text_color=TEXT,
                        fg_color=ACCENT_MID, hover_color=ACCENT_HOVER, border_color=BORDER_STRONG, corner_radius=6,
                        checkbox_width=20, checkbox_height=20, font=font(12),
                        command=lambda: setattr(self.notifier, "attach_screenshot", self.discord_shot_var.get())
                        ).pack(side="left", padx=(18, 0))
        self.discord_detail = ctk.CTkLabel(panel, text="", text_color=MUTED, anchor="w", wraplength=820,
                                           justify="left", font=font(12))
        self.discord_detail.pack(fill="x", padx=20, pady=(4, 16))
        self._refresh_discord_status()

    def _refresh_discord_status(self, note=None):
        state, detail, last = self.notifier.status()
        color = {CONNECTED: SEV_GREEN, READY: TEAL, ERROR: SEV_RED}.get(state, GREY)
        self.discord_status.configure(text=f"● {state}", text_color=color, fg_color=blend(color, PANEL, .16))
        lines = [note] if note else []
        if state == NOT_CONFIGURED:
            lines.append("Discord notifications disabled — paste your webhook URL above and press Save URL. "
                         "Monitoring, alarm and PostgreSQL work normally without it.")
        elif state == ERROR and detail:
            lines.append(f"Last error: {detail}")
        elif state == READY and not last:
            lines.append("Webhook configured — nothing sent yet in this run.")
        if last:
            lines.append(f"Last notification: {last}")
        self.discord_detail.configure(text="\n".join(lines), text_color=SEV_ORANGE if state == ERROR else MUTED)

    def _paste_discord_url(self, _event=None):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return "break"
        self.discord_url_entry.delete(0, "end")
        self.discord_url_entry.insert(0, text.strip())
        return "break"

    def save_discord_url(self):
        url = self.discord_url_entry.get().strip()
        if not url:
            self._refresh_discord_status(note="Paste the webhook URL into the field first.")
            return
        if not is_valid_webhook_url(url):
            self._refresh_discord_status(note="That is not a Discord webhook URL — it must start with "
                                              "https://discord.com/api/webhooks/ . Nothing was saved.")
            return
        self._store_discord_url(url, "Webhook URL saved")

    def clear_discord_url(self):
        if not self.notifier.channel.present:
            self.discord_url_entry.delete(0, "end")
            return
        if not messagebox.askyesno("Remove Discord webhook",
                                   "Remove the saved Discord webhook URL?\n\nDiscord notifications stop until "
                                   "you paste a URL again.", parent=self):
            return
        self._store_discord_url("", "Webhook URL removed")

    def _store_discord_url(self, url, label):
        self.discord_url_entry.delete(0, "end")          # never keep the secret in the widget
        self.focus_set()
        try:
            where = store_webhook_url(url)
        except OSError as exc:
            where = f"for this run only (could not save permanently: {type(exc).__name__})"
        self.notifier.reconfigure(url)
        log.info("Discord webhook URL %s from the GUI (%s)", "set" if url else "removed", where)
        self._log_gui_event("INFO", "INFO", f"Discord webhook URL {'set' if url else 'removed'} ({where})")
        self._refresh_discord_status(note=f"{label} — {where}. "
                                          + ("Press Test Discord Notification to check it." if url else ""))

    def _toggle_discord(self):
        on = bool(self.discord_switch.get())
        self.notifier.set_enabled(on)
        self.discord_switch.configure(text=f"Discord Notifications {'ON' if on else 'OFF'}")
        self._log_gui_event("INFO", "INFO", f"Discord notifications switched {'ON' if on else 'OFF'}")

    def test_discord(self):
        if not self.notifier.channel.configured():
            self._refresh_discord_status(note=f"Cannot send a test: {self.notifier.channel.problem()}.")
            return
        if not self.notifier.send_test():
            self._refresh_discord_status(note="Test not queued (notification queue busy) — try again.")
            return
        self.discord_test_btn.configure(state="disabled")
        self.after(3000, lambda: self.discord_test_btn.configure(state="normal"))
        self._refresh_discord_status(note="Sending test message to Discord…")

    def _dashboard_screenshot(self, timeout=3.0):
        """Called on the notification worker thread; the capture itself runs on the Tk thread.
        A burst of alerts (e.g. 3 VMs at once) reuses one capture."""
        cached = self._shot_cache
        if cached and time.monotonic() - cached[0] < 4.0:
            return cached[1]
        if self._closing:
            return None
        done, box = threading.Event(), {}
        self._post(lambda: self.after(500, lambda: self._capture_dashboard(box, done)))   # let the alert render
        return box.get("jpeg") if done.wait(timeout) else None

    def _capture_dashboard(self, box, done):
        """Tk thread. If another page is open, show the Dashboard, give Tk a real repaint pass
        (~300 ms, a partial repaint would mix both pages), capture, then switch back."""
        if self._closing:
            done.set()
            return
        page = self.tabs.get()

        def capture():
            try:
                img = capture_window(int(self.wm_frame(), 16))
                if img is not None:
                    box["jpeg"] = to_jpeg(img)
                    self._shot_cache = (time.monotonic(), box["jpeg"])
            finally:
                if page != "Overview" and not self._closing:
                    self.tabs.set(page)
                    self._highlight_nav(page)
                done.set()

        if page == "Overview":
            capture()
        else:
            self.tabs.set("Overview")
            self._highlight_nav("Overview")
            self.after(300, capture)

    def _on_notify(self, kind, payload):
        """Notification worker results, handled on the Tk thread."""
        if kind == "record" and self.session_id:
            self.db.enqueue("event", dict(payload, session_id=self.session_id))   # History & Alerts
        self._refresh_discord_status()

    # =============================================================== queue / events
    def _post(self, fn):
        """Run fn on the Tk main thread (safe to call from any thread)."""
        self.ui_queue.put(("call", fn))

    def _drain_queue(self):
        if self._closing:
            return
        for _ in range(300):
            try:
                msg = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(msg)
            except Exception:
                log.exception("UI message failed: %r", msg[0])
        self.after(150, self._drain_queue)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "snapshot":
            if msg[1] is self.engine:
                self._on_snapshot(msg[2])
        elif kind == "event":
            self.recent_events.appendleft(msg[2])
            self._render_recent_events()
        elif kind == "call":
            msg[1]()
        elif kind == "notify":
            self._on_notify(msg[1], msg[2])
        elif kind == "db_error":
            self._db_write_error = msg[1]
            self.db_status_label.configure(text="DATABASE WRITE ERROR", text_color=SEV_ORANGE)
            self._update_footer()
        elif kind == "db_ok":
            self._db_write_error = None
            self.db_status_label.configure(text="CONNECTED • POSTGRESQL", text_color=SEV_GREEN)
            self._update_footer()

    def _on_snapshot(self, snap):
        self.latest = snap
        self._rerender_dashboard()
        self._update_alarm(snap["alarms"])

    def _rerender_dashboard(self):
        snap = self.latest
        if not snap:
            return
        th = self.thresholds.all()
        paused = self.engine is None
        for vm_id, panel in self.vm_panels.items():
            panel.render(snap["vms"][vm_id], th, paused=paused)
        vms = snap["vms"].values()
        online = sum(1 for v in vms if v["status"] in (ONLINE, RECOVERED))
        offline = [v["vm_id"] for v in vms if v["status"] == OFFLINE]
        total = len(snap["vms"])
        self.card_online.set(f"{online} / {total}",
                             ("Offline: " + ", ".join(offline)) if offline else
                             ("All VMs responding" if online == total else "Waiting / retrying"),
                             SEV_GREEN if online == total else (SEV_RED if offline else SEV_ORANGE))
        n = len(snap["alarms"])
        detail = ", ".join(a["text"].replace(" CRITICAL", "") for a in snap["alarms"])
        self.card_alarms.set(str(n), (detail[:28] + "…" if len(detail) > 29 else detail) or "No critical conditions",
                             SEV_RED if n else SEV_GREEN)
        cpus = [(v["values"]["CPU"], v["vm_id"]) for v in vms if v["values"]["CPU"] is not None]
        if cpus:
            value, vm_id = max(cpus)
            self.card_peak.set(f"{value:.1f}%", f"on {vm_id}", LEVEL_COLORS[classify(value, th["CPU"])])
        else:
            self.card_peak.set("—", "No live data", GREY)
        self.card_poll.set(f"{snap['time']:%H:%M:%S}",
                           ("PAUSED" if paused else f"every {config.POLL_INTERVAL_SECONDS}s • cycle {snap['cycle']}"),
                           TEXT if not paused else GREY)

    def _log_gui_event(self, event_type, severity, message):
        ev = {"time": dt.datetime.now(), "session_id": self.session_id, "vm_id": None, "hostname": None,
              "ip": None, "event_type": event_type, "metric": None, "value": None, "threshold": None,
              "severity": severity, "status": None, "message": message}
        self.recent_events.appendleft(ev)
        self._render_recent_events()
        if self.session_id:
            self.db.enqueue("event", ev)

    # =============================================================== alarm
    def _update_alarm(self, items):
        self.alarm_items = items
        keys = {i["key"] for i in items}
        self.alarm_muted &= keys                         # a resolved condition is forgotten
        unmuted = [i for i in items if i["key"] not in self.alarm_muted]
        should_sound = self.alarm_enabled and bool(unmuted)
        if should_sound:
            self.alarm.start_loop()                      # no-op if already sounding
        elif self.alarm.playing:
            self.alarm.stop()                            # condition resolved / muted / disabled

        if not self.alarm_enabled:
            text, color = (f"ALARM OFF • {len(items)} ACTIVE" if items else "ALARM OFF"), GREY
        elif not items:
            text, color = "ALARM ARMED • ALL CLEAR", SEV_GREEN
        elif should_sound:
            text, color = f"⚠ ALARM SOUNDING ({len(unmuted)})", SEV_RED
        else:
            text, color = f"ALARM MUTED ({len(items)} ACTIVE)", SEV_ORANGE
        self.alarm_status.configure(text=text, text_color=color)
        self.mute_btn.configure(state="normal" if should_sound else "disabled")
        alarm_vms = {i["vm_id"] for i in items}
        for vm_id, panel in self.vm_panels.items():
            panel.alarm = vm_id in alarm_vms
            if not panel.alarm:
                panel.apply_blink(False)
        self._update_footer()

    def mute_alarm(self):
        self.alarm_muted = {i["key"] for i in self.alarm_items}
        log.info("Alarm muted for: %s", ", ".join(sorted(self.alarm_muted)))
        self._update_alarm(self.alarm_items)

    def _toggle_alarm_enabled(self):
        self.alarm_enabled = bool(self.alarm_switch.get())
        self.alarm_switch.configure(text="Audible alarm ON" if self.alarm_enabled else "Audible alarm OFF")
        self._update_alarm(self.alarm_items)

    def _blink_tick(self):
        if self._closing:
            return
        self._blink = not self._blink
        for panel in self.vm_panels.values():
            if panel.alarm:
                panel.apply_blink(self._blink)
        self.after(600, self._blink_tick)

    def _clock_tick(self):
        if self._closing:
            return
        self.clock_label.configure(text=dt.datetime.now().strftime("%a %d %b %Y   %H:%M:%S"))
        self.after(1000, self._clock_tick)

    def _update_footer(self):
        if time.monotonic() < self._test_until:
            return
        if self.alarm_items:
            text = "⚠ ALARM ACTIVE • " + ", ".join(i["text"] for i in self.alarm_items)
            color = SEV_RED
        elif self.engine:
            online = 0
            if self.latest:
                online = sum(1 for v in self.latest["vms"].values() if v["status"] in (ONLINE, RECOVERED))
            text = (f"● LIVE TELEMETRY • {online}/{len(config.VMS)} VMs online • polling every "
                    f"{config.POLL_INTERVAL_SECONDS}s • offline after {config.FAILURE_LIMIT} failed checks")
            if self.session_id:
                text += f" • session #{self.session_id}"
                color = SEV_GREEN
            else:
                text += " • NOT RECORDING (PostgreSQL offline)"
                color = SEV_ORANGE
            if self._db_write_error:
                text += f" • DB WRITE ERROR: {self._db_write_error[:80]}"
                color = SEV_ORANGE
        elif self._stopping or self._starting:
            text, color = "Please wait…", MUTED
        else:
            text, color = "○ MONITORING STOPPED • press Start Monitoring in the sidebar", MUTED
        self.footer_status.configure(text=text[:210], text_color=color)

    # =============================================================== monitoring control
    def toggle_monitoring(self):
        if self.engine:
            self.stop_monitoring()
        else:
            self.start_monitoring()

    def start_monitoring(self):
        if self.engine or self._starting or self._stopping or self._closing:
            return
        self._starting = True
        self.monitor_btn.configure(state="disabled", text="Starting…")

        def worker():
            sid, err = None, None
            if self.db.ok:
                try:
                    sid = self.db.start_session(config.VMS, self.thresholds.all())
                except Exception as exc:
                    err = str(exc)
            self._post(lambda: self._launch_engine(sid, err))

        threading.Thread(target=worker, name="start-session", daemon=True).start()

    def _launch_engine(self, sid, err):
        self._starting = False
        if self._closing:
            return
        self.session_id = sid
        self.engine = MonitorEngine(config.VMS, self.thresholds, self.ui_queue, self.db if sid else None, sid,
                                    notifier=self.notifier)
        self.engine.start()
        log.info("Monitoring started (session %s)", sid or "live-only")
        self._update_monitor_controls()
        if err:
            messagebox.showwarning("Session not recorded",
                                   f"Monitoring is running, but the session could not be stored:\n\n{err}", parent=self)

    def stop_monitoring(self):
        engine = self.engine
        if not engine or self._stopping:
            return
        self._stopping = True
        sid = self.session_id
        self.engine = None
        self.session_id = None
        self.monitor_btn.configure(state="disabled", text="Stopping…")
        self._update_alarm([])
        self._rerender_dashboard()

        def worker():
            engine.stop(timeout=10)
            if sid:
                self.db.enqueue("end_session", sid)
            self._post(self._after_stop)

        threading.Thread(target=worker, name="stop-monitoring", daemon=True).start()

    def _after_stop(self):
        self._stopping = False
        log.info("Monitoring stopped")
        self._update_monitor_controls()

    def _update_monitor_controls(self):
        if self.engine:
            self.monitor_btn.configure(state="normal", text="■  Stop Monitoring", fg_color=PANEL_ALT,
                                       hover_color=PANEL_HOVER, border_width=1, border_color=BORDER_STRONG)
            if self.session_id:
                self.monitor_status.configure(text=f"● MONITORING • SESSION #{self.session_id}", text_color=SEV_GREEN)
            else:
                self.monitor_status.configure(text="● MONITORING • LIVE ONLY", text_color=SEV_ORANGE)
        else:
            self.monitor_btn.configure(state="normal", text="▶  Start Monitoring", fg_color=ACCENT_MID,
                                       hover_color=ACCENT_HOVER, border_width=0)
            self.monitor_status.configure(text="○ STOPPED", text_color=MUTED)
        self.session_info.configure(text=f"Current session: {('#' + str(self.session_id)) if self.session_id else '—'}")
        if self.engine:
            color = SEV_GREEN if self.session_id else SEV_ORANGE
            self.live_chip.configure(text="●  LIVE" if self.session_id else "●  LIVE ONLY", text_color=color,
                                     fg_color=blend(color, BG, .14))
        else:
            self.live_chip.configure(text="○  STOPPED", text_color=MUTED, fg_color=PANEL_ALT)
        self._update_footer()

    # =============================================================== database
    def connect_database(self, start_monitoring=False, interactive=True):
        if self._db_connecting:
            return
        self._db_connecting = True
        self.db_retry_btn.pack_forget()
        self.db_status_label.configure(text="CONNECTING • POSTGRESQL", text_color=MUTED)

        def worker():
            try:
                self.db.connect()
                self.db.upsert_vms(config.VMS)
                if self.engine is None and not self._starting:
                    closed = self.db.close_orphan_sessions()
                    if closed:
                        log.info("Closed %d unfinished session(s) from a previous run", closed)
                stored = self.db.load_thresholds()
                if not stored:
                    self.db.save_thresholds(self.thresholds.all())
                retention = self.db.get_retention_days(config.RETENTION_DAYS)
                self._post(lambda: self._on_db_ready(stored, retention, start_monitoring))
            except Exception as exc:
                err = str(exc)
                log.error("Database unavailable: %s", err)
                self._post(lambda: self._on_db_failed(err, start_monitoring, interactive))

        threading.Thread(target=worker, name="db-connect", daemon=True).start()

    def _on_db_ready(self, stored, retention, start_monitoring):
        self._db_connecting = False
        if stored:
            try:
                self.thresholds.update(stored)
            except ThresholdError as exc:
                log.warning("Stored thresholds ignored: %s", exc)
        self._fill_threshold_entries()
        self.retention_days = retention
        self.retention_entry.delete(0, "end")
        self.retention_entry.insert(0, str(retention))
        self.db_status_label.configure(text="CONNECTED • POSTGRESQL", text_color=SEV_GREEN)
        if self.engine and not self.session_id:
            def worker():   # monitoring was running live-only: start recording now
                try:
                    sid = self.db.start_session(config.VMS, self.thresholds.all())
                    self._post(lambda: self._attach_session(sid))
                except Exception as exc:
                    log.error("Could not start session: %s", exc)
            threading.Thread(target=worker, daemon=True).start()
        elif start_monitoring and not self.engine:
            self.start_monitoring()
        self._update_footer()

    def _attach_session(self, sid):
        if self.engine:
            self.session_id = sid
            self.engine.attach_database(self.db, sid)
            self._update_monitor_controls()

    def _on_db_failed(self, err, start_monitoring, interactive):
        self._db_connecting = False
        self.db_status_label.configure(text="DATABASE OFFLINE", text_color=SEV_RED)
        self.db_retry_btn.pack(fill="x", pady=(0, 6), before=self.db_status_label)
        if start_monitoring and not self.engine:
            self.start_monitoring()           # live monitoring still works without recording
        self._update_footer()
        if interactive:
            messagebox.showerror(
                "PostgreSQL unavailable",
                f"{err}\n\nLive VM monitoring will continue, but samples, alerts, sessions, history, "
                "CSV and PDF export need the database.\n\nCheck that the PostgreSQL service is running and that "
                "EMSYS_DB_PASSWORD (and EMSYS_DB_HOST / EMSYS_DB_USER if needed) are set, then press "
                "'Retry DB connection' in the sidebar.", parent=self)

    def _require_db(self, silent=False):
        if self.db.ok:
            return True
        if not silent:
            messagebox.showerror("PostgreSQL unavailable",
                                 f"{self.db.error or 'PostgreSQL is not connected.'}\n\n"
                                 "Use 'Retry DB connection' in the sidebar.", parent=self)
        return False

    def _db_unavailable_lines(self):
        return [("PostgreSQL is not connected — history is unavailable.", "CRITICAL"), "",
                self.db.error or "", "", "Press 'Retry DB connection' in the sidebar after fixing it."]

    def manual_cleanup(self):
        if not self._require_db():
            return
        days = self.retention_days
        if not messagebox.askyesno("Confirm cleanup", f"Delete samples and events older than {days} days?\n"
                                                      "Session records are kept.", parent=self):
            return

        def worker():
            try:
                samples, events = self.db.cleanup(days)
                self._post(lambda: messagebox.showinfo("Cleanup complete",
                                                       f"Removed {samples} samples and {events} events.", parent=self))
                self._post(self.refresh_history)
            except Exception as exc:
                err = str(exc)
                self._post(lambda: messagebox.showerror("Cleanup failed", err, parent=self))

        threading.Thread(target=worker, daemon=True).start()

    def periodic_cleanup(self):
        if self._closing:
            return
        if self.db.ok:
            days = self.retention_days

            def worker():
                try:
                    removed = self.db.cleanup(days)
                    log.info("Retention cleanup removed %d samples and %d events", *removed)
                except Exception as exc:
                    log.warning("Automatic cleanup failed: %s", exc)
            threading.Thread(target=worker, daemon=True).start()
        self.after(24 * 3600 * 1000, self.periodic_cleanup)

    # =============================================================== exports
    def export_csv(self):
        if not self._require_db():
            return
        dlg = ctk.CTkToplevel(self)
        dlg.title("Export CSV")
        dlg.geometry("400x250")
        dlg.configure(fg_color=BG)
        dlg.transient(self)
        dlg.resizable(False, False)
        ctk.CTkLabel(dlg, text="EXPORT CSV", text_color=ACCENT,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", padx=20, pady=(16, 8))
        form = ctk.CTkFrame(dlg, fg_color=PANEL, corner_radius=12)
        form.pack(fill="x", padx=20)
        ctk.CTkLabel(form, text="Data", text_color=MUTED).grid(row=0, column=0, padx=14, pady=(14, 6), sticky="w")
        kind = option_menu(form, ["Resource samples (CPU/RAM/Disk)", "Alerts & events"], width=230)
        kind.grid(row=0, column=1, padx=10, pady=(14, 6))
        ctk.CTkLabel(form, text="Range", text_color=MUTED).grid(row=1, column=0, padx=14, pady=(6, 14), sticky="w")
        ranges = (["Current session"] if self.session_id else []) + ["Last 24 hours", "All records"]
        scope = option_menu(form, ranges, width=230)
        scope.grid(row=1, column=1, padx=10, pady=(6, 14))

        def go():
            k = "samples" if kind.get().startswith("Resource") else "events"
            sc = {"Current session": "session", "Last 24 hours": "24h"}.get(scope.get(), "all")
            sid = self.session_id
            dlg.destroy()
            default = f"vigilant_vm_{k}_{sc}_{dt.datetime.now():%Y%m%d_%H%M}.csv"
            path = filedialog.asksaveasfilename(parent=self, title="Export CSV", initialfile=default,
                                                defaultextension=".csv", filetypes=[("CSV files", "*.csv")])
            if not path:
                return

            def worker():
                try:
                    n = reports.export_csv(self.db, path, k, sc, sid)
                    self._post(lambda: messagebox.showinfo("CSV exported", f"Saved {n} rows to:\n{path}", parent=self))
                except Exception as exc:
                    err = str(exc)
                    self._post(lambda: messagebox.showerror("Export failed", err, parent=self))

            threading.Thread(target=worker, name="csv-export", daemon=True).start()

        accent_button(dlg, "Export…", go, width=120).pack(pady=14)
        dlg.after(150, lambda: (dlg.lift(), dlg.focus_force()))

    def export_pdf(self, session_id=None):
        if not self._require_db():
            return
        try:
            reports.ensure_reportlab()
        except ImportError:
            messagebox.showwarning("PDF library required", "Install it first:\n\npip install reportlab", parent=self)
            return
        sid = session_id or self.session_id
        default = f"VIGILANT_VM_Report_session{sid or 'latest'}_{dt.datetime.now():%Y%m%d_%H%M}.pdf"
        path = filedialog.asksaveasfilename(parent=self, title="Save VIGILANT PDF report", initialfile=default,
                                            defaultextension=".pdf", filetypes=[("PDF files", "*.pdf")])
        if not path:
            return
        thresholds = self.thresholds.all()
        self.footer_status.configure(text="Generating PDF report…", text_color=MUTED)

        def worker():
            try:
                target = sid or self.db.latest_session_id()
                if target is None:
                    raise DatabaseError("No monitoring sessions have been recorded yet.")
                rep = self.db.session_report(target)
                reports.build_session_pdf(path, rep, thresholds, self.db.cfg["dbname"], config.LOGO_FILE)
                self._post(lambda: messagebox.showinfo("PDF report created",
                                                       f"Session #{target} report saved:\n{path}", parent=self))
            except Exception as exc:
                err = str(exc)
                log.exception("PDF report failed")
                self._post(lambda: messagebox.showerror("PDF report failed", err, parent=self))
            self._post(self._update_footer)

        threading.Thread(target=worker, name="pdf-export", daemon=True).start()

    # =============================================================== navigation / exit
    def navigate(self, tab_name):
        self.tabs.set(tab_name)
        self._highlight_nav(tab_name)
        if tab_name == "History & Alerts":
            self.refresh_history()
        elif tab_name == "Sessions":
            self.refresh_sessions()
        elif tab_name == "Processes":
            self.refresh_processes(force=True)

    def _highlight_nav(self, tab_name):
        for name, item in self.nav_buttons.items():
            item.set_active(name == tab_name)
        for label, page, _icon, sub in NAV_PAGES:
            if page == tab_name:
                self.page_title.configure(text=label)
                self.page_sub.configure(text=sub)

    def close_app(self):
        if self._closing:
            return
        self._closing = True
        self.withdraw()
        try:
            self.alarm.stop()
        except Exception:
            pass
        engine, sid = self.engine, self.session_id
        self.engine = None
        if engine:
            engine.stop(timeout=4)
        if sid:
            self.db.enqueue("end_session", sid)
        self.notifier.shutdown(timeout=2)
        self.db.shutdown(timeout=4)
        log.info("VIGILANT central monitor closed")
        self.destroy()


def run_central() -> int:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    log.info("Starting %s — monitoring %s", config.APP_NAME, ", ".join(f"{v.vm_id}={v.base_url}" for v in config.VMS))
    app = CentralApp()
    app.mainloop()
    return 0
