"""
HTTP communication with the Flask node agents (central side).

The client is deliberately tolerant so it works with:
  * the node agent shipped in this project (/api/metrics, /api/processes), and
  * an older/custom node agent: it auto-detects a JSON endpoint from
    config.CANDIDATE_METRIC_PATHS and accepts common key names
    (cpu / cpu_percent, ram / memory / memory_percent, disk / disk_percent,
    processes / top_processes, nested {"percent": ..} objects, ...).
  * As a last resort it reads the numbers from the node's HTML page.
No values are ever invented: if nothing can be read, the check FAILS.
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Optional

import requests

import config
from config import VMConfig


class VMFetchError(Exception):
    """A health check failed (connection refused, timeout, unusable response, ...)."""


_HEADERS = {"Accept": "application/json", "User-Agent": f"VIGILANT-Central/{config.VERSION}"}
CPU_KEYS = ("cpu", "cpu_percent", "cpu_usage", "cpu_usage_percent", "cpupercent", "cpu_load")
RAM_KEYS = ("ram", "ram_percent", "memory", "memory_percent", "mem", "mem_percent",
            "ram_usage", "memory_usage", "virtual_memory")
DISK_KEYS = ("disk", "disk_percent", "disk_usage", "disk_usage_percent", "storage", "root_disk")
PROC_KEYS = ("processes", "top_processes", "top_procs", "procs", "process_list")
NESTED_PERCENT_KEYS = ("percent", "usage", "used_percent", "usage_percent", "pct", "value")
EXTRA_KEYS = {"uptime_seconds": "uptime_seconds", "uptime": "uptime_seconds",
              "process_count": "process_count", "load_avg": "load_avg", "load_1m": "load_avg",
              "ram_used_gb": "ram_used_gb", "ram_total_gb": "ram_total_gb",
              "disk_used_gb": "disk_used_gb", "disk_total_gb": "disk_total_gb",
              "cpu_count": "cpu_count", "os": "os", "platform": "os", "ip": "reported_ip"}


# ------------------------------------------------------------------ parsing helpers
def _num(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (list, tuple)):
        return _num(v[0]) if v else None
    try:
        return float(str(v).strip().rstrip("%").strip())
    except ValueError:
        return None


def _lower(d: dict) -> dict:
    return {str(k).lower(): v for k, v in d.items()}


def _clamp(v):
    return None if v is None else max(0.0, min(100.0, float(v)))


def _pick_percent(data: dict, keys) -> Optional[float]:
    for key in keys:
        if key not in data:
            continue
        val = data[key]
        if isinstance(val, dict):
            inner = _lower(val)
            for k in NESTED_PERCENT_KEYS:
                n = _num(inner.get(k))
                if n is not None:
                    return n
        else:
            n = _num(val)
            if n is not None:
                return n
    return None


def _unwrap(payload: dict) -> dict:
    data = _lower(payload)
    if _pick_percent(data, CPU_KEYS) is not None:
        return data
    for wrapper in ("data", "metrics", "stats", "system", "result", "resources"):
        inner = data.get(wrapper)
        if isinstance(inner, dict):
            merged = dict(data)
            merged.update(_lower(inner))
            if _pick_percent(merged, CPU_KEYS) is not None:
                return merged
    return data


def normalize_processes(items) -> list[dict]:
    out = []
    for p in items or []:
        if not isinstance(p, dict):
            continue
        q = _lower(p)
        pid = _num(q.get("pid"))
        name = q.get("name") or q.get("process") or q.get("command") or q.get("cmd") or "?"
        cpu = next((_num(q[k]) for k in ("cpu", "cpu_percent", "cpu%", "cpu_usage") if _num(q.get(k)) is not None), 0.0)
        mem = next((_num(q[k]) for k in ("mem", "memory", "memory_percent", "mem_percent", "mem%", "ram")
                    if _num(q.get(k)) is not None), 0.0)
        row = {"pid": int(pid) if pid is not None else 0, "name": str(name), "cpu": float(cpu), "mem": float(mem)}
        rss = _num(q.get("rss_mb"))
        if rss is not None:
            row["rss_mb"] = rss
        out.append(row)
    return out


def normalize_metrics(payload) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    data = _unwrap(payload)
    cpu = _pick_percent(data, CPU_KEYS)
    if cpu is None:
        return None
    host = None
    for k in ("hostname", "host", "node", "node_name"):
        if isinstance(data.get(k), str) and data[k].strip():
            host = data[k].strip()
            break
    procs = []
    for k in PROC_KEYS:
        if isinstance(data.get(k), list):
            procs = normalize_processes(data[k])
            break
    extra = {}
    for src, dst in EXTRA_KEYS.items():
        if src in data and dst not in extra:
            v = data[src]
            extra[dst] = v if isinstance(v, str) else _num(v)
    return {"cpu": _clamp(cpu), "ram": _clamp(_pick_percent(data, RAM_KEYS)),
            "disk": _clamp(_pick_percent(data, DISK_KEYS)), "hostname": host,
            "processes": procs, "extra": extra}


def parse_html(text: str) -> Optional[dict]:
    """Best-effort fallback for a node that only serves an HTML page."""
    plain = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", plain))
    plain = re.sub(r"\s+", " ", plain)

    def grab(labels):
        m = re.search(rf"\b(?:{labels})\b[^0-9%]{{0,40}}?(\d+(?:\.\d+)?)\s*%", plain, re.I)
        return float(m.group(1)) if m else None

    cpu = grab("cpu")
    if cpu is None:
        return None
    host = re.search(r"host(?:name)?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9._-]*)", plain, re.I)
    return {"cpu": _clamp(cpu), "ram": _clamp(grab("memory|ram|mem")), "disk": _clamp(grab("disk|storage")),
            "hostname": host.group(1) if host else None, "processes": [], "extra": {}}


# ------------------------------------------------------------------ HTTP
def _get(url: str, timeout: float, params=None) -> requests.Response:
    try:
        return requests.get(url, headers=_HEADERS, timeout=timeout, params=params)
    except requests.exceptions.ConnectTimeout:
        raise VMFetchError(f"connection timed out after {timeout:g}s")
    except requests.exceptions.ReadTimeout:
        raise VMFetchError(f"no response within {timeout:g}s")
    except requests.exceptions.ConnectionError as exc:
        text = str(exc).lower()
        if "refused" in text:
            raise VMFetchError("connection refused (node agent not running?)")
        if "no route" in text or "unreachable" in text:
            raise VMFetchError("host unreachable (VM down or network issue)")
        if "timed out" in text:
            raise VMFetchError(f"connection timed out after {timeout:g}s")
        raise VMFetchError("connection failed: " + str(exc)[:120])
    except requests.RequestException as exc:
        raise VMFetchError(str(exc)[:160])


def fetch_metrics(vm: VMConfig, preferred_path: Optional[str] = None,
                  timeout: float = config.REQUEST_TIMEOUT_SECONDS) -> tuple[dict, str]:
    """One health check. Returns (normalized metrics, endpoint path) or raises VMFetchError."""
    if vm.metrics_path:
        paths = [vm.metrics_path]
    else:
        paths = []
        for p in ((preferred_path,) if preferred_path else ()) + tuple(config.CANDIDATE_METRIC_PATHS):
            if p and p not in paths:
                paths.append(p)
    html_hit = None
    problems = []
    for path in paths:
        # A connection-level failure raises immediately: one timeout per VM per cycle.
        resp = _get(vm.base_url + path, timeout)
        if resp.status_code >= 400:
            problems.append(f"{path}: HTTP {resp.status_code}")
            continue
        ctype = resp.headers.get("Content-Type", "").lower()
        body = resp.text
        payload = None
        if "json" in ctype or body.lstrip()[:1] in ("{", "["):
            try:
                payload = resp.json()
            except ValueError:
                payload = None
        if isinstance(payload, dict):
            m = normalize_metrics(payload)
            if m is not None:
                m["source"] = path
                return m, path
            problems.append(f"{path}: JSON without a CPU field")
            continue
        if html_hit is None and ("html" in ctype or "<" in body[:300]):
            m = parse_html(body)
            if m is not None:
                m["source"] = f"{path} (HTML page)"
                html_hit = (m, path)
                if path == preferred_path:
                    return html_hit
    if html_hit:
        return html_hit
    raise VMFetchError("reachable, but no metrics endpoint recognised ("
                       + ("; ".join(problems[-3:]) or "no JSON found") + ")")


def _sort(procs, sort):
    key = "mem" if sort == "mem" else "cpu"
    other = "cpu" if key == "mem" else "mem"
    return sorted(procs, key=lambda p: (p.get(key, 0), p.get(other, 0)), reverse=True)


def fetch_processes(vm: VMConfig, sort: str = "cpu", limit: int = config.PROCESS_LIMIT,
                    metrics_path: Optional[str] = None,
                    timeout: float = config.REQUEST_TIMEOUT_SECONDS):
    """Process list of the REMOTE VM. Returns (rows, source description, total or None)."""
    for path in config.CANDIDATE_PROCESS_PATHS:
        resp = _get(vm.base_url + path, timeout, params={"sort": sort, "limit": limit})
        if resp.status_code >= 400:
            continue
        try:
            payload = resp.json()
        except ValueError:
            continue
        total = None
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            low = _lower(payload)
            items = next((low[k] for k in PROC_KEYS if isinstance(low.get(k), list)), None)
            total = _num(low.get("total"))
        else:
            items = None
        if items is None:
            continue
        procs = _sort(normalize_processes(items), sort)
        return procs[:limit], path, int(total) if total is not None else len(procs)
    metrics, _path = fetch_metrics(vm, metrics_path, timeout)
    procs = metrics["processes"]
    if not procs:
        raise VMFetchError("node agent does not publish process information "
                           "(update the node to this project's monitor.py to get /api/processes)")
    return _sort(procs, sort)[:limit], f"{metrics['source']} (top processes only)", None
