import { useQuery } from '@tanstack/react-query';
import {
  Clock,
  Cpu,
  Download,
  FileText,
  Loader2,
  Play,
  RotateCw,
  Square,
  Wrench,
} from 'lucide-react';
import { motion } from 'motion/react';
import { useState } from 'react';
import { api } from '../../api/endpoints';
import { CopyButton } from '../../components/ui/CopyButton';
import { Card, IconButton, Modal } from '../../components/ui/primitives';
import { useAppStore } from '../../stores/useAppStore';
import { TERMINAL_STATUSES, type Artifact, type PlanItem } from '../../types/api';
import { MinimalStatusMark, statusMeta } from '../../utils/status';
import { cn, formatTokens } from '../../utils/text';
import { formatBytes, formatDuration } from '../../utils/time';
import { useNow } from '../../utils/useNow';

function Plan({ plan }: { plan: PlanItem[] }) {
  const scrollToTurn = (index: number) => {
    const el =
      document.getElementById(`turn-item-${index + 1}`) ||
      document.querySelector(`[data-turn="${index + 1}"]`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  };

  return (
    <ol className="relative space-y-1 pl-1">
      {/* 纵向工程标尺引线 */}
      <div className="absolute left-[7px] top-2 bottom-2 w-[1px] bg-ink/[0.08] dark:bg-white/[0.08]" />
      {plan.map((p, idx) => (
        <li
          key={p.id}
          onClick={() => scrollToTurn(idx)}
          className={cn(
            'group relative flex cursor-pointer items-start gap-2 rounded-lg px-1.5 py-1 text-[13px] leading-snug transition-colors hover:bg-ink/[0.04] dark:hover:bg-white/[0.04]',
            p.status === 'completed' && 'text-mute',
            p.status === 'pending' && 'text-mute/80',
          )}
          title="点击跳转至对应回合"
        >
          {p.status === 'completed' ? (
            <svg width="14" height="14" viewBox="0 0 14 14" className="relative z-10 mt-1 shrink-0 bg-card">
              <motion.path
                d="M2.5 7.5L5.5 10.5L11.5 3.5"
                fill="none"
                stroke="#2E7D32"
                strokeWidth="2.2"
                strokeLinecap="round"
                strokeLinejoin="round"
                initial={{ pathLength: 0 }}
                animate={{ pathLength: 1 }}
                transition={{ duration: 0.38, ease: [0.16, 1, 0.3, 1] }}
              />
            </svg>
          ) : p.status === 'in_progress' ? (
            <span className="relative z-10 mt-1 flex h-3.5 w-3.5 shrink-0 items-center justify-center bg-card">
              <span className="absolute h-2.5 w-2.5 rounded-full border border-ink/40 dark:border-white/40" />
              <span className="h-1 w-1 rounded-full bg-ink dark:bg-white animate-ping" />
            </span>
          ) : (
            <span className="relative z-10 mt-1.5 flex h-3 w-3 shrink-0 items-center justify-center bg-card">
              <span className="h-1.5 w-1.5 rounded-full bg-ink/20 dark:bg-white/25" />
            </span>
          )}
          <span className="relative min-w-0 break-words group-hover:text-ink dark:group-hover:text-white">
            {p.title}
            {p.status === 'completed' && (
              <motion.span
                aria-hidden
                className="pointer-events-none absolute left-0 top-[55%] h-[1px] w-full bg-[#1A1A1A]/40 dark:bg-white/40"
                initial={{ scaleX: 0 }}
                animate={{ scaleX: 1 }}
                style={{ transformOrigin: 'left center' }}
                transition={{ duration: 0.32, ease: [0.16, 1, 0.3, 1], delay: 0.08 }}
              />
            )}
          </span>
        </li>
      ))}
    </ol>
  );
}


function ArtifactViewer({
  taskId,
  artifact,
  onClose,
}: {
  taskId: string;
  artifact: Artifact;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ['artifact', taskId, artifact.artifact_id],
    queryFn: () => api.tasks.artifact(taskId, artifact.artifact_id),
    retry: false,
  });
  const download = () => {
    if (q.data === undefined) return;
    const url = URL.createObjectURL(new Blob([q.data], { type: 'text/plain;charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = artifact.artifact_id;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <Modal title={artifact.artifact_id} onClose={onClose} wide>
      <div className="mb-2 flex items-center gap-2 text-xs text-mute">
        <span className="tabular-nums">{formatBytes(artifact.size)}</span>
        <span className="flex-1" />
        {q.data !== undefined && <CopyButton text={q.data} label="复制产物" />}
        <IconButton label="下载" disabled={q.data === undefined} onClick={download}>
          <Download size={14} />
        </IconButton>
      </div>
      {q.isPending && <Loader2 size={18} className="mx-auto animate-spin text-mute" />}
      {q.isError && <p className="text-sm text-bad">{(q.error as Error).message}</p>}
      {q.data !== undefined && (
        <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded-xl bg-code p-3 font-mono text-[12px] leading-relaxed text-code-ink">
          {q.data}
        </pre>
      )}
    </Modal>
  );
}

export function TaskCard({ sessionId }: { sessionId: string }) {
  const run = useAppStore((s) => (s.runSession === sessionId ? s.run : null));
  const cancel = useAppStore((s) => s.cancel);
  const resume = useAppStore((s) => s.resume);
  const [viewing, setViewing] = useState<Artifact | null>(null);
  const live = !!run && !TERMINAL_STATUSES.includes(run.status);
  const now = useNow(live);
  const meta = run ? statusMeta(run.status) : null;
  const toolCount = run ? run.items.filter((item) => item.kind === 'tool').length : 0;
  const artifacts = useQuery({
    queryKey: ['artifacts', run?.taskId, run?.status],
    queryFn: () => api.tasks.artifacts(run?.taskId as string),
    enabled: !!run && TERMINAL_STATUSES.includes(run.status),
  });
  const elapsed = run?.startedTs ? ((run.finishedTs ?? now) - run.startedTs) * 1000 : null;

  // 过滤内部命令增量/输出记录（如 step_1_bash），仅保留用户真实关注的生成产物
  const isInternalArtifact = (id: string) =>
    /^step_\d+/.test(id) || id.includes('bash');

  const deliverables = (artifacts.data ?? []).filter(
    (a) => !isInternalArtifact(a.artifact_id),
  );

  return (
    <Card
      id="task"
      title="任务"
      aside={
        <span className="flex items-center">
          {live && (
            <IconButton
              label="取消任务"
              className="h-7 w-7 text-bad hover:text-bad"
              onClick={() => void cancel()}
            >
              <Square size={13} fill="currentColor" />
            </IconButton>
          )}
          {run && !live && run.status === 'failed' && (
            <IconButton label="从检查点续跑" className="h-7 w-7" onClick={() => void resume()}>
              <Play size={14} />
            </IconButton>
          )}
        </span>
      }
    >
      {run && meta ? (
        <div className="space-y-3">
          <div className="flex items-start gap-2">
            <p className="line-clamp-3 min-w-0 flex-1 break-words text-[13px] leading-snug">
              {run.goal}
            </p>
            <MinimalStatusMark status={run.status} meta={meta} />
          </div>

          {run.plan.length > 0 && <Plan plan={run.plan} />}

          <div className="flex items-center justify-between py-1 text-[12.5px]">
            <div className="flex items-center gap-1.5" title={`回合数：${run.stepCount}`}>
              <RotateCw size={13} className="shrink-0 text-mute/60" />
              <span className="font-medium tabular-nums text-ink">{run.stepCount}</span>
            </div>
            <div className="flex items-center gap-1.5" title={`工具调用：${toolCount}`}>
              <Wrench size={13} className="shrink-0 text-mute/60" />
              <span className="font-medium tabular-nums text-ink">{toolCount}</span>
            </div>
            <div
              className="flex items-center gap-1.5"
              title={`Token 消耗：${formatTokens(run.totalTokens)}`}
            >
              <Cpu size={13} className="shrink-0 text-mute/60" />
              <span className="font-medium tabular-nums text-ink">
                {formatTokens(run.totalTokens)}
              </span>
            </div>
            <div
              className="flex items-center gap-1.5"
              title={`用时：${elapsed !== null ? formatDuration(Math.max(0, elapsed)) : '—'}`}
            >
              <Clock size={13} className="shrink-0 text-mute/60" />
              <span className="font-medium tabular-nums text-ink">
                {elapsed !== null ? formatDuration(Math.max(0, elapsed)) : '—'}
              </span>
            </div>
          </div>

          {run.conclusion && (
            <div className="rounded border border-ink/10 bg-ink/[0.02] p-2 text-[12px] space-y-1">
              <div className="flex items-center justify-between text-2xs font-semibold text-ink-ghost">
                <span>任务结论</span>
                <span
                  className={cn(
                    'rounded px-1.5 py-0.2',
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
                    ? '已完成'
                    : run.conclusion.status === 'partial'
                      ? '部分完成'
                      : run.conclusion.status === 'needs_input'
                        ? '需补充信息'
                        : '受阻'}
                </span>
              </div>
              <p className="text-ink-heavy text-[11.5px] line-clamp-2">{run.conclusion.summary}</p>
              {run.conclusion.remaining && run.conclusion.remaining.length > 0 && (
                <p className="text-warn text-[10.5px]">剩余 {run.conclusion.remaining.length} 项未完成</p>
              )}
            </div>
          )}

          {run.verificationRecords && run.verificationRecords.length > 0 && (
            <div className="rounded border border-ink/10 bg-ink/[0.02] p-2 text-[12px] space-y-1">
              <div className="flex items-center justify-between text-2xs font-semibold text-ink-ghost">
                <span>验证记录</span>
                <span className="font-mono text-[10px] text-ink-subtle">
                  {run.verificationRecords.filter((r) => r.result === 'passed').length} / {run.verificationRecords.length} 通过
                </span>
              </div>
              <ul className="space-y-0.5 text-[11px]">
                {run.verificationRecords.slice(0, 3).map((v, i) => (
                  <li key={i} className="flex items-center justify-between gap-1 text-[10.5px]">
                    <span className="truncate flex-1 text-ink-subtle">{v.goal}</span>
                    <span
                      className={cn(
                        'shrink-0 font-medium',
                        v.result === 'passed'
                          ? 'text-ok'
                          : v.result === 'failed'
                            ? 'text-bad'
                            : 'text-warn',
                      )}
                    >
                      {v.result === 'passed' ? '通过' : v.result === 'failed' ? '未通过' : '不确定'}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {deliverables.length > 0 && (
            <ul className="space-y-0.5 text-[12.5px]">
              {deliverables.map((a) => (
                <li key={a.artifact_id}>
                  <button
                    className="group -mx-1 flex w-[calc(100%+0.5rem)] items-center gap-2 rounded-lg px-1.5 py-1 text-left font-mono text-mute hover:bg-ink/[0.045] hover:text-ink"
                    onClick={() => setViewing(a)}
                    aria-label={`查看产物 ${a.artifact_id}`}
                  >
                    <FileText
                      size={13}
                      className="shrink-0 text-accent/70 group-hover:text-accent"
                    />
                    <span className="min-w-0 flex-1 truncate">{a.artifact_id}</span>
                    <span className="shrink-0 tabular-nums text-2xs text-mute/60">
                      {formatBytes(a.size)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <p className="py-2 text-center text-mute">—</p>
      )}

      {viewing && run && (
        <ArtifactViewer taskId={run.taskId} artifact={viewing} onClose={() => setViewing(null)} />
      )}
    </Card>
  );
}
