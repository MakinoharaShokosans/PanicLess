/**
 * 输入台。任务运行期间提交的消息进入队列（可撤回、可调序），当前任务正常结束后依次提交；
 * 失败 / 取消 / 终止会暂停队列。设计见 docs/10_frontend/04_redesign.md §5.5。
 */
import { useQuery } from '@tanstack/react-query';
import {
  AlertOctagon,
  ArrowUp,
  ChevronDown,
  ChevronUp,
  ListPlus,
  Pause,
  Play,
  Square,
  Undo2,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { api, isUnsupported } from '../../api/endpoints';
import { useModels, useSessionSettings } from '../../api/useSettings';
import { Select } from '../../components/ui/Select';
import { Button, IconButton, Modal } from '../../components/ui/primitives';
import { queueOf, useQueueStore } from '../../stores/useQueueStore';
import { useUiStore } from '../../stores/useUiStore';
import { useCockpitStore } from '../../stores/useCockpitStore';
import { useAppStore } from '../../stores/useAppStore';
import type { PermissionLevel } from '../../types/api';
import { TERMINAL_STATUSES } from '../../types/api';
import { cn, formatTokens } from '../../utils/text';

const AUTO = '__auto__';

function ModelPicker({
  sessionId,
  workspaceModel,
}: {
  sessionId: string;
  workspaceModel: string | null;
}) {
  const models = useModels();
  const { query, save } = useSessionSettings(sessionId);
  const current = query.data?.model ?? AUTO;
  return (
    <Select
      label="模型"
      value={current}
      placement="up"
      disabled={!query.data || save.isPending}
      leading={<span aria-hidden className="h-2 w-2 shrink-0 rounded-full bg-[#2B2A27] dark:bg-white" />}
      onChange={(value) =>
        save.mutate({
          model: value === AUTO ? null : value,
          context_token_limit: query.data?.context_token_limit ?? null,
        })
      }
      options={[
        {
          value: AUTO,
          label: workspaceModel ? `默认 · ${workspaceModel}` : '默认',
          hint: '按工作区设置或配置的端点顺序，失败时自动降级',
        },
        ...(models.data ?? []).map((m) => ({
          value: m.name,
          label: m.name,
          hint: `${m.model} · ${formatTokens(m.context_window)} 上下文`,
        })),
      ]}
    />
  );
}

/**
 * 母题 A：极坐标尺规旋切 (Compass & Polar Sweep)
 * 高精度 SRE 仪表盘：刻度背景、12 点钟基准线、显式百分比参数微标、悬停参数气泡
 */
function ContextRing({
  percent,
  tokens,
  limit,
  onClick,
}: {
  percent: number;
  tokens?: number | null;
  limit?: number | null;
  onClick: () => void;
}) {
  const r = 8.5;
  const c = 2 * Math.PI * r;
  const tone =
    percent > 85
      ? 'stroke-[#DC2626] text-[#DC2626]'
      : percent > 65
        ? 'stroke-[#D97706] text-[#D97706]'
        : 'stroke-[#2B2A27] text-[#2B2A27] dark:stroke-zinc-200 dark:text-zinc-200';

  const label =
    tokens && limit
      ? `${formatTokens(tokens)} / ${formatTokens(limit)} Token 容量`
      : `上下文容量 ${Math.round(percent)}%`;

  return (
    <IconButton label={label} hint="点击检视" onClick={onClick} side="top" className="h-8 w-auto px-1.5">
      <div className="flex items-center gap-1.5 select-none">
        <svg width="22" height="22" viewBox="0 0 24 24" className="shrink-0 -rotate-90">
          {/* 刻度背景轨与 1px 细线外圆 */}
          <circle
            cx="12"
            cy="12"
            r={r}
            fill="none"
            strokeWidth="1.8"
            className="stroke-ink/[0.1] dark:stroke-white/[0.15]"
          />
          {/* 12 点钟基准工程刻度引线 */}
          <line
            x1="12"
            y1="1.5"
            x2="12"
            y2="4"
            stroke="currentColor"
            strokeWidth="1.6"
            className="stroke-ink/40 dark:stroke-white/50"
          />
          {/* 3、6、9 点钟微型刻度点 */}
          <circle cx="21" cy="12" r="0.65" className="fill-ink/30 dark:fill-white/30" />
          <circle cx="12" cy="21" r="0.65" className="fill-ink/30 dark:fill-white/30" />
          <circle cx="3" cy="12" r="0.65" className="fill-ink/30 dark:fill-white/30" />
          {/* 动态旋切极轴圆弧 */}
          <circle
            cx="12"
            cy="12"
            r={r}
            fill="none"
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeDasharray={c}
            strokeDashoffset={c * (1 - Math.min(percent, 100) / 100)}
            className={cn('transition-[stroke-dashoffset,stroke] duration-500 ease-out', tone)}
          />
          {/* 中心微支点 */}
          <circle cx="12" cy="12" r="1.2" className="fill-ink/30 dark:fill-white/40" />
        </svg>
        <span className="font-mono text-[9.5px] font-semibold tabular-nums text-ink/80 dark:text-white/80">
          {Math.round(percent)}%
        </span>
      </div>
    </IconButton>
  );
}

export function Composer({
  sessionId,
  workspaceId,
  busy,
  contextPercent,
  contextTokens,
  contextLimit,
  onSubmit,
  onCancel,
}: {
  sessionId: string;
  workspaceId: string;
  /** 当前有任务在运行或等待审批 */
  busy: boolean;
  contextPercent: number | null;
  contextTokens?: number | null;
  contextLimit?: number | null;
  onSubmit: (text: string, level: PermissionLevel, confirmedFull: boolean) => Promise<void>;
  onCancel: () => void;
}) {
  const phone = useUiStore((s) => s.phone);
  const openCard = useUiStore((s) => s.openCard);
  const run = useAppStore((s) => (s.runSession === sessionId ? s.run : null));
  const sendTaskMessage = useAppStore((s) => s.sendTaskMessage);
  const isRunningTask = Boolean(run && !TERMINAL_STATUSES.includes(run.status));

  const draftKey = `linuxops.draft.${sessionId}`;
  const [text, setText] = useState(() => {
    try {
      return window.localStorage.getItem(draftKey) ?? '';
    } catch {
      return '';
    }
  });
  const area = useRef<HTMLTextAreaElement>(null);
  const queue = useQueueStore((s) => queueOf(s, sessionId));
  const { enqueue, remove, move, setPaused, clear } = useQueueStore.getState();

  const wsSettings = useQuery({
    queryKey: ['wsettings', workspaceId],
    queryFn: () => api.workspaces.settings(workspaceId),
    retry: false,
  });
  const wsData = wsSettings.isError && isUnsupported(wsSettings.error) ? null : wsSettings.data;
  const permissionLevel = useCockpitStore(
    (s) => (sessionId ? s.sessionPermissions[sessionId] : null) ?? s.permissionLevel,
  );
  const danger = permissionLevel === 'full_permissions';
  const [asking, setAsking] = useState<string | null>(null);

  useEffect(() => {
    try {
      if (text) window.localStorage.setItem(draftKey, text);
      else window.localStorage.removeItem(draftKey);
    } catch {
      /* 存储不可用：草稿只在本次页面内有效 */
    }
  }, [text, draftKey]);

  // 输入框随内容长高，最多八行
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 8 * 24 + 16)}px`;
  }, [text]);

  async function send(message: string, confirmed: boolean) {
    if (isRunningTask && run) {
      const res = await sendTaskMessage(run.taskId, message);
      if (res.ok) {
        setText('');
      }
      return;
    }
    if (busy) {
      enqueue(sessionId, message, permissionLevel);
    } else {
      if (danger && !confirmed) {
        setAsking(message);
        return;
      }
      await onSubmit(message, permissionLevel, confirmed);
    }
    setText('');
    setAsking(null);
  }

  function submit() {
    const message = text.trim();
    if (!message) return;
    if (danger && !isRunningTask) setAsking(message);
    else void send(message, false);
  }

  const ready = !!text.trim();

  return (
    <div className="mx-auto w-full max-w-[780px] px-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-5 sm:pb-5">
      {queue.items.length > 0 && (
        <div className="mb-2 overflow-hidden rounded-2xl border border-ink/[0.07] bg-card/80 backdrop-blur-xl dark:border-white/[0.08]">
          <div className="flex items-center gap-1 px-3 pt-1.5 text-2xs font-semibold text-mute">
            {queue.paused ? <Pause size={12} className="text-warn" /> : <ListPlus size={12} />}
            <span className="tabular-nums">{queue.items.length}</span>
            <span className={cn('ml-1 font-normal', queue.paused ? 'text-warn' : 'text-mute/80')}>
              {queue.paused ? '队列已暂停' : '待提交'}
            </span>
            <span className="flex-1" />
            {queue.paused && (
              <IconButton
                label="继续提交队列"
                className="h-6 w-6"
                onClick={() => setPaused(sessionId, false)}
              >
                <Play size={12} />
              </IconButton>
            )}
            <IconButton label="清空队列" className="h-6 w-6" onClick={() => clear(sessionId)}>
              <Undo2 size={12} />
            </IconButton>
          </div>
          <ul className="max-h-40 overflow-y-auto px-1.5 pb-1.5">
            {queue.items.map((m, i) => (
              <li
                key={m.id}
                className="group flex items-center gap-1 rounded-lg px-1.5 py-1 text-[13px] hover:bg-ink/[0.04]"
              >
                <span className="w-4 shrink-0 text-center text-2xs tabular-nums text-mute">
                  {i + 1}
                </span>
                <span className="min-w-0 flex-1 truncate">{m.text}</span>
                <span className="flex shrink-0 opacity-100 sm:opacity-0 sm:group-focus-within:opacity-100 sm:group-hover:opacity-100">
                  <IconButton
                    label="上移"
                    className="h-6 w-6"
                    disabled={i === 0}
                    onClick={() => move(sessionId, m.id, -1)}
                  >
                    <ChevronUp size={13} />
                  </IconButton>
                  <IconButton
                    label="下移"
                    className="h-6 w-6"
                    disabled={i === queue.items.length - 1}
                    onClick={() => move(sessionId, m.id, 1)}
                  >
                    <ChevronDown size={13} />
                  </IconButton>
                  <IconButton
                    label="撤回"
                    className="h-6 w-6"
                    onClick={() => remove(sessionId, m.id)}
                  >
                    <Undo2 size={13} />
                  </IconButton>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className={cn(
          'rounded-[24px] border bg-card/95 p-2.5 shadow-float backdrop-blur-2xl transition-all duration-300',
          'focus-within:border-[#2B2A27] dark:focus-within:border-white/40 focus-within:ring-1 focus-within:ring-[#2B2A27]/20',
          danger
            ? 'border-[#DC2626] shadow-[0_0_15px_rgba(220,38,38,0.08)]'
            : permissionLevel === 'read_only'
              ? 'border-ok/30'
              : 'border-ink/[0.08] dark:border-white/[0.09]',
        )}
      >
        {danger && (
          <div className="mb-1.5 flex items-center gap-1.5 px-2.5 py-1 font-mono text-[10.5px] font-semibold text-[#DC2626] border-b border-[#DC2626]/20 bg-[#DC2626]/[0.04] rounded-t-[18px]">
            <AlertOctagon size={12} className="shrink-0" />
            <span>高危系统提权模式 (PRIVILEGED) · 所有系统级命令将直接执行 (高危审计)</span>
          </div>
        )}
        {isRunningTask && (
          <div className="mb-1.5 flex items-center gap-1.5 px-2.5 py-1 text-[10.5px] text-mute border-b border-ink/5 dark:border-white/5 bg-accent/[0.03] rounded-t-[18px]">
            <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent animate-pulse" />
            <span>
              {run?.status === 'waiting_for_approval'
                ? '任务正在等待审批中：补充纠正信息将排队进入收件箱，审批继续后在下一次模型调用前合入。当前工具不会被立即中断；发送消息不代表恢复或批准。'
                : '任务运行中：补充纠正信息将排队进入收件箱，在下一次模型调用前合入。当前执行中的工具不会被立即中断；发送消息不代表批准操作。'}
            </span>
          </div>
        )}
        <textarea
          ref={area}
          aria-label="消息"
          rows={1}
          maxLength={20000}
          value={text}
          placeholder={
            isRunningTask
              ? run?.status === 'waiting_for_approval'
                ? '补充纠正信息（审批等待中，将在下轮模型调用前处理）...'
                : '补充纠正信息（如：刚才端口说错了，是 8081）...'
              : busy
                ? '排队下一条消息'
                : '消息'
          }
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== 'Enter' || e.nativeEvent.isComposing) return;
            // 手机上 Enter 换行（软键盘），桌面 Enter 发送、Shift+Enter 换行
            if ((!phone && !e.shiftKey) || e.metaKey || e.ctrlKey) {
              e.preventDefault();
              submit();
            }
          }}
          className="block max-h-52 min-h-[2.5rem] w-full resize-none bg-transparent px-2.5 py-1.5 text-[15px] leading-6 outline-none placeholder:text-mute/50"
        />
        <div className="mt-1 flex items-center gap-1.5">
          <ModelPicker sessionId={sessionId} workspaceModel={wsData?.model ?? null} />
          <span className="min-w-0 flex-1" />
          {contextPercent !== null && (
            <ContextRing
              percent={contextPercent}
              tokens={contextTokens}
              limit={contextLimit}
              onClick={() => {
                openCard('context');
                useCockpitStore.getState().setRightDrawerView('tasks');
                useUiStore.getState().setInspectorOpen(true);
              }}
            />
          )}
          {busy && (
            <IconButton
              label="取消任务"
              side="top"
              onClick={onCancel}
              className="text-bad hover:text-bad"
            >
              <Square size={15} fill="currentColor" />
            </IconButton>
          )}
          <IconButton
            type="submit"
            label={isRunningTask ? '发送补充消息' : busy ? '加入队列' : '发送'}
            hint="Enter"
            side="top"
            disabled={!ready}
            className={cn(
              'h-9 w-9 rounded-full transition-all',
              ready
                ? 'bg-[#2B2A27] text-white hover:bg-[#1A1A1A] dark:bg-white dark:text-[#1A1A1A] dark:hover:bg-zinc-200 shadow-sm'
                : 'bg-ink/10 text-mute/50',
            )}
          >
            {isRunningTask ? (
              <ArrowUp size={18} strokeWidth={2.5} />
            ) : busy ? (
              <ListPlus size={17} />
            ) : (
              <ArrowUp size={18} strokeWidth={2.5} />
            )}
          </IconButton>
        </div>
      </form>

      {asking !== null && (
        <Modal title="以高危系统提权 (PRIVILEGED) 运行？" onClose={() => setAsking(null)}>
          <p className="text-sm text-ink leading-relaxed">
            当前处于<strong>高危系统提权 (FULL PRIVILEGED)</strong> 模式，此任务中<strong>所有系统级命令将自动执行，不再发起二次确认</strong>，包括 sudo 提权、服务启停与系统核心文件修改。
          </p>
          <div className="mt-4 flex justify-end gap-2">
            <Button autoFocus onClick={() => setAsking(null)}>
              取消
            </Button>
            <Button variant="danger" onClick={() => void send(asking, true)}>
              确认执行
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
