import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, onUnauthorized } from '../api/client';

type AuthState = 'checking' | 'authenticated' | 'anonymous';

export interface AdminIdentity { email: string; display_name: string }

interface AuthValue {
  state: AuthState;
  admin: AdminIdentity | null;
  login: (email: string, password: string, code: string) => Promise<void>;
  logout: () => Promise<void>;
  logoutEverywhere: () => Promise<void>;
}

interface MeResponse { authenticated: boolean; admin?: AdminIdentity }

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>('checking');
  const [admin, setAdmin] = useState<AdminIdentity | null>(null);

  const refresh = useCallback(async () => {
    const me = await api.get<MeResponse>('/api/admin/me');
    setAdmin(me?.admin ?? null);
    setState('authenticated');
  }, []);

  useEffect(() => {
    let current = true;
    refresh().catch(() => { if (current) setState('anonymous'); });
    const off = onUnauthorized(() => { setAdmin(null); setState('anonymous'); });
    return () => { current = false; off(); };
  }, [refresh]);

  const login = useCallback(async (email: string, password: string, code: string) => {
    await api.post('/api/admin/login', { email, password, code });
    await refresh();
  }, [refresh]);

  const end = useCallback(async (path: string) => {
    try {
      await api.post(path);
    } finally {
      setAdmin(null);
      setState('anonymous');
    }
  }, []);
  const logout = useCallback(() => end('/api/admin/logout'), [end]);
  const logoutEverywhere = useCallback(() => end('/api/admin/logout-all'), [end]);

  const value = useMemo(() => ({ state, admin, login, logout, logoutEverywhere }),
                        [state, admin, login, logout, logoutEverywhere]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}
