// Worker tests: node --test tests/worker.test.mjs
// fetch and caches.default are stubbed, so nothing leaves this machine. Upstream answers come from tests/fixtures/
// (real MLB Stats API responses for WS5, and a hand-written doubleheader schedule).
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import worker, { trim, trimTeam, trimSeries } from '../worker/index.js';

const fx = (name) => JSON.parse(readFileSync(new URL('./fixtures/' + name, import.meta.url), 'utf8'));
const WS5 = fx('upstream-849799.json');
const WS5_BOX = fx('upstream-boxscore-849799.json');
const WS5_GAME = fx('upstream-game-849799.json');
const WS5_REGULAR = fx('upstream-regular-158-139.json');
const DH_REGULAR = fx('upstream-regular-doubleheader.json');

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });

// Upstream by URL; a test can replace any entry.
let routes, store, calls;
function realRoutes() {
  return [
    [/schedule\?sportId=1&hydrate=linescore&gamePk=849799$/, () => json(WS5)],
    [/schedule\?sportId=1&gamePk=849799$/, () => json(WS5)],
    [/game\/849799\/boxscore$/, () => json(WS5_BOX)],
    [/hydrate=seriesStatus&gamePk=849799$/, () => json(WS5_GAME)],
    [/season=2026&gameType=R&teamId=158&opponentId=139$/, () => json(WS5_REGULAR)],
  ];
}
beforeEach(() => {
  store = new Map();
  calls = [];
  routes = realRoutes();
  globalThis.caches = {
    default: {
      match: async (req) => (store.has(req.url) ? store.get(req.url).clone() : undefined),
      put: async (req, res) => { store.set(req.url, res.clone()); },
    },
  };
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), opts });
    const hit = routes.find(([re]) => re.test(String(url)));
    if (!hit) return json({ totalGames: 0, dates: [] });
    return hit[1](String(url), opts);
  };
});
const route = (re, fn) => routes.unshift([re, fn]);

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

