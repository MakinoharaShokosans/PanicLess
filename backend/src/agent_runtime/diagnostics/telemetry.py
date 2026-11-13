"""Linux 当前进程可见的真实状态；读取受限时保留未知值，不补样例。"""

from __future__ import annotations

import ipaddress
import os
import platform
import re
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _read(root: Path, name: str) -> str:
    return (root / name).read_text(encoding="utf-8")


def _cpu(root: Path) -> tuple[int, int]:
    first = _read(root, "stat").splitlines()[0].split()
    if first[0] != "cpu" or len(first) < 5:
        raise ValueError("missing CPU counters")
    values = [int(value) for value in first[1:9]]
    # guest counters are already included in user/nice, so exclude them.
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def _disk_counters(root: Path) -> dict[str, tuple[int, int]]:
    result = {}
    for line in _read(root, "diskstats").splitlines():
        parts = line.split()
        if len(parts) >= 14:
            # Linux diskstats sector units are always 512 bytes.
            result[f"{parts[0]}:{parts[1]}"] = (int(parts[5]) * 512, int(parts[9]) * 512)
    return result


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


def disks(root: Path) -> list[dict[str, Any]]:
    result = []
    for line in _read(root, "self/mountinfo").splitlines():
        before, after = line.split(" - ", 1)
        fields, device = before.split(), after.split()
        mount = _unescape_mount(fields[4])
        source = _unescape_mount(device[1])
        # Report root (including container overlay) and real device mounts, not proc/sys/cgroups.
        if mount != "/" and not source.startswith("/dev/"):
            continue
        item: dict[str, Any] = {
            "mount_point": mount,
            "device": source,
            "device_id": fields[2],
            "filesystem": device[0],
            "read_only": "ro" in fields[5].split(","),
            "total_bytes": None,
            "available_bytes": None,
            "used_percent": None,
            "inode_used_percent": None,
            "read_bytes_per_sec": None,
            "write_bytes_per_sec": None,
            "io_status": "unavailable",
            "io_reason": "no_block_device_counters",
        }
        try:
            stat = os.statvfs(mount)
            item.update(
                total_bytes=stat.f_blocks * stat.f_frsize,
                available_bytes=stat.f_bavail * stat.f_frsize,
                used_percent=(
                    round(100 * (stat.f_blocks - stat.f_bfree) / stat.f_blocks, 2)
                    if stat.f_blocks
                    else None
                ),
                inode_used_percent=(
                    round(100 * (stat.f_files - stat.f_ffree) / stat.f_files, 2)
                    if stat.f_files
                    else None
                ),
                status="ok",
            )
        except OSError as exc:
            item.update(status="unavailable", error=type(exc).__name__)
        result.append(item)
    return result


def _address(value: str, ipv6: bool) -> tuple[str, int]:
    raw, port = value.split(":")
    packed = bytes.fromhex(raw)
    if sys.byteorder == "little":
        packed = b"".join(packed[i : i + 4][::-1] for i in range(0, len(packed), 4))
    family = socket.AF_INET6 if ipv6 else socket.AF_INET
    return socket.inet_ntop(family, packed), int(port, 16)


_TCP_STATES = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "03": "SYN_RECV",
    "04": "FIN_WAIT1",
    "05": "FIN_WAIT2",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "09": "LAST_ACK",
    "0A": "LISTEN",
    "0B": "CLOSING",
    "0C": "NEW_SYN_RECV",
}


