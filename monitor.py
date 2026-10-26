#!/usr/bin/env python3
"""
VIGILANT — Embedded Systems Proxmox VM Monitoring Project
=========================================================

One project, clearly separated modes:

  python3 monitor.py --mode node      # inside each Ubuntu VM (Flask + psutil agent, port 5000)
  python  monitor.py --mode central   # on the Windows host: VIGILANT VM Monitor GUI
  python  monitor.py --mode check     # terminal test: can we reach the 3 VMs and PostgreSQL?
  python  monitor.py --mode init-db   # create the PostgreSQL database + tables
  python  monitor.py --mode discord-preview   # send one sample of each Discord message type

Layout:
  config.py            all settings (VM IPs, thresholds, timing, DB)
  node_agent.py        NODE MODE
  central/vm_client.py     HTTP communication with the node agents
  central/monitoring.py    polling engine, offline detection, events
  central/severity.py      threshold classification
  central/database.py      PostgreSQL layer
  central/alarm.py         audible alarm
  central/reports.py       CSV / PDF export
  central/gui.py           VIGILANT GUI (customtkinter)
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
from applog import setup_logging  # noqa: E402

PIP_HINTS = {
    "flask": "flask", "psutil": "psutil", "customtkinter": "customtkinter",
    "PIL": "pillow", "requests": "requests", "psycopg2": "psycopg2-binary",
    "reportlab": "reportlab",
}


def _missing(exc: ImportError, mode: str) -> int:
    name = (getattr(exc, "name", "") or "").split(".")[0]
    pkg = PIP_HINTS.get(name, name or "?")
    req = "requirements-node.txt" if mode == "node" else "requirements-central.txt"
    print(f"\n[VIGILANT] Missing Python package '{name}'.\n"
          f"Install it with:   pip install {pkg}\n"
          f"or everything:     pip install -r {req}\n", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="VIGILANT Proxmox VM Monitor")
    parser.add_argument("--mode", required=True, choices=["node", "central", "check", "init-db", "discord-preview"],
                        help="node = VM agent, central = GUI, check = connectivity test, init-db = create tables")
    parser.add_argument("--host", default=None, help="node mode bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="node mode port (default 5000)")
    args = parser.parse_args(argv)

    setup_logging(args.mode)
    try:
        if args.mode == "node":
            from node_agent import run_node
            return run_node(args.host or config.NODE_BIND_HOST, args.port or config.NODE_PORT)
        if args.mode == "central":
            from central.gui import run_central
            return run_central()
        if args.mode == "check":
            from central.diagnostics import run_check
            return run_check()
        if args.mode == "init-db":
            from central.diagnostics import run_init_db
            return run_init_db()
        if args.mode == "discord-preview":
            from central.diagnostics import run_discord_preview
            return run_discord_preview()
    except ImportError as exc:
        return _missing(exc, args.mode)
    return 1


if __name__ == "__main__":
    sys.exit(main())
