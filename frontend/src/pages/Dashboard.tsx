import { api } from '../api/client';
import type { Dashboard as DashboardData } from '../api/types';
import { ActivityList } from '../components/ActivityList';
import { StatusBadge } from '../components/Badge';
import { PageHeader } from '../components/Layout';
import { ProgressBar } from '../components/ProgressBar';
import { Empty, ErrorNotice, Loading } from '../components/States';
import { formatDate, formatDateTime, jobLabel } from '../format';
import { Link } from '../router';
import { useResource } from '../useResource';

export function Dashboard() {
  const { data, error, loading, reload } = useResource(() => api.get<DashboardData>('/api/admin/dashboard'), 'dashboard');
  if (loading && !data) return <><PageHeader title="Dashboard" /><Loading /></>;
  if (error || !data) return <><PageHeader title="Dashboard" /><ErrorNotice error={error!} onRetry={reload} /></>;
  const c = data.counts;
  const stages = [
    { label: 'Waiting for onboarding', value: c.pending_invitations, note: 'invitations sent, form not submitted', to: '/admin/clients' },
    { label: 'Active projects', value: c.active_projects, note: `${c.completed_projects} completed`, to: '/admin/projects' },
    { label: 'Open tasks', value: c.open_tasks, note: `${c.completed_tasks} done`, to: '/admin/projects' },
    // Failures lead when there are any: that is what needs attention.
    c.dead_jobs > 0
      ? { label: 'Integration jobs failed', value: c.dead_jobs, note: `${c.pending_jobs + c.running_jobs} waiting`,
          to: '/admin/integrations', alert: true }
      : { label: 'Integration jobs waiting', value: c.pending_jobs + c.running_jobs, note: 'none failed',
          to: '/admin/integrations', alert: false },
  ];
  return (
    <>
      <PageHeader title="Dashboard">
        <button type="button" className="button small" onClick={reload}>Refresh</button>
      </PageHeader>
      <section aria-label="Delivery pipeline" className="pipeline">
        {stages.map((s) => (
          <Link key={s.label} to={s.to} className={`stage${s.alert ? ' stage-alert' : ''}`}>
            <span className="stage-value">{s.value}</span>
            <span className="stage-label">{s.label}</span>
            <span className="stage-note">{s.note}</span>
          </Link>
        ))}
      </section>
      <p className="pipeline-foot">
        {c.total_clients} clients ({c.active_clients} active, {c.archived_clients} archived)
      </p>
      <div className="columns">
        <section className="panel">
          <h2>Failed integration jobs</h2>
          {data.dead_jobs.length === 0 ? <Empty>No failed jobs. Nothing needs attention.</Empty> : (
            <table className="table compact">
              <thead><tr><th>Job</th><th>Error</th><th>Attempts</th><th>Updated</th></tr></thead>
              <tbody>
                {data.dead_jobs.map((j) => (
                  <tr key={j.id}>
                    <td><Link to={`/admin/integrations?job=${j.id}`}>{jobLabel(j.job_type)}</Link></td>
                    <td><code>{j.last_error_code ?? '—'}</code></td>
                    <td>{j.attempts}</td>
                    <td>{formatDateTime(j.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h2>Active projects</h2>
          {data.active_projects.length === 0 ? <Empty>No active projects yet. They appear when a client submits onboarding.</Empty> : (
            <ul className="project-list">
              {data.active_projects.map((p) => (
                <li key={p.id}>
                  <div>
                    <Link to={`/admin/projects/${p.id}`}>{p.name}</Link>
                    <span className="muted"> for {p.client_name}</span>
                  </div>
                  <ProgressBar progress={p.progress} />
                  <span className="muted">Due {formatDate(p.due_date)}</span>
                  <StatusBadge status={p.status} />
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="panel">
          <h2>Recent activity</h2>
          <ActivityList items={data.recent_activity} showContext />
        </section>
      </div>
    </>
  );
}
