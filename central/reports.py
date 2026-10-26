"""CSV and PDF reports for the VIGILANT central monitor."""
from __future__ import annotations

import datetime as dt
import os

import config
from central.monitoring import format_duration
from central.severity import LEVEL_COLORS, classify

ACCENT = "#df2531"
ACCENT_DARK = "#8f1720"


def export_csv(db, path: str, kind: str, scope: str, session_id=None) -> int:
    """kind = 'samples' | 'events', scope = 'session' | '24h' | 'all'."""
    return db.export_csv(path, kind, scope, session_id)


def ensure_reportlab():
    import reportlab  # noqa: F401  (raises ImportError with a clear name)


def _f(v, suffix="%"):
    return "—" if v is None else f"{float(v):.1f}{suffix}"


def _ts(v):
    return v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, dt.datetime) else (str(v) if v else "—")


def build_session_pdf(path: str, report: dict, thresholds: dict, db_name: str, logo_path=None):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    s = report["session"]
    counts = report["counts"]
    vm_stats = report["vm_stats"]
    events = report["events"]
    generated = dt.datetime.now()

    styles = getSampleStyleSheet()
    h_title = ParagraphStyle("t", parent=styles["Title"], fontSize=19, leading=23, textColor=colors.HexColor("#111111"),
                             alignment=TA_LEFT, spaceAfter=2)
    h_kicker = ParagraphStyle("k", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor(ACCENT),
                              fontName="Helvetica-Bold", spaceAfter=2)
    h_sub = ParagraphStyle("s", parent=styles["Normal"], fontSize=10.5, textColor=colors.HexColor("#444444"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12.5, textColor=colors.HexColor(ACCENT_DARK),
                        spaceBefore=12, spaceAfter=6)
    body = ParagraphStyle("b", parent=styles["BodyText"], fontSize=9, leading=12)
    cell = ParagraphStyle("c", parent=styles["BodyText"], fontSize=7.6, leading=9.4)
    small = ParagraphStyle("sm", parent=body, fontSize=8, textColor=colors.HexColor("#666666"))

    def table(data, widths, header_bg=ACCENT_DARK, zebra=True, extra=None):
        t = Table(data, colWidths=widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_bg)),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d8cccc")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        if zebra:
            style.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f0f0")]))
        style.extend(extra or [])
        t.setStyle(TableStyle(style))
        return t

    started, ended = s.get("started_at"), s.get("ended_at")
    duration = format_duration(((ended or generated) - started).total_seconds()) if started else "—"

    story = []
    head = [Paragraph("VIGILANT • CENTRAL PROXMOX VM MONITOR", h_kicker),
            Paragraph(config.FULL_NAME, small),
            Paragraph(config.PROJECT_TITLE, h_title),
            Paragraph(f"Monitoring Session Report — Session #{s['session_id']}", h_sub)]
    if logo_path and os.path.isfile(logo_path):
        try:
            from reportlab.lib.utils import ImageReader
            iw, ih = ImageReader(logo_path).getSize()
            logo = Image(logo_path, width=38 * mm, height=38 * mm * ih / iw)
            story.append(Table([[logo, head]], colWidths=[42 * mm, None],
                               style=[("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        except Exception:
            story.extend(head)
    else:
        story.extend(head)
    story.append(Spacer(1, 4))
    story.append(Table([[""]], colWidths=[180 * mm], rowHeights=[2],
                       style=[("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(ACCENT))]))
    story.append(Spacer(1, 8))

    # 1. Session information
    story.append(Paragraph("1. Monitoring session", h2))
    info = [["Field", "Value"],
            ["Session ID", f"#{s['session_id']}"],
            ["Started", _ts(started)],
            ["Ended", _ts(ended) if ended else "Still running (report generated mid-session)"],
            ["Duration", duration],
            ["Central monitor host", s.get("central_host") or "—"],
            ["VMs monitored", Paragraph(s.get("vms_monitored") or "—", cell)],
            ["Polling", f"every {s.get('poll_interval_s') or config.POLL_INTERVAL_SECONDS:g} s, "
                        f"timeout {config.REQUEST_TIMEOUT_SECONDS:g} s, OFFLINE after "
                        f"{s.get('failure_limit') or config.FAILURE_LIMIT} consecutive failures"],
            ["Report generated", _ts(generated) + f"  (database: {db_name})"]]
    story.append(table(info, [48 * mm, 132 * mm], extra=[("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold")]))

    # 2. Statistics
    story.append(Paragraph("2. Session statistics", h2))
    stats = [["Samples", "Alerts (W+C)", "Critical", "Warning", "Elevated", "Offline events", "Recoveries"],
             [report["total_samples"], counts["alerts"], counts["critical"], counts["warnings"],
              counts["elevated"], counts["offline_events"], counts["recoveries"]]]
    story.append(table(stats, [25.7 * mm] * 7, extra=[("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                                      ("FONTSIZE", (0, 1), (-1, 1), 11),
                                                      ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold")]))

    # 3. VM list
    story.append(Paragraph("3. Monitored virtual machines", h2))
    vm_rows = [["VM", "Proxmox VMID", "Hostname", "IP address", "Samples", "Availability", "Last status"]]
    by_id = {v["vm_id"]: v for v in vm_stats}
    for vm in config.VMS:
        st = by_id.get(vm.vm_id)
        avail = (f"{100.0 * st['online_samples'] / st['samples']:.1f}%" if st and st["samples"] else "—")
        vm_rows.append([vm.vm_id, vm.proxmox_vmid, (st or {}).get("hostname") or vm.hostname,
                        f"{vm.ip}:{vm.port}", (st or {}).get("samples", 0), avail, (st or {}).get("last_status") or "—"])
    story.append(table(vm_rows, [18 * mm, 22 * mm, 36 * mm, 34 * mm, 18 * mm, 24 * mm, 28 * mm]))
    story.append(Paragraph("Availability = samples with a successful health check ÷ all polling cycles.", small))

    # 4. Resource summary (peak cells coloured by severity)
    story.append(Paragraph("4. CPU / RAM / Disk summary", h2))
    res = [["VM", "Avg CPU", "Peak CPU", "Avg RAM", "Peak RAM", "Avg Disk", "Peak Disk"]]
    extra = []
    for r, vm in enumerate(config.VMS, start=1):
        st = by_id.get(vm.vm_id) or {}
        res.append([vm.vm_id, _f(st.get("avg_cpu")), _f(st.get("max_cpu")), _f(st.get("avg_ram")),
                    _f(st.get("max_ram")), _f(st.get("avg_disk")), _f(st.get("max_disk"))])
        for col, metric, key in ((2, "CPU", "max_cpu"), (4, "RAM", "max_ram"), (6, "DISK", "max_disk")):
            if st.get(key) is not None:
                lvl = classify(float(st[key]), thresholds[metric])
                if lvl:
                    extra.append(("TEXTCOLOR", (col, r), (col, r), colors.HexColor(LEVEL_COLORS[lvl] if lvl > 1 else "#b8860b")))
                    extra.append(("FONTNAME", (col, r), (col, r), "Helvetica-Bold"))
    story.append(table(res, [24 * mm] + [26 * mm] * 6, extra=extra + [("ALIGN", (1, 0), (-1, -1), "CENTER")]))

    # 5. Thresholds
    th_rows = [["Metric", "GREEN (normal)", "YELLOW (elevated)", "ORANGE (warning)", "RED (critical, alarm)"]]
    for m in config.METRICS:
        y, o, r = thresholds[m]
        th_rows.append([m, f"< {y:g}%", f"{y:g} – {o:g}%", f"{o:g} – {r:g}%", f"≥ {r:g}%"])
    story.append(KeepTogether([Paragraph("5. Severity thresholds (current configuration)", h2),
                               table(th_rows, [24 * mm, 36 * mm, 40 * mm, 40 * mm, 40 * mm], extra=[
                                   ("TEXTCOLOR", (1, 1), (1, -1), colors.HexColor("#1e9e57")),
                                   ("TEXTCOLOR", (2, 1), (2, -1), colors.HexColor("#b8860b")),
                                   ("TEXTCOLOR", (3, 1), (3, -1), colors.HexColor("#d96b00")),
                                   ("TEXTCOLOR", (4, 1), (4, -1), colors.HexColor(ACCENT)),
                                   ("ALIGN", (1, 0), (-1, -1), "CENTER")])]))

    sev_color = {"CRITICAL": ACCENT, "WARNING": "#d96b00", "ELEVATED": "#b8860b", "OFFLINE": ACCENT,
                 "RECOVERED": "#119b7a", "NORMAL": "#1e9e57"}

    def event_table(rows, empty_text):
        if not rows:
            return Paragraph(empty_text, body)
        data = [["Time", "VM", "IP", "Event", "Metric", "Value", "Severity", "Message"]]
        extra_style = []
        for i, e in enumerate(rows, start=1):
            value = e.get("metric_value")
            if e["event_type"] == "RECOVERED" and value is not None:
                value_txt = format_duration(value)
            else:
                value_txt = _f(value) if value is not None else "—"
            data.append([_ts(e["occurred_at"])[5:], e.get("vm_id") or "—", e.get("ip_address") or "—",
                         e["event_type"], e.get("metric") or "—", value_txt, e["severity"],
                         Paragraph(e["message"], cell)])
            c = sev_color.get(e["severity"])
            if c:
                extra_style += [("TEXTCOLOR", (6, i), (6, i), colors.HexColor(c)),
                                ("FONTNAME", (6, i), (6, i), "Helvetica-Bold")]
        return table(data, [24 * mm, 13 * mm, 23 * mm, 19 * mm, 12 * mm, 14 * mm, 18 * mm, 57 * mm],
                     extra=extra_style)

    alerts = [e for e in events if e["event_type"] in ("THRESHOLD", "RESOLVED")]
    outages = [e for e in events if e["event_type"] in ("OFFLINE", "RECOVERED")]
    story.append(Paragraph("6. Threshold alerts", h2))
    story.append(event_table(alerts, "No threshold alerts were recorded in this session."))
    story.append(Paragraph("7. Offline / recovery events", h2))
    story.append(event_table(outages, "No VM went offline during this session."))
    if len(events) >= 500:
        story.append(Paragraph("Event list truncated to the first 500 events; export CSV for the full list.", small))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Data was collected by the VIGILANT central monitor from the Flask + psutil node agents running "
        "inside the Proxmox VE Ubuntu VMs (HTTP polling) and stored in PostgreSQL. A VM is declared OFFLINE "
        f"only after {config.FAILURE_LIMIT} consecutive failed health checks.", small))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor(ACCENT))
        canvas.setLineWidth(0.8)
        canvas.line(15 * mm, 12 * mm, A4[0] - 15 * mm, 12 * mm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(15 * mm, 8 * mm, f"VIGILANT Proxmox VM Monitor • Session #{s['session_id']} • "
                                           f"generated {generated:%Y-%m-%d %H:%M}")
        canvas.drawRightString(A4[0] - 15 * mm, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                            bottomMargin=18 * mm, title=f"VIGILANT VM Monitoring Report — Session {s['session_id']}",
                            author="VIGILANT Central Monitor", subject=config.PROJECT_TITLE)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path
