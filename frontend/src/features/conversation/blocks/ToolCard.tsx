/**
 * 通道 A：底层机器执行流（Machine Timeline）
 * 彻底去框化：无边框极简引线流 (Timeline Rail)，严禁框中套框
 * 节点格式：1px 细线分支 ➔ 微型图标 ➔ font-mono text-[12px] 灰色命令 ➔ 耗时微标
 * │ ⎿ [⚡ bash] echo "=== iostat ===" · 464ms · exit 1 ✕
 */
import { useMutation } from '@tanstack/react-query';
import {
  AlertCircle,
  AlertTriangle,
  BookOpen,
  Bookmark,
  Bot,
  Check,
  FilePen,
  FilePlus,
  FileText,
  FolderTree,
  Globe,
  History,
  Loader2,
  Plug,
  Search,
  ShieldCheck,
  SquareTerminal,
  Wrench,
  X,
} from 'lucide-react';
import { useState } from 'react';
import { api } from '../../../api/endpoints';
import { CopyButton } from '../../../components/ui/CopyButton';
import { Tip } from '../../../components/ui/Tooltip';
import { Chevron, Reveal } from '../../../components/ui/primitives';
import { SreDiffCard } from '../../../components/domain/SreDiffCard';
import { useCockpitStore } from '../../../stores/useCockpitStore';
import { useUiStore } from '../../../stores/useUiStore';
import type { TimelineItem } from '../../../stores/taskReducer';
import { cn, stripControl } from '../../../utils/text';
import { clock, formatBytes, formatDuration } from '../../../utils/time';

type Item = Extract<TimelineItem, { kind: 'tool' }>;

const ICONS: Record<string, typeof Wrench> = {
  bash: SquareTerminal,
  view_file: FileText,
  write_file: FilePlus,
  edit_file: FilePen,
  list_dir: FolderTree,
  grep: Search,
  load_skill: BookOpen,
  websearch: Globe,
  delegate: Bot,
  spawn_subagent: Bot,
  record_verification: ShieldCheck,
  report_outcome: Check,
  read_task_record: History,
  note: Bookmark,
  read_artifact: FileText,
};

function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v);
}

export function jumpToCall(callId: string): void {
  if (!callId) return;
  const el = document.getElementById(`call-${callId}`);
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('bg-accent/15');
    setTimeout(() => el.classList.remove('bg-accent/15'), 2000);
  }
}

export interface ParsedSubagentReport {
  subtaskId: string | null;
  artifactId: string | null;
  role: string | null;
  status: string | null;
  findings: Array<{
    statement: string;
    verified: boolean;
    citations: Array<{ kind: string; ref: string; note: string }>;
    droppedCount: number;
  }>;
  unresolved: string[];
  warnings: string[];
  stats: {
    steps?: number;
    toolCalls?: number;
    tokens?: number;
    elapsedSec?: number;
  } | null;
}

