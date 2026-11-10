import { vi } from 'vitest';

type Handler = (init: RequestInit, url: URL) => { status?: number; body?: unknown } | Promise<{ status?: number; body?: unknown }>;

export interface Call { method: string; path: string; search: string; init: RequestInit }

/** Route table keyed by "METHOD /path" (query string ignored). Unknown routes are 404. */
export function mockFetch(routes: Record<string, Handler | { status?: number; body?: unknown }>) {
  const calls: Call[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), 'http://localhost');
    const method = (init.method ?? 'GET').toUpperCase();
    calls.push({ method, path: url.pathname, search: url.search, init });
    const route = routes[`${method} ${url.pathname}`];
    const result = typeof route === 'function' ? await route(init, url) : route ?? { status: 404, body: { detail: 'Not found.', code: 'not_found' } };
    const status = result.status ?? 200;
    return new Response(status === 204 ? null : JSON.stringify(result.body ?? {}), {
      status, headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fn);
  return calls;
}

export const counts = {
  total_clients: 7, active_clients: 6, archived_clients: 1, pending_invitations: 3, active_projects: 4,
  completed_projects: 2, open_tasks: 31, completed_tasks: 9, pending_jobs: 11, running_jobs: 1, dead_jobs: 2,
};

export const dashboard = {
  counts,
  active_projects: [{ id: 'p1', name: 'Acme Website', status: 'active', due_date: '2026-12-01', created_at: '2026-11-01T10:00:00Z',
    client_id: 'c1', client_name: 'Acme', progress: { total_tasks: 10, completed_tasks: 3, open_tasks: 7, percent: 30 } }],
  dead_jobs: [{ id: 'j1', job_type: 'clickup.create_task', subject_type: 'task', subject_id: 't1', attempts: 1,
    last_error_code: 'CLICKUP_UNAUTHORIZED', updated_at: '2026-11-07T10:00:00Z' }],
  recent_activity: [{ id: 'a1', event_type: 'crm.deal_received', summary: 'Deal won in CRM: Acme Website', actor_type: 'crm',
    client_visible: false, created_at: '2026-11-07T09:00:00Z', client_id: 'c1', project_id: null, client_name: 'Acme', project_name: null }],
};
