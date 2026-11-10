import type { ReactNode } from 'react';
import { useAuth } from '../auth/AuthContext';
import { Link, useLocation } from '../router';

const NAV = [
  { to: '/admin', label: 'Dashboard', match: (p: string) => p === '/admin' || p === '/admin/' },
  { to: '/admin/clients', label: 'Clients', match: (p: string) => p.startsWith('/admin/clients') },
  { to: '/admin/projects', label: 'Projects', match: (p: string) => p.startsWith('/admin/projects') },
  { to: '/admin/integrations', label: 'Integrations', match: (p: string) => p.startsWith('/admin/integrations') },
];

export function Layout({ children }: { children: ReactNode }) {
  const { path } = useLocation();
  const { logout, logoutEverywhere, admin } = useAuth();
  return (
    <div className="shell">
      <a className="skip-link" href="#main">Skip to content</a>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <span>Lead-to-Delivery</span>
        </div>
        <nav aria-label="Main">
          <ul>
            {NAV.map((item) => (
              <li key={item.to}>
                <Link to={item.to} className="nav-link" aria-current={item.match(path) ? 'page' : undefined}>
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <div className="sidebar-account">
          {admin && <p className="signed-in" title={admin.email}>{admin.display_name}<span>{admin.email}</span></p>}
          <button type="button" className="button ghost sign-out" onClick={() => { void logout(); }}>
            Sign out
          </button>
          <button type="button" className="link-button sign-out-all" onClick={() => { void logoutEverywhere(); }}>
            Sign out on all devices
          </button>
        </div>
      </aside>
      <main id="main" className="content" tabIndex={-1}>{children}</main>
    </div>
  );
}

export function PageHeader({ title, children, back }: { title: string; children?: ReactNode; back?: { to: string; label: string } }) {
  return (
    <header className="page-header">
      <div>
        {back && <Link to={back.to} className="back-link">{back.label}</Link>}
        <h1>{title}</h1>
      </div>
      {children && <div className="page-actions">{children}</div>}
    </header>
  );
}
