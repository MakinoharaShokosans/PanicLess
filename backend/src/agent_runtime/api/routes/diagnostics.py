"""实测遥测与诊断；诊断结果不是基准测试或正式回归套件结果。"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.diagnostics.telemetry import collect

router = APIRouter(tags=["diagnostics"])
Services = Annotated[ApiServices, Depends(services_of)]


@router.get("/system/telemetry")
async def telemetry() -> dict[str, Any]:
    return await asyncio.to_thread(collect)


@router.get("/system/diagnostics")
@router.post("/system/diagnostics/run")
async def diagnostics(svc: Services) -> dict[str, Any]:
    started = time.monotonic()
    checks: list[dict[str, Any]] = []
    tick = time.monotonic()
    try:
        # Exercise the actual application's open database through its public read API.
        async with asyncio.timeout(5):
            await svc.memory.get_app_setting("system_prompt")
        checks.append({"name": "sqlite", "status": "ok"})
    except Exception as exc:
        checks.append({"name": "sqlite", "status": "error", "error": type(exc).__name__})
    checks[-1]["duration_ms"] = round((time.monotonic() - tick) * 1000, 2)
    tick = time.monotonic()
    try:
        async with asyncio.timeout(10):
            dependencies = await svc.dependency_checks()
        for name, status in dependencies.items():
            checks.append({"name": name, "status": status})
        if not dependencies:
            checks.append(
                {
                    "name": "dependencies",
                    "status": "unknown",
                    "reason": "no_dependency_checks_configured",
                }
            )
    except Exception as exc:
        checks.append({"name": "dependencies", "status": "error", "error": type(exc).__name__})
    return {
        "kind": "runtime_health_checks",
        "checked_at": datetime.now(UTC).isoformat(),
        "status": "ok" if all(c["status"] == "ok" for c in checks) else "degraded",
        "checks": checks,
        "passed": sum(c["status"] == "ok" for c in checks),
        "total": len(checks),
        "dependency_checks_duration_ms": round((time.monotonic() - tick) * 1000, 2),
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
    }
