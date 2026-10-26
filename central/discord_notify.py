"""
Discord webhook delivery channel for the VIGILANT notification layer.

Everything Discord-specific lives here: embed layout, colours, the HTTP call,
rate-limit handling. central/notifications.py decides WHAT to send and calls
DiscordChannel.send() on its background worker thread, never on the polling
thread or the Tk main thread.

SECURITY: the webhook URL is a secret. It is read from the environment
(config.DISCORD_WEBHOOK_URL <- VIGILANT_DISCORD_WEBHOOK_URL), kept in a private
attribute and never logged, displayed, stored or put into error messages.
"""
from __future__ import annotations

import functools
import io
import json
import logging
import os
import re
import socket
import sys
import time

import requests

import config
from central.monitoring import format_duration
from central.severity import classify
from central.notifications import (CRITICAL, OFFLINE, RECOVERED, RESOLVED, TEST, WARNING, Channel, Notification,
                                   NotificationError)

WEBHOOK_PATTERN = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[\w-]+/?$")
_SECRET_PATTERN = re.compile(r"/api(?:/v\d+)?/webhooks/[^\s'\"?]+")

# Semantic colour -> Discord embed colour (same palette as the VIGILANT GUI)
EMBED_COLORS = {
    "GREEN": 0x2ECC71,      # RECOVERED / RESOLVED
    "YELLOW": 0xF4C430,
    "ORANGE": 0xFF8C1A,     # WARNING
    "RED": 0xDF2531,        # CRITICAL
    "OFFLINE": 0x8B0A14,    # dark red
    "TEST": 0xA66BFF,
}
TITLES = {
    CRITICAL: "🚨 VIGILANT ALERT",
    WARNING: "⚠️ VIGILANT WARNING",
    OFFLINE: "🔴 VIGILANT VM OFFLINE",
    RECOVERED: "🟢 VIGILANT VM RECOVERED",
    RESOLVED: "✅ VIGILANT ALERT RESOLVED",
    TEST: "🧪 VIGILANT TEST",
}
SEVERITY_TEXT = {"NORMAL": "🟢 NORMAL", "ELEVATED": "🟡 ELEVATED", "WARNING": "🟠 WARNING",
                 "CRITICAL": "🔴 CRITICAL", "OFFLINE": "⛔ OFFLINE", "RECOVERED": "🟢 RECOVERED"}
LEVEL_WORD = {"WARNING": "orange", "CRITICAL": "red", "NORMAL": "orange"}
METRIC_TEXT = {"CPU": "CPU", "RAM": "Memory", "DISK": "Disk"}
METRIC_ICON = {"CPU": "🧠", "RAM": "🧮", "DISK": "💾"}
# Thumbnail badge per kind: (semantic colour, Segoe Fluent / MDL2 glyph)
BADGES = {CRITICAL: ("RED", "\uE7BA"), WARNING: ("ORANGE", "\uE7BA"), OFFLINE: ("OFFLINE", "\uE7E8"),
          RECOVERED: ("GREEN", "\uE73E"), RESOLVED: ("GREEN", "\uE930"), TEST: ("TEST", "\uE945")}
LOGO_FILE, BADGE_FILE, SHOT_FILE = "vigilant.png", "status.png", "dashboard.jpg"


def scrub(text: str) -> str:
    """Remove anything that looks like a webhook id/token from a message."""
    return _SECRET_PATTERN.sub("/api/webhooks/***", str(text or ""))


class _ScrubFilter(logging.Filter):
    """urllib3 logs request paths at DEBUG level; make sure a webhook token can never reach a log file."""

    def filter(self, record):
        message = record.getMessage()
        if "/webhooks/" in message:
            record.msg, record.args = scrub(message), None
        return True


for _name in ("urllib3.connectionpool", "urllib3.util.retry"):
    logging.getLogger(_name).addFilter(_ScrubFilter())


# ----------------------------------------------------------------- generated images (logo + status badge)
def _font(names, size):
    from PIL import ImageFont
    for name in names:
        try:
            return ImageFont.truetype(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name), size)
        except OSError:
            continue
    return None


