# VIGILANT — Central Proxmox VM Monitor

**Virtual Infrastructure Guard & Intelligent Live Analytics Network Technology**  
*Embedded Systems Proxmox VM Monitoring Project*

The original single-file system monitor design (dark sidebar, red accent, arc gauges, trend graphs,
PostgreSQL, alarm, CSV/PDF) redesigned to monitor **three Ubuntu VMs running in
Proxmox VE** instead of the local PC.

```
Windows host ── VirtualBox ── Proxmox VE 9.2.2 ── VM101 / VM102 / VM103 (Ubuntu Server)
     │                                                │
     │  VIGILANT CENTRAL  (python monitor.py          │  VIGILANT NODE (python3 monitor.py --mode node)
     │  --mode central)                               │  Flask + psutil on port 5000
     └──── HTTP GET every 5 s ───────────────────────►│  /api/metrics  /api/processes  /api/health
     │
     └──── PostgreSQL (samples, alerts, offline/recovery events, sessions, thresholds)
```

---

## 1. Folder / file structure

```
vigilant_vm_monitor/
├── monitor.py               ← entry point: --mode node | central | check | init-db
├── config.py                ← ALL settings: the 3 VMs, timing, thresholds, PostgreSQL
├── node_agent.py            ← NODE MODE (Flask + psutil, runs inside each VM)
├── applog.py                ← logging (console + logs/vigilant_<mode>.log)
├── schema.sql               ← PostgreSQL tables (applied automatically)
├── requirements-node.txt    ← packages for the VMs
├── requirements-central.txt ← packages for the Windows host
├── assets/                  ← vigilant_team.jpg (team photo), optional alarm_loop.wav
└── central/                 ← CENTRAL MODE
    ├── vm_client.py         ← HTTP communication with the node agents
    ├── monitoring.py        ← polling thread, FAILURE_LIMIT offline detection, events
    ├── severity.py          ← GREEN/YELLOW/ORANGE/RED classification + threshold store
    ├── database.py          ← PostgreSQL layer (pool, writer queue, queries, CSV export)
    ├── alarm.py             ← VIGILANT audible alarm
    ├── notifications.py     ← notification layer: what to send, spam protection, worker thread
    ├── discord_notify.py    ← Discord webhook channel (embeds, colours, HTTP)
    ├── reports.py           ← PDF report (+ CSV wrapper)
    ├── diagnostics.py       ← --mode check / --mode init-db
    └── gui.py               ← VIGILANT GUI (customtkinter)
```

**Team photo & alarm sound:** the sidebar and the PDF report show
`assets/vigilant_team.jpg` (replace that file to change the picture). The alarm plays
`assets/vigilant_alarm.wav`, a professional monitor-style alert tone (3-pulse + 2-pulse burst, IEC 60601-1-8
style, 2.6 s loop). To use your own sound instead, delete or rename that file and put `alarm_loop.wav` next to
`monitor.py`. To regenerate the VIGILANT tone:
`python -c "from central.alarm import generate_professional_alarm as g; g('assets/vigilant_alarm.wav')"`.
Without any file a built-in two-tone siren is used.

---

## 2. Required pip packages

| Where | Packages |
|---|---|
| Each Ubuntu VM (node) | `flask psutil` |
| Windows host (central) | `customtkinter pillow requests psycopg2-binary reportlab psutil` |

Python 3.9+ (3.11/3.12 recommended). Tkinter ships with the python.org Windows installer.

---

## 3. PostgreSQL setup (Windows host)

1. Install PostgreSQL 15/16/17 from postgresql.org (keep port 5432, remember the
   `postgres` password). The service starts automatically.
2. Tell VIGILANT the password (same EMSYS_* variable names as the original monitor):

   ```powershell
   setx EMSYS_DB_PASSWORD "your_postgres_password"
   # optional (defaults shown):
   setx EMSYS_DB_HOST localhost
   setx EMSYS_DB_PORT 5432
   setx EMSYS_DB_USER postgres
   setx EMSYS_DB_NAME vigilant_vm_monitor
   ```
   Close and reopen the terminal after `setx`.
