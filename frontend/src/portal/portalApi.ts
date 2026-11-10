// Client portal API client. The bearer token is sent ONLY in the Authorization
// header of same-origin /api/portal requests: no cookies, no referrer, no cache,
// never in a URL, never logged.

import { ApiError, toError } from '../api/client';
import type { OnboardingFormResponse, PortalMe, PortalProject } from './types';

async function call<T>(token: string, method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', Authorization: `Bearer ${token}` };
  const init: RequestInit = {
    method, headers, credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
  };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let response: Response;
  try {
    response = await fetch(`/api/portal${path}`, init);
  } catch {
    throw new ApiError(0, 'Cannot reach the server. Check your connection and try again.', 'network_error');
  }
  if (!response.ok) throw await toError(response);
  return (await response.json()) as T;
}

export const portalApi = {
  me: (token: string) => call<PortalMe>(token, 'GET', '/me'),
  onboarding: (token: string) => call<OnboardingFormResponse>(token, 'GET', '/onboarding'),
  submit: (token: string, form: unknown) =>
    call<{ status: 'submitted'; project: PortalProject }>(token, 'POST', '/onboarding', form),
  project: (token: string) => call<PortalProject>(token, 'GET', '/project'),
};