@functools.lru_cache(maxsize=None)
def _logo_png() -> bytes:
    """128 px VIGILANT mark: dark rounded tile, red ring, white V."""
    from PIL import Image, ImageDraw
    s = 4                                                   # supersample for smooth edges
    img = Image.new("RGBA", (128 * s, 128 * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 128 * s - 1, 128 * s - 1), radius=30 * s, fill=(19, 19, 22, 255))
    d.ellipse((14 * s, 14 * s, 114 * s, 114 * s), outline=(229, 50, 62, 255), width=7 * s)
    f = _font(("bahnschrift.ttf", "segoeuib.ttf", "arialbd.ttf"), 60 * s)
    if f is not None:
        d.text((64 * s, 66 * s), "V", font=f, fill=(244, 244, 245, 255), anchor="mm")
    return _png(img.resize((128, 128), Image.LANCZOS))


@functools.lru_cache(maxsize=None)
def _badge_png(kind: str) -> bytes:
    """128 px status badge: soft glow, coloured disc, white Fluent icon."""
    from PIL import Image, ImageDraw, ImageFilter
    color_name, glyph = BADGES.get(kind, BADGES[TEST])
    rgb = tuple((EMBED_COLORS[color_name] >> sh) & 0xFF for sh in (16, 8, 0))
    s = 4
    img = Image.new("RGBA", (128 * s, 128 * s), (0, 0, 0, 0))
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((6 * s, 6 * s, 122 * s, 122 * s), fill=rgb + (110,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(7 * s)))
    d = ImageDraw.Draw(img)
    d.ellipse((16 * s, 16 * s, 112 * s, 112 * s), fill=rgb + (255,))
    f = _font(("SegoeIcons.ttf", "segmdl2.ttf"), 46 * s)
    if f is not None:
        d.text((64 * s, 64 * s), glyph, font=f, fill=(255, 255, 255, 255), anchor="mm")
    else:
        d.text((64 * s, 64 * s), "!", font=_font(("segoeuib.ttf", "arialbd.ttf"), 56 * s),
               fill=(255, 255, 255, 255), anchor="mm")
    return _png(img.resize((128, 128), Image.LANCZOS))


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def build_images(n: Notification) -> dict:
    """{filename: png bytes} referenced by the embed (attachment://...). Empty if Pillow is unavailable."""
    try:
        files = {LOGO_FILE: _logo_png(), BADGE_FILE: _badge_png(n.kind)}
    except Exception as exc:                         # Pillow missing / font problem: send without icons
        logging.getLogger("vigilant.notify").info("Discord images disabled: %s", type(exc).__name__)
        files = {}
    if n.screenshot:
        files[SHOT_FILE] = n.screenshot
    return files


# ----------------------------------------------------------------- embed layout
def _pct(value) -> str:
    return f"{value:.1f}%" if value is not None else "—"


def _field(name, value, inline=True) -> dict:
    return {"name": name, "value": str(value if value not in (None, "") else "—")[:1024], "inline": inline}


def _vm_field(n: Notification) -> dict:
    """VM id, hostname and IP on one full-width line (mobile shows every field stacked, so fewer is better)."""
    return _field("🖥️ Virtual machine", f"**{n.vm_id}** · `{n.hostname}` · `{n.ip}`", inline=False)


def _when(n: Notification, label="🕒 Time", inline=False) -> dict:
    ts = int(n.time.timestamp())                     # Discord shows it in each reader's own time zone
    return _field(label, f"<t:{ts}:F> · <t:{ts}:R>", inline=inline)


ZONE_SQUARES = ("🟩", "🟨", "🟧", "🟥")
EMPTY_SQUARE = "⬛"


def gauge(value, thresholds=None, color=None, cells=10) -> str:
    """Emoji bar, 10 % per square. Each filled square takes the colour of the threshold zone it lies in,
    so the bar itself shows where yellow / orange / red start; a legend line follows. Emoji all have the same
    width, so unlike a code block this never wraps or misaligns on phones."""
    value = max(0.0, min(float(value or 0), 100.0))
    filled = min(cells, int(value / (100 / cells) + 0.5))
    squares = []
    for i in range(cells):
        if i >= filled:
            squares.append(EMPTY_SQUARE)
        elif thresholds:
            squares.append(ZONE_SQUARES[classify((i + 0.5) * 100 / cells, thresholds)])
        else:
            squares.append({"GREEN": "🟩", "YELLOW": "🟨", "ORANGE": "🟧"}.get(color, "🟥"))
    bar = "".join(squares) + f"  **{value:.1f}%**"
    if thresholds:
        y, o, r = thresholds
        bar += f"\n-# 🟨 yellow {y:g}%  ·  🟧 orange {o:g}%  ·  🟥 red {r:g}%"
    return bar