3. Create the database and tables (also happens automatically on first `--mode central`):

   ```powershell
   python monitor.py --mode init-db
   ```
   Manual alternative:
   ```powershell
   psql -U postgres -c "CREATE DATABASE vigilant_vm_monitor;"
   psql -U postgres -d vigilant_vm_monitor -f schema.sql
   ```

### Tables (see `schema.sql`)

| Table | Contents |
|---|---|
| `vm_info` | VM ID, hostname, IP, port, last seen |
| `monitor_sessions` | session ID, start/end, central host, VMs monitored, poll interval, failure limit, thresholds at start |
| `resource_samples` | one row per VM per 5 s poll: CPU, RAM, Disk, severity, status, top-5 processes (JSONB). Metrics NULL when the VM did not answer |
| `monitor_events` | THRESHOLD alerts, RESOLVED, OFFLINE, RECOVERED, INFO, TEST — with VM, IP, metric, value, threshold, severity, status, message |
| `threshold_settings` | yellow / orange / red per metric |
| `app_settings` | data retention |
| views `alert_events`, `offline_recovery_events` | convenience views for demos |

---

## 4. Installation

### On each Ubuntu VM (VM101, VM102, VM103)

Copy these files to the VM (e.g. `~/vigilant/`): `monitor.py`, `config.py`,
`node_agent.py`, `applog.py`, `requirements-node.txt`.

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv
cd ~/vigilant
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-node.txt
sudo ufw allow 5000/tcp          # only if ufw is enabled
```

> **Your existing node agent.** I did not have your current node script, so this
> project ships a compatible node mode (same command, same port, same page with
> CPU / Memory / Disk / Top Processes / Hostname, plus JSON endpoints).
> Recommended: replace the node files with these so the Processes page gets the
> full list from `/api/processes`. If you keep your current node, the central
> monitor auto-detects a JSON endpoint (`/api/metrics`, `/api/stats`, `/metrics`,
> `/stats`, `/status`, `/` …) and common key names (`cpu`/`cpu_percent`,
> `memory`/`ram`/`memory_percent`, `disk`/`disk_percent`, `top_processes`).
> Run `python monitor.py --mode check` to see exactly which endpoint each VM uses.

### On the Windows host

```powershell
cd C:\path\to\vigilant_vm_monitor
python -m venv venv
venv\Scripts\activate
pip install -r requirements-central.txt
```

---

## 5. Run NODE mode (inside each VM)

```bash
cd ~/vigilant && source venv/bin/activate
python3 monitor.py --mode node                 # 0.0.0.0:5000
```

Check from the Windows browser: `http://192.168.56.111:5000/` (page) and
`http://192.168.56.111:5000/api/metrics` (JSON).

Start automatically at boot (optional) — `/etc/systemd/system/vigilant-node.service`:

```ini
[Unit]
Description=VIGILANT node agent
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/vigilant
ExecStart=/home/ubuntu/vigilant/venv/bin/python3 monitor.py --mode node
Restart=always

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now vigilant-node
```
(Use your real user name instead of `ubuntu`.)

## 6. Run CENTRAL mode (Windows host)

```powershell
venv\Scripts\activate
python monitor.py --mode check      # optional: tests the 3 VMs + PostgreSQL in the terminal
python monitor.py --mode central    # VIGILANT GUI
```

Monitoring starts automatically after the database connects (a new session is
created). Use **Stop / Start Monitoring** in the sidebar to end / begin sessions.

---

## 7. Configure the three VM IP addresses

Edit **only** `config.py`, section 1:

```python
VMS = [
    VMConfig("VM101", "ubuntu-monitor-01", "192.168.56.111", 5000),
    VMConfig("VM102", "ubuntu-monitor-02", "192.168.56.112", 5000),
    VMConfig("VM103", "ubuntu-monitor-03", "192.168.56.113", 5000),
]
```

