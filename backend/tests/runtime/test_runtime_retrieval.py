"""应用级检索生命周期；只注入内存服务，不打开 Chroma 或请求模型。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from agent_runtime.runtime.retrieval import RuntimeRetrieval
from baseagent_config import LoadedSettings
from baseagent_config.models import RetrievalConfig
from tools.retrieval.errors import RetrievalNotConfiguredError, RetrievalServiceError
from tools.retrieval.records import SearchResult

pytestmark = pytest.mark.unit
_TARGET = "agent_runtime.runtime.retrieval"


@pytest.fixture
def setup_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = RetrievalConfig(knowledge_enabled=True)
    source = Mock(return_value=config, control_path=tmp_path / "control.json")
    source_factory = Mock(return_value=source)
    monkeypatch.setattr(f"{_TARGET}.RetrievalConfigSource", source_factory)
    instances = []

    def create(config, **kwargs):
        service = SimpleNamespace(
            config=config,
            last_reload_error=None,
            open=AsyncMock(),
            aclose=AsyncMock(),
            refresh=AsyncMock(),
            search=AsyncMock(return_value=SearchResult("disk", "cfg", None, "not_indexed")),
        )
        instances.append(service)
        return service

    factory = Mock(side_effect=create)
    monkeypatch.setattr(f"{_TARGET}.RetrievalService", factory)
    loaded = cast(
        LoadedSettings,
        SimpleNamespace(
            settings=SimpleNamespace(retrieval=config),
            config_path=tmp_path / "config.toml",
            home=tmp_path,
        ),
    )
    runtime = RuntimeRetrieval(loaded)
    return runtime, source, source_factory, factory, instances


async def test_disabled_configuration_never_opens_store(setup_runtime) -> None:
    runtime, source, _, factory, _ = setup_runtime
    source.return_value = RetrievalConfig()
    await runtime.refresh()
    with pytest.raises(RetrievalNotConfiguredError):
        await runtime.search("disk")
    await runtime.aclose()
    factory.assert_not_called()


async def test_lazy_open_is_shared_and_closed_once(setup_runtime) -> None:
    runtime, _, source_factory, factory, instances = setup_runtime
    factory.assert_not_called()
    await asyncio.gather(
        runtime.search("disk", top_k=2, source_ids=("ops",)),
        runtime.search("network"),
    )
    assert factory.call_count == 1 and source_factory.call_count == 1
    service = instances[0]
    service.open.assert_awaited_once()
    service.search.assert_any_await("disk", top_k=2, source_ids=("ops",))
    await runtime.aclose()
    await runtime.aclose()
    service.aclose.assert_awaited_once()
    with pytest.raises(RetrievalServiceError, match="关闭"):
        await runtime.search("disk")
    assert factory.call_count == 1


async def test_failed_open_can_retry_with_a_new_service(setup_runtime) -> None:
    runtime, _, _, factory, instances = setup_runtime
    create = factory.side_effect

    def fail_once(*args, **kwargs):
        service = create(*args, **kwargs)
        if len(instances) == 1:
            # Real RetrievalService.open owns cleanup of its partial initialization.
            service.open.side_effect = RetrievalServiceError("open failed")
        return service

    factory.side_effect = fail_once
    with pytest.raises(RetrievalServiceError, match="open failed"):
        await runtime.search("disk")
    result = await runtime.search("disk")
    assert result.retrieval_mode == "not_indexed" and len(instances) == 2
    instances[0].search.assert_not_awaited()
    await runtime.aclose()
    instances[1].aclose.assert_awaited_once()


async def test_hot_config_and_invalid_reload_keep_last_valid_value(setup_runtime) -> None:
    runtime, source, _, factory, _ = setup_runtime
    changed = source.return_value.model_copy(update={"default_top_k": 2})
    source.return_value = changed
    await runtime.refresh()
    assert runtime.current_config().default_top_k == 2
    source.side_effect = ValueError("sensitive malformed config")
    await runtime.refresh()
    assert runtime.current_config() == changed
    assert runtime.last_reload_error == "ValueError"
    factory.assert_not_called()
    source.side_effect = None
    await runtime.refresh()
    assert runtime.last_reload_error is None
    await runtime.aclose()


async def test_open_service_owns_refresh_and_current_config(setup_runtime) -> None:
    runtime, _, _, _, instances = setup_runtime
    await runtime.search("disk")
    service = instances[0]
    changed = service.config.model_copy(update={"default_top_k": 2})

    def update():
        service.config = changed
        service.last_reload_error = "invalid later config"

    service.refresh.side_effect = update
    await runtime.refresh()
    assert runtime.current_config() == changed
    assert runtime.last_reload_error == "invalid later config"
    await runtime.aclose()
