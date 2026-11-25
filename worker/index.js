// diamondmetrics-site Worker. Only /api/* reaches this script (assets.run_worker_first in wrangler.jsonc);
// every other path is a static file served from public/ without running any code.
//
// GET /api/linescore/{gamePk}: the game's state and line score from the MLB Stats API, trimmed to what the page shows.

const UPSTREAM = 'https://statsapi.mlb.com/api/v1/schedule?sportId=1&hydrate=linescore&gamePk=';
const TIMEOUT_MS = 5000;

// Seconds: how long the edge keeps a copy, and the max-age the browser is told.
export const EDGE_TTL = { final: 86400, live: 15, preview: 300 };
export const BROWSER_TTL = { final: 86400, live: 10, preview: 300 };

const BASE_HEADERS = {
  'Content-Type': 'application/json; charset=utf-8',
  'X-Content-Type-Options': 'nosniff',
  'Content-Security-Policy': "default-src 'none'",
  'Referrer-Policy': 'strict-origin-when-cross-origin',
};

function reply(body, status, cacheControl, method, extra) {
  const headers = new Headers({ ...BASE_HEADERS, 'Cache-Control': cacheControl, ...(extra || {}) });
  return new Response(method === 'HEAD' ? null : JSON.stringify(body), { status, headers });
}

const error = (status, message, method, extra) => reply({ error: message }, status, 'no-store', method, extra);

const int = (x) => (typeof x === 'number' && Number.isFinite(x) ? x : null);
const STATES = { Preview: 'preview', Live: 'live', Final: 'final' };

// The upstream schedule response -> the trimmed shape, or null when the game is not in it.
export function trim(data) {
  const game = data && Array.isArray(data.dates) && data.dates[0] && Array.isArray(data.dates[0].games) ? data.dates[0].games[0] : null;
  if (!game) return null;
  const status = game.status || {};
  const ls = game.linescore || {};
  const half = (side) => (side && int(side.runs) !== null ? side.runs : null);
  const rhe = (t) => ({ r: int(t && t.runs), h: int(t && t.hits), e: int(t && t.errors) });
  const teams = ls.teams || {};
  return {
    state: STATES[status.abstractGameState] || 'preview',
    detail: typeof status.detailedState === 'string' ? status.detailedState : '',
    inning: int(ls.currentInning),
    half: typeof ls.inningState === 'string' ? ls.inningState.toLowerCase() : null,
    outs: int(ls.outs),
    innings: (Array.isArray(ls.innings) ? ls.innings : [])
      .filter((i) => i && int(i.num) !== null)
      .map((i) => ({ n: i.num, away: half(i.away), home: half(i.home) })),
    away: rhe(teams.away),
    home: rhe(teams.home),
  };
}

async function linescore(gamePk, method, ctx) {
  const cache = caches.default;
  const key = new Request('https://linescore.cache/' + gamePk);
  const hit = await cache.match(key);
  if (hit) {
    // The stored copy carries the edge lifetime; the browser gets its own max-age again.
    const body = await hit.json();
    return reply(body, 200, 'public, max-age=' + (BROWSER_TTL[body.state] ?? 10), method);
  }

  let data;
  try {
    const r = await fetch(UPSTREAM + gamePk, { signal: AbortSignal.timeout(TIMEOUT_MS), headers: { Accept: 'application/json' } });
    if (!r.ok) return error(502, 'upstream error', method);
    data = await r.json();
  } catch (e) {
    return error(502, 'upstream error', method);
  }
  const body = trim(data);
  if (!body) return error(404, 'unknown game', method);

  const stored = new Response(JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'max-age=' + EDGE_TTL[body.state] },
  });
  const put = cache.put(key, stored);
  if (ctx && typeof ctx.waitUntil === 'function') ctx.waitUntil(put); else await put;
  return reply(body, 200, 'public, max-age=' + BROWSER_TTL[body.state], method);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/api/')) return env.ASSETS.fetch(request);
    const method = request.method;
    if (method !== 'GET' && method !== 'HEAD') return error(405, 'method not allowed', method, { Allow: 'GET, HEAD' });
    const m = /^\/api\/linescore\/([^/]+)$/.exec(url.pathname);
    if (!m) return error(404, 'not found', method);
    if (!/^\d{1,8}$/.test(m[1])) return error(400, 'gamePk must be 1 to 8 digits', method);
    return linescore(m[1], method, ctx);
  },
};
