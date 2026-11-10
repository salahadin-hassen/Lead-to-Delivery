import { humanize } from '../format';

const TONE: Record<string, string> = {
  active: 'ok', succeeded: 'ok', done: 'ok', submitted: 'ok', processed: 'ok', success: 'ok', completed: 'ok',
  pending: 'wait', running: 'wait', in_progress: 'wait', on_hold: 'wait', retryable: 'wait', received: 'wait',
  dead: 'bad', rejected: 'bad', failed: 'bad', permanent: 'bad', unknown: 'bad', expired: 'bad', revoked: 'bad',
  archived: 'muted', cancelled: 'muted', todo: 'muted',
};

export function StatusBadge({ status, label }: { status: string | null | undefined; label?: string }) {
  if (!status) return <span className="badge muted">—</span>;
  return <span className={`badge ${TONE[status] ?? 'muted'}`}>{label ?? humanize(status)}</span>;
}
