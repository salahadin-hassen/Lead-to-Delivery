import { useState, type FormEvent } from 'react';
import { ApiError } from '../api/client';
import { useAuth } from '../auth/AuthContext';

const MESSAGES: Record<string, string> = {
  invalid_credentials: 'The email, password or code is not correct.',
  login_throttled: 'Too many failed attempts. Wait a few minutes, then try again.',
  admin_not_configured: 'Administrator sign-in is not set up on the server (ADMIN_MFA_ENCRYPTION_KEY).',
};

export function Login() {
  const { login } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!email.trim() || !password || !code.trim()) {
      setError('Enter your email, password and the code from your authenticator app.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await login(email.trim(), password, code.trim());
    } catch (err) {
      setError(err instanceof ApiError ? (MESSAGES[err.code ?? ''] ?? err.message) : 'Sign-in failed.');
    } finally {
      setPassword(''); // never kept after an attempt
      setCode('');
      setBusy(false);
    }
  }

  const invalid = error ? true : undefined;
  const describedBy = error ? 'login-error' : undefined;
  return (
    <main className="login">
      <form className="login-panel" onSubmit={submit} noValidate>
        <div className="brand"><span className="brand-mark" aria-hidden="true" /><span>Lead-to-Delivery</span></div>
        <h1>Sign in</h1>
        <label htmlFor="email">Email</label>
        <input id="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)}
               aria-invalid={invalid} aria-describedby={describedBy} disabled={busy} autoFocus />
        <label htmlFor="password">Password</label>
        <input id="password" type="password" autoComplete="current-password" value={password}
               onChange={(e) => setPassword(e.target.value)} aria-invalid={invalid} aria-describedby={describedBy}
               disabled={busy} />
        <label htmlFor="code">Authenticator code</label>
        <input id="code" type="text" inputMode="numeric" autoComplete="one-time-code" value={code}
               onChange={(e) => setCode(e.target.value)} aria-invalid={invalid}
               aria-describedby={['code-hint', describedBy].filter(Boolean).join(' ')} disabled={busy} />
        <p id="code-hint" className="hint">6 digits from your app, or one of your recovery codes.</p>
        {error && <p id="login-error" className="field-error" role="alert">{error}</p>}
        <button type="submit" className="button primary" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
      </form>
    </main>
  );
}
