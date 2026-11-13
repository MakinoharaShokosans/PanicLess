/** 一次任务的运行视图：思考、汇报、工具调用、审批、交付，按事件顺序排列。 */
import {
  AlertCircle,
  BookOpen,
  Check,
  Download,
  GitFork,
  MessageSquare,
  Play,
  ShieldCheck,
  X,
} from 'lucide-react';
import { useMemo } from 'react';
import { api } from '../../api/endpoints';
import { SafeMarkdown } from '../../components/common/SafeMarkdown';
import { HitlApprovalCard } from '../../components/domain/HitlApprovalCard';
import { CopyButton } from '../../components/ui/CopyButton';
import { IconButton } from '../../components/ui/primitives';
import { Tip } from '../../components/ui/Tooltip';
import { useAppStore } from '../../stores/useAppStore';
import { useCockpitStore } from '../../stores/useCockpitStore';
import { useUiStore } from '../../stores/useUiStore';
import type { RunState, TimelineItem } from '../../stores/taskReducer';
import { TERMINAL_STATUSES, type TaskMessage } from '../../types/api';
import { cn } from '../../utils/text';
import { clock, formatDuration } from '../../utils/time';
import { useNow } from '../../utils/useNow';
import { AgentGroup } from './blocks/AgentGroup';
import { ReasoningBlock } from './blocks/ReasoningBlock';
import { SayBlock } from './blocks/SayBlock';
import { jumpToCall, ToolCard } from './blocks/ToolCard';

type AgentItem = Extract<TimelineItem, { kind: 'agent' }>;
type Node = { key: string; item: TimelineItem } | { key: string; agents: AgentItem[] };

/** 相邻的同类 agent 事件合并成一组 */
function nodesOf(items: TimelineItem[]): Node[] {
  const out: Node[] = [];
  for (const [index, item] of items.entries()) {
    const key = `${item.seq}-${index}`;
    const prev = out[out.length - 1];
    if (item.kind === 'agent' && prev && 'agents' in prev && prev.agents[0]?.group === item.group) {
      prev.agents.push(item);
    } else if (item.kind === 'agent') {
      out.push({ key, agents: [item] });
    } else {
      out.push({ key, item });
    }
  }
  return out;
}

const EMPTY_MESSAGES: TaskMessage[] = [];

