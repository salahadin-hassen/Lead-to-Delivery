// Client portal: /portal#t=<token>. Separate from the admin console: no admin
// session, no admin API calls, and nothing internal is shown.

import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/client';
import { ProgressBar } from '../components/ProgressBar';
import { formatDate, formatDateTime } from '../format';
import { portalApi } from './portalApi';
import { SchemaForm, serverFieldErrors, type FieldErrors } from './SchemaForm';
import { captureToken } from './token';
import type { OnboardingFormResponse, PortalMe, PortalProject, PortalTask } from './types';

type View =
  | { kind: 'loading' }
  | { kind: 'no_link' }
  | { kind: 'denied'; title: string; message: string }
  | { kind: 'error'; message: string }
  | { kind: 'closed'; me: PortalMe }
  | { kind: 'onboarding'; me: PortalMe; form: OnboardingFormResponse }
  | { kind: 'project'; me: PortalMe; project: PortalProject; justSubmitted: boolean }
  | { kind: 'project_pending'; me: PortalMe };

const DENIED: Record<string, { title: string; message: string }> = {
  token_expired: { title: 'This link has expired', message: 'Please ask the agency for a new access link.' },
  token_revoked: { title: 'This link is no longer active', message: 'Please ask the agency for a new access link.' },
  token_invalid: { title: 'This link is not valid', message: 'Check that you opened the full link from the agency, or ask them for a new one.' },
  token_missing: { title: 'Access link required', message: 'Open the link the agency sent you.' },
};

const STATUS_LABEL: Record<string, string> = {
  active: 'In progress', on_hold: 'On hold', completed: 'Completed', cancelled: 'Cancelled',
  pending: 'Not started', in_progress: 'In progress', done: 'Done', todo: 'To do',
};
const TONE: Record<string, string> = {
  active: 'wait', in_progress: 'wait', on_hold: 'wait', completed: 'ok', done: 'ok',
  cancelled: 'muted', pending: 'muted', todo: 'muted',
};

function Status({ value }: { value: string }) {
  return <span className={`badge ${TONE[value] ?? 'muted'}`}>{STATUS_LABEL[value] ?? value}</span>;
}

function deniedView(error: unknown): View {
  if (error instanceof ApiError && error.status === 401) {
    const d = DENIED[error.code ?? ''] ?? DENIED.token_invalid;
    return { kind: 'denied', ...d };
  }
  return { kind: 'error', message: error instanceof Error ? error.message : 'Something went wrong. Try again.' };
}

