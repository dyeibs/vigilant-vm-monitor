"""
Notification layer (channel-agnostic).

The monitoring engine reports what it observes; this module decides WHAT is
worth telling a human and hands it to a delivery channel (currently Discord,
see central/discord_notify.py) on a background worker thread. The polling
thread and the GUI never wait for the network, and a failing channel never
affects monitoring.

Spam protection (the monitor polls every 5 s):
  * a metric alert is sent when the level ESCALATES to ORANGE (WARNING) or
    RED (CRITICAL) — never again while it stays at that level;
  * RED -> ORANGE is silent (the alert is still active); escalating again to a
    level already announced is limited to once per DISCORD_COOLDOWN_SECONDS
    per VM / metric / level;
  * one RESOLVED message when an announced alert drops back below Orange
    (levels already include the engine's hysteresis, so they do not flap);
  * OFFLINE once (after FAILURE_LIMIT consecutive failures), RECOVERED once.
"""
from __future__ import annotations

import datetime as dt
import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Optional

import config
from central.severity import COLOR_NAMES, LEVEL_NAMES

log = logging.getLogger("vigilant.notify")

# Notification kinds
WARNING = "WARNING"        # ORANGE threshold
CRITICAL = "CRITICAL"      # RED threshold
OFFLINE = "OFFLINE"
RECOVERED = "RECOVERED"
RESOLVED = "RESOLVED"
TEST = "TEST"
KINDS = (WARNING, CRITICAL, OFFLINE, RECOVERED, RESOLVED)
KIND_LABELS = {WARNING: "Orange threshold", CRITICAL: "Critical threshold", OFFLINE: "VM Offline",
               RECOVERED: "VM Recovery", RESOLVED: "Alert Resolved"}

# Channel status
NOT_CONFIGURED = "NOT CONFIGURED"
READY = "READY"            # configured, nothing sent yet in this run
CONNECTED = "CONNECTED"    # last delivery succeeded
ERROR = "ERROR"            # last delivery failed (or the URL is invalid)


class NotificationError(Exception):
    """Readable delivery problem. Must NEVER contain the webhook URL."""


@dataclass
class Notification:
    kind: str
    time: dt.datetime
    vm_id: Optional[str] = None
    hostname: Optional[str] = None
    ip: Optional[str] = None
    metric: Optional[str] = None
    value: Optional[float] = None
    threshold: Optional[float] = None
    severity: Optional[str] = None           # NORMAL / ELEVATED / WARNING / CRITICAL / OFFLINE / RECOVERED / TEST
    color: Optional[str] = None              # GREEN / YELLOW / ORANGE / RED / OFFLINE / TEST (semantic)
    previous_severity: Optional[str] = None
    status: Optional[str] = None             # VM status at the time (ONLINE, OFFLINE, RECOVERED ...)
    downtime_s: Optional[float] = None
    detail: str = ""
    thresholds: Optional[tuple] = None       # (yellow, orange, red) of the metric, for the channel's gauge
    screenshot: Optional[bytes] = field(default=None, repr=False)   # JPEG of the dashboard, if attached

    def summary(self) -> str:
        if self.kind in (WARNING, CRITICAL):
            return f"{self.vm_id} {self.metric} {self.severity} {self.value:.1f}%"
        if self.kind == RESOLVED:
            return f"{self.vm_id} {self.metric} RESOLVED (was {self.previous_severity}) {self.value:.1f}%"
        if self.kind == OFFLINE:
            return f"{self.vm_id} OFFLINE"
        if self.kind == RECOVERED:
            return f"{self.vm_id} RECOVERED"
        return "Test message"


class Channel:
    """Interface for a delivery channel (Discord today; e-mail, Telegram ... later)."""
    name = "channel"
    record_tag = "NOTIFICATION"     # severity used for the History & Alerts record
    present = False                 # a destination was provided (it may still be invalid)

    def configured(self) -> bool:
        return False

    def problem(self) -> str:
        """Why the channel cannot send ('' when it can). Must not reveal secrets."""
        return "not configured"

    def configure(self, destination: str) -> None:
        """Change the destination at runtime."""
        raise NotImplementedError

    def send(self, n: Notification) -> None:
        """Deliver synchronously (called on the worker thread). Raise NotificationError on failure."""
        raise NotImplementedError


