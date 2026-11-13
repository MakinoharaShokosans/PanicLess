"""操作界面的持久化配置；复用 app_settings，任务启动时读取一次。"""

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.memory import MemoryStore
from baseagent_config.models import BashShellConfig

TOOL_SETTINGS_KEY = "operator.tools"
CUSTOM_RULES_KEY = "operator.rules"


class OperatorToolSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    bash_max_timeout_sec: int = Field(ge=1)
    bash_output_max_bytes: int = Field(ge=256, le=1_048_576)


async def load_tool_settings(store: MemoryStore, config: BashShellConfig) -> OperatorToolSettings:
    raw = await store.get_app_setting(TOOL_SETTINGS_KEY)
    value = (
        OperatorToolSettings.model_validate_json(raw)
        if raw
        else OperatorToolSettings(
            bash_max_timeout_sec=config.max_timeout_sec,
            bash_output_max_bytes=config.output_max_bytes,
        )
    )
    # 配置文件的硬上限可能在重启后降低，已有界面覆盖不能突破新上限。
    return value.model_copy(
        update={"bash_max_timeout_sec": min(value.bash_max_timeout_sec, config.max_timeout_sec)}
    )


async def load_custom_rules(store: MemoryStore) -> str:
    return await store.get_app_setting(CUSTOM_RULES_KEY) or ""


def append_custom_rules(system: str, rules: str) -> str:
    return f"{system}\n\n## 用户配置的机房规则\n{rules}" if rules else system
