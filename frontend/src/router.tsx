import { useSyncExternalStore, type MouseEvent, type ReactNode } from 'react';

const listeners = new Set<() => void>();
const notify = () => listeners.forEach((l) => l());
if (typeof window !== 'undefined') window.addEventListener('popstate', notify);

export function navigate(to: string, options: { replace?: boolean } = {}): void {
  if (options.replace) window.history.replaceState(null, '', to);
  else window.history.pushState(null, '', to);
  notify();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useLocation(): { path: string; search: URLSearchParams } {
  const href = useSyncExternalStore(subscribe, () => window.location.pathname + window.location.search);
  const url = new URL(href, 'http://local');
  return { path: url.pathname, search: url.searchParams };
}

/** '/admin/clients/:id' against a path -> { id } or null. */
export function matchPath(pattern: string, path: string): Record<string, string> | null {
  const p = pattern.split('/').filter(Boolean);
  const a = path.replace(/\/+$/, '').split('/').filter(Boolean);
  if (p.length !== a.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < p.length; i += 1) {
    if (p[i].startsWith(':')) params[p[i].slice(1)] = decodeURIComponent(a[i]);
    else if (p[i] !== a[i]) return null;
  }
  return params;
}

export function Link({ to, children, className, ...rest }: { to: string; children: ReactNode; className?: string;
  'aria-current'?: 'page' | undefined }) {
  const onClick = (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    navigate(to);
  };
  return <a href={to} onClick={onClick} className={className} {...rest}>{children}</a>;
}
