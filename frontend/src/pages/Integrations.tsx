import { useState, type FormEvent } from 'react';
import { api, ApiError } from '../api/client';
import type { IntegrationStatus, Job, JobDetail, Page, WebhookEvent } from '../api/types';
import { StatusBadge } from '../components/Badge';
import { PageHeader } from '../components/Layout';
import { Pagination } from '../components/Pagination';
import { Empty, ErrorNotice, Loading } from '../components/States';
import { formatDateTime, humanize, jobLabel, jobStatusLabel } from '../format';
import { Link, navigate, useLocation } from '../router';
import { useResource } from '../useResource';

const LIMIT = 25;

export function Integrations() {
  const { search } = useLocation();
  const tab = search.get('tab') === 'events' ? 'events' : 'jobs';
  const jobId = search.get('job');
  return (
    <>
      <PageHeader title="Integrations" />
      <StatusPanel />
      <div className="tabs" role="tablist" aria-label="Integration history">
        <button type="button" role="tab" aria-selected={tab === 'jobs'} className="tab"
                onClick={() => navigate('/admin/integrations')}>Integration jobs</button>
        <button type="button" role="tab" aria-selected={tab === 'events'} className="tab"
                onClick={() => navigate('/admin/integrations?tab=events')}>CRM events</button>
      </div>
      {tab === 'jobs' ? (jobId ? <JobPanel id={jobId} /> : <Jobs project={search.get('project')} />) : <Events />}
    </>
  );
}

function StatusPanel() {
  const { data, error, reload } = useResource(() => api.get<IntegrationStatus>('/api/admin/integrations/status'), 'status');
  if (error) return <ErrorNotice error={error} onRetry={reload} />;
  if (!data) return <Loading />;
  const cu = data.clickup;
  const counts = (jobs: Partial<Record<string, number>>) =>
    `${jobs.pending ?? 0} waiting, ${jobs.succeeded ?? 0} done, ${jobs.dead ?? 0} failed`;
  return (
    <section className="integration-grid" aria-label="Integration status">
      <article className="panel">
        <h2>ClickUp</h2>
        <p>{!cu.credentials_present ? 'Not configured: set CLICKUP_API_TOKEN and CLICKUP_LIST_ID.'
          : !cu.configuration_valid ? `Configuration problem: ${humanize(cu.configuration_error)}.`
          : cu.live_verification === 'verified_by_successful_job'
            ? `Working: last task created ${formatDateTime(cu.last_success_at)}.`
            : 'Configured, but no task has been created yet, so the connection is unverified.'}</p>
        <p className="muted">Creates one ClickUp task per project task. Changes made here are not sent back.</p>
        <p className="muted">Jobs: {counts(cu.jobs)}</p>
      </article>
      <article className="panel">
        <h2>Slack</h2>
        <p>{!data.slack.credentials_present ? 'Not configured: set SLACK_WEBHOOK_URL.'
          : !data.slack.configuration_valid ? 'Configuration problem: SLACK_WEBHOOK_URL is not a Slack incoming-webhook URL.'
          : data.slack.live_verification === 'verified_by_successful_job'
            ? `Working: Slack last accepted a message ${formatDateTime(data.slack.last_success_at)}.`
            : 'Configured, but no message has been sent yet, so the connection is unverified.'}</p>
        <p className="muted">Posts a message to the team channel when a project is created.</p>
        <p className="muted">Jobs: {counts(data.slack.jobs)}</p>
      </article>
      <article className="panel">
        <h2>Google Sheets</h2>
        <p>{!data.google_sheets.service_account_file_configured || !data.google_sheets.spreadsheet_id_configured
          ? 'Not configured: set GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_SHEETS_SPREADSHEET_ID.'
          : !data.google_sheets.configuration_valid ? `Configuration problem: ${humanize(data.google_sheets.configuration_error)}.`
          : data.google_sheets.live_verification === 'verified_by_successful_job'
            ? `Working: last row written ${formatDateTime(data.google_sheets.last_success_at)}.`
            : 'Configured, but no row has been written yet, so the connection is unverified.'}</p>
        <p className="muted">Keeps one row per project in the Projects sheet, updated when tasks or status change.</p>
        <p className="muted">Jobs: {counts(data.google_sheets.jobs)}</p>
      </article>
    </section>
  );
}

