// Same-origin JSON client for /api/admin.
//
// * The session is an HttpOnly cookie: never read, stored or sent by hand.
// * Every request uses credentials: 'include'; state-changing requests add
//   X-Requested-With (the backend's CSRF check) and a JSON body.
// * A 401 from any call notifies the auth layer, which drops the session state.

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly fieldErrors: { loc: (string | number)[]; msg: string }[];

  constructor(status: number, message: string, code: string | null = null,
              fieldErrors: { loc: (string | number)[]; msg: string }[] = []) {
    super(message);
    this.status = status;
    this.code = code;
    this.fieldErrors = fieldErrors;
  }
}

type UnauthorizedListener = () => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();

export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

export type Query = Record<string, string | number | boolean | null | undefined>;

function withQuery(path: string, query?: Query): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `${path}?${text}` : path;
}

async function toError(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const detail = (body as { detail?: unknown } | null)?.detail;
  const code = (body as { code?: unknown } | null)?.code;
  if (Array.isArray(detail)) {
    const fields = detail.map((d) => ({ loc: (d?.loc ?? []) as (string | number)[], msg: String(d?.msg ?? '') }));
    return new ApiError(response.status, 'Some fields need attention.', 'validation_error', fields);
  }
  const fallback = response.status >= 500 ? 'The server could not complete the request. Try again.'
    : `Request failed (${response.status}).`;
  return new ApiError(response.status, typeof detail === 'string' ? detail : fallback,
                      typeof code === 'string' ? code : null);
}

export async function request<T>(method: string, path: string, options: { body?: unknown; query?: Query } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const init: RequestInit = { method, credentials: 'include', headers };
  if (method !== 'GET') {
    headers['X-Requested-With'] = 'XMLHttpRequest';
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
  }
  let response: Response;
  try {
    response = await fetch(withQuery(path, options.query), init);
  } catch {
    throw new ApiError(0, 'Cannot reach the server. Check your connection and try again.', 'network_error');
  }
  if (response.status === 401 && path !== '/api/admin/login') {
    unauthorizedListeners.forEach((listener) => listener());
  }
  if (!response.ok) throw await toError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string, query?: Query) => request<T>('GET', path, { query }),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, { body }),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, { body }),
};
