#!/usr/bin/env python3
"""Demo Supervisor XML-RPC server over a Unix socket.

Simulates the Supervisor XML-RPC interface so the control panel can be
developed and tested without a real Supervisor installation.

Usage:
    python demo_supervisor.py [--socket /tmp/supervisor.sock]
"""

import argparse
import os
import random
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler
from xmlrpc.server import SimpleXMLRPCDispatcher

# ---------------------------------------------------------------------------
# Fake process definitions
# ---------------------------------------------------------------------------

BOOT_TIME = time.time()

PROCESS_DEFS = [
    {
        "name": "web_01",
        "group": "web",
        "command": "/usr/bin/gunicorn -w 4 -b 0.0.0.0:8000 app:app",
        "directory": "/srv/web",
        "uid": "www-data",
        "autostart": True,
        "startsecs": 2,
        "startretries": 3,
        "stopsignal": "TERM",
        "stopwaitsecs": 10,
        "stdout_logfile": "/var/log/supervisor/web_01.log",
        "stderr_logfile": "/var/log/supervisor/web_01_err.log",
        "redirect_stderr": False,
        "initial_state": "RUNNING",
        "start_offset": 7200,
    },
    {
        "name": "web_02",
        "group": "web",
        "command": "/usr/bin/gunicorn -w 4 -b 0.0.0.0:8001 app:app",
        "directory": "/srv/web",
        "uid": "www-data",
        "autostart": True,
        "startsecs": 2,
        "startretries": 3,
        "stopsignal": "TERM",
        "stopwaitsecs": 10,
        "stdout_logfile": "/var/log/supervisor/web_02.log",
        "stderr_logfile": "/var/log/supervisor/web_02_err.log",
        "redirect_stderr": False,
        "initial_state": "RUNNING",
        "start_offset": 7190,
    },
    {
        "name": "worker",
        "group": "celery",
        "command": "/usr/bin/celery -A myapp worker --loglevel=info",
        "directory": "/srv/web",
        "uid": "deploy",
        "autostart": True,
        "startsecs": 5,
        "startretries": 5,
        "stopsignal": "TERM",
        "stopwaitsecs": 30,
        "stdout_logfile": "/var/log/supervisor/celery_worker.log",
        "stderr_logfile": "/var/log/supervisor/celery_worker_err.log",
        "redirect_stderr": False,
        "initial_state": "RUNNING",
        "start_offset": 3600,
    },
    {
        "name": "beat",
        "group": "celery",
        "command": "/usr/bin/celery -A myapp beat --loglevel=info",
        "directory": "/srv/web",
        "uid": "deploy",
        "autostart": True,
        "startsecs": 3,
        "startretries": 3,
        "stopsignal": "TERM",
        "stopwaitsecs": 10,
        "stdout_logfile": "/var/log/supervisor/celery_beat.log",
        "stderr_logfile": "/var/log/supervisor/celery_beat_err.log",
        "redirect_stderr": False,
        "initial_state": "STOPPED",
        "start_offset": 0,
    },
    {
        "name": "redis",
        "group": "redis",
        "command": "/usr/bin/redis-server /etc/redis/redis.conf",
        "directory": "/",
        "uid": "redis",
        "autostart": True,
        "startsecs": 1,
        "startretries": 3,
        "stopsignal": "TERM",
        "stopwaitsecs": 10,
        "stdout_logfile": "/var/log/supervisor/redis.log",
        "stderr_logfile": "/dev/null",
        "redirect_stderr": True,
        "initial_state": "RUNNING",
        "start_offset": 86400,
    },
    {
        "name": "nginx",
        "group": "nginx",
        "command": "/usr/sbin/nginx -g 'daemon off;'",
        "directory": "/",
        "uid": "root",
        "autostart": True,
        "startsecs": 1,
        "startretries": 3,
        "stopsignal": "QUIT",
        "stopwaitsecs": 5,
        "stdout_logfile": "/var/log/supervisor/nginx.log",
        "stderr_logfile": "/var/log/supervisor/nginx_err.log",
        "redirect_stderr": False,
        "initial_state": "RUNNING",
        "start_offset": 86410,
    },
    {
        "name": "logrotate",
        "group": "logrotate",
        "command": "/usr/sbin/logrotate -f /etc/logrotate.conf",
        "directory": "/",
        "uid": "root",
        "autostart": False,
        "startsecs": 1,
        "startretries": 1,
        "stopsignal": "TERM",
        "stopwaitsecs": 5,
        "stdout_logfile": "/var/log/supervisor/logrotate.log",
        "stderr_logfile": "/var/log/supervisor/logrotate_err.log",
        "redirect_stderr": False,
        "initial_state": "EXITED",
        "start_offset": 0,
    },
]

