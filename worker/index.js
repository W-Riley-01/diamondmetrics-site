// diamondmetrics-site Worker. Only /api/* reaches this script (assets.run_worker_first in wrangler.jsonc);
// every other path is a static file served from public/ without running any code.
//
// GET /api/linescore/{gamePk}  the game's state and line score
// GET /api/boxscore/{gamePk}   each team's starting pitcher (with his line) and starting catcher
// GET /api/series/{gamePk}     the regular-season series between the two teams, and the postseason series
// All from the MLB Stats API, trimmed to what the page shows. Nothing from upstream is passed through as it is.

const API = 'https://statsapi.mlb.com/api/v1/';
const TIMEOUT_MS = 5000;

// Seconds: how long the edge keeps a copy, and the max-age the browser is told, by game state.
export const EDGE_TTL = { final: 86400, live: 15, preview: 300 };
export const BROWSER_TTL = { final: 86400, live: 10, preview: 300 };
// The season series changes at most once a game: an hour, or a day once the two teams have no meetings left.
export const SERIES_TTL = { open: 3600, done: 86400 };

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

class Upstream extends Error {}

// One upstream call: JSON, or Upstream on an error status, bad JSON or no answer within 5 seconds.
async function getJSON(path) {
  let r;
  try {
    r = await fetch(API + path, { signal: AbortSignal.timeout(TIMEOUT_MS), headers: { Accept: 'application/json' } });
  } catch (e) {
    throw new Upstream('fetch');
  }
  if (!r.ok) throw new Upstream('status ' + r.status);
  try {
    return await r.json();
  } catch (e) {
    throw new Upstream('json');
  }
}

const int = (x) => (typeof x === 'number' && Number.isFinite(x) ? x : null);
const STATES = { Preview: 'preview', Live: 'live', Final: 'final' };
const stateOf = (game) => STATES[game && game.status && game.status.abstractGameState] || 'preview';
const firstGame = (data) => (data && Array.isArray(data.dates) && data.dates[0] && Array.isArray(data.dates[0].games) ? data.dates[0].games[0] || null : null);
const allGames = (data) => (data && Array.isArray(data.dates) ? data.dates.flatMap((d) => (d && Array.isArray(d.games) ? d.games : [])) : []);