function Jobs({ project }: { project: string | null }) {
  const [filters, setFilters] = useState({ job_type: '', status: '', created_from: '', created_to: '' });
  const [offset, setOffset] = useState(0);
  const { data, error, loading, reload } = useResource(
    () => api.get<Page<Job>>('/api/admin/integration-jobs', { ...filters, project_id: project, limit: LIMIT, offset }),
    `${JSON.stringify(filters)}|${project}|${offset}`);
  const set = (f: keyof typeof filters) => (e: { target: { value: string } }) => { setOffset(0); setFilters({ ...filters, [f]: e.target.value }); };
  return (
    <section aria-label="Integration jobs">
      <form className="filters" onSubmit={(e) => e.preventDefault()}>
        <label htmlFor="job-type">Integration</label>
        <select id="job-type" value={filters.job_type} onChange={set('job_type')}>
          <option value="">All</option><option value="clickup.create_task">ClickUp task</option>
          <option value="sheets.upsert_project">Google Sheets row</option><option value="slack.notify">Slack message</option>
        </select>
        <label htmlFor="job-status">Status</label>
        <select id="job-status" value={filters.status} onChange={set('status')}>
          <option value="">All</option><option value="pending">Pending</option><option value="running">Running</option>
          <option value="succeeded">Succeeded</option><option value="dead">Failed</option>
        </select>
        <label htmlFor="job-from">From</label><input id="job-from" type="date" value={filters.created_from} onChange={set('created_from')} />
        <label htmlFor="job-to">To</label><input id="job-to" type="date" value={filters.created_to} onChange={set('created_to')} />
        {project && <span className="tag">One project <Link to="/admin/integrations">Show all</Link></span>}
      </form>
      {loading && !data ? <Loading /> : error ? <ErrorNotice error={error} onRetry={reload} /> : data && (
        data.items.length === 0 ? <Empty>No jobs match these filters.</Empty> : (
          <>
            <table className="table">
              <thead><tr><th>Job</th><th>For</th><th>Status</th><th>Attempts</th><th>Next run</th><th>Last error</th><th>Created</th></tr></thead>
              <tbody>
                {data.items.map((j) => (
                  <tr key={j.id}>
                    <td><Link to={`/admin/integrations?job=${j.id}`}>{jobLabel(j.job_type)}</Link></td>
                    <td>{j.task_title ?? j.project_name ?? '—'}</td>
                    <td><StatusBadge status={j.status} label={jobStatusLabel(j.status)} /></td>
                    <td>{j.attempts}/{j.max_attempts}</td>
                    <td>{j.status === 'pending' ? formatDateTime(j.next_run_at) : '—'}</td>
                    <td>{j.last_error_code ? <code>{j.last_error_code}</code> : '—'}</td>
                    <td>{formatDateTime(j.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          </>
        ))}
    </section>
  );
}

function JobPanel({ id }: { id: string }) {
  const { data, error, loading, reload } = useResource(() => api.get<JobDetail>(`/api/admin/integration-jobs/${id}`), id);
  if (loading && !data) return <Loading />;
  if (error || !data) return <ErrorNotice error={error!} onRetry={reload} />;
  const j = data;
  return (
    <section className="panel" aria-label="Job details">
      <Link to="/admin/integrations" className="back-link">All jobs</Link>
      <h2>{jobLabel(j.job_type)} <StatusBadge status={j.status} label={jobStatusLabel(j.status)} /></h2>
      <dl className="facts">
        <div><dt>For</dt><dd>{j.project_id ? <Link to={`/admin/projects/${j.project_id}`}>{j.task_title ?? j.project_name}</Link> : '—'}</dd></div>
        <div><dt>Attempts</dt><dd>{j.attempts} of {j.max_attempts}</dd></div>
        <div><dt>Last error</dt><dd>{j.last_error_code ? <code>{j.last_error_code}</code> : '—'}{j.last_error ? ` ${j.last_error}` : ''}</dd></div>
        <div><dt>Job ID</dt><dd><code>{j.id}</code></dd></div>
      </dl>
      {j.resolution && (
        <p className="notice notice-info">Marked as delivered by {j.resolution.resolved_by} on {formatDateTime(j.resolution.resolved_at)}:
          the message was found in Slack after attempt {j.resolution.after_attempt}, so it was not sent again.</p>
      )}
      {j.reconciliation && (
        <p className="muted">Reconciliation confirmed by {j.reconciliation.confirmed_by} on {formatDateTime(j.reconciliation.confirmed_at)}.</p>
      )}
      <h3>Attempts</h3>
      {j.attempt_history.length === 0 ? <Empty>Not attempted yet.</Empty> : (
        <table className="table compact">
          <thead><tr><th>#</th><th>Result</th><th>Started</th><th>Duration</th><th>HTTP</th><th>Error</th></tr></thead>
          <tbody>
            {j.attempt_history.map((a) => (
              <tr key={a.attempt_number}>
                <td>{a.attempt_number}</td>
                <td><StatusBadge status={a.outcome ?? 'running'} label={a.outcome ? undefined : 'In progress'} /></td>
                <td>{formatDateTime(a.started_at)}</td>
                <td>{a.duration_ms !== null ? `${a.duration_ms} ms` : '—'}</td>
                <td>{a.http_status ?? '—'}</td>
                <td>{a.error_code ? <><code>{a.error_code}</code> {a.error_message}</> : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {j.status === 'dead' && j.requires_reconciliation && j.job_type === 'slack.notify' && <ResolveDelivered job={j} onDone={reload} />}
      {j.status === 'dead' && <RetryForm job={j} onDone={reload} />}
    </section>
  );
}

function RetryForm({ job, onDone }: { job: JobDetail; onDone: () => void }) {
  const [attempts, setAttempts] = useState(3);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const needsCheck = job.requires_reconciliation;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await api.post(`/api/admin/integration-jobs/${job.id}/retry`,
                     { additional_attempts: attempts, reconciliation_confirmed: needsCheck ? confirmed : false });
      onDone();
    } catch (err) { setError(err as ApiError); } finally { setBusy(false); }
  }

  return (
    <form className="retry" onSubmit={submit} aria-label="Retry job">
      <h3>Retry</h3>
      {needsCheck && (
        <div className="notice notice-warn">
          <p>The last attempt may have reached the other system before it failed, so it is unclear whether it worked.
            Retrying without checking could create a duplicate.</p>
          {job.job_type === 'clickup.create_task' && (
            <p>In ClickUp, search the task list for <code>[LTD-TASK:{job.subject_id}]</code>. Delete any duplicates.
              If exactly one task exists, the retry links to it instead of creating another.</p>
          )}
          {job.job_type === 'sheets.upsert_project' && (
            <p>Google may have added the row before the connection failed. In the Projects sheet, search column A
              (project_id) for <code>{job.subject_id}</code>. If it is there, delete any extra copies and retry: the
              retry updates that row. Confirm only that you checked; a fresh search runs before anything is written.</p>
          )}
          {job.job_type === 'slack.notify' && (
            <p>Slack cannot be searched automatically. Open the team channel and look for the "New project ready"
              message for {job.project_name ?? 'this project'}. Retry only if it is not there; if it is, use
              "Message is in the channel" instead.</p>
          )}
          <label className="check">
            <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
            {job.job_type === 'slack.notify' ? 'I checked the channel and the message is not there'
              : job.job_type === 'sheets.upsert_project' ? 'I checked the sheet and removed any duplicate rows for this project'
              : 'I checked the other system and resolved any duplicates'}
          </label>
        </div>
      )}
      <label htmlFor="retry-attempts">Extra attempts</label>
      <input id="retry-attempts" type="number" min={1} max={20} value={attempts}
             onChange={(e) => setAttempts(Math.max(1, Math.min(20, Number(e.target.value) || 1)))} />
      {error && <ErrorNotice error={error} />}
      <button type="submit" className="button primary" disabled={busy || (needsCheck && !confirmed)}>
        {busy ? 'Retrying…' : 'Retry job'}
      </button>
    </form>
  );
}

function ResolveDelivered({ job, onDone }: { job: JobDetail; onDone: () => void }) {
  const [found, setFound] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await api.post(`/api/admin/integration-jobs/${job.id}/resolve-delivered`, { message_found_in_channel: true });
      onDone();
    } catch (err) { setError(err as ApiError); } finally { setBusy(false); }
  }

  return (
    <form className="retry" onSubmit={submit} aria-label="Mark message as delivered">
      <h3>Message is in the channel</h3>
      <p>Slack may have accepted the message even though the last attempt failed. If you can see it in the channel,
        mark the job as delivered. Nothing is sent again.</p>
      <label className="check">
        <input type="checkbox" checked={found} onChange={(e) => setFound(e.target.checked)} />
        I found this project's "New project ready" message in the Slack channel
      </label>
      {error && <ErrorNotice error={error} />}
      <button type="submit" className="button" disabled={busy || !found}>{busy ? 'Saving…' : 'Mark as delivered'}</button>
    </form>
  );
}

function Events() {
  const [status, setStatus] = useState('');
  const [offset, setOffset] = useState(0);
  const { data, error, loading, reload } = useResource(
    () => api.get<Page<WebhookEvent>>('/api/admin/webhook-events', { status, limit: LIMIT, offset }), `${status}|${offset}`);
  return (
    <section aria-label="CRM events">
      <form className="filters" onSubmit={(e) => e.preventDefault()}>
        <label htmlFor="event-status">Status</label>
        <select id="event-status" value={status} onChange={(e) => { setOffset(0); setStatus(e.target.value); }}>
          <option value="">All</option><option value="processed">Processed</option><option value="rejected">Rejected</option>
        </select>
      </form>
      {loading && !data ? <Loading /> : error ? <ErrorNotice error={error} onRetry={reload} /> : data && (
        data.items.length === 0 ? <Empty>No CRM events yet. They appear when the CRM sends a won deal.</Empty> : (
          <>
            <table className="table">
              <thead><tr><th>Received</th><th>Source</th><th>Event ID</th><th>Deal</th><th>Status</th><th>Duplicates</th><th>Client</th></tr></thead>
              <tbody>
                {data.items.map((e) => (
                  <tr key={e.id}>
                    <td>{formatDateTime(e.created_at)}</td>
                    <td>{humanize(e.source)}</td>
                    <td><code>{e.external_event_id}</code></td>
                    <td>{e.deal_id ? <code>{e.deal_id}</code> : '—'} {humanize(e.service_type)}</td>
                    <td><StatusBadge status={e.status} label={e.error_code ? humanize(e.error_code) : undefined} /></td>
                    <td>{e.duplicate_count || '—'}</td>
                    <td>{e.client_id ? <Link to={`/admin/clients/${e.client_id}`}>{e.client_name}</Link> : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          </>
        ))}
    </section>
  );
}
