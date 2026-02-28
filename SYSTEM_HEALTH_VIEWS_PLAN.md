# System Health Views — Plan

This document describes the full set of system health views for the TAKNET-PS Aggregator dashboard: **detailed live metrics** (like top/htop) and **tracking over time**.

---

## 1. Entry Point

- **Dashboard:** The **System Health** card (CPU / Memory / Disk summary) is clickable for **admin** users and links to **Config → System Health** (`/config/health`).
- **Sidebar:** Under **System**, add **Health** (admin only), same destination.

---

## 2. System Health Page (`/config/health`)

Single page with sections (tabs or stacked cards). Access: **admin** only.

### 2.1 Overview (top)

- **Uptime:** System and app uptime (same as dashboard).
- **Summary bars:** CPU %, Memory (used/total GB, %), Disk (used/total GB, %) with color thresholds (green &lt; 70%, yellow 70–90%, red &gt; 90%).
- **Last updated:** Timestamp of the snapshot.

### 2.2 CPU Detail

- **Per-core usage:** One bar per logical CPU (from `psutil.cpu_percent(percpu=True)`), labeled “Core 0”, “Core 1”, …
- **Top processes by CPU:** Table columns: PID, User, CPU %, MEM %, Command (short). Sortable by CPU (default). Top 20–30 processes. Data from `psutil.Process` iterated over `psutil.pids()` or `psutil.process_iter()` with `one_shot=True` for efficiency.

### 2.3 Memory Detail

- **Breakdown:** Total, Used, Available, Percent; optional: cached, buffers, shared (from `psutil.virtual_memory()`).
- **Top processes by memory:** Table: PID, User, CPU %, MEM %, RSS (MB), Command. Sortable by MEM. Top 20–30.

### 2.4 Disk Detail

- **Per-mount:** Table or list: Mount point, Total GB, Used GB, Free GB, Percent. From `psutil.disk_partitions()` and `psutil.disk_usage(mountpoint)` for each. Focus on “real” mounts (exclude squashfs, tmpfs if desired, or show all).
- **I/O (optional, phase 2):** Read/write bytes per disk from `psutil.disk_io_counters(perdisk=True)` if available.

### 2.5 Processes (top-like)

- **Single sortable table:** PID, User, CPU %, MEM %, RSS (MB), Status, Command (truncated). Sort by CPU or MEM (toggle). Optional: filter by name. Data from same process list as above; show top 50.

### 2.6 History / Trends

- **Time-series:** Store periodic snapshots and show simple charts for:
  - **CPU %** (aggregate) over time
  - **Memory %** over time
  - **Disk %** (root `/`) over time
- **Storage:** In-memory ring buffer (e.g. last 60 points, one sample per minute) in the Flask process. No SQLite schema change for v1. Endpoint: `GET /api/health/history?minutes=60`.
- **UI:** Three small sparklines or line charts (one per metric), X-axis = time (e.g. last 60 minutes), Y-axis = percent. Auto-refresh every 60 seconds when on the health page.

---

## 3. API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/system` | Existing; returns summary (cpu_percent, memory, disk, uptime). Used by dashboard and health overview. |
| GET | `/api/health/detail` | New. Returns: `cpu_per_core`, `memory_breakdown`, `disk_partitions`, `processes` (list of {pid, username, cpu_percent, mem_percent, rss_mb, status, cmdline}). Admin only. |
| GET | `/api/health/history?minutes=60` | New. Returns `{ cpu: [%], memory: [%], disk: [%], timestamps: [iso] }` for the last N minutes from the in-memory buffer. Admin only. |

---

## 4. Data Collection for History

- **Scheduler:** Use the existing APScheduler in the Flask app. Add a job every 60 seconds that appends a snapshot to a global list: `[ { "ts": time.time(), "cpu": x, "memory": y, "disk": z }, ... ]`.
- **Cap:** Keep the last 120 points (e.g. 2 hours at 1/min). Trim when length &gt; 120.
- **Scope:** Snapshot = same fields as `_get_system_info()` (cpu_percent, memory.percent, disk.percent). No process list in history to keep memory small.

---

## 5. Implementation Order

1. **Phase 1 (this PR):**
   - Dashboard System Health card → link to `/config/health` (admin only).
   - Sidebar: add “Health” under System (admin only).
   - Route `GET /config/health` → `config/health.html`.
   - `GET /api/health/detail` with per-CPU, memory breakdown, disk partitions, top processes (CPU + MEM).
   - Health page: Overview, CPU detail (per-core + top processes), Memory detail (breakdown + top processes), Disk (per-mount), Processes table (sortable). All data from one fetch to `/api/health/detail`; refresh every 10–15 seconds.
2. **Phase 2:**
   - In-memory history buffer + scheduler job.
   - `GET /api/health/history`.
   - “History” section on health page with three time-series charts (simple SVG or canvas, or a small chart library).
3. **Phase 3 (optional):**
   - Disk I/O stats.
   - Network I/O (optional).
   - Persist history to SQLite for longer retention (e.g. 24 hours) and restart-safe history.

---

## 6. Security and Performance

- **Admin only:** Both the page and `/api/health/detail`, `/api/health/history` are protected with `@admin_required`. Process list can expose running services and users.
- **Cost:** `process_iter()` with `one_shot=True` and limited count (e.g. top 50) keeps CPU cost low. History is one snapshot per minute.
- **Container:** Dashboard runs inside a container; `psutil` sees the **host** only if the container is not namespaced for PID (e.g. when using host PID mode). By default, Docker containers have their own PID namespace, so the dashboard will see only processes **inside** the container (few processes). For true “host” health (all processes, full CPU), the dashboard would need to run with `pid: host` or collect metrics from the host (e.g. an agent). Document this in the health page: “Metrics are from the dashboard container’s view; for full host metrics run `htop` or `top` on the server.”

---

## 7. Summary

| View | Content |
|------|--------|
| **Overview** | Uptime, CPU/Mem/Disk summary bars, last updated |
| **CPU** | Per-core bars, top processes by CPU |
| **Memory** | Breakdown (used/available/cached), top processes by MEM |
| **Disk** | Per-mount usage (and optional I/O later) |
| **Processes** | Sortable top-50 table (PID, user, CPU%, MEM%, command) |
| **History** | CPU %, Memory %, Disk % over last N minutes (sparklines) |

Entry: Dashboard System Health card (admin) and sidebar **System → Health** (admin).
