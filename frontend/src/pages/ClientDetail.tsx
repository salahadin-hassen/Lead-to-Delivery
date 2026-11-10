import { useState, type FormEvent } from 'react';
import { api, ApiError } from '../api/client';
import type { AccessLink, ClientDetail as ClientData, Invitation } from '../api/types';
import { ActivityList } from '../components/ActivityList';
import { StatusBadge } from '../components/Badge';
import { PageHeader } from '../components/Layout';
import { ProgressBar } from '../components/ProgressBar';
import { Empty, ErrorNotice, Loading } from '../components/States';
import { formatDate, formatDateTime, humanize, money } from '../format';
import { Link } from '../router';
import { useResource } from '../useResource';

export function ClientDetail({ id }: { id: string }) {
  const { data, error, loading, reload } = useResource(() => api.get<ClientData>(`/api/admin/clients/${id}`), id);
  const [editing, setEditing] = useState(false);
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const back = { to: '/admin/clients', label: 'Clients' };
  if (loading && !data) return <><PageHeader title="Client" back={back} /><Loading /></>;
  if (error || !data) return <><PageHeader title="Client" back={back} /><ErrorNotice error={error!} onRetry={reload} /></>;

  async function setStatus(action: 'archive' | 'restore') {
    if (action === 'archive' && !window.confirm('Archive this client? Projects and history are kept. New CRM deals for an archived client are rejected until you restore it.')) return;
    setActionError(null);
    try { await api.post(`/api/admin/clients/${id}/${action}`); reload(); }
    catch (err) { setActionError(err as ApiError); }
  }

  return (
    <>
      <PageHeader title={data.name} back={back}>
        <StatusBadge status={data.status} />
        <button type="button" className="button" onClick={() => setEditing(!editing)}>{editing ? 'Close editor' : 'Edit details'}</button>
        {data.status === 'active'
          ? <button type="button" className="button danger" onClick={() => void setStatus('archive')}>Archive</button>
          : <button type="button" className="button" onClick={() => void setStatus('restore')}>Restore</button>}
      </PageHeader>
      {actionError && <ErrorNotice error={actionError} />}
      {editing && <EditClient client={data} onSaved={() => { setEditing(false); reload(); }} />}
      <dl className="facts">
        <div><dt>Company</dt><dd>{data.company ?? '—'}</dd></div>
        <div><dt>Email</dt><dd>{data.email ?? '—'}</dd></div>
        <div><dt>Phone</dt><dd>{data.phone ?? '—'}</dd></div>
        <div><dt>CRM</dt><dd>{data.crm_source ? <>{humanize(data.crm_source)} <code>{data.crm_external_id}</code></> : 'Added by hand'}</dd></div>
        <div><dt>Added</dt><dd>{formatDate(data.created_at)}</dd></div>
      </dl>

      <section className="panel">
        <h2>Onboarding invitations</h2>
        {data.invitations.length === 0 ? <Empty>No invitations. They are created when a CRM deal is won.</Empty> : (
          <table className="table">
            <thead><tr><th>Deal</th><th>Service</th><th>Value</th><th>Status</th><th>Access link valid until</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>{data.invitations.map((inv) => <InvitationRow key={inv.id} inv={inv} onChanged={reload} />)}</tbody>
          </table>
        )}
      </section>

      <div className="columns">
        <section className="panel">
          <h2>Projects</h2>
          {data.projects.length === 0 ? <Empty>No projects yet. A project starts when the client submits onboarding.</Empty> : (
            <ul className="project-list">
              {data.projects.map((p) => (
                <li key={p.id}>
                  <Link to={`/admin/projects/${p.id}`}>{p.name}</Link>
                  <ProgressBar progress={p.progress} />
                  <StatusBadge status={p.status} />
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="panel">
          <h2>Activity</h2>
          <ActivityList items={data.activity} showContext />
        </section>
      </div>
    </>
  );
}

function InvitationRow({ inv, onChanged }: { inv: Invitation; onChanged: () => void }) {
  const [link, setLink] = useState<AccessLink | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const canRegenerate = inv.status === 'pending' || inv.status === 'submitted';

  async function regenerate() {
    const what = inv.status === 'submitted' ? 'a new project access link (valid 180 days)' : 'a new onboarding link';
    if (!window.confirm(`Create ${what}? The client's current link stops working immediately.`)) return;
    setBusy(true); setError(null); setCopied(false);
    try {
      setLink(await api.post<AccessLink>(`/api/admin/invitations/${inv.id}/regenerate-access-link`));
      onChanged();
    } catch (err) { setError(err as ApiError); } finally { setBusy(false); }
  }

  async function copy() {
    if (!link) return;
    try { await navigator.clipboard.writeText(link.access_url); setCopied(true); }
    catch { setCopied(false); }
  }

  return (
    <>
      <tr>
        <td>{inv.deal_name ?? '—'}{inv.project_id && <> <Link to={`/admin/projects/${inv.project_id}`}>project</Link></>}</td>
        <td>{humanize(inv.service_type)}</td>
        <td>{money(inv.deal_value_cents, inv.currency)}</td>
        <td><StatusBadge status={inv.status} /></td>
        <td>{inv.access_link_expires_at ? formatDateTime(inv.access_link_expires_at) : 'No active link'}</td>
        <td>
          {canRegenerate
            ? <button type="button" className="button small" onClick={() => void regenerate()} disabled={busy}>
                {busy ? 'Creating…' : inv.status === 'submitted' ? 'New project link' : 'New onboarding link'}</button>
            : <span className="muted">{inv.status === 'expired' ? 'Expired: cannot be reopened' : '—'}</span>}
        </td>
      </tr>
      {(link || error) && (
        <tr className="row-detail"><td colSpan={6}>
          {error && <ErrorNotice error={error} />}
          {link && (
            <div className="one-time-link" role="status">
              <p><strong>Copy this link now.</strong> It is shown only once and is not stored. Send it to the client privately.
                {' '}Valid until {formatDateTime(link.expires_at)}.</p>
              <div className="copy-row">
                <input readOnly value={link.access_url} aria-label="New access link" onFocus={(e) => e.target.select()} />
                <button type="button" className="button primary small" onClick={() => void copy()}>{copied ? 'Copied' : 'Copy link'}</button>
                <button type="button" className="button ghost small" onClick={() => setLink(null)}>Done</button>
              </div>
            </div>
          )}
        </td></tr>
      )}
    </>
  );
}

function EditClient({ client, onSaved }: { client: ClientData; onSaved: () => void }) {
  const [form, setForm] = useState({ name: client.name, email: client.email ?? '', company: client.company ?? '', phone: client.phone ?? '' });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (f: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [f]: e.target.value });

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!form.name.trim()) { setError('Name is required.'); return; }
    setBusy(true); setError(null);
    try { await api.patch(`/api/admin/clients/${client.id}`, form); onSaved(); }
    catch (err) {
      setError(err instanceof ApiError ? (err.fieldErrors.length ? `Check ${err.fieldErrors.map((f) => f.loc.at(-1)).join(', ')}.` : err.message) : 'Could not save.');
    } finally { setBusy(false); }
  }

  return (
    <form className="panel form-grid" onSubmit={submit} aria-label="Edit client" noValidate>
      <label htmlFor="ec-name">Name</label><input id="ec-name" value={form.name} onChange={set('name')} maxLength={200} />
      <label htmlFor="ec-email">Email</label><input id="ec-email" type="email" value={form.email} onChange={set('email')} />
      <label htmlFor="ec-company">Company</label><input id="ec-company" value={form.company} onChange={set('company')} maxLength={300} />
      <label htmlFor="ec-phone">Phone</label><input id="ec-phone" value={form.phone} onChange={set('phone')} maxLength={40} />
      <p className="hint">The CRM link cannot be changed here.</p>
      {error && <p className="field-error" role="alert">{error}</p>}
      <div className="form-actions"><button type="submit" className="button primary" disabled={busy}>{busy ? 'Saving…' : 'Save changes'}</button></div>
    </form>
  );
}
