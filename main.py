#!/usr/bin/env python3
"""Supervisor control panel — FastAPI web interface."""

import os
import time
import threading
from collections import deque
from pathlib import Path
import xmlrpc.client
import socket
import http.client

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response

app = FastAPI(title="Supervisor Control Panel")


# ---------------------------------------------------------------------------
# Supervisor / proc helpers
# ---------------------------------------------------------------------------


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        self.sock = sock


class UnixSocketTransport(xmlrpc.client.Transport):
    def __init__(self, socket_path):
        super().__init__()
        self.socket_path = socket_path

    def make_connection(self, host):
        return UnixSocketHTTPConnection(self.socket_path)


clk_tck = os.sysconf("SC_CLK_TCK")
cpu_sample_interval = 1.0
cpu_window = 10.0  # seconds of history used to compute CPU%
supervisor_socket = ""


def read_proc_stat_ticks(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split()
        return int(fields[13]) + int(fields[14])
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return None


def sample_cpu_ticks(pids):
    return {pid: read_proc_stat_ticks(pid) for pid in pids}



def get_memory_kb(pid):
    try:
        rss = vmswap = 0
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                elif line.startswith("VmSwap:"):
                    vmswap = int(line.split()[1])
        return rss, vmswap
    except (FileNotFoundError, PermissionError, ValueError):
        return None, None


def fmt_kb(kb):
    if kb is None:
        return "-"
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:.1f}G"
    if kb >= 1024:
        return f"{kb / 1024:.1f}M"
    return f"{kb}K"


def get_server():
    transport = UnixSocketTransport(supervisor_socket)
    return xmlrpc.client.ServerProxy("http://localhost/RPC2", transport=transport)


# ---------------------------------------------------------------------------
# Background CPU sampler
# ---------------------------------------------------------------------------

cpu_cache: dict[int, float | None] = {}
cpu_lock = threading.Lock()
# deque of (timestamp, {pid: ticks}) snapshots
tick_history: deque[tuple[float, dict[int, int | None]]] = deque()


def cpu_sampler():
    while True:
        try:
            processes = get_server().supervisor.getAllProcessInfo()
            pids = [p["pid"] for p in processes if p.get("pid")]
            now = time.monotonic()
            snapshot = (now, sample_cpu_ticks(pids))

            with cpu_lock:
                tick_history.append(snapshot)
                # drop samples older than the window
                cutoff = now - cpu_window
                while tick_history and tick_history[0][0] < cutoff:
                    tick_history.popleft()

                # compute CPU% between oldest and newest snapshot in window
                if len(tick_history) >= 2:
                    t0, ticks0 = tick_history[0]
                    t1, ticks1 = tick_history[-1]
                    interval = t1 - t0
                    if interval > 0:
                        cpu_cache.clear()
                        for pid in ticks1:
                            v0, v1 = ticks0.get(pid), ticks1.get(pid)
                            if v0 is not None and v1 is not None:
                                cpu_cache[pid] = (v1 - v0) / clk_tck / interval * 100
                            else:
                                cpu_cache[pid] = None
        except Exception:
            pass
        time.sleep(cpu_sample_interval)


def collect_status():
    """Return processes_data or raise an exception."""
    server = get_server()
    processes = server.supervisor.getAllProcessInfo()

    with cpu_lock:
        cpu_pct = dict(cpu_cache)

    rows = []
    for proc in processes:
        name = proc.get("name", "unknown")
        group = proc.get("group", "")
        full_name = f"{group}:{name}" if group and group != name else name
        state = proc.get("statename", "UNKNOWN")

        pid = proc.get("pid", 0)
        description = proc.get("description", "")

        now = proc.get("now", 0)
        start = proc.get("start", 0)
        if state == "RUNNING" and start:
            uptime_secs = now - start
        else:
            uptime_secs = 0

        rss, swap = get_memory_kb(pid) if pid else (None, None)
        pct = cpu_pct.get(pid)
        rows.append(
            {
                "name": name,
                "group": group,
                "full_name": full_name,
                "state": state,
                "pid": str(pid) if pid else "-",
                "cpu": f"{pct:.1f}%" if pct is not None else "-",
                "description": description,
                "rss_bytes": (rss or 0) * 1024,
                "swap_bytes": (swap or 0) * 1024,
                "cpu_raw": pct or 0.0,
                "uptime_seconds": uptime_secs,
            }
        )

    return rows


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/favicon.svg")
async def favicon():
    return Response((Path(__file__).parent / "favicon.svg").read_text(), media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((Path(__file__).parent / "index.html").read_text())


@app.get("/json", name="status_json")
async def status_json():
    try:
        rows = collect_status()
        return {
            "processes": rows,
            "total_cpu": f"{sum(r['cpu_raw'] for r in rows):.1f}%",
            "total_rss_bytes": sum(r["rss_bytes"] for r in rows),
            "total_swap_bytes": sum(r["swap_bytes"] for r in rows),
        }
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)


