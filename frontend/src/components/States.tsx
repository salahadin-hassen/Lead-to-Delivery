import type { ReactNode } from 'react';
import type { ApiError } from '../api/client';

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return <p className="state" role="status">{label}</p>;
}

export function ErrorNotice({ error, onRetry }: { error: ApiError | Error; onRetry?: () => void }) {
  // Always rendered as text: server messages never become HTML.
  return (
    <div className="notice notice-error" role="alert">
      <p>{error.message}</p>
      {onRetry && <button type="button" className="button small" onClick={onRetry}>Try again</button>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="state empty">{children}</p>;
}
