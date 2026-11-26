/* Diamond Metrics: MLB predictions.
   One script for every page; <body data-page> says which one to draw: "home" (/), "game" (/game/{gamePk}) or "bts"
   (/beat-the-streak). Reads /data/page.json (and optionally /data/site.json) and builds the page with DOM calls.
   Live data comes from the same-origin Worker: /api/slate for the home page's cards and the game page's other games,
   and /api/linescore, boxscore, series, standings and battery for the game on a game page. page.json stays the source
   for every prediction. Text is inserted as text nodes only, there are no inline styles, and no third-party requests. */
(() => {
  'use strict';

  // Strong / watch cut-offs, the same ones the original app used. A cell is judged by the
  // whole percent it shows, so a cell that reads "70%" is never left unhighlighted at a 70% cut-off.
  const THRESH = {
    hit: [0.70, 0.60], tb: [0.45, 0.35], hr: [0.18, 0.12], rbi: [0.35, 0.25], sb: [0.15, 0.08],
    ml: [0.58, 0.50], f5: [0.55, 0.48], nrfi: [0.58, 0.50],
    k3: [0.70, 0.55], k4: [0.60, 0.45], k5: [0.50, 0.35], k6: [0.35, 0.22], k7: [0.22, 0.12],
  };

  const TEAM_CODES = {
    'Arizona Diamondbacks': 'ARI', 'Atlanta Braves': 'ATL', 'Baltimore Orioles': 'BAL',
    'Boston Red Sox': 'BOS', 'Chicago Cubs': 'CHC', 'Chicago White Sox': 'CWS',
    'Cincinnati Reds': 'CIN', 'Cleveland Guardians': 'CLE', 'Colorado Rockies': 'COL',
    'Detroit Tigers': 'DET', 'Houston Astros': 'HOU', 'Kansas City Royals': 'KC',
    'Los Angeles Angels': 'LAA', 'Los Angeles Dodgers': 'LAD', 'Miami Marlins': 'MIA',
    'Milwaukee Brewers': 'MIL', 'Minnesota Twins': 'MIN', 'New York Mets': 'NYM',
    'New York Yankees': 'NYY', 'Oakland Athletics': 'OAK', 'Athletics': 'ATH',
    'Philadelphia Phillies': 'PHI', 'Pittsburgh Pirates': 'PIT', 'San Diego Padres': 'SD',
    'San Francisco Giants': 'SF', 'Seattle Mariners': 'SEA', 'St. Louis Cardinals': 'STL',
    'Tampa Bay Rays': 'TB', 'Texas Rangers': 'TEX', 'Toronto Blue Jays': 'TOR',
    'Washington Nationals': 'WSH',
  };
  const NICK = {
    'Arizona Diamondbacks': 'Diamondbacks', 'Atlanta Braves': 'Braves', 'Baltimore Orioles': 'Orioles',
    'Boston Red Sox': 'Red Sox', 'Chicago Cubs': 'Cubs', 'Chicago White Sox': 'White Sox',
    'Cincinnati Reds': 'Reds', 'Cleveland Guardians': 'Guardians', 'Colorado Rockies': 'Rockies',
    'Detroit Tigers': 'Tigers', 'Houston Astros': 'Astros', 'Kansas City Royals': 'Royals',
    'Los Angeles Angels': 'Angels', 'Los Angeles Dodgers': 'Dodgers', 'Miami Marlins': 'Marlins',
    'Milwaukee Brewers': 'Brewers', 'Minnesota Twins': 'Twins', 'New York Mets': 'Mets',
    'New York Yankees': 'Yankees', 'Oakland Athletics': 'Athletics', 'Athletics': 'Athletics',
    'Philadelphia Phillies': 'Phillies', 'Pittsburgh Pirates': 'Pirates', 'San Diego Padres': 'Padres',
    'San Francisco Giants': 'Giants', 'Seattle Mariners': 'Mariners', 'St. Louis Cardinals': 'Cardinals',
    'Tampa Bay Rays': 'Rays', 'Texas Rangers': 'Rangers', 'Toronto Blue Jays': 'Blue Jays',
    'Washington Nationals': 'Nationals',
  };
  // Park elevation in metres by home team code, copied from STADIUM_INFO (elevation_m) in the engine's weather.py.
  // ATH is not in that table yet (it still maps the A's to the Coliseum as OAK), so an A's home game shows no elevation.
  const ELEV_M = {
    ARI: 331, ATL: 305, BAL: 13, BOS: 6, CHC: 182, CWS: 181, CIN: 148, CLE: 200, COL: 1581, DET: 183,
    HOU: 14, KC: 268, LAA: 48, LAD: 142, MIA: 4, MIL: 181, MIN: 255, NYM: 4, NYY: 9, OAK: 6,
    PHI: 9, PIT: 223, SD: 10, SF: 5, SEA: 6, STL: 136, TB: 13, TEX: 171, TOR: 78, WSH: 5,
  };
  const code = (name) => TEAM_CODES[name] || String(name || '?').split(' ').pop().slice(0, 3).toUpperCase();
  const nick = (name) => NICK[name] || String(name || '?').split(' ').pop();
  const matchup = (g) => nick(g.away_team) + ' at ' + nick(g.home_team);

  // Live data refresh: every 30 s while live; every 5 min for a game that starts within 15 min.
  const LIVE_MS = 30000;
  const PRE_MS = 300000;
  const PRE_WINDOW_MS = 15 * 60000;
  const FETCH_TIMEOUT_MS = 10000;
  // A rainout is abstractGameState "Final", so a stopped game is told by its detailed state, before anything else.
  const STOPPED = /^(Postponed|Cancelled|Suspended)/;

  // Names compared without case or accents (Díaz = Diaz).
  const sameName = (a, b) => String(a || '').normalize('NFD').replace(/[̀-ͯ]/g, '').trim().toLowerCase()
    === String(b || '').normalize('NFD').replace(/[̀-ͯ]/g, '').trim().toLowerCase();

  const SVGNS = 'http://www.w3.org/2000/svg';

  // ---------- small DOM helpers ----------
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'style') throw new Error('inline styles are blocked by the page policy');
        if (k === 'class') el.className = v;
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? '' : String(v));
      }
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(typeof kid === 'object' && kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  // A small icon on a 16-unit grid, drawn in currentColor (the slot sets the colour). label null: decorative.
  function svgIcon(shapes, size, label, extraClass) {
    const s = document.createElementNS(SVGNS, 'svg');
    for (const [k, v] of [['viewBox', '0 0 16 16'], ['width', size], ['height', size], ['focusable', 'false'], ['class', 'ico' + (extraClass ? ' ' + extraClass : '')]]) s.setAttribute(k, String(v));
    if (label) { s.setAttribute('role', 'img'); s.setAttribute('aria-label', label); } else s.setAttribute('aria-hidden', 'true');
    for (const [tag, attrs] of shapes) {
      const el = document.createElementNS(SVGNS, tag);
      const all = { fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', ...attrs };
      for (const [k, v] of Object.entries(all)) el.setAttribute(k, v);
      s.append(el);
    }
    return s;
  }

  const CHECK = [['path', { d: 'M3 8.5l3.2 3.2L13 4.5', 'stroke-width': '2.4' }]];
  // A filled bat laid on the diagonal: knob, thin handle, tapering out to a rounded barrel.
  const BAT = [['path', {
    d: 'M-5.7-.75H-1C2-.75 3-2.5 5.5-2.5A2.5 2.5 0 0 1 5.5 2.5C3 2.5 2 .75-1 .75H-5.7A1.5 1.5 0 1 1-5.7-.75z',
    transform: 'translate(8 8) rotate(-45)', fill: 'currentColor', stroke: 'none',
  }]];
  // A firework burst: a small centre dot inside an empty ring, and ten rays radiating out from the ring, alternating
  // long and short. (Rays meeting in the middle read as an asterisk; six even arms read as a snowflake.)
  const BURST = [
    ['circle', { cx: '8', cy: '8', r: '1.2', fill: 'currentColor', stroke: 'none' }],
    ['path', { d: 'M8 4.6V1M11.23 6.95L14.66 5.84M10 10.75L12.11 13.66M6 10.75L3.89 13.66M4.77 6.95L1.34 5.84', 'stroke-width': '1.4' }],
    ['path', { d: 'M10.29 4.84L11.29 3.47M11.71 9.21L13.33 9.73M8 11.9V13.6M4.29 9.21L2.67 9.73M5.71 4.84L4.71 3.47', 'stroke-width': '1.4' }],
  ];
  // Lineup result icons, one per batter prop.
  const ICONS = {
    hit: { label: 'got a hit', shapes: BAT },
    tb: { label: 'two or more bases', shapes: BAT },
    hr: { label: 'homered', shapes: BURST },
    rbi: { label: 'drove in a run', shapes: [['path', { d: 'M3 2.8h10v6L8 13.4 3 8.8z' }]] },
    sb: { label: 'stole a base', shapes: [['path', { d: 'M8 2.4L13.6 8 8 13.6 2.4 8z' }]] },
  };

  // A check mark with words for screen readers, in a fixed-width slot so numbers stay aligned.
  const mark = (yes, words) => h('span', { class: 'ck' }, yes ? [svgIcon(CHECK, 16, null), h('span', { class: 'sr' }, ' ' + words)] : null);
  // A lineup result icon in the same slot; the icon itself carries the words. The homer burst gets .pop: the lineups
  // are drawn once per page and never redrawn by the live refresh, so the pop plays once.
  const propMark = (yes, kind) => h('span', { class: 'ck' }, yes ? svgIcon(ICONS[kind].shapes, 14, ICONS[kind].label, kind === 'hr' ? 'pop' : null) : null);
  // A K under each strikeout total the starter reached, in the same slot; screen readers get the full sentence.
  const kMark = (yes, n) => h('span', { class: 'ck' }, yes ? [h('span', { class: 'kmark', 'aria-hidden': 'true' }, 'K'), h('span', { class: 'sr' }, 'Reached ' + n + ' strikeouts.')] : null);

  // A board lamp. Decorative: the text next to it carries the meaning. state: '' (unlit), 'on' (live), 'hit' or 'miss'.
  const lamp = (state) => h('span', { class: 'lamp' + (state ? ' ' + state : ''), 'aria-hidden': 'true' });

  // ---------- number and date formatting ----------
  const ok = (x) => typeof x === 'number' && Number.isFinite(x);
  // Whole percent, rounded half up on the decimal value (toPrecision drops binary float noise such as 28.499999999999996).
  const whole = (p) => Math.round(Number((p * 100).toPrecision(12)));
  const wholePct = (r) => (r <= 0 ? '<1%' : r >= 100 ? '>99%' : r + '%');
  const pct = (p) => (ok(p) ? wholePct(whole(p)) : '–');
  const dist = (p) => Number(Math.abs(p - 0.5).toPrecision(12)); // so 0.45 and 0.55 count as the same distance from 50%

  function tier(p, key) {
    const t = THRESH[key];
    if (!t || !ok(p)) return 'p-low';
    const r = whole(p);
    if (r >= whole(t[0])) return 'p-strong';
    if (r >= whole(t[1])) return 'p-watch';
    return 'p-low';
  }
  const btier = (p, key) => tier(p, key).replace('p-', 'b-');

  const pcell = (p, key) => h('span', { class: 'p ' + tier(p, key) }, pct(p));
  const fix2 = (x) => (ok(x) ? x.toFixed(2) : '–');
  const dot3 = (x) => (ok(x) ? x.toFixed(3).replace(/^0\./, '.') : '–');
  const runs = (x) => (ok(x) ? x.toFixed(1).replace(/\.0$/, '') + (x === 1 ? ' run' : ' runs') : '–');
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

  function ymd(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || '');
    return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
  }

  // "Wednesday, October 28, 2026" (withYear) or "Wednesday, October 28". Read in UTC, so the viewer's zone never shifts it.
  function longDate(s, withYear) {
    const p = ymd(s);
    if (!p) return '';
    const opts = { weekday: 'long', month: 'long', day: 'numeric', timeZone: 'UTC' };
    if (withYear) opts.year = 'numeric';
    return new Date(Date.UTC(p[0], p[1] - 1, p[2])).toLocaleDateString('en-US', opts);
  }

  // "October 28"
  function monthDay(s) {
    const p = ymd(s);
    return p ? new Date(Date.UTC(p[0], p[1] - 1, p[2])).toLocaleDateString('en-US', { month: 'long', day: 'numeric', timeZone: 'UTC' }) : '';
  }

  // "July 7–9", "June 30–July 2", or "July 7" for one day.
  function dateRange(a, b) {
    const p = ymd(a);
    const q = ymd(b) || p;
    if (!p) return '';
    const month = (x) => new Date(Date.UTC(x[0], x[1] - 1, 1)).toLocaleDateString('en-US', { month: 'long', timeZone: 'UTC' });
    if (p[1] === q[1] && p[2] === q[2] && p[0] === q[0]) return month(p) + ' ' + p[2];
    return month(p) + ' ' + p[2] + '–' + (p[1] === q[1] ? '' : month(q) + ' ') + q[2];
  }

  function daysUntil(s) {
    const p = ymd(s);
    if (!p) return null;
    const n = new Date();
    const today = Date.UTC(n.getFullYear(), n.getMonth(), n.getDate());
    return Math.round((Date.UTC(p[0], p[1] - 1, p[2]) - today) / 86400000);
  }

  function clock(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
  }

  // "Updated 10:00 AM CDT." when the update was on the slate's date (in the viewer's zone), else with its own date:
  // "Updated Nov 18, 12:13 PM EST." (the replay ran weeks after its game).
  function updatedText(page) {
    const d = new Date(page.updated_at);
    if (!page.updated_at || Number.isNaN(d.getTime())) return '';
    const p = ymd(page.date);
    const sameDay = p && d.getFullYear() === p[0] && d.getMonth() + 1 === p[1] && d.getDate() === p[2];
    return 'Updated ' + (sameDay ? clock(page.updated_at)
      : d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' })) + '.';
  }

  const ordinal = (n) => {
    const v = n % 100;
    const s = ['th', 'st', 'nd', 'rd'];
    return n + (s[(v - 20) % 10] || s[v] || s[0]);
  };

  // ---------- data ----------
  async function getJSON(path) {
    const r = await fetch(path, { cache: 'no-cache' });
    if (!r.ok) throw new Error(path + ' returned ' + r.status);
    return r.json();
  }

  // One Worker answer, or null on any failure (the page then leaves that part as it is, without an error).
  async function fetchApi(path, valid) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS);
    try {
      const r = await fetch(path, { signal: ctl.signal });
      if (!r.ok) return null;
      const body = await r.json();
      return body && valid(body) ? body : null;
    } catch (e) {
      return null;
    } finally {
      clearTimeout(t);
    }
  }
  const getApi = (name, gamePk, valid) => (/^\d{1,8}$/.test(String(gamePk)) ? fetchApi('/api/' + name + '/' + gamePk, valid) : Promise.resolve(null));
  const getLineScore = (pk) => getApi('linescore', pk, (b) => typeof b.state === 'string' && Array.isArray(b.innings));
  const getBoxScore = (pk) => getApi('boxscore', pk, (b) => typeof b.state === 'string' && b.away && b.home);
  const getSeries = (pk) => getApi('series', pk, (b) => b.regular && typeof b.regular === 'object');
  const getStandings = (pk) => getApi('standings', pk, (b) => 'away' in b && 'home' in b);
  const getBattery = (pk) => getApi('battery', pk, (b) => b.away && b.home && typeof b.away === 'object' && typeof b.home === 'object');
  const getSlate = (date) => (ymd(date) ? fetchApi('/api/slate/' + date, (b) => b.games && typeof b.games === 'object') : Promise.resolve(null));

  // ---------- Opening Day countdown ----------
  function renderBanner(site) {
    const el = document.getElementById('banner');
    if (!el || !site || !site.opening_day) return;
    const n = daysUntil(site.opening_day);
    if (n === null || n < 0) return;
    const year = ymd(site.opening_day)[0];
    const label = site.opening_label || longDate(site.opening_day, true);
    const kids = n === 0
      ? [h('p', { class: 'cd-t' }, h('b', null, 'Opening Day ' + year + ' is today'), ', ' + label + '.')]
      : [h('span', { class: 'digits', role: 'img', 'aria-label': n + (n === 1 ? ' day' : ' days') },
        String(n).split('').map((d) => h('span', { class: 'dg' }, d))),
      h('p', { class: 'cd-t' }, h('b', null, (n === 1 ? 'day' : 'days') + ' to Opening Day ' + year), ', ' + label + '.')];
    if (site.opening_note) kids.push(h('p', { class: 'cd-note' }, site.opening_note));
    el.replaceChildren(...kids);
    el.setAttribute('role', 'region');
    el.setAttribute('aria-label', 'Opening Day countdown');
    el.hidden = false;
  }

  // ---------- game helpers ----------
  // "bottom 5th, 2 outs" from page.json (the fallback when the API can't be fetched); '' when the inning isn't known.
  function pageWhere(g) {
    const half = g.inning_half ? String(g.inning_half).toLowerCase() : '';
    const where = g.inning ? [half, ordinal(g.inning)].filter(Boolean).join(' ') : '';
    const outs = where && typeof g.outs === 'number' ? ', ' + g.outs + (g.outs === 1 ? ' out' : ' outs') : '';
    return where + outs;
  }

  // The same from the API (line score or slate): half is top, bottom, middle or end; outs only during a half.
  function apiWhere(ls) {
    const where = ok(ls.inning) ? [ls.half, ordinal(ls.inning)].filter(Boolean).join(' ') : '';
    const during = ls.half === 'top' || ls.half === 'bottom';
    const outs = where && during && ok(ls.outs) ? ', ' + ls.outs + (ls.outs === 1 ? ' out' : ' outs') : '';
    return where + outs;
  }
  const liveText = (where) => 'Live' + (where ? ', ' + where : '');

  const stopped = (ls) => !!(ls && STOPPED.test(ls.detail || ''));

  // A game as it stands: from its slate entry when there is one (the detailed state first), else from page.json.
  // kind: preview, live, final or stopped; where: the inning while live; away/home: runs or null; rec: "10–8" or null.
  function gameNow(g, s) {
    if (s) {
      const kind = stopped(s) ? 'stopped' : s.state === 'live' || s.state === 'final' ? s.state : 'preview';
      const side = (k) => s[k] || {};
      const rec = (k) => (ok(side(k).wins) && ok(side(k).losses) ? side(k).wins + '–' + side(k).losses : null);
      return {
        kind, detail: s.detail || '', where: kind === 'live' ? apiWhere(s) : '',
        away: ok(side('away').runs) ? side('away').runs : null, home: ok(side('home').runs) ? side('home').runs : null,
        rec: { away: rec('away'), home: rec('home') },
      };
    }
    const kind = g.status === 'live' || g.status === 'final' ? g.status : 'preview';
    return {
      kind, detail: '', where: kind === 'live' ? pageWhere(g) : '',
      away: ok(g.away_score) ? g.away_score : null, home: ok(g.home_score) ? g.home_score : null,
      rec: { away: null, home: null },
    };
  }

  const byStart = (a, b) => (Date.parse(a.start_time_utc) || 0) - (Date.parse(b.start_time_utc) || 0);

  const finalResults = (g) => (g.status === 'final' && g.results ? g.results : null);

  function finalTotal(g) {
    if (g.status !== 'final') return null;
    const res = g.results;
    if (res && ok(res.total_runs)) return res.total_runs;
    return ok(g.away_score) && ok(g.home_score) ? g.away_score + g.home_score : null;
  }

  // The pick's box-score row in game g once it is final, or null.
  function resultIn(pick, g) {
    if (g.status !== 'final' || !g.results || !g.results.batters) return null;
    const side = g.away_team === pick.team ? 'away' : g.home_team === pick.team ? 'home' : null;
    if (!side) return null;
    const row = ((g.lineups && g.lineups[side]) || []).find((r) => r.name === pick.name);
    return row ? (g.results.batters[side] || []).find((r) => r.id === row.id) || null : null;
  }

  // The pick's game: by game_id when the file gives one, else his team's game (on a doubleheader without game_id, the
  // final game he has a box-score row in).
  function pickGame(pick, games) {
    if (pick.game_id !== undefined && pick.game_id !== null) return games.find((g) => g.game_id === pick.game_id) || null;
    const mine = games.filter((g) => g.away_team === pick.team || g.home_team === pick.team);
    return mine.find((g) => resultIn(pick, g)) || mine[0] || null;
  }

  // The over/under line closest to even: of the lines in `over` (key -> chance of going over), the one whose
  // unrounded chance is closest to 50%; on a tie, the higher line. lineOf turns a key into the line number.
  function closestLine(over, lineOf) {
    let best = null;
    for (const k of Object.keys(over || {})) {
      const p = over[k];
      const line = lineOf(k);
      if (line === null || !ok(p)) continue;
      const d = dist(p);
      if (!best || d < best.d || (d === best.d && line > best.line)) best = { line, d, p, over: whole(p) };
    }
    return best;
  }
  // Total runs: the .5 keys of markets.total_over.
  const ouLine = (over) => closestLine(over, (k) => (k.endsWith('.5') ? Number(k) : null));

  // Dew point in whole °F: the file's value, or Magnus from temperature and humidity (the engine's constants 6.1078, 7.5, 237.3).
  function dewPointF(w) {
    if (ok(w.dew_point_f)) return Math.round(w.dew_point_f);
    if (!ok(w.temp_f) || !ok(w.humidity_pct) || w.humidity_pct <= 0) return null;
    const t = (w.temp_f - 32) * 5 / 9;
    const g = Math.log10(w.humidity_pct / 100) + (7.5 * t) / (t + 237.3);
    return Math.round(((237.3 * g) / (7.5 - g)) * 9 / 5 + 32);
  }

  // Elevation in feet: the file's value, or the park table in metres converted and rounded to the nearest 10 ft.
  function elevationFt(g, w) {
    if (ok(w.elevation_ft)) return Math.round(w.elevation_ft);
    const m = ELEV_M[code(g.home_team)];
    return ok(m) ? Math.round((m * 3.28084) / 10) * 10 : null;
  }

  // ---------- the board ----------
  // A numeral plate: the number in big digits, the % sign smaller. cls is the board tier (b-strong, b-watch, b-low, b-plain).
  function plate(text, cls, caption) {
    const m = /^(.*?)(%?)$/.exec(String(text));
    return h('div', { class: 'pl' },
      h('span', { class: 'plate ' + cls }, h('span', { class: 'n' }, m[1]), m[2] ? h('span', { class: 'pc' }, '%') : null),
      caption ? h('div', { class: 'cap' }, caption) : null);
  }
  const mini = (p, key) => h('span', { class: 'mini ' + (key ? btier(p, key) : 'b-plain') }, pct(p));
  // Lamp-row value: bold numerals in the board tier colour, no plate (plates are for the board's main numbers).
  const num = (text, cls) => h('b', { class: 'num ' + cls }, text);

  // The board's top line: park, then first pitch, the date, or a stopped game's status; a lit lamp while live.
  function topLine(g, date, ls) {
    const where = g.park ? g.park + '.' : null;
    const day = longDate(date, true);
    const first = clock(g.start_time_utc);
    let when;
    let live = null;
    if (ls) {
      if (stopped(ls)) when = ls.detail + '.';
      else if (ls.state === 'preview') when = first ? 'First pitch ' + first + '.' : null;
      else when = day ? day + '.' : null;
      if (ls.state === 'live' && !stopped(ls)) live = liveText(apiWhere(ls));
    } else {
      when = g.status === 'final' || g.status === 'live' ? (day ? day + '.' : null) : (first ? 'First pitch ' + first + '.' : null);
      if (g.status === 'live') live = liveText(pageWhere(g));
    }
    return [h('p', { class: 'bd-where' }, [where, when].filter(Boolean).join(' ')),
      live ? h('p', { class: 'bd-live' }, lamp('on'), live) : null];
  }

  // The line score: a plate per inning (at least nine), then R, H and E. ls is null before it loads or when it can't be fetched.
  function lineScoreTable(g, ls) {
    const inns = (ls && ls.innings) || [];
    const n = Math.max(9, inns.length, ls && ok(ls.inning) ? ls.inning : 0);
    const stop = stopped(ls);
    const fin = ls && ls.state === 'final' && !stop;
    const live = ls && ls.state === 'live' && !stop;
    const lastN = inns.length ? inns[inns.length - 1].n : null;
    // The half being played (or about to be): top -> away, bottom or middle -> home, end -> next inning's away.
    let cur = null;
    if (live && ok(ls.inning)) {
      if (ls.half === 'top') cur = ['away', ls.inning];
      else if (ls.half === 'bottom' || ls.half === 'middle') cur = ['home', ls.inning];
      else if (ls.half === 'end') cur = ['away', ls.inning + 1];
    }
    const total = (side, k) => (ls && ls[side] && ok(ls[side][k]) ? String(ls[side][k]) : '–');
    const cols = [];
    for (let i = 1; i <= n; i += 1) cols.push(i);

    function row(side) {
      return h('tr', null,
        h('th', { scope: 'row', class: 'ls-team' }, code(g[side + '_team'])),
        cols.map((i) => {
          const inn = inns.find((x) => x.n === i);
          const v = inn ? inn[side] : null;
          let text = ok(v) ? String(v) : '';
          if (fin && side === 'home' && i === lastN && lastN >= 9 && !ok(v)) text = 'X';
          const isCur = cur && cur[0] === side && cur[1] === i;
          return h('td', null, h('span', { class: 'ip' + (isCur ? ' cur' : '') }, text,
            isCur ? h('span', { class: 'sr' }, ' (now batting)') : null));
        }),
        ['r', 'h', 'e'].map((k, j) => h('td', { class: 'rhe' + (j === 0 ? ' first' : '') }, h('span', { class: 'ip big' }, total(side, k)))));
    }

    return h('table', { class: 'ls' },
      h('caption', { class: 'sr' }, 'Line score, ' + g.away_team + ' at ' + g.home_team),
      h('thead', null, h('tr', null,
        h('th', { scope: 'col' }, h('span', { class: 'sr' }, 'Team')),
        cols.map((i) => h('th', { scope: 'col' }, String(i))),
        h('th', { scope: 'col', class: 'rhe first' }, h('span', { 'aria-hidden': 'true' }, 'R'), h('span', { class: 'sr' }, 'Runs')),
        h('th', { scope: 'col', class: 'rhe' }, h('span', { 'aria-hidden': 'true' }, 'H'), h('span', { class: 'sr' }, 'Hits')),
        h('th', { scope: 'col', class: 'rhe' }, h('span', { 'aria-hidden': 'true' }, 'E'), h('span', { class: 'sr' }, 'Errors')))),
      h('tbody', null, row('away'), row('home')));
  }

  // "14 games", "4.5 games", "1 game".
  const gamesText = (n) => String(n) + (n === 1 ? ' game' : ' games');

  // The standing as two lines, so neither breaks mid-phrase: ["1st in the NL Central, 14 games up.", "35–17 in the
  // division."], or ["3rd in the AL East, 11 games back.", ...], ["Tied for 1st in the NL West.", ...],
  // ["Tied for 3rd in the AL East, 2 games back.", ...]. The second is null when the division record is missing.
  function standingLines(t) {
    if (!t || !ok(t.place) || typeof t.division !== 'string') return [null, null];
    let s = (t.tied ? 'Tied for ' : '') + ordinal(t.place) + ' in the ' + t.division;
    if (t.place === 1 && !t.tied && ok(t.lead) && t.lead > 0) s += ', ' + gamesText(t.lead) + ' up';
    else if (t.place > 1 && ok(t.gamesBack)) s += ', ' + gamesText(t.gamesBack) + ' back';
    const dr = t.divisionRecord;
    return [s + '.', dr && ok(dr.w) && ok(dr.l) ? dr.w + '–' + dr.l + ' in the division.' : null];
  }

  // A stolen-base row's numbers: pitcher "2 stolen, 1 caught, 0 pickoffs"; catcher "48 stolen, 25 caught, 34%" or
  // "no attempts"; "(2 teams)" when the season total covers more than one team.
  function runningText(p, kind) {
    if (!p || !ok(p.sb) || !ok(p.cs)) return 'no stats yet';
    let s;
    if (kind === 'pitcher') {
      s = p.sb + ' stolen, ' + p.cs + ' caught, ' + (ok(p.pickoffs) ? p.pickoffs : 0) + (p.pickoffs === 1 ? ' pickoff' : ' pickoffs');
    } else if (p.sb + p.cs === 0) {
      s = 'no attempts';
    } else {
      s = p.sb + ' stolen, ' + p.cs + ' caught, ' + Math.round(Number(((p.cs * 100) / (p.sb + p.cs)).toPrecision(12))) + '%';
    }
    return s + (ok(p.teams) && p.teams > 1 ? ' (' + p.teams + ' teams)' : '');
  }

  // The season series strip: each entry is [label or null, text].
  function seriesLines(g, s) {
    const out = [];
    const r = s.regular || {};
    const team = (side) => nick(g[side + '_team']);
    const other = (side) => (side === 'away' ? 'home' : 'away');
    const wins = { away: r.awayWins, home: r.homeWins };
    if (ok(r.played) && ok(wins.away) && ok(wins.home)) {
      let text;
      if (r.played === 0) text = 'first meeting';
      else if (wins.away === wins.home) text = 'tied ' + wins.away + '–' + wins.home;
      else {
        const lead = wins.away > wins.home ? 'away' : 'home';
        text = team(lead) + ' ' + wins[lead] + '–' + wins[other(lead)];
      }
      out.push(['Season series:', text]);
      for (const sw of Array.isArray(r.sweeps) ? r.sweeps : []) {
        if (sw && (sw.winner === 'away' || sw.winner === 'home') && ok(sw.games)) {
          out.push([null, team(sw.winner) + ' swept a ' + sw.games + '-game series, ' + dateRange(sw.startDate, sw.endDate) + '.']);
        }
      }
      if ((r.seasonSweep === 'away' || r.seasonSweep === 'home') && ok(r.left)) {
        const w = r.seasonSweep;
        out.push([null, r.left === 0
          ? team(w) + ' swept the season series ' + wins[w] + '–0.'
          : team(other(w)) + " haven't beaten the " + team(w) + ' this season (0–' + wins[w] + ', ' + r.left + (r.left === 1 ? ' game' : ' games') + ' left).']);
      }
    }
    const ps = s.postseason;
    if (ps && typeof ps.description === 'string' && ps.result && ok(ps.result.wins) && ok(ps.result.losses)) {
      const pr = ps.result;
      const score = pr.wins + '–' + pr.losses;
      const text = pr.leader === 'away' || pr.leader === 'home'
        ? team(pr.leader) + (pr.over ? ' win ' : ' lead ') + score + '.'
        : 'tied ' + score + '.';
      out.push([ps.description + ':', text]);
    }
    return out;
  }

  // The model's pick for each board market and whether it came true (page.json results only).
  // Each item: { cls, label (text and nodes), pick (null when even), hit (true, false, or null when unknown) }.
  function boardPicks(g) {
    const m = g.markets || {};
    const res = finalResults(g);
    const items = [];

    // Winner: the higher unrounded win chance; exactly even means no pick.
    const aw = m.away_win;
    const hw = m.home_win;
    if (ok(aw) && ok(hw)) {
      const pick = aw === hw || aw === 0.5 || hw === 0.5 ? null : aw > hw ? 'away' : 'home';
      let hit = null;
      if (pick && res && ok(res.away_score) && ok(res.home_score) && res.away_score !== res.home_score) {
        hit = (res.away_score > res.home_score ? 'away' : 'home') === pick;
      }
      items.push({ cls: 'lr-win', label: ['Winner ', pick ? nick(g[pick + '_team']) : 'even'], pick, hit });
    }

    // First 5: the highest of away, home and tie; a tie for the top means no pick.
    const f5 = { away: m.f5_away, home: m.f5_home, tie: m.f5_tie };
    if (Object.values(f5).every(ok)) {
      const top = Math.max(f5.away, f5.home, f5.tie);
      const leaders = Object.keys(f5).filter((k) => f5[k] === top);
      const pick = leaders.length === 1 ? leaders[0] : null;
      const led = res ? res.led_after_5 : null;
      const hit = pick && (led === 'away' || led === 'home' || led === 'tie') ? led === pick : null;
      items.push({ cls: 'lr-f5', label: ['First 5 ', pick === 'tie' ? 'Tie' : pick ? nick(g[pick + '_team']) : 'even'], pick, hit });
    }

    // Total runs: Over or Under on the board's line, whichever is likelier.
    const ou = ouLine(m.total_over);
    if (ou) {
      const pick = ou.p > 0.5 ? 'over' : ou.p < 0.5 ? 'under' : null;
      const hit = pick && res && ok(res.total_runs) ? (res.total_runs > ou.line) === (pick === 'over') : null;
      items.push({ cls: 'lr-tot', label: pick ? ['Total ', pick === 'over' ? 'Over ' : 'Under ', String(ou.line)] : ['Total ', String(ou.line), ' even'], pick, hit });
    }

    // NRFI: no run in the 1st when the chance is above 50%, otherwise YRFI with the other side of the number.
    const p = m.no_run_first;
    const nrPick = ok(p) ? (p > 0.5 ? 'nrfi' : p < 0.5 ? 'yrfi' : null) : null;
    const rif = res ? res.run_in_first : null;
    items.push({
      cls: 'lr-nr',
      label: nrPick === 'yrfi'
        ? ['YRFI ', num(wholePct(100 - whole(p)), 'b-plain')]
        : ['NRFI ', num(pct(p), btier(p, 'nrfi'))],
      pick: nrPick,
      hit: nrPick && typeof rif === 'boolean' ? (nrPick === 'nrfi' ? !rif : rif) : null,
    });
    return items;
  }

  // The result lamps, one per board market. mode 'pre': unlit lamps (before and during the game); 'final': green if the
  // pick came true and red if it missed; 'none': no lamps at all (a postponed, cancelled or suspended game).
  // No lamp when there is no pick, or the result isn't in page.json. short: show only the market's name (Winner,
  // First 5, Total, NRFI) and keep the pick as screen-reader text, so a card's lamps fit on one row.
  function lampList(g, mode, short) {
    return h('ul', { class: 'lr' }, boardPicks(g).map((it) => {
      let state = null;
      if (it.pick && mode === 'pre') state = '';
      else if (it.pick && mode === 'final' && it.hit !== null) state = it.hit ? 'hit' : 'miss';
      return h('li', { class: it.cls },
        state === null ? null : lamp(state),
        short ? [it.label[0].trim(), h('span', { class: 'sr' }, ' ', it.label.slice(1))] : it.label,
        state === 'hit' || state === 'miss' ? h('span', { class: 'sr' }, state === 'hit' ? " Model's pick came true." : " Model's pick missed.") : null);
    }));
  }

  function board(g, date) {
    const m = g.markets || {};
    const ou = ouLine(m.total_over);

    // Sportsbook layout: the away row shows the over, the home row the under, for the same line; the pair adds to 100.
    function ouCell(side) {
      if (!ou) return plate('–', 'b-plain');
      const over = side === 'away';
      return plate(wholePct(over ? ou.over : 100 - ou.over), 'b-plain', (over ? 'Over ' : 'Under ') + ou.line);
    }

    // Under each team name: its record and standing (from /api/standings), the battery (SP from page.json, C from the
    // boxscore) and the battery's record together (from /api/battery). The API parts are filled in later, or stay
    // hidden if they can't be fetched.
    const bline = (pos, name) => [h('span', { class: 'pos' }, pos), ' ', h('span', { class: 'who' }, name)];
    const slots = {};
    for (const side of ['away', 'home']) {
      slots[side] = {
        rec: h('span', { class: 'rec', hidden: true }),
        stand: h('span', { class: 'bl bl-st', hidden: true }),
        divrec: h('span', { class: 'bl bl-dr', hidden: true }),
        catcher: h('span', { class: 'bl bl-c' }, bline('C', 'not announced')),
        bat: h('span', { class: 'bl bl-bat', hidden: true }),
      };
    }

    function row(side) {
      const team = g[side + '_team'];
      const st = g.starters && g.starters[side];
      return h('tr', null,
        h('th', { scope: 'row', class: 'team' },
          h('span', { class: 'nameline' }, h('span', { class: 'nick' }, nick(team)), slots[side].rec),
          slots[side].stand,
          slots[side].divrec,
          h('span', { class: 'bl bl-sp' }, bline('SP', st && st.name ? st.name : 'not announced')),
          slots[side].catcher,
          slots[side].bat),
        h('td', { 'data-label': 'Odds to win' }, plate(pct(m[side + '_win']), btier(m[side + '_win'], 'ml'))),
        h('td', { 'data-label': 'First 5 winner' }, plate(pct(m['f5_' + side]), btier(m['f5_' + side], 'f5'))),
        h('td', { 'data-label': 'Total runs O/U' }, ouCell(side)));
    }

    const table = h('table', { class: 'bd' },
      h('caption', { class: 'sr' }, g.away_team + ' at ' + g.home_team + ': odds to win, first 5 winner, total runs over and under'),
      h('thead', null, h('tr', null,
        h('th', { scope: 'col' }, h('span', { class: 'sr' }, 'Team')),
        h('th', { scope: 'col' }, 'Odds to win'),
        h('th', { scope: 'col' }, 'First 5 winner'),
        h('th', { scope: 'col' }, 'Total runs O/U'))),
      h('tbody', null, row('away'), row('home')));

    const lampMode = g.status === 'final' ? 'final' : 'pre';
    const lamps = h('div', { class: 'lamprow' }, lampList(g, lampMode));

    const top = h('div', { class: 'bd-top' }, topLine(g, date, null));
    // The scroll box stays in place across refreshes, so keyboard focus on it is kept.
    const lsBox = h('div', { class: 'ls-scroll', tabindex: '0', role: 'region', 'aria-label': 'Line score' }, lineScoreTable(g, null));
    // The season series strip stays hidden until the series loads (and stays hidden if it can't).
    const ser = h('div', { class: 'ser', hidden: true });
    const el = h('section', { class: 'board', 'aria-labelledby': 'board-h' },
      h('h2', { id: 'board-h', class: 'sr' }, g.away_team + ' at ' + g.home_team),
      top,
      lsBox,
      ser,
      h('div', { class: 'model' }, table, lamps));
    el.setLineScore = (ls) => {
      top.replaceChildren(...topLine(g, date, ls).filter(Boolean));
      lsBox.replaceChildren(lineScoreTable(g, ls));
      lamps.replaceChildren(lampList(g, stopped(ls) ? 'none' : lampMode));
    };
    el.setCatchers = (box) => {
      for (const side of ['away', 'home']) {
        const c = box[side] && box[side].catcher && typeof box[side].catcher.name === 'string' ? box[side].catcher.name : null;
        slots[side].catcher.replaceChildren(...bline('C', c || 'not announced'));
      }
    };
    el.setStandings = (s) => {
      for (const side of ['away', 'home']) {
        const t = s[side];
        const rec = t && ok(t.wins) && ok(t.losses) ? t.wins + '–' + t.losses : null;
        slots[side].rec.replaceChildren(rec || '');
        slots[side].rec.hidden = !rec;
        const [place, div] = standingLines(t);
        slots[side].stand.replaceChildren(place || '');
        slots[side].stand.hidden = !place;
        slots[side].divrec.replaceChildren(div || '');
        slots[side].divrec.hidden = !div;
      }
    };
    el.setBattery = (b) => {
      for (const side of ['away', 'home']) {
        const t = b[side] && b[side].together;
        const line = t && ok(t.w) && ok(t.l) ? (t.w + t.l === 0 ? 'First start together this season' : 'Battery ' + t.w + '–' + t.l + ' this season') : null;
        slots[side].bat.replaceChildren(line || '');
        slots[side].bat.hidden = !line;
      }
    };
    el.setSeries = (s) => {
      const lines = seriesLines(g, s);
      ser.replaceChildren(...lines.map(([lab, text]) => h('p', null, lab ? [h('span', { class: 'lab' }, lab), ' '] : null, text)));
      ser.hidden = lines.length === 0;
    };
    return el;
  }

  // ---------- sections under the board ----------
  const section = (cls, title, sub, ...kids) => h('section', { class: 'sec' + (cls ? ' ' + cls : '') },
    h('h2', null, title), sub ? h('p', { class: 'subl' }, sub) : null, kids);

  // A labelled reading: small label, value, and an optional note under it.
  const stat = (k, v, note) => h('div', null, h('div', { class: 'k' }, k), h('div', { class: 'v' }, v), note ? h('div', { class: 'note' }, note) : null);

  function conditions(g) {
    const w = g.weather;
    const title = 'Home run conditions';
    if (!w) return section('cond', title, null, h('p', { class: 'cond-note' }, 'The forecast for this park was not available when the prediction ran.'));
    const indoor = g.roof === 'dome' || w.wind_label === 'Indoors';
    const kids = [];
    if (!indoor) {
      const wind = !w.wind_mph ? 'Calm' : Math.round(w.wind_mph) + ' mph' + (w.wind_from && w.wind_from !== '—' ? ' from ' + w.wind_from : '');
      const cr = ok(w.carry_ft) ? Math.round(w.carry_ft) : null;
      const carry = cr === null ? '–' : (cr > 0 ? '+' : cr < 0 ? '−' : '') + Math.abs(cr) + ' ft';
      const dew = dewPointF(w);
      const elev = elevationFt(g, w);
      kids.push(h('div', { class: 'wx' },
        stat('Temperature', ok(w.temp_f) ? Math.round(w.temp_f) + '°F' : '–'),
        stat('Humidity', ok(w.humidity_pct) ? Math.round(w.humidity_pct) + '%' : '–'),
        stat('Dew point', dew === null ? '–' : dew + '°F'),
        stat('Wind', wind, w.wind_label || null),
        elev === null ? null : stat('Elevation', elev.toLocaleString('en-US') + ' ft'),
        stat('Carry', carry)));
    }
    if (ok(w.hr_score)) {
      const score = Math.max(0, Math.min(100, w.hr_score));
      const fill = h('div', { class: 'fill' + (score >= 55 ? ' hot' : '') });
      fill.style.width = score + '%';
      kids.push(h('div', { class: 'hrbox' },
        h('div', { class: 'hrrow' },
          h('b', null, w.hr_label || ''),
          h('span', null, h('span', { class: 'hrnum' }, String(Math.round(score))), ' out of 100')),
        h('div', { class: 'track', 'aria-hidden': 'true' }, fill)));
    }
    return section('cond', title, null, kids);
  }

  function totals(g) {
    const m = g.markets || {};
    const lines = Object.keys(m.total_over || {}).filter((k) => k.endsWith('.5')).sort((a, b) => Number(a) - Number(b));
    if (!lines.length) return null;
    const total = finalTotal(g);
    let sub = 'Chance the game goes over each line.';
    if (total !== null) {
      const over = lines.filter((k) => total > Number(k)).length;
      sub += ' It finished with ' + runs(total) + ', ' + (over === 0 ? 'under all of them.' : 'over ' + over + ' of them.');
    }
    return section('totals', 'Total runs', sub, h('div', { class: 'tot' }, lines.map((k) => {
      const p = m.total_over[k];
      const fill = h('div', { class: 'bar-fill' });
      fill.style.width = (ok(p) ? Math.max(0, Math.min(100, whole(p))) : 0) + '%';
      return h('div', { class: 'tot-row' },
        h('span', { class: 'tot-l' }, 'Over ' + k),
        h('div', { class: 'bar', 'aria-hidden': 'true' }, fill),
        h('span', { class: 'tot-v' }, pct(p)),
        mark(total !== null && total > Number(k), 'went over'));
    })));
  }

  // One team's starting pitcher block. The starter's name and every prediction come from page.json; once the game has
  // started, the boxscore adds his line. setLine(team, state) fills it in.
  function pitcher(g, side) {
    const p = g.starters && g.starters[side];
    const team = g[side + '_team'];
    const res = finalResults(g);
    const threw = res && res.starter_k && ok(res.starter_k[side]) ? res.starter_k[side] : null;
    const lineSlot = h('div', { class: 'pline-slot' });
    // Stolen bases against the pitcher and the catcher this season (from /api/battery); hidden until then.
    const running = h('div', { class: 'rg', hidden: true });
    const ladder = p ? Object.keys(p.k_over || {}).sort((a, b) => Number(a) - Number(b)) : [];
    const f = p ? p.first_inning : null;
    const el = h('div', { class: 'pit' },
      h('div', { class: 'who' }, nick(team)),
      h('div', { class: 'nm' }, p && p.name ? p.name : 'Starter not announced'),
      lineSlot,
      running,
      p ? h('div', { class: 'ladder' }, ladder.map((k) => h('div', { class: 'rung' },
        h('div', { class: 'l' }, k + '+ K'),
        h('div', { class: 'v' }, pcell(p.k_over[k], 'k' + k)),
        kMark(threw !== null && threw >= Number(k), Number(k))))) : null,
      !p ? null : f
        ? h('div', { class: 'fi' },
          h('div', { class: 'fi-l' }, 'First inning'),
          h('div', { class: 'stats' },
            stat('NRFI record', (ok(f.scoreless) ? f.scoreless : '–') + '-' + (ok(f.not_scoreless) ? f.not_scoreless : '–')),
            stat('ERA', fix2(f.era)),
            stat('OBP', dot3(f.opp_obp)),
            ok(f.whip) ? stat('WHIP', fix2(f.whip)) : null))
        : h('p', { class: 'fi-none' }, 'First-inning record not shown (fewer than 5 starts).'));
    el.setRunning = (b) => {
      const rows = [];
      for (const [who, kind] of [[b && b.pitcher, 'pitcher'], [b && b.catcher, 'catcher']]) {
        if (who && typeof who.name === 'string') rows.push(h('p', { class: 'rg-row' }, h('span', { class: 'rg-name' }, who.name), ' ', runningText(who, kind)));
      }
      running.replaceChildren(...rows);
      running.hidden = rows.length === 0;
    };
    el.setLine = (t, state) => {
      const sp = t && t.pitcher && typeof t.pitcher.name === 'string' ? t.pitcher : null;
      const line = sp && sp.line && typeof sp.line.ip === 'string' ? sp.line : null;
      if (!line || (state !== 'live' && state !== 'final')) { lineSlot.replaceChildren(); return; }
      const n = (x) => (ok(x) ? String(x) : '–');
      // replaceChildren would write a null as the text "null", so leave the note out rather than pass null
      const kids = [h('div', { class: 'pline' }, h('div', { class: 'stats' },
        stat('IP', line.ip), stat('H', n(line.h)), stat('BB', n(line.bb)), stat('SO', n(line.so))))];
      if (!p || !sameName(sp.name, p.name)) kids.unshift(h('p', { class: 'started' }, 'Started: ' + sp.name));
      lineSlot.replaceChildren(...kids);
    };
    return el;
  }

  function lineupTable(g, side) {
    const team = g[side + '_team'];
    const rows = (g.lineups && g.lineups[side]) || [];
    const res = finalResults(g);
    const fin = !!(res && res.batters);
    const byId = {};
    if (fin) for (const r of res.batters[side] || []) byId[r.id] = r;
    const cell = (p, key, kind, yes) => h('td', null, h('span', { class: 'pv' }, pcell(p, key), fin ? propMark(yes, kind) : null));
    const thead = h('thead', null, h('tr', null,
      h('th', { class: 'o', scope: 'col' }, '#'),
      h('th', { class: 'l', scope: 'col' }, 'Player'),
      ['1+ hit', '2+ bases', 'Homer', 'RBI', 'Steal'].map((c) => h('th', { scope: 'col', class: 'v' }, c)),
      fin ? h('th', { class: 'l', scope: 'col' }, 'Result') : null));
    const tbody = h('tbody', null, rows.map((r) => {
      const b = byId[r.id] || null;
      return h('tr', null,
        h('td', { class: 'o' }, String(r.slot)),
        h('td', { class: 'n' }, r.name),
        cell(r.hit, 'hit', 'hit', b && b.h >= 1),
        cell(r.tb2, 'tb', 'tb', b && b.tb >= 2),
        cell(r.hr, 'hr', 'hr', b && b.hr >= 1),
        cell(r.rbi, 'rbi', 'rbi', b && b.rbi >= 1),
        cell(r.sb, 'sb', 'sb', b && b.sb >= 1),
        fin ? h('td', { class: 'rs' }, b ? b.line : '–') : null);
    }));
    const table = h('table', { class: 't' + (fin ? ' fin' : '') }, h('caption', { class: 'sr' }, team + ' batting order'), thead, tbody);
    return h('div', { class: 'lu' },
      h('h3', null, nick(team)),
      h('div', { class: 'scroll', tabindex: '0', role: 'region', 'aria-label': nick(team) + ' batting order table' }, table));
  }

  // ---------- rail ----------
  function cutoffs() {
    const rows = [
      ['Odds to win', 'ml'], ['First 5 winner', 'f5'], ['NRFI', 'nrfi'],
      ['Starter 3+ strikeouts', 'k3'], ['Starter 4+ strikeouts', 'k4'], ['Starter 5+ strikeouts', 'k5'],
      ['Starter 6+ strikeouts', 'k6'], ['Starter 7+ strikeouts', 'k7'],
      ['Batter 1+ hit', 'hit'], ['Batter 2+ total bases', 'tb'], ['Batter 1+ home run', 'hr'],
      ['Batter 1+ RBI', 'rbi'], ['Batter 1+ stolen base', 'sb'],
    ];
    const cut = (x) => whole(x) + '%';
    return h('details', { class: 'how' },
      h('summary', null, 'Cut-off for every market'),
      h('p', { class: 'key' }, 'Yellow numbers on a green plate clear the strong cut-off, plain numbers clear the watch line, and grey numbers fall below it.'),
      h('table', null,
        h('caption', { class: 'sr' }, 'Strong and watch cut-offs for every market'),
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Market'), h('th', { scope: 'col', class: 'r' }, 'Strong'), h('th', { scope: 'col', class: 'r' }, 'Watch'))),
        h('tbody', null, rows.map(([label, key]) => h('tr', null,
          h('td', null, label),
          h('td', { class: 'r' }, cut(THRESH[key][0]) + '+'),
          h('td', { class: 'r' }, cut(THRESH[key][1]) + '+'))))),
      h('p', null, 'Home run conditions score, 0 to 100: 65 and up Very HR Friendly, 55 to 64 HR Friendly, 45 to 54 Neutral, 35 to 44 HR Suppressed, under 35 Very HR Suppressed. One point is about one foot of expected carry change. Indoor parks are scored a neutral 50.'));
  }

  // The game page's other games: one line each (teams, then the score or first pitch), each a link to its page. The
  // slate is fetched once, with no polling; without it each line shows what page.json has.
  function otherGames(games, date) {
    const status = (g, s) => {
      const now = gameNow(g, s);
      const score = ok(now.away) && ok(now.home) ? ' ' + now.away + '–' + now.home : '';
      if (now.kind === 'stopped') return now.detail;
      if (now.kind === 'final') return 'Final' + score;
      if (now.kind === 'live') return 'Live' + score;
      return clock(g.start_time_utc);
    };
    const items = games.map((g) => {
      const st = h('span', { class: 'og-st' }, status(g, null));
      const li = h('li', null, h('a', { href: '/game/' + g.game_id }, h('span', { class: 'og-t' }, matchup(g)), ' ', st));
      li.set = (s) => st.replaceChildren(status(g, s));
      return li;
    });
    getSlate(date).then((sl) => {
      if (!sl) return;
      games.forEach((g, i) => { const s = sl.games[String(g.game_id)]; if (s) items[i].set(s); });
    });
    return section('others', 'Other games', null, h('ul', { class: 'og' }, items));
  }

  // ---------- live data for the game on a game page ----------
  // One timer: each tick fetches the line score and the boxscore together. The series and the standings are fetched
  // once; the battery on the first tick, and again only when the boxscore's starters or catchers change (a lineup
  // posted after the page opened).
  function liveScore(g, view) {
    let timer = null;
    let due = false; // a refresh is owed, kept while the tab is hidden
    let last = null; // the last state the API gave
    let who = null;  // the starters and catchers the battery was last fetched for

    // Milliseconds until the next refresh, or null for none.
    function nextDelay(ls) {
      if (stopped(ls)) return null;
      const state = ls ? ls.state : last || (g.status === 'scheduled' ? 'preview' : g.status);
      if (state === 'live') return LIVE_MS;
      if (state !== 'preview') return null;
      const start = Date.parse(g.start_time_utc);
      if (!Number.isFinite(start)) return null;
      const wait = start - PRE_WINDOW_MS - Date.now();
      return wait > 0 ? wait : PRE_MS;
    }

    async function load() {
      timer = null;
      due = false;
      const [ls, box] = await Promise.all([getLineScore(g.game_id), getBoxScore(g.game_id)]);
      if (ls) { last = ls.state; view.board.setLineScore(ls); }
      if (box) {
        const state = ls ? ls.state : box.state;
        view.board.setCatchers(box);
        view.pitchers.away.setLine(box.away, state);
        view.pitchers.home.setLine(box.home, state);
      }
      const id = (x) => (x && ok(x.id) ? x.id : null);
      const key = box ? JSON.stringify(['away', 'home'].map((s) => [id(box[s].pitcher), id(box[s].catcher)])) : who || 'unknown';
      if (key !== who) {
        who = key;
        getBattery(g.game_id).then((b) => {
          if (!b) return;
          view.board.setBattery(b);
          view.pitchers.away.setRunning(b.away);
          view.pitchers.home.setRunning(b.home);
        });
      }
      const next = nextDelay(ls);
      if (next === null) return;
      due = true;
      if (!document.hidden) timer = setTimeout(load, next);
    }

    // Hidden tab: no requests. Visible again: refresh at once, then carry on.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { clearTimeout(timer); timer = null; } else if (due && timer === null) load();
    });

    load();
    getSeries(g.game_id).then((s) => { if (s) view.board.setSeries(s); });
    getStandings(g.game_id).then((s) => { if (s) view.board.setStandings(s); });
  }

  // ---------- page titles ----------
  // The replay's title block (title, headline, notice, updated), as every page showed it before the split.
  function replayHead(page, site) {
    const out = [h('h1', null, site.replay_title || 'MLB predictions')];
    if (site.replay_headline) out.push(h('p', { class: 'lede' }, site.replay_headline));
    if (page.label) out.push(h('p', { class: 'notice' }, page.label));
    const upd = updatedText(page);
    if (upd) out.push(h('p', { class: 'upd' }, upd));
    return out;
  }

  // ---------- home page ----------
  const GROUPS = [['live', 'In progress'], ['preview', 'Later today'], ['final', 'Final'], ['stopped', 'Postponed']];

  // One card per game, the whole card a link to the game page. update(slate entry or null) redraws it in place, so the
  // link (and keyboard focus on it) survives a refresh.
  function card(g) {
    const m = g.markets || {};
    const el = h('a', { class: 'card', href: '/game/' + g.game_id });
    el.update = (s) => {
      const now = gameNow(g, s);
      el.kind = now.kind;
      const live = now.kind === 'live';
      const fin = now.kind === 'final';
      let st;
      if (live) st = [lamp('on'), cap(now.where) || 'In progress'];
      else if (fin) st = 'Final';
      else if (now.kind === 'stopped') st = now.detail;
      else st = clock(g.start_time_utc);
      const started = now.kind !== 'preview' && ok(now.away) && ok(now.home);
      const loser = fin && started && now.away !== now.home ? (now.away < now.home ? 'away' : 'home') : null;
      // the record sits beside the nickname and drops under it when the card is narrow
      const team = (side) => h('div', { class: 'cd-team' },
        h('span', { class: 'cd-name' }, h('span', { class: 'cd-nick' }, nick(g[side + '_team'])),
          now.rec[side] ? h('span', { class: 'cd-rec' }, now.rec[side]) : null),
        mini(m[side + '_win'], 'ml'),
        started ? h('span', { class: 'cd-runs' + (loser === side ? ' lose' : '') }, String(now[side])) : null);
      let foot;
      if (fin) foot = lampList(g, 'final', true);
      else {
        const ou = ouLine(m.total_over);
        const nr = boardPicks(g).find((it) => it.cls === 'lr-nr');
        foot = h('ul', { class: 'lr' }, ou ? h('li', null, 'Total O/U ' + ou.line) : null, h('li', null, nr.label));
      }
      el.replaceChildren(
        h('div', { class: 'cd-top' }, h('span', { class: 'cd-st' + (live ? ' live' : '') }, st), g.park ? h('span', { class: 'cd-park' }, g.park) : null),
        team('away'), team('home'),
        h('div', { class: 'cd-foot' }, foot, h('span', { class: 'cd-more' }, 'Full game')));
      // The card read as one sentence: "Brewers at Rays, final, 4 to 1. Full game."
      const score = started ? now.away + ' to ' + now.home : null;
      const words = fin ? ['final', score]
        : live ? [now.where || 'in progress', score]
          : now.kind === 'stopped' ? [now.detail.charAt(0).toLowerCase() + now.detail.slice(1), ok(now.away) && ok(now.home) ? now.away + ' to ' + now.home : null]
            : [clock(g.start_time_utc) ? 'first pitch ' + clock(g.start_time_utc) : null];
      el.setAttribute('aria-label', [matchup(g), ...words].filter(Boolean).join(', ') + '. Full game.');
    };
    el.update(null);
    return el;
  }

  // Home: the slate once on load, then every 30 seconds while any game is live or within 15 minutes of first pitch,
  // on one timer, paused while the tab is hidden. With nothing near, it wakes when the next game's window opens.
  function watchSlate(date, games, onSlate) {
    let timer = null;
    let due = false;
    let last = null;
    function nextDelay() {
      let wait = null;
      for (const g of games) {
        const kind = gameNow(g, last ? last.games[String(g.game_id)] || null : null).kind;
        if (kind === 'live') return LIVE_MS;
        if (kind !== 'preview') continue;
        const start = Date.parse(g.start_time_utc);
        if (!Number.isFinite(start)) continue;
        const w = start - PRE_WINDOW_MS - Date.now();
        if (w <= 0) return LIVE_MS;
        wait = wait === null ? w : Math.min(wait, w);
      }
      return wait;
    }
    async function tick() {
      timer = null;
      due = false;
      const s = await getSlate(date);
      if (s) { last = s; onSlate(s); }
      const next = nextDelay();
      if (next === null) return;
      due = true;
      if (!document.hidden) timer = setTimeout(tick, next);
    }
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { clearTimeout(timer); timer = null; } else if (due && timer === null) tick();
    });
    tick();
  }

  function btsTop(picks, games, replay) {
    const top = picks.slice(0, 3);
    const list = top.length
      ? h('ol', { class: 'bts' }, top.map((p, i) => {
        const g = pickGame(p, games);
        return h('li', null,
          h('span', { class: 'rk' }, String(i + 1)),
          h('div', null,
            h('div', { class: 'name' }, p.name),
            h('div', { class: 'meta' }, g ? h('a', { href: '/game/' + g.game_id }, matchup(g)) : nick(p.team))),
          pcell(p.hit, 'hit'));
      }))
      : h('p', { class: 'msg' }, 'Picks appear once lineups are posted.');
    return section('', 'Beat the Streak', replay ? 'The best chances of a hit.' : 'The best chances of a hit today.', list,
      h('p', { class: 'more' }, h('a', { href: '/beat-the-streak' }, 'All picks')));
  }

  function renderHome(app, page, site) {
    const games = page.games.slice().sort(byStart);
    const picks = Array.isArray(page.beat_the_streak) ? page.beat_the_streak : [];
    const upd = updatedText(page);
    const replay = page.note === 'replay';
    const head = replay ? replayHead(page, site)
      : [h('h1', null, longDate(page.date, false) || 'MLB predictions'),
        h('p', { class: 'upd' }, [games.length + (games.length === 1 ? ' game.' : ' games.'), upd].filter(Boolean).join(' '))];

    const cards = games.map(card);
    const groups = h('div', { class: 'groups' });
    let shape = '';
    // Each card under its group heading; moved only when a group changes, and focus follows a moved card.
    function place() {
      const now = cards.map((c) => c.kind).join();
      if (now === shape) return;
      shape = now;
      const f = document.activeElement;
      groups.replaceChildren(...GROUPS.filter(([key]) => cards.some((c) => c.kind === key)).map(([key, title]) => h('div', { class: 'grp' },
        h('h3', null, title),
        h('div', { class: 'cards' }, cards.filter((c) => c.kind === key)))));
      if (f && f !== document.activeElement && f.isConnected) f.focus();
    }

    app.replaceChildren(...head, h('div', { class: 'cols' },
      h('div', { class: 'maincol' }, section('games', replay ? 'Games' : "Today's games", null,
        games.length ? groups : h('p', { class: 'msg' }, 'No games are scheduled for this date.'))),
      h('aside', { class: 'rail', 'aria-label': 'Beat the Streak' }, btsTop(picks, page.games, replay))));
    if (!games.length) return;
    place();
    // Only games in page.json are updated; anything else on the slate is ignored.
    watchSlate(page.date, games, (s) => {
      games.forEach((g, i) => { const e = s.games[String(g.game_id)]; if (e) cards[i].update(e); });
      place();
    });
  }

  // ---------- game page ----------
  function renderGamePage(app, page, site) {
    const m = /^\/game\/(\d{1,8})$/.exec(location.pathname);
    const g = m ? page.games.find((x) => String(x.game_id) === m[1]) : null;
    const back = h('p', { class: 'back' }, h('a', { href: '/' }, 'All games'));
    if (!g) {
      app.replaceChildren(h('p', { class: 'msg' }, "This game isn't on today's slate."), back);
      return;
    }
    const md = monthDay(page.date);
    document.title = matchup(g) + (md ? ', ' + md : '') + ': Diamond Metrics';
    const upd = updatedText(page);
    const head = page.note === 'replay' ? replayHead(page, site)
      : [h('h1', null, matchup(g)), h('p', { class: 'upd' }, [longDate(page.date, false) ? longDate(page.date, false) + '.' : '', upd].filter(Boolean).join(' '))];

    const b = board(g, page.date);
    const pitchers = { away: pitcher(g, 'away'), home: pitcher(g, 'home') };
    const others = page.games.filter((x) => x !== g).sort(byStart);
    app.replaceChildren(...head, back, b, h('div', { class: 'cols' },
      h('div', { class: 'maincol' }, [conditions(g), totals(g),
        section('pitchers', 'Starting pitchers', null, h('div', { class: 'pits' }, pitchers.away, pitchers.home)),
        section('lineups', 'Lineups and player props', null, lineupTable(g, 'away'), lineupTable(g, 'home'))]),
      h('aside', { class: 'rail', 'aria-label': others.length ? 'Other games and cut-offs' : 'Cut-offs' },
        others.length ? otherGames(others, page.date) : null, cutoffs())));
    liveScore(g, { board: b, pitchers });
  }

  // ---------- Beat the Streak page ----------
  function renderBts(app, page, site) {
    const picks = Array.isArray(page.beat_the_streak) ? page.beat_the_streak : [];
    const sub = page.note === 'replay' ? site.replay_title || '' : longDate(page.date, false);
    const list = picks.length
      ? h('ol', { class: 'bts full' }, picks.map((p, i) => {
        const g = pickGame(p, page.games);
        const r = g ? resultIn(p, g) : null;
        const vs = g ? h('a', { href: '/game/' + g.game_id }, g.away_team === p.team ? 'at ' + nick(g.home_team) : 'vs ' + nick(g.away_team)) : null;
        return h('li', null,
          h('span', { class: 'rk' }, String(i + 1)),
          h('div', null,
            h('div', { class: 'name' }, p.name),
            h('div', { class: 'meta' }, nick(p.team), vs ? [' ', vs] : null,
              r && r.line ? h('span', { class: 'line' }, r.line, r.h >= 1 ? h('span', { class: 'ck' }, svgIcon(BAT, 14, 'got a hit')) : null) : null)),
          pcell(p.hit, 'hit'));
      }))
      : h('p', { class: 'msg' }, 'Picks appear once lineups are posted.');
    // replaceChildren would write a null as the text "null", so the missing date line is filtered out
    app.replaceChildren(...[
      h('h1', null, 'Beat the Streak'),
      sub ? h('p', { class: 'upd' }, sub) : null,
      h('p', { class: 'lede' }, "Beat the Streak is MLB's free contest: pick a batter to get a hit on the days you play, and 57 correct picks in a row win the grand prize."),
      h('div', { class: 'picks' }, list)].filter(Boolean));
  }

  async function main() {
    const app = document.getElementById('app');
    const render = { home: renderHome, game: renderGamePage, bts: renderBts }[document.body.getAttribute('data-page')];
    if (!app || !render) return;
    let page;
    let site = {};
    try {
      page = await getJSON('/data/page.json');
      if (!page || !Array.isArray(page.games)) throw new Error('page.json has no games list');
    } catch (e) {
      app.replaceChildren(h('p', { class: 'msg', role: 'alert' }, 'The predictions did not load. Try reloading the page.'));
      return;
    }
    try { site = (await getJSON('/data/site.json')) || {}; } catch (e) { site = {}; }
    renderBanner(site);
    try {
      render(app, page, site);
    } catch (e) {
      app.replaceChildren(h('p', { class: 'msg', role: 'alert' }, 'The predictions loaded but could not be shown. Try reloading the page.'));
      if (window.console) console.error(e);
    }
  }

  main();
})();
