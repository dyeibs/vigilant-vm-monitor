"""
VIGILANT NODE MODE — runs INSIDE each Ubuntu VM.

    python3 monitor.py --mode node            (listens on 0.0.0.0:5000)

Endpoints
---------
  GET /                 human-readable page (CPU, Memory, Disk, Top Processes, Hostname)
  GET /api/health       {"status": "ok", ...}            — lightweight health check
  GET /api/metrics      CPU / RAM / Disk + top processes  — polled by central mode
  GET /api/processes    ?sort=cpu|mem&limit=50            — full process list

Only Flask + psutil are needed on the VMs (no GUI / database packages).
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import os
import platform
import socket
import threading
import time
from string import Template

import psutil
from flask import Flask, Response, jsonify, request

import config

log = logging.getLogger("vigilant.node")
_CPU_COUNT = max(psutil.cpu_count(logical=True) or 1, 1)


# ------------------------------------------------------------------ sampling
class NodeSampler:
    """Background sampler so HTTP requests answer instantly (no blocking psutil calls)."""

    def __init__(self, cpu_interval: float = 1.0, process_every: int = 2):
        self._lock = threading.Lock()
        self._cpu = 0.0
        self._processes: list[dict] = []
        self._process_total = 0
        self._cpu_interval = cpu_interval
        self._process_every = process_every
        self._thread = threading.Thread(target=self._loop, name="node-sampler", daemon=True)

    def start(self):
        psutil.cpu_percent(interval=None)
        self._scan_processes()          # primes per-process CPU baselines
        self._thread.start()

    def _loop(self):
        tick = 0
        while True:
            try:
                cpu = psutil.cpu_percent(interval=self._cpu_interval)   # blocks 1 s, background only
                with self._lock:
                    self._cpu = float(cpu)
                tick += 1
                if tick % self._process_every == 0:
                    self._scan_processes()
            except Exception as exc:  # never let the sampler die
                log.warning("Sampler error: %s", exc)
                time.sleep(1)

    def _scan_processes(self):
        rows = []
        # process_iter() caches Process objects, so cpu_percent(None) is a delta since the last scan.
        for proc in psutil.process_iter(["pid", "name", "username"]):
            try:
                pid = proc.pid
                if pid == 0:
                    continue
                cpu = max(0.0, proc.cpu_percent(interval=None) / _CPU_COUNT)  # Task-Manager style
                mem = proc.memory_percent()
                try:
                    rss = proc.memory_info().rss / (1024 ** 2)
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    rss = 0.0
                rows.append({
                    "pid": pid,
                    "name": proc.info.get("name") or "(unknown)",
                    "user": proc.info.get("username") or "",
                    "cpu": round(cpu, 2),
                    "mem": round(float(mem), 2),
                    "rss_mb": round(rss, 1),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        rows.sort(key=lambda p: (p["cpu"], p["mem"]), reverse=True)
        with self._lock:
            self._processes = rows
            self._process_total = len(rows)

    def cpu(self) -> float:
        with self._lock:
            return self._cpu

    def processes(self) -> tuple[list[dict], int]:
        with self._lock:
            return list(self._processes), self._process_total


SAMPLER = NodeSampler()


def _primary_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))      # no packet is sent
            return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def collect_metrics() -> dict:
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("/")
    cpu = round(SAMPLER.cpu(), 1)
    procs, total = SAMPLER.processes()
    try:
        load1 = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        load1 = None
    top5 = procs[:5]
    return {
        "agent": "vigilant-node",
        "agent_version": config.VERSION,
        "hostname": socket.gethostname(),
        "ip": _primary_ip(),
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        # short + explicit names for compatibility with older/other clients
        "cpu": cpu, "cpu_percent": cpu,
        "ram": vm.percent, "memory_percent": vm.percent,
        "disk": du.percent, "disk_percent": du.percent,
        "ram_used_gb": round(vm.used / 1024 ** 3, 2),
        "ram_total_gb": round(vm.total / 1024 ** 3, 2),
        "disk_used_gb": round(du.used / 1024 ** 3, 2),
        "disk_total_gb": round(du.total / 1024 ** 3, 2),
        "cpu_count": _CPU_COUNT,
        "load_avg": load1,
        "uptime_seconds": int(time.time() - psutil.boot_time()),
        "process_count": total,
        "os": f"{platform.system()} {platform.release()}",
        "top_processes": top5,
    }


# ------------------------------------------------------------------ web page
PAGE = Template("""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="5">
<title>VIGILANT Node • $host</title>
<style>
 body{margin:0;background:#080808;color:#fff;font-family:Segoe UI,Ubuntu,Arial,sans-serif}
 header{background:#101010;border-bottom:1px solid #3a3031;padding:18px 28px}
 header b{letter-spacing:.35em;font-size:18px} header span{color:#df2531;font-size:11px;font-weight:700;letter-spacing:.2em;margin-left:14px}
 main{padding:22px 28px;max-width:980px}
 .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
 .card{background:#151515;border:1px solid #3a3031;border-radius:14px;padding:16px}
 .lbl{color:#b6a9aa;font-size:11px;font-weight:700;letter-spacing:.08em}
 .val{font-size:30px;font-weight:700;margin:6px 0}
 .bar{height:8px;background:#2a2426;border-radius:6px;overflow:hidden}.bar i{display:block;height:100%;background:#df2531}
 table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
 th{color:#b6a9aa;text-align:left;font-size:11px;border-bottom:1px solid #3a3031;padding:6px}
 td{padding:6px;border-bottom:1px solid #1f1a1b}
 .meta{color:#b6a9aa;font-size:13px;margin:0 0 16px}
</style></head><body>
<header><b>VIGILANT</b><span>NODE AGENT</span></header>
<main>
 <p class="meta">Hostname: <b style="color:#fff">$host</b> &nbsp;•&nbsp; IP $ip &nbsp;•&nbsp; $os &nbsp;•&nbsp; Uptime $uptime &nbsp;•&nbsp; $time</p>
 <div class="grid">
  <div class="card"><div class="lbl">CPU</div><div class="val">$cpu%</div><div class="bar"><i style="width:$cpu%"></i></div></div>
  <div class="card"><div class="lbl">MEMORY</div><div class="val">$ram%</div><div class="bar"><i style="width:$ram%"></i></div><div class="meta" style="margin:8px 0 0">$ram_used / $ram_total GB</div></div>
  <div class="card"><div class="lbl">DISK ( / )</div><div class="val">$disk%</div><div class="bar"><i style="width:$disk%"></i></div><div class="meta" style="margin:8px 0 0">$disk_used / $disk_total GB</div></div>
 </div>
 <div class="card" style="margin-top:14px"><div class="lbl">TOP PROCESSES (by CPU) — $pcount running</div>
  <table><tr><th>PID</th><th>PROCESS</th><th>CPU %</th><th>MEM %</th></tr>$rows</table></div>
 <p class="meta" style="margin-top:14px">JSON API: <code>/api/health</code> • <code>/api/metrics</code> • <code>/api/processes?sort=cpu&amp;limit=50</code></p>
</main></body></html>""")


def _fmt_uptime(seconds: int) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return f"{d}d {h:02}h {m:02}m" if d else f"{h:02}h {m:02}m"


def create_app() -> Flask:
    app = Flask("vigilant_node")

    @app.get("/")
    def index():
        m = collect_metrics()
        procs, _ = SAMPLER.processes()
        rows = "".join(
            f"<tr><td>{p['pid']}</td><td>{html.escape(p['name'][:40])}</td>"
            f"<td>{p['cpu']:.1f}</td><td>{p['mem']:.1f}</td></tr>" for p in procs[:10])
        page = PAGE.substitute(
            host=html.escape(m["hostname"]), ip=m["ip"], os=html.escape(m["os"]),
            uptime=_fmt_uptime(m["uptime_seconds"]), time=m["timestamp"].replace("T", " "),
            cpu=f"{m['cpu']:.1f}", ram=f"{m['ram']:.1f}", disk=f"{m['disk']:.1f}",
            ram_used=m["ram_used_gb"], ram_total=m["ram_total_gb"],
            disk_used=m["disk_used_gb"], disk_total=m["disk_total_gb"],
            pcount=m["process_count"], rows=rows)
        return Response(page, mimetype="text/html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "hostname": socket.gethostname(),
                        "time": dt.datetime.now().isoformat(timespec="seconds"),
                        "uptime_seconds": int(time.time() - psutil.boot_time())})

    @app.get("/api/metrics")
    def metrics():
        return jsonify(collect_metrics())

    @app.get("/api/processes")
    def processes():
        sort = (request.args.get("sort") or "cpu").lower()
        try:
            limit = max(1, min(int(request.args.get("limit", 50)), 1000))
        except ValueError:
            limit = 50
        procs, total = SAMPLER.processes()
        if sort in ("mem", "memory", "ram"):
            procs.sort(key=lambda p: (p["mem"], p["cpu"]), reverse=True)
            sort = "mem"
        elif sort == "pid":
            procs.sort(key=lambda p: p["pid"])
        elif sort == "name":
            procs.sort(key=lambda p: p["name"].lower())
        else:
            sort = "cpu"
        return jsonify({"hostname": socket.gethostname(), "sort": sort, "total": total,
                        "processes": procs[:limit]})

    return app


def run_node(host: str, port: int) -> int:
    SAMPLER.start()
    app = create_app()
    logging.getLogger("werkzeug").setLevel(logging.WARNING)   # avoid one log line per poll
    log.info("VIGILANT node agent on %s — http://%s:%s  (API: /api/metrics)",
             socket.gethostname(), _primary_ip(), port)
    app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
    return 0
