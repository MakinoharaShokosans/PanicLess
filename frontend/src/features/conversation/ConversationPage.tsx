/** 对话页：消息流 + 输入台 + 右侧检视器。 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams, useSearch } from '@tanstack/react-router';
import {
  ArrowDown,
  BookOpen,
  Boxes,
  ChevronRight,
  HardDrive,
  Menu,
  PanelRight,
  Radio,
  SquareTerminal,
  WifiOff,
  Zap,
} from 'lucide-react';
import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api/endpoints';
import {
  hasSessionMeta,
  sessionTitle,
  useSessionTasks,
  useSessions,
} from '../../api/queries';
import { useContextSnapshot, useSessionSettings } from '../../api/useSettings';
import { SafeMarkdown } from '../../components/common/SafeMarkdown';
import { StarLogo } from '../../components/common/BrandLogo';
import { McpStatus } from '../../components/domain/McpStatus';
import { SrePermissionGuard } from '../../components/domain/SreProductionGuard';
import { SreStorageGaugeStrip } from '../../components/domain/SreNetworkStorageDial';
import { InlineEdit } from '../../components/ui/InlineEdit';
import { IconButton } from '../../components/ui/primitives';
import { Tip } from '../../components/ui/Tooltip';
import { useAppStore } from '../../stores/useAppStore';
import { queueOf, useQueueStore } from '../../stores/useQueueStore';
import { useUiStore } from '../../stores/useUiStore';
import { useCockpitStore } from '../../stores/useCockpitStore';
import { TERMINAL_STATUSES, type PermissionLevel, type Task, type Turn } from '../../types/api';
import { rememberLocation } from '../../utils/lastLocation';
import {
  CONNECTION_LABEL,
  MinimalStatusMark,
  connectionHealthy,
  statusMeta,
} from '../../utils/status';
import { cn } from '../../utils/text';
import { Inspector } from '../inspector/Inspector';
import { Composer } from './Composer';
import { ConversationTimelineRail, type DialogueNode } from './ConversationTimelineRail';
import { RunView } from './RunView';
import { SreQuickCards } from './SreQuickCards';
import { useReplay } from './useReplay';

function UserMessage({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[88%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-ink/[0.06] px-3.5 py-2 text-[14.5px] leading-relaxed">
        {text}
      </div>
    </div>
  );
}

function PastTask({ task, turn }: { task: Task; turn: Turn | undefined }) {
  const [open, setOpen] = useState(false);
  const replay = useReplay(task.task_id, task.task_goal, open, task.status);
  const meta = statusMeta(task.status);
  return (
    <div className="space-y-2">
      {open && replay ? (
        <RunView
          run={{
            ...replay,
            delivery: replay.delivery || turn?.content || '',
            conclusion: replay.conclusion ?? task.conclusion ?? null,
            verificationRecords:
              replay.verificationRecords && replay.verificationRecords.length > 0
                ? replay.verificationRecords
                : task.verification_records ?? [],
          }}
          live={false}
        />
      ) : (
        turn?.content && (
          <div className="py-1">
            <SafeMarkdown text={turn.content} />
          </div>
        )
      )}
      <div className="flex items-center gap-1.5 border-t border-ink/[0.06] pt-2 text-[12px] text-mute dark:border-white/[0.06]">
        <button
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex items-center gap-1.5 transition-colors hover:text-ink"
        >
          <ChevronRight size={12} className={cn('transition-transform', open && 'rotate-90')} />
          <span className="underline decoration-ink/15 decoration-dashed underline-offset-2 hover:decoration-ink/40 dark:decoration-white/20 dark:hover:decoration-white/50">
            {open ? '收起过程' : '过程'}
          </span>
          {task.status !== 'succeeded' && (
            <span
              className={cn(
                meta.tone === 'amber'
                  ? 'text-warn'
                  : meta.tone === 'blue'
                    ? 'text-accent'
                    : meta.tone === 'slate'
                      ? 'text-mute'
                      : 'text-bad',
              )}
            >
              {meta.label}
            </span>
          )}
          <span className="tabular-nums">{task.step_count} 回合</span>
        </button>
      </div>
    </div>
  );
}

export function ConversationPage() {
  const { workspaceId = '', sessionId = '' } = useParams({ strict: false }) as {
    workspaceId?: string;
    sessionId?: string;
  };
  const client = useQueryClient();
  const search = useSearch({ strict: false }) as { task?: string };
  const sessions = useSessions();
  const tasks = useSessionTasks(sessionId, 50);
  const turns = useQuery({
    queryKey: ['turns', sessionId],
    queryFn: () => api.sessions.turns(sessionId, 200),
    enabled: !!sessionId,
  });
  const snapshot = useContextSnapshot(sessionId);
  const { query: settings } = useSessionSettings(sessionId);

  const run = useAppStore((s) => (s.runSession === sessionId ? s.run : null));
  const runSession = useAppStore((s) => s.runSession);
  const connection = useAppStore((s) => s.connection);
  const submit = useAppStore((s) => s.submit);
  const attach = useAppStore((s) => s.attach);
  const detach = useAppStore((s) => s.detach);
  const cancel = useAppStore((s) => s.cancel);
  const notify = useAppStore((s) => s.notify);

  const phone = useUiStore((s) => s.phone);
  const narrow = useUiStore((s) => s.narrow);
  const setNavOpen = useUiStore((s) => s.setNavOpen);
  const inspectorOpen = useUiStore((s) => s.inspectorOpen);
  const drawerOpen = useUiStore((s) => s.drawerOpen);
  const toggleInspector = useUiStore((s) => s.toggleInspector);
  const markSessionRead = useUiStore((s) => s.markSessionRead);

  const terminalOpen = useCockpitStore((s) => s.terminalOpen);
  const toggleTerminal = useCockpitStore((s) => s.toggleTerminal);
  const openArtifact = useCockpitStore((s) => s.openArtifact);
  const rightDrawerView = useCockpitStore((s) => s.rightDrawerView);
  const setRightDrawerView = useCockpitStore((s) => s.setRightDrawerView);
  const inspectorWide = useCockpitStore((s) => s.inspectorWide);
  const toggleInspectorWide = useCockpitStore((s) => s.toggleInspectorWide);
  const drawerWidths = useCockpitStore((s) => s.drawerWidths);
  const setDrawerWidth = useCockpitStore((s) => s.setDrawerWidth);

  const queue = useQueueStore((s) => queueOf(s, sessionId));
  const session = sessions.data?.find((s) => s.session_id === sessionId);
  const ordered = useMemo(() => [...(tasks.data ?? [])].reverse(), [tasks.data]);
  const latest = tasks.data?.[0];
  const terminal = !!run && TERMINAL_STATUSES.includes(run.status);
  const busy = !!run && !terminal;
  const [renaming, setRenaming] = useState(false);

  // 切换会话：停掉旧订阅；会话变了就清掉旧任务视图
  useEffect(() => {
    if (useAppStore.getState().runSession !== sessionId) detach();
  }, [sessionId, detach]);

  // 会话独立权限隔离：激活当前会话的独立权限状态，离开时重置为安全默认
  useEffect(() => {
    if (sessionId) {
      useCockpitStore.getState().setActiveSessionId(sessionId);
    }
    return () => {
      useCockpitStore.getState().setActiveSessionId(null);
    };
  }, [sessionId]);

  // 若当前会话尚未手动设定权限，且存在历史最新任务，则自动继承该会话历史任务的权限基线
  useEffect(() => {
    if (!sessionId || !latest?.permission_level) return;
    useCockpitStore.getState().initSessionPermission(sessionId, latest.permission_level);
  }, [sessionId, latest?.permission_level]);

  // 记住位置，供根路径返回
  useEffect(() => {
    if (sessionId) rememberLocation(sessionId, workspaceId);
  }, [workspaceId, sessionId]);

  // ?task=:id：深链接定位到某个历史任务；只解析一次，之后不再覆盖用户的操作
  const deepLinked = useRef<string | null>(null);
  useEffect(() => {
    const wanted = search.task;
    if (!wanted || deepLinked.current === wanted) return;
    deepLinked.current = wanted;
    const local = tasks.data?.find((t) => t.task_id === wanted);
    if (local) {
      void attach(sessionId, local);
      return;
    }
    void api.tasks
      .get(wanted)
      .then((task) => {
        if (task.session_id !== sessionId) {
          notify('该任务不属于这个会话', 'error');
          return;
        }
        return attach(sessionId, task);
      })
      .catch((e: Error) => notify(e.message, 'error'));
  }, [search.task, sessionId, tasks.data, attach, notify]);

  // 打开会话时接上最近的任务（进行中的继续订阅，已结束的回放事件库）
  useEffect(() => {
    const waitingForLink = !!search.task && run?.taskId !== search.task;
    if (!latest || runSession === sessionId || waitingForLink) return;
    void attach(sessionId, latest);
  }, [latest, sessionId, runSession, search.task, run?.taskId, attach]);

  // 刷新后遗留的队列不自动继续：没有前端就没有调度者
  useEffect(() => {
    const q = queueOf(useQueueStore.getState(), sessionId);
    if (q.items.length && !q.paused) useQueueStore.getState().setPaused(sessionId, true);
  }, [sessionId]);

  // 任务结束：刷新依赖它的查询
  useEffect(() => {
    if (!terminal) return;
    for (const key of ['tasks', 'turns', 'activity', 'context', 'smemory', 'sessions'])
      void client.invalidateQueries({ queryKey: [key] });
  }, [terminal, client]);

  // 队列调度
  const submitting = useRef(false);
  const handled = useRef(new Set<string>());
  useEffect(() => {
    const q = queueOf(useQueueStore.getState(), sessionId);
    if (!q.items.length || submitting.current) return;
    const idle = !run || terminal;
    if (!idle) return;
    if (run && terminal && run.status !== 'succeeded') {
      if (!q.paused && !handled.current.has(run.taskId)) {
        handled.current.add(run.taskId);
        useQueueStore.getState().setPaused(sessionId, true);
      }
      return;
    }
    if (q.paused) return;
    const message = useQueueStore.getState().shift(sessionId);
    if (!message) return;
    submitting.current = true;
    void submit(sessionId, message.text, message.level, message.level === 'full_permissions')
      .then((task) => {
        if (!task) {
          useQueueStore.getState().unshift(sessionId, message);
          useQueueStore.getState().setPaused(sessionId, true);
        } else void client.invalidateQueries({ queryKey: ['tasks'] });
      })
      .finally(() => {
        submitting.current = false;
      });
  }, [run, terminal, queue, sessionId, submit, client]);

  // 首个任务成功后，由对话所选模型生成会话标题（只试一次）
  const titled = useRef(new Set<string>());
  useEffect(() => {
    if (
      run?.status !== 'succeeded' ||
      !session ||
      !hasSessionMeta(session) ||
      session.title ||
      titled.current.has(sessionId)
    )
      return;
    if ((tasks.data?.length ?? 0) !== 1) return;
    titled.current.add(sessionId);
    api.sessions
      .generateTitle(sessionId)
      .then(() => client.invalidateQueries({ queryKey: ['sessions'] }))
      .catch(() => undefined);
  }, [run?.status, session, sessionId, tasks.data, workspaceId, client]);

  // 滚动：位于底部时跟随新内容，向上翻阅则停止并出现“回到最新”
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [away, setAway] = useState(false);
  // 滚动跟随：文本增量与命令实时输出都算“新内容”
  const tail = run?.items.at(-1);
  const tailSize =
    tail && 'text' in tail
      ? tail.text.length
      : tail && 'stdout' in tail
        ? tail.stdout.length + tail.stderr.length
        : 0;
  const tick = (run?.items.length ?? 0) + tailSize;
  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) {
      el.scrollTop = el.scrollHeight;
      markSessionRead(sessionId);
    }
  }, [sessionId, tick, run?.status, ordered.length, markSessionRead]);
  useEffect(() => {
    stick.current = true;
    setAway(false);
    markSessionRead(sessionId);
  }, [sessionId, markSessionRead]);

  const turnByTask = useMemo(() => {
    const map = new Map<string, Turn>();
    for (const t of turns.data ?? []) if (t.role === 'assistant') map.set(t.task_id, t);
    return map;
  }, [turns.data]);

  const limit = settings.data
    ? (settings.data.context_token_limit ?? settings.data.defaults.context_token_limit)
    : snapshot.data?.context_window;
  const percent = snapshot.data && limit ? (snapshot.data.total_tokens / limit) * 100 : null;
  const meta = run ? statusMeta(run.status) : null;
  const inspectorShown = narrow ? drawerOpen : inspectorOpen;

  const title = session
    ? sessionTitle(session, { status: null, goal: latest?.task_goal ?? '', at: 0 })
    : '';
  const known = run && ordered.some((t) => t.task_id === run.taskId);

  const allTasks = useMemo(() => {
    const list = [...ordered];
    if (run && !known) {
      list.push({
        task_id: run.taskId,
        task_goal: run.goal,
        status: run.status,
        session_id: sessionId,
        workspace_id: workspaceId || session?.workspace_id || '',
        created_at: run.startedTs ?? 0,
        updated_at: run.finishedTs ?? 0,
        step_count: run.stepCount,
      } as unknown as Task);
    }
    return list;
  }, [ordered, run, known, sessionId, workspaceId, session?.workspace_id]);

  const timelineDialogues: DialogueNode[] = useMemo(() => {
    return allTasks.map((t, index) => ({
      id: t.task_id,
      goal: t.task_goal,
      index,
    }));
  }, [allTasks]);

  const [activeTaskIndex, setActiveTaskIndex] = useState(0);

  const scrollToTurn = useCallback(
    (index: number) => {
      const el = document.getElementById(`chat-turn-${index}`);
      if (el) {
        stick.current = index === timelineDialogues.length - 1;
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        setActiveTaskIndex(index);
      }
    },
    [timelineDialogues.length],
  );

  return (
    <div className="flex h-full min-h-0">
      <div className="relative flex min-w-0 flex-1 flex-col">
        <header className="relative z-sticky flex h-12 shrink-0 items-center gap-2 border-b border-ink/[0.06] bg-canvas/80 px-3 backdrop-blur-2xl dark:border-white/[0.06] sm:px-4">
          {phone && (
            <IconButton label="导航" onClick={() => setNavOpen(true)}>
              <Menu size={17} />
            </IconButton>
          )}
          <nav aria-label="位置" className="flex min-w-0 items-center gap-1.5 text-[13px]">
            {renaming && session ? (
              <InlineEdit
                label="会话标题"
                value={title}
                onCancel={() => setRenaming(false)}
                onCommit={(next) => {
                  setRenaming(false);
                  api.sessions
                    .update(sessionId, { title: next })
                    .then(() => client.invalidateQueries({ queryKey: ['sessions'] }))
                    .catch((e: Error) => notify(e.message, 'error'));
                }}
              />
            ) : (
              <button
                className={cn(
                  'min-w-0 truncate text-mute hover:text-ink',
                  phone && 'flex-1 text-left',
                )}
                onDoubleClick={
                  session && hasSessionMeta(session) ? () => setRenaming(true) : undefined
                }
              >
                {title}
              </button>
            )}
          </nav>
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {/* 全局 SRE 权限安全基线与全屏护栏 (按会话独立隔离：只读取证 / 工作区读写 / 高危系统提权) */}
            <SrePermissionGuard sessionId={sessionId} />

            {/* 存储压力槽条 (/ 22% | /boot 47%) */}
            <div className="hidden 2xl:flex items-center">
              <SreStorageGaugeStrip />
            </div>

            {/* 端口暴露面雷达按钮 (母题 A: 极坐标尺规旋切，大盘化抽屉联动) */}
            <Tip label="端口暴露面极坐标雷达大盘 (0.0.0.0 vs 127.0.0.1)">
              <IconButton
                label="端口暴露面雷达"
                active={inspectorShown && rightDrawerView === 'network'}
                onClick={() => {
                  if (inspectorShown && rightDrawerView === 'network') {
                    toggleInspector();
                  } else {
                    if (!inspectorShown) toggleInspector();
                    setRightDrawerView('network');
                  }
                }}
              >
                <Radio size={16} />
              </IconButton>
            </Tip>

            {/* 交付产物阅读抽屉按钮 (母题 D: 折叠撕边展开) */}
            <Tip label="交付产物阅读抽屉 (SOP 固化)">
              <IconButton
                label="交付产物阅读器"
                active={inspectorShown && rightDrawerView === 'artifacts'}
                onClick={() => {
                  if (inspectorShown && rightDrawerView === 'artifacts') {
                    toggleInspector();
                  } else {
                    openArtifact('系统全要素巡检与调优诊断大盘.md', '');
                    if (!inspectorShown) toggleInspector();
                    setRightDrawerView('artifacts');
                  }
                }}
              >
                <BookOpen size={16} />
              </IconButton>
            </Tip>

            {/* Live PTY 终端穿透与人类协同接管台 (快捷键 Ctrl+~) */}
            <Tip label="实时 PTY 终端与人机接管 (Ctrl+~)">
              <IconButton
                label="实时 PTY 终端"
                active={terminalOpen}
                onClick={() => toggleTerminal()}
              >
                <SquareTerminal size={16} />
              </IconButton>
            </Tip>

            {/* 常驻就绪墨印与主机遥测胶囊 (极简参数微标 + 纯 SVG + Tips 穿透) */}
            <div
              className="hidden xl:flex items-center gap-1.5 rounded-md border border-ink/[0.08] bg-ink/[0.02] px-2 py-0.5 font-mono text-[10.5px] text-mute dark:border-white/[0.08] dark:bg-white/[0.03]"
              aria-label="主机探针遥测状态"
            >
              <Tip label="SRE Kernel Harness: 实时遥测挂载 (127.0.0.1 · 6.8.0-sre)">
                <span className="flex cursor-default items-center gap-1 px-1 py-0.5 font-semibold text-ok rounded hover:bg-ink/[0.04] dark:hover:bg-white/[0.06] transition-colors">
                  <span className="h-1.5 w-1.5 rounded-full bg-ok animate-pulse" />
                  <span>SRE ONLINE</span>
                </span>
              </Tip>

              <span className="text-ink/15 dark:text-white/20 select-none">|</span>

              <Tip label="CPU Load: 0.42, 0.39, 0.46 (10 核 i5-12500H)">
                <span className="flex cursor-default items-center gap-1 px-1 py-0.5 rounded text-ink/80 dark:text-zinc-300 hover:bg-ink/[0.04] dark:hover:bg-white/[0.06] transition-colors">
                  <Zap size={11} strokeWidth={1.8} className="shrink-0" />
                  <span className="tabular-nums">0.42</span>
                </span>
              </Tip>

              <span className="text-ink/15 dark:text-white/20 select-none">|</span>

              <Tip label="Memory: 18.2G / 32.0G used (57%) · Swap: 0B">
                <span className="flex cursor-default items-center gap-1 px-1 py-0.5 rounded text-ink/80 dark:text-zinc-300 hover:bg-ink/[0.04] dark:hover:bg-white/[0.06] transition-colors">
                  <HardDrive size={11} strokeWidth={1.8} className="shrink-0" />
                  <span className="tabular-nums">57%</span>
                </span>
              </Tip>

              <span className="text-ink/15 dark:text-white/20 select-none">|</span>

              <Tip label="Docker: 8 running, 0 failed units">
                <span className="flex cursor-default items-center gap-1 px-1 py-0.5 rounded text-ink/80 dark:text-zinc-300 hover:bg-ink/[0.04] dark:hover:bg-white/[0.06] transition-colors">
                  <Boxes size={11} strokeWidth={1.8} className="shrink-0" />
                  <span className="tabular-nums">8</span>
                </span>
              </Tip>
            </div>
            {run && meta && (
              <MinimalStatusMark status={run.status} meta={meta} className="px-1" />
            )}
            {!connectionHealthy(connection) && (
              <Tip label={`事件流${CONNECTION_LABEL[connection]}`}>
                <span
                  role="status"
                  className="flex h-7 w-7 items-center justify-center rounded-full bg-warn/12 text-warn"
                >
                  <WifiOff size={14} />
                </span>
              </Tip>
            )}
            <McpStatus />
            <IconButton
              label="检视器"
              hint="Ctrl+J"
              active={inspectorShown}
              onClick={toggleInspector}
            >
              <PanelRight size={17} />
            </IconButton>
          </div>
        </header>

        <div className="relative flex min-h-0 flex-1 flex-col">
          <div
            ref={scroller}
            onScroll={(e) => {
              const el = e.currentTarget;
              const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
              stick.current = nearBottom;
              setAway(!nearBottom);
              if (nearBottom) {
                markSessionRead(sessionId);
                if (timelineDialogues.length) setActiveTaskIndex(timelineDialogues.length - 1);
              } else if (el.scrollTop < 40) {
                setActiveTaskIndex(0);
              } else {
                const containerTop = el.getBoundingClientRect().top;
                let bestIndex = 0;
                let minDiff = Infinity;
                for (let i = 0; i < timelineDialogues.length; i++) {
                  const turnEl = document.getElementById(`chat-turn-${i}`);
                  if (!turnEl) continue;
                  const rect = turnEl.getBoundingClientRect();
                  const diff = Math.abs(rect.top - containerTop - 60);
                  if (diff < minDiff) {
                    minDiff = diff;
                    bestIndex = i;
                  }
                }
                setActiveTaskIndex(bestIndex);
              }
            }}
            className="no-scrollbar min-h-0 flex-1 overflow-y-auto"
          >
            <div className="mx-auto w-full max-w-[780px] space-y-5 px-3 py-5 sm:px-5">
              {allTasks.map((t, index) => (
                <div
                  key={t.task_id}
                  id={`chat-turn-${index}`}
                  data-task-index={index}
                  className="scroll-mt-4 space-y-3"
                >
                  <UserMessage text={t.task_goal} />
                  {run && run.taskId === t.task_id ? (
                    <RunView run={run} live />
                  ) : (
                    <PastTask task={t} turn={turnByTask.get(t.task_id)} />
                  )}
                </div>
              ))}
              {!allTasks.length && !run && tasks.isSuccess && (
                <div className="flex flex-col items-center gap-6 py-12 text-center">
                  <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-ink/[0.1] bg-ink/[0.03] text-ink backdrop-blur dark:border-white/[0.12] dark:bg-white/[0.04]">
                    <StarLogo size={24} />
                  </div>
                  <div>
                    <h3 className="text-base font-semibold text-ink">LinuxOpsPilot SRE 运维助理已就绪</h3>
                    <p className="mx-auto mt-1 max-w-md text-xs text-mute">
                      支持并行系统探测、生产配置热修与自动快照备份。点击下方常用运维卡片快速发起，或在输入框提交自定义排障任务：
                    </p>
                  </div>
                  <div className="w-full max-w-[680px]">
                    <SreQuickCards
                      onSelect={(prompt) => {
                        void submit(sessionId, prompt, 'workspace_write', false);
                      }}
                      disabled={busy}
                    />
                  </div>
                </div>
              )}
            </div>
          </div>

          <ConversationTimelineRail
            dialogues={timelineDialogues}
            activeIndex={activeTaskIndex}
            onSelect={scrollToTurn}
          />
        </div>

        <AnimatePresence>
          {away && (
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 8 }}
              className="pointer-events-none absolute inset-x-0 bottom-[7.5rem] z-sticky flex justify-center"
            >
              <IconButton
                label="回到最新"
                className="pointer-events-auto h-9 w-9 rounded-full bg-raised shadow-float"
                onClick={() => {
                  stick.current = true;
                  setAway(false);
                  markSessionRead(sessionId);
                  const el = scroller.current;
                  el?.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
                }}
              >
                <ArrowDown size={16} />
              </IconButton>
            </motion.div>
          )}
        </AnimatePresence>

        <Composer
          sessionId={sessionId}
          workspaceId={workspaceId || session?.workspace_id || ''}
          busy={busy}
          contextPercent={percent}
          contextTokens={snapshot.data?.total_tokens ?? null}
          contextLimit={limit ?? null}
          onCancel={() => void cancel()}
          onSubmit={async (text: string, level: PermissionLevel, confirmed: boolean) => {
            const task = await submit(sessionId, text, level, confirmed);
            if (task) void client.invalidateQueries({ queryKey: ['tasks'] });
          }}
        />
      </div>

      {narrow ? (
        <AnimatePresence>
          {drawerOpen && (
            <>
              <motion.div
                key="scrim"
                aria-hidden
                className="fixed inset-0 z-drawer bg-black/10 backdrop-blur-[2px]"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
                onClick={toggleInspector}
              />
              <motion.aside
                key="drawer"
                aria-label="检视器"
                className={cn(
                  'fixed inset-y-0 right-0 z-modal border-l border-[#2B2A27]/20 bg-canvas/98 shadow-float backdrop-blur-2xl dark:border-white/[0.12]',
                  rightDrawerView === 'artifacts'
                    ? 'w-[min(880px,96vw)]'
                    : rightDrawerView === 'network'
                      ? 'w-[min(700px,96vw)]'
                      : 'w-[min(420px,92vw)]',
                )}
                initial={{ x: '100%' }}
                animate={{ x: 0 }}
                exit={{ x: '100%' }}
                transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
              >
                <Inspector sessionId={sessionId} onClose={toggleInspector} />
              </motion.aside>
            </>
          )}
        </AnimatePresence>
      ) : (
        <motion.aside
          aria-label="检视器"
          initial={false}
          animate={{
            width: !inspectorOpen
              ? 0
              : inspectorWide
                ? rightDrawerView === 'artifacts'
                  ? typeof window !== 'undefined'
                    ? Math.min(1080, Math.max(760, window.innerWidth - 280))
                    : 1040
                  : rightDrawerView === 'network'
                    ? typeof window !== 'undefined'
                      ? Math.min(840, Math.max(640, window.innerWidth - 280))
                      : 800
                    : 520
                : (drawerWidths[rightDrawerView] ??
                  (rightDrawerView === 'artifacts' ? 760 : rightDrawerView === 'network' ? 640 : 380)),
            opacity: inspectorOpen ? 1 : 0,
          }}
          transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
          className="relative z-sticky shrink-0 overflow-hidden border-l border-ink/[0.06] dark:border-white/[0.06]"
        >
          {/* 左侧可拖拽调宽手柄 (SRE 工业刻度阻尼感，双击切换宽屏) */}
          {inspectorOpen && (
            <div
              onPointerDown={(e) => {
                e.preventDefault();
                const startX = e.clientX;
                const currentW = !inspectorWide
                  ? (drawerWidths[rightDrawerView] ??
                    (rightDrawerView === 'artifacts' ? 760 : rightDrawerView === 'network' ? 640 : 380))
                  : rightDrawerView === 'artifacts'
                    ? 1040
                    : rightDrawerView === 'network'
                      ? 800
                      : 520;

                const handlePointerMove = (ev: PointerEvent) => {
                  const deltaX = startX - ev.clientX;
                  const minW =
                    rightDrawerView === 'tasks' ? 320 : rightDrawerView === 'network' ? 440 : 520;
                  const maxW =
                    typeof window !== 'undefined'
                      ? Math.max(minW, window.innerWidth - 260)
                      : 1200;
                  const newW = Math.max(minW, Math.min(maxW, Math.round(currentW + deltaX)));
                  setDrawerWidth(rightDrawerView, newW);
                };

                const handlePointerUp = () => {
                  window.removeEventListener('pointermove', handlePointerMove);
                  window.removeEventListener('pointerup', handlePointerUp);
                };

                window.addEventListener('pointermove', handlePointerMove);
                window.addEventListener('pointerup', handlePointerUp);
              }}
              onDoubleClick={toggleInspectorWide}
              title="按住拖拽调节宽度，双击切换宽屏"
              className="group absolute inset-y-0 left-0 z-20 w-1.5 cursor-col-resize select-none hover:bg-[#2B2A27]/30 active:bg-[#2B2A27] dark:hover:bg-white/30 dark:active:bg-white transition-colors"
            >
              <div className="absolute inset-y-0 left-0 w-px bg-transparent group-hover:bg-[#2B2A27] dark:group-hover:bg-white" />
            </div>
          )}

          <div
            className="h-full"
            style={{
              width: !inspectorOpen
                ? 0
                : inspectorWide
                  ? rightDrawerView === 'artifacts'
                    ? typeof window !== 'undefined'
                      ? Math.min(1080, Math.max(760, window.innerWidth - 280))
                      : 1040
                    : rightDrawerView === 'network'
                      ? typeof window !== 'undefined'
                        ? Math.min(840, Math.max(640, window.innerWidth - 280))
                        : 800
                      : 520
                  : (drawerWidths[rightDrawerView] ??
                    (rightDrawerView === 'artifacts' ? 760 : rightDrawerView === 'network' ? 640 : 380)),
            }}
          >
            <Inspector sessionId={sessionId} onClose={toggleInspector} />
          </div>
        </motion.aside>
      )}
    </div>
  );
}
