"""真实数据解析与诊断结果：没有预设端口、健康状态或通过数量。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from agent_runtime.api.deps import services_of
from agent_runtime.api.routes import diagnostics
from agent_runtime.diagnostics import telemetry

pytestmark = pytest.mark.unit


def write(root: Path, name: str, body: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_network_parses_real_socket_rows_and_owners(tmp_path: Path) -> None:
    for name in ("tcp", "tcp6", "udp", "udp6"):
        write(tmp_path, f"net/{name}", "header\n")
    write(tmp_path, "net/tcp", "header\n0: 0100007F:1538 00000000:0000 0A 0 0 0 0 0 123\n")
    write(tmp_path, "net/udp", "header\n0: 00000000:0035 00000000:0000 07 0 0 0 0 0 124\n")
    write(tmp_path, "17/comm", "postgres\n")
    (tmp_path / "17/fd").mkdir()
    (tmp_path / "17/fd/3").symlink_to("socket:[123]")
    data = telemetry.network(tmp_path)
    assert data["total"] == 2 and not data["truncated"]
    tcp, udp = data["entries"]
    assert tcp["bind"] == "127.0.0.1" and tcp["port"] == 5432
    assert tcp["state"] == "LISTEN" and tcp["binding_scope"] == "loopback"
    assert tcp["owners"] == [{"pid": 17, "name": "postgres"}]
    assert udp["port"] == 53 and udp["state"] == "BOUND"
    assert udp["owners"] == [] and udp["owner_status"] == "unknown"
    assert telemetry.network(tmp_path, limit=1)["truncated"]


def test_ipv6_address_and_unknown_tables(tmp_path: Path) -> None:
    assert telemetry._address("00000000000000000000000001000000:0016", True) == ("::1", 22)
    data = telemetry.network(tmp_path)
    assert data["entries"] == [] and data["status"] == "partial"
    assert set(data["errors"]) == {"tcp", "tcp6", "udp", "udp6"}


def test_missing_metrics_remain_unknown(tmp_path: Path) -> None:
    data = telemetry.collect(tmp_path, sample_seconds=0)
    assert data["load_average"] is None and data["cpu_used_percent"] is None
    assert data["disks"] is None and data["host"]["tainted"] is None
    assert data["network"]["entries"] == []
    assert "cpu" in data["errors"] and "disk_io" in data["errors"]


def test_cpu_and_disk_rates_use_counter_deltas(tmp_path: Path, monkeypatch) -> None:
    write(tmp_path, "stat", "cpu 10 0 10 80 0 0 0 0 0 0\n")
    write(tmp_path, "diskstats", "8 0 sda 1 0 10 0 1 0 20 0 0 0 0\n")
    write(tmp_path, "self/mountinfo", "1 0 8:0 / / rw - ext4 /dev/sda rw\n")
    write(tmp_path, "uptime", "123 456\n")
    write(tmp_path, "loadavg", "1.25 2.5 3.75 1/2 3\n")
    write(tmp_path, "sys/kernel/tainted", "0\n")
    write(tmp_path, "pressure/io", "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    monkeypatch.setattr(
        telemetry.os,
        "statvfs",
        lambda _: SimpleNamespace(
            f_blocks=100, f_bfree=40, f_bavail=30, f_frsize=4096, f_files=1000, f_ffree=600
        ),
    )

    def advance(_):
        write(tmp_path, "stat", "cpu 20 0 20 100 0 0 0 0 0 0\n")
        write(tmp_path, "diskstats", "8 0 sda 2 0 12 0 2 0 24 0 0 0 0\n")

    monkeypatch.setattr(telemetry.time, "sleep", advance)
    data = telemetry.collect(tmp_path)
    assert data["cpu_used_percent"] == 50
    assert data["load_average"] == [1.25, 2.5, 3.75]
    assert data["host"]["uptime_seconds"] == 123
    disk = data["disks"][0]
    assert disk["used_percent"] == 60 and disk["inode_used_percent"] == 40
    assert disk["available_bytes"] == 30 * 4096
    assert disk["read_bytes_per_sec"] > 0 and disk["io_status"] == "ok"
    assert disk["write_bytes_per_sec"] == pytest.approx(2 * disk["read_bytes_per_sec"], abs=0.02)


@pytest.mark.parametrize("path", ["/system/diagnostics", "/system/diagnostics/run"])
async def test_diagnostic_http_routes_report_actual_checks(path: str) -> None:
    services = SimpleNamespace(
        memory=SimpleNamespace(get_app_setting=AsyncMock(return_value=None)),
        dependency_checks=AsyncMock(return_value={"bash_shell": "down"}),
    )
    app = FastAPI()
    app.include_router(diagnostics.router)

    async def get_services():
        return services

    app.dependency_overrides[services_of] = get_services
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await (c.post(path) if path.endswith("/run") else c.get(path))
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "runtime_health_checks" and body["status"] == "degraded"
    assert body["passed"] == 1 and body["total"] == 2
    assert body["checks"][1] == {"name": "bash_shell", "status": "down"}
    assert body["checks"][0]["duration_ms"] >= 0
    services.memory.get_app_setting.assert_awaited_once()
    services.dependency_checks.assert_awaited_once()


async def test_failed_and_absent_probes_never_report_success() -> None:
    services = SimpleNamespace(
        memory=SimpleNamespace(get_app_setting=AsyncMock(side_effect=OSError)),
        dependency_checks=AsyncMock(return_value={}),
    )
    result = await diagnostics.diagnostics(services)
    assert result["status"] == "degraded" and result["passed"] == 0
    assert [item["status"] for item in result["checks"]] == ["error", "unknown"]