def build_payload(n: Notification, preview: bool = False, images: bool = True, screenshot: bool = False) -> dict:
    """Discord webhook JSON for one notification."""
    metric = METRIC_TEXT.get(n.metric, n.metric)
    icon = METRIC_ICON.get(n.metric, "📊")
    if n.kind in (CRITICAL, WARNING):
        verb = "is CRITICAL" if n.kind == CRITICAL else "is high"
        description = (f"### {icon} {n.vm_id} · {metric} {verb}\n"
                       f"{metric} reached **{_pct(n.value)}** — above the {LEVEL_WORD[n.severity]} threshold "
                       f"of **{n.threshold:g}%**.\n\n{gauge(n.value, n.thresholds, n.color)}")
        fields = [_vm_field(n),
                  _field(f"📈 {metric} usage", f"**{_pct(n.value)}**"),
                  _field(f"🎯 {LEVEL_WORD[n.severity].capitalize()} threshold", f"{n.threshold:g}%"),
                  _field("⚡ Severity · Status", SEVERITY_TEXT.get(n.severity, n.severity)),
                  _when(n, "🕒 Detected")]
    elif n.kind == RESOLVED:
        description = (f"### {icon} {n.vm_id} · {metric} back to normal\n"
                       f"{metric} dropped to **{_pct(n.value)}**, below the orange threshold of "
                       f"**{n.threshold:g}%**.\n\n{gauge(n.value, n.thresholds, 'GREEN')}")
        fields = [_vm_field(n),
                  _field(f"📉 {metric} now", f"**{_pct(n.value)}**"),
                  _field("⏮️ Was", SEVERITY_TEXT.get(n.previous_severity, "—")),
                  _field("📌 Status", "✅ RESOLVED"),
                  _when(n, "🕒 Resolved")]
    elif n.kind == OFFLINE:
        checks = config.FAILURE_LIMIT
        description = (f"### ⛔ {n.vm_id} stopped responding\n"
                       f"The VM failed **{checks} consecutive health checks** and is now marked **OFFLINE**.\n\n"
                       + "❌" * checks + f"  **{checks} / {checks}** checks failed")
        fields = [_vm_field(n), _field("📌 Status", "⛔ OFFLINE"), _when(n, "🕒 Detected", inline=True)]
        if n.detail:
            fields.append(_field("🧾 Last error", f"```{scrub(n.detail)[:300]}```", inline=False))
    elif n.kind == RECOVERED:
        description = (f"### ✅ {n.vm_id} is back online\n"
                       f"Health checks are passing again after **{format_duration(n.downtime_s)}** of downtime.")
        fields = [_vm_field(n), _field("📌 Status", "🟢 RECOVERED"),
                  _field("⏱️ Downtime", f"**{format_duration(n.downtime_s)}**"), _when(n, "🕒 Back online")]
    else:
        vms = len(config.VMS)
        description = ("### Discord webhook notification is working.\n"
                       "This is a test message from the VIGILANT central monitor — no action needed.")
        fields = [_field("💻 Central monitor", f"`{socket.gethostname()}`"),
                  _field("🖥️ Monitoring", f"{vms} VMs · every {config.POLL_INTERVAL_SECONDS} s"),
                  _when(n, "🕒 Sent")]
    title = TITLES.get(n.kind, "VIGILANT")
    footer = f"VIGILANT Central Monitor · {socket.gethostname()}"
    if preview:
        title, footer = f"[PREVIEW] {title}", f"PREVIEW · sample data · {footer}"
    embed = {
        "author": {"name": "VIGILANT · Proxmox VM Monitor"},
        "title": title,
        "description": description[:4096],
        "color": EMBED_COLORS.get(n.color, EMBED_COLORS["TEST"]),
        "fields": fields,
        "timestamp": n.time.astimezone().isoformat(),
        "footer": {"text": footer},
    }
    files = []
    if screenshot:
        embed["image"] = {"url": f"attachment://{SHOT_FILE}"}       # large image at the bottom of the embed
        files.append(SHOT_FILE)
    if images:
        files[:0] = [LOGO_FILE, BADGE_FILE]
        embed["author"]["icon_url"] = f"attachment://{LOGO_FILE}"
        embed["footer"]["icon_url"] = f"attachment://{LOGO_FILE}"
        embed["thumbnail"] = {"url": f"attachment://{BADGE_FILE}"}
    payload = {
        "username": config.DISCORD_USERNAME,
        "embeds": [embed],
        "allowed_mentions": {"parse": []},       # never ping @everyone / roles from monitoring data
    }
    if files:
        payload["attachments"] = [{"id": i, "filename": name} for i, name in enumerate(files)]
    return payload


