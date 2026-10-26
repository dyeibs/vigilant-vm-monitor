"""
VIGILANT — Virtual Infrastructure Guard & Intelligent Live Analytics Network Technology
Proxmox VM Monitor : CENTRAL CONFIGURATION
=====================================================

Everything you normally need to change lives in THIS file:

  * VMS ..................... the three Proxmox Ubuntu VMs (ID, hostname, IP, port)
  * timing .................. polling interval, request timeout, FAILURE_LIMIT
  * DEFAULT_THRESHOLDS ...... severity levels (yellow / orange / red)
  * DB_CONFIG ............... PostgreSQL connection (or EMSYS_DB_* env vars)
  * DISCORD_* ............... Discord webhook notifications (URL via env var only)

This module is imported by BOTH node mode and central mode, so it only uses
the Python standard library.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

APP_NAME = "VIGILANT — Proxmox VM Monitor"
FULL_NAME = "Virtual Infrastructure Guard & Intelligent Live Analytics Network Technology"
PROJECT_TITLE = "Embedded Systems Proxmox VM Monitoring Project"
VERSION = "2.0"


@dataclass(frozen=True)
class VMConfig:
    vm_id: str                          # label shown everywhere, e.g. "VM101"
    hostname: str                       # expected Ubuntu hostname
    ip: str                             # IP address of the VM's Flask node agent
    port: int = 5000                    # node agent port
    metrics_path: Optional[str] = None  # None = auto-detect the node's JSON endpoint

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{self.port}"

    @property
    def proxmox_vmid(self) -> str:
        digits = "".join(ch for ch in self.vm_id if ch.isdigit())
        return digits or self.vm_id


# ---------------------------------------------------------------------------
# 1) THE MONITORED VMs  — edit IPs / hostnames / ports here (one place only)
# ---------------------------------------------------------------------------
VMS = [
    VMConfig("VM101", "ubuntu-monitor-01", "192.168.56.111", 5000),
    VMConfig("VM102", "ubuntu-monitor-02", "192.168.56.112", 5000),
    VMConfig("VM103", "ubuntu-monitor-03", "192.168.56.113", 5000),
]

# ---------------------------------------------------------------------------
# 2) POLLING / OFFLINE DETECTION
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = 5          # one health check per VM every 5 s
REQUEST_TIMEOUT_SECONDS = 2.5      # HTTP timeout per request
FAILURE_LIMIT = 3                  # OFFLINE only after 3 consecutive failures
RECOVERED_DISPLAY_SECONDS = 30     # how long the "RECOVERED" badge stays visible
EVENT_COOLDOWN_SECONDS = 60        # same metric/level is not re-logged within this time
HYSTERESIS_PERCENT = 2.0           # a level clears only when value drops 2 % below it
LOG_ELEVATED_EVENTS = True         # also log YELLOW (ELEVATED) crossings to history
PROCESS_REFRESH_SECONDS = 5        # Processes page auto-refresh
PROCESS_LIMIT = 60                 # max rows requested from a node
HISTORY_POINTS = 60                # dashboard trend length (60 x 5 s = 5 minutes)

# ---------------------------------------------------------------------------
# 3) SEVERITY THRESHOLDS  (yellow, orange, red) — defaults; editable in the GUI
# ---------------------------------------------------------------------------
METRICS = ("CPU", "RAM", "DISK")
DEFAULT_THRESHOLDS = {
    "CPU": (60.0, 70.0, 85.0),
    "RAM": (60.0, 70.0, 85.0),
    "DISK": (60.0, 75.0, 90.0),
}

# Endpoints the central monitor tries on each node (first JSON hit is cached).
CANDIDATE_METRIC_PATHS = ("/api/metrics", "/api/stats", "/api/status", "/metrics",
                          "/stats", "/status", "/api", "/data", "/")
CANDIDATE_PROCESS_PATHS = ("/api/processes", "/processes", "/api/procs")

# ---------------------------------------------------------------------------
# 4) POSTGRESQL  (same EMSYS_* variables as the original EMSYS monitor)
# ---------------------------------------------------------------------------
DB_CONFIG = {
    "host": os.getenv("EMSYS_DB_HOST", "localhost"),
    "port": int(os.getenv("EMSYS_DB_PORT", "5432")),
    "user": os.getenv("EMSYS_DB_USER", "postgres"),
    "password": os.getenv("EMSYS_DB_PASSWORD", os.getenv("PGPASSWORD", "")),
    "dbname": os.getenv("EMSYS_DB_NAME", "vigilant_vm_monitor"),
}
RETENTION_DAYS = int(os.getenv("EMSYS_RETENTION_DAYS", "30"))

# ---------------------------------------------------------------------------
# 5) NODE MODE
# ---------------------------------------------------------------------------
NODE_BIND_HOST = os.getenv("VIGILANT_NODE_HOST", "0.0.0.0")
NODE_PORT = int(os.getenv("VIGILANT_NODE_PORT", "5000"))

# ---------------------------------------------------------------------------
# 6) DISCORD WEBHOOK NOTIFICATIONS  (sent by the central monitor only)
# ---------------------------------------------------------------------------
# The webhook URL is a SECRET: it is read ONLY from the environment variable
# VIGILANT_DISCORD_WEBHOOK_URL (see README). Never paste the URL into this file.
# Not set = Discord notifications disabled; everything else keeps working.
DISCORD_WEBHOOK_URL = os.getenv("VIGILANT_DISCORD_WEBHOOK_URL", "")

DISCORD_NOTIFICATIONS_ENABLED = True     # start-up state of the ON/OFF switch (Threshold Settings page)

DISCORD_NOTIFY_ORANGE = True             # metric reached ORANGE (WARNING)
DISCORD_NOTIFY_RED = True                # metric reached RED (CRITICAL)
DISCORD_NOTIFY_OFFLINE = True            # VM OFFLINE after FAILURE_LIMIT failed checks
DISCORD_NOTIFY_RECOVERY = True           # VM back online
DISCORD_NOTIFY_RESOLVED = True           # an ORANGE/RED alert dropped back below Orange

DISCORD_ATTACH_SCREENSHOT = True         # attach a screenshot of the VIGILANT dashboard (GUI only)

DISCORD_COOLDOWN_SECONDS = 60           # same VM/metric/level is not re-announced within this time
DISCORD_TIMEOUT_SECONDS = 8              # HTTP timeout per webhook call (runs in a background thread)
DISCORD_USERNAME = "VIGILANT"            # sender name shown in the Discord channel

# ---------------------------------------------------------------------------
# Paths (no need to edit)
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
LOGO_FILE = os.path.join(ASSETS_DIR, "vigilant_team.jpg")   # team photo shown in the sidebar and PDF
ALARM_WAV_CANDIDATES = (                     # first existing file is used
    os.path.join(ASSETS_DIR, "vigilant_alarm.wav"),   # VIGILANT alert tone
    os.path.join(BASE_DIR, "alarm_loop.wav"),         # optional: your own sound (remove the tone above to use it)
    os.path.join(ASSETS_DIR, "alarm_loop.wav"),
)
