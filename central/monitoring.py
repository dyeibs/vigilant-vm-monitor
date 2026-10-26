"""
Central monitoring engine.

A background thread polls all VMs every POLL_INTERVAL_SECONDS (the three HTTP
requests run in parallel), applies offline detection (FAILURE_LIMIT consecutive
failures), evaluates thresholds, creates events, queues database writes and
publishes a snapshot to the GUI through a queue. The GUI thread never blocks
on the network.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import config
from central.severity import LEVEL_NAMES, ThresholdStore, classify_with_hysteresis
from central.vm_client import VMFetchError, fetch_metrics

log = logging.getLogger("vigilant.monitor")

CONNECTING = "CONNECTING"
ONLINE = "ONLINE"
RETRYING = "RETRYING"      # 1..FAILURE_LIMIT-1 consecutive failures
OFFLINE = "OFFLINE"
RECOVERED = "RECOVERED"


def format_duration(seconds) -> str:
    seconds = int(max(0, seconds or 0))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h:02}h {m:02}m"
    if h:
        return f"{h}h {m:02}m {s:02}s"
    if m:
        return f"{m}m {s:02}s"
    return f"{s}s"


class VMState:
    """Everything the central monitor knows about one VM."""

    def __init__(self, vm: config.VMConfig):
        self.vm = vm
        self.status = CONNECTING
        self.failures = 0
        self.first_failure_at = None
        self.last_update = None        # last successful sample
        self.last_check = None
        self.last_error = ""
        self.offline_since = None
        self.recovered_at = None
        self.values = {m: None for m in config.METRICS}
        self.levels = {m: 0 for m in config.METRICS}
        self.history = {m: deque(maxlen=config.HISTORY_POINTS) for m in config.METRICS}
        self.reported_hostname = None
        self.extra = {}
        self.processes = []
        self.source = None
        self.metrics_path = vm.metrics_path
        self.last_event_at = {}
        self.offline_count = 0
        self.recovery_count = 0

    def sample_severity(self, fresh: bool) -> str:
        if self.status == OFFLINE:
            return "OFFLINE"
        if not fresh:
            return "UNKNOWN"
        return LEVEL_NAMES[max(self.levels.values())]

    def snapshot(self) -> dict:
        return {
            "vm_id": self.vm.vm_id, "hostname": self.vm.hostname, "ip": self.vm.ip, "port": self.vm.port,
            "status": self.status, "failures": self.failures, "last_update": self.last_update,
            "last_check": self.last_check, "last_error": self.last_error,
            "offline_since": self.offline_since, "recovered_at": self.recovered_at,
            "values": dict(self.values), "levels": dict(self.levels),
            "history": {m: list(d) for m, d in self.history.items()},
            "reported_hostname": self.reported_hostname, "extra": dict(self.extra),
            "process_count": len(self.processes), "source": self.source,
            "metrics_path": self.metrics_path,
            "offline_count": self.offline_count, "recovery_count": self.recovery_count,
        }


class MonitorEngine:
    """Owns the polling thread. Messages to the GUI: ("snapshot", engine, dict) / ("event", engine, dict).
    `notifier` (optional, central/notifications.py) receives every metric observation and event;
    it only queues work, so the polling loop never waits for Discord or any other channel."""

    def __init__(self, vms, thresholds: ThresholdStore, ui_queue, db=None, session_id=None, notifier=None):
        self.vms = list(vms)
        self.thresholds = thresholds
        self.ui_queue = ui_queue
        self.db = db
        self.session_id = session_id
        self.notifier = notifier
        self.states = {vm.vm_id: VMState(vm) for vm in self.vms}
        self.cycles = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=max(1, len(self.vms)), thread_name_prefix="vm-poll")
        self._thread = threading.Thread(target=self._run, name="monitor-engine", daemon=True)

    # ---------------------------------------------------------------- control
    def start(self):
        self._notify("reset")
        self._thread.start()

    def stop(self, timeout: float = 10.0):
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def poke(self):
        """Run the next poll immediately (e.g. right after thresholds change)."""
        self._wake.set()

    def attach_database(self, db, session_id):
        self.db, self.session_id = db, session_id

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    # ---------------------------------------------------------------- loop
    def _run(self):
        names = ", ".join(f"{vm.vm_id} ({vm.ip})" for vm in self.vms)
        self._emit(None, dt.datetime.now(), "INFO", "INFO",
                   f"Monitoring started — {names}, every {config.POLL_INTERVAL_SECONDS}s")
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                log.exception("Polling cycle failed")
            delay = max(0.2, config.POLL_INTERVAL_SECONDS - (time.monotonic() - started))
            self._wake.wait(delay)
            self._wake.clear()
        self._emit(None, dt.datetime.now(), "INFO", "INFO", "Monitoring stopped")

    def poll_once(self):
        futures = {}
        for vm_id, st in self.states.items():
            try:
                futures[vm_id] = self._pool.submit(fetch_metrics, st.vm, st.metrics_path)
            except RuntimeError:          # pool shut down while stopping
                return
        for vm_id, fut in futures.items():
            st = self.states[vm_id]
            data, err = None, ""
            try:
                data, path = fut.result()
            except VMFetchError as exc:
                err = str(exc)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
            now = dt.datetime.now()
            st.last_check = now
            if data is None:
                self._on_failure(st, err, now)
                fresh = False
            else:
                st.metrics_path = path
                self._on_success(st, data, now)
                fresh = True
            self._record_sample(st, now, fresh)
        self.cycles += 1
        self._publish()

    # ---------------------------------------------------------------- state machine
    def _on_success(self, st: VMState, data: dict, now):
        previous = st.status
        st.failures = 0
        st.first_failure_at = None
        st.last_error = ""
        st.values = {"CPU": data["cpu"], "RAM": data["ram"], "DISK": data["disk"]}
        for m, v in st.values.items():
            if v is not None:
                st.history[m].append(v)
        st.extra = data.get("extra", {})
        st.processes = data.get("processes", [])
        st.reported_hostname = data.get("hostname")
        st.source = data.get("source")
        st.last_update = now

        if previous == OFFLINE:
            downtime = (now - st.offline_since).total_seconds() if st.offline_since else 0
            st.status = RECOVERED
            st.recovered_at = now
            st.recovery_count += 1
            self._emit(st, now, "RECOVERED", "RECOVERED",
                       f"{st.vm.vm_id} RECOVERED at {now:%H:%M:%S} after {format_duration(downtime)} offline",
                       value=round(downtime, 1))
            st.offline_since = None
        elif previous == RECOVERED and st.recovered_at and \
                (now - st.recovered_at).total_seconds() < config.RECOVERED_DISPLAY_SECONDS:
            pass                          # keep showing RECOVERED for a while
        else:
            if previous == CONNECTING:
                log.info("%s online via %s%s", st.vm.vm_id, st.vm.base_url, st.source)
            st.status = ONLINE
        self._evaluate_thresholds(st, now)

    def _on_failure(self, st: VMState, error: str, now):
        st.failures += 1
        st.last_error = error
        if st.first_failure_at is None:
            st.first_failure_at = now
        if st.status == OFFLINE:
            return                        # already reported: no duplicate offline alerts
        if st.failures >= config.FAILURE_LIMIT:
            st.status = OFFLINE
            st.offline_since = st.first_failure_at
            st.offline_count += 1
            st.values = {m: None for m in config.METRICS}
            st.levels = {m: 0 for m in config.METRICS}
            self._emit(st, now, "OFFLINE", "OFFLINE",
                       f"{st.vm.vm_id} became OFFLINE ({config.FAILURE_LIMIT} consecutive failed health checks "
                       f"since {st.offline_since:%H:%M:%S}; last error: {error})")
        else:
            if st.status != CONNECTING:
                st.status = RETRYING
            log.warning("%s health check failed (%d/%d): %s", st.vm.vm_id, st.failures,
                        config.FAILURE_LIMIT, error)

    def _evaluate_thresholds(self, st: VMState, now):
        all_th = self.thresholds.all()
        for metric in config.METRICS:
            value = st.values.get(metric)
            if value is None:
                continue
            th = all_th[metric]
            prev = st.levels[metric]
            level = classify_with_hysteresis(value, th, prev, config.HYSTERESIS_PERCENT)
            self._notify("observe_metric", st.vm, metric, level, value, th, st.status, now)
            if level == prev:
                continue
            st.levels[metric] = level
            if level > prev:
                if level == 1 and not config.LOG_ELEVATED_EVENTS:
                    continue
                key = (metric, level)
                last = st.last_event_at.get(key)
                if last and (now - last).total_seconds() < config.EVENT_COOLDOWN_SECONDS:
                    continue
                st.last_event_at[key] = now
                name = LEVEL_NAMES[level]
                self._emit(st, now, "THRESHOLD", name, f"{st.vm.vm_id} {metric} reached {value:.1f}% - {name}",
                           metric=metric, value=value, threshold=th[level - 1])
            elif level == 0 and prev >= 2:
                self._emit(st, now, "RESOLVED", "NORMAL",
                           f"{st.vm.vm_id} {metric} back to normal ({value:.1f}%)",
                           metric=metric, value=value, threshold=th[0])

    # ---------------------------------------------------------------- outputs
    def _emit(self, st, now, event_type, severity, message, metric=None, value=None, threshold=None):
        event = {
            "time": now, "session_id": self.session_id,
            "vm_id": st.vm.vm_id if st else None, "hostname": st.vm.hostname if st else None,
            "ip": st.vm.ip if st else None, "event_type": event_type, "metric": metric,
            "value": value, "threshold": threshold, "severity": severity,
            "status": st.status if st else None, "message": message,
        }
        (log.warning if severity in ("CRITICAL", "OFFLINE") else log.info)("[%s] %s", severity, message)
        self.ui_queue.put(("event", self, event))
        if self.db is not None and self.session_id is not None:
            self.db.enqueue("event", event)
        if st is not None:
            self._notify("handle_event", event)

    def _notify(self, method, *args):
        """Hand an observation to the notification layer — never blocks, never raises."""
        if self.notifier is None:
            return
        try:
            getattr(self.notifier, method)(*args)
        except Exception:
            log.exception("Notification layer error (monitoring continues)")

    def _record_sample(self, st: VMState, now, fresh: bool):
        if self.db is None or self.session_id is None:
            return
        self.db.enqueue("sample", {
            "time": now, "session_id": self.session_id, "vm_id": st.vm.vm_id,
            "hostname": st.reported_hostname or st.vm.hostname, "ip": st.vm.ip,
            "cpu": st.values["CPU"] if fresh else None, "ram": st.values["RAM"] if fresh else None,
            "disk": st.values["DISK"] if fresh else None,
            "severity": st.sample_severity(fresh), "status": st.status,
            "processes": st.processes[:5] if fresh else [],
        })

    def _publish(self):
        alarms = []
        for vm_id, st in self.states.items():
            if st.status == OFFLINE:
                alarms.append({"key": f"{vm_id}:OFFLINE", "vm_id": vm_id, "text": f"{vm_id} OFFLINE"})
                continue
            for m in config.METRICS:
                v = st.values.get(m)
                if st.levels[m] >= 3 and v is not None:
                    alarms.append({"key": f"{vm_id}:{m}", "vm_id": vm_id, "text": f"{vm_id} {m} {v:.1f}% CRITICAL"})
        snapshot = {"time": dt.datetime.now(), "cycle": self.cycles, "session_id": self.session_id,
                    "vms": {vid: st.snapshot() for vid, st in self.states.items()}, "alarms": alarms}
        self.ui_queue.put(("snapshot", self, snapshot))
