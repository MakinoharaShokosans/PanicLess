"""任务运行器：后台运行、状态映射、并发名额、取消与启动接管。

设计文档：docs/01_architecture/02_task_lifecycle.md、docs/02_runtime/02_main_loop.md §4。
状态目前只存在进程内存里；持久化的任务元数据表属于 API 层的工作（见路线图）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from loguru import logger

from agent_runtime.checkpoint import RecoveryReport, StoredStatus, mark_cancelled
from agent_runtime.checkpoint.tool_journal import ToolJournal
from agent_runtime.core import (
    AgentState,
    ApprovalDecision,
    Message,
    ToolResult,
    apply_update,
    current_task_id,
    unanswered_calls,
)
from agent_runtime.loop import TaskOutcome, TaskRuntime, run
from agent_runtime.runtime.active import ActiveTasks
from agent_runtime.runtime.errors import (
    TaskAlreadyExistsError,
    TaskAlreadyRunningError,
    TaskNotFoundError,
    TaskQueueFullError,
)
from agent_runtime.runtime.resume import ResumeDeps, prepare_resume
from agent_runtime.runtime.run import RunnerDeps, TaskRequest, create_task

TaskStatus = Literal[
    "running",
    "waiting_for_approval",
    "succeeded",
    "partial",
    "needs_input",
    "blocked",
    "terminated",
    "failed",
    "cancelled",
]
TERMINAL: frozenset[TaskStatus] = frozenset(
    {"succeeded", "partial", "needs_input", "blocked", "terminated", "failed", "cancelled"}
)


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: str
    status: TaskStatus
    #: 终止/失败/取消的原因；其余为空
    reason: str = ""
    #: 最近一次已知的状态（运行中为 None）
    state: AgentState | None = None


class TaskObserver(Protocol):
    """任务生命周期通知。``task_finished`` 即“收尾”：它出错不会改变任务结果。"""

    async def task_started(self, task_id: str, state: AgentState) -> None: ...

    async def task_finished(self, snapshot: TaskSnapshot) -> None: ...


class _Record:
    def __init__(self, status: TaskStatus, reason: str = "", state: AgentState | None = None):
        self.status: TaskStatus = status
        self.reason = reason
        self.state = state
        self.task: asyncio.Task[None] | None = None
        self.shutting_down = False
        self.session_id = state.session_id if state is not None else ""
        self.pause_requested = False
        self.paused = asyncio.Event()
        self.continue_run = asyncio.Event()
        self.continue_run.set()
        self.inbox_lock = asyncio.Lock()

    def snapshot(self, task_id: str) -> TaskSnapshot:
        return TaskSnapshot(task_id, self.status, self.reason, self.state)


def _new_task_id() -> str:
    return "task_" + uuid.uuid4().hex[:12]


class TaskRunner:
    """同一事件循环线程内使用。``max_concurrent`` 只统计 ``running`` 的任务：
    挂起等待审批的任务释放执行资源，不占名额。"""

    def __init__(
        self,
        deps: RunnerDeps,
        *,
        max_concurrent: int = 1,
        observer: TaskObserver | None = None,
        new_task_id: Callable[[], str] = _new_task_id,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent 必须至少为 1")
        self._deps = deps
        self._max = max_concurrent
        self._observer = observer
        self._new_id = new_task_id
        self._active = ActiveTasks()
        self._resume_deps = ResumeDeps(
            checkpoints=deps.checkpoints,
            build_runtime=deps.build_runtime,
            is_idempotent=deps.is_idempotent,
            active=self._active,
        )
        self._records: dict[str, _Record] = {}
        self._manual_sessions: set[str] = set()

    def attach_observer(self, observer: TaskObserver) -> None:
        """生命周期观察者依赖运行器本身（任务服务），所以允许装配后再接上，且只能接一次。"""
        if self._observer is not None:
            raise RuntimeError("观察者已经接上")
        self._observer = observer

    # ------------------------------------------------------------------ 查询

    def snapshot(self, task_id: str) -> TaskSnapshot:
        return self._record(task_id).snapshot(task_id)

    def list_tasks(self) -> list[TaskSnapshot]:
        return [r.snapshot(tid) for tid, r in self._records.items()]

    def _record(self, task_id: str) -> _Record:
        record = self._records.get(task_id)
        if record is None:
            raise TaskNotFoundError(f"没有任务 {task_id}")
        return record

    def _running(self) -> int:
        return sum(1 for r in self._records.values() if r.status == "running")

    async def wait(self, task_id: str) -> TaskSnapshot:
        """等到该任务不再运行（终态或挂起）。"""
        task = self._record(task_id).task
        if task is not None:
            await asyncio.wait([task])
        return self.snapshot(task_id)

    async def wait_all(self) -> None:
        while pending := [r.task for r in self._records.values() if r.task and not r.task.done()]:
            await asyncio.wait(pending)

    def control_state(self, task_id: str) -> dict[str, str]:
        record = self._record(task_id)
        mode = "inactive"
        if record.status == "running":
            mode = (
                "paused"
                if record.paused.is_set()
                else "pause_requested"
                if record.pause_requested
                else "autonomous"
            )
        return {"task_id": task_id, "status": record.status, "control_state": mode}

    def control(self, task_id: str, action: Literal["pause", "resume"]) -> dict[str, str]:
        """协作式暂停：当前步骤结束后停在下一次模型/工具调用之前。"""
        record = self._record(task_id)
        if record.status != "running":
            raise TaskAlreadyRunningError("只有正在运行的任务可以暂停或解除暂停")
        if action == "pause":
            record.pause_requested = True
            record.continue_run.clear()
        else:
            self._check_manual_session(record.session_id)
            record.pause_requested = False
            record.paused.clear()
            record.continue_run.set()
        return self.control_state(task_id)

    async def send_message(self, task_id: str, content: str) -> dict[str, object]:
        record = self._record(task_id)
        async with record.inbox_lock:
            if record.status not in {"running", "waiting_for_approval"}:
                raise TaskAlreadyRunningError("只有运行中或等待审批的任务可以接收补充消息")
            return await self._deps.checkpoints.enqueue_message(task_id, content)

    async def _merge_inbox(self, state: AgentState) -> AgentState:
        messages = await self._deps.checkpoints.inbox(state.task_id, after=state.inbox_cursor)
        if not messages:
            return state
        merged = apply_update(
            state,
            {
                "messages": [
                    Message(role="user", content=str(item["content"])) for item in messages
                ],
                "inbox_cursor": messages[-1]["message_id"],
                "conclusion": None,
            },
        )
        await self._deps.checkpoints.commit(merged)
        return merged

    def _check_manual_session(self, session_id: str) -> None:
        if session_id in self._manual_sessions:
            raise TaskAlreadyRunningError("该会话正在执行人工操作，请等待操作结束")

    def acquire_manual_session(self, session_id: str) -> None:
        """同一事件循环内同步检查并占用，终端/回滚与 Agent 不同时修改会话现场。"""
        self._check_manual_session(session_id)
        for record in self._records.values():
            if (
                record.session_id == session_id
                and record.status == "running"
                and not record.paused.is_set()
            ):
                raise TaskAlreadyRunningError("请等待该会话的 Agent 实际暂停或结束后再操作")
        self._manual_sessions.add(session_id)

    def release_manual_session(self, session_id: str) -> None:
        self._manual_sessions.discard(session_id)

    # ------------------------------------------------------------------ 提交与续跑

    def new_task_id(self) -> str:
        return self._new_id()

    async def submit(self, request: TaskRequest, task_id: str | None = None) -> str:
        """登记并在后台运行，立即返回 task_id。名额满则 :class:`TaskQueueFullError`，不留痕迹。

        ``task_id`` 由调用方预先分配时（要先写元数据再运行），重复使用已登记的 id 会被拒绝。
        """
        self._check_manual_session(request.session_id)
        if self._running() >= self._max:
            raise TaskQueueFullError(f"并发任务已满（上限 {self._max}）")
        task_id = task_id or self._new_id()
        if task_id in self._records:
            raise TaskAlreadyExistsError(f"任务 {task_id} 已存在，不会覆盖")
        record = _Record("running")
        record.session_id = request.session_id
        self._records[task_id] = record  # 与上面的检查之间没有 await，名额不会被并发提交抢占
        try:
            state = await create_task(self._deps, request, task_id)
        except BaseException:
            del self._records[task_id]
            raise
        self._active.try_acquire(task_id)
        self._spawn(task_id, record, state)
        return task_id

    async def resume(
        self,
        task_id: str,
        decision: ApprovalDecision | Mapping[str, object] | None = None,
    ) -> None:
        """续跑。校验错误同步抛给调用方，任务保持原状；通过后转入后台运行。"""
        record = await self._record_for_resume(task_id)
        self._check_manual_session(record.session_id)
        if not self._active.try_acquire(task_id):
            raise TaskAlreadyRunningError(f"任务 {task_id} 已在运行")
        try:
            if self._running() >= self._max:
                raise TaskQueueFullError(f"并发任务已满（上限 {self._max}）")
            state = await prepare_resume(self._resume_deps, task_id, decision)
            self._check_manual_session(state.session_id)
        except BaseException:
            self._active.release(task_id)
            raise
        record.status, record.reason = "running", ""
        record.pause_requested = False
        record.paused.clear()
        record.continue_run.set()
        self._spawn(task_id, record, state)

    async def _record_for_resume(self, task_id: str) -> _Record:
        if task_id in self._records:
            return self._records[task_id]
        stored = await self._deps.checkpoints.load(task_id)
        meta = await self._deps.checkpoints.load_status(task_id)
        if stored is None or meta is None:
            raise TaskNotFoundError(f"没有任务 {task_id}")
        record = _Record(_status_of_stored(meta), meta.reason, stored)
        self._records[task_id] = record
        return record

    def _spawn(self, task_id: str, record: _Record, state: AgentState) -> None:
        record.status, record.state = "running", None
        record.session_id = state.session_id
        record.task = asyncio.create_task(self._run(task_id, record, state), name=task_id)

    def _controlled(self, runtime: TaskRuntime, record: _Record) -> TaskRuntime:
        async def boundary() -> None:
            while record.pause_requested:
                record.paused.set()
                try:
                    await record.continue_run.wait()
                finally:
                    record.paused.clear()

        async def before_model(state: AgentState) -> AgentState:
            await boundary()
            if runtime.before_model is not None:
                state = await runtime.before_model(state)
            return await self._merge_inbox(state)

        async def tools(state: AgentState, calls):
            await boundary()
            return await runtime.tools(state, calls)

        # 压缩也可能调用模型；暂停后不在后台继续发出压缩请求。
        async def compact(state: AgentState, force: bool):
            await boundary()
            assert runtime.compact is not None
            return await runtime.compact(state, force)

        return replace(
            runtime,
            tools=tools,
            compact=compact if runtime.compact else None,
            before_model=before_model,
        )

    # ------------------------------------------------------------------ 后台运行

    async def _run(self, task_id: str, record: _Record, state: AgentState) -> None:
        current_task_id.set(task_id)  # 网关的调用记录等旁路据此知道属于哪个任务
        try:
            if self._observer is not None:
                await _swallow(self._observer.task_started(task_id, state))
            runtime = await self._deps.build_runtime(state)
            while True:
                outcome = await run(self._controlled(runtime, record), state)
                # 与消息入队互斥：已接收的消息不能落在最终回复之后而永久未处理。
                async with record.inbox_lock:
                    if outcome.status not in {"waiting_for_approval", "terminated"}:
                        merged = await self._merge_inbox(outcome.state)
                        if merged is not outcome.state:
                            state = merged
                            continue
                    await self._settle(task_id, record, outcome)
                    break
        except asyncio.CancelledError:
            if record.shutting_down:
                record.status, record.reason = "failed", "服务关闭"
            raise  # 用户取消由 cancel() 收尾
        except Exception as exc:
            logger.exception("任务 {} 失败", task_id)
            await self._finish(task_id, record, "failed", f"{type(exc).__name__}: {exc}", None)
        finally:
            self._active.release(task_id)

    async def _settle(self, task_id: str, record: _Record, outcome: TaskOutcome) -> None:
        match outcome.status:
            case "waiting_for_approval":
                record.status, record.reason, record.state = (
                    "waiting_for_approval",
                    "",
                    outcome.state,
                )
                hook = getattr(self._observer, "task_waiting", None)
                if hook is not None:  # 可选通知：只有关心挂起的观察者实现它
                    await _swallow(hook(task_id, outcome.state))
            case "terminated":
                reason = outcome.termination_reason
                await self._deps.checkpoints.commit(
                    outcome.state, outcome="terminated", reason=reason
                )
                await self._finish(task_id, record, "terminated", reason, outcome.state)
            case "succeeded" | "partial" | "needs_input" | "blocked":
                status = outcome.status
                await self._deps.checkpoints.commit(outcome.state, outcome=status)
                await self._finish(task_id, record, status, "", outcome.state)

    async def _finish(
        self,
        task_id: str,
        record: _Record,
        status: TaskStatus,
        reason: str,
        state: AgentState | None,
    ) -> None:
        record.status, record.reason, record.state = status, reason, state
        if self._observer is not None:
            await _swallow(self._observer.task_finished(record.snapshot(task_id)))

    # ------------------------------------------------------------------ 取消、关停、接管

    async def cancel(self, task_id: str) -> bool:
        """用户取消。运行中的任务被中断，挂起的任务作废其请求；两者都把“用户取消”的终态
        落盘（补齐未配对的工具调用），重启后不会被当成中断或等待审批。已结束的任务返回 False。"""
        record = self._record(task_id)
        if record.status in TERMINAL:
            return False
        if record.task is not None and not record.task.done():
            record.task.cancel()
            await asyncio.wait([record.task])
        stored = await self._deps.checkpoints.load(task_id)
        final = stored
        if stored is not None:
            journal = ToolJournal(self._deps.checkpoints.journal_path)
            turn = sum(m.role == "assistant" for m in stored.messages)
            results: dict[str, ToolResult | None] = {}
            for call in unanswered_calls(stored.messages):
                status, result, _ = journal.load(task_id, turn, call.id)
                if status != "queued":
                    results[call.id] = result
            final = mark_cancelled(stored, results)
            await self._deps.checkpoints.commit(final, outcome="cancelled", reason="用户取消")
        await self._finish(task_id, record, "cancelled", "用户取消", final)
        return True

    def discard(self, task_id: str) -> bool:
        """忘掉一个已结束或挂起的任务（删除任务时用）。运行中的任务不能忘，返回 False。"""
        record = self._records.get(task_id)
        if record is None:
            return True
        if record.status == "running":
            return False
        del self._records[task_id]
        return True

    async def shutdown(self) -> None:
        """停止所有运行中的任务但不写终态：状态停在上一次提交点，下次启动可续跑。"""
        tasks = []
        for record in self._records.values():
            if record.task is not None and not record.task.done():
                record.shutting_down = True
                record.task.cancel()
                tasks.append(record.task)
        if tasks:
            await asyncio.wait(tasks)

    async def adopt(self, report: RecoveryReport) -> None:
        """启动时接管上次进程留下的任务：中断的记为失败（等用户显式续跑，不自动续跑），
        挂起的恢复为等待审批。"""
        for task_id in report.interrupted:
            self._records.setdefault(task_id, _Record("failed", "进程中断"))
        for task_id in report.waiting_for_approval:
            self._records.setdefault(task_id, _Record("waiting_for_approval"))


def _status_of_stored(meta: StoredStatus) -> TaskStatus:
    if meta.status == "waiting":
        return "waiting_for_approval"
    if meta.status == "finished":
        return meta.outcome or "succeeded"  # type: ignore[return-value]
    return "failed"


async def _swallow(awaitable: Awaitable[None]) -> None:
    try:
        await awaitable
    except Exception:
        logger.exception("任务生命周期通知失败；不影响任务结果")