def network(root: Path, limit: int = 512) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    errors = {}
    total = 0
    for name in ("tcp", "tcp6", "udp", "udp6"):
        try:
            for line in _read(root, f"net/{name}").splitlines()[1:]:
                parts = line.split()
                bind, port = _address(parts[1], name.endswith("6"))
                peer, peer_port = _address(parts[2], name.endswith("6"))
                total += 1
                if len(entries) >= limit:
                    continue
                address = ipaddress.ip_address(bind)
                entries.append(
                    {
                        "id": f"{name}:{parts[9]}:{bind}:{port}:{peer}:{peer_port}",
                        "protocol": "TCP" if name.startswith("tcp") else "UDP",
                        "bind": bind,
                        "port": port,
                        "peer": peer,
                        "peer_port": peer_port,
                        "state": _TCP_STATES.get(parts[3], parts[3])
                        if name.startswith("tcp")
                        else ("CONNECTED" if peer_port else "BOUND"),
                        "binding_scope": "loopback"
                        if address.is_loopback
                        else ("all_interfaces" if address.is_unspecified else "interface"),
                        "socket_inode": parts[9],
                        "owners": [],
                    }
                )
        except (OSError, ValueError, IndexError) as exc:
            errors[name] = type(exc).__name__
    wanted = {item["socket_inode"] for item in entries}
    owners: dict[str, list[dict[str, Any]]] = {}
    denied = 0
    try:
        processes = list(root.iterdir())
    except OSError as exc:
        processes = []
        errors["processes"] = type(exc).__name__
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            comm = (process / "comm").read_text().strip()
            seen = set()
            for fd in (process / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:
                    continue  # Process descriptors may close during the scan.
                if target.startswith("socket:["):
                    inode = target[8:-1]
                    if inode in wanted and inode not in seen:
                        owners.setdefault(inode, []).append(
                            {"pid": int(process.name), "name": comm}
                        )
                        seen.add(inode)
        except PermissionError:
            denied += 1
        except FileNotFoundError:
            continue  # A process exited during the scan.
        except OSError as exc:
            errors["processes"] = type(exc).__name__
    for item in entries:
        item["owners"] = owners.get(item["socket_inode"], [])
        item["owner_status"] = "known" if item["owners"] else "unknown"
    return {
        "status": "partial" if errors or denied else "ok",
        "entries": entries,
        "total": total,
        "truncated": total > limit,
        "owner_permission_denied": denied,
        "errors": errors,
    }


def collect(proc_root: Path = Path("/proc"), sample_seconds: float = 0.1) -> dict[str, Any]:
    errors: dict[str, str] = {}
    result: dict[str, Any] = {
        "scope": "backend_process_namespace",
        "host": {
            "hostname": socket.gethostname(),
            "kernel": platform.release(),
            "system": platform.system(),
            "architecture": platform.machine(),
            "cpu_count": os.cpu_count(),
            "uptime_seconds": None,
            "tainted": None,
        },
        "load_average": None,
        "cpu_used_percent": None,
        "io_pressure": None,
        "disks": None,
        "network": None,
    }
    reads = {
        "uptime_seconds": lambda: float(_read(proc_root, "uptime").split()[0]),
        "tainted": lambda: int(_read(proc_root, "sys/kernel/tainted").strip()),
        "load_average": lambda: [float(v) for v in _read(proc_root, "loadavg").split()[:3]],
        "io_pressure": lambda: _read(proc_root, "pressure/io").strip(),
        "disks": lambda: disks(proc_root),
        "network": lambda: network(proc_root),
    }
    for name, read in reads.items():
        try:
            target = result["host"] if name in {"uptime_seconds", "tainted"} else result
            target[name] = read()
        except (OSError, ValueError, IndexError) as exc:
            errors[name] = type(exc).__name__
    before_cpu = before_disks = None
    try:
        before_cpu = _cpu(proc_root)
    except (OSError, ValueError, IndexError) as exc:
        errors["cpu"] = type(exc).__name__
    try:
        before_disks = _disk_counters(proc_root)
    except (OSError, ValueError, IndexError) as exc:
        errors["disk_io"] = type(exc).__name__
    start = time.monotonic()
    time.sleep(sample_seconds)
    if before_cpu is not None:
        try:
            after_cpu = _cpu(proc_root)
            total = after_cpu[0] - before_cpu[0]
            idle = after_cpu[1] - before_cpu[1]
            if total > 0 and 0 <= idle <= total:
                result["cpu_used_percent"] = round(100 * (1 - idle / total), 2)
            else:
                errors["cpu"] = "no_valid_counter_delta"
        except (OSError, ValueError, IndexError) as exc:
            errors["cpu"] = type(exc).__name__
    if before_disks is not None:
        try:
            after_disks = _disk_counters(proc_root)
            elapsed = time.monotonic() - start
            for item in result["disks"] or []:
                key = item["device_id"]
                if key in before_disks and key in after_disks and elapsed > 0:
                    delta = [
                        b - a for a, b in zip(before_disks[key], after_disks[key], strict=True)
                    ]
                    if min(delta) >= 0:
                        item.update(
                            read_bytes_per_sec=round(delta[0] / elapsed, 2),
                            write_bytes_per_sec=round(delta[1] / elapsed, 2),
                            io_status="ok",
                            io_reason=None,
                        )
        except (OSError, ValueError, IndexError) as exc:
            errors["disk_io"] = type(exc).__name__
    result.update(
        sample_seconds=round(time.monotonic() - start, 4),
        collected_at=datetime.now(UTC).isoformat(),
        errors=errors,
    )
    incomplete = bool(errors) or result["network"] is None or result["network"]["status"] != "ok"
    incomplete |= any(
        item["status"] != "ok" or item["io_status"] != "ok" for item in result["disks"] or []
    )
    result["status"] = "partial" if incomplete else "ok"
    return result
