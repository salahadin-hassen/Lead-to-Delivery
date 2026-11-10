import { useState, type FormEvent } from 'react';
import { ApiError } from '../api/client';
import { useAuth } from '../auth/AuthContext';

const MESSAGES: Record<string, string> = {
  invalid_credentials: 'That password is not correct.',
  login_throttled: 'Too many failed attempts. Wait a few minutes, then try again.',
  admin_not_configured: 'Administrator sign-in is not set up on the server (ADMIN_PASSWORD_HASH).',
};

export function Login() {
  const { login } = useAuth();
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!password) { setError('Enter the administrator password.'); return; }
    setBusy(true);
    setError(null);
    try {
      await login(password);
    } catch (err) {
      setError(err instanceof ApiError ? (MESSAGES[err.code ?? ''] ?? err.message) : 'Sign-in failed.');
    } finally {
      setPassword(''); // never kept after an attempt
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <form className="login-panel" onSubmit={submit} noValidate>
        <div className="brand"><span className="brand-mark" aria-hidden="true" /><span>Lead-to-Delivery</span></div>
        <h1>Sign in</h1>
        <label htmlFor="password">Administrator password</label>
        <input id="password" type="password" autoComplete="current-password" value={password}
               onChange={(e) => setPassword(e.target.value)} aria-invalid={error ? true : undefined}
               aria-describedby={error ? 'login-error' : undefined} disabled={busy} autoFocus />
        {error && <p id="login-error" className="field-error" role="alert">{error}</p>}
        <button type="submit" className="button primary" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
      </form>
    </main>
  );
}