export function PortalApp() {
  const [view, setView] = useState<View>({ kind: 'loading' });
  const tokenRef = useRef<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const inFlight = useRef(false);

  const load = useCallback(async (justSubmitted = false) => {
    const token = tokenRef.current;
    if (!token) return;
    setView({ kind: 'loading' });
    try {
      const me = await portalApi.me(token);
      if (me.next_step === 'onboarding') {
        setView({ kind: 'onboarding', me, form: await portalApi.onboarding(token) });
      } else if (me.next_step === 'project') {
        try {
          setView({ kind: 'project', me, project: await portalApi.project(token), justSubmitted });
        } catch (error) {
          if (error instanceof ApiError && error.status === 404) setView({ kind: 'project_pending', me });
          else throw error;
        }
      } else {
        setView({ kind: 'closed', me });
      }
    } catch (error) {
      setView(deniedView(error));
    }
  }, []);

  useEffect(() => {
    document.title = 'Project portal';
    const state = captureToken();
    if (state.kind === 'ok') {
      tokenRef.current = state.token;
      void load();
    } else if (state.kind === 'malformed') {
      setView({ kind: 'denied', ...DENIED.token_invalid });
    } else {
      setView({ kind: 'no_link' });
    }
  }, [load]);

  const submit = async (body: Record<string, unknown>) => {
    const token = tokenRef.current;
    if (!token || inFlight.current || view.kind !== 'onboarding') return;
    inFlight.current = true;
    setSubmitting(true);
    setFieldErrors({});
    setFormError(null);
    try {
      const result = await portalApi.submit(token, body);
      setView({ kind: 'project', me: { ...view.me, next_step: 'project' }, project: result.project, justSubmitted: true });
    } catch (error) {
      if (error instanceof ApiError && error.status === 422) {
        setFieldErrors(serverFieldErrors(error.fieldErrors));
        setFormError('Some answers need attention. They are marked below.');
      } else if (error instanceof ApiError && error.status === 409) {
        await load(error.code === 'already_submitted');
      } else if (error instanceof ApiError && error.status === 401) {
        setView(deniedView(error));
      } else {
        setFormError(error instanceof Error ? error.message : 'Something went wrong. Try again.');
      }
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  };

  const me = 'me' in view ? view.me : null;
  return (
    <div className="portal">
      <header className="portal-header">
        <div className="portal-header-inner">
          <span className="brand"><span className="brand-mark" aria-hidden="true" />Project portal</span>
          {me && <span className="portal-client">{me.client.company ?? me.client.name}</span>}
        </div>
      </header>
      <main className="portal-main" id="main">
        {view.kind === 'loading' && <p className="state" role="status">Loading your project…</p>}
        {view.kind === 'no_link' && (
          <section className="portal-card">
            <h1>Open your access link</h1>
            <p>This portal opens from the private link the agency sent you. For your security, this page does not
              remember the link, so after reloading, open the link from your message again.</p>
          </section>
        )}
        {view.kind === 'denied' && (
          <section className="portal-card" role="alert">
            <h1>{view.title}</h1>
            <p>{view.message}</p>
          </section>
        )}
        {view.kind === 'error' && (
          <section className="portal-card" role="alert">
            <h1>We could not load your project</h1>
            <p>{view.message}</p>
            <button type="button" className="button" onClick={() => void load()}>Try again</button>
          </section>
        )}
        {view.kind === 'closed' && (
          <section className="portal-card">
            <h1>This invitation is closed</h1>
            <p>{view.me.invitation.status === 'expired'
              ? 'The time to complete onboarding has passed. Please ask the agency for a new link.'
              : 'Please contact the agency for a new access link.'}</p>
          </section>
        )}
        {view.kind === 'onboarding' && (
          <>
            <section className="portal-intro">
              <p className="eyebrow">{view.form.template_name ?? 'New project'}</p>
              <h1>Welcome, {view.me.client.name}</h1>
              <p className="lede">Tell us about your project. Your answers set up the project plan; you can follow
                its progress here afterwards. This link is valid until {formatDate(view.me.invitation.expires_at)}.</p>
            </section>
            {formError && <div className="notice notice-error" role="alert"><p>{formError}</p></div>}
            <section className="portal-card">
              <SchemaForm schema={view.form.form_schema} prefill={view.form.prefill} serverErrors={fieldErrors}
                          submitting={submitting} onSubmit={(b) => void submit(b)} />
            </section>
          </>
        )}
        {view.kind === 'project_pending' && (
          <section className="portal-card">
            <h1>Your project is being set up</h1>
            <p>Your details were received{view.me.invitation.submitted_at
              ? ` on ${formatDate(view.me.invitation.submitted_at)}` : ''}. Check back shortly.</p>
            <button type="button" className="button" onClick={() => void load()}>Refresh</button>
          </section>
        )}
        {view.kind === 'project' && <ProjectDashboard me={view.me} project={view.project} justSubmitted={view.justSubmitted} />}
      </main>
    </div>
  );
}

function TaskList({ tasks }: { tasks: PortalTask[] }) {
  if (!tasks.length) return <p className="muted small">No tasks to show yet.</p>;
  return (
    <ul className="portal-tasks">
      {tasks.map((t, i) => (
        <li key={`${t.title}-${i}`}>
          <span className={`task-dot ${t.status}`} aria-hidden="true" />
          <span className="task-title">{t.title}</span>
          <Status value={t.status} />
          <span className="muted small">{t.status === 'done' && t.completed_at ? `Done ${formatDate(t.completed_at)}`
            : t.due_date ? `Due ${formatDate(t.due_date)}` : ''}</span>
        </li>
      ))}
    </ul>
  );
}

function ProjectDashboard({ me, project, justSubmitted }: { me: PortalMe; project: PortalProject; justSubmitted: boolean }) {
  const p = project.progress;
  return (
    <>
      {justSubmitted && (
        <div className="notice notice-info" role="status">
          <p>Thank you. Your project details were submitted and your project plan has been created.</p>
        </div>
      )}
      <section className="portal-intro">
        <p className="eyebrow">{me.invitation.template_name ?? 'Project'}</p>
        <div className="portal-title-row">
          <h1>{project.name}</h1>
          <Status value={project.status} />
        </div>
        {project.description && <p className="lede">{project.description}</p>}
      </section>

      <section className="portal-card portal-progress" aria-labelledby="progress-heading">
        <h2 id="progress-heading">Progress</h2>
        <p className="portal-percent"><strong>{p.percent}%</strong> complete</p>
        <ProgressBar progress={p} />
        <dl className="facts">
          <div><dt>Tasks completed</dt><dd>{p.completed_tasks} of {p.total_tasks}</dd></div>
          <div><dt>Start date</dt><dd>{formatDate(project.start_date)}</dd></div>
          <div><dt>Target date</dt><dd>{formatDate(project.due_date)}</dd></div>
        </dl>
      </section>

      <div className="portal-columns">
        <section className="portal-card" aria-labelledby="milestones-heading">
          <h2 id="milestones-heading">Milestones</h2>
          {project.milestones.length === 0 && <p className="muted">No milestones yet.</p>}
          <ol className="portal-milestones">
            {project.milestones.map((m) => (
              <li key={`${m.position}-${m.name}`}>
                <div className="milestone-head">
                  <h3>{m.name}</h3>
                  <Status value={m.status} />
                  {m.due_date && <span className="muted small">Due {formatDate(m.due_date)}</span>}
                </div>
                <TaskList tasks={m.tasks} />
              </li>
            ))}
          </ol>
          {project.unassigned_tasks.length > 0 && (
            <>
              <h3>Other tasks</h3>
              <TaskList tasks={project.unassigned_tasks} />
            </>
          )}
        </section>
        <section className="portal-card" aria-labelledby="activity-heading">
          <h2 id="activity-heading">Recent updates</h2>
          {project.activity.length === 0 ? <p className="muted">No updates yet.</p> : (
            <ul className="timeline">
              {project.activity.map((a, i) => (
                <li key={`${a.created_at}-${i}`}>
                  <time dateTime={a.created_at}>{formatDateTime(a.created_at)}</time>
                  <p className="timeline-summary">{a.summary}</p>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}
