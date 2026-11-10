import type { Activity } from '../api/types';
import { formatDateTime } from '../format';
import { Empty } from './States';

export function ActivityList({ items, showContext = false }: { items: Activity[]; showContext?: boolean }) {
  if (!items.length) return <Empty>No activity yet.</Empty>;
  return (
    <ol className="timeline">
      {items.map((a) => (
        <li key={a.id}>
          <time dateTime={a.created_at}>{formatDateTime(a.created_at)}</time>
          <div>
            <p className="timeline-summary">{a.summary}</p>
            <p className="timeline-meta">
              {a.actor_type === 'admin' ? 'You' : a.actor_type === 'crm' ? 'CRM' : a.actor_type === 'client' ? 'Client'
                : a.actor_type === 'worker' ? 'Integration worker' : 'System'}
              {showContext && (a.project_name || a.client_name) ? `, ${a.project_name ?? a.client_name}` : ''}
              {a.client_visible ? ', visible to client' : ''}
            </p>
          </div>
        </li>
      ))}
    </ol>
  );
}
