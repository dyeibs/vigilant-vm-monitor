"""Terminal diagnostics: `python monitor.py --mode check` and `--mode init-db`."""
from __future__ import annotations

import config
from central.vm_client import VMFetchError, fetch_metrics, fetch_processes


def run_check() -> int:
    print(f"\nVIGILANT connectivity check — {len(config.VMS)} VMs, timeout {config.REQUEST_TIMEOUT_SECONDS}s\n")
    failures = 0
    for vm in config.VMS:
        print(f"[{vm.vm_id}] {vm.hostname}  {vm.base_url}")
        try:
            m, path = fetch_metrics(vm)
            print(f"   OK   endpoint {path}  •  CPU {m['cpu']}%  RAM {m['ram']}%  DISK {m['disk']}%"
                  f"  •  reported hostname: {m['hostname'] or '?'}")
            if m["hostname"] and m["hostname"] != vm.hostname:
                print(f"   NOTE hostname differs from config.py ({vm.hostname}) — check the IP mapping")
            try:
                procs, source, total = fetch_processes(vm, "cpu", 3, path)
                top = ", ".join(f"{p['name']}({p['cpu']:.1f}%)" for p in procs)
                print(f"   OK   processes via {source}: {total if total is not None else len(procs)} • top: {top}")
            except VMFetchError as exc:
                print(f"   WARN processes: {exc}")
        except VMFetchError as exc:
            failures += 1
            print(f"   FAIL {exc}")
    print("\nPostgreSQL:")
    try:
        from central.database import Database
        db = Database(config.DB_CONFIG)
        db.connect()
        print(f"   OK   {config.DB_CONFIG['user']}@{config.DB_CONFIG['host']}:{config.DB_CONFIG['port']}"
              f"/{config.DB_CONFIG['dbname']} (tables ready)")
        db.shutdown()
    except Exception as exc:
        failures += 1
        print(f"   FAIL {exc}")
    print("\nDiscord notifications (optional, no message is sent here):")
    from central.discord_notify import DiscordChannel
    channel = DiscordChannel(config.DISCORD_WEBHOOK_URL)
    if channel.configured():
        print("   OK   webhook URL found in VIGILANT_DISCORD_WEBHOOK_URL (use 'Test Discord Notification' in the GUI)")
    else:
        print(f"   INFO disabled — {channel.problem()}")
    print("\nResult:", "all checks passed" if not failures else f"{failures} problem(s) — see README troubleshooting")
    return 0 if not failures else 1


def run_discord_preview() -> int:
    """Send one sample of every Discord notification type, labelled [PREVIEW] (sample data, not real alerts)."""
    import datetime as dt
    import time

    from central.discord_notify import DiscordChannel
    from central.notifications import (CRITICAL, OFFLINE, RECOVERED, RESOLVED, TEST, WARNING, Notification,
                                       NotificationError)
    channel = DiscordChannel(config.DISCORD_WEBHOOK_URL)
    if not channel.configured():
        print(f"Discord preview not sent: {channel.problem()}")
        return 1
    now = dt.datetime.now()
    a, b, c = config.VMS[0], config.VMS[1 % len(config.VMS)], config.VMS[2 % len(config.VMS)]
    samples = [
        Notification(TEST, now, severity="TEST", color="TEST"),
        Notification(WARNING, now, b.vm_id, b.hostname, b.ip, "RAM", 73.2, 70.0, "WARNING", "ORANGE", status="ONLINE",
                     thresholds=config.DEFAULT_THRESHOLDS["RAM"]),
        Notification(CRITICAL, now, a.vm_id, a.hostname, a.ip, "CPU", 86.4, 85.0, "CRITICAL", "RED", status="ONLINE",
                     thresholds=config.DEFAULT_THRESHOLDS["CPU"]),
        Notification(RESOLVED, now, a.vm_id, a.hostname, a.ip, "CPU", 42.1, 70.0, "NORMAL", "GREEN",
                     previous_severity="CRITICAL", status="ONLINE", thresholds=config.DEFAULT_THRESHOLDS["CPU"]),
        Notification(OFFLINE, now, c.vm_id, c.hostname, c.ip, severity="OFFLINE", color="OFFLINE", status="OFFLINE",
                     detail=f"connection refused ({c.base_url})"),
        Notification(RECOVERED, now, c.vm_id, c.hostname, c.ip, severity="RECOVERED", color="GREEN",
                     status="RECOVERED", downtime_s=42),
    ]
    failures = 0
    for n in samples:
        n.preview = True
        try:
            channel.send(n)
            print(f"   OK   {n.kind}")
        except NotificationError as exc:
            failures += 1
            print(f"   FAIL {n.kind}: {exc}")
        time.sleep(1.2)                      # stay well inside Discord's webhook rate limit
    return 0 if not failures else 1


def run_init_db() -> int:
    from central.database import Database
    db = Database(config.DB_CONFIG)
    try:
        db.connect()
        db.upsert_vms(config.VMS)
        if not db.load_thresholds():
            db.save_thresholds(config.DEFAULT_THRESHOLDS)
        print(f"Database '{config.DB_CONFIG['dbname']}' and tables are ready; {len(config.VMS)} VMs registered.")
        return 0
    except Exception as exc:
        print(f"FAILED: {exc}")
        return 1
    finally:
        db.shutdown()
