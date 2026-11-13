import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Sidebar } from './Sidebar';

vi.mock('@tanstack/react-router', () => ({
  Link: ({
    children,
    to,
    ...props
  }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { to?: string }) => (
    <a href={to} {...props}>
      {children}
    </a>
  ),
  useNavigate: () => vi.fn(),
  useParams: () => ({ sessionId: 'sess-normal' }),
  useRouterState: () => ({ location: { pathname: '/s/sess-normal' } }),
}));

describe('Sidebar Attention Section Deduplication', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
      },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    queryClient.clear();
  });

  it('renders attention session under 待处理 and prevents duplicate row in main list', async () => {
    const mockSessions = [
      {
        session_id: 'sess-attention',
        workspace_id: 'ws-1',
        title: '磁盘空间清理',
        created_at: 1000,
        updated_at: 2000,
        pinned: false,
        archived: false,
      },
      {
        session_id: 'sess-normal',
        workspace_id: 'ws-2',
        title: 'Linux深度巡检',
        created_at: 1000,
        updated_at: 1500,
        pinned: false,
        archived: false,
      },
    ];

    const fetchMock = vi.fn(async (url: string) => {
      const urlStr = String(url);
      if (urlStr.includes('/sessions')) {
        if (urlStr.includes('/tasks')) {
          if (urlStr.includes('sess-attention')) {
            return new Response(
              JSON.stringify([
                {
                  task_id: 't-1',
                  task_goal: '清理磁盘',
                  status: 'waiting_for_approval',
                  created_at: 2000,
                  updated_at: 2000,
                },
              ]),
              { status: 200, headers: { 'content-type': 'application/json' } },
            );
          }
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        }
        return new Response(JSON.stringify(mockSessions), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <QueryClientProvider client={queryClient}>
        <Sidebar collapsedRail={false} />
      </QueryClientProvider>,
    );

    // Wait for sessions to load
    await waitFor(() => {
      expect(screen.getByText('Linux深度巡检')).toBeInTheDocument();
    });

    // Wait for activity query to mark sess-attention as waiting_for_approval
    await waitFor(() => {
      expect(screen.getByText(/待处理 \(1\)/)).toBeInTheDocument();
    });

    // "磁盘空间清理" should appear EXACTLY ONCE in the sidebar (under 待处理, not duplicated below)
    const attentionRows = screen.getAllByText('磁盘空间清理');
    expect(attentionRows).toHaveLength(1);

    // "Linux深度巡检" should also appear once in the regular list
    const normalRows = screen.getAllByText('Linux深度巡检');
    expect(normalRows).toHaveLength(1);
  });

  it('triggers in-place settings modal without URL navigation when settings button is clicked', async () => {
    const { useUiStore } = await import('../../stores/useUiStore');
    useUiStore.setState({ settingsOpen: false });

    render(
      <QueryClientProvider client={queryClient}>
        <Sidebar collapsedRail={false} />
      </QueryClientProvider>,
    );

    const settingsBtn = screen.getByRole('button', { name: '系统配置 (Ctrl+,)' });
    expect(settingsBtn).toBeInTheDocument();
    // It should not be wrapped inside an <a> tag linking to /settings
    expect(settingsBtn.closest('a')).toBeNull();

    settingsBtn.click();
    expect(useUiStore.getState().settingsOpen).toBe(true);
  });
});
