import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { api, onUnauthorized } from '../api/client';

type AuthState = 'checking' | 'authenticated' | 'anonymous';

interface AuthValue {
  state: AuthState;
  login: (password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>('checking');

  useEffect(() => {
    let current = true;
    api.get('/api/admin/me')
      .then(() => { if (current) setState('authenticated'); })
      .catch(() => { if (current) setState('anonymous'); });
    const off = onUnauthorized(() => setState('anonymous'));
    return () => { current = false; off(); };
  }, []);

  const login = useCallback(async (password: string) => {
    await api.post('/api/admin/login', { password });
    setState('authenticated');
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post('/api/admin/logout');
    } finally {
      setState('anonymous');
    }
  }, []);

  const value = useMemo(() => ({ state, login, logout }), [state, login, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error('useAuth must be used inside AuthProvider');
  return value;
}
