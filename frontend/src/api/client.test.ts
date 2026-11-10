import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError, onUnauthorized } from './client';
import { mockFetch } from '../test/fetchMock';

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); });

describe('api client', () => {
  it('sends cookies, adds the CSRF header and JSON only for mutations', async () => {
    const calls = mockFetch({ 'GET /api/admin/clients': { body: { items: [] } }, 'POST /api/admin/clients': { status: 201, body: { id: 'x' } } });
    await api.get('/api/admin/clients', { q: 'acme', status: '', limit: 20 });
    await api.post('/api/admin/clients', { name: 'Acme' });
    const [get, post] = calls;
    expect(get.init.credentials).toBe('include');
    expect(get.search).toBe('?q=acme&limit=20');
    expect((get.init.headers as Record<string, string>)['X-Requested-With']).toBeUndefined();
    expect(post.init.credentials).toBe('include');
    expect((post.init.headers as Record<string, string>)['X-Requested-With']).toBe('XMLHttpRequest');
    expect((post.init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect(post.init.body).toBe('{"name":"Acme"}');
  });

  it('notifies the auth layer on 401 and surfaces the server message', async () => {
    mockFetch({ 'GET /api/admin/dashboard': { status: 401, body: { detail: 'Administrator sign-in required.', code: 'admin_auth_required' } } });
    const listener = vi.fn();
    const off = onUnauthorized(listener);
    await expect(api.get('/api/admin/dashboard')).rejects.toMatchObject({ status: 401, code: 'admin_auth_required' });
    expect(listener).toHaveBeenCalledTimes(1);
    off();
  });

  it('maps validation errors and network failures to readable errors', async () => {
    mockFetch({ 'PATCH /api/admin/clients/1': { status: 422, body: { detail: [{ loc: ['body', 'email'], msg: 'bad', type: 'x' }] } } });
    const err = await api.patch('/api/admin/clients/1', { email: 'x' }).catch((e) => e) as ApiError;
    expect(err.fieldErrors[0].loc).toEqual(['body', 'email']);
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('offline'); }));
    await expect(api.get('/api/admin/me')).rejects.toMatchObject({ code: 'network_error' });
  });

  it('never writes to browser storage', async () => {
    mockFetch({ 'POST /api/admin/login': { body: { authenticated: true } } });
    await api.post('/api/admin/login', { password: 'secret' });
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});