To force a specific node endpoint, add it: `VMConfig("VM101", "...", "192.168.56.111", 5000, "/api/metrics")`.
Timing (`POLL_INTERVAL_SECONDS = 5`, `REQUEST_TIMEOUT_SECONDS = 2.5`, `FAILURE_LIMIT = 3`)
and default thresholds are in the same file.

---

## 8. How it works (communication with the agents)

* A background **MonitorEngine** thread runs every 5 s and sends the three
  `GET http://<ip>:5000/api/metrics` requests **in parallel** (timeout 2.5 s), so a
  dead VM costs at most one timeout and never delays the others.
* Results go to the GUI through a queue drained every 150 ms → the GUI never freezes.
* **Offline detection:** 1st and 2nd failures show `RETRYING 1/3`, `2/3` (no alert).
  The 3rd consecutive failure sets **OFFLINE**, records **one** OFFLINE event
  (with the time of the first failure) and triggers the alarm. Further failures add
  nothing. The first successful check afterwards records **RECOVERED** with the
  downtime, stops the alarm and shows a teal RECOVERED badge for 30 s.
* **Severity:** values are classified GREEN/YELLOW/ORANGE/RED with the current
  thresholds. An event is logged when a level rises (cooldown 60 s per metric/level)
  and a RESOLVED event when it falls back to normal; a 2 % hysteresis prevents
  flapping around a threshold.
* **Alarm:** sounds for any RED metric or OFFLINE VM, starts once and loops (the
  original monitor behaviour), stops automatically when every condition clears.
  *Mute* silences the current conditions only — a new condition sounds again.
  *Audible alarm ON/OFF* disables sound completely (visual alarm stays). Affected VM
  panels blink red.

---

## 9. How to test each feature (demo script)

| Feature | How to test | Expected |
|---|---|---|
| Live dashboard | Start central mode | 3 VM panels ONLINE, gauges update every 5 s, "Last update" time changes |
| CPU severity | On a VM: `sudo apt install -y stress-ng` then `stress-ng --cpu 0 --timeout 90s` (or `yes > /dev/null &` per core, stop with `killall yes`) | CPU gauge turns yellow → orange → red; History shows `VM101 CPU reached 86.x% - CRITICAL`; alarm sounds; panel blinks |
| RAM severity | `stress-ng --vm 1 --vm-bytes 80% --timeout 90s` | RAM WARNING / CRITICAL events |
| Disk severity | `fallocate -l 5G /tmp/fill.img` (size for ≥ 75 %/90 %), remove with `rm /tmp/fill.img` | DISK WARNING/CRITICAL, RESOLVED after deletion |
| Quick severity demo | Threshold Settings → set CPU to 5 / 10 / 15 → Save | Applies immediately; gauges recolour; alerts + alarm within one poll. Press *Reset to defaults* afterwards |
| Offline detection | Stop the agent (`Ctrl+C` / `sudo systemctl stop vigilant-node`) or shut down the VM in Proxmox | `RETRYING 1/3`, `2/3`, then OFFLINE after ~15 s; one OFFLINE event; alarm |
| Recovery | Start the agent / VM again | RECOVERED event with downtime, alarm stops, RECOVERED badge |
| No duplicate offline alerts | Leave a VM offline for a minute | History shows a single OFFLINE event |
| Mute / Alarm OFF / Test alarm | Sidebar *Mute alarm*, *Audible alarm* switch; Threshold Settings *Test alarm* | Sound stops but visual alarm stays; test plays 3 s and logs a TEST event |
| Processes | Processes page → choose VM101/102/103, sort CPU % / Memory % | Remote process list (PID, CPU %, MEM %, RSS, name), auto-refresh every 5 s |
| History & filters | History & Alerts → VM / Severity / Event / From–To → Apply | Filtered alerts and samples, colour-coded |
| Sessions | Sessions → Refresh, pick an ID → View details | Start/end, VMs, alerts, offline events, recoveries, per-VM stats and events |
| CSV | Footer *Export CSV* → Resource samples / Alerts & events, range | CSV with `timestamp, vm_id, hostname, ip, cpu, ram, disk, severity, status` |
| PDF | Footer *PDF Report* (current session) or Sessions → *PDF for this session* | Report with title, session info, statistics, VM list, CPU/RAM/Disk summary, thresholds, alerts, offline/recovery events |
| DB failure handling | Stop the PostgreSQL service, start central mode | Clear error dialog, sidebar "DATABASE OFFLINE", live monitoring continues ("NOT RECORDING"); start PostgreSQL → *Retry DB connection* → recording resumes |