# State numeric codes (mirrors Supervisor's ProcessStates enum)
STATE_CODE = {
    "STOPPED": 0,
    "STARTING": 10,
    "RUNNING": 20,
    "BACKOFF": 30,
    "STOPPING": 40,
    "EXITED": 100,
    "FATAL": 200,
    "UNKNOWN": 1000,
}

# Fake log lines for stdout / stderr
LOG_LINES_STDOUT = [
    "[INFO] Server started on port {port}",
    "[INFO] Worker process ready (pid={pid})",
    "[DEBUG] Received request GET /health",
    "[INFO] Response 200 OK in 3ms",
    "[INFO] Database connection pool: 5/20 active",
    "[DEBUG] Cache hit ratio: 94.2%",
    "[INFO] Scheduled task completed: cleanup_sessions",
    "[WARNING] Slow query detected (482ms): SELECT * FROM events",
    "[INFO] Graceful reload triggered",
    "[DEBUG] Heartbeat OK",
]
LOG_LINES_STDERR = [
    "[WARNING] Config value 'timeout' not set, using default 30s",
    "[ERROR] Retrying connection to upstream (attempt 1/3)",
    "[WARNING] Memory usage above 80% threshold",
    "[ERROR] Upstream returned 502, retrying",
    "[WARNING] Deprecated config key 'workers' — use 'num_workers'",
]

# ---------------------------------------------------------------------------
# Mutable process state store
# ---------------------------------------------------------------------------

lock = threading.Lock()


def make_state():
    """Build the mutable runtime state dict from definitions."""
    state = {}
    for d in PROCESS_DEFS:
        full = f"{d['group']}:{d['name']}" if d["group"] != d["name"] else d["name"]
        initial = d["initial_state"]
        pid = random.randint(10000, 65000) if initial == "RUNNING" else 0
        start_time = int(BOOT_TIME) - d["start_offset"] if initial == "RUNNING" else 0
        state[full] = {
            "name": d["name"],
            "group": d["group"],
            "statename": initial,
            "pid": pid,
            "start": start_time,
            "defn": d,
        }
    return state


processes = make_state()
next_pid = 70000


def alloc_pid():
    global next_pid
    next_pid += 1
    return next_pid


# ---------------------------------------------------------------------------
# XML-RPC handler implementations
# ---------------------------------------------------------------------------


class SupervisorNamespace:
    """Implements supervisor.* XML-RPC methods."""

    # --- process info -------------------------------------------------------

    def getAllProcessInfo(self):
        now = int(time.time())
        result = []
        with lock:
            for full_name, ps in processes.items():
                d = ps["defn"]
                result.append(
                    {
                        "name": ps["name"],
                        "group": ps["group"],
                        "description": proc_description(ps, now),
                        "start": ps["start"],
                        "stop": 0,
                        "now": now,
                        "state": STATE_CODE.get(ps["statename"], 1000),
                        "statename": ps["statename"],
                        "spawnerr": "",
                        "exitstatus": 0,
                        "logfile": d["stdout_logfile"],
                        "stdout_logfile": d["stdout_logfile"],
                        "stderr_logfile": d["stderr_logfile"],
                        "pid": ps["pid"],
                    }
                )
        return result

    def getAllConfigInfo(self):
        result = []
        with lock:
            for full_name, ps in processes.items():
                d = ps["defn"]
                result.append(
                    {
                        "name": d["name"],
                        "group": d["group"],
                        "command": d["command"],
                        "directory": d["directory"],
                        "uid": d["uid"],
                        "autostart": d["autostart"],
                        "startsecs": d["startsecs"],
                        "startretries": d["startretries"],
                        "stopsignal": d["stopsignal"],
                        "stopwaitsecs": d["stopwaitsecs"],
                        "stdout_logfile": d["stdout_logfile"],
                        "stderr_logfile": d["stderr_logfile"],
                        "redirect_stderr": d["redirect_stderr"],
                    }
                )
        return result

    # --- process control ----------------------------------------------------

    def startProcess(self, name, wait=True):
        with lock:
            ps = find_process(name)
            if ps["statename"] == "RUNNING":
                raise make_fault(60, f"ALREADY_STARTED: {name}")
            ps["statename"] = "RUNNING"
            ps["pid"] = alloc_pid()
            ps["start"] = int(time.time())
        return True

    def stopProcess(self, name, wait=True):
        with lock:
            ps = find_process(name)
            if ps["statename"] in ("STOPPED", "EXITED"):
                raise make_fault(70, f"NOT_RUNNING: {name}")
            ps["statename"] = "STOPPED"
            ps["pid"] = 0
            ps["start"] = 0
        return True

    # --- process group management -------------------------------------------

    def stopProcessGroup(self, name):
        results = []
        with lock:
            for full_name, ps in processes.items():
                if ps["group"] == name and ps["statename"] == "RUNNING":
                    ps["statename"] = "STOPPED"
                    ps["pid"] = 0
                    ps["start"] = 0
                    results.append(
                        {
                            "name": ps["name"],
                            "group": name,
                            "status": 0,
                            "description": "stopped",
                        }
                    )
        return results

    def addProcessGroup(self, name):
        # In this demo all groups already exist; just mark them as added
        return True

    def removeProcessGroup(self, name):
        return True

    # --- config reload -------------------------------------------------------

    def reloadConfig(self):
        # Return [[added, changed, removed]] — demo always returns no changes
        return [["", "", ""]]

    # --- log tailing ---------------------------------------------------------

    def tailProcessStdoutLog(self, name, offset, length):
        return fake_log(name, "stdout", offset, length)

    def tailProcessStderrLog(self, name, offset, length):
        return fake_log(name, "stderr", offset, length)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_process(name):
    """Look up a process by full name, group:name, or bare name. Raises Fault if not found."""
    if name in processes:
        return processes[name]
    for full_name, ps in processes.items():
        if ps["name"] == name:
            return ps
    raise make_fault(10, f"BAD_NAME: {name}")


