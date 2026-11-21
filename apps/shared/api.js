// Shared API client for SiREN prototype: login, REST, WebSocket.
// Contract: contracts/openapi.yaml + contracts/events.json. Wire format snake_case.

const STORE_KEY = 'siren.session';
const CRED_KEY = 'siren.credentials';

export function loadSession() {
  try {
    const s = JSON.parse(sessionStorage.getItem(STORE_KEY) || 'null');
    return s || null;
  } catch { return null; }
}

export function loadCredentials() {
  try {
    const c = JSON.parse(sessionStorage.getItem(CRED_KEY) || 'null');
    return c || null;
  } catch { return null; }
}

export async function login(app, username, password) {
  const r = await fetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw apiError(r.status, j);
  const sess = { app, token: j.token, refresh_token: j.refresh_token, role: j.role, member_id: j.member_id };
  sessionStorage.setItem(STORE_KEY, JSON.stringify(sess));
  // Remember the credentials so a server restart (tokens live in process memory) can
  // re-login transparently instead of showing a dead "missing bearer token" toast.
  sessionStorage.setItem(CRED_KEY, JSON.stringify({ app, username, password }));
  return sess;
}

export function apiError(status, body) {
  const e = new Error(body?.error || `HTTP ${status}`);
  e.status = status;
  e.detail = body?.detail || null;
  e.allowed = body?.allowed || null;
  return e;
}

// Indonesian toast text for an API error, including `allowed` when present.
export function errText(e) {
  let t = `${e.error || e.message}`;
  if (e.detail) t += ` — ${e.detail}`;
  if (e.allowed?.length) t += ` · boleh: ${e.allowed.join(', ')}`;
  return t;
}

export async function rest(method, path, body, retry = true) {
  const s = loadSession();
  const headers = { 'Content-Type': 'application/json' };
  if (s?.token) headers.Authorization = `Bearer ${s.token}`;
  const r = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const err = apiError(r.status, await r.json().catch(() => ({})));
    const creds = loadCredentials();
    if (err.status === 401 && retry && creds) {
      await login(creds.app, creds.username, creds.password);
      return rest(method, path, body, false);
    }
    throw err;
  }
  return r.status === 204 ? null : r.json();
}

// Restore a stored session, but verify it against the server first: tokens live in process
// memory, so a restarted dev server invalidates them. auth/refresh is the probe because it
// requires a valid bearer token (most GET endpoints are unauthenticated). On success the
// server rotates a fresh access token; on 401 re-login transparently when credentials are
// known, otherwise drop the session and show the login form.
export async function ensureSession(app) {
  const s = loadSession();
  if (!s || s.app !== app) return null;
  try {
    const r = await rest('POST', '/auth/refresh', { refresh_token: s.refresh_token }, false);
    const sess = { app, token: r.token, refresh_token: r.refresh_token ?? s.refresh_token,
                   role: s.role, member_id: s.member_id };
    sessionStorage.setItem(STORE_KEY, JSON.stringify(sess));
    return sess;
  } catch (e) {
    if (e.status !== 401) return s;
    const c = loadCredentials();
    if (c && c.app === app) return login(c.app, c.username, c.password);
    sessionStorage.removeItem(STORE_KEY);
    return null;
  }
}

// WebSocket per contracts/events.json: envelope {id,type,ts,v,payload}.
// Tracks last event id; on reconnect sends snapshot.request with since_event_id.
export function connectWS(app, token, handlers) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}/ws?app=${app}&token=${encodeURIComponent(token)}`;
  const client_id = 'ui_' + Math.random().toString(36).slice(2, 8);
  let ws = null, lastEventId = null, closed = false, hbTimer = null, retryTimer = null;

  function open() {
    ws = new WebSocket(url);
    ws.onopen = () => {
      send('snapshot.request', lastEventId ? { since_event_id: lastEventId } : {});
      hbTimer = setInterval(() => {
        send('heartbeat', { client_id, app, member_id: handlers.memberId ? handlers.memberId() : null });
      }, 30000);
    };
    ws.onmessage = (m) => {
      let ev;
      try { ev = JSON.parse(m.data); } catch { return; }
      if (ev.type === 'error') { handlers.onError?.(ev.payload); return; }
      if (ev.type === 'snapshot') lastEventId = ev.payload.last_event_id ?? null;
      else if (ev.id) lastEventId = ev.id;
      handlers.onEvent(ev);
    };
    ws.onclose = () => {
      clearInterval(hbTimer);
      if (!closed) retryTimer = setTimeout(open, 1000);
    };
  }

  function send(type, payload) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type, payload }));
  }

  open();
  return {
    send,
    close() { closed = true; clearInterval(hbTimer); clearTimeout(retryTimer); ws?.close(); },
  };
}