SQL you can show during the demo:
```sql
SELECT * FROM offline_recovery_events ORDER BY occurred_at DESC;
SELECT vm_id, ROUND(AVG(cpu_percent)::numeric,1) avg_cpu, MAX(cpu_percent) max_cpu
FROM resource_samples GROUP BY vm_id;
```

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| All VMs `RETRYING`/`OFFLINE`, error *connection timed out* / *host unreachable* | Windows must be on the 192.168.56.0/24 host-only network: `ping 192.168.56.111`. Check the VirtualBox host-only adapter (192.168.56.1) and the Proxmox bridge (`vmbr0`) the VMs use. |
| *connection refused* | The node agent is not running, or bound to 127.0.0.1. Run `python3 monitor.py --mode node` on the VM; `ss -ltnp \| grep 5000` must show `0.0.0.0:5000`. |
| Works in the VM but not from Windows | `sudo ufw allow 5000/tcp`; check the Proxmox firewall (Datacenter / VM → Firewall). |
| *reachable, but no metrics endpoint recognised* | Your node doesn't publish JSON. Use this project's node files (section 4), or set `metrics_path` in `config.py`. `python monitor.py --mode check` shows details. |
| Processes page: *does not publish process information* | Old node agent without `/api/processes` or top processes → update the VM to this node mode. |
| `password authentication failed` | `setx EMSYS_DB_PASSWORD "..."` and open a new terminal. |
| `could not connect` / `connection refused` on 5432 | Start the PostgreSQL service (Windows Services → postgresql-x64-NN). |
| `permission denied to create database` | Create it manually (section 3) or use the `postgres` superuser. |
| `ModuleNotFoundError` | `pip install -r requirements-central.txt` (on the VM: `requirements-node.txt`). The app prints the exact package. |
| No alarm sound | Check the *Audible alarm* switch and Windows volume; put `alarm_loop.wav` next to `monitor.py`. Non-Windows systems use the system bell. |
| Hostname shows "(reports: …)" | The IP in `config.py` answers with a different hostname — IPs of two VMs are swapped. |
| Wrong/odd times | Central mode stores the Windows host's local time; keep the host clock correct. |
| Logs | `logs/vigilant_central.log` (Windows) and `logs/vigilant_node.log` (VM). |
| Discord problems | See section 11.5. |

---

## 11. Discord webhook notifications

The **central monitor** (not the VM agents) posts to a Discord channel when:

| Event | When | Message |
|---|---|---|
| ORANGE threshold | a CPU / RAM / Disk level **rises** to ORANGE | ⚠️ VIGILANT WARNING (orange embed) |
| RED threshold | a level **rises** to RED | 🚨 VIGILANT ALERT (red embed) |
| VM offline | the 3rd consecutive failed health check | 🔴 VIGILANT VM OFFLINE (dark-red embed) |
| VM recovered | first successful check after OFFLINE (with downtime) | 🟢 VIGILANT VM RECOVERED (green embed) |
| Alert resolved | an announced ORANGE/RED alert drops back below Orange | ✅ VIGILANT ALERT RESOLVED (green embed) |

**No spam:** nothing is re-sent while a level stays the same (CPU 86 → 90 → 94 % = one
message). RED → ORANGE is silent. The same VM/metric/level is announced at most once per
`DISCORD_COOLDOWN_SECONDS` (60 s). Offline failures 1 and 2 send nothing, failure 3 sends
OFFLINE, further failures nothing, recovery one RECOVERED. Levels use the same 2 % hysteresis
as the dashboard, so values hovering on a threshold do not flap.