def make_fault(code, msg):
    import xmlrpc.client

    return xmlrpc.client.Fault(code, msg)


def proc_description(ps, now):
    if ps["statename"] == "RUNNING":
        elapsed = now - ps["start"]
        h, r = divmod(elapsed, 3600)
        m, s = divmod(r, 60)
        return f"pid {ps['pid']}, uptime {h}:{m:02d}:{s:02d}"
    return ps["statename"].lower()


def fake_log(name, stream, offset, length):
    """Return (log_text, new_offset, overflow)."""
    lines = LOG_LINES_STDOUT if stream == "stdout" else LOG_LINES_STDERR
    seed = hash(name + stream) % 1000
    rng = random.Random(seed)
    num_lines = rng.randint(30, 80)
    base_time = time.time() - num_lines * 5

    log_lines = []
    for i in range(num_lines):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base_time + i * 5))
        template = lines[rng.randint(0, len(lines) - 1)]
        line = template.format(
            pid=rng.randint(10000, 65000), port=rng.randint(8000, 9000)
        )
        log_lines.append(f"{ts} {line}")

    full_log = "\n".join(log_lines) + "\n"
    if offset > 0:
        chunk = full_log[offset : offset + length]
        overflow = offset + length < len(full_log)
    else:
        chunk = full_log[-length:] if len(full_log) > length else full_log
        overflow = len(full_log) > length
    return chunk, len(full_log), overflow


# ---------------------------------------------------------------------------
# Unix-socket XML-RPC server
# ---------------------------------------------------------------------------


class XMLRPCRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler that dispatches XML-RPC calls."""

    def address_string(self):
        return "unix"

    def log_message(self, fmt, *args):
        print(f"  {fmt % args}", flush=True)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        response = self.server.dispatcher._marshaled_dispatch(data)
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class UnixSocketXMLRPCServer(socketserver.UnixStreamServer):
    def __init__(self, socket_path):
        self.dispatcher = SimpleXMLRPCDispatcher(allow_none=True, encoding=None)
        ns = SupervisorNamespace()
        self.dispatcher.register_instance(NamespaceDispatcher({"supervisor": ns}))
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        super().__init__(socket_path, XMLRPCRequestHandler)
        print(f"Listening on Unix socket: {socket_path}", flush=True)


class NamespaceDispatcher:
    """Dispatches dotted method names like 'supervisor.getAllProcessInfo'."""

    def __init__(self, namespaces):
        self.ns = namespaces

    def _dispatch(self, method, params):
        parts = method.split(".", 1)
        if len(parts) == 2:
            ns_name, fn_name = parts
            if ns_name in self.ns:
                fn = getattr(self.ns[ns_name], fn_name, None)
                if fn:
                    return fn(*params)
        raise Exception(f"Method not found: {method}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Demo Supervisor XML-RPC server")
    parser.add_argument(
        "--socket",
        default="/tmp/supervisor.sock",
        help="Unix socket path (default: /tmp/supervisor.sock)",
    )
    args = parser.parse_args()

    server = UnixSocketXMLRPCServer(args.socket)
    print(f"Demo supervisor running — {len(processes)} fake processes", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
        if os.path.exists(args.socket):
            os.unlink(args.socket)
