import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { mockFetch, type Call } from '../test/fetchMock';
import schema from './fixtures/website_build.schema.json';
import { resetToken } from './token';
import type { PortalMe, PortalProject } from './types';

const TOKEN = 'Abcdefghijklmnopqrstuvwxyz0123456789_-ABCDE';

const me = (next: PortalMe['next_step'], status: PortalMe['invitation']['status'] = 'pending'): PortalMe => ({
  client: { name: 'Dana Example', company: 'Example Studio' },
  invitation: { status, service_type: 'website_build', template_name: 'Website build', deal_name: 'Example site',
                expires_at: '2026-12-10T10:00:00Z', submitted_at: status === 'submitted' ? '2026-11-10T10:00:00Z' : null },
  next_step: next,
});

const project: PortalProject = {
  id: '6f1c2b8e-0000-4000-8000-000000000001', name: 'Example site', status: 'active', description: 'A new site.',
  start_date: '2026-11-10', due_date: '2027-01-15', created_at: '2026-11-10T10:00:00Z',
  progress: { total_tasks: 10, completed_tasks: 3, open_tasks: 7, percent: 30 },
  milestones: [
    { name: 'Discovery', position: 1, status: 'done', due_date: '2026-11-20',
      tasks: [{ title: 'Kick-off call', description: null, status: 'done', due_date: null, completed_at: '2026-11-11T09:00:00Z' }] },
    { name: 'Design', position: 2, status: 'in_progress', due_date: '2026-12-10',
      tasks: [{ title: 'Homepage design', description: null, status: 'in_progress', due_date: '2026-12-01', completed_at: null }] },
  ],
  unassigned_tasks: [],
  activity: [{ type: 'project.created', summary: 'Project created from your onboarding answers', created_at: '2026-11-10T10:00:00Z' }],
};

const onboardingForm = { status: 'pending', service_type: 'website_build', template_name: 'Website build',
                         submitted_at: null, form_schema: schema, prefill: { project_name: 'Example site' } };

function open(path: string) {
  window.history.replaceState(null, '', path);
}

beforeEach(() => resetToken());
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); });

function expectTokenOnlyInHeader(calls: Call[]) {
  expect(calls.length).toBeGreaterThan(0);
  for (const c of calls) {
    expect(c.path.startsWith('/api/portal/')).toBe(true);
    expect(c.path + c.search).not.toContain(TOKEN);
    expect((c.init.headers as Record<string, string>).Authorization).toBe(`Bearer ${TOKEN}`);
    expect(c.init.credentials).toBe('omit');
    expect(c.init.referrerPolicy).toBe('no-referrer');
    expect(c.init.cache).toBe('no-store');
  }
}

describe('invitation link handling', () => {
  it('asks for the link when none is present and calls no API', async () => {
    const calls = mockFetch({});
    open('/portal');
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Open your access link' })).toBeTruthy();
    expect(calls).toHaveLength(0);
  });

  it('reads the token from the fragment, removes it from the address bar and never stores it', async () => {
    const calls = mockFetch({ 'GET /api/portal/me': { body: me('project', 'submitted') },
                              'GET /api/portal/project': { body: project } });
    open(`/portal#t=${TOKEN}`);
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Example site' })).toBeTruthy();
    expect(window.location.hash).toBe('');
    expect(window.location.href).not.toContain(TOKEN);
    expect(localStorage.length + sessionStorage.length).toBe(0);
    expect(document.cookie).not.toContain(TOKEN);
    expectTokenOnlyInHeader(calls);
    expect(calls.some((c) => c.path.startsWith('/api/admin'))).toBe(false);
  });

  it('picks up a link opened in a tab that already shows the portal', async () => {
    const calls = mockFetch({ 'GET /api/portal/me': { body: me('project', 'submitted') },
                              'GET /api/portal/project': { body: project } });
    open('/portal');
    render(<App />);
    await screen.findByRole('heading', { name: 'Open your access link' });
    window.history.replaceState(null, '', `/portal#t=${TOKEN}`);
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    expect(await screen.findByRole('heading', { name: 'Example site' })).toBeTruthy();
    expect(window.location.hash).toBe('');
    expectTokenOnlyInHeader(calls);
  });

  it('rejects a malformed token without calling the API', async () => {
    const calls = mockFetch({});
    open('/portal#t=short');
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'This link is not valid' })).toBeTruthy();
    expect(calls).toHaveLength(0);
  });

  it.each([
    ['token_expired', 'This link has expired'],
    ['token_revoked', 'This link is no longer active'],
    ['token_invalid', 'This link is not valid'],
  ])('explains a %s link without showing server internals', async (code, heading) => {
    mockFetch({ 'GET /api/portal/me': { status: 401, body: { detail: 'x', code } } });
    open(`/portal#t=${TOKEN}`);
    render(<App />);
    expect(await screen.findByRole('heading', { name: heading })).toBeTruthy();
  });

  it('shows the closed state for an expired invitation', async () => {
    mockFetch({ 'GET /api/portal/me': { body: me('contact_agency', 'expired') } });
    open(`/portal#t=${TOKEN}`);
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'This invitation is closed' })).toBeTruthy();
    expect(screen.getByText(/time to complete onboarding has passed/)).toBeTruthy();
  });
});

async function openForm(routes: Parameters<typeof mockFetch>[0] = {}) {
  const calls = mockFetch({ 'GET /api/portal/me': { body: me('onboarding') },
                            'GET /api/portal/onboarding': { body: onboardingForm }, ...routes });
  open(`/portal#t=${TOKEN}`);
  render(<App />);
  await screen.findByRole('heading', { name: 'Welcome, Dana Example' });
  return calls;
}