**Never blocks:** messages go through a queue to a background worker thread; the polling
thread and the GUI never wait for Discord. If Discord is down the failure is logged, the
status shows **ERROR**, and monitoring continues normally.

Code: `central/notifications.py` decides what to send (spam protection, worker thread;
channel-agnostic) and `central/discord_notify.py` does the Discord part (embed layout,
colours, HTTP). The engine only calls `notifier.observe_metric(...)` / `notifier.handle_event(...)`.

### 11.1 Create the webhook in Discord
1. In your Discord server: **Server Settings → Integrations → Webhooks → New Webhook**
   (you need the *Manage Webhooks* permission).
2. Name it `VIGILANT`, choose the alert channel, click **Copy Webhook URL**.
   It looks like `https://discord.com/api/webhooks/<id>/<token>`.
3. Treat that URL like a password: anyone who has it can post to your channel.

### 11.2 Give the URL to VIGILANT (Windows environment variable)

**Easiest: paste it in the GUI.** Threshold Settings → *DISCORD NOTIFICATIONS* → **Webhook URL**
field (Ctrl+V or right-click) → **Save URL**. It takes effect immediately (no restart), the field is
masked and emptied after saving, and the URL is stored only as your Windows user environment
variable `VIGILANT_DISCORD_WEBHOOK_URL` (same as the PowerShell method below). **Clear** removes it.

Or from PowerShell:

**Current PowerShell window only** (lost when the window closes):
```powershell
$env:VIGILANT_DISCORD_WEBHOOK_URL = "YOUR_WEBHOOK_URL"
python monitor.py --mode central
```

**Permanently for your Windows user (recommended).** You type the URL at a prompt, so it is
not saved in your PowerShell history:
```powershell
$url = Read-Host "Paste the Discord webhook URL"
[Environment]::SetEnvironmentVariable("VIGILANT_DISCORD_WEBHOOK_URL", $url, "User")
Remove-Variable url
```
Then **close and reopen** PowerShell / VS Code (programs that are already open keep the old
environment). GUI alternative: *Start → "Edit environment variables for your account" →
New… → Name `VIGILANT_DISCORD_WEBHOOK_URL`, Value = the URL*.

Check (prints only whether it is set, never the secret):
```powershell
if ($env:VIGILANT_DISCORD_WEBHOOK_URL) { "set" } else { "NOT set" }
python monitor.py --mode check      # Discord section: "OK webhook URL found ..."
```
Remove it: `[Environment]::SetEnvironmentVariable("VIGILANT_DISCORD_WEBHOOK_URL", $null, "User")`.

**Security rules:** never put the URL in `config.py` or any file in this folder, never commit
it, and never show it on screen during a demo or in a screenshot. VIGILANT never displays it,
logs it or stores it in PostgreSQL. If it leaks: Discord → the webhook → **Delete** (or create
a new one) and set the new URL. Without the variable VIGILANT runs normally and logs
*"Discord notifications disabled"*.

### 11.3 Settings
`config.py` section 6: `DISCORD_NOTIFICATIONS_ENABLED`, `DISCORD_NOTIFY_ORANGE / _RED /
_OFFLINE / _RECOVERY / _RESOLVED`, `DISCORD_COOLDOWN_SECONDS`, `DISCORD_TIMEOUT_SECONDS`.
The **Threshold Settings** page has a *DISCORD NOTIFICATIONS* panel:
the *Discord Notifications ON/OFF* switch, **Webhook Status** (`NOT CONFIGURED` / `READY` /
`CONNECTED` / `ERROR` + reason), the five notification-type checkboxes and
**🧪 Test Discord Notification**. GUI changes apply immediately for the running program;
the start-up defaults come from `config.py`. No test message is sent automatically.

