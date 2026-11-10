import { useEffect } from 'react';
import { AuthProvider, useAuth } from './auth/AuthContext';
import { Layout, PageHeader } from './components/Layout';
import { Loading } from './components/States';
import { ClientDetail } from './pages/ClientDetail';
import { Clients } from './pages/Clients';
import { Dashboard } from './pages/Dashboard';
import { Integrations } from './pages/Integrations';
import { Login } from './pages/Login';
import { ProjectDetail } from './pages/ProjectDetail';
import { Projects } from './pages/Projects';
import { Link, matchPath, navigate, useLocation } from './router';

function Routes() {
  const { path } = useLocation();
  let m: Record<string, string> | null;
  if (matchPath('/admin', path)) return <Dashboard />;
  if (matchPath('/admin/clients', path)) return <Clients />;
  if ((m = matchPath('/admin/clients/:id', path))) return <ClientDetail key={m.id} id={m.id} />;
  if (matchPath('/admin/projects', path)) return <Projects />;
  if ((m = matchPath('/admin/projects/:id', path))) return <ProjectDetail key={m.id} id={m.id} />;
  if (matchPath('/admin/integrations', path)) return <Integrations />;
  return <><PageHeader title="Page not found" /><p><Link to="/admin">Go to the dashboard</Link></p></>;
}

function Gate() {
  const { state } = useAuth();
  const { path } = useLocation();
  useEffect(() => {
    if (!path.startsWith('/admin')) navigate('/admin', { replace: true });
  }, [path]);
  if (state === 'checking') return <main className="login"><Loading label="Checking your session…" /></main>;
  if (state === 'anonymous') return <Login />;
  return <Layout><Routes /></Layout>;
}

export function App() {
  return <AuthProvider><Gate /></AuthProvider>;
}
