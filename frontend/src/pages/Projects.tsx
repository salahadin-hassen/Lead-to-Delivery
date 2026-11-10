import { useState } from 'react';
import { api } from '../api/client';
import type { Page, ProjectSummary } from '../api/types';
import { StatusBadge } from '../components/Badge';
import { PageHeader } from '../components/Layout';
import { Pagination } from '../components/Pagination';
import { ProgressBar } from '../components/ProgressBar';
import { Empty, ErrorNotice, Loading } from '../components/States';
import { formatDate } from '../format';
import { Link } from '../router';
import { useResource } from '../useResource';

const LIMIT = 20;

export function Projects() {
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [offset, setOffset] = useState(0);
  const { data, error, loading, reload } = useResource(
    () => api.get<Page<ProjectSummary>>('/api/admin/projects', { q: search, status, limit: LIMIT, offset }),
    `${search}|${status}|${offset}`);
  return (
    <>
      <PageHeader title="Projects" />
      <form className="filters" role="search" onSubmit={(e) => { e.preventDefault(); setOffset(0); setSearch(query.trim()); }}>
        <label htmlFor="project-search">Search</label>
        <input id="project-search" type="search" value={query} placeholder="Project or client"
               onChange={(e) => setQuery(e.target.value)} maxLength={100} />
        <label htmlFor="project-status">Status</label>
        <select id="project-status" value={status} onChange={(e) => { setOffset(0); setStatus(e.target.value); }}>
          <option value="">All</option><option value="active">Active</option><option value="on_hold">On hold</option>
          <option value="completed">Completed</option><option value="cancelled">Cancelled</option>
        </select>
        <button type="submit" className="button">Search</button>
      </form>
      {loading && !data ? <Loading /> : error ? <ErrorNotice error={error} onRetry={reload} /> : data && (
        data.items.length === 0 ? <Empty>{search || status ? 'No projects match these filters.' : 'No projects yet. A project is created when a client submits onboarding.'}</Empty> : (
          <>
            <table className="table">
              <thead><tr><th>Project</th><th>Client</th><th>Service</th><th>Progress</th><th>Due</th><th>Status</th></tr></thead>
              <tbody>
                {data.items.map((p) => (
                  <tr key={p.id}>
                    <td><Link to={`/admin/projects/${p.id}`}>{p.name}</Link></td>
                    <td>{p.client_id ? <Link to={`/admin/clients/${p.client_id}`}>{p.client_name}</Link> : '—'}</td>
                    <td>{p.template_name ?? '—'}</td>
                    <td><ProgressBar progress={p.progress} /></td>
                    <td>{formatDate(p.due_date)}</td>
                    <td><StatusBadge status={p.status} /></td>
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
