"""
PostgreSQL persistence layer for the VIGILANT central monitor.

* Creates the database (if allowed) and the tables from schema.sql.
* Uses a small thread-safe connection pool; broken connections are discarded
  and replaced automatically, so the app recovers when PostgreSQL comes back.
* High-frequency writes (samples, events) go through a background writer queue
  so the GUI and the polling thread never wait for the database.
* Every failure is converted into a readable DatabaseError message.
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import json
import logging
import os
import queue
import socket
import threading

import psycopg2
import psycopg2.extras
import psycopg2.pool
from psycopg2 import sql

import config

log = logging.getLogger("vigilant.db")
SCHEMA_FILE = os.path.join(config.BASE_DIR, "schema.sql")


class DatabaseError(Exception):
    """Readable database problem shown to the user."""


def friendly_error(exc: Exception, cfg: dict) -> str:
    text = str(exc).strip()
    first = text.splitlines()[0] if text else type(exc).__name__
    low = first.lower()
    where = f"{cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['dbname']}"
    hint = ""
    if "password authentication failed" in low or "no password supplied" in low:
        hint = "  → Set EMSYS_DB_PASSWORD to your PostgreSQL password (see README)."
    elif "connection refused" in low or "could not connect" in low or "timeout" in low or "timed out" in low:
        hint = "  → Is the PostgreSQL service running and listening on that host/port?"
    elif "permission denied to create database" in low:
        hint = "  → Create the database manually (README step 3) or use a superuser account."
    elif "does not exist" in low and "role" in low:
        hint = "  → That PostgreSQL user does not exist; set EMSYS_DB_USER (default: postgres)."
    return f"PostgreSQL error ({where}): {first}{hint}"


def _load_schema() -> str:
    try:
        with open(SCHEMA_FILE, encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        raise DatabaseError(f"schema.sql not found next to monitor.py ({exc}).")


class Database:
    def __init__(self, cfg=None, notify=None):
        self.cfg = dict(cfg or config.DB_CONFIG)
        self.notify = notify                 # thread-safe callable, e.g. ui_queue.put
        self.pool = None
        self.ok = False
        self.error = None
        self._queue: queue.Queue = queue.Queue(maxsize=5000)
        self._writer = None
        self._write_failing = False
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- setup
    def connect(self):
        with self._lock:
            self._close_pool()
            try:
                schema = _load_schema()
                self._ensure_database()
                self.pool = psycopg2.pool.ThreadedConnectionPool(
                    1, 8, connect_timeout=5, application_name="VIGILANT central", **self.cfg)
                with self._conn() as conn, conn.cursor() as cur:
                    cur.execute(schema)
                self.ok, self.error = True, None
            except DatabaseError as exc:
                self.ok, self.error = False, str(exc)
                self._close_pool()
                raise
            except psycopg2.Error as exc:
                self.ok, self.error = False, friendly_error(exc, self.cfg)
                self._close_pool()
                raise DatabaseError(self.error) from exc
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._writer_loop, name="db-writer", daemon=True)
            self._writer.start()
        log.info("PostgreSQL ready: %s@%s:%s/%s", self.cfg["user"], self.cfg["host"],
                 self.cfg["port"], self.cfg["dbname"])

    def _ensure_database(self):
        try:
            psycopg2.connect(connect_timeout=5, **self.cfg).close()
            return
        except psycopg2.OperationalError as exc:
            if "does not exist" not in str(exc) or "database" not in str(exc):
                raise
        log.info("Creating database %s", self.cfg["dbname"])
        admin = dict(self.cfg)
        admin["dbname"] = "postgres"
        conn = psycopg2.connect(connect_timeout=5, **admin)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.cfg["dbname"])))
        finally:
            conn.close()

    def _close_pool(self):
        if self.pool is not None:
            try:
                self.pool.closeall()
            except Exception:
                pass
        self.pool = None

    @contextlib.contextmanager
    def _conn(self):
        if self.pool is None:
            raise DatabaseError(self.error or "PostgreSQL is not connected.")
        try:
            conn = self.pool.getconn()
        except psycopg2.Error as exc:
            raise DatabaseError(friendly_error(exc, self.cfg)) from exc
        broken = False
        try:
            yield conn
            conn.commit()
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            broken = True
            raise DatabaseError(friendly_error(exc, self.cfg)) from exc
        except Exception:
            try:
                conn.rollback()
            except Exception:
                broken = True
            raise
        finally:
            try:
                self.pool.putconn(conn, close=broken or bool(conn.closed))
            except Exception:
                pass

    def _query(self, query, params=(), dict_rows=True):
        with self._conn() as conn:
            factory = psycopg2.extras.RealDictCursor if dict_rows else None
            with conn.cursor(cursor_factory=factory) as cur:
                cur.execute(query, params)
                return cur.fetchall() if cur.description else []

    # ----------------------------------------------------------------- writer queue
    def enqueue(self, kind: str, payload) -> bool:
        if not self.ok:
            return False
        try:
            self._queue.put_nowait((kind, payload))
            return True
        except queue.Full:
            log.warning("Database queue full; a %s record was skipped.", kind)
            return False

    def _writer_loop(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                kind, payload = item
                if kind == "sample":
                    self.insert_sample(payload)
                elif kind == "event":
                    self.insert_event(payload)
                elif kind == "end_session":
                    self.end_session(payload)
                if self._write_failing:
                    self._write_failing = False
                    log.info("Database writes resumed.")
                    self._notify(("db_ok", "Database writes resumed"))
            except Exception as exc:
                log.error("Database write failed: %s", exc)
                if not self._write_failing:
                    self._write_failing = True
                    self._notify(("db_error", str(exc)))
            finally:
                self._queue.task_done()

    def _notify(self, message):
        if self.notify:
            try:
                self.notify(message)
            except Exception:
                pass

    def shutdown(self, timeout: float = 5.0):
        if self._writer and self._writer.is_alive():
            try:
                self._queue.put(None, timeout=1)
            except queue.Full:
                pass
            self._writer.join(timeout)
        self._close_pool()

    # ----------------------------------------------------------------- VMs & settings
    def upsert_vms(self, vms):
        with self._conn() as conn, conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO vm_info(vm_id, hostname, ip_address, port, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (vm_id) DO UPDATE SET hostname = EXCLUDED.hostname,
                    ip_address = EXCLUDED.ip_address, port = EXCLUDED.port, updated_at = NOW()
            """, [(vm.vm_id, vm.hostname, vm.ip, vm.port) for vm in vms])

    def load_thresholds(self) -> dict:
        rows = self._query("SELECT metric, yellow, orange, red FROM threshold_settings")
        return {r["metric"]: (r["yellow"], r["orange"], r["red"]) for r in rows}

    def save_thresholds(self, thresholds: dict):
        with self._conn() as conn, conn.cursor() as cur:
            for metric, (y, o, r) in thresholds.items():
                cur.execute("""
                    INSERT INTO threshold_settings(metric, yellow, orange, red, updated_at)
                    VALUES (%s, %s, %s, %s, LOCALTIMESTAMP)
                    ON CONFLICT (metric) DO UPDATE SET yellow = EXCLUDED.yellow, orange = EXCLUDED.orange,
                        red = EXCLUDED.red, updated_at = NOW()
                """, (metric, y, o, r))

    def get_retention_days(self, default: int) -> int:
        rows = self._query("SELECT setting_value FROM app_settings WHERE setting_key = 'retention_days'")
        try:
            days = int(rows[0]["setting_value"]) if rows else default
            return days if 1 <= days <= 3650 else default
        except (TypeError, ValueError):
            return default

    def set_retention_days(self, days: int):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_settings(setting_key, setting_value) VALUES ('retention_days', %s)
                ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
            """, (str(days),))

    # ----------------------------------------------------------------- sessions
    def close_orphan_sessions(self) -> int:
        """Sessions left open by a crash/power-off get an end time = their last sample."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE monitor_sessions s
                SET ended_at = COALESCE((SELECT MAX(sampled_at) FROM resource_samples r
                                         WHERE r.session_id = s.session_id), s.started_at)
                WHERE ended_at IS NULL
            """)
            return cur.rowcount

    def start_session(self, vms, thresholds: dict) -> int:
        vm_text = ", ".join(f"{vm.vm_id} ({vm.ip})" for vm in vms)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO monitor_sessions(started_at, central_host, vms_monitored, poll_interval_s,
                                             failure_limit, thresholds_json)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING session_id
            """, (dt.datetime.now(), socket.gethostname(), vm_text, config.POLL_INTERVAL_SECONDS, config.FAILURE_LIMIT,
                  psycopg2.extras.Json({m: list(v) for m, v in thresholds.items()})))
            return cur.fetchone()[0]

    def end_session(self, session_id):
        if not session_id:
            return
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE monitor_sessions SET ended_at = %s WHERE session_id = %s AND ended_at IS NULL",
                        (dt.datetime.now(), session_id))

    def latest_session_id(self):
        rows = self._query("SELECT session_id FROM monitor_sessions ORDER BY started_at DESC LIMIT 1")
        return rows[0]["session_id"] if rows else None

    def sessions_summary(self, limit: int = 200):
        return self._query("""
            SELECT s.session_id, s.started_at, s.ended_at, s.vms_monitored, s.central_host,
                   COUNT(e.event_id) FILTER (WHERE e.event_type = 'THRESHOLD'
                                             AND e.severity IN ('WARNING','CRITICAL')) AS alerts,
                   COUNT(e.event_id) FILTER (WHERE e.event_type = 'OFFLINE')   AS offline_events,
                   COUNT(e.event_id) FILTER (WHERE e.event_type = 'RECOVERED') AS recoveries
            FROM monitor_sessions s
            LEFT JOIN monitor_events e ON e.session_id = s.session_id
            GROUP BY s.session_id
            ORDER BY s.started_at DESC
            LIMIT %s
        """, (limit,))

    def session_report(self, session_id, event_limit: int = 500) -> dict:
        session = self._query("SELECT * FROM monitor_sessions WHERE session_id = %s", (session_id,))
        if not session:
            raise DatabaseError(f"Session #{session_id} does not exist.")
        vm_stats = self._query("""
            SELECT vm_id, MAX(hostname) AS hostname, MAX(ip_address) AS ip_address,
                   COUNT(*) AS samples,
                   COUNT(*) FILTER (WHERE cpu_percent IS NOT NULL) AS online_samples,
                   AVG(cpu_percent)  AS avg_cpu,  MAX(cpu_percent)  AS max_cpu,
                   AVG(ram_percent)  AS avg_ram,  MAX(ram_percent)  AS max_ram,
                   AVG(disk_percent) AS avg_disk, MAX(disk_percent) AS max_disk,
                   (ARRAY_AGG(status ORDER BY sampled_at DESC))[1] AS last_status,
                   MAX(sampled_at) FILTER (WHERE cpu_percent IS NOT NULL) AS last_online
            FROM resource_samples WHERE session_id = %s
            GROUP BY vm_id ORDER BY vm_id
        """, (session_id,))
        counts = self._query("""
            SELECT COUNT(*) FILTER (WHERE event_type = 'THRESHOLD' AND severity IN ('WARNING','CRITICAL')) AS alerts,
                   COUNT(*) FILTER (WHERE event_type = 'THRESHOLD' AND severity = 'CRITICAL') AS critical,
                   COUNT(*) FILTER (WHERE event_type = 'THRESHOLD' AND severity = 'WARNING')  AS warnings,
                   COUNT(*) FILTER (WHERE event_type = 'THRESHOLD' AND severity = 'ELEVATED') AS elevated,
                   COUNT(*) FILTER (WHERE event_type = 'OFFLINE')   AS offline_events,
                   COUNT(*) FILTER (WHERE event_type = 'RECOVERED') AS recoveries,
                   COUNT(*) AS total_events
            FROM monitor_events WHERE session_id = %s
        """, (session_id,))[0]
        events = self._query("""
            SELECT occurred_at, vm_id, hostname, ip_address, event_type, metric, metric_value,
                   threshold_value, severity, vm_status, message
            FROM monitor_events WHERE session_id = %s
            ORDER BY occurred_at, event_id LIMIT %s
        """, (session_id, event_limit))
        return {"session": session[0], "vm_stats": vm_stats, "counts": counts, "events": events,
                "total_samples": sum(int(v["samples"]) for v in vm_stats)}

    # ----------------------------------------------------------------- inserts
    def insert_sample(self, s: dict):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO resource_samples(session_id, sampled_at, vm_id, hostname, ip_address,
                    cpu_percent, ram_percent, disk_percent, severity, status, top_processes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (s["session_id"], s["time"], s["vm_id"], s["hostname"], s["ip"], s["cpu"], s["ram"],
                  s["disk"], s["severity"], s["status"], psycopg2.extras.Json(s.get("processes") or [])))
            if s["cpu"] is not None:
                cur.execute("UPDATE vm_info SET last_seen = %s WHERE vm_id = %s", (s["time"], s["vm_id"]))

    def insert_event(self, e: dict):
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO monitor_events(session_id, occurred_at, vm_id, hostname, ip_address, event_type,
                    metric, metric_value, threshold_value, severity, vm_status, message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (e.get("session_id"), e["time"], e.get("vm_id"), e.get("hostname"), e.get("ip"),
                  e["event_type"], e.get("metric"), e.get("value"), e.get("threshold"),
                  e["severity"], e.get("status"), e["message"]))

    # ----------------------------------------------------------------- history queries
    @staticmethod
    def _filters(time_col, vm_id=None, severity=None, event_type=None, since=None, until=None, session_id=None):
        clauses, params = [], []
        if vm_id:
            clauses.append("vm_id = %s"); params.append(vm_id)
        if severity:
            clauses.append("severity = %s"); params.append(severity)
        if event_type:
            clauses.append("event_type = %s"); params.append(event_type)
        if since:
            clauses.append(f"{time_col} >= %s"); params.append(since)
        if until:
            clauses.append(f"{time_col} <= %s"); params.append(until)
        if session_id:
            clauses.append("session_id = %s"); params.append(session_id)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", params

    def query_events(self, limit=500, **filters):
        where, params = self._filters("occurred_at", **filters)
        return self._query(f"""
            SELECT occurred_at, vm_id, ip_address, event_type, metric, metric_value, threshold_value,
                   severity, vm_status, message, session_id
            FROM monitor_events {where}
            ORDER BY occurred_at DESC, event_id DESC LIMIT %s
        """, (*params, limit))

    def query_samples(self, limit=400, **filters):
        filters.pop("event_type", None)
        where, params = self._filters("sampled_at", **filters)
        return self._query(f"""
            SELECT sampled_at, vm_id, ip_address, cpu_percent, ram_percent, disk_percent, severity, status
            FROM resource_samples {where}
            ORDER BY sampled_at DESC, sample_id DESC LIMIT %s
        """, (*params, limit))

    # ----------------------------------------------------------------- export / maintenance
    SAMPLE_HEADERS = ["timestamp", "session_id", "vm_id", "hostname", "ip", "cpu", "ram", "disk", "severity", "status"]
    EVENT_HEADERS = ["timestamp", "session_id", "vm_id", "hostname", "ip", "event_type", "metric", "value",
                     "threshold", "severity", "status", "message"]

    def export_csv(self, path: str, kind: str, scope: str, session_id=None) -> int:
        """kind: 'samples' | 'events'; scope: 'session' | '24h' | 'all'. Streams rows to the file."""
        time_col = "sampled_at" if kind == "samples" else "occurred_at"
        if scope == "session":
            if not session_id:
                raise DatabaseError("No monitoring session is active or selected.")
            where, params = "WHERE session_id = %s", [session_id]
        elif scope == "24h":
            where, params = f"WHERE {time_col} >= %s", [dt.datetime.now() - dt.timedelta(hours=24)]
        else:
            where, params = "", []
        if kind == "samples":
            query = f"""SELECT sampled_at, session_id, vm_id, hostname, ip_address,
                               ROUND(cpu_percent::numeric, 2), ROUND(ram_percent::numeric, 2),
                               ROUND(disk_percent::numeric, 2), severity, status
                        FROM resource_samples {where} ORDER BY sampled_at, sample_id"""
            headers = self.SAMPLE_HEADERS
        else:
            query = f"""SELECT occurred_at, session_id, vm_id, hostname, ip_address, event_type, metric,
                               ROUND(metric_value::numeric, 2), threshold_value, severity, vm_status, message
                        FROM monitor_events {where} ORDER BY occurred_at, event_id"""
            headers = self.EVENT_HEADERS
        count = 0
        with self._conn() as conn:
            with conn.cursor(name="vigilant_export") as cur:      # server-side cursor: streams big tables
                cur.itersize = 2000
                cur.execute(query, params)
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
                    for row in cur:
                        row = list(row)
                        row[0] = row[0].strftime("%Y-%m-%d %H:%M:%S")
                        writer.writerow(["" if v is None else v for v in row])
                        count += 1
        return count

    def cleanup(self, retention_days: int):
        with self._conn() as conn, conn.cursor() as cur:
            cutoff = dt.datetime.now() - dt.timedelta(days=retention_days)
            cur.execute("DELETE FROM resource_samples WHERE sampled_at < %s", (cutoff,))
            samples = cur.rowcount
            cur.execute("DELETE FROM monitor_events WHERE occurred_at < %s", (cutoff,))
            return samples, cur.rowcount


def to_json(value) -> str:
    return json.dumps(value, default=str)
