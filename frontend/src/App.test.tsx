import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { dashboard, mockFetch } from './test/fetchMock';

beforeEach(() => window.history.replaceState(null, '', '/admin'));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('route protection and login', () => {
  it('shows the sign-in form when there is no session, then the dashboard after login', async () => {
    let authed = false;
    const calls = mockFetch({
      'GET /api/admin/me': () => (authed ? { body: { authenticated: true } } : { status: 401, body: { code: 'admin_auth_required' } }),
      'POST /api/admin/login': (init) => {
        authed = JSON.parse(String(init.body)).password === 'right';
        return authed ? { body: { authenticated: true } } : { status: 401, body: { detail: 'Incorrect password.', code: 'invalid_credentials' } };
      },
      'GET /api/admin/dashboard': () => (authed ? { body: dashboard } : { status: 401, body: {} }),
    });
    render(<App />);
    const password = await screen.findByLabelText('Administrator password');
    expect(screen.queryByText('Dashboard')).toBeNull();

    fireEvent.change(password, { target: { value: 'wrong' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect((await screen.findByRole('alert')).textContent).toBe('That password is not correct.');
    expect((screen.getByLabelText('Administrator password') as HTMLInputElement).value).toBe('');

    fireEvent.change(screen.getByLabelText('Administrator password'), { target: { value: 'right' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeTruthy();
    expect(calls.filter((c) => c.path === '/api/admin/dashboard').length).toBeGreaterThan(0);
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it('returns to sign-in when an API call reports 401', async () => {
    mockFetch({ 'GET /api/admin/me': { body: { authenticated: true } }, 'GET /api/admin/dashboard': { status: 401, body: {} } });
    render(<App />);
    expect(await screen.findByLabelText('Administrator password')).toBeTruthy();
  });
});

describe('dashboard', () => {
  it('renders the numbers the API returns (nothing hard-coded)', async () => {
    mockFetch({ 'GET /api/admin/me': { body: {} }, 'GET /api/admin/dashboard': { body: dashboard } });
    render(<App />);
    const pipeline = await screen.findByRole('region', { name: 'Delivery pipeline' });
    const stages = within(pipeline).getAllByRole('link').map((l) => l.textContent);
    expect(stages).toEqual([
      '3Waiting for onboardinginvitations sent, form not submitted',
      '4Active projects2 completed',
      '31Open tasks9 done',
      '2Integration jobs failed12 waiting',  // failures lead when there are any
    ]);
    expect(screen.getByText('7 clients (6 active, 1 archived)')).toBeTruthy();
    expect(screen.getByText('CLICKUP_UNAUTHORIZED')).toBeTruthy();
    expect(screen.getByText('Deal won in CRM: Acme Website')).toBeTruthy();
  });

  it('shows loading, then an error as plain text with a retry', async () => {
    let fail = true;
    mockFetch({
      'GET /api/admin/me': { body: {} },
      'GET /api/admin/dashboard': () => (fail ? { status: 500, body: { detail: '<img src=x onerror="alert(1)">broken' } } : { body: dashboard }),
    });
    const { container } = render(<App />);
    expect(await screen.findByText('Loading…')).toBeTruthy();
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('<img src=x onerror="alert(1)">broken');
    expect(container.querySelector('img')).toBeNull();
    fail = false;
    fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('region', { name: 'Delivery pipeline' })).toBeTruthy();
  });

  it('shows empty states on a fresh install', async () => {
    mockFetch({ 'GET /api/admin/me': { body: {} }, 'GET /api/admin/dashboard': {
      body: { counts: Object.fromEntries(Object.keys(dashboard.counts).map((k) => [k, 0])), active_projects: [], dead_jobs: [], recent_activity: [] } } });
    render(<App />);
    expect(await screen.findByText('No failed jobs. Nothing needs attention.')).toBeTruthy();
    expect(screen.getByText('Integration jobs waiting')).toBeTruthy();
    expect(screen.getByText('none failed')).toBeTruthy();
    expect(screen.getByText('No activity yet.')).toBeTruthy();
  });
});

describe('integration retry', () => {
  const job = {
    id: 'j1', job_type: 'clickup.create_task', subject_type: 'task', subject_id: 'task-uuid', status: 'dead', attempts: 1,
    max_attempts: 5, next_run_at: '2026-11-07T10:00:00Z', last_error_code: 'UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION',
    last_error: 'outcome unknown', created_at: '2026-11-07T10:00:00Z', completed_at: null, updated_at: '2026-11-07T10:00:00Z',
    project_id: 'p1', project_name: 'Acme', task_title: 'Kickoff', idempotency_key: 'k', locked_by: null, locked_until: null,
    reconciliation: null, resolution: null, requires_reconciliation: true,
    attempt_history: [{ attempt_number: 1, outcome: 'unknown', worker_id: 'w', started_at: '2026-11-07T10:00:00Z',
      finished_at: '2026-11-07T10:00:01Z', http_status: null, error_code: 'CLICKUP_CREATE_OUTCOME_UNKNOWN',
      error_message: 'ReadTimeout after sending the create request.', external_id: null, duration_ms: 15000 }],
  };
  const status = { clickup: { implemented: true, credentials_present: false, configuration_valid: false, configuration_error: 'CLICKUP_NOT_CONFIGURED',
    live_verification: 'not_verified', last_success_at: null, jobs: { dead: 1 }, two_way_sync: false },
    google_sheets: { implemented: true, service_account_file_configured: false, spreadsheet_id_configured: false, worksheet_configured: true, credentials_present: false, configuration_valid: false, configuration_error: 'SHEETS_NOT_CONFIGURED', live_verification: 'not_verified', last_success_at: null, jobs: { pending: 2 } },
    slack: { implemented: true, credentials_present: false, configuration_valid: false, configuration_error: 'SLACK_NOT_CONFIGURED',
      live_verification: 'not_verified', last_success_at: null, jobs: {} } };

  it('requires the reconciliation confirmation before retrying an unknown outcome', async () => {
    window.history.replaceState(null, '', '/admin/integrations?job=j1');
    const calls = mockFetch({
      'GET /api/admin/me': { body: {} },
      'GET /api/admin/integrations/status': { body: status },
      'GET /api/admin/integration-jobs/j1': { body: job },
      'POST /api/admin/integration-jobs/j1/retry': { body: { ...job, status: 'pending', requires_reconciliation: false } },
    });
    render(<App />);
    expect(await screen.findByText(/\[LTD-TASK:task-uuid\]/)).toBeTruthy();
    expect(screen.getByText('Not configured: set CLICKUP_API_TOKEN and CLICKUP_LIST_ID.')).toBeTruthy();
    expect(screen.getByText('Not configured: set GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_SHEETS_SPREADSHEET_ID.')).toBeTruthy();
    const retry = screen.getByRole('button', { name: 'Retry job' }) as HTMLButtonElement;
    expect(retry.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(/I checked the other system/));
    expect(retry.disabled).toBe(false);
    fireEvent.click(retry);
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    const post = calls.find((c) => c.method === 'POST')!;
    expect(JSON.parse(String(post.init.body))).toEqual({ additional_attempts: 3, reconciliation_confirmed: true });
  });
});

describe('slack unknown outcome', () => {
  const slackJob = {
    id: 's1', job_type: 'slack.notify', subject_type: 'project', subject_id: 'p1', status: 'dead', attempts: 1,
    max_attempts: 5, next_run_at: '2026-11-07T10:00:00Z', last_error_code: 'UNKNOWN_OUTCOME_REQUIRES_RECONCILIATION',
    last_error: 'SLACK_DELIVERY_UNKNOWN: outcome unknown', created_at: '2026-11-07T10:00:00Z', completed_at: null,
    updated_at: '2026-11-07T10:00:00Z', project_id: 'p1', project_name: 'Acme Website', task_title: null,
    idempotency_key: 'k', locked_by: null, locked_until: null, reconciliation: null, resolution: null,
    requires_reconciliation: true, attempt_history: [],
  };
  const status = { clickup: { implemented: true, credentials_present: true, configuration_valid: true, configuration_error: null,
    live_verification: 'not_verified', last_success_at: null, jobs: {}, two_way_sync: false },
    google_sheets: { implemented: true, service_account_file_configured: true, spreadsheet_id_configured: true, worksheet_configured: true, credentials_present: true, configuration_valid: false, configuration_error: 'SHEETS_INVALID_CREDENTIALS', live_verification: 'not_verified', last_success_at: null, jobs: {} },
    slack: { implemented: true, credentials_present: true, configuration_valid: true, configuration_error: null,
      live_verification: 'verified_by_successful_job', last_success_at: '2026-11-07T09:00:00Z', jobs: { dead: 1 } } };

  it('offers separate "absent: retry" and "present: mark delivered" paths', async () => {
    window.history.replaceState(null, '', '/admin/integrations?job=s1');
    const calls = mockFetch({
      'GET /api/admin/me': { body: {} },
      'GET /api/admin/integrations/status': { body: status },
      'GET /api/admin/integration-jobs/s1': { body: slackJob },
      'POST /api/admin/integration-jobs/s1/resolve-delivered': { body: { ...slackJob, status: 'succeeded' } },
    });
    render(<App />);
    expect(await screen.findByText(/Working: Slack last accepted a message/)).toBeTruthy();
    expect(screen.getByText('Configuration problem: SHEETS INVALID CREDENTIALS.')).toBeTruthy();
    expect(screen.getByLabelText('I checked the channel and the message is not there')).toBeTruthy();
    const mark = screen.getByRole('button', { name: 'Mark as delivered' }) as HTMLButtonElement;
    expect(mark.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(/I found this project's "New project ready" message/));
    fireEvent.click(mark);
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.path).toBe('/api/admin/integration-jobs/s1/resolve-delivered');
    expect(JSON.parse(String(post.init.body))).toEqual({ message_found_in_channel: true });
    expect(calls.some((c) => c.path.endsWith('/retry'))).toBe(false);
  });
});

describe('access link regeneration', () => {
  it('shows the new link once with a copy action', async () => {
    window.history.replaceState(null, '', '/admin/clients/c1');
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const client = { id: 'c1', name: 'Acme', company: 'Acme Co', email: 'a@acme.example', phone: null, status: 'active',
      crm_source: 'generic', crm_external_id: 'x', created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z',
      invitations: [{ id: 'i1', status: 'submitted', service_type: 'website_build', deal_name: 'Website', crm_source: 'generic',
        crm_deal_id: 'd1', deal_value_cents: 250000, currency: 'USD', expires_at: '2026-12-01T00:00:00Z', submitted_at: '2026-11-02T00:00:00Z',
        created_at: '2026-11-01T00:00:00Z', access_link_expires_at: null, project_id: 'p1' }],
      projects: [], activity: [] };
    mockFetch({
      'GET /api/admin/me': { body: {} },
      'GET /api/admin/clients/c1': { body: client },
      'POST /api/admin/invitations/i1/regenerate-access-link': { status: 201, body: {
        access_url: 'https://hub.example/portal#t=abc123', expires_at: '2027-05-01T00:00:00Z', kind: 'project_access' } },
    });
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'New project link' }));
    const input = await screen.findByLabelText('New access link') as HTMLInputElement;
    expect(input.value).toBe('https://hub.example/portal#t=abc123');
    expect(screen.getByText(/shown only once/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(screen.queryByLabelText('New access link')).toBeNull();
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
});
