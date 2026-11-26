// Worker tests: node --test tests/worker.test.mjs
// fetch, caches.default and the ASSETS binding are stubbed, so nothing leaves this machine. Upstream answers come from
// tests/fixtures/ (real MLB Stats API responses for WS5 and three schedule dates, and a hand-written doubleheader).
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import worker, { trim, trimTeam, trimSeries, teamStanding, dayBefore, together, runningGame, trimSlate, validDate } from '../worker/index.js';

const fx = (name) => JSON.parse(readFileSync(new URL('./fixtures/' + name, import.meta.url), 'utf8'));
const WS5 = fx('upstream-849799.json');
const WS5_BOX = fx('upstream-boxscore-849799.json');
const WS5_GAME = fx('upstream-game-849799.json');
const WS5_REGULAR = fx('upstream-regular-158-139.json');
const DH_REGULAR = fx('upstream-regular-doubleheader.json');
const WS5_PROBABLE = fx('upstream-game-probable-849799.json');
const STANDINGS_2026 = fx('upstream-standings-2026.json');
const LOGS = Object.fromEntries([694819, 661388, 656876, 689414].map((id) => [id, fx(`upstream-log-${id}.json`)]));
const PEOPLE_WS5 = fx('upstream-people-ws5.json');
// Real schedules: 15 regular-season finals; two rainouts among 14 finals; World Series Game 5.
const SLATES = Object.fromEntries(['2026-04-14', '2026-05-23', '2026-10-28'].map((d) => [d, fx(`upstream-slate-${d}.json`)]));

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });

