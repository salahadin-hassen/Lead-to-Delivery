import { useState, type FormEvent } from 'react';
import { api, ApiError } from '../api/client';
import type { Client, Page } from '../api/types';
import { StatusBadge } from '../components/Badge';
import { PageHeader } from '../components/Layout';
import { Pagination } from '../components/Pagination';
import { Empty, ErrorNotice, Loading } from '../components/States';
import { formatDate } from '../format';
import { Link, navigate } from '../router';
import { useResource } from '../useResource';

const LIMIT = 20;

export function Clients() {
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const key = `${search}|${status}|${offset}`;
  const { data, error, loading, reload } = useResource(
    () => api.get<Page<Client>>('/api/admin/clients', { q: search, status, limit: LIMIT, offset }), key);

  return (
    <>
      <PageHeader title="Clients">
        <button type="button" className="button primary" onClick={() => setCreating(true)}>Add client</button>
      </PageHeader>
      {creating && <NewClient onCancel={() => setCreating(false)} />}
      <form className="filters" role="search" onSubmit={(e) => { e.preventDefault(); setOffset(0); setSearch(query.trim()); }}>
        <label htmlFor="client-search">Search</label>
        <input id="client-search" type="search" value={query} placeholder="Name, company or email"
               onChange={(e) => setQuery(e.target.value)} maxLength={100} />
        <label htmlFor="client-status">Status</label>
        <select id="client-status" value={status} onChange={(e) => { setOffset(0); setStatus(e.target.value); }}>
          <option value="">All</option><option value="active">Active</option><option value="archived">Archived</option>
        </select>
        <button type="submit" className="button">Search</button>
      </form>
      {loading && !data ? <Loading /> : error ? <ErrorNotice error={error} onRetry={reload} /> : data && (
        data.items.length === 0 ? <Empty>{search || status ? 'No clients match these filters.' : 'No clients yet. Clients are created when a CRM deal is won, or with "Add client".'}</Empty> : (
          <>
            <table className="table">
              <thead><tr><th>Name</th><th>Company</th><th>Email</th><th>Projects</th><th>Status</th><th>Added</th></tr></thead>
              <tbody>
                {data.items.map((c) => (
                  <tr key={c.id}>
                    <td><Link to={`/admin/clients/${c.id}`}>{c.name}</Link></td>
                    <td>{c.company ?? '—'}</td>
                    <td>{c.email ?? '—'}</td>
                    <td>{c.project_count}{c.pending_invitations ? ` (+${c.pending_invitations} onboarding)` : ''}</td>
                    <td><StatusBadge status={c.status} /></td>
                    <td>{formatDate(c.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pagination total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          </>
        ))}
    </>
  );
}

function NewClient({ onCancel }: { onCancel: () => void }) {
  const [form, setForm] = useState({ name: '', email: '', company: '', phone: '' });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (field: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [field]: e.target.value });

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!form.name.trim()) { setError('Name is required.'); return; }
    setBusy(true); setError(null);
    try {
      const created = await api.post<Client>('/api/admin/clients', form);
      navigate(`/admin/clients/${created.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? (err.fieldErrors.length ? `Check ${err.fieldErrors.map((f) => f.loc.at(-1)).join(', ')}.` : err.message) : 'Could not create the client.');
    } finally { setBusy(false); }
  }

  return (
    <form className="panel form-grid" onSubmit={submit} aria-label="New client" noValidate>
      <h2>New client</h2>
      <label htmlFor="nc-name">Name</label><input id="nc-name" value={form.name} onChange={set('name')} required maxLength={200} />
      <label htmlFor="nc-email">Email</label><input id="nc-email" type="email" value={form.email} onChange={set('email')} />
      <label htmlFor="nc-company">Company</label><input id="nc-company" value={form.company} onChange={set('company')} maxLength={300} />
      <label htmlFor="nc-phone">Phone</label><input id="nc-phone" value={form.phone} onChange={set('phone')} maxLength={40} />
      {error && <p className="field-error" role="alert">{error}</p>}
      <div className="form-actions">
        <button type="submit" className="button primary" disabled={busy}>{busy ? 'Creating…' : 'Create client'}</button>
        <button type="button" className="button ghost" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
