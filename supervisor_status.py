#!/usr/bin/env python3
"""Query supervisor service status via Unix socket."""

import xmlrpc.client
import socket
import http.client
import os
import time


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


CLK_TCK = os.sysconf("SC_CLK_TCK")
CPU_SAMPLE_INTERVAL = 1.0


def read_proc_stat_ticks(pid):
    """Return utime + stime in ticks for a pid, or None on error."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split()
        return int(fields[13]) + int(fields[14])
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return None


def sample_cpu_ticks(pids):
    return {pid: read_proc_stat_ticks(pid) for pid in pids}


def get_cpu_percent(before, after, interval):
    """Compute cpu% per pid from two tick samples."""
    result = {}
    for pid, t1 in before.items():
        t2 = after.get(pid)
        if t1 is None or t2 is None:
            result[pid] = None
        else:
            delta_ticks = t2 - t1
            result[pid] = (delta_ticks / CLK_TCK) / interval * 100
    return result


def get_memory_kb(pid):
    """Read RSS and VmSwap from /proc/{pid}/status."""
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



def get_supervisor_status(socket_path="/tmp/supervisor.sock"):
    transport = UnixSocketTransport(socket_path)
    server = xmlrpc.client.ServerProxy("http://localhost/RPC2", transport=transport)

    try:
        processes = server.supervisor.getAllProcessInfo()
    except ConnectionRefusedError:
        print(f"Error: Could not connect to supervisor at {socket_path}")
        return
    except FileNotFoundError:
        print(f"Error: Socket file not found: {socket_path}")
        return

    if not processes:
        print("No processes managed by supervisor.")
        return

    with open("/proc/loadavg") as f:
        parts = f.read().split()
    load1, load5, load15 = parts[0], parts[1], parts[2]
    print(f"Load average: {load1} (1m)  {load5} (5m)  {load15} (15m)\n")

    running_pids = [p["pid"] for p in processes if p.get("pid")]
    cpu_before = sample_cpu_ticks(running_pids)
    time.sleep(CPU_SAMPLE_INTERVAL)
    cpu_after = sample_cpu_ticks(running_pids)
    cpu_pct = get_cpu_percent(cpu_before, cpu_after, CPU_SAMPLE_INTERVAL)

    col_widths = {"name": 20, "state": 10, "pid": 8, "uptime": 12, "cpu": 8, "rss": 10, "swap": 10}
    header = (
        f"{'NAME':<{col_widths['name']}}"
        f"{'STATE':<{col_widths['state']}}"
        f"{'PID':<{col_widths['pid']}}"
        f"{'UPTIME':<{col_widths['uptime']}}"
        f"{'CPU%':>{col_widths['cpu']}}"
        f"{'RSS':>{col_widths['rss']}}"
        f"{'SWAP':>{col_widths['swap']}}"
        f"  {'DESCRIPTION'}"
    )
    print(header)
    import shutil
    sep = "-" * shutil.get_terminal_size().columns
    print(sep)

    total_rss = total_swap = 0
    total_cpu = 0.0

    for proc in processes:
        name = proc.get("name", "unknown")
        group = proc.get("group", "")
        full_name = f"{group}:{name}" if group and group != name else name
        state = proc.get("statename", "UNKNOWN")
        pid = proc.get("pid", 0)
        description = proc.get("description", "")

        # Format uptime from 'now' string in description or calculate from start
        now = proc.get("now", 0)
        start = proc.get("start", 0)
        if state == "RUNNING" and start:
            uptime_secs = now - start
            hours, rem = divmod(uptime_secs, 3600)
            mins, secs = divmod(rem, 60)
            uptime = f"{hours}h {mins}m {secs}s"
        else:
            uptime = "-"

        pid_str = str(pid) if pid else "-"
        GREEN, RESET = "\033[32m", "\033[0m"
        ansi_extra = len(GREEN) + len(RESET)
        if state == "RUNNING":
            name_str = f"{GREEN}{full_name}{RESET}"
            name_width = col_widths['name'] + ansi_extra
            state_str = f"{GREEN}{state}{RESET}"
            state_width = col_widths['state'] + ansi_extra
        else:
            name_str = full_name
            name_width = col_widths['name']
            state_str = state
            state_width = col_widths['state']
        rss, swap = get_memory_kb(pid) if pid else (None, None)
        pct = cpu_pct.get(pid)
        cpu_str = f"{pct:.1f}%" if pct is not None else "-"
        total_rss += rss or 0
        total_swap += swap or 0
        total_cpu += pct or 0
        print(
            f"{name_str:<{name_width}}"
            f"{state_str:<{state_width}}"
            f"{pid_str:<{col_widths['pid']}}"
            f"{uptime:<{col_widths['uptime']}}"
            f"{cpu_str:>{col_widths['cpu']}}"
            f"{fmt_kb(rss):>{col_widths['rss']}}"
            f"{fmt_kb(swap):>{col_widths['swap']}}"
            f"  {description}"
        )

    sep = "-" * shutil.get_terminal_size().columns
    print(sep)
    total_cpu_str = f"{total_cpu:.1f}%"
    print(
        f"{'TOTAL':<{col_widths['name']}}"
        f"{'':<{col_widths['state']}}"
        f"{'':<{col_widths['pid']}}"
        f"{'':<{col_widths['uptime']}}"
        f"{total_cpu_str:>{col_widths['cpu']}}"
        f"{fmt_kb(total_rss):>{col_widths['rss']}}"
        f"{fmt_kb(total_swap):>{col_widths['swap']}}"
    )


if __name__ == "__main__":
    import sys

    socket_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/supervisor.sock"
    get_supervisor_status(socket_path)