export function RunView({
  run,
  live,
  onFork,
}: {
  run: RunState;
  /** 当前正在订阅的任务（可审批、可续跑）；回放的历史任务为 false */
  live: boolean;
  onFork?: () => void;
}) {
  const approve = useAppStore((s) => s.approve);
  const reject = useAppStore((s) => s.reject);
  const resume = useAppStore((s) => s.resume);
  const messages = useAppStore((s) => (run.taskId ? s.taskMessages[run.taskId] : undefined)) ?? EMPTY_MESSAGES;
  const openArtifact = useCockpitStore((s) => s.openArtifact);
  const nodes = useMemo(() => nodesOf(run.items), [run.items]);
  const terminal = TERMINAL_STATUSES.includes(run.status);
  const working = !terminal && run.status !== 'waiting_for_approval';
  const streaming = run.items.some(
    (i) => (i.kind === 'reasoning' || i.kind === 'say') && i.streaming,
  );
  const now = useNow(working && !streaming);

  return (
    <div className="space-y-1">
      {nodes.map((n) => {
        if ('agents' in n) return <AgentGroup key={n.key} items={n.agents} />;
        const item = n.item;
        const turn = 'turn' in item ? item.turn : 'step' in item ? item.step : undefined;
        let content = null;
        switch (item.kind) {
          case 'reasoning':
            content = <ReasoningBlock item={item} />;
            break;
          case 'say':
            content = <SayBlock item={item} />;
            break;
          case 'tool':
            content = <ToolCard item={item} taskId={run.taskId} />;
            break;
          case 'decision':
            content = (
              <div className="relative pl-3 py-0.5 border-l border-line-subtle dark:border-[#27272A] font-mono text-[11.5px]">
                <span className="text-ink-ghost select-none mr-1.5">⎿</span>
                <span className={item.approved ? 'text-ok font-semibold' : 'text-warn font-semibold'}>
                  {item.approved ? '已批准' : `已拒绝：${item.text}`}
                </span>
              </div>
            );
            break;
          case 'guard':
            content = (
              <div className="relative pl-3 py-0.5 border-l border-line-subtle dark:border-[#27272A] font-mono text-[11.5px] text-warn">
                <span className="text-ink-ghost select-none mr-1.5">⎿</span>
                <span>{item.text}</span>
              </div>
            );
            break;
          case 'unknown':
            content = (
              <div className="relative pl-3 py-0.5 border-l border-line-subtle dark:border-[#27272A] font-mono text-[10px] text-ink-ghost">
                <span className="text-ink-ghost select-none mr-1.5">⎿</span>
                <span>{item.name}</span>
              </div>
            );
            break;
          default:
            content = null;
        }
        if (!content) return null;
        return (
          <div
            key={n.key}
            id={turn ? `turn-item-${turn}` : undefined}
            data-turn={turn}
            className="scroll-mt-6"
          >
            {content}
          </div>
        );
      })}

      {live && run.approval && (
        <HitlApprovalCard
          request={run.approval}
          onApprove={(decision) => approve(decision)}
          onReject={(reason) => reject(reason)}
        />
      )}

      {working && !streaming && (
        <div className="flex items-center gap-2 text-[12.5px] text-mute" role="status">
          <span className="flex gap-0.5" aria-hidden>
            {[0, 1, 2].map((i) => (
              <span
                key={i}
                className="h-1 w-1 animate-dots rounded-full bg-accent"
                style={{ animationDelay: `${i * 0.18}s` }}
              />
            ))}
          </span>
          {run.startedTs && (
            <span className="tabular-nums">
              {formatDuration(Math.max(0, (now - run.startedTs) * 1000))}
            </span>
          )}
        </div>
      )}

      {/* 运行中补充消息收件箱 */}
      {messages.length > 0 && (
        <div className="my-2 rounded-lg border border-ink/10 bg-canvas/60 p-2.5 dark:border-white/10 text-[11px]">
          <div className="flex items-center justify-between pb-1.5 border-b border-ink/10 dark:border-white/10">
            <span className="font-semibold text-ink flex items-center gap-1.5">
              <MessageSquare size={13} className="text-accent" />
              任务收件箱（补充消息 {messages.length} 条）
            </span>
            <span className="text-[10px] text-ink-ghost">
              当前执行中的工具不会被立即中断；将在下一次模型调用前合入
            </span>
          </div>
          <ul className="mt-1.5 space-y-1">
            {messages.map((m) => (
              <li
                key={m.message_id}
                className="flex items-start justify-between gap-2 rounded bg-ink/[0.03] p-1.5 dark:bg-white/[0.03]"
              >
                <div className="min-w-0 flex-1">
                  <p className="text-ink-heavy whitespace-pre-wrap break-words">{m.content}</p>
                  <span className="text-[9.5px] text-ink-ghost mt-0.5 block">
                    {clock(m.created_at)}
                  </span>
                </div>
                <span
                  className={cn(
                    'shrink-0 rounded px-1.5 py-0.5 text-[9.5px] font-semibold',
                    m.status === 'consumed'
                      ? 'bg-ok/10 text-ok'
                      : 'bg-warn/10 text-warn animate-pulse',
                  )}
                >
                  {m.status === 'consumed' ? '已合入处理' : '已排队，下一轮处理'}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 真实验证记录 */}
      {run.verificationRecords && run.verificationRecords.length > 0 && (
        <div className="my-2 space-y-2 rounded-lg border border-ink/10 bg-canvas/60 p-2.5 dark:border-white/10">
          <div className="flex items-center justify-between border-b border-ink/10 pb-1.5 dark:border-white/10">
            <span className="font-semibold text-ink flex items-center gap-1.5 text-[12px]">
              <ShieldCheck size={14} className="text-accent" />
              真实验证记录 ({run.verificationRecords.length})
            </span>
            <span className="text-[10px] text-ink-ghost">
              依据真实执行证据 · 普通 Bash 成功不代表验证通过
            </span>
          </div>
          {(() => {
            const maxRev = Math.max(...run.verificationRecords!.map((r) => r.revision ?? 1));
            return (
              <div className="space-y-1.5">
                {run.verificationRecords!.map((rec, i) => {
                  const isHistorical = rec.revision < maxRev;
                  return (
                    <div
                      key={`${rec.goal}-${rec.revision}-${i}`}
                      className={cn(
                        'rounded p-2 text-[11px] border',
                        isHistorical
                          ? 'border-dashed border-ink/10 bg-ink/[0.01] opacity-75'
                          : 'border-ink/10 bg-ink/[0.03] dark:bg-white/[0.03]',
                      )}
                    >
                      <div className="flex items-center justify-between pb-1 border-b border-ink/5 dark:border-white/5">
                        <div className="flex items-center gap-1.5">
                          <span className="font-semibold text-ink">{rec.goal}</span>
                          <span className="rounded bg-ink/10 px-1 py-0.2 font-mono text-[9.5px] text-ink-subtle">
                            v{rec.revision}
                            {isHistorical ? '（历史版本·修改前）' : '（最新改动版本）'}
                          </span>
                        </div>
                        <span
                          className={cn(
                            'flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold',
                            rec.result === 'passed'
                              ? 'bg-ok/10 text-ok'
                              : rec.result === 'failed'
                                ? 'bg-bad/10 text-bad'
                                : 'bg-warn/10 text-warn',
                          )}
                        >
                          {rec.result === 'passed' ? (
                            <>
                              <Check size={11} /> 验证通过
                            </>
                          ) : rec.result === 'failed' ? (
                            <>
                              <X size={11} /> 验证未通过
                            </>
                          ) : (
                            <>
                              <AlertCircle size={11} /> 结论不确定
                            </>
                          )}
                        </span>
                      </div>
                      <div className="mt-1 space-y-0.5 text-[10.5px]">
                        <p className="text-ink-body">
                          <span className="text-ink-ghost">检查内容: </span>
                          {rec.check}
                        </p>
                        <p className="text-ink-heavy">
                          <span className="text-ink-ghost">观察结果: </span>
                          {rec.summary}
                        </p>
                        {rec.evidence_call_ids && rec.evidence_call_ids.length > 0 && (
                          <div className="flex items-center gap-1 pt-0.5 text-[10px]">
                            <span className="text-ink-ghost">关联证据调用:</span>
                            {rec.evidence_call_ids.map((cid) => (
                              <button
                                key={cid}
                                type="button"
                                onClick={() => jumpToCall(cid)}
                                className="rounded bg-accent/10 px-1 py-0.2 font-mono text-accent hover:bg-accent/20"
                                title="点击跳转至该调用"
                              >
                                #{cid}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            );
          })()}
        </div>
      )}

      {/* 任务最终结论 */}
      {run.conclusion && (
        <div className="my-2 space-y-2 rounded-lg border border-ink/10 bg-canvas/60 p-3 dark:border-white/10">
          <div className="flex items-center justify-between border-b border-ink/10 pb-1.5 dark:border-white/10">
            <span className="font-semibold text-ink flex items-center gap-1.5 text-[12px]">
              <Check size={14} className="text-ok" />
              任务最终结论
            </span>
            <span
              className={cn(
                'rounded px-2 py-0.5 text-[10.5px] font-semibold',
                run.conclusion.status === 'completed'
                  ? 'bg-ok/10 text-ok'
                  : run.conclusion.status === 'partial'
                    ? 'bg-warn/10 text-warn'
                    : run.conclusion.status === 'needs_input'
                      ? 'bg-accent/10 text-accent'
                      : 'bg-bad/10 text-bad',
              )}
            >
              {run.conclusion.status === 'completed'
                ? '全部完成 (completed)'
                : run.conclusion.status === 'partial'
                  ? '部分完成 (partial)'
                  : run.conclusion.status === 'needs_input'
                    ? '需要补充信息 (needs_input)'
                    : '受阻 (blocked)'}
            </span>
          </div>
          <p className="text-[12px] text-ink-heavy leading-relaxed">{run.conclusion.summary}</p>
          {run.conclusion.remaining && run.conclusion.remaining.length > 0 && (
            <div className="rounded border border-warn/20 bg-warn/[0.04] p-2 space-y-1">
              <div className="flex items-center gap-1 text-[10.5px] font-semibold text-warn">
                <AlertCircle size={12} />
                <span>未完成事项 ({run.conclusion.remaining.length})</span>
              </div>
              <ul className="space-y-0.5 pl-4 list-disc text-[11px] text-ink-heavy">
                {run.conclusion.remaining.map((item, idx) => (
                  <li key={idx}>{item}</li>
                ))}
              </ul>
            </div>
          )}
          {run.conclusion.status !== 'completed' && (
            <p className="text-[10.5px] text-ink-ghost italic border-t border-ink/5 pt-1.5 dark:border-white/5">
              提示：本轮任务已结束。如需继续推进未完成事项，请在同一会话中提交新任务（将自动继承上下文与经验）。
            </p>
          )}
        </div>
      )}

      {run.error && <p className="text-[13px] text-bad">{run.error}</p>}

      {terminal && run.delivery && (
        <div className="py-2 space-y-2">
          <div className="flex items-center justify-between border-b border-[#2B2A27]/10 pb-1.5 dark:border-white/10">
            <span className="font-mono text-[11px] font-semibold text-ink dark:text-zinc-200">
              [ SRE 任务交付与巡检大盘 ]
            </span>
            <Tip label="展开 480px 巡检与调优诊断大盘 · TOC / SOP" side="left">
              <button
                type="button"
                aria-label="展开产物抽屉"
                onClick={() => {
                  openArtifact('系统全要素巡检与调优诊断大盘.md', run.delivery || '');
                  useUiStore.getState().setInspectorOpen(true);
                }}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded border border-[#2B2A27]/20 bg-canvas text-ink transition hover:border-[#2B2A27] hover:bg-ink/[0.04] active:scale-95 dark:border-white/20 dark:text-zinc-200 dark:hover:border-white dark:hover:bg-white/10"
              >
                <BookOpen size={14} />
              </button>
            </Tip>
          </div>
          <SafeMarkdown text={run.delivery} />
        </div>
      )}

      {terminal && (
        <div className="flex items-center gap-1 border-t border-ink/[0.06] pt-2 text-[12px] text-mute dark:border-white/[0.06]">
          <span className="tabular-nums">
            {run.stepCount} 回合 · {run.totalTokens.toLocaleString()} Token
            {run.startedTs && run.finishedTs
              ? ` · ${formatDuration((run.finishedTs - run.startedTs) * 1000)}`
              : ''}
          </span>
          {run.terminationReason && run.status !== 'succeeded' && (
            <span className="ml-2 min-w-0 truncate text-bad">{run.terminationReason}</span>
          )}
          <span className="ml-auto flex items-center">
            {run.delivery && <CopyButton text={run.delivery} label="复制交付" />}
            <IconButton
              label="下载轨迹"
              className="h-6 w-6"
              onClick={() => window.open(api.tasks.traceUrl(run.taskId))}
            >
              <Download size={13} />
            </IconButton>
            {onFork && (
              <IconButton label="从这里分支" className="h-6 w-6" onClick={onFork}>
                <GitFork size={13} />
              </IconButton>
            )}
            {live && run.status === 'failed' && (
              <IconButton label="从检查点续跑" className="h-6 w-6" onClick={() => void resume()}>
                <Play size={13} />
              </IconButton>
            )}
          </span>
        </div>
      )}
    </div>
  );
}
