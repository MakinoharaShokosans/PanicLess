import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api/endpoints';
import { ApiError } from '../../api/http';
import { emptyRun, reduceTaskEvent, runFromSnapshot, type RunState } from '../../stores/taskReducer';
import { useAppStore } from '../../stores/useAppStore';
import type { ServerEvent, Task, TaskConclusion, VerificationRecord } from '../../types/api';
import { ToolCard, parseSubagentReport } from './blocks/ToolCard';
import { Composer } from './Composer';
import { RunView } from './RunView';

type ToolItem = Parameters<typeof ToolCard>[0]['item'];

function mockTool(overrides: Partial<ToolItem> & Pick<ToolItem, 'tool' | 'args'>): ToolItem {
  return {
    kind: 'tool',
    seq: 1,
    callId: 'call-1',
    step: 1,
    state: 'ok',
    startTs: 1000,
    stdout: '',
    stderr: '',
    ...overrides,
  };
}

function renderWithClient(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe('Agent Closed-Loop Integration Tests', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useAppStore.setState({
      runSession: null,
      run: null,
      connection: 'closed',
      taskMessages: {},
      notice: null,
    });
  });

  describe('1. Task Statuses and Conclusion Handling', () => {
    it('handles task.finished event with conclusion across partial, needs_input, and blocked', () => {
      const conclusion: TaskConclusion = {
        status: 'partial',
        summary: '排查了端口冲突，但防火墙规则未能全部放通',
        remaining: ['核实云厂商安全组入方向 8080 端口', '检查 iptables FORWARD 链'],
      };

      const verification: VerificationRecord[] = [
        {
          goal: '验证 8080 端口存活',
          check: 'curl -I http://127.0.0.1:8080',
          result: 'passed',
          summary: 'HTTP/1.1 200 OK',
          evidence_call_ids: ['call-curl-1'],
          revision: 1,
        },
      ];

      const finishedEvent: ServerEvent = {
        event: 'task.finished',
        task_id: 't-1',
        seq: 10,
        ts: 1000,
        v: 1,
        status: 'partial',
        conclusion,
        verification_records: verification,
      };

      const state = reduceTaskEvent(emptyRun('t-1', '测试任务'), finishedEvent);
      expect(state.status).toBe('partial');
      expect(state.conclusion).toEqual(conclusion);
      expect(state.verificationRecords).toEqual(verification);
    });

    it('maps snapshot correctly in runFromSnapshot without data loss', () => {
      const task: Task = {
        task_id: 'task-snap-1',
        session_id: 'sess-1',
        workspace_id: 'ws-1',
        task_goal: '验证快照恢复',
        permission_level: 'workspace_write',
        status: 'blocked',
        approval_request: null,
        plan: [],
        step_count: 5,
        total_tokens: 1200,
        consecutive_errors: 0,
        termination_reason: '',
        created_at: 1000,
        started_at: 1000,
        finished_at: 1050,
        artifact_count: 0,
        conclusion: {
          status: 'blocked',
          summary: '权限不足，无法修改 /etc/sysctl.conf',
          remaining: ['需要 full_permissions 授权'],
        },
        verification_records: [
          {
            goal: '检查 sysctl 写入权限',
            check: 'sysctl -w net.ipv4.ip_forward=1',
            result: 'failed',
            summary: 'Permission denied',
            evidence_call_ids: ['c-sysctl'],
            revision: 1,
          },
        ],
      };

      const run = runFromSnapshot(task);
      expect(run.status).toBe('blocked');
      expect(run.conclusion?.status).toBe('blocked');
      expect(run.conclusion?.summary).toContain('权限不足');
      expect(run.verificationRecords).toHaveLength(1);
      expect(run.verificationRecords?.[0]?.result).toBe('failed');
    });

    it('renders conclusion details and remaining items in RunView, prompting new task submission', () => {
      const run: RunState = {
        ...emptyRun('t-partial', '排查网络'),
        status: 'partial',
        conclusion: {
          status: 'partial',
          summary: '本地监听正常，但公网不可达',
          remaining: ['排查安全组', '检查公网 IP 路由'],
        },
      };

      renderWithClient(<RunView run={run} live={false} />);

      expect(screen.getByText('任务最终结论')).toBeDefined();
      expect(screen.getByText('部分完成 (partial)')).toBeDefined();
      expect(screen.getByText('本地监听正常，但公网不可达')).toBeDefined();
      expect(screen.getByText('未完成事项 (2)')).toBeDefined();
      expect(screen.getByText('排查安全组')).toBeDefined();
      expect(screen.getByText('检查公网 IP 路由')).toBeDefined();
      expect(screen.getByText(/提示：本轮任务已结束。如需继续推进未完成事项，请在同一会话中提交新任务/)).toBeDefined();

      // Ensure no resume button is shown for partial status
      expect(screen.queryByLabelText('从检查点续跑')).toBeNull();
    });

    it('only allows /resume when run status is failed', async () => {
      const runPartial: RunState = {
        ...emptyRun('t-blocked', '阻断任务'),
        status: 'blocked',
      };
      useAppStore.setState({ run: runPartial, runSession: 'sess-1' });

      // Attempting resume when blocked triggers notification and does NOT call api.tasks.resume
      const resumeSpy = vi.spyOn(api.tasks, 'resume');
      await useAppStore.getState().resume();

      expect(resumeSpy).not.toHaveBeenCalled();
      expect(useAppStore.getState().notice?.message).toContain('不可续跑。继续处理请在同一会话提交新任务');

      // Now set to failed status
      const runFailed: RunState = {
        ...emptyRun('t-failed', '失败任务'),
        status: 'failed',
      };
      useAppStore.setState({ run: runFailed, runSession: 'sess-1' });
      resumeSpy.mockResolvedValueOnce({
        task_id: 't-failed',
        status: 'running',
        message: 'resumed',
      });

      await useAppStore.getState().resume();
      expect(resumeSpy).toHaveBeenCalledWith('t-failed');
    });
  });

  describe('2. Real Verification Records & Multi-Revision Handling', () => {
    it('distinguishes passed, failed, and inconclusive, and highlights historical revisions', () => {
      const records: VerificationRecord[] = [
        {
          goal: '端口连通性检查',
          check: 'curl -I http://127.0.0.1:8080',
          result: 'passed',
          summary: 'HTTP 200 OK（修改前版本）',
          evidence_call_ids: ['c-1'],
          revision: 1,
        },
        {
          goal: '端口连通性检查',
          check: 'curl -I http://127.0.0.1:8080',
          result: 'failed',
          summary: 'Connection refused（改动后最新验证）',
          evidence_call_ids: ['c-2'],
          revision: 2,
        },
        {
          goal: '防火墙规则一致性',
          check: 'iptables -L -n -v',
          result: 'inconclusive',
          summary: '输出被截断，无法断定是否阻断',
          evidence_call_ids: ['c-3'],
          revision: 2,
        },
      ];

      const run: RunState = {
        ...emptyRun('t-records', '验证测试'),
        status: 'succeeded',
        verificationRecords: records,
      };

      renderWithClient(<RunView run={run} live={false} />);

      expect(screen.getByText('真实验证记录 (3)')).toBeDefined();
      expect(screen.getByText('依据真实执行证据 · 普通 Bash 成功不代表验证通过')).toBeDefined();

      // Revision badges
      expect(screen.getByText('v1（历史版本·修改前）')).toBeDefined();
      expect(screen.getAllByText('v2（最新改动版本）')).toHaveLength(2);

      // Result statuses
      expect(screen.getByText('验证通过')).toBeDefined();
      expect(screen.getByText('验证未通过')).toBeDefined();
      expect(screen.getByText('结论不确定')).toBeDefined();

      // Evidence call links
      expect(screen.getByText('#c-1')).toBeDefined();
      expect(screen.getByText('#c-2')).toBeDefined();
      expect(screen.getByText('#c-3')).toBeDefined();
    });
  });

  describe('3. In-flight Messages (Running Task Supplement)', () => {
    it('submits in-flight message via POST /api/v1/tasks/{id}/messages and queues with 202', async () => {
      const postSpy = vi.spyOn(api.tasks, 'sendMessage').mockResolvedValueOnce({
        message_id: 'msg-1',
        task_id: 'task-run-1',
        content: '刚才端口说错了，是 8081',
        status: 'queued',
        created_at: 1000,
      });

      const res = await useAppStore
        .getState()
        .sendTaskMessage('task-run-1', '刚才端口说错了，是 8081');

      expect(res.ok).toBe(true);
      expect(postSpy).toHaveBeenCalledWith('task-run-1', '刚才端口说错了，是 8081');
      expect(useAppStore.getState().notice?.message).toBe('已排队，下一轮处理');

      const messages = useAppStore.getState().taskMessages['task-run-1'];
      expect(messages).toHaveLength(1);
      expect(messages?.[0]?.status).toBe('queued');
    });

    it('recovers message statuses with fetchTaskMessages and dedupes', async () => {
      vi.spyOn(api.tasks, 'messages').mockResolvedValueOnce([
        {
          message_id: 'msg-1',
          task_id: 'task-run-1',
          content: '刚才端口说错了，是 8081',
          status: 'consumed',
          created_at: 1000,
        },
      ]);

      await useAppStore.getState().fetchTaskMessages('task-run-1');

      const messages = useAppStore.getState().taskMessages['task-run-1'];
      expect(messages).toHaveLength(1);
      expect(messages?.[0]?.status).toBe('consumed');
    });

    it('handles 409 conflict when task finishes concurrently and retains input', async () => {
      vi.spyOn(api.tasks, 'sendMessage').mockRejectedValueOnce(
        new ApiError(409, 'TASK_CONFLICT', 'Task already finished'),
      );
      vi.spyOn(api.tasks, 'get').mockResolvedValueOnce({
        task_id: 'task-run-1',
        session_id: 'sess-1',
        workspace_id: 'ws-1',
        task_goal: '目标',
        permission_level: 'workspace_write',
        status: 'succeeded',
        created_at: 1000,
      } as unknown as Task);

      const res = await useAppStore
        .getState()
        .sendTaskMessage('task-run-1', '补充信息');

      expect(res.ok).toBe(false);
      expect(res.conflict).toBe(true);
      expect(useAppStore.getState().notice?.message).toContain('任务已结束，消息未送达');
    });

    it('renders in-flight message composer banner and allows sending during running task', async () => {
      const activeRun: RunState = {
        ...emptyRun('task-active-1', '运行中任务'),
        status: 'running',
      };
      useAppStore.setState({ runSession: 'sess-1', run: activeRun });

      const sendSpy = vi.spyOn(api.tasks, 'sendMessage').mockResolvedValueOnce({
        message_id: 'msg-2',
        task_id: 'task-active-1',
        content: '修改 nginx 配置',
        status: 'queued',
        created_at: 1000,
      });

      renderWithClient(
        <Composer
          sessionId="sess-1"
          workspaceId="ws-1"
          busy={true}
          contextPercent={20}
          onSubmit={vi.fn()}
          onCancel={vi.fn()}
        />,
      );

      expect(
        screen.getByText(/任务运行中：补充纠正信息将排队进入收件箱，在下一次模型调用前合入。当前执行中的工具不会被立即中断；发送消息不代表批准操作。/),
      ).toBeDefined();

      const textarea = screen.getByPlaceholderText('补充纠正信息（如：刚才端口说错了，是 8081）...');
      fireEvent.change(textarea, { target: { value: '修改 nginx 配置' } });

      const submitBtn = screen.getByRole('button', { name: '发送补充消息' });
      fireEvent.click(submitBtn);

      await waitFor(() => {
        expect(sendSpy).toHaveBeenCalledWith('task-active-1', '修改 nginx 配置');
      });
    });
  });

  describe('4. Subagent Real Outcomes & Citations', () => {
    it('parses structured subagent report text cleanly without hallucinating IDs', () => {
      const reportText = `
子任务 ID: subtask_abc123
角色: 诊断专家
状态: partial
1. 发现端口 3306 被本地 mysqld 占用
引用[tool_call]: call_netstat_1 — netstat -tlpn
（1 条引用不在实际访问记录里，已丢弃）
2. 发现外部连接被 iptables 丢弃（未经验证）
未解决：
- 未排查 SELinux 状态
- 未确认远程客户端 IP
护栏标注：
- 尝试执行 iptables -F 被拦截
（5 步，3 次工具调用，450 Token，用时 1.2 秒）
artifact://subtask_abc123_report
      `;

      const parsed = parseSubagentReport(reportText);
      expect(parsed.subtaskId).toBe('subtask_abc123');
      expect(parsed.artifactId).toBe('subtask_abc123_report');
      expect(parsed.status).toBe('partial');
      expect(parsed.findings).toHaveLength(2);
      expect(parsed.findings[0]?.verified).toBe(true);
      expect(parsed.findings[0]?.citations[0]?.ref).toBe('call_netstat_1');
      expect(parsed.findings[0]?.droppedCount).toBe(1);
      expect(parsed.findings[1]?.verified).toBe(false);
      expect(parsed.unresolved).toEqual(['未排查 SELinux 状态', '未确认远程客户端 IP']);
      expect(parsed.warnings).toEqual(['尝试执行 iptables -F 被拦截']);
      expect(parsed.stats?.steps).toBe(5);
    });

    it('renders subagent tool card with findings, dropped citations, and artifact link', () => {
      const report = `
子任务 ID: sub_xyz
角色: 安全核查
状态: completed
1. 系统存在异常监听
引用[call]: c_ss — ss -ant
      `;

      renderWithClient(
        <ToolCard
          taskId="task-1"
          item={mockTool({
            callId: 'call-sub-1',
            tool: 'spawn_subagent',
            args: { role: '安全核查', goal: '排查后门' },
            result: report,
          })}
        />,
      );

      // Open details
      fireEvent.click(screen.getByRole('button', { name: /spawn_subagent/ }));

      expect(screen.getByText('子 AGENT 委派 · 角色: 安全核查')).toBeDefined();
      expect(screen.getByText('排查后门')).toBeDefined();
      expect(screen.getByText('sub_xyz')).toBeDefined();
      expect(screen.getByText('全部完成')).toBeDefined();
      expect(screen.getByText('系统存在异常监听')).toBeDefined();
      expect(screen.getByText('引用 [call]:')).toBeDefined();
      expect(screen.getByText('c_ss')).toBeDefined();
    });
  });

  describe('5. Evidence Tool Cards (record_verification, report_outcome, read_task_record, note)', () => {
    it('renders record_verification tool card accurately', () => {
      renderWithClient(
        <ToolCard
          taskId="task-1"
          item={mockTool({
            callId: 'call-ver-1',
            tool: 'record_verification',
            args: {
              goal: '确认服务已恢复',
              check: 'curl -I http://127.0.0.1:80',
              summary: '返回 HTTP 200',
              result: 'passed',
              evidence_call_ids: ['c-curl-80'],
            },
            result: 'Verification recorded',
          })}
        />,
      );

      fireEvent.click(screen.getByRole('button', { name: /record_verification/ }));

      expect(screen.getByText('目标验证记录 (record_verification)')).toBeDefined();
      expect(screen.getByText('验证通过 (passed)')).toBeDefined();
      expect(screen.getByText('确认服务已恢复')).toBeDefined();
      expect(screen.getByText('curl -I http://127.0.0.1:80')).toBeDefined();
      expect(screen.getByText('#c-curl-80')).toBeDefined();
    });

    it('renders report_outcome tool card accurately', () => {
      renderWithClient(
        <ToolCard
          taskId="task-1"
          item={mockTool({
            callId: 'call-rep-1',
            tool: 'report_outcome',
            args: {
              status: 'needs_input',
              summary: '需要数据库 root 密码以完成迁移',
              remaining: ['获取并输入数据库密码'],
            },
            result: 'Outcome saved',
          })}
        />,
      );

      fireEvent.click(screen.getByRole('button', { name: /report_outcome/ }));

      expect(screen.getByText('任务最终结论 (report_outcome)')).toBeDefined();
      expect(screen.getByText('需要补充信息')).toBeDefined();
      expect(screen.getByText('需要数据库 root 密码以完成迁移')).toBeDefined();
      expect(screen.getByText('获取并输入数据库密码')).toBeDefined();
    });

    it('renders read_task_record tool card and handles truncation alerts', () => {
      const recordsJson = JSON.stringify({
        records: [
          {
            index: 1,
            role: 'assistant',
            name: 'bash',
            call_id: 'c-old-1',
            text: 'iostat 采集结果截断示例...',
            offset: 0,
            total_chars: 5000,
            next_offset: 200,
          },
        ],
        next_start: 5,
      });

      renderWithClient(
        <ToolCard
          taskId="task-1"
          item={mockTool({
            callId: 'call-read-1',
            tool: 'read_task_record',
            args: {
              query: 'iostat',
              call_id: 'c-old-1',
              start: 0,
              limit: 5,
            },
            result: recordsJson,
          })}
        />,
      );

      fireEvent.click(screen.getByRole('button', { name: /read_task_record/ }));

      expect(screen.getByText('原始记录回读 (read_task_record)')).toBeDefined();
      expect(screen.getByText('匹配到 1 条记录')).toBeDefined();
      expect(screen.getByText('#c-old-1')).toBeDefined();
      expect(screen.getByText(/当前正文已截断（已显示 0–200 \/ 共 5000 字符）/)).toBeDefined();
      expect(screen.getByText(/达到单次 limit 上限，还有后续记录（next_start=5）/)).toBeDefined();
    });

    it('distinguishes note memory kinds: experience vs historical fact', () => {
      // 1. Fact note
      const { unmount } = renderWithClient(
        <ToolCard
          taskId="task-1"
          item={mockTool({
            callId: 'call-note-1',
            tool: 'note',
            args: {
              kind: 'fact',
              text: '发现端口 8000 曾被 nodejs 监听',
            },
            result: 'Note saved',
          })}
        />,
      );

      fireEvent.click(screen.getByRole('button', { name: /note/ }));
      expect(screen.getByText('历史现场观察（非当前状态）')).toBeDefined();
      expect(screen.getByText(/此为历史任务记录的现场观察，非当前服务器状态/)).toBeDefined();
      expect(screen.getByText('发现端口 8000 曾被 nodejs 监听')).toBeDefined();

      unmount();

      // 2. Experience note
      renderWithClient(
        <ToolCard
          taskId="task-1"
          item={mockTool({
            callId: 'call-note-2',
            tool: 'note',
            args: {
              kind: 'experience',
              text: 'CentOS 7 下重启 firewalld 会清空 docker 链',
            },
            result: 'Note saved',
          })}
        />,
      );

      fireEvent.click(screen.getByRole('button', { name: /note/ }));
      expect(screen.getByText('稳定经验（待结合现场验证）')).toBeDefined();
      expect(screen.getByText(/稳定经验：可跨会话复用，但应用于生产环境前应重新验证现场条件/)).toBeDefined();
      expect(screen.getByText('CentOS 7 下重启 firewalld 会清空 docker 链')).toBeDefined();
    });
  });
});
