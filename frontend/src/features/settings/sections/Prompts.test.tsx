import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Prompts } from './Prompts';

// Mock clipboard
Object.assign(navigator, {
  clipboard: {
    writeText: vi.fn().mockImplementation(() => Promise.resolve()),
  },
});

vi.mock('../../../api/endpoints', () => ({
  api: {
    prompts: {
      list: vi.fn().mockResolvedValue([]),
      get: vi.fn().mockImplementation((name: string) =>
        Promise.resolve({
          name,
          content: `Mock content for ${name}`,
          default: `Default mock content for ${name}`,
          customized: false,
          max_chars: 20000,
        }),
      ),
      save: vi.fn().mockImplementation((name: string, content: string) =>
        Promise.resolve({
          name,
          content,
          default: `Default mock content for ${name}`,
          customized: true,
          max_chars: 20000,
        }),
      ),
      reset: vi.fn().mockImplementation((name: string) =>
        Promise.resolve({
          name,
          content: `Default mock content for ${name}`,
          default: `Default mock content for ${name}`,
          customized: false,
          max_chars: 20000,
        }),
      ),
    },
  },
}));

function renderWithClient(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      {ui}
    </QueryClientProvider>,
  );
}

describe('Prompts Section (双栏宽屏高反差图纸)', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  it('renders top-right lightweight KV Cache capsule', () => {
    renderWithClient(<Prompts />);
    expect(screen.getByText(/KV CACHE: LOCKED \(75\.5% HIT\)/i)).toBeInTheDocument();
  });

  it('renders 28px physical segmented switcher with 3 options and no middle 11-item nav', () => {
    renderWithClient(<Prompts />);

    // Switcher tabs
    expect(screen.getByRole('tab', { name: /SRE 核心工作法/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /自定义机房规则/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /内部管道/i })).toBeInTheDocument();

    // Verify obsolete 3rd column categories do NOT exist
    expect(screen.queryByText('智能体核心模板')).not.toBeInTheDocument();
    expect(screen.queryByText('实时动态上下文')).not.toBeInTheDocument();
    expect(screen.queryByText('安全合规防御')).not.toBeInTheDocument();
  });

  it('Tab 1 (SRE 核心工作法): shows preset baseline, protected badge, and copy button', async () => {
    renderWithClient(<Prompts />);

    // Default tab is Tab 1 (preset)
    expect(screen.getByText('PRESET · PROTECTED')).toBeInTheDocument();
    expect(screen.getByText(/核心协议只读保护/i)).toBeInTheDocument();

    // Textarea is read-only
    const textarea = screen.getByLabelText('SRE 核心工作法全文');
    expect(textarea).toHaveAttribute('readonly');

    // Click Copy button
    const copyBtn = screen.getByRole('button', { name: /复制 SRE 基线全文/i });
    await waitFor(() => {
      fireEvent.click(copyBtn);
    });
    expect(navigator.clipboard.writeText).toHaveBeenCalled();
  });

  it('Tab 2 (自定义机房规则): switches to custom rules editor, allows editing and saving', async () => {
    renderWithClient(<Prompts />);

    // Switch to Tab 2
    fireEvent.click(screen.getByRole('tab', { name: /自定义机房规则/i }));

    expect(screen.getByText(/机房运维与安全约束规范 \(Custom Rules\)/i)).toBeInTheDocument();
    expect(screen.getByText('WORKSPACE · EDITABLE')).toBeInTheDocument();

    const editor = screen.getByLabelText('自定义机房规则编辑框');
    expect(editor).not.toHaveAttribute('readonly');

    // Edit content
    fireEvent.change(editor, { target: { value: '# 自定义规则测试\n禁止执行 rm -rf' } });
    expect(screen.getByText('● 未保存更改')).toBeInTheDocument();

    // Click Save button
    const saveBtn = screen.getByRole('button', { name: /保存规则/i });
    fireEvent.click(saveBtn);

    expect(localStorage.getItem('linuxops_custom_project_rules')).toContain('禁止执行 rm -rf');
    expect(screen.getByText('● 已保存')).toBeInTheDocument();
  });

  it('Tab 3 (内部管道): opens dropdown menu and selects internal pipeline templates', async () => {
    renderWithClient(<Prompts />);

    // Click dropdown button
    const dropdownBtn = screen.getByRole('tab', { name: /内部管道/i });
    fireEvent.click(dropdownBtn);

    // Dropdown options visible
    expect(screen.getByRole('menuitem', { name: /长会话压缩/i })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /任务内压缩/i })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /子智能体汇报/i })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /联网搜索·提炼/i })).toBeInTheDocument();

    // Select "任务内压缩"
    fireEvent.click(screen.getByRole('menuitem', { name: /任务内压缩/i }));

    // Editor switches to selected internal pipeline
    await waitFor(() => {
      expect(screen.getByText(/任务内压缩 \(task_compactor\.md\)/i)).toBeInTheDocument();
    });
    expect(screen.getByText('INTERNAL · PIPELINE')).toBeInTheDocument();
  });
});