@app.get("/metrics")
async def metrics():
    try:
        rows = collect_status()
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        return Response(
            f"# supervisor unreachable: {exc}\n",
            media_type="text/plain; version=0.0.4; charset=utf-8",
            status_code=503,
        )

    def prom(name, help_text, mtype, samples):
        lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {mtype}"]
        for labels, value in samples:
            lstr = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"{name}{{{lstr}}} {value}")
        return lines

    out = []
    out += prom(
        "supervisor_process_up",
        "1 if the process is in RUNNING state, 0 otherwise",
        "gauge",
        [
            (
                {"name": r["full_name"], "state": r["state"]},
                1 if r["state"] == "RUNNING" else 0,
            )
            for r in rows
        ],
    )
    out += prom(
        "supervisor_process_cpu_percent",
        "CPU usage of the process in percent",
        "gauge",
        [({"name": r["full_name"]}, r["cpu_raw"]) for r in rows],
    )
    out += prom(
        "supervisor_process_rss_bytes",
        "Resident set size of the process in bytes",
        "gauge",
        [({"name": r["full_name"]}, r["rss_bytes"]) for r in rows],
    )
    out += prom(
        "supervisor_process_swap_bytes",
        "Swap usage of the process in bytes",
        "gauge",
        [({"name": r["full_name"]}, r["swap_bytes"]) for r in rows],
    )
    out += prom(
        "supervisor_process_uptime_seconds",
        "Uptime of the process in seconds",
        "gauge",
        [({"name": r["full_name"]}, r["uptime_seconds"]) for r in rows],
    )

    return Response(
        "\n".join(out) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8"
    )


@app.post("/process/{name}/start", name="process_start")
async def process_start(name: str):
    get_server().supervisor.startProcess(name)


@app.post("/process/{name}/stop", name="process_stop")
async def process_stop(name: str):
    get_server().supervisor.stopProcess(name)


@app.post("/process/{name}/restart", name="process_restart")
async def process_restart(name: str):
    server = get_server()
    server.supervisor.stopProcess(name)
    server.supervisor.startProcess(name)


@app.get("/config", name="config")
async def config_json():
    try:
        configs = get_server().supervisor.getAllConfigInfo()
        result = {}
        for c in configs:
            name = c.get("name", "")
            group = c.get("group", "")
            full_name = f"{group}:{name}" if group and group != name else name
            FIELDS = {
                "command": "command",
                "directory": "directory",
                "user": "uid",
                "autostart": "autostart",
                "startsecs": "startsecs",
                "startretries": "startretries",
                "stopsignal": "stopsignal",
                "stopwaitsecs": "stopwaitsecs",
                "stdout_logfile": "stdout_logfile",
                "stderr_logfile": "stderr_logfile",
                "redirect_stderr": "redirect_stderr",
            }
            result[full_name] = {k: c[v] for k, v in FIELDS.items() if v in c}
        return result
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)


@app.get("/process/{name}/log/{stream}", name="process_log")
async def process_log(name: str, stream: str, length: int = 4096):
    if stream not in ("stdout", "stderr"):
        return JSONResponse(
            {"error": "stream must be stdout or stderr"}, status_code=400
        )
    try:
        server = get_server()
        tail_fn = (
            server.supervisor.tailProcessStdoutLog
            if stream == "stdout"
            else server.supervisor.tailProcessStderrLog
        )
        bytes_, offset, overflow = tail_fn(name, 0, length)
        return {"log": bytes_, "offset": offset, "overflow": overflow}
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/supervisord/reread", name="supervisord_reread")
async def supervisord_reread():
    added, changed, removed = get_server().supervisor.reloadConfig()[0]
    return {"added": added, "changed": changed, "removed": removed}


@app.post("/supervisord/update", name="supervisord_update")
async def supervisord_update():
    server = get_server()
    added, changed, removed = server.supervisor.reloadConfig()[0]
    for name in removed:
        server.supervisor.stopProcessGroup(name)
        server.supervisor.removeProcessGroup(name)
    for name in changed:
        server.supervisor.stopProcessGroup(name)
        server.supervisor.removeProcessGroup(name)
        server.supervisor.addProcessGroup(name)
    for name in added:
        server.supervisor.addProcessGroup(name)
    return {"added": added, "changed": changed, "removed": removed}


# ---------------------------------------------------------------------------
# CLI entry point (uvicorn)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Supervisor Control Panel")
    parser.add_argument(
        "--socket", default="/tmp/supervisor.sock", help="Path to supervisor Unix socket"
    )
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument(
        "--cpu-interval",
        type=float,
        default=cpu_sample_interval,
        help="CPU sampling interval in seconds (default: 0.2)",
    )
    args = parser.parse_args()

    supervisor_socket = args.socket
    cpu_sample_interval = args.cpu_interval
    threading.Thread(target=cpu_sampler, daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port, reload=False)