class NotificationManager:
    """Decides what to send (thread-safe, pure in-memory, called from the polling thread)
    and delivers it on its own worker thread.

    listener(kind, payload) is called from the worker thread with:
      ("status", None)   — channel status changed
      ("record", event)  — a History & Alerts event dict describing a delivery (session_id unset)
    """

    def __init__(self, channel: Channel, listener=None, enabled=True, flags=None,
                 cooldown_seconds=60):
        self.channel = channel
        self.listener = listener
        self.cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._flags = {k: True for k in KINDS}
        self._flags.update(flags or {})
        self._alerts = {}           # (vm_id, metric) -> [announced level, peak announced level]
        self._offline = set()       # VMs that went OFFLINE (a RECOVERED message is due)
        self._last_sent = {}        # cooldown key -> datetime
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._thread = None
        self._closed = False
        self.screenshot_provider = None   # callable(timeout) -> JPEG bytes | None (set by the GUI)
        self.attach_screenshot = True
        self.state = READY if channel.configured() else (ERROR if channel.present else NOT_CONFIGURED)
        self.detail = channel.problem()
        self.last_result = ""

    # ------------------------------------------------------------ settings (GUI thread)
    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool):
        with self._lock:
            self._enabled = bool(value)
        log.info("%s notifications %s", self.channel.name, "ON" if value else "OFF")

    def flags(self) -> dict:
        with self._lock:
            return dict(self._flags)

    def set_flag(self, kind: str, value: bool):
        with self._lock:
            self._flags[kind] = bool(value)

    def status(self):
        return self.state, self.detail, self.last_result

    def reconfigure(self, destination: str):
        """Point the channel at a new destination at runtime (e.g. a webhook URL pasted in the GUI)."""
        self.channel.configure(destination)
        self.state = READY if self.channel.configured() else (ERROR if self.channel.present else NOT_CONFIGURED)
        self.detail, self.last_result = self.channel.problem(), ""
        log.info("%s destination %s", self.channel.name, "updated" if self.channel.configured() else "cleared")

    # ------------------------------------------------------------ observations (polling thread)
    def reset(self):
        """New monitoring run: forget active alerts silently (cooldowns are kept)."""
        with self._lock:
            self._alerts.clear()
            self._offline.clear()

    def observe_metric(self, vm, metric, level, value, th, status, now):
        """Called once per poll for every fresh metric value; `level` already includes hysteresis."""
        key = (vm.vm_id, metric)
        with self._lock:
            alert = self._alerts.setdefault(key, [0, 0])
            announced, peak = alert
            if level >= 2:
                if level < announced:            # RED -> ORANGE: still an active alert, stay quiet
                    alert[0] = level
                    return
                if level == announced:
                    return
                kind = CRITICAL if level >= 3 else WARNING
                if not self._allowed(kind) or not self._cooldown_ok((vm.vm_id, metric, level), now):
                    return                       # retried on the next poll while still elevated
                alert[0], alert[1] = level, max(peak, level)
                n = Notification(kind, now, vm.vm_id, vm.hostname, vm.ip, metric, value, th[level - 1],
                                 LEVEL_NAMES[level], COLOR_NAMES[level], status=status, thresholds=tuple(th))
            elif announced:
                self._alerts[key] = [0, 0]
                if not self._allowed(RESOLVED):
                    return
                n = Notification(RESOLVED, now, vm.vm_id, vm.hostname, vm.ip, metric, value, th[1],
                                 LEVEL_NAMES[level], "GREEN", previous_severity=LEVEL_NAMES[peak], status=status,
                                 thresholds=tuple(th))
            else:
                return
        self._enqueue(n)

    def handle_event(self, event: dict):
        """Engine events (the same dicts that go to History & Alerts)."""
        event_type, vm_id = event.get("event_type"), event.get("vm_id")
        if not vm_id or event_type not in (OFFLINE, RECOVERED):
            return
        common = dict(vm_id=vm_id, hostname=event.get("hostname"), ip=event.get("ip"), status=event.get("status"))
        with self._lock:
            if event_type == OFFLINE:
                for key in [k for k in self._alerts if k[0] == vm_id]:
                    del self._alerts[key]        # metrics are unknown while offline: no RESOLVED spam
                self._offline.add(vm_id)
                if not self._allowed(OFFLINE):
                    return
                last_error = event.get("message", "").partition("last error: ")[2]
                last_error = last_error[:-1] if last_error.endswith(")") else last_error
                n = Notification(OFFLINE, event["time"], severity="OFFLINE", color="OFFLINE",
                                 detail=last_error, **common)
            else:
                if vm_id not in self._offline:
                    return
                self._offline.discard(vm_id)
                if not self._allowed(RECOVERED):
                    return
                n = Notification(RECOVERED, event["time"], severity="RECOVERED", color="GREEN",
                                 downtime_s=event.get("value"), **common)
        self._enqueue(n)

    def send_test(self) -> bool:
        """Queue a clearly labelled test message (works even when the ON/OFF switch is off)."""
        if not self.channel.configured():
            return False
        return self._enqueue(Notification(TEST, dt.datetime.now(), severity="TEST", color="TEST"))

    def _allowed(self, kind) -> bool:
        return self._enabled and self._flags.get(kind, False) and self.channel.configured()

    def _cooldown_ok(self, key, now) -> bool:
        last = self._last_sent.get(key)
        if last and (now - last).total_seconds() < self.cooldown_seconds:
            return False
        self._last_sent[key] = now
        return True

    # ------------------------------------------------------------ delivery (worker thread)
    def _enqueue(self, n: Notification) -> bool:
        if self._closed:
            return False
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, name=f"{self.channel.name}-notify", daemon=True)
            self._thread.start()
        try:
            self._queue.put_nowait(n)
            return True
        except queue.Full:
            log.warning("%s notification queue full; '%s' was dropped", self.channel.name, n.summary())
            return False

    def _worker(self):
        while True:
            n = self._queue.get()
            try:
                if n is None:
                    return
                self._deliver(n)
            finally:
                self._queue.task_done()

    def _deliver(self, n: Notification):
        if self.attach_screenshot and self.screenshot_provider and n.screenshot is None:
            try:                                 # waits here (worker thread), never in the GUI or poller
                n.screenshot = self.screenshot_provider(3.0)
            except Exception as exc:
                log.info("Screenshot skipped: %s", type(exc).__name__)
        try:
            self.channel.send(n)
            ok, err = True, ""
        except NotificationError as exc:
            ok, err = False, str(exc)
        except Exception as exc:                 # never let a channel bug kill the worker
            ok, err = False, f"unexpected {type(exc).__name__}"
        stamp = f"{dt.datetime.now():%H:%M:%S}"
        if ok:
            log.info("%s notification sent: %s", self.channel.name, n.summary())
            self.state, self.detail = CONNECTED, ""
            self.last_result = f"{stamp}  sent: {n.summary()}"
        else:
            log.warning("%s notification FAILED (%s): %s", self.channel.name, n.summary(), err)
            self.state, self.detail = ERROR, err
            self.last_result = f"{stamp}  FAILED: {n.summary()}"
        self._notify("status", None)
        self._notify("record", self._record(n, ok, err))

    def _record(self, n: Notification, ok: bool, err: str) -> dict:
        tag = self.channel.record_tag
        if n.kind == TEST:
            event_type, severity = "TEST", "TEST"
            message = (f"{self.channel.name} test notification sent" if ok
                       else f"{self.channel.name} test notification FAILED: {err}")
        else:
            event_type, severity = "INFO", tag if ok else f"{tag}_FAILED"
            message = (f"{tag}: {n.summary()}" if ok else f"{tag}_FAILED: {n.summary()} — {err}")
        return {"time": dt.datetime.now(), "session_id": None, "vm_id": n.vm_id, "hostname": n.hostname,
                "ip": n.ip, "event_type": event_type, "metric": n.metric,
                "value": n.value if n.kind in (WARNING, CRITICAL, RESOLVED) else None,
                "threshold": n.threshold, "severity": severity, "status": n.severity, "message": message}

    def _notify(self, kind, payload):
        if self.listener:
            try:
                self.listener(kind, payload)
            except Exception:
                pass

    def shutdown(self, timeout: float = 3.0):
        """Let already-queued messages go out (bounded wait), then stop the worker."""
        self._closed = True
        if self._thread and self._thread.is_alive():
            try:
                self._queue.put(None, timeout=0.5)
            except queue.Full:
                return
            self._thread.join(timeout)


def build_default_manager(listener=None) -> NotificationManager:
    """The notification setup used by the central GUI (settings from config.py)."""
    from central.discord_notify import DiscordChannel
    channel = DiscordChannel(config.DISCORD_WEBHOOK_URL)
    manager = NotificationManager(
        channel, listener=listener, enabled=config.DISCORD_NOTIFICATIONS_ENABLED,
        flags={WARNING: config.DISCORD_NOTIFY_ORANGE, CRITICAL: config.DISCORD_NOTIFY_RED,
               OFFLINE: config.DISCORD_NOTIFY_OFFLINE, RECOVERED: config.DISCORD_NOTIFY_RECOVERY,
               RESOLVED: config.DISCORD_NOTIFY_RESOLVED},
        cooldown_seconds=config.DISCORD_COOLDOWN_SECONDS)
    manager.attach_screenshot = config.DISCORD_ATTACH_SCREENSHOT
    if channel.configured():
        log.info("Discord notifications configured (webhook URL from VIGILANT_DISCORD_WEBHOOK_URL)")
    else:
        log.info("Discord notifications disabled: %s", channel.problem())
    return manager