// ======================= /api/linescore =======================
test('linescore WS5: the trimmed shape, and nothing else from upstream', async () => {
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

test('all endpoints: gamePk must be 1 to 8 digits, and bad ids never reach upstream', async () => {
  for (const ep of ['linescore', 'boxscore', 'series']) {
    for (const bad of ['abc', '123456789', '12a', '-1', '1.5']) {
      const res = await get(`/api/${ep}/${bad}`);
      assert.equal(res.status, 400, ep + ' ' + bad);
      checkHeaders(res);
      assert.equal(res.headers.get('cache-control'), 'no-store');
    }
  }
  assert.equal(calls.length, 0);
});

test('all endpoints: GET and HEAD only', async () => {
  for (const ep of ['linescore', 'boxscore', 'series']) {
    const head = await get(`/api/${ep}/849799`, 'HEAD');
    assert.equal(head.status, 200, ep);
    assert.equal(await head.text(), '');
    checkHeaders(head);
    for (const m of ['POST', 'PUT', 'DELETE', 'OPTIONS']) {
      const res = await get(`/api/${ep}/849799`, m);
      assert.equal(res.status, 405, ep + ' ' + m);
      assert.equal(res.headers.get('allow'), 'GET, HEAD');
      checkHeaders(res);
    }
  }
});

test('other /api paths are 404; anything else goes to the assets', async () => {
  assert.equal((await get('/api/other')).status, 404);
  assert.equal((await get('/api/linescore/1/extra')).status, 404);
  assert.equal((await get('/api/boxscores/1')).status, 404);
  const asset = await get('/index.html');
  assert.equal(await asset.text(), 'asset');
});

test('all endpoints: an unknown game is 404 and not cached', async () => {
  // the boxscore endpoint upstream answers 200 with empty teams for unknown games; the schedule call decides
  route(/game\/99999999\/boxscore$/, () => json({ teams: { away: {}, home: {} } }));
  for (const ep of ['linescore', 'boxscore', 'series']) {
    const res = await get(`/api/${ep}/99999999`);
    assert.equal(res.status, 404, ep);
    checkHeaders(res);
    assert.equal(res.headers.get('cache-control'), 'no-store');
  }
  assert.equal(store.size, 0);
});

test('all endpoints: upstream error, bad JSON and timeout are 502 and not cached', async () => {
  const bad = [
    () => new Response('oops', { status: 500 }),
    () => new Response('<html>', { status: 200 }),
    () => { throw new DOMException('The operation timed out.', 'TimeoutError'); },
    () => { throw new TypeError('network'); },
  ];
  // each upstream call of each endpoint, one at a time
  const targets = [
    ['linescore', /hydrate=linescore/], ['boxscore', /sportId=1&gamePk=849799$/], ['boxscore', /boxscore$/],
    ['series', /hydrate=seriesStatus/], ['series', /gameType=R/],
  ];
  for (const [ep, re] of targets) {
    for (const b of bad) {
      routes = realRoutes();
      route(re, b);
      store.clear();
      const res = await get(`/api/${ep}/849799`);
      assert.equal(res.status, 502, ep + ' ' + re);
      checkHeaders(res);
      assert.equal(res.headers.get('cache-control'), 'no-store');
      assert.equal(store.size, 0);
    }
  }
});

test('linescore: cache lifetime by state, edge copy and browser header', async () => {
  for (const [abstract, edge, browser] of [['Final', 86400, 86400], ['Live', 15, 10], ['Preview', 300, 300]]) {
    store.clear();
    route(/gamePk=745001$/, () => json(game(abstract, abstract, { innings: [] })));
    const res = await get('/api/linescore/745001');
    assert.equal(res.headers.get('cache-control'), 'public, max-age=' + browser, abstract);
    assert.equal(store.get('https://linescore.cache/745001').headers.get('cache-control'), 'max-age=' + edge, abstract);
  }
});

test('linescore: a cache hit skips upstream and sets the browser max-age again', async () => {
  route(/gamePk=745002$/, () => json(game('Live', 'In Progress', { currentInning: 5, inningState: 'Bottom', outs: 2, innings: [] })));
  await get('/api/linescore/745002');
  assert.equal(calls.length, 1);
  const hit = await get('/api/linescore/745002');
  assert.equal(calls.length, 1, 'served from the cache');
  assert.equal(hit.status, 200);
  checkHeaders(hit);
  assert.equal(hit.headers.get('cache-control'), 'public, max-age=10', 'not the 15-second edge value');
  assert.equal((await hit.json()).half, 'bottom');
});

test('linescore trim: innings not batted and missing totals are null; preview has an empty line score', () => {
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

test('linescore trim: postponed and suspended keep their detail text', () => {
  assert.equal(trim(game('Final', 'Postponed', {})).detail, 'Postponed');
  const s = trim(game('Live', 'Suspended: Rain', { currentInning: 6, inningState: 'Top', innings: [] }));
  assert.equal(s.detail, 'Suspended: Rain');
  assert.equal(s.state, 'live');
});

// ======================= /api/boxscore =======================
test('boxscore WS5: starters with their lines and the starting catchers, nothing else', async () => {
  const res = await get('/api/boxscore/849799');
  assert.equal(res.status, 200);
  checkHeaders(res);
  assert.equal(res.headers.get('cache-control'), 'public, max-age=86400');
  const body = await res.json();
  assert.deepEqual(body, {
    state: 'final',
    away: { pitcher: { id: 694819, name: 'Jacob Misiorowski', line: { ip: '5.2', h: 2, bb: 1, so: 2 } }, catcher: { id: 661388, name: 'William Contreras' } },
    home: { pitcher: { id: 656876, name: 'Drew Rasmussen', line: { ip: '4.0', h: 2, bb: 0, so: 4 } }, catcher: { id: 689414, name: 'Liam Hicks' } },
  });
  assert.ok(!JSON.stringify(body).includes('Megill') && !JSON.stringify(body).includes('copyright'), 'nothing raw passed through');
  assert.equal(calls.length, 2, 'the schedule (for the state) and the boxscore');
});

test('boxscore: the starting catcher is a starter (n00) whose first position was C, not a substitute', () => {
  const p = (id, name, order, positions, pos) => ({ person: { id, fullName: name }, battingOrder: order, allPositions: positions && positions.map((a) => ({ abbreviation: a })), position: { abbreviation: pos } });
  const team = {
    pitchers: [],
    players: {
      ID1: p(1, 'Sub Catcher', '701', ['PH', 'C'], 'C'),
      ID2: p(2, 'Starting Catcher', '400', ['C', '1B'], '1B'), // moved to first base later: still the starting catcher
      ID3: p(3, 'Bench Catcher', undefined, undefined, 'C'),
    },
  };
  assert.deepEqual(trimTeam(team).catcher, { id: 2, name: 'Starting Catcher' });
  // before the game: a posted lineup has batting order and position but no allPositions yet
  const posted = { pitchers: [], players: { ID5: p(5, 'Posted Catcher', '800', undefined, 'C'), ID6: p(6, 'Posted Short', '900', undefined, 'SS') } };
  assert.deepEqual(trimTeam(posted), { pitcher: null, catcher: { id: 5, name: 'Posted Catcher' } });
  // no lineup yet
  assert.deepEqual(trimTeam({ pitchers: [], players: { ID3: team.players.ID3 } }), { pitcher: null, catcher: null });
  assert.deepEqual(trimTeam(undefined), { pitcher: null, catcher: null });
});

test('boxscore: the starter is the first pitcher; a starter with no pitching stats yet has a null line', () => {
  const t = trimTeam({ pitchers: [10, 11], players: { ID10: { person: { id: 10, fullName: 'Opener Guy' }, stats: { pitching: {} } }, ID11: { person: { id: 11, fullName: 'Bulk Guy' } } } });
  assert.deepEqual(t.pitcher, { id: 10, name: 'Opener Guy', line: { ip: null, h: null, bb: null, so: null } });
});

test('boxscore: cache lifetime follows the game state; a hit sets the browser max-age again', async () => {
  route(/sportId=1&gamePk=745003$/, () => json(game('Live', 'In Progress', {})));
  route(/game\/745003\/boxscore$/, () => json({ teams: { away: {}, home: {} } }));
  const res = await get('/api/boxscore/745003');
  assert.equal(res.headers.get('cache-control'), 'public, max-age=10');
  assert.equal(store.get('https://boxscore.cache/745003').headers.get('cache-control'), 'max-age=15');
  assert.deepEqual(await res.json(), { state: 'live', away: { pitcher: null, catcher: null }, home: { pitcher: null, catcher: null } });
  const n = calls.length;
  const hit = await get('/api/boxscore/745003');
  assert.equal(calls.length, n, 'served from the cache');
  assert.equal(hit.headers.get('cache-control'), 'public, max-age=10');
  checkHeaders(hit);
});

// ======================= /api/series =======================
test('series WS5: Brewers 2-1 in the regular season, no sweeps, World Series won 4-1', async () => {
  const res = await get('/api/series/849799');
  assert.equal(res.status, 200);
  checkHeaders(res);
  const body = await res.json();
  assert.deepEqual(body, {
    season: 2026,
    regular: { awayWins: 2, homeWins: 1, played: 3, left: 0, sweeps: [], seasonSweep: null },
    postseason: { description: 'World Series', result: { leader: 'away', wins: 4, losses: 1, over: true } },
  });
  assert.equal(res.headers.get('cache-control'), 'public, max-age=86400', 'no meetings left: a day');
  assert.match(calls[1].url, /season=2026&gameType=R&teamId=158&opponentId=139$/);
  assert.ok(!JSON.stringify(body).includes('copyright') && !JSON.stringify(body).includes('Grapefruit'));
});

// Synthetic games in the upstream shape, for the counting rules.
const G = (pk, when, state, detail, winner, num, of, awayId = 158, homeId = 112) => ({
  gamePk: pk, gameDate: when, officialDate: when.slice(0, 10), status: { abstractGameState: state, detailedState: detail },
  teams: { away: { team: { id: awayId }, isWinner: winner === awayId }, home: { team: { id: homeId }, isWinner: winner === homeId } },
  seriesGameNumber: num, gamesInSeries: of,
});
const sched = (...games) => ({ dates: games.map((g) => ({ date: g.officialDate, games: [g] })) });
const thisGame = (when, type = 'R', extra = {}) => ({ gameDate: when, season: '2026', gameType: type, teams: { away: { team: { id: 158 } }, home: { team: { id: 112 } } }, ...extra });

test('series doubleheader: on game 1 of a doubleheader, game 2 does not count yet', () => {
  const g1 = thisGame('2026-05-12T17:05:00Z');
  const s1 = trimSeries(g1, DH_REGULAR);
  assert.deepEqual(s1.regular, { awayWins: 2, homeWins: 0, played: 2, left: 2, sweeps: [], seasonSweep: 'away' });
  // on game 2's page, the same day: all three count, and the series is a sweep
  const s2 = trimSeries(thisGame('2026-05-12T23:10:00Z'), DH_REGULAR);
  assert.deepEqual(s2.regular, { awayWins: 3, homeWins: 0, played: 3, left: 1, sweeps: [{ winner: 'away', games: 3, startDate: '2026-05-11', endDate: '2026-05-12' }], seasonSweep: 'away' });
});

test('series: a later meeting already final does not count on an earlier game', () => {
  const s = trimSeries(thisGame('2026-04-01T23:00:00Z'), sched(
    G(1, '2026-04-01T23:00:00Z', 'Final', 'Final', 158, 1, 2),
    G(2, '2026-04-02T23:00:00Z', 'Final', 'Final', 112, 2, 2)));
  assert.deepEqual(s.regular, { awayWins: 1, homeWins: 0, played: 1, left: 1, sweeps: [], seasonSweep: 'away' });
});

test('series: a tied series, an incomplete series is not a sweep, the side follows this game', () => {
  const s = trimSeries(thisGame('2026-06-30T23:00:00Z'), sched(
    G(1, '2026-06-01T23:00:00Z', 'Final', 'Final', 158, 1, 3),
    G(2, '2026-06-02T23:00:00Z', 'Final', 'Final', 112, 2, 3, 112, 158), // the teams swapped home and away
    G(3, '2026-07-10T23:00:00Z', 'Preview', 'Scheduled', null, 1, 3)));
  assert.deepEqual(s.regular, { awayWins: 1, homeWins: 1, played: 2, left: 1, sweeps: [], seasonSweep: null });
});

test('series: a completed season sweep, and a series sweep with its dates', () => {
  const s = trimSeries(thisGame('2026-09-01T23:00:00Z'), sched(
    G(1, '2026-06-30T23:00:00Z', 'Final', 'Final', 112, 1, 3),
    G(2, '2026-07-01T23:00:00Z', 'Final', 'Final', 112, 2, 3),
    G(3, '2026-07-02T23:00:00Z', 'Final', 'Final', 112, 3, 3)));
  assert.deepEqual(s.regular, { awayWins: 0, homeWins: 3, played: 3, left: 0, sweeps: [{ winner: 'home', games: 3, startDate: '2026-06-30', endDate: '2026-07-02' }], seasonSweep: 'home' });
});

test('series: postponed and rescheduled counts once; cancelled is gone; a still-postponed game is left', () => {
  const s = trimSeries(thisGame('2026-08-31T23:00:00Z'), sched(
    G(7, '2026-05-01T23:00:00Z', 'Final', 'Postponed', null, 1, 2),
    G(8, '2026-05-02T23:00:00Z', 'Final', 'Final', 158, 2, 2),
    G(7, '2026-08-01T23:00:00Z', 'Final', 'Final', 158, 1, 1),
    G(9, '2026-08-02T23:00:00Z', 'Final', 'Cancelled', null, 1, 1),
    G(10, '2026-08-03T23:00:00Z', 'Final', 'Postponed', null, 1, 1)));
  assert.deepEqual(s.regular, { awayWins: 2, homeWins: 0, played: 2, left: 1, sweeps: [], seasonSweep: 'away' });
});

test('series: postseason in progress (tied, and a leader), and none for a regular-season game', () => {
  const tied = trimSeries(thisGame('2026-10-20T00:00:00Z', 'L', { seriesStatus: { description: 'NLCS', isTied: true, isOver: false, wins: 2, losses: 2, winningTeam: { id: 112 } } }), sched());
  assert.deepEqual(tied.postseason, { description: 'NLCS', result: { leader: null, wins: 2, losses: 2, over: false } });
  const lead = trimSeries(thisGame('2026-10-20T00:00:00Z', 'W', { seriesStatus: { description: 'World Series', isTied: false, isOver: false, wins: 3, losses: 1, winningTeam: { id: 112 } } }), sched());
  assert.deepEqual(lead.postseason, { description: 'World Series', result: { leader: 'home', wins: 3, losses: 1, over: false } });
  assert.equal(trimSeries(thisGame('2026-06-01T23:00:00Z', 'R', { seriesStatus: { description: 'x' } }), sched()).postseason, null);
  assert.deepEqual(trimSeries(thisGame('2026-06-01T23:00:00Z'), sched()).regular, { awayWins: 0, homeWins: 0, played: 0, left: 0, sweeps: [], seasonSweep: null });
});

test('series: an hour while meetings are left, a day once none are; a hit keeps it', async () => {
  route(/hydrate=seriesStatus&gamePk=745004$/, () => json({ dates: [{ games: [thisGame('2026-06-01T23:00:00Z')] }] }));
  route(/teamId=158&opponentId=112$/, () => json(sched(G(1, '2026-06-01T23:00:00Z', 'Final', 'Final', 158, 1, 3), G(2, '2026-06-02T23:00:00Z', 'Preview', 'Scheduled', null, 2, 3))));
  const res = await get('/api/series/745004');
  assert.equal(res.headers.get('cache-control'), 'public, max-age=3600');
  assert.equal(store.get('https://series.cache/745004').headers.get('cache-control'), 'max-age=3600');
  const n = calls.length;
  const hit = await get('/api/series/745004');
  assert.equal(calls.length, n);
  assert.equal(hit.headers.get('cache-control'), 'public, max-age=3600');
  checkHeaders(hit);
  assert.equal((await hit.json()).regular.left, 1);
});
