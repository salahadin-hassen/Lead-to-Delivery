// The client's access token arrives in the URL fragment (/portal#t=<token>).
//
// * Fragments are never sent to a server or in a Referer header.
// * On load the token is read once, then removed from the address bar
//   (history.replaceState), so it does not stay visible, in screenshots or in
//   bookmarks made later.
// * It is kept in memory only: never in localStorage, sessionStorage or a cookie.
//   Reloading the page therefore needs the original link again.

const TOKEN_RE = /^[A-Za-z0-9_-]{32,128}$/;

export type TokenState = { kind: 'none' } | { kind: 'malformed' } | { kind: 'ok'; token: string };

let current: TokenState = { kind: 'none' };

/** Read #t=... from the current URL (if present), then strip the fragment. */
export function captureToken(): TokenState {
  const hash = window.location.hash.replace(/^#/, '');
  const params = new URLSearchParams(hash);
  if (params.has('t')) {
    const value = (params.get('t') ?? '').trim();
    current = TOKEN_RE.test(value) ? { kind: 'ok', token: value } : { kind: 'malformed' };
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
  }
  return current;
}

export function tokenState(): TokenState {
  return current;
}

/** Tests only. */
export function resetToken(): void {
  current = { kind: 'none' };
}
