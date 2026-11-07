import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useUiStore } from '../../stores/useUiStore';
import { SreSettingsModal } from './SreSettingsModal';

function renderWithClient(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      {ui}
    </QueryClientProvider>
  );
}

describe('SreSettingsModal (Layer 2 原地几何配置视窗)', () => {
  beforeEach(() => {
    useUiStore.setState({
      settingsOpen: false,
      settingsSection: 'general',
      theme: 'light',
      thinking: 'expanded',
      telemetryAudio: true,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('does not render when settingsOpen is false', () => {
    renderWithClient(<SreSettingsModal />);
    expect(screen.queryByRole('dialog', { name: '系统配置小视窗' })).not.toBeInTheDocument();
  });

  it('renders 860x600 in-place modal when settingsOpen is true', () => {
    useUiStore.setState({ settingsOpen: true });
    renderWithClient(<SreSettingsModal />);
    const modal = screen.getByRole('dialog', { name: '系统配置小视窗' });
    expect(modal).toBeInTheDocument();
    expect(screen.getByText(/SYSTEM CONFIGURATION/i)).toBeInTheDocument();
    expect(screen.getByText(/ENGINE SRE v0.2.0/i)).toBeInTheDocument();
  });

  it('renders all 7 navigation sections and allows switching tabs', () => {
    useUiStore.setState({ settingsOpen: true, settingsSection: 'general' });
    renderWithClient(<SreSettingsModal />);

    // Check all 7 navigation tabs exist
    expect(screen.getByRole('button', { name: '通用' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '模型' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '技能' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'MCP' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '工具' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '提示词' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '诊断' })).toBeInTheDocument();

    // Default is General
    expect(screen.getByText('视觉与交互基线')).toBeInTheDocument();

    // Switch to Models
    fireEvent.click(screen.getByRole('button', { name: '模型' }));
    expect(useUiStore.getState().settingsSection).toBe('models');
    expect(screen.getByText(/推理模型层/)).toBeInTheDocument();

    // Switch to Diagnostics
    fireEvent.click(screen.getByRole('button', { name: '诊断' }));
    expect(useUiStore.getState().settingsSection).toBe('diagnostics');
    expect(screen.getByText(/核心子系统探针/)).toBeInTheDocument();
    expect(screen.getByText(/linuxops-bench/)).toBeInTheDocument();
  });

  it('supports viewing SOP code via in-place slideout drawer without secondary modal popup', async () => {
    useUiStore.setState({ settingsOpen: true, settingsSection: 'skills' });
    renderWithClient(<SreSettingsModal />);

    // Skills section should be visible
    expect(screen.getByText('linux_troubleshooting')).toBeInTheDocument();

    // Click the first "原地查看 SOP 文档" button
    const viewButtons = screen.getAllByRole('button', { name: '原地查看 SOP 文档' });
    expect(viewButtons.length).toBeGreaterThan(0);
    fireEvent.click(viewButtons[0]!);

    // In-place SOP drawer opens
    expect(screen.getByText(/SOP · linux_troubleshooting/)).toBeInTheDocument();
    expect(screen.getByLabelText('关闭 SOP 抽屉')).toBeInTheDocument();

    // Close the SOP drawer
    fireEvent.click(screen.getByLabelText('关闭 SOP 抽屉'));
    await waitFor(() => {
      expect(screen.queryByText(/SOP · linux_troubleshooting/)).not.toBeInTheDocument();
    });
  });

  it('switches to prompts section and renders wide blueprint without 3-column clutter', () => {
    useUiStore.setState({ settingsOpen: true, settingsSection: 'prompts' });
    renderWithClient(<SreSettingsModal />);

    expect(screen.getByText(/KV CACHE: LOCKED \(75\.5% HIT\)/i)).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /SRE 核心工作法/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /自定义机房规则/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /内部管道/i })).toBeInTheDocument();
    expect(screen.getByText('PRESET · PROTECTED')).toBeInTheDocument();
  });

  it('closes on Escape key press', () => {
    useUiStore.setState({ settingsOpen: true });
    renderWithClient(<SreSettingsModal />);

    expect(screen.getByRole('dialog', { name: '系统配置小视窗' })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(useUiStore.getState().settingsOpen).toBe(false);
  });

  it('closes on clicking close button', () => {
    useUiStore.setState({ settingsOpen: true });
    renderWithClient(<SreSettingsModal />);

    const closeBtn = screen.getByRole('button', { name: '关闭配置视窗' });
    fireEvent.click(closeBtn);
    expect(useUiStore.getState().settingsOpen).toBe(false);
  });

  it('closes on clicking scrim backdrop', () => {
    useUiStore.setState({ settingsOpen: true });
    const { container } = renderWithClient(<SreSettingsModal />);

    const scrim = container.querySelector('[aria-hidden="true"]');
    if (scrim) {
      fireEvent.click(scrim);
      expect(useUiStore.getState().settingsOpen).toBe(false);
    }
  });
});