ENV_VAR = "VIGILANT_DISCORD_WEBHOOK_URL"


def clean_url(value: str) -> str:
    return (value or "").strip().strip('"').strip("'").strip()


def is_valid_webhook_url(value: str) -> bool:
    return bool(WEBHOOK_PATTERN.match(clean_url(value)))


def store_webhook_url(value: str) -> str:
    """Persist the URL ('' removes it) as the Windows *user* environment variable — the same
    place as the README method; never in a project file, PostgreSQL or a log. Also updates
    this process. Returns a short description of where it was stored."""
    value = clean_url(value)
    if value:
        os.environ[ENV_VAR] = value
    else:
        os.environ.pop(ENV_VAR, None)
    if sys.platform != "win32":
        return "for this run only (not Windows: set the variable in your shell profile)"
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        if value:
            winreg.SetValueEx(key, ENV_VAR, 0, winreg.REG_SZ, value)
        else:
            try:
                winreg.DeleteValue(key, ENV_VAR)
            except FileNotFoundError:
                pass
    try:   # tell Explorer so newly opened terminals see the change (like setx); best effort
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 1000, None)
    except Exception:
        pass
    return "saved to your Windows user environment variable"


class DiscordChannel(Channel):
    name = "Discord"
    record_tag = "DISCORD_NOTIFICATION"

    def __init__(self, webhook_url: str):
        self._session = None
        self.configure(webhook_url)

    def configure(self, webhook_url: str):
        """Switch to a new webhook URL at runtime ('' = not configured)."""
        self._url = clean_url(webhook_url)
        self.present = bool(self._url)
        self._valid = bool(WEBHOOK_PATTERN.match(self._url))
        self._session = None

    def __repr__(self):                      # never show the secret, even in a debugger / traceback
        return f"<DiscordChannel {'configured' if self._valid else 'not configured'}>"

    def configured(self) -> bool:
        return self._valid

    def problem(self) -> str:
        if not self.present:
            return "VIGILANT_DISCORD_WEBHOOK_URL is not set"
        if not self._valid:
            return "VIGILANT_DISCORD_WEBHOOK_URL is not a valid Discord webhook URL"
        return ""

    def send(self, n: Notification) -> None:
        if not self._valid:
            raise NotificationError(self.problem())
        if self._session is None:
            self._session = requests.Session()
        images = build_images(n)
        payload = build_payload(n, preview=getattr(n, "preview", False), images=LOGO_FILE in images,
                                screenshot=SHOT_FILE in images)
        timeout = config.DISCORD_TIMEOUT_SECONDS
        for _attempt in range(3):
            try:
                if images:     # multipart: embed JSON + the logo / status badge it references
                    files = {f"files[{i}]": (name, data, "image/jpeg" if name.endswith(".jpg") else "image/png")
                             for i, (name, data) in enumerate(images.items())}
                    resp = self._session.post(self._url, data={"payload_json": json.dumps(payload)}, files=files,
                                              timeout=timeout)
                else:
                    resp = self._session.post(self._url, json=payload, timeout=timeout)
            except requests.ConnectTimeout:
                raise NotificationError(f"cannot reach discord.com within {timeout:g} s "
                                        "(network, DNS, proxy or firewall)") from None
            except requests.Timeout:
                raise NotificationError(f"Discord did not answer within {timeout:g} s (timeout)") from None
            except requests.ConnectionError:
                raise NotificationError("cannot reach discord.com (network, DNS, proxy or firewall)") from None
            except requests.RequestException as exc:
                raise NotificationError(f"HTTP request failed ({type(exc).__name__})") from None
            code = resp.status_code
            if code in (200, 204):
                return
            if code == 429:                      # rate limited: wait as instructed (bounded), then retry
                try:
                    wait = float(resp.json().get("retry_after", 1.0))
                except ValueError:
                    wait = float(resp.headers.get("Retry-After", 1.0) or 1.0)
                if wait > 30:
                    raise NotificationError(f"rate limited by Discord for {wait:.0f} s")
                time.sleep(max(0.5, wait))
                continue
            if code in (401, 403, 404):
                raise NotificationError(f"webhook rejected by Discord (HTTP {code}) — "
                                        "it was deleted or the URL is wrong")
            if code == 400:
                raise NotificationError(f"Discord rejected the message (HTTP 400): {scrub(resp.text)[:160]}")
            raise NotificationError(f"Discord returned HTTP {code}")
        raise NotificationError("rate limited by Discord (gave up after 3 attempts)")
