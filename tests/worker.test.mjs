// Worker tests: node --test tests/worker.test.mjs
// fetch and caches.default are stubbed, so nothing leaves this machine.
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import worker, { trim } from '../worker/index.js';

const WS5 = JSON.parse(readFileSync(new URL('./fixtures/upstream-849799.json', import.meta.url), 'utf8'));

let store, upstream, calls;
beforeEach(() => {
  store = new Map();
  calls = [];
  upstream = async () => new Response(JSON.stringify(WS5), { status: 200 });
  globalThis.caches = {
    default: {
      match: async (req) => (store.has(req.url) ? store.get(req.url).clone() : undefined),
      put: async (req, res) => { store.set(req.url, res.clone()); },
    },
  };
  globalThis.fetch = async (url, opts) => { calls.push({ url: String(url), opts }); return upstream(url, opts); };
});

const ctx = { waitUntil: (p) => p };
const env = { ASSETS: { fetch: async () => new Response('asset', { status: 200 }) } };
const get = (path, method = 'GET') => worker.fetch(new Request('https://app.example' + path, { method }), env, ctx);

function game(abstract, detailed, linescore) {
  return { dates: [{ games: [{ status: { abstractGameState: abstract, detailedState: detailed }, linescore }] }] };
}

function checkHeaders(res) {
  assert.equal(res.headers.get('content-type'), 'application/json; charset=utf-8');
  assert.equal(res.headers.get('x-content-type-options'), 'nosniff');
  assert.equal(res.headers.get('content-security-policy'), "default-src 'none'");
  assert.equal(res.headers.get('referrer-policy'), 'strict-origin-when-cross-origin');
}

test('WS5: the trimmed shape, and nothing else from upstream', async () => {
  const res = await get('/api/linescore/849799');
  assert.equal(res.status, 200);
  checkHeaders(res);
  const body = await res.json();
  assert.deepEqual(Object.keys(body).sort(), ['away', 'detail', 'half', 'home', 'inning', 'innings', 'outs', 'state']);
  assert.equal(body.state, 'final');
  assert.equal(body.detail, 'Final');
  assert.equal(body.inning, 9);
  assert.equal(body.half, 'bottom');
  assert.equal(body.outs, 3);
  assert.deepEqual(body.innings.map((i) => [i.n, i.away, i.home]),
    [[1, 0, 0], [2, 1, 0], [3, 0, 0], [4, 0, 0], [5, 1, 0], [6, 2, 0], [7, 0, 0], [8, 0, 0], [9, 0, 1]]);
  assert.deepEqual(body.away, { r: 4, h: 8, e: 2 });
  assert.deepEqual(body.home, { r: 1, h: 4, e: 0 });
  assert.ok(!JSON.stringify(body).includes('Megill'), 'no player data passed through');
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /^https:\/\/statsapi\.mlb\.com\/api\/v1\/schedule\?.*gamePk=849799/);
  assert.ok(calls[0].opts.signal instanceof AbortSignal, 'the upstream request has a timeout signal');
});

test('gamePk must be 1 to 8 digits', async () => {
  for (const bad of ['abc', '123456789', '12a', '-1', '1.5']) {
    const res = await get('/api/linescore/' + bad);
    assert.equal(res.status, 400, bad);
    checkHeaders(res);
    assert.equal(res.headers.get('cache-control'), 'no-store');
  }
  for (const good of ['1', '12345678']) assert.equal((await get('/api/linescore/' + good)).status, 200, good);
  assert.equal(calls.length, 2, 'bad ids never reach upstream');
});

test('GET and HEAD only', async () => {
  const head = await get('/api/linescore/849799', 'HEAD');
  assert.equal(head.status, 200);
  assert.equal(await head.text(), '');
  checkHeaders(head);
  for (const m of ['POST', 'PUT', 'DELETE', 'OPTIONS']) {
    const res = await get('/api/linescore/849799', m);
    assert.equal(res.status, 405, m);
    assert.equal(res.headers.get('allow'), 'GET, HEAD');
    checkHeaders(res);
  }
});