// Upstream by URL; a test can replace any entry.
let routes, store, calls, assetCalls;
function realRoutes() {
  return [
    [/schedule\?sportId=1&hydrate=linescore&gamePk=849799$/, () => json(WS5)],
    [/schedule\?sportId=1&gamePk=849799$/, () => json(WS5)],
    [/game\/849799\/boxscore$/, () => json(WS5_BOX)],
    [/hydrate=seriesStatus&gamePk=849799$/, () => json(WS5_GAME)],
    [/season=2026&gameType=R&teamId=158&opponentId=139$/, () => json(WS5_REGULAR)],
    [/hydrate=probablePitcher&gamePk=849799$/, () => json(WS5_PROBABLE)],
    [/standings\?leagueId=103,104&season=2026&standingsTypes=regularSeason&hydrate=division$/, () => json(STANDINGS_2026)],
    [/people\/(\d+)\/stats\?stats=gameLog&group=(pitching|fielding)&season=2026&gameType=R,F,D,L,W$/, (url) => json(LOGS[Number(/people\/(\d+)\//.exec(url)[1])])],
    [/people\?personIds=694819,661388,656876,689414&hydrate=stats\(group=\[pitching,fielding\],type=\[season\],season=2026\)$/, () => json(PEOPLE_WS5)],
    [/schedule\?sportId=1&date=(2026-04-14|2026-05-23|2026-10-28)&hydrate=linescore$/, (url) => json(SLATES[/date=([\d-]+)/.exec(url)[1]])],
  ];
}
beforeEach(() => {
  store = new Map();
  calls = [];
  assetCalls = [];
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
// The assets: "asset:{path}" with the headers _headers would add, status 200, and every request recorded.
const env = {
  ASSETS: {
    fetch: async (req) => {
      const u = new URL(req.url);
      assetCalls.push({ path: u.pathname, method: req.method, inm: req.headers.get('if-none-match') });
      return new Response(req.method === 'HEAD' ? null : 'asset:' + u.pathname, {
        status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Content-Security-Policy': "default-src 'none'; script-src 'self'", 'X-Frame-Options': 'DENY' },
      });
    },
  },
};
const get = (path, method = 'GET', headers) => worker.fetch(new Request('https://app.example' + path, { method, headers }), env, ctx);

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

test('all game endpoints: gamePk must be 1 to 8 digits, and bad ids never reach upstream', async () => {
  for (const ep of ['linescore', 'boxscore', 'series', 'standings', 'battery']) {
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
  for (const path of ['/api/linescore/849799', '/api/boxscore/849799', '/api/series/849799', '/api/standings/849799', '/api/battery/849799', '/api/slate/2026-10-28']) {
    const head = await get(path, 'HEAD');
    assert.equal(head.status, 200, path);
    assert.equal(await head.text(), '');
    checkHeaders(head);
    for (const m of ['POST', 'PUT', 'DELETE', 'OPTIONS']) {
      const res = await get(path, m);
      assert.equal(res.status, 405, path + ' ' + m);
      assert.equal(res.headers.get('allow'), 'GET, HEAD');
      checkHeaders(res);
    }
  }
});

test('other /api paths are 404; anything that is not /api or /game goes to the assets untouched', async () => {
  for (const p of ['/api/other', '/api/linescore/1/extra', '/api/boxscores/1', '/api/slate/', '/api/slate/2026-04-14/x', '/api/slates/2026-04-14']) {
    const res = await get(p);
    assert.equal(res.status, 404, p);
    checkHeaders(res);
  }
  for (const p of ['/', '/index.html', '/beat-the-streak', '/games', '/gamer/1', '/data/page.json']) {
    const asset = await get(p);
    assert.equal(await asset.text(), 'asset:' + p, p);
  }
  assert.deepEqual(assetCalls.map((c) => c.path), ['/', '/index.html', '/beat-the-streak', '/games', '/gamer/1', '/data/page.json']);
  assert.equal(calls.length, 0);
});

test('all game endpoints: an unknown game is 404 and not cached', async () => {
  // the boxscore endpoint upstream answers 200 with empty teams for unknown games; the schedule call decides
  route(/game\/99999999\/boxscore$/, () => json({ teams: { away: {}, home: {} } }));
  for (const ep of ['linescore', 'boxscore', 'series', 'standings', 'battery']) {
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
    ['linescore/849799', /hydrate=linescore&gamePk/], ['boxscore/849799', /sportId=1&gamePk=849799$/], ['boxscore/849799', /boxscore$/],
    ['series/849799', /hydrate=seriesStatus/], ['series/849799', /gameType=R/],
    ['standings/849799', /hydrate=probablePitcher/], ['standings/849799', /standings\?/],
    ['battery/849799', /hydrate=probablePitcher/], ['battery/849799', /boxscore$/], ['battery/849799', /people\/656876\/stats/], ['battery/849799', /people\/689414\/stats/], ['battery/849799', /people\?personIds/],
    ['slate/2026-04-14', /date=2026-04-14/],
  ];
  for (const [path, re] of targets) {
    for (const b of bad) {
      routes = realRoutes();
      route(re, b);
      store.clear();
      const res = await get(`/api/${path}`);
      assert.equal(res.status, 502, path + ' ' + re);
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

// ======================= cache-hit fallback =======================
test('a cached copy without X-Browser-Max-Age (stored by the round-4 Worker) uses the TTL for its state', async () => {
  const old = (body) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'max-age=86400' } });
  store.set('https://linescore.cache/849799', old({ state: 'final', detail: 'Final', inning: 9, half: 'bottom', outs: 3, innings: [], away: {}, home: {} }));
  const fin = await get('/api/linescore/849799');
  assert.equal(calls.length, 0, 'served from the old cached copy');
  assert.equal(fin.headers.get('cache-control'), 'public, max-age=86400', 'not the 10-second fallback');
  checkHeaders(fin);
  store.set('https://linescore.cache/745010', old({ state: 'live', innings: [] }));
  assert.equal((await get('/api/linescore/745010')).headers.get('cache-control'), 'public, max-age=10');
  store.set('https://linescore.cache/745011', old({ state: 'preview', innings: [] }));
  assert.equal((await get('/api/linescore/745011')).headers.get('cache-control'), 'public, max-age=300');
  store.set('https://linescore.cache/745012', old({ innings: [] }));
  assert.equal((await get('/api/linescore/745012')).headers.get('cache-control'), 'public, max-age=10', 'no state at all: 10');
});

// ======================= /api/standings =======================
test('standings WS5: final regular-season standings for a postseason game, a day in the cache', async () => {
  const res = await get('/api/standings/849799');
  assert.equal(res.status, 200);
  checkHeaders(res);
  assert.equal(res.headers.get('cache-control'), 'public, max-age=86400');
  assert.deepEqual(await res.json(), {
    season: 2026, asOf: 'final',
    away: { wins: 103, losses: 59, division: 'NL Central', place: 1, tied: false, gamesBack: 0, lead: 14, divisionRecord: { w: 35, l: 17 } },
    home: { wins: 98, losses: 64, division: 'AL East', place: 1, tied: false, gamesBack: 0, lead: 4.5, divisionRecord: { w: 28, l: 24 } },
  });
  const st = calls.find((c) => /standings\?/.test(c.url)).url;
  assert.ok(!/date=/.test(st), 'no date for a postseason game: ' + st);
});

test('standings: a regular-season game uses the standings going into its date (the day before), an hour', async () => {
  const g = { gamePk: 745020, season: '2026', gameType: 'R', officialDate: '2026-04-01', gameNumber: 1, status: { abstractGameState: 'Preview' },
    teams: { away: { team: { id: 139 } }, home: { team: { id: 158 } } } };
  route(/hydrate=probablePitcher&gamePk=745020$/, () => json({ dates: [{ games: [g] }] }));
  route(/standings\?.*&date=2026-03-31$/, () => json(STANDINGS_2026));
  const res = await get('/api/standings/745020');
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('cache-control'), 'public, max-age=3600');
  const body = await res.json();
  assert.equal(body.asOf, '2026-03-31');
  assert.equal(body.away.division, 'AL East');
  assert.ok(calls.some((c) => /standings\?leagueId=103,104&season=2026&standingsTypes=regularSeason&hydrate=division&date=2026-03-31$/.test(c.url)));
  assert.equal(dayBefore('2026-03-01'), '2026-02-28');
  assert.equal(dayBefore('2024-03-01'), '2024-02-29');
  assert.equal(dayBefore('2027-01-01'), '2026-12-31');
});

test('standings: places come from games back, ties are ties (divisionRank never ties), lead only for a sole leader', () => {
  const T = (id, w, l, gb, rank) => ({ team: { id }, wins: w, losses: l, divisionGamesBack: gb, divisionRank: rank, records: { divisionRecords: [{ division: { id: 205 }, wins: 1, losses: 1 }] } });
  const data = { records: [{ division: { id: 205, nameShort: 'NL Central' }, teamRecords: [
    T(1, 5, 1, '-', '1'), T(2, 4, 2, '1.0', '2'), T(3, 3, 3, '2.0', '3'), T(4, 3, 3, '2.0', '4'), T(5, 3, 3, '2.0', '5')] }] };
  assert.deepEqual([1, 2, 3, 4, 5].map((id) => { const s = teamStanding(data, id); return [s.place, s.tied, s.gamesBack, s.lead]; }),
    [[1, false, 0, 1], [2, false, 1, null], [3, true, 2, null], [3, true, 2, null], [3, true, 2, null]]);
  const tiedTop = { records: [{ division: { id: 203, nameShort: 'NL West' }, teamRecords: [T(7, 6, 2, '-', '1'), T(8, 6, 2, '-', '2'), T(9, 4, 4, '2.0', '3')] }] };
  assert.deepEqual([7, 8].map((id) => { const s = teamStanding(tiedTop, id); return [s.place, s.tied, s.lead]; }), [[1, true, null], [1, true, null]]);
  assert.equal(teamStanding(tiedTop, 999), null, 'a team not in the standings');
  assert.deepEqual(teamStanding(data, 1).divisionRecord, { w: 1, l: 1 });
});

// ======================= /api/battery =======================
test('battery WS5: records together (gamePk and same team only) and the running game, a day once final', async () => {
  const res = await get('/api/battery/849799');
  assert.equal(res.status, 200);
  checkHeaders(res);
  assert.equal(res.headers.get('cache-control'), 'public, max-age=86400');
  assert.deepEqual(await res.json(), {
    season: 2026,
    away: { pitcher: { id: 694819, name: 'Jacob Misiorowski', sb: 2, cs: 1, pickoffs: 0, teams: 1 }, catcher: { id: 661388, name: 'William Contreras', sb: 48, cs: 25, teams: 1 }, together: { w: 20, l: 7 } },
    home: { pitcher: { id: 656876, name: 'Drew Rasmussen', sb: 23, cs: 6, pickoffs: 0, teams: 1 }, catcher: { id: 689414, name: 'Liam Hicks', sb: 58, cs: 11, teams: 2 }, together: { w: 8, l: 1 } },
  });
  assert.ok(calls.some((c) => /people\?personIds=694819,661388,656876,689414&hydrate=stats\(group=\[pitching,fielding\],type=\[season\],season=2026\)$/.test(c.url)), 'one call for all four players');
});

test('battery WS5 (real logs): the Rays doubleheader, Hicks for the Marlins, and the substitute game do not count', () => {
  const game = WS5_PROBABLE.dates[0].games[0];
  const pk = (log) => new Set(log.stats[0].splits.map((s) => s.game.gamePk));
  // 22 Sep doubleheader: Hicks caught game 1 (823543), Rasmussen started game 2 (823494)
  assert.ok(pk(LOGS[689414]).has(823543) && !pk(LOGS[656876]).has(823543));
  assert.ok(pk(LOGS[656876]).has(823494) && !pk(LOGS[689414]).has(823494));
  // 17 May: both started 822982, Hicks for the Marlins
  const r = LOGS[656876].stats[0].splits.find((s) => s.game.gamePk === 822982);
  const h = LOGS[689414].stats[0].splits.find((s) => s.game.gamePk === 822982);
  assert.ok(r.stat.gamesStarted === 1 && h.stat.gamesStarted === 1 && h.position.abbreviation === 'C' && r.team.id !== h.team.id);
  assert.deepEqual(together(LOGS[656876], LOGS[689414], game), { w: 8, l: 1 });
  assert.deepEqual(together(LOGS[694819], LOGS[661388], game), { w: 20, l: 7 });
});

// Synthetic logs for the matching rules.
const S = (pk, date, num, team, extra) => ({ date, gameType: 'R', isWin: true, team: { id: team }, game: { gamePk: pk, gameNumber: num }, stat: { gamesStarted: 1 }, ...extra });
const plog = (...s) => ({ stats: [{ splits: s }] });
const GAME = { gamePk: 900, officialDate: '2026-06-10', gameNumber: 2 };

test('battery together: a doubleheader where the pitcher starts one game and the catcher the other does not count', () => {
  const pitcher = plog(S(501, '2026-06-01', 2, 10));
  const catcher = plog(S(500, '2026-06-01', 1, 10, { position: { abbreviation: 'C' } }));
  assert.deepEqual(together(pitcher, catcher, GAME), { w: 0, l: 0 });
});

test('battery together: a DH start, a substitute at C and opposite teams do not count; the team result is isWin', () => {
  const pitcher = plog(S(1, '2026-05-01', 1, 10), S(2, '2026-05-02', 1, 10), S(3, '2026-05-03', 1, 10), S(4, '2026-05-04', 1, 10, { isWin: false }), S(5, '2026-05-05', 1, 10, { stat: { gamesStarted: 0 } }));
  const catcher = plog(
    S(1, '2026-05-01', 1, 10, { position: { abbreviation: 'DH' } }),
    S(2, '2026-05-02', 1, 10, { position: { abbreviation: 'C' }, stat: { gamesStarted: 0 } }),
    S(3, '2026-05-03', 1, 11, { position: { abbreviation: 'C' } }),
    S(4, '2026-05-04', 1, 10, { position: { abbreviation: 'C' } }),
    S(5, '2026-05-05', 1, 10, { position: { abbreviation: 'C' } }));
  assert.deepEqual(together(pitcher, catcher, GAME), { w: 0, l: 1 }, 'only game 4 counts (a loss); game 5 the pitcher came on in relief');
});

test('battery together: only games before this one (same date: a lower game number); this game never counts', () => {
  const pitcher = plog(S(10, '2026-06-10', 1, 10), S(900, '2026-06-10', 2, 10), S(11, '2026-06-11', 1, 10), S(12, '2026-06-09', 1, 10, { gameType: 'D' }));
  const catcher = plog(...[10, 900, 11].map((pk, i) => S(pk, ['2026-06-10', '2026-06-10', '2026-06-11'][i], [1, 2, 1][i], 10, { position: { abbreviation: 'C' } })), S(12, '2026-06-09', 1, 10, { position: { abbreviation: 'C' } }));
  assert.deepEqual(together(pitcher, catcher, GAME), { w: 2, l: 0 }, 'game 1 of today and yesterday (any game type) count; this game and tomorrow do not');
});

test('battery running game: season total across teams, catcher from his C split only, zero attempts', () => {
  const hicks = PEOPLE_WS5.people.find((p) => p.id === 689414);
  assert.deepEqual(runningGame(hicks, 'catcher'), { id: 689414, name: 'Liam Hicks', sb: 58, cs: 11, teams: 2 });
  const nobody = { id: 5, fullName: 'New Guy', stats: [{ group: { displayName: 'pitching' }, splits: [{ team: { id: 1 }, stat: { stolenBases: 0, caughtStealing: 0, pickoffs: 0 } }] }] };
  assert.deepEqual(runningGame(nobody, 'pitcher'), { id: 5, name: 'New Guy', sb: 0, cs: 0, pickoffs: 0, teams: 1 });
  const dhOnly = { id: 6, fullName: 'DH Guy', stats: [{ group: { displayName: 'fielding' }, splits: [{ team: { id: 1 }, position: { abbreviation: 'DH' }, stat: {} }] }] };
  assert.deepEqual(runningGame(dhOnly, 'catcher'), { id: 6, name: 'DH Guy', sb: null, cs: null, teams: 1 });
});

test('battery: no catcher yet (lineup not posted) means no catcher and no record; the probable pitcher is used; an hour', async () => {
  const g = { gamePk: 745030, season: '2026', gameType: 'R', officialDate: '2026-06-10', gameNumber: 1, status: { abstractGameState: 'Preview' },
    teams: { away: { team: { id: 158 }, probablePitcher: { id: 694819, fullName: 'Jacob Misiorowski' } }, home: { team: { id: 139 } } } };
  route(/hydrate=probablePitcher&gamePk=745030$/, () => json({ dates: [{ games: [g] }] }));
  route(/game\/745030\/boxscore$/, () => json({ teams: { away: { pitchers: [], players: {} }, home: { pitchers: [], players: {} } } }));
  route(/people\?personIds=694819&hydrate/, () => json({ people: [PEOPLE_WS5.people.find((p) => p.id === 694819)] }));
  const res = await get('/api/battery/745030');
  assert.equal(res.status, 200);
  assert.equal(res.headers.get('cache-control'), 'public, max-age=3600');
  assert.deepEqual(await res.json(), {
    season: 2026,
    away: { pitcher: { id: 694819, name: 'Jacob Misiorowski', sb: 2, cs: 1, pickoffs: 0, teams: 1 }, catcher: null, together: null },
    home: { pitcher: null, catcher: null, together: null },
  });
  assert.ok(!calls.some((c) => /gameLog&group=fielding/.test(c.url)), 'no catcher log asked for');
});

// ======================= /api/slate =======================
const ENTRY_KEYS = ['away', 'detail', 'half', 'home', 'inning', 'outs', 'state'];

test('slate 2026-04-14 (real): every game, the trimmed shape, regular-season records, a day once all are final', async () => {
  const res = await get('/api/slate/2026-04-14');
  assert.equal(res.status, 200);
  checkHeaders(res);
  assert.equal(res.headers.get('cache-control'), 'public, max-age=86400');
  assert.equal(store.get('https://slate.cache/2026-04-14').headers.get('cache-control'), 'max-age=86400');
  const body = await res.json();
  assert.deepEqual(Object.keys(body).sort(), ['date', 'games']);
  assert.equal(body.date, '2026-04-14');
  const up = SLATES['2026-04-14'].dates[0].games;
  assert.equal(Object.keys(body.games).length, 15);
  for (const g of up) {
    const e = body.games[g.gamePk];
    assert.deepEqual(Object.keys(e).sort(), ENTRY_KEYS, String(g.gamePk));
    assert.deepEqual(Object.keys(e.away).sort(), ['losses', 'runs', 'wins']);
    assert.equal(e.state, 'final');
    assert.equal(e.detail, 'Final');
    for (const s of ['away', 'home']) {
      assert.deepEqual([e[s].runs, e[s].wins, e[s].losses], [g.linescore.teams[s].runs, g.teams[s].leagueRecord.wins, g.teams[s].leagueRecord.losses], g.gamePk + ' ' + s);
    }
  }
  assert.deepEqual(body.games[824853].away, { runs: body.games[824853].away.runs, wins: 10, losses: 8 });
  const raw = JSON.stringify(body);
  assert.ok(!raw.includes('copyright') && !raw.includes('Yankees') && !raw.includes('venue'), 'nothing raw passed through');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-04-14&hydrate=linescore');
  assert.ok(calls[0].opts.signal instanceof AbortSignal, 'the upstream request has a timeout signal');
});

test('slate 2026-05-23 (real): a rainout is "Final" upstream; it keeps its Postponed detail and no runs', async () => {
  const up = SLATES['2026-05-23'].dates[0].games.find((g) => g.gamePk === 823543);
  assert.deepEqual([up.status.abstractGameState, up.status.codedGameState, up.status.detailedState, up.status.statusCode, up.status.reason, up.rescheduleGameDate],
    ['Final', 'D', 'Postponed', 'DR', 'Rain', '2026-09-22'], 'the real response, as captured');
  const res = await get('/api/slate/2026-05-23');
  const body = await res.json();
  assert.deepEqual(body.games[823543], {
    state: 'final', detail: 'Postponed', inning: null, half: null, outs: null,
    away: { runs: null, wins: 95, losses: 61 }, home: { runs: null, wins: 90, losses: 66 },
  });
  assert.equal(body.games[824840].detail, 'Postponed');
  assert.equal(Object.keys(body.games).length, 16);
  assert.equal(res.headers.get('cache-control'), 'public, max-age=86400', 'finals and rainouts: every game is over');
});

test('slate 2026-10-28 (real World Series): no records in the postseason (leagueRecord is the series, 4-1)', async () => {
  assert.deepEqual(SLATES['2026-10-28'].dates[0].games[0].teams.away.leagueRecord.wins, 4);
  const body = await (await get('/api/slate/2026-10-28')).json();
  assert.deepEqual(body, {
    date: '2026-10-28',
    games: { 849799: { state: 'final', detail: 'Final', inning: 9, half: 'bottom', outs: 3, away: { runs: 4, wins: null, losses: null }, home: { runs: 1, wins: null, losses: null } } },
  });
});

// A synthetic schedule game for the slate rules.
const SG = (pk, abstract, detail, extra = {}) => ({ gamePk: pk, gameType: 'R', status: { abstractGameState: abstract, detailedState: detail },
  teams: { away: { leagueRecord: { wins: 1, losses: 2 } }, home: { leagueRecord: { wins: 2, losses: 1 } } }, linescore: {}, ...extra });
const day = (...games) => ({ dates: games.length ? [{ games }] : [] });

test('slate: cache lifetimes (live 15 s / 10 s; every game over a day; otherwise, and no games, 5 minutes)', async () => {
  const cases = [
    ['live', day(SG(1, 'Final', 'Final'), SG(2, 'Live', 'In Progress')), 15, 10],
    ['preview only', day(SG(1, 'Preview', 'Scheduled')), 300, 300],
    ['final and preview', day(SG(1, 'Final', 'Final'), SG(2, 'Preview', 'Pre-Game')), 300, 300],
    ['final and suspended', day(SG(1, 'Final', 'Final'), SG(2, 'Live', 'Suspended: Rain')), 86400, 86400],
    ['postponed and cancelled', day(SG(1, 'Final', 'Postponed'), SG(2, 'Final', 'Cancelled')), 86400, 86400],
    ['suspended and live', day(SG(1, 'Live', 'Suspended: Rain'), SG(2, 'Live', 'In Progress')), 15, 10],
    ['no games', day(), 300, 300],
  ];
  for (const [label, data, edge, browser] of cases) {
    store.clear();
    route(/date=2026-06-01&/, () => json(data));
    const res = await get('/api/slate/2026-06-01');
    assert.equal(res.status, 200, label);
    assert.equal(res.headers.get('cache-control'), 'public, max-age=' + browser, label);
    assert.equal(store.get('https://slate.cache/2026-06-01').headers.get('cache-control'), 'max-age=' + edge, label);
  }
  assert.deepEqual(await (await get('/api/slate/2026-06-01')).json(), { date: '2026-06-01', games: {} });
});

test('slate: a cache hit skips upstream and sets the browser max-age again', async () => {
  route(/date=2026-06-02&/, () => json(day(SG(1, 'Live', 'In Progress'))));
  await get('/api/slate/2026-06-02');
  assert.equal(calls.length, 1);
  const hit = await get('/api/slate/2026-06-02');
  assert.equal(calls.length, 1, 'served from the cache');
  checkHeaders(hit);
  assert.equal(hit.headers.get('cache-control'), 'public, max-age=10');
});

test('slate: the date must be a real YYYY-MM-DD date (400), and bad dates never reach upstream', async () => {
  for (const bad of ['2026-4-14', '20260414', '2026-02-30', '2026-13-01', '2026-00-10', 'abc', '2026-04-14x', '849799', '2026-04-1%20']) {
    const res = await get('/api/slate/' + bad);
    assert.equal(res.status, 400, bad);
    checkHeaders(res);
    assert.equal(res.headers.get('cache-control'), 'no-store');
    assert.deepEqual(await res.json(), { error: 'date must be YYYY-MM-DD' });
  }
  assert.equal(calls.length, 0);
  assert.ok(validDate('2024-02-29') && !validDate('2026-02-29'));
});

test('slate trim: live inning and runs, records only for gameType R, a gamePk listed twice keeps the entry that is not stopped', () => {
  const live = SG(5, 'Live', 'In Progress', { linescore: { currentInning: 6, inningState: 'Top', outs: 1, teams: { away: { runs: 2 }, home: { runs: 3 } } } });
  const susp = SG(5, 'Live', 'Suspended: Rain', { linescore: { currentInning: 6, inningState: 'Top', outs: 1, teams: { away: { runs: 2 }, home: { runs: 3 } } } });
  for (const order of [[susp, live], [live, susp]]) {
    assert.equal(trimSlate(day(...order), '2026-06-03').games[5].detail, 'In Progress');
  }
  assert.deepEqual(trimSlate(day(live), 'd').games[5], { state: 'live', detail: 'In Progress', inning: 6, half: 'top', outs: 1, away: { runs: 2, wins: 1, losses: 2 }, home: { runs: 3, wins: 2, losses: 1 } });
  for (const t of ['S', 'E', 'F', 'D', 'L', 'W', 'A']) {
    const e = trimSlate(day(SG(6, 'Preview', 'Scheduled', { gameType: t })), 'd').games[6];
    assert.deepEqual([e.away.wins, e.away.losses, e.home.wins], [null, null, null], t);
  }
  // before first pitch: no runs; a score without a line score falls back to teams.score
  assert.deepEqual(trimSlate(day(SG(7, 'Preview', 'Scheduled')), 'd').games[7].away.runs, null);
  assert.equal(trimSlate(day(SG(8, 'Final', 'Final', { linescore: undefined, teams: { away: { score: 4 }, home: { score: 1 } } })), 'd').games[8].away.runs, 4);
});

// ======================= /game =======================
test('/game/{1-8 digits}: the game page from the assets (/game), with the asset headers, the browser headers passed on', async () => {
  for (const id of ['849799', '1', '12345678']) {
    assetCalls = [];
    const res = await get('/game/' + id, 'GET', { 'If-None-Match': '"abc"' });
    assert.equal(res.status, 200, id);
    assert.equal(await res.text(), 'asset:/game');
    assert.equal(res.headers.get('content-security-policy'), "default-src 'none'; script-src 'self'", 'the _headers rules come with the asset');
    assert.equal(res.headers.get('x-frame-options'), 'DENY');
    assert.deepEqual(assetCalls, [{ path: '/game', method: 'GET', inm: '"abc"' }]);
  }
  assetCalls = [];
  const head = await get('/game/849799', 'HEAD');
  assert.equal(head.status, 200);
  assert.equal(await head.text(), '');
  assert.deepEqual(assetCalls.map((c) => [c.path, c.method]), [['/game', 'HEAD']]);
  assert.equal(calls.length, 0, 'no upstream call');
});

test('/game, /game/ and any other /game path: the 404 page with status 404 and its headers, never a 304', async () => {
  for (const p of ['/game', '/game/', '/game/abc', '/game/123456789', '/game/1/x', '/game/-1', '/game/1.5', '/game/849799/']) {
    assetCalls = [];
    const res = await get(p, 'GET', { 'If-None-Match': '"abc"' });
    assert.equal(res.status, 404, p);
    assert.equal(await res.text(), 'asset:/404', p);
    assert.equal(res.headers.get('content-security-policy'), "default-src 'none'; script-src 'self'", p);
    assert.deepEqual(assetCalls, [{ path: '/404', method: 'GET', inm: null }], p);
  }
});

test('/game: GET and HEAD only', async () => {
  for (const m of ['POST', 'PUT', 'DELETE', 'OPTIONS']) {
    assetCalls = [];
    const res = await get('/game/849799', m);
    assert.equal(res.status, 405, m);
    assert.equal(res.headers.get('allow'), 'GET, HEAD');
    assert.equal(assetCalls.length, 0);
  }
});
