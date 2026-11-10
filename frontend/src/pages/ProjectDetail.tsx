import { useState, type FormEvent } from 'react';
import { api, ApiError } from '../api/client';
import type { ProjectDetail as ProjectData, ProjectStatus, Task, TaskStatus } from '../api/types';
import { ActivityList } from '../components/ActivityList';
import { StatusBadge } from '../components/Badge';
import { PageHeader } from '../components/Layout';
import { ProgressBar } from '../components/ProgressBar';
import { Empty, ErrorNotice, Loading } from '../components/States';
import { formatDate, humanize, jobLabel, jobStatusLabel } from '../format';
import { Link } from '../router';
import { useResource } from '../useResource';

const TASK_STATUSES: { value: TaskStatus; label: string }[] = [
  { value: 'todo', label: 'To do' }, { value: 'in_progress', label: 'In progress' }, { value: 'done', label: 'Done' },
];
const PROJECT_STATUSES: ProjectStatus[] = ['active', 'on_hold', 'completed', 'cancelled'];

export function ProjectDetail({ id }: { id: string }) {
  const { data, error, loading, reload } = useResource(() => api.get<ProjectData>(`/api/admin/projects/${id}`), id);
  const [project, setProject] = useState<ProjectData | null>(null);
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const back = { to: '/admin/projects', label: 'Projects' };
  const shown = project?.id === id ? project : data;
  if (loading && !shown) return <><PageHeader title="Project" back={back} /><Loading /></>;
  if (error || !shown) return <><PageHeader title="Project" back={back} /><ErrorNotice error={error!} onRetry={reload} /></>;
  const p = shown;

  async function save(path: string, body: object) {
    setActionError(null);
    try {
      const result = await api.patch<{ changed: boolean; project: ProjectData }>(path, body);
      setProject(result.project);
    } catch (err) { setActionError(err as ApiError); }
  }

  const unassigned = p.tasks.filter((t) => !t.milestone_id || !p.milestones.some((m) => m.id === t.milestone_id));
  return (
    <>
      <PageHeader title={p.name} back={back}>
        <label htmlFor="project-status" className="inline-label">Project status</label>
        <select id="project-status" value={p.status}
                onChange={(e) => void save(`/api/admin/projects/${p.id}`, { status: e.target.value })}>
          {PROJECT_STATUSES.map((s) => <option key={s} value={s}>{humanize(s)}</option>)}
        </select>
      </PageHeader>
      {actionError && <ErrorNotice error={actionError} />}
      <dl className="facts">
        <div><dt>Client</dt><dd><Link to={`/admin/clients/${p.client.id}`}>{p.client.name}</Link>{p.client.company ? `, ${p.client.company}` : ''}</dd></div>
        <div><dt>Service</dt><dd>{p.template?.name ?? '—'}</dd></div>
        <div><dt>Dates</dt><dd>{formatDate(p.start_date)} to {formatDate(p.due_date)}</dd></div>
        <div><dt>Progress</dt><dd><ProgressBar progress={p.progress} /></dd></div>
      </dl>
      {p.description && <p className="lede">{p.description}</p>}
      <p className="notice notice-info">
        Status and assignment changes are saved here only. They are not sent to ClickUp: ClickUp tasks are created once,
        and two-way sync is not available yet.
      </p>

      {p.milestones.map((m) => (
        <section key={m.id} className="panel">
          <h2>{m.name} <span className="muted">due {formatDate(m.due_date)}</span></h2>
          <TaskTable tasks={p.tasks.filter((t) => t.milestone_id === m.id)} projectId={p.id} onSave={save} />
        </section>
      ))}
      {unassigned.length > 0 && (
        <section className="panel"><h2>Other tasks</h2><TaskTable tasks={unassigned} projectId={p.id} onSave={save} /></section>
      )}
      {p.tasks.length === 0 && <Empty>This project has no tasks.</Empty>}

      <div className="columns">
        <section className="panel">
          <h2>Integration jobs for this project</h2>
          {p.integration_jobs.length === 0 ? <Empty>No integration jobs.</Empty> : (
            <ul className="plain">
              {p.integration_jobs.map((j) => (
                <li key={`${j.job_type}-${j.status}`}>{jobLabel(j.job_type)}: {j.n} <StatusBadge status={j.status} label={jobStatusLabel(j.status)} /></li>
              ))}
            </ul>
          )}
          <Link to={`/admin/integrations?project=${p.id}`}>See job history</Link>
        </section>
        <section className="panel"><h2>Activity</h2><ActivityList items={p.activity} /></section>
      </div>
    </>
  );
}

function TaskTable({ tasks, projectId, onSave }: { tasks: Task[]; projectId: string;
  onSave: (path: string, body: object) => Promise<void> }) {
  if (!tasks.length) return <Empty>No tasks in this milestone.</Empty>;
  return (
    <table className="table">
      <thead><tr><th>Task</th><th>Due</th><th>Status</th><th>Assignee</th><th>ClickUp</th></tr></thead>
      <tbody>
        {tasks.map((t) => (
          <tr key={t.id}>
            <td>{t.title}{!t.client_visible && <span className="tag">Internal</span>}</td>
            <td>{formatDate(t.due_date)}</td>
            <td>
              <label className="sr-only" htmlFor={`status-${t.id}`}>Status of {t.title}</label>
              <select id={`status-${t.id}`} value={t.status}
                      onChange={(e) => void onSave(`/api/admin/projects/${projectId}/tasks/${t.id}`, { status: e.target.value })}>
                {TASK_STATUSES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
              </select>
            </td>
            <td><Assignee task={t} projectId={projectId} onSave={onSave} /></td>
            <td>
              {t.clickup_url ? <a href={t.clickup_url} target="_blank" rel="noopener noreferrer">Open in ClickUp</a>
                : t.clickup_job_status
                  ? <Link to={`/admin/integrations?job=${t.clickup_job_id}`}><StatusBadge status={t.clickup_job_status}
                      label={t.clickup_job_status === 'dead' ? 'Sync failed' : humanize(t.clickup_job_status)} /></Link>
                  : <span className="muted">Not queued</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Assignee({ task, projectId, onSave }: { task: Task; projectId: string;
  onSave: (path: string, body: object) => Promise<void> }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(task.assignee_name ?? '');
  const [email, setEmail] = useState(task.assignee_email ?? '');
  if (!editing) {
    return (
      <button type="button" className="link-button" onClick={() => setEditing(true)}
              aria-label={`Change assignee of ${task.title}`}>
        {task.assignee_name ?? task.assignee_email ?? 'Unassigned'}
      </button>
    );
  }
  async function submit(e: FormEvent) {
    e.preventDefault();
    await onSave(`/api/admin/projects/${projectId}/tasks/${task.id}`, { assignee_name: name, assignee_email: email });
    setEditing(false);
  }
  return (
    <form className="inline-form" onSubmit={submit}>
      <input aria-label="Assignee name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} placeholder="Name" />
      <input aria-label="Assignee email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="Email" />
      <button type="submit" className="button small primary">Save</button>
      <button type="button" className="button small ghost" onClick={() => setEditing(false)}>Cancel</button>
    </form>
  );
}
