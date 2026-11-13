"""工具执行参数与机房规则。保存后用于下一次任务装配/手工命令。"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.api.deps import ApiServices, services_of
from agent_runtime.api.errors import ApiError
from agent_runtime.runtime.operator_settings import (
    CUSTOM_RULES_KEY,
    TOOL_SETTINGS_KEY,
    OperatorToolSettings,
    load_custom_rules,
    load_tool_settings,
)

router = APIRouter(tags=["settings"])
Services = Annotated[ApiServices, Depends(services_of)]


class CustomRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(max_length=20_000)


@router.get("/settings/tools", response_model=OperatorToolSettings)
async def get_tools(svc: Services) -> OperatorToolSettings:
    return await load_tool_settings(svc.memory, svc.settings.bash_shell)


@router.put("/settings/tools", response_model=OperatorToolSettings)
async def put_tools(body: OperatorToolSettings, svc: Services) -> OperatorToolSettings:
    if body.bash_max_timeout_sec > svc.settings.bash_shell.max_timeout_sec:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            f"Bash 超时不能超过配置上限 {svc.settings.bash_shell.max_timeout_sec} 秒",
        )
    await svc.memory.set_app_setting(TOOL_SETTINGS_KEY, body.model_dump_json())
    return body


@router.get("/settings/rules", response_model=CustomRules)
async def get_rules(svc: Services) -> CustomRules:
    return CustomRules(content=await load_custom_rules(svc.memory))


@router.put("/settings/rules", response_model=CustomRules)
async def put_rules(body: CustomRules, svc: Services) -> CustomRules:
    content = body.content.strip()
    await svc.memory.set_app_setting(CUSTOM_RULES_KEY, content or None)
    return CustomRules(content=content)