**Dashboard screenshot:** with *Attach dashboard screenshot* ticked (default: `DISCORD_ATTACH_SCREENSHOT = True`),
each message includes a picture of the VIGILANT Dashboard taken at the moment of the alert. Only the VIGILANT
window is captured (Win32 `PrintWindow`), even when other windows cover it; nothing else on your screen is
included. If another page is open, VIGILANT shows the Dashboard for about 0.3 s to take the picture and then
returns. When the window is minimised or the screen is locked, the message is sent without the picture. A burst
of alerts shares one capture. `--mode discord-preview` has no GUI, so its samples have no screenshot.

**History & Alerts:** each delivery is recorded (event `INFO`, severity
`DISCORD_NOTIFICATION` or `DISCORD_NOTIFICATION_FAILED`, with VM / metric / value);
the test button records a `TEST` event. Filter by those severities in History & Alerts.
A Discord failure is never counted as a VM failure. No database schema change was needed.

### 11.4 Testing Discord

| Test | Steps | Expected |
|---|---|---|
| 1. Threshold | Threshold Settings → CPU Yellow 5, Orange 10, Red 15 → *Save*. On a VM: `stress-ng --cpu 0 --timeout 60s` | Dashboard RED, alarm sounds, History `THRESHOLD … CRITICAL`, **one** 🚨 Discord message per affected VM (a VM idling at 10–15 % CPU sends ⚠️ WARNING first). No more messages while it stays RED. When stress ends: ✅ RESOLVED. Then restore CPU 60/70/85, RAM 60/70/85, Disk 60/75/90 (*Reset to defaults*) |
| 2. Offline / recovery | On VM103: `sudo systemctl stop vigilant-node` (or Ctrl+C) | `RETRYING 1/3`, `2/3` (no Discord), then OFFLINE + alarm + History OFFLINE + **one** 🔴 message. `sudo systemctl start vigilant-node` → RECOVERED, alarm clears, History RECOVERED, **one** 🟢 message with the downtime |
| 3. Test button | Threshold Settings → **▶ Test Discord Notification** | One 🧪 VIGILANT TEST message; status **CONNECTED**; History shows a `TEST` event |
| 3b. Preview all designs | `python monitor.py --mode discord-preview` | Six messages labelled **[PREVIEW]** with sample data (test, warning, critical, resolved, offline, recovered) so you can see every layout. Not real alerts; nothing is written to PostgreSQL |
| 4. Spam check | During test 1 leave stress running for 2–3 minutes | Still only the first message (`logs/vigilant_central.log` shows each `Discord notification sent: …`) |
| 5. Discord down | Disconnect the Windows host from the internet, repeat test 2 | Dashboard, alarm and PostgreSQL work as usual; status **ERROR**; History `DISCORD_NOTIFICATION_FAILED` |

### 11.5 Troubleshooting Discord

| Symptom | Fix |
|---|---|
| Status **NOT CONFIGURED** | The variable is not visible to this process. Set it (11.2), open a **new** terminal / restart VS Code, and start VIGILANT from there. |
| **ERROR: … not a valid Discord webhook URL** | Copy the full URL again (must start with `https://discord.com/api/webhooks/`), with no spaces or line breaks. |
| **ERROR: webhook rejected by Discord (HTTP 401/404)** | The webhook was deleted or regenerated in Discord. Copy the new URL and update the variable. |
| **ERROR: cannot reach discord.com** / timeout | No internet, or DNS / proxy / firewall blocks HTTPS (port 443) from the Windows host. The VMs do not need internet. |
| **ERROR: rate limited** | Discord allows about 30 messages/min per webhook; VIGILANT waits and retries automatically. Make sure `DISCORD_COOLDOWN_SECONDS` is not 0. |
| CONNECTED but no message visible | You are looking at a different channel; check which channel the webhook posts to. |
| CRITICAL arrives without a WARNING first | The value jumped past Orange straight to Red within one poll, and only the current level is announced. |
| Messages right after starting monitoring | Conditions that are already ORANGE/RED when a session starts are announced once. That is expected. |