test('other /api paths are 404; anything else goes to the assets', async () => {
  assert.equal((await get('/api/other')).status, 404);
  assert.equal((await get('/api/linescore/1/extra')).status, 404);
  const asset = await get('/index.html');
  assert.equal(await asset.text(), 'asset');
});

test('unknown game is 404 and not cached', async () => {
  upstream = async () => new Response(JSON.stringify({ totalGames: 0, dates: [] }), { status: 200 });
  const res = await get('/api/linescore/99999999');
  assert.equal(res.status, 404);
  checkHeaders(res);
  assert.equal(res.headers.get('cache-control'), 'no-store');
  assert.equal(store.size, 0);
});

test('upstream error, bad JSON and timeout are 502 and not cached', async () => {
  const cases = [
    async () => new Response('oops', { status: 500 }),
    async () => new Response('<html>', { status: 200 }),
    async () => { throw new DOMException('The operation timed out.', 'TimeoutError'); },
    async () => { throw new TypeError('network'); },
  ];
  for (const c of cases) {
    upstream = c;
    const res = await get('/api/linescore/849799');
    assert.equal(res.status, 502);
    checkHeaders(res);
    assert.equal(res.headers.get('cache-control'), 'no-store');
  }
  assert.equal(store.size, 0);
});

test('cache lifetime by state: edge copy and browser header', async () => {
  for (const [abstract, edge, browser] of [['Final', 86400, 86400], ['Live', 15, 10], ['Preview', 300, 300]]) {
    store.clear();
    upstream = async () => new Response(JSON.stringify(game(abstract, abstract, { innings: [] })), { status: 200 });
    const res = await get('/api/linescore/745001');
    assert.equal(res.headers.get('cache-control'), 'public, max-age=' + browser, abstract);
    const stored = store.get('https://linescore.cache/745001');
    assert.equal(stored.headers.get('cache-control'), 'max-age=' + edge, abstract);
  }
});

test('a cache hit skips upstream and sets the browser max-age again', async () => {
  upstream = async () => new Response(JSON.stringify(game('Live', 'In Progress', { currentInning: 5, inningState: 'Bottom', outs: 2, innings: [] })), { status: 200 });
  await get('/api/linescore/745002');
  assert.equal(calls.length, 1);
  const hit = await get('/api/linescore/745002');
  assert.equal(calls.length, 1, 'served from the cache');
  assert.equal(hit.status, 200);
  checkHeaders(hit);
  assert.equal(hit.headers.get('cache-control'), 'public, max-age=10', 'not the 15-second edge value');
  assert.equal((await hit.json()).half, 'bottom');
});

test('trim: innings not batted and missing totals are null; preview has an empty line score', () => {
  const t = trim(game('Live', 'In Progress', {
    currentInning: 2, inningState: 'Top', outs: 1,
    innings: [{ num: 1, away: { runs: 0, hits: 0, errors: 0 }, home: { runs: 2, hits: 2, errors: 0 } }, { num: 2, away: { runs: 1 }, home: {} }],
    teams: { away: { runs: 1, hits: 1, errors: 0 }, home: { runs: 2, hits: 2 } },
  }));
  assert.deepEqual(t.innings, [{ n: 1, away: 0, home: 2 }, { n: 2, away: 1, home: null }]);
  assert.deepEqual(t.home, { r: 2, h: 2, e: null });
  assert.equal(t.half, 'top');
  const p = trim(game('Preview', 'Scheduled', undefined));
  assert.deepEqual(p, { state: 'preview', detail: 'Scheduled', inning: null, half: null, outs: null, innings: [], away: { r: null, h: null, e: null }, home: { r: null, h: null, e: null } });
});

test('trim: postponed and suspended keep their detail text', () => {
  assert.equal(trim(game('Final', 'Postponed', {})).detail, 'Postponed');
  const s = trim(game('Live', 'Suspended: Rain', { currentInning: 6, inningState: 'Top', innings: [] }));
  assert.equal(s.detail, 'Suspended: Rain');
  assert.equal(s.state, 'live');
});