export function parseSubagentReport(rawText: string): ParsedSubagentReport {
  const text = formatToolText(rawText);
  const subtaskMatch = /子任务\s*ID[：:]\s*([A-Za-z0-9_-]+)/.exec(text);
  const artifactMatch = /artifact:\/\/([A-Za-z0-9_.-]+)/.exec(text);
  const roleMatch = /角色[：:]\s*(\S+)/.exec(text);
  const statusMatch = /状态[：:]\s*(\S+)/.exec(text);

  const findings: ParsedSubagentReport['findings'] = [];
  const unresolved: string[] = [];
  const warnings: string[] = [];

  const lines = text.split('\n');
  let section: 'findings' | 'unresolved' | 'warnings' | 'other' = 'other';
  let currentFinding: ParsedSubagentReport['findings'][0] | null = null;

  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith('未解决：') || trimmed.startsWith('未解决:')) {
      section = 'unresolved';
      if (currentFinding) {
        findings.push(currentFinding);
        currentFinding = null;
      }
      continue;
    }
    if (trimmed.startsWith('护栏标注：') || trimmed.startsWith('护栏标注:')) {
      section = 'warnings';
      if (currentFinding) {
        findings.push(currentFinding);
        currentFinding = null;
      }
      continue;
    }
    if (trimmed.startsWith('（') && trimmed.includes('步') && trimmed.includes('Token')) {
      section = 'other';
      if (currentFinding) {
        findings.push(currentFinding);
        currentFinding = null;
      }
      continue;
    }

    if (section === 'unresolved') {
      if (trimmed.startsWith('- ')) {
        unresolved.push(trimmed.slice(2).trim());
      }
      continue;
    }

    if (section === 'warnings') {
      if (trimmed.startsWith('- ')) {
        warnings.push(trimmed.slice(2).trim());
      }
      continue;
    }

    const findingMatch = /^(\d+)\.\s*(.+)$/.exec(trimmed);
    if (findingMatch) {
      if (currentFinding) findings.push(currentFinding);
      const rawStmt = findingMatch[2] ?? '';
      const verified = !rawStmt.includes('（未经验证）');
      const statement = rawStmt.replace(/（未经验证）/g, '').trim();
      currentFinding = { statement, verified, citations: [], droppedCount: 0 };
      continue;
    }

    if (currentFinding) {
      const citeMatch = /引用\[([^\]]+)\][：:]\s*([^\s—]+)(?:\s*—\s*(.*))?/.exec(trimmed);
      if (citeMatch) {
        currentFinding.citations.push({
          kind: citeMatch[1] ?? '',
          ref: citeMatch[2] ?? '',
          note: citeMatch[3] ?? '',
        });
        continue;
      }
      const droppedMatch = /（(\d+)\s*条引用不在实际访问记录里，已丢弃）/.exec(trimmed);
      if (droppedMatch) {
        currentFinding.droppedCount = Number(droppedMatch[1]);
        continue;
      }
    }
  }
  if (currentFinding) findings.push(currentFinding);

  const statsMatch = /（(\d+)\s*步，(\d+)\s*次工具调用，(\d+)\s*Token，用时\s*([\d.]+)\s*秒）/.exec(text);
  const stats = statsMatch
    ? {
        steps: Number(statsMatch[1]),
        toolCalls: Number(statsMatch[2]),
        tokens: Number(statsMatch[3]),
        elapsedSec: Number(statsMatch[4]),
      }
    : null;

  return {
    subtaskId: subtaskMatch ? (subtaskMatch[1] ?? null) : null,
    artifactId: artifactMatch ? (artifactMatch[1] ?? null) : null,
    role: roleMatch ? (roleMatch[1] ?? null) : null,
    status: statusMatch ? (statusMatch[1] ?? null) : null,
    findings,
    unresolved,
    warnings,
    stats,
  };
}

/** 一行摘要：最能说明“这次调用做了什么”的参数 */
function summaryOf(item: Item): string {
  const a = item.args;
  switch (item.tool) {
    case 'bash':
      return str(a['command']).split('\n')[0] ?? '';
    case 'view_file':
    case 'write_file':
    case 'edit_file':
    case 'list_dir':
      return str(a['path']) || '.';
    case 'grep':
      return `${str(a['pattern'])}${a['path'] ? `  ${str(a['path'])}` : ''}`;
    case 'load_skill':
      return str(a['skill_name']);
    case 'websearch':
      return str(a['topic'] ?? a['query']);
    case 'spawn_subagent':
      return `[${str(a['role'] || 'subagent')}] ${str(a['goal'])}`;
    case 'record_verification':
      return `[${str(a['result'] || 'check')}] ${str(a['goal'])}: ${str(a['check'])}`;
    case 'report_outcome':
      return `[${str(a['status'])}] ${str(a['summary'])}`;
    case 'read_task_record':
      return `query="${str(a['query'])}"${a['call_id'] ? ` call_id=${str(a['call_id'])}` : ''}`;
    case 'note':
      return `[${str(a['kind'])}] ${str(a['text'] || a['action'])}`;
    default: {
      const first = Object.values(a)[0];
      return first === undefined ? '' : str(first);
    }
  }
}

/** 过滤内部信封协议标签与防注入警示语 */
function formatToolText(text: string): string {
  if (!text) return '';
  let out = text.trim();
  out = out.replace(/^<(?:tool_observation|external_content)\b[^>]*>\s*/i, '');
  out = out.replace(/\s*<\/(?:tool_observation|external_content)>\s*$/i, '');
  out = out.replace(/^以上内容来自不可信来源[^\n]*\n+/i, '');
  return out.trim();
}

