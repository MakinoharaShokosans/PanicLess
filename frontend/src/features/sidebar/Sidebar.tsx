/**
 * 侧栏：需要处理的会话、历史会话平铺列表、搜索、底部设置入口。
 * 手机宽度下由外壳放进抽屉；桌面可折叠成图标条。
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useParams } from '@tanstack/react-router';
import {
  Archive,
  ArchiveRestore,
  ChevronRight,
  GitFork,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Settings,
  ShieldAlert,
  Trash2,
  X,
} from 'lucide-react';
import { motion } from 'motion/react';
import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api/endpoints';
import { http } from '../../api/http';
import { subscribeGlobal } from '../../api/sse';
import {
  hasSessionMeta,
  sessionTitle,
  useSessionActivity,
  useSessions,
  type Activity,
} from '../../api/queries';
import { ThemeToggle } from '../../components/layout/ThemeToggle';
import { BrandBadge, StarLogo } from '../../components/common/BrandLogo';
import { ConfirmDialog } from '../../components/modals/ConfirmDialog';
import { InlineEdit } from '../../components/ui/InlineEdit';
import { MoreMenu } from '../../components/ui/MoreMenu';
import { StatusDot } from '../../components/ui/StatusDot';
import { Tip } from '../../components/ui/Tooltip';
import { IconButton, Reveal } from '../../components/ui/primitives';
import { useAppStore } from '../../stores/useAppStore';
import { useUiStore } from '../../stores/useUiStore';
import type { Session } from '../../types/api';
import { cn } from '../../utils/text';
import { relativeTime } from '../../utils/time';

function sortSessions(list: Session[] | undefined | null): Session[] {
  if (!Array.isArray(list)) return [];
  return [...list].sort(
    (a, b) =>
      Number(!!b.pinned) - Number(!!a.pinned) ||
      (b.updated_at ?? b.created_at) - (a.updated_at ?? a.created_at),
  );
}

export function Sidebar({ collapsedRail = false }: { collapsedRail?: boolean }) {
  const sessionsQuery = useSessions();
  const allSessions = useMemo(
    () => (Array.isArray(sessionsQuery.data) ? sessionsQuery.data : []),
    [sessionsQuery.data],
  );
  const sortedSessions = useMemo(() => sortSessions(allSessions), [allSessions]);

  const params = useParams({ strict: false }) as { sessionId?: string };
  const navigate = useNavigate();
  const client = useQueryClient();
  const phone = useUiStore((s) => s.phone);
  const toggleSidebar = useUiStore((s) => s.toggleSidebar);
  const setNavOpen = useUiStore((s) => s.setNavOpen);
  const notify = useAppStore((s) => s.notify);

  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [deletingSession, setDeletingSession] = useState<Session | null>(null);
  const [editingSession, setEditingSession] = useState<string | null>(null);

  // 活动状态：最近的若干未归档会话
  const watched = useMemo(
    () =>
      sortedSessions
        .filter((s) => !s.archived)
        .slice(0, 24)
        .map((s) => s.session_id),
    [sortedSessions],
  );
  const activity = useSessionActivity(watched, params.sessionId);

  const attention = useMemo(() => {
    const out: { session: Session; activity: Activity }[] = [];
    for (const s of sortedSessions) {
      const a = activity[s.session_id];
      if (
        a &&
        (a.status === 'waiting_for_approval' ||
          a.status === 'failed' ||
          a.status === 'needs_input' ||
          a.status === 'blocked') &&
        !s.archived
      ) {
        out.push({ session: s, activity: a });
      }
    }
    return out
      .filter(
        (x) =>
          x.session.session_id !== params.sessionId || x.activity.status === 'waiting_for_approval',
      )
      .sort(
        (a, b) =>
          Number(b.activity.status === 'waiting_for_approval') -
          Number(a.activity.status === 'waiting_for_approval'),
      );
  }, [sortedSessions, activity, params.sessionId]);

  const refreshSessions = () => client.invalidateQueries({ queryKey: ['sessions'] });

  // 全局事件流：跨会话/任务状态变更时实时失效缓存并更新侧栏
  useEffect(() => {
    const handle = subscribeGlobal({
      headers: () => http.headers(),
      onEvent: (ev) => {
        const data = (ev.data || {}) as Record<string, unknown>;
        const sessionId = (data.session_id as string) || undefined;
        if (sessionId) {
          void client.invalidateQueries({ queryKey: ['activity', sessionId] });
          void client.invalidateQueries({ queryKey: ['tasks', sessionId] });
        }
        void client.invalidateQueries({ queryKey: ['sessions'] });
      },
    });
    return () => handle.close();
  }, [client]);

  const go = (sessionId: string) => {
    if (phone) setNavOpen(false);
    void navigate({ to: '/s/$sessionId', params: { sessionId } });
  };

  const create = useMutation({
    mutationFn: () => api.sessions.create(),
    onSuccess: async (session) => {
      await refreshSessions();
      go(session.session_id);
    },
    onError: (e: Error) => notify(e.message, 'error'),
  });

  const patch = useMutation({
    mutationFn: ({
      session,
      body,
    }: {
      session: Session;
      body: Parameters<typeof api.sessions.update>[1];
    }) => api.sessions.update(session.session_id, body),
    onSuccess: () => refreshSessions(),
    onError: (e: Error) => notify(e.message, 'error'),
  });

  const fork = useMutation({
    mutationFn: (session: Session) => api.sessions.fork(session.session_id),
    onSuccess: async (next) => {
      await refreshSessions();
      go(next.session_id);
    },
    onError: (e: Error) => notify(e.message, 'error'),
  });

  const q = query.trim().toLowerCase();

  const attentionIds = useMemo(
    () => new Set(attention.map((x) => x.session.session_id)),
    [attention],
  );

  const visibleSessions = useMemo(
    () =>
      sortedSessions.filter(
        (s) => !s.archived && (collapsedRail ? true : !attentionIds.has(s.session_id)),
      ),
    [sortedSessions, collapsedRail, attentionIds],
  );
  const archivedSessions = useMemo(
    () => sortedSessions.filter((s) => s.archived),
    [sortedSessions],
  );
  const filteredSessions = q
    ? sortedSessions.filter((s) =>
        sessionTitle(s, activity[s.session_id]).toLowerCase().includes(q),
      )
    : visibleSessions;

  return (
    <aside
      aria-label="侧栏"
      className={cn(
        'relative z-drawer flex h-full shrink-0 flex-col overflow-hidden border-r border-ink/[0.08] bg-side/90 backdrop-blur-2xl transition-[width] duration-350 ease-[cubic-bezier(0.16,1,0.3,1)] dark:border-white/[0.08]',
        collapsedRail ? 'w-[60px]' : 'w-[240px] max-w-[86vw]',
      )}
    >
      <div className="flex h-12 shrink-0 items-center px-2.5 overflow-hidden">
        {collapsedRail ? (
          <div className="flex w-full items-center justify-between">
            <Link
              to="/"
              aria-label="返回首页"
              className="flex h-8 w-8 items-center justify-center rounded-lg transition-transform hover:scale-105 active:scale-95"
            >
              <StarLogo size={20} />
            </Link>
            <IconButton label="展开侧栏" hint="Ctrl+B" side="right" onClick={toggleSidebar}>
              <PanelLeftOpen size={16} />
            </IconButton>
          </div>
        ) : (
          <div className="flex w-full items-center gap-1.5">
            <Link
              to="/"
              className="flex items-center transition-opacity hover:opacity-80"
              aria-label="LinuxOpsPilot 首页"
            >
              <BrandBadge />
            </Link>
            <span className="min-w-0 flex-1" />
            <IconButton
              label={searching ? '关闭搜索' : '搜索会话'}
              active={searching}
              onClick={() => {
                setSearching((v) => !v);
                setQuery('');
              }}
            >
              <Search size={16} />
            </IconButton>
            {phone ? (
              <IconButton label="关闭" onClick={() => setNavOpen(false)}>
                <X size={16} />
              </IconButton>
            ) : (
              <IconButton label="折叠侧栏" hint="Ctrl+B" onClick={toggleSidebar}>
                <PanelLeftClose size={16} />
              </IconButton>
            )}
          </div>
        )}
      </div>

      <div
        className={cn(
          'pt-1 pb-2 transition-all duration-350',
          collapsedRail ? 'flex justify-center px-2' : 'px-2.5',
        )}
      >
        <button
          onClick={() => create.mutate()}
          disabled={create.isPending}
          aria-label="新建对话"
          className={cn(
            'group relative flex h-8 items-center justify-center rounded-lg border border-[#2B2A27]/25 bg-canvas font-medium text-ink shadow-[0_1px_2px_rgba(0,0,0,0.03)] transition-all duration-200 hover:border-[#2B2A27] hover:bg-subtle active:scale-[0.98] disabled:opacity-50 dark:border-white/20 dark:bg-canvas dark:hover:border-white/50 dark:hover:bg-white/[0.04]',
            collapsedRail ? 'w-8 p-0' : 'w-full gap-1.5 px-3 text-[12.5px]',
          )}
        >
          <Plus
            size={collapsedRail ? 16 : 14}
            className="shrink-0 transition-transform duration-200 group-hover:rotate-90"
          />
          <span
            className={cn(
              'overflow-hidden whitespace-nowrap transition-[opacity,transform,max-width] duration-200 ease-out',
              collapsedRail
                ? 'max-w-0 opacity-0 -translate-x-3 pointer-events-none'
                : 'max-w-[120px] opacity-100 translate-x-0',
            )}
          >
            新建对话
          </span>
        </button>
      </div>

      {!collapsedRail && (
        <Reveal open={searching}>
          <div className="px-3 pb-2">
            <input
              autoFocus
              aria-label="搜索会话"
              className="field py-1.5 text-[13px]"
              placeholder="搜索会话标题"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
        </Reveal>
      )}

      {!q && attention.length > 0 && (
        collapsedRail ? (
          <div className="relative flex justify-center px-2 py-1">
            <IconButton
              label={`${attention.length} 个会话需要处理`}
              side="right"
              onClick={() => attention[0] && go(attention[0].session.session_id)}
            >
              {attention[0]?.activity.status === 'waiting_for_approval' ? (
                <ShieldAlert size={16} className="text-warn" />
              ) : (
                <StatusDot status={attention[0]?.activity.status ?? null} size={10} />
              )}
            </IconButton>
          </div>
        ) : (
          <div className="px-2 pb-2 mb-1 border-b border-ink/[0.06] dark:border-white/[0.06]">
            <div className="flex items-center gap-1.5 px-2 py-1 text-2xs font-semibold text-warn">
              <ShieldAlert size={13} />
              <span>待处理 ({attention.length})</span>
            </div>
            <div className="space-y-0.5">
              {attention.map(({ session: s, activity: a }) => (
                <SessionRow
                  key={s.session_id}
                  session={s}
                  activity={a}
                  current={s.session_id === params.sessionId}
                  editing={editingSession === s.session_id}
                  collapsed={false}
                  onOpen={() => go(s.session_id)}
                  onRename={(title) => {
                    setEditingSession(null);
                    patch.mutate({ session: s, body: { title } });
                  }}
                  onStartRename={() => setEditingSession(s.session_id)}
                  onCancelRename={() => setEditingSession(null)}
                  onPin={() => patch.mutate({ session: s, body: { pinned: !s.pinned } })}
                  onArchive={() => patch.mutate({ session: s, body: { archived: !s.archived } })}
                  onFork={() => fork.mutate(s)}
                  onDelete={() => setDeletingSession(s)}
                />
              ))}
            </div>
          </div>
        )
      )}

      <nav
        className={cn(
          'min-h-0 flex-1 space-y-1 overflow-y-auto pb-2',
          collapsedRail ? 'flex flex-col items-center px-1' : 'px-2',
        )}
      >
        <div className={cn('space-y-0.5', collapsedRail ? 'flex w-full flex-col items-center' : '')}>
          {filteredSessions.map((s) => (
            <SessionRow
              key={s.session_id}
              session={s}
              activity={activity[s.session_id]}
              current={s.session_id === params.sessionId}
              editing={editingSession === s.session_id}
              collapsed={collapsedRail}
              onOpen={() => go(s.session_id)}
              onRename={(title) => {
                setEditingSession(null);
                patch.mutate({ session: s, body: { title } });
              }}
              onStartRename={() => setEditingSession(s.session_id)}
              onCancelRename={() => setEditingSession(null)}
              onPin={() => patch.mutate({ session: s, body: { pinned: !s.pinned } })}
              onArchive={() => patch.mutate({ session: s, body: { archived: !s.archived } })}
              onFork={() => fork.mutate(s)}
              onDelete={() => setDeletingSession(s)}
            />
          ))}

          {!q && archivedSessions.length > 0 && !collapsedRail && (
            <ArchivedGroup count={archivedSessions.length}>
              {archivedSessions.map((s) => (
                <SessionRow
                  key={s.session_id}
                  session={s}
                  activity={activity[s.session_id]}
                  current={s.session_id === params.sessionId}
                  editing={false}
                  collapsed={false}
                  onOpen={() => go(s.session_id)}
                  onRename={() => undefined}
                  onStartRename={() => undefined}
                  onCancelRename={() => undefined}
                  onPin={() => undefined}
                  onArchive={() => patch.mutate({ session: s, body: { archived: false } })}
                  onFork={() => fork.mutate(s)}
                  onDelete={() => setDeletingSession(s)}
                />
              ))}
            </ArchivedGroup>
          )}

          {sessionsQuery.isSuccess && sortedSessions.length === 0 && !collapsedRail && (
            <div className="px-3 py-8 text-center text-xs text-mute">
              暂无历史会话
            </div>
          )}
        </div>
      </nav>

      <div
        className={cn(
          'shrink-0 border-t border-ink/[0.08] dark:border-white/[0.08]',
          collapsedRail ? 'flex flex-col items-center gap-1.5 py-2.5' : 'flex h-12 items-center gap-1 px-3',
        )}
      >
        <ThemeToggle />
        <IconButton
          label="系统配置 (Ctrl+,)"
          side={collapsedRail ? 'right' : 'top'}
          tabIndex={-1}
          onClick={() => {
            if (phone) setNavOpen(false);
            useUiStore.getState().setSettingsOpen(true);
          }}
        >
          <Settings size={16} />
        </IconButton>
      </div>

      {deletingSession && (
        <ConfirmDialog
          title="删除会话"
          confirmLabel="删除"
          onClose={() => setDeletingSession(null)}
          onConfirm={async () => {
            await api.sessions.remove(deletingSession.session_id);
            await refreshSessions();
            if (params.sessionId === deletingSession.session_id) void navigate({ to: '/' });
          }}
        >
          该会话及其对话记录、专属作业空间目录将被安全清理。
        </ConfirmDialog>
      )}
    </aside>
  );
}

function ArchivedGroup({ count, children }: { count: number; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="pt-1">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1 text-2xs text-mute hover:text-ink"
      >
        <Archive size={12} /> 已归档 ({count})
        <ChevronRight size={11} className={cn('transition-transform', open && 'rotate-90')} />
      </button>
      <Reveal open={open}>
        <div className="opacity-70">{children}</div>
      </Reveal>
    </div>
  );
}

function SessionRow({
  session,
  activity,
  current,
  editing,
  collapsed = false,
  onOpen,
  onRename,
  onStartRename,
  onCancelRename,
  onPin,
  onArchive,
  onFork,
  onDelete,
}: {
  session: Session;
  activity: Activity | undefined;
  current: boolean;
  editing: boolean;
  collapsed?: boolean;
  onOpen: () => void;
  onRename: (title: string) => void;
  onStartRename: () => void;
  onCancelRename: () => void;
  onPin: () => void;
  onArchive: () => void;
  onFork: () => void;
  onDelete: () => void;
}) {
  const readSessions = useUiStore((s) => s.readSessions);
  const initialReadCutoff = useUiStore((s) => s.initialReadCutoff);
  const title = sessionTitle(session, activity);
  const when = session.updated_at ?? (activity?.status ? activity.at : session.created_at);
  const manageable = hasSessionMeta(session);

  const readAt = readSessions[session.session_id] ?? initialReadCutoff;
  const isRead = readAt >= (activity?.at ?? 0);
  const displayStatus = activity?.status === 'succeeded' && isRead ? null : activity?.status;

  if (collapsed) {
    return (
      <Tip label={title} side="right">
        <button
          onClick={onOpen}
          aria-label={title}
          className={cn(
            'flex h-8 w-8 items-center justify-center rounded-lg transition-colors',
            current
              ? 'bg-ink/[0.08] text-ink dark:bg-white/[0.1] dark:text-white font-medium'
              : 'text-mute hover:bg-ink/[0.04] hover:text-ink',
          )}
        >
          {displayStatus === 'waiting_for_approval' ? (
            <ShieldAlert size={14} className="text-warn" />
          ) : displayStatus ? (
            <StatusDot status={displayStatus} size={7} />
          ) : (
            <MessageSquare size={15} />
          )}
        </button>
      </Tip>
    );
  }

  return (
    <motion.div layout="position" transition={{ duration: 0.18 }} className="group relative">
      <div
        role="link"
        tabIndex={0}
        aria-current={current ? 'page' : undefined}
        onClick={editing ? undefined : onOpen}
        onKeyDown={(e) => {
          if (!editing && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault();
            onOpen();
          }
        }}
        onDoubleClick={manageable ? onStartRename : undefined}
        className={cn(
          'relative flex h-8 cursor-pointer items-center gap-2 rounded-lg pl-2 pr-1 text-[13px] transition-colors',
          current
            ? 'font-medium text-ink bg-ink/[0.04] dark:bg-white/[0.06]'
            : 'text-mute hover:bg-ink/[0.035] hover:text-ink',
        )}
      >
        {displayStatus === 'waiting_for_approval' ? (
          <span title="待办审查命令" className="inline-flex shrink-0">
            <ShieldAlert
              size={14}
              className="text-warn"
              aria-label="待办审查命令"
            />
          </span>
        ) : (
          displayStatus && <StatusDot status={displayStatus} size={7} />
        )}
        {editing ? (
          <InlineEdit
            label="会话标题"
            value={title}
            onCommit={onRename}
            onCancel={onCancelRename}
          />
        ) : (
          <>
            <span className="relative min-w-0 max-w-full overflow-hidden transition-[opacity,transform] duration-200 ease-out">
              <span className="block truncate">{title}</span>
              {current && (
                <motion.div
                  layoutId="active-session-underline"
                  className="absolute -bottom-0.5 inset-x-0 h-[2px] rounded-full bg-accent"
                  transition={{ type: 'spring', stiffness: 450, damping: 35 }}
                />
              )}
            </span>
            <span className="min-w-0 flex-1" />
            {session.pinned && (
              <Pin size={11} className="shrink-0 text-accent" aria-label="已置顶" />
            )}
            <span className="hidden shrink-0 text-2xs tabular-nums text-mute/70 sm:inline sm:group-focus-within:hidden sm:group-hover:hidden">
              {relativeTime(when)}
            </span>
          </>
        )}
        {!editing && (
          // 手机上常驻；桌面上悬停或聚焦时才出现，避免挤占标题
          <span
            className="flex shrink-0 opacity-100 sm:pointer-events-none sm:opacity-0 sm:group-focus-within:pointer-events-auto sm:group-focus-within:opacity-100 sm:group-hover:pointer-events-auto sm:group-hover:opacity-100"
            onClick={(e) => e.stopPropagation()}
          >
            <MoreMenu
              label="会话操作"
              className="h-6 w-6"
              items={[
                {
                  label: '重命名',
                  icon: <Pencil size={14} />,
                  disabled: !manageable,
                  note: manageable ? undefined : '未提供',
                  onSelect: onStartRename,
                },
                {
                  label: session.pinned ? '取消置顶' : '置顶',
                  icon: session.pinned ? <PinOff size={14} /> : <Pin size={14} />,
                  disabled: !manageable,
                  note: manageable ? undefined : '未提供',
                  onSelect: onPin,
                },
                {
                  label: session.archived ? '取消归档' : '归档',
                  icon: session.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />,
                  disabled: !manageable,
                  note: manageable ? undefined : '未提供',
                  onSelect: onArchive,
                },
                { label: '分支', icon: <GitFork size={14} />, onSelect: onFork },
                {
                  label: '删除',
                  icon: <Trash2 size={14} />,
                  danger: true,
                  separated: true,
                  onSelect: onDelete,
                },
              ]}
            />
          </span>
        )}
      </div>
    </motion.div>
  );
}