function fillRequired() {
  const set = (label: string, value: string) =>
    fireEvent.change(screen.getByLabelText(label, { exact: false }), { target: { value } });
  set('Short description of the project', 'A new marketing website.');
  set('What business goals should this project achieve?', 'More qualified enquiries.');
  set('Requirements and scope', 'Ten pages, contact form, blog.');
  set('Preferred channel', 'video_call');
  fireEvent.click(within(screen.getByRole('radiogroup')).getByLabelText('Yes'));
}

describe('onboarding form', () => {
  it('is generated from the API schema, with the prefill applied', async () => {
    await openForm();
    expect((screen.getByLabelText('Project name', { exact: false }) as HTMLInputElement).value).toBe('Example site');
    expect(screen.getByRole('group', { name: 'Communication preferences' })).toBeTruthy();
    expect(screen.getByRole('group', { name: 'About your service' })).toBeTruthy();
    expect(screen.getByLabelText('Preferred CMS', { exact: false }).tagName).toBe('SELECT');
    expect(screen.getByLabelText('Roughly how many pages?', { exact: false }).getAttribute('type')).toBe('number');
    expect(screen.getByLabelText('Desired delivery date', { exact: false }).getAttribute('type')).toBe('date');
    expect(screen.getByLabelText('Current website (if any)', { exact: false }).getAttribute('type')).toBe('url');
    expect(screen.queryByText(/At least 1 characters/)).toBeNull();
  });

  it('shows field errors and sends nothing while required answers are missing', async () => {
    const calls = await openForm();
    fireEvent.click(screen.getByRole('button', { name: 'Submit project details' }));
    expect((await screen.findAllByText('This field is required.')).length).toBeGreaterThanOrEqual(4);
    expect(screen.getByLabelText('Requirements and scope', { exact: false }).getAttribute('aria-invalid')).toBe('true');
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0);
  });

  it('submits once, in the API shape, and shows the generated project', async () => {
    let posts = 0;
    let resolvePost: (v: { status: number; body: unknown }) => void = () => {};
    const calls = await openForm({
      'POST /api/portal/onboarding': () => { posts += 1; return new Promise((r) => { resolvePost = r; }); },
    });
    fillRequired();
    const button = screen.getByRole('button', { name: 'Submit project details' });
    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.submit(button.closest('form')!);
    await waitFor(() => expect(posts).toBe(1));
    expect(screen.getByRole('button', { name: 'Submitting…' })).toHaveProperty('disabled', true);
    resolvePost({ status: 201, body: { status: 'submitted', project } });

    expect(await screen.findByText(/Your project details were submitted/)).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Example site' })).toBeTruthy();
    const post = calls.find((c) => c.method === 'POST')!;
    expect(JSON.parse(String(post.init.body))).toEqual({
      project_name: 'Example site', project_description: 'A new marketing website.',
      business_goals: 'More qualified enquiries.', scope_requirements: 'Ten pages, contact form, blog.',
      communication: { preferred_channel: 'video_call', update_frequency: 'weekly' },
      service_details: { cms_preference: 'no_preference', has_brand_assets: true },
    });
    expectTokenOnlyInHeader(calls);
  });

  it('shows the server validation message next to the field', async () => {
    await openForm({
      'POST /api/portal/onboarding': { status: 422, body: { detail: [
        { loc: ['body', 'desired_delivery_date'], msg: 'Value error, desired_delivery_date must be in the future', type: 'value_error' }] } },
    });
    fillRequired();
    fireEvent.click(screen.getByRole('button', { name: 'Submit project details' }));
    expect(await screen.findByText('Must be in the future.')).toBeTruthy();
    expect(screen.queryByText(/desired_delivery_date/)).toBeNull();
    expect(screen.getByLabelText('Desired delivery date', { exact: false }).getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('alert').textContent).toContain('Some answers need attention');
  });

  it('shows the project when the invitation was already submitted elsewhere', async () => {
    let submitted = false;
    await openForm({
      'GET /api/portal/me': () => ({ body: submitted ? me('project', 'submitted') : me('onboarding') }),
      'POST /api/portal/onboarding': () => { submitted = true; return { status: 409, body: { code: 'already_submitted', detail: 'x' } }; },
      'GET /api/portal/project': { body: project },
    });
    fillRequired();
    fireEvent.click(screen.getByRole('button', { name: 'Submit project details' }));
    expect(await screen.findByRole('heading', { name: 'Example site' })).toBeTruthy();
  });
});

describe('project dashboard', () => {
  it('shows progress, milestones, tasks and updates from the API only', async () => {
    mockFetch({ 'GET /api/portal/me': { body: me('project', 'submitted') }, 'GET /api/portal/project': { body: project } });
    open(`/portal#t=${TOKEN}`);
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Example site' })).toBeTruthy();
    expect(screen.getByText('30%')).toBeTruthy();
    expect(screen.getByText('3 of 10')).toBeTruthy();
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('30');
    expect(screen.getByRole('heading', { name: 'Discovery' })).toBeTruthy();
    expect(screen.getByText('Homepage design')).toBeTruthy();
    expect(screen.getByText('Project created from your onboarding answers')).toBeTruthy();
    expect(screen.queryByText(/clickup/i)).toBeNull();
  });

  it('explains when the project is not ready yet', async () => {
    mockFetch({ 'GET /api/portal/me': { body: me('project', 'submitted') },
                'GET /api/portal/project': { status: 404, body: { code: 'project_not_found', detail: 'x' } } });
    open(`/portal#t=${TOKEN}`);
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Your project is being set up' })).toBeTruthy();
  });
});