/** 展开态详情（去框化裸露排版） */
function Detail({ item, taskId }: { item: Item; taskId: string }) {
  const [full, setFull] = useState<string | null>(null);
  const [artifactError, setArtifactError] = useState<string | null>(null);
  const openArtifact = useCockpitStore((s) => s.openArtifact);
  const setInspectorOpen = useUiStore((s) => s.setInspectorOpen);

  const load = useMutation({
    mutationFn: () => api.tasks.artifact(taskId, item.artifactId as string),
    onSuccess: (text) => setFull(text),
  });

  const a = item.args;
  const live = item.state === 'running';
  const raw = full ?? item.result ?? '';
  const text = formatToolText(raw);
  const failed = item.state === 'failed';
  const cut = item.truncated || (!!item.artifactId && full === null);
  const edits = Array.isArray(a['edits']) ? (a['edits'] as Record<string, unknown>[]) : null;

  const hasCustomOutput =
    item.tool === 'spawn_subagent' ||
    item.tool === 'record_verification' ||
    item.tool === 'report_outcome' ||
    item.tool === 'read_task_record' ||
    item.tool === 'note';

  const isSpecialTool =
    item.tool === 'bash' ||
    item.tool === 'edit_file' ||
    item.tool === 'write_file' ||
    hasCustomOutput;

  return (
    <div className="font-mono text-[11px] leading-5 space-y-2 select-text">
      {/* 命令行 (CMD) */}
      {item.tool === 'bash' && (
        <div>
          <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-ink-ghost pb-0.5">
            <span className="font-semibold text-ink-subtle">CMD</span>
            <CopyButton text={str(a['command'])} label="复制命令" />
          </div>
          <div className="text-ink-heavy whitespace-pre-wrap break-all py-0.5">
            {str(a['command'])}
          </div>
        </div>
      )}

      {/* 文件编辑 Diff (母题 B: 游标卡尺矩形画线) */}
      {item.tool === 'edit_file' && (
        <div>
          <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-ink-ghost pb-0.5">
            <span className="font-semibold text-ink-subtle">DIFF: {str(a['path'])}</span>
          </div>
          <div className="space-y-2 pt-1">
            {edits
              ? edits.map((e, i) => (
                  <SreDiffCard
                    key={i}
                    before={str(e['old_string'])}
                    after={str(e['new_string'])}
                    path={i === 0 ? str(a['path']) : undefined}
                  />
                ))
              : a['old_string'] !== undefined && (
                  <SreDiffCard
                    before={str(a['old_string'])}
                    after={str(a['new_string'])}
                    path={str(a['path'])}
                  />
                )}
          </div>
        </div>
      )}

      {/* 文件写入 */}
      {item.tool === 'write_file' && (
        <div>
          <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-ink-ghost pb-0.5">
            <span className="font-semibold text-ink-subtle">WRITE: {str(a['path'])}</span>
            <CopyButton text={str(a['content'])} label="复制内容" />
          </div>
          <div className="text-ink-heavy whitespace-pre-wrap break-all py-0.5 max-h-56 overflow-auto">
            {str(a['content']).split('\n').slice(0, 60).join('\n')}
          </div>
        </div>
      )}

      {/* 子 Agent 动态委派 (spawn_subagent) */}
      {item.tool === 'spawn_subagent' && (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-ink-ghost pb-0.5">
            <span className="font-semibold text-ink-subtle">
              子 AGENT 委派 · 角色: {str(a['role'] || 'subagent')}
            </span>
            {Boolean(a['subtask_id']) && (
              <span className="text-2xs text-accent">续问已存在子任务</span>
            )}
          </div>
          <div className="text-ink-heavy whitespace-pre-wrap break-all py-0.5">
            {str(a['goal'])}
          </div>
          {Array.isArray(a['assigned_tools']) && (
            <div className="flex items-center gap-1.5 flex-wrap pt-0.5">
              <span className="text-[10px] text-ink-ghost">授权工具:</span>
              {(a['assigned_tools'] as string[]).map((t) => (
                <span
                  key={t}
                  className="rounded bg-ink/5 px-1.5 py-0.5 text-[10px] text-ink-subtle dark:bg-white/10"
                >
                  {t}
                </span>
              ))}
            </div>
          )}

          {(() => {
            const parsed = parseSubagentReport(raw);
            const subId =
              parsed.subtaskId ||
              (typeof a['subtask_id'] === 'string' ? a['subtask_id'] : null) ||
              item.artifactId;
            const status = parsed.status;
            const isPartial = status === 'partial' || status === 'timeout';
            return (
              <div className="mt-2 space-y-2 rounded border border-ink/10 bg-canvas/60 p-2.5 dark:border-white/10">
                <div className="flex items-center justify-between border-b border-ink/10 pb-1.5 dark:border-white/10">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-ink">子任务成果</span>
                    {subId && (
                      <span className="flex items-center gap-1 text-[10px] text-ink-ghost">
                        ID: <code className="text-ink-subtle">{subId}</code>
                        <CopyButton text={subId} label="复制子任务ID" />
                      </span>
                    )}
                  </div>
                  {status && (
                    <span
                      className={cn(
                        'rounded px-1.5 py-0.5 text-[10px] font-semibold',
                        status === 'completed'
                          ? 'bg-ok/10 text-ok'
                          : isPartial
                            ? 'bg-warn/10 text-warn'
                            : 'bg-bad/10 text-bad',
                      )}
                    >
                      {status === 'completed'
                        ? '全部完成'
                        : status === 'partial'
                          ? '部分成果'
                          : status === 'timeout'
                            ? '超时中断 (部分成果)'
                            : status}
                    </span>
                  )}
                </div>

                {isPartial && (
                  <div className="flex items-center gap-1.5 text-[10.5px] text-warn bg-warn/5 p-1.5 rounded">
                    <AlertTriangle size={12} className="shrink-0" />
                    <span>子 Agent 返回部分成果，未完成所有目标；中间发现与未解决项已完整保留。</span>
                  </div>
                )}

                {/* 中间发现 */}
                <div className="space-y-1.5">
                  <div className="text-[10px] uppercase tracking-wider text-ink-ghost">
                    中间发现 ({parsed.findings.length})
                  </div>
                  {parsed.findings.length === 0 ? (
                    <p className="text-[10.5px] text-mute italic">未提炼出独立发现</p>
                  ) : (
                    <ol className="space-y-1 text-[11px]">
                      {parsed.findings.map((f, i) => (
                        <li key={i} className="rounded bg-ink/[0.03] p-1.5 dark:bg-white/[0.03]">
                          <div className="flex items-start gap-1.5">
                            <span className="shrink-0 tabular-nums text-ink-ghost">{i + 1}.</span>
                            <span className="flex-1 text-ink-heavy">{f.statement}</span>
                            <span
                              className={cn(
                                'shrink-0 rounded px-1 py-0.2 text-[9.5px]',
                                f.verified ? 'bg-ok/10 text-ok' : 'bg-warn/10 text-warn',
                              )}
                            >
                              {f.verified ? '已验证' : '未经验证'}
                            </span>
                          </div>
                          {f.citations.length > 0 && (
                            <div className="mt-1 pl-4 space-y-0.5 text-[10px] text-ink-ghost">
                              {f.citations.map((c, ci) => (
                                <div key={ci} className="flex items-center gap-1">
                                  <span>引用 [{c.kind}]:</span>
                                  <span className="font-mono text-ink-subtle">{c.ref}</span>
                                  {c.note && <span>— {c.note}</span>}
                                </div>
                              ))}
                            </div>
                          )}
                          {f.droppedCount > 0 && (
                            <p className="mt-0.5 pl-4 text-[9.5px] text-bad">
                              （{f.droppedCount} 条引用不在实际访问记录里，已丢弃）
                            </p>
                          )}
                        </li>
                      ))}
                    </ol>
                  )}
                </div>

                {/* 未解决项 */}
                {parsed.unresolved.length > 0 && (
                  <div className="space-y-1 rounded border border-warn/20 bg-warn/[0.04] p-1.5">
                    <div className="flex items-center gap-1 text-[10px] font-semibold text-warn">
                      <AlertTriangle size={11} />
                      <span>未解决项 ({parsed.unresolved.length})</span>
                    </div>
                    <ul className="space-y-0.5 pl-4 text-[10.5px] text-ink-heavy list-disc">
                      {parsed.unresolved.map((u, ui) => (
                        <li key={ui}>{u}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* 护栏标注 */}
                {parsed.warnings.length > 0 && (
                  <div className="space-y-1 rounded border border-ink/10 bg-ink/[0.02] p-1.5">
                    <div className="text-[10px] font-semibold text-ink-subtle">护栏标注</div>
                    <ul className="space-y-0.5 pl-4 text-[10.5px] text-ink-ghost list-disc">
                      {parsed.warnings.map((w, wi) => (
                        <li key={wi}>{w}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* 原始会话与观察证据 */}
                {(subId || parsed.artifactId || item.artifactId) && (
                  <div className="flex items-center justify-between border-t border-ink/10 pt-1.5 dark:border-white/10 text-[10.5px]">
                    <span className="text-ink-ghost">原始会话与观察证据</span>
                    <button
                      type="button"
                      onClick={async () => {
                        const artId = parsed.artifactId || item.artifactId || subId;
                        if (!artId) return;
                        try {
                          const artifactText = await api.tasks.artifact(taskId, artId);
                          openArtifact(`${artId}.txt`, artifactText);
                          setInspectorOpen(true);
                        } catch (e) {
                          setArtifactError((e as Error).message);
                        }
                      }}
                      className="flex items-center gap-1 text-accent hover:underline font-mono"
                    >
                      <FileText size={11} />
                      <span>
                        打开证据文件 (artifact://{parsed.artifactId || item.artifactId || subId})
                      </span>
                    </button>
                  </div>
                )}
                {artifactError && <p className="text-[10px] text-bad">{artifactError}</p>}
              </div>
            );
          })()}
        </div>
      )}

      {/* 显式验证记录 (record_verification) */}
      {item.tool === 'record_verification' && (
        <div className="space-y-2">
          <div className="flex items-center justify-between border-b border-ink/10 pb-1 dark:border-white/10">
            <div className="flex items-center gap-1.5 font-semibold text-ink">
              <ShieldCheck size={13} className="text-accent" />
              <span>目标验证记录 (record_verification)</span>
            </div>
            {Boolean(a['result']) && (
              <span
                className={cn(
                  'flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold',
                  a['result'] === 'passed'
                    ? 'bg-ok/10 text-ok'
                    : a['result'] === 'failed'
                      ? 'bg-bad/10 text-bad'
                      : 'bg-warn/10 text-warn',
                )}
              >
                {a['result'] === 'passed' ? (
                  <>
                    <Check size={11} /> 验证通过 (passed)
                  </>
                ) : a['result'] === 'failed' ? (
                  <>
                    <X size={11} /> 验证未通过 (failed)
                  </>
                ) : (
                  <>
                    <AlertCircle size={11} /> 结论不确定 (inconclusive)
                  </>
                )}
              </span>
            )}
          </div>

          <dl className="space-y-1 text-[11px]">
            <div className="flex gap-2">
              <dt className="text-ink-ghost shrink-0 w-16">验证目标:</dt>
              <dd className="text-ink-heavy font-semibold">{str(a['goal'])}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-ink-ghost shrink-0 w-16">检查内容:</dt>
              <dd className="text-ink-body">{str(a['check'])}</dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-ink-ghost shrink-0 w-16">观察说明:</dt>
              <dd className="text-ink-body">{str(a['summary'])}</dd>
            </div>
            {Array.isArray(a['evidence_call_ids']) && (
              <div className="flex items-start gap-2 pt-0.5">
                <dt className="text-ink-ghost shrink-0 w-16">证据调用:</dt>
                <dd className="flex flex-wrap gap-1">
                  {(a['evidence_call_ids'] as string[]).map((cid) => (
                    <button
                      key={cid}
                      type="button"
                      onClick={() => jumpToCall(cid)}
                      className="rounded bg-accent/10 px-1.5 py-0.5 font-mono text-[10px] text-accent hover:bg-accent/20 transition-colors"
                      title="点击跳转至该工具调用"
                    >
                      #{cid}
                    </button>
                  ))}
                </dd>
              </div>
            )}
          </dl>
          <div className="rounded bg-ink/[0.02] p-1.5 text-[10px] text-ink-ghost border border-ink/5 dark:border-white/5">
            <span>运行时反馈: {item.result || (live ? '正在执行...' : '无返回')}</span>
            <span className="ml-2 italic text-mute">
              · 仅真实调用的直接证据有效，普通 Bash 成功不再作为验证凭据
            </span>
          </div>
        </div>
      )}

      {/* 任务最终结论汇报 (report_outcome) */}
      {item.tool === 'report_outcome' && (
        <div className="space-y-2">
          <div className="flex items-center justify-between border-b border-ink/10 pb-1 dark:border-white/10">
            <div className="flex items-center gap-1.5 font-semibold text-ink">
              <Check size={13} className="text-ok" />
              <span>任务最终结论 (report_outcome)</span>
            </div>
            {Boolean(a['status']) && (
              <span
                className={cn(
                  'rounded px-1.5 py-0.5 text-[10px] font-semibold',
                  a['status'] === 'completed'
                    ? 'bg-ok/10 text-ok'
                    : a['status'] === 'partial'
                      ? 'bg-warn/10 text-warn'
                      : a['status'] === 'needs_input'
                        ? 'bg-accent/10 text-accent'
                        : 'bg-bad/10 text-bad',
                )}
              >
                {a['status'] === 'completed'
                  ? '全部完成'
                  : a['status'] === 'partial'
                    ? '部分完成'
                    : a['status'] === 'needs_input'
                      ? '需要补充信息'
                      : '受阻'}
              </span>
            )}
          </div>

          <div className="space-y-1 text-[11px]">
            <div>
              <span className="text-[10px] text-ink-ghost block">依据说明:</span>
              <p className="text-ink-heavy mt-0.5">{str(a['summary'])}</p>
            </div>
            {Array.isArray(a['remaining']) && (
              <div className="pt-1">
                <span className="text-[10px] text-ink-ghost block">未完成事项:</span>
                {(a['remaining'] as string[]).length === 0 ? (
                  <p className="text-[10.5px] text-ok mt-0.5">无剩余事项，已完全解决</p>
                ) : (
                  <ul className="mt-0.5 space-y-0.5 pl-4 list-disc text-warn text-[10.5px]">
                    {(a['remaining'] as string[]).map((rem, ri) => (
                      <li key={ri}>{rem}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
          <div className="text-[10px] text-ink-ghost">
            <span>运行时反馈: {item.result || (live ? '正在记录...' : '已记录')}</span>
          </div>
        </div>
      )}

      {/* 原始记录回读 (read_task_record) */}
      {item.tool === 'read_task_record' && (
        <div className="space-y-2">
          <div className="flex items-center justify-between border-b border-ink/10 pb-1 dark:border-white/10">
            <div className="flex items-center gap-1.5 font-semibold text-ink">
              <History size={13} className="text-accent" />
              <span>原始记录回读 (read_task_record)</span>
            </div>
            <span className="text-[10px] text-ink-ghost">
              start={str(a['start'] ?? 0)} · limit={str(a['limit'] ?? 10)}
            </span>
          </div>

          <div className="flex items-center gap-3 text-[10.5px] text-ink-ghost flex-wrap">
            {Boolean(a['query']) && (
              <span>
                关键词: <code className="text-ink-heavy font-semibold">"{str(a['query'])}"</code>
              </span>
            )}
            {Boolean(a['call_id']) && (
              <button
                type="button"
                onClick={() => jumpToCall(str(a['call_id']))}
                className="text-accent hover:underline"
              >
                定位调用: #{str(a['call_id'])}
              </button>
            )}
          </div>

          {(() => {
            let parsed: {
              records?: Array<{
                index: number;
                role: string;
                name?: string;
                call_id?: string;
                text: string;
                offset: number;
                total_chars: number;
                next_offset?: number | null;
              }>;
              next_start?: number | null;
            } | null = null;
            try {
              if (raw) parsed = JSON.parse(raw);
            } catch {
              parsed = null;
            }

            if (!raw && live) {
              return <p className="text-[10.5px] text-mute">正在检索原始记录...</p>;
            }
            if (!raw) {
              return <p className="text-[10.5px] text-mute">原始内容未加载</p>;
            }
            if (!parsed || !Array.isArray(parsed.records)) {
              return (
                <div className="space-y-1">
                  <p className="text-[10.5px] text-ink-body whitespace-pre-wrap">{raw}</p>
                </div>
              );
            }

            return (
              <div className="space-y-2">
                <div className="text-[10px] text-ink-ghost uppercase">
                  匹配到 {parsed.records.length} 条记录
                </div>
                {parsed.records.length === 0 ? (
                  <p className="text-[10.5px] text-mute italic">未检索到匹配的消息记录</p>
                ) : (
                  <div className="space-y-1.5 max-h-72 overflow-auto">
                    {parsed.records.map((r) => (
                      <div
                        key={r.index}
                        className="rounded bg-ink/[0.03] p-1.5 text-[10.5px] dark:bg-white/[0.03]"
                      >
                        <div className="flex items-center justify-between text-ink-ghost pb-0.5 border-b border-ink/5 dark:border-white/5">
                          <span>
                            #{r.index} · {r.role} {r.name ? `(${r.name})` : ''}
                          </span>
                          {r.call_id && (
                            <button
                              type="button"
                              onClick={() => jumpToCall(r.call_id!)}
                              className="font-mono text-accent hover:underline text-[10px]"
                            >
                              #{r.call_id}
                            </button>
                          )}
                        </div>
                        <pre className="mt-1 font-mono text-[10px] whitespace-pre-wrap break-all text-ink-body max-h-36 overflow-auto">
                          {r.text}
                        </pre>
                        {r.next_offset !== null && r.next_offset !== undefined && (
                          <div className="mt-1 text-[9.5px] text-warn">
                            当前正文已截断（已显示 {r.offset}–{r.next_offset} / 共 {r.total_chars}{' '}
                            字符），可用 offset={r.next_offset} 继续回读
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
                {parsed.next_start !== null && parsed.next_start !== undefined && (
                  <div className="flex items-center gap-1 text-[10px] text-warn">
                    <AlertTriangle size={11} />
                    <span>达到单次 limit 上限，还有后续记录（next_start={parsed.next_start}）</span>
                  </div>
                )}
              </div>
            );
          })()}
        </div>
      )}

      {/* 笔记与记忆 (note) */}
      {item.tool === 'note' && (
        <div className="space-y-2">
          <div className="flex items-center justify-between border-b border-ink/10 pb-1 dark:border-white/10">
            <div className="flex items-center gap-1.5 font-semibold text-ink">
              <Bookmark size={13} className="text-accent" />
              <span>笔记 / 记忆记录 (note)</span>
            </div>
            {Boolean(a['kind']) && (
              <span
                className={cn(
                  'rounded px-1.5 py-0.5 text-[10px] font-semibold',
                  a['kind'] === 'experience'
                    ? 'bg-accent/10 text-accent'
                    : a['kind'] === 'fact'
                      ? 'bg-warn/10 text-warn'
                      : 'bg-bad/10 text-bad',
                )}
              >
                {a['kind'] === 'experience'
                  ? '稳定经验（待结合现场验证）'
                  : a['kind'] === 'fact'
                    ? '历史现场观察（非当前状态）'
                    : '失败尝试记录'}
              </span>
            )}
          </div>

          {a['kind'] === 'fact' && (
            <div className="rounded bg-warn/[0.05] p-1.5 text-[10px] text-warn border border-warn/15 flex items-center gap-1.5">
              <AlertTriangle size={12} className="shrink-0" />
              <span>
                注意：此为历史任务记录的现场观察，非当前服务器状态；现场采样时间以原始工具调用证据为准。
              </span>
            </div>
          )}

          {a['kind'] === 'experience' && (
            <div className="rounded bg-accent/[0.05] p-1.5 text-[10px] text-accent border border-accent/15 flex items-center gap-1.5">
              <Bookmark size={12} className="shrink-0" />
              <span>稳定经验：可跨会话复用，但应用于生产环境前应重新验证现场条件。</span>
            </div>
          )}

          {Boolean(a['text']) && (
            <div className="text-[11px] text-ink-heavy whitespace-pre-wrap py-0.5">
              {str(a['text'])}
            </div>
          )}

          {a['kind'] === 'failed_attempt' && (
            <dl className="space-y-1 text-[11px]">
              {Boolean(a['action']) && (
                <div>
                  <dt className="text-[10px] text-ink-ghost">失败操作:</dt>
                  <dd className="text-bad font-semibold">{str(a['action'])}</dd>
                </div>
              )}
              {Boolean(a['failure_reason']) && (
                <div>
                  <dt className="text-[10px] text-ink-ghost">失败原因:</dt>
                  <dd className="text-ink-body">{str(a['failure_reason'])}</dd>
                </div>
              )}
              {Boolean(a['conclusion']) && (
                <div>
                  <dt className="text-[10px] text-ink-ghost">教训结论:</dt>
                  <dd className="text-ink-body">{str(a['conclusion'])}</dd>
                </div>
              )}
            </dl>
          )}
        </div>
      )}

      {/* 非专用工具的通用参数 */}
      {!isSpecialTool && (
        <div>
          <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-ink-ghost pb-0.5">
            <span className="font-semibold text-ink-subtle">ARGS</span>
            <CopyButton text={() => JSON.stringify(a, null, 2)} label="复制参数" />
          </div>
          <dl className="space-y-0.5">
            {Object.entries(a).map(([k, v]) => (
              <div key={k} className="flex gap-2">
                <dt className="text-ink-ghost shrink-0">{k}:</dt>
                <dd className="text-ink-body break-all">{str(v)}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {/* 实时/最终输出 (OUTPUT / STDERR) - 针对非 spawn/verification/outcome/read_task/note 工具 */}
      {!hasCustomOutput && (live ? !!(item.stdout || item.stderr) : !!(text || item.stdout || item.stderr)) && (
        <div className="pt-1">
          <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-ink-ghost pb-0.5">
            <span className={cn('font-semibold', failed ? 'text-bad' : 'text-ink-subtle')}>
              {failed ? 'STDERR' : 'OUTPUT'}
            </span>
            <CopyButton text={text || item.stdout + item.stderr} label="复制输出" />
          </div>
          <pre
            className={cn(
              'font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-all max-h-80 overflow-auto py-0.5 select-text',
              failed ? 'text-bad' : 'text-ink-body',
            )}
          >
            {stripControl(text || item.stdout + item.stderr)}
          </pre>
          {cut && (
            <div className="mt-1 flex items-center gap-1.5 text-[10.5px] text-warn">
              <AlertTriangle size={11} />
              <span>已截断{item.bytes ? ` · 共 ${formatBytes(item.bytes)}` : ''}</span>
              {item.artifactId && (
                <button
                  className="ml-2 underline hover:text-ink font-mono text-[10.5px]"
                  disabled={load.isPending}
                  onClick={() => load.mutate()}
                >
                  {load.isPending && <Loader2 size={10} className="animate-spin inline mr-1" />}
                  加载完整输出
                </button>
              )}
            </div>
          )}
          {load.isError && (
            <p className="mt-1 text-[11px] text-bad">{(load.error as Error).message}</p>
          )}
        </div>
      )}

      {/* 影响的文件列表 */}
      {item.changedFiles && item.changedFiles.length > 0 && (
        <div className="pt-1">
          <div className="text-[10px] uppercase tracking-wider text-ink-ghost pb-0.5">CHANGED_FILES</div>
          <ul className="space-y-0.5">
            {item.changedFiles.map((f) => (
              <li key={f.path} className="flex gap-2 text-ink-body">
                <span className="min-w-0 flex-1 truncate">{f.path}</span>
                {f.added !== undefined && <span className="text-ok font-semibold">+{f.added}</span>}
                {f.removed !== undefined && <span className="text-bad font-semibold">-{f.removed}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 底部微标信息 */}
      <div className="mt-2 pt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px] text-ink-ghost border-t border-line-subtle dark:border-white/[0.06]">
        <span>第 {item.step} 回合</span>
        <span className="tabular-nums">开始 {clock(item.startTs)}</span>
        {item.durationMs !== undefined && (
          <span className="tabular-nums">耗时 {formatDuration(item.durationMs)}</span>
        )}
        {item.exitCode !== undefined && (
          <span className={cn('tabular-nums font-semibold', item.exitCode !== 0 ? 'text-bad' : 'text-ok')}>
            exit {item.exitCode}
          </span>
        )}
        {item.timedOut && <span className="text-bad font-semibold">超时</span>}
        {item.folded && <span>已折叠</span>}
        {item.callId && <span className="text-ink-ghost/60">{item.callId}</span>}
      </div>
    </div>
  );
}

/**
 * 通道 A：底层机器执行流卡片 (去框化无边框极简引线流)
 */
export function ToolCard({ item, taskId }: { item: Item; taskId: string }) {
  const running = item.state === 'running';
  const [open, setOpen] = useState(false);
  const Icon = item.tool.startsWith('mcp__') ? Plug : (ICONS[item.tool] ?? Wrench);
  const duration = item.durationMs ?? (item.endTs ? (item.endTs - item.startTs) * 1000 : undefined);
  const expanded = open || (running && item.tool === 'bash' && !!(item.stdout || item.stderr));
  const summary = summaryOf(item);
  const mcpName = item.tool.startsWith('mcp__')
    ? item.tool.split('__').slice(1).join(' · ')
    : item.tool;

  return (
    <div
      id={item.callId ? `call-${item.callId}` : undefined}
      className="relative pl-3 py-0.5 border-l border-line-subtle dark:border-[#27272A] group transition-colors scroll-mt-12"
    >
      {/* 节点收起态：1px 细线分支 ➔ 图标 ➔ 等宽灰色命令 ➔ 耗时微标 */}
      <button
        aria-expanded={expanded}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 text-left font-mono text-[11.5px] text-ink-subtle hover:text-ink-heavy transition-colors py-0.5 group/btn"
      >
        {/* 1px 引线分支符号 */}
        <span className="text-ink-ghost select-none shrink-0 font-mono text-[11px]">⎿</span>

        {/* 工具标识徽标 */}
        <span className="inline-flex items-center gap-1 font-semibold text-ink-heavy shrink-0">
          <Icon
            size={12}
            className={cn('shrink-0', item.state === 'failed' ? 'text-bad' : 'text-ink-subtle')}
          />
          <span>[{mcpName}]</span>
        </span>

        {/* 命令行 / 摘要 */}
        <Tip label={summary} side="top" align="start" disabled={summary.length < 40}>
          <span className="min-w-0 flex-1 truncate text-ink-subtle group-hover/btn:text-ink-body">
            {summary}
          </span>
        </Tip>

        {/* 状态与耗时微标 */}
        <span className="flex shrink-0 items-center gap-1.5 text-[10.5px] tabular-nums text-ink-ghost">
          {duration !== undefined && !running && <span>{formatDuration(duration)}</span>}
          {item.exitCode !== undefined && item.exitCode !== 0 ? (
            <span className="text-bad flex items-center gap-0.5 font-semibold">
              exit {item.exitCode} <X size={11} />
            </span>
          ) : running ? (
            <Loader2 size={11} className="animate-spin text-accent" aria-label="运行中" />
          ) : item.state === 'ok' ? (
            <Check size={11} className="text-ok" aria-label="成功" />
          ) : (
            <X size={11} className="text-bad" aria-label="失败" />
          )}
        </span>

        <Chevron open={expanded} size={11} className="text-ink-ghost group-hover/btn:text-ink-heavy shrink-0" />
      </button>

      {/* 展开态：依托左侧 1px 墨线向下延展，使用极浅底色区分，严禁框中套框 */}
      <Reveal open={expanded}>
        <div className="mt-1 mb-2 ml-1 pl-3.5 py-2.5 border-l border-ink/25 dark:border-white/25 bg-[#FAFAFA]/95 dark:bg-[#121214]/95 rounded-r">
          <Detail item={item} taskId={taskId} />
        </div>
      </Reveal>
    </div>
  );
}