// ---------- /api/linescore ----------
// The schedule response (hydrate=linescore) -> the trimmed shape, or null when the game is not in it.
export function trim(data) {
  const game = firstGame(data);
  if (!game) return null;
  const status = game.status || {};
  const ls = game.linescore || {};
  const half = (side) => (side && int(side.runs) !== null ? side.runs : null);
  const rhe = (t) => ({ r: int(t && t.runs), h: int(t && t.hits), e: int(t && t.errors) });
  const teams = ls.teams || {};
  return {
    state: stateOf(game),
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

async function linescore(pk) {
  const body = trim(await getJSON('schedule?sportId=1&hydrate=linescore&gamePk=' + pk));
  return body ? { body, edge: EDGE_TTL[body.state], browser: BROWSER_TTL[body.state] } : null;
}

// ---------- /api/boxscore ----------
// One team of the boxscore -> its starting pitcher (the first to pitch) with his line, and its starting catcher
// (a starter in the batting order, code n00, whose first position was C; substitutes have codes like 701).
export function trimTeam(team) {
  const players = (team && team.players) || {};
  const pid = team && Array.isArray(team.pitchers) && team.pitchers.length ? team.pitchers[0] : null;
  const pp = pid !== null ? players['ID' + pid] : null;
  let pitcher = null;
  if (pp && pp.person && typeof pp.person.fullName === 'string') {
    const st = (pp.stats && pp.stats.pitching) || {};
    pitcher = {
      id: int(pid),
      name: pp.person.fullName,
      line: { ip: typeof st.inningsPitched === 'string' ? st.inningsPitched : null, h: int(st.hits), bb: int(st.baseOnBalls), so: int(st.strikeOuts) },
    };
  }
  const c = Object.values(players).find((p) => {
    if (!p || !p.person || !/^[1-9]00$/.test(String(p.battingOrder || ''))) return false;
    const first = Array.isArray(p.allPositions) && p.allPositions[0] ? p.allPositions[0] : p.position;
    return !!first && first.abbreviation === 'C';
  });
  const catcher = c ? { id: int(c.person.id), name: c.person.fullName } : null;
  return { pitcher, catcher };
}

export function trimBox(box, state) {
  const teams = (box && box.teams) || {};
  return { state, away: trimTeam(teams.away), home: trimTeam(teams.home) };
}

async function boxscore(pk) {
  // The boxscore has no game status (and answers 200 for unknown games), so the schedule call says whether the game
  // exists and what state it is in.
  const [sched, box] = await Promise.all([getJSON('schedule?sportId=1&gamePk=' + pk), getJSON('game/' + pk + '/boxscore')]);
  const game = firstGame(sched);
  if (!game) return null;
  const body = trimBox(box, stateOf(game));
  return { body, edge: EDGE_TTL[body.state], browser: BROWSER_TTL[body.state] };
}

// ---------- /api/series ----------
const POSTSEASON = new Set(['F', 'D', 'L', 'W']);
const detailOf = (g) => (g && g.status && typeof g.status.detailedState === 'string' ? g.status.detailedState : '');

// The regular-season meetings and this game -> the season series as seen from this game.
// Played: final games (one winner) that started no later than this game, this game included once it is final.
// Left: every other meeting, each gamePk once (a rescheduled game appears under both dates), cancelled ones excluded.
export function trimSeries(game, regular) {
  const awayId = game.teams && game.teams.away && game.teams.away.team && game.teams.away.team.id;
  const homeId = game.teams && game.teams.home && game.teams.home.team && game.teams.home.team.id;
  const side = (id) => (id === awayId ? 'away' : id === homeId ? 'home' : null);
  const cutoff = Date.parse(game.gameDate);

  const byPk = new Map();
  for (const g of allGames(regular)) {
    if (!g || int(g.gamePk) === null || /^Cancelled/.test(detailOf(g))) continue;
    const prev = byPk.get(g.gamePk);
    // keep the entry that will be (or was) played, not the postponed date
    if (!prev || (/^Postponed/.test(detailOf(prev)) && !/^Postponed/.test(detailOf(g)))) byPk.set(g.gamePk, g);
  }
  const winnerOf = (g) => {
    const t = g.teams || {};
    if (t.away && t.away.isWinner === true) return side(t.away.team && t.away.team.id);
    if (t.home && t.home.isWinner === true) return side(t.home.team && t.home.team.id);
    return null;
  };
  const played = [...byPk.values()]
    .filter((g) => stateOf(g) === 'final' && !/^Postponed/.test(detailOf(g)) && winnerOf(g) && Date.parse(g.gameDate) <= cutoff)
    .sort((a, b) => Date.parse(a.gameDate) - Date.parse(b.gameDate));

  const wins = { away: 0, home: 0 };
  for (const g of played) wins[winnerOf(g)] += 1;

  // Series: a new one starts at seriesGameNumber 1 (or when the number doesn't go up). A sweep is a series of 2+ games
  // whose every game is in `played`, all won by the same team.
  const sweeps = [];
  let run = [];
  const close = () => {
    const n = run.length ? int(run[0].gamesInSeries) : null;
    if (n !== null && n >= 2 && run.length === n && run.every((g) => winnerOf(g) === winnerOf(run[0]))) {
      sweeps.push({ winner: winnerOf(run[0]), games: n, startDate: run[0].officialDate, endDate: run[run.length - 1].officialDate });
    }
    run = [];
  };
  for (const g of played) {
    const num = int(g.seriesGameNumber);
    if (run.length && (num === 1 || num === null || num <= int(run[run.length - 1].seriesGameNumber))) close();
    run.push(g);
  }
  close();

  const seasonSweep = played.length && played.every((g) => winnerOf(g) === winnerOf(played[0])) ? winnerOf(played[0]) : null;

  let postseason = null;
  const ss = game.seriesStatus;
  if (POSTSEASON.has(game.gameType) && ss && typeof ss.description === 'string') {
    const leader = ss.isTied ? null : side(ss.winningTeam && ss.winningTeam.id);
    postseason = { description: ss.description, result: { leader, wins: int(ss.wins), losses: int(ss.losses), over: ss.isOver === true } };
  }

  return {
    season: int(Number(game.season)),
    regular: { awayWins: wins.away, homeWins: wins.home, played: played.length, left: byPk.size - played.length, sweeps, seasonSweep },
    postseason,
  };
}

async function series(pk) {
  const game = firstGame(await getJSON('schedule?sportId=1&hydrate=seriesStatus&gamePk=' + pk));
  if (!game) return null;
  const awayId = game.teams && game.teams.away && game.teams.away.team && game.teams.away.team.id;
  const homeId = game.teams && game.teams.home && game.teams.home.team && game.teams.home.team.id;
  if (int(awayId) === null || int(homeId) === null || !/^\d{4}$/.test(String(game.season))) throw new Upstream('game');
  const regular = await getJSON('schedule?sportId=1&season=' + game.season + '&gameType=R&teamId=' + awayId + '&opponentId=' + homeId);
  const body = trimSeries(game, regular);
  const ttl = body.regular.left === 0 ? SERIES_TTL.done : SERIES_TTL.open;
  return { body, edge: ttl, browser: ttl };
}

const ROUTES = { linescore, boxscore, series };

// Cached: the stored copy keeps the edge lifetime in Cache-Control and the browser's max-age in its own header,
// so a cache hit tells the browser the right max-age again.
async function cached(name, pk, method, ctx) {
  const cache = caches.default;
  const key = new Request('https://' + name + '.cache/' + pk);
  const hit = await cache.match(key);
  if (hit) {
    const age = Number(hit.headers.get('X-Browser-Max-Age')) || 10;
    return reply(await hit.json(), 200, 'public, max-age=' + age, method);
  }
  let got;
  try {
    got = await ROUTES[name](pk);
  } catch (e) {
    if (e instanceof Upstream) return error(502, 'upstream error', method);
    throw e;
  }
  if (!got) return error(404, 'unknown game', method);
  const stored = new Response(JSON.stringify(got.body), {
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'max-age=' + got.edge, 'X-Browser-Max-Age': String(got.browser) },
  });
  const put = cache.put(key, stored);
  if (ctx && typeof ctx.waitUntil === 'function') ctx.waitUntil(put); else await put;
  return reply(got.body, 200, 'public, max-age=' + got.browser, method);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith('/api/')) return env.ASSETS.fetch(request);
    const method = request.method;
    if (method !== 'GET' && method !== 'HEAD') return error(405, 'method not allowed', method, { Allow: 'GET, HEAD' });
    const m = /^\/api\/(linescore|boxscore|series)\/([^/]+)$/.exec(url.pathname);
    if (!m) return error(404, 'not found', method);
    if (!/^\d{1,8}$/.test(m[2])) return error(400, 'gamePk must be 1 to 8 digits', method);
    return cached(m[1], m[2], method, ctx);
  },
};
