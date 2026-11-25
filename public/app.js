/* diamondmetrics: MLB predictions.
   Reads data/page.json (and optionally data/site.json) and builds the page with DOM calls.
   The selected game's line score, starters' lines and catchers, and season series come from the same-origin Worker
   (/api/linescore, /api/boxscore and /api/series). page.json stays the source for every prediction.
   Text is inserted as text nodes only, there are no inline styles, and no third-party requests. */
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

  // Live data refresh: every 30 s while live; every 5 min for a game that starts within 15 min.
  const LIVE_MS = 30000;
  const PRE_MS = 300000;
  const PRE_WINDOW_MS = 15 * 60000;
  const FETCH_TIMEOUT_MS = 10000;
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
  function svgIcon(shapes, size, label) {
    const s = document.createElementNS(SVGNS, 'svg');
    for (const [k, v] of [['viewBox', '0 0 16 16'], ['width', size], ['height', size], ['focusable', 'false'], ['class', 'ico']]) s.setAttribute(k, String(v));
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
  // Lineup result icons, one per batter prop.
  const ICONS = {
    // A filled bat laid on the diagonal: knob, thin handle, tapering out to a rounded barrel.
    hit: {
      label: 'got a hit',
      shapes: [['path', {
        d: 'M-5.7-.75H-1C2-.75 3-2.5 5.5-2.5A2.5 2.5 0 0 1 5.5 2.5C3 2.5 2 .75-1 .75H-5.7A1.5 1.5 0 1 1-5.7-.75z',
        transform: 'translate(8 8) rotate(-45)', fill: 'currentColor', stroke: 'none',
      }]],
    },
    tb: { label: 'two or more bases', shapes: [['path', { d: 'M3 8.5l3.2 3.2L13 4.5', 'stroke-width': '2.2' }]] },
    hr: { label: 'homered', shapes: [['circle', { cx: '8', cy: '8', r: '5.8' }], ['path', { d: 'M4.8 3.6c1.5 1.3 2.2 2.7 2.2 4.4s-.7 3.1-2.2 4.4', 'stroke-width': '1.2' }], ['path', { d: 'M11.2 3.6C9.7 4.9 9 6.3 9 8s.7 3.1 2.2 4.4', 'stroke-width': '1.2' }]] },
    rbi: { label: 'drove in a run', shapes: [['path', { d: 'M3 2.8h10v6L8 13.4 3 8.8z' }]] },
    sb: { label: 'stole a base', shapes: [['path', { d: 'M8 2.4L13.6 8 8 13.6 2.4 8z' }]] },
  };

  // A check mark with words for screen readers, in a fixed-width slot so numbers stay aligned.
  const mark = (yes, words) => h('span', { class: 'ck' }, yes ? [svgIcon(CHECK, 16, null), h('span', { class: 'sr' }, ' ' + words)] : null);
  // A lineup result icon in the same slot; the icon itself carries the words.
  const propMark = (yes, kind) => h('span', { class: 'ck' }, yes ? svgIcon(ICONS[kind].shapes, 14, ICONS[kind].label) : null);

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

  function ymd(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || '');
    return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
  }

  function longDate(s) {
    const p = ymd(s);
    if (!p) return '';
    return new Date(Date.UTC(p[0], p[1] - 1, p[2])).toLocaleDateString('en-US', {
      weekday: 'long', month: 'long', day: 'numeric', year: 'numeric', timeZone: 'UTC',
    });
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

  function stamp(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
    });
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

  // One Worker endpoint, or null on any failure (the page then leaves that part empty, without an error).
  async function getApi(name, gamePk, valid) {
    if (!/^\d{1,8}$/.test(String(gamePk))) return null;
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS);
    try {
      const r = await fetch('/api/' + name + '/' + gamePk, { signal: ctl.signal });
      if (!r.ok) return null;
      const body = await r.json();
      return body && valid(body) ? body : null;
    } catch (e) {
      return null;
    } finally {
      clearTimeout(t);
    }
  }
  const getLineScore = (pk) => getApi('linescore', pk, (b) => typeof b.state === 'string' && Array.isArray(b.innings));
  const getBoxScore = (pk) => getApi('boxscore', pk, (b) => typeof b.state === 'string' && b.away && b.home);
  const getSeries = (pk) => getApi('series', pk, (b) => b.regular && typeof b.regular === 'object');

  // ---------- Opening Day countdown ----------
  function renderBanner(site) {
    const el = document.getElementById('banner');
    if (!el || !site || !site.opening_day) return;
    const n = daysUntil(site.opening_day);
    if (n === null || n < 0) return;
    const year = ymd(site.opening_day)[0];
    const label = site.opening_label || longDate(site.opening_day);
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
  function statusText(g) {
    if (g.status === 'final') return 'Final ' + g.away_score + '–' + g.home_score;
    if (g.status === 'live') return 'Live';
    return clock(g.start_time_utc);
  }

  // "Live, bottom 5th, 2 outs" from page.json (the fallback when the line score can't be fetched).
  function liveText(g) {
    const half = g.inning_half ? String(g.inning_half).toLowerCase() : '';
    const where = g.inning ? [half, ordinal(g.inning)].filter(Boolean).join(' ') : '';
    const outs = typeof g.outs === 'number' ? ', ' + g.outs + (g.outs === 1 ? ' out' : ' outs') : '';
    return 'Live' + (where ? ', ' + where + outs : '');
  }

  // The same line from the API: half is top, bottom, middle or end; outs only during a half.
  function apiLiveText(ls) {
    const where = ok(ls.inning) ? [ls.half, ordinal(ls.inning)].filter(Boolean).join(' ') : '';
    const during = ls.half === 'top' || ls.half === 'bottom';
    const outs = during && ok(ls.outs) ? ', ' + ls.outs + (ls.outs === 1 ? ' out' : ' outs') : '';
    return 'Live' + (where ? ', ' + where + outs : '');
  }

  const stopped = (ls) => !!(ls && STOPPED.test(ls.detail || ''));

  const finalResults = (g) => (g.status === 'final' && g.results ? g.results : null);

  function finalTotal(g) {
    if (g.status !== 'final') return null;
    const res = g.results;
    if (res && ok(res.total_runs)) return res.total_runs;
    return ok(g.away_score) && ok(g.home_score) ? g.away_score + g.home_score : null;
  }

  // The pick's box-score row once his game is final, or null.
  function pickResult(pick, games) {
    for (const g of games) {
      if (g.status !== 'final' || !g.results || !g.results.batters) continue;
      if (pick.game_id !== undefined && pick.game_id !== null && g.game_id !== pick.game_id) continue;
      const side = g.away_team === pick.team ? 'away' : g.home_team === pick.team ? 'home' : null;
      if (!side) continue;
      const row = ((g.lineups && g.lineups[side]) || []).find((r) => r.name === pick.name);
      if (!row) continue;
      const res = (g.results.batters[side] || []).find((r) => r.id === row.id);
      if (res) return res;
    }
    return null;
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
  // Total runs: the .5 keys of markets.total_over. Strikeouts: k_over[n] is n or more, the over on line n - 0.5.
  const ouLine = (over) => closestLine(over, (k) => (k.endsWith('.5') ? Number(k) : null));
  const kLine = (over) => closestLine(over, (k) => (/^\d+$/.test(k) ? Number(k) - 0.5 : null));

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
    const day = longDate(date);
    const first = clock(g.start_time_utc);
    let when;
    let live = null;
    if (ls) {
      if (stopped(ls)) when = ls.detail + '.';
      else if (ls.state === 'preview') when = first ? 'First pitch ' + first + '.' : null;
      else when = day ? day + '.' : null;
      if (ls.state === 'live' && !stopped(ls)) live = apiLiveText(ls);
    } else {
      when = g.status === 'final' || g.status === 'live' ? (day ? day + '.' : null) : (first ? 'First pitch ' + first + '.' : null);
      if (g.status === 'live') live = liveText(g);
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

  function board(g, date) {
    const m = g.markets || {};
    const fin = g.status === 'final';
    const ou = ouLine(m.total_over);

    // Sportsbook layout: the away row shows the over, the home row the under, for the same line; the pair adds to 100.
    function ouCell(side) {
      if (!ou) return plate('–', 'b-plain');
      const over = side === 'away';
      return plate(wholePct(over ? ou.over : 100 - ou.over), 'b-plain', (over ? 'Over ' : 'Under ') + ou.line);
    }

    // The battery under each team name: the starter from page.json, the catcher from the boxscore (filled in later).
    const bline = (pos, name) => [h('span', { class: 'pos' }, pos), ' ', h('span', { class: 'who' }, name)];
    const catchers = {
      away: h('span', { class: 'bl bl-c' }, bline('C', 'not announced')),
      home: h('span', { class: 'bl bl-c' }, bline('C', 'not announced')),
    };

    function row(side) {
      const team = g[side + '_team'];
      const st = g.starters && g.starters[side];
      return h('tr', null,
        h('th', { scope: 'row', class: 'team' },
          h('span', { class: 'nick' }, nick(team)),
          h('span', { class: 'bl bl-sp' }, bline('SP', st && st.name ? st.name : 'not announced')),
          catchers[side]),
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

    // Result lamps: unlit before and during the game; on a final, green if the pick came true and red if it missed.
    // No lamp when there is no pick, or the result isn't in page.json.
    const lamps = h('div', { class: 'lamprow' }, h('ul', { class: 'lr' }, boardPicks(g).map((it) => {
      let state = null;
      if (it.pick && !fin) state = '';
      else if (it.pick && it.hit !== null) state = it.hit ? 'hit' : 'miss';
      return h('li', { class: it.cls },
        state === null ? null : lamp(state),
        it.label,
        state === 'hit' || state === 'miss' ? h('span', { class: 'sr' }, state === 'hit' ? " Model's pick came true." : " Model's pick missed.") : null);
    })));

    const top = h('div', { class: 'bd-top' }, topLine(g, date, null));
    // The scroll box stays in place across refreshes, so keyboard focus on it is kept.
    const lsBox = h('div', { class: 'ls-scroll', tabindex: '0', role: 'region', 'aria-label': 'Line score' }, lineScoreTable(g, null));
    // The season series strip stays hidden until the series loads (and stays hidden if it can't).
    const ser = h('div', { class: 'ser', hidden: true });
    const el = h('section', { id: 'today', class: 'board', 'aria-labelledby': 'board-h' },
      h('h2', { id: 'board-h', class: 'sr' }, g.away_team + ' at ' + g.home_team),
      top,
      lsBox,
      ser,
      h('div', { class: 'model' }, table, lamps));
    el.setLineScore = (ls) => {
      top.replaceChildren(...topLine(g, date, ls).filter(Boolean));
      lsBox.replaceChildren(lineScoreTable(g, ls));
    };
    el.setCatchers = (box) => {
      for (const side of ['away', 'home']) {
        const c = box[side] && box[side].catcher && typeof box[side].catcher.name === 'string' ? box[side].catcher.name : null;
        catchers[side].replaceChildren(...bline('C', c || 'not announced'));
      }
    };
    el.setSeries = (s) => {
      const lines = seriesLines(g, s);
      ser.replaceChildren(...lines.map(([lab, text]) => h('p', null, lab ? [h('span', { class: 'lab' }, lab), ' '] : null, text)));
      ser.hidden = lines.length === 0;
    };
    return el;
  }

  function outOfTown(games, sel, onPick) {
    return h('section', { class: 'oot', 'aria-labelledby': 'oot-h' },
      h('h2', { id: 'oot-h' }, "Today's games"),
      h('div', { class: 'oot-grid' }, games.map((g) => {
        const m = g.markets || {};
        return h('button', {
          type: 'button', class: 'gm', 'data-id': g.game_id, 'aria-pressed': String(g.game_id === sel),
          onclick: () => onPick(g.game_id),
        },
        h('span', { class: 'gm-t' }, code(g.away_team)), mini(m.away_win, 'ml'),
        h('span', { class: 'gm-at' }, 'at'),
        h('span', { class: 'gm-t' }, code(g.home_team)), mini(m.home_win, 'ml'),
        h('span', { class: 'st' }, statusText(g)));
      })));
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
    const kl = p ? kLine(p.k_over) : null;
    const ladder = p ? Object.keys(p.k_over || {}).sort((a, b) => Number(a) - Number(b)) : [];
    const f = p ? p.first_inning : null;
    const el = h('div', { class: 'pit' },
      h('div', { class: 'who' }, nick(team)),
      h('div', { class: 'nm' }, p && p.name ? p.name : 'Starter not announced'),
      kl ? h('div', { class: 'kou' },
        h('div', { class: 'kou-l' }, 'Strikeouts O/U ' + kl.line),
        h('div', { class: 'kou-v' },
          h('span', null, 'Over ', h('b', null, wholePct(kl.over))),
          h('span', null, 'Under ', h('b', null, wholePct(100 - kl.over))))) : null,
      lineSlot,
      p ? h('div', { class: 'ladder' }, ladder.map((k) => h('div', { class: 'rung' },
        h('div', { class: 'l' }, k + '+ K'),
        h('div', { class: 'v' }, pcell(p.k_over[k], 'k' + k)),
        mark(threw !== null && threw >= Number(k), 'reached')))) : null,
      !p ? null : f
        ? h('div', { class: 'fi' },
          h('div', { class: 'fi-l' }, 'First inning'),
          h('div', { class: 'stats' },
            stat('NRFI record', (ok(f.scoreless) ? f.scoreless : '–') + '-' + (ok(f.not_scoreless) ? f.not_scoreless : '–')),
            stat('ERA', fix2(f.era)),
            stat('OBP', dot3(f.opp_obp)),
            ok(f.whip) ? stat('WHIP', fix2(f.whip)) : null))
        : h('p', { class: 'fi-none' }, 'First-inning record not shown (fewer than 5 starts).'));
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
  function beatTheStreak(picks, games) {
    const list = picks.length
      ? h('ol', { class: 'bts' }, picks.map((p, i) => {
        const r = pickResult(p, games);
        return h('li', null,
          h('span', { class: 'rk' }, String(i + 1)),
          h('div', null,
            h('div', { class: 'name' }, p.name),
            h('div', { class: 'meta' }, nick(p.team), r && r.line ? h('span', { class: 'line' }, r.line) : null)),
          pcell(p.hit, 'hit'));
      }))
      : h('p', { class: 'msg' }, 'Picks appear once lineups are posted.');
    const s = section('', 'Beat the Streak', 'The five best chances of a hit.', list);
    s.id = 'bts';
    return s;
  }

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

  // ---------- live data for the selected game ----------
  // One game at a time, one timer: each tick fetches the line score and the boxscore together. The series is fetched
  // once per game. gen changes on every game switch, so a late answer for the old game is dropped.
  function liveScore() {
    let gen = 0;
    let timer = null;
    let due = null;  // the next refresh, kept while the tab is hidden: { my, g, view }
    let last = null; // the last state the API gave for this game

    // Milliseconds until the next refresh, or null for none.
    function nextDelay(g, ls) {
      if (stopped(ls)) return null;
      const state = ls ? ls.state : last || (g.status === 'scheduled' ? 'preview' : g.status);
      if (state === 'live') return LIVE_MS;
      if (state !== 'preview') return null;
      const start = Date.parse(g.start_time_utc);
      if (!Number.isFinite(start)) return null;
      const wait = start - PRE_WINDOW_MS - Date.now();
      return wait > 0 ? wait : PRE_MS;
    }

    function fire() {
      timer = null;
      const d = due;
      due = null;
      if (d && d.my === gen) load(d.my, d.g, d.view);
    }

    async function load(my, g, view) {
      const [ls, box] = await Promise.all([getLineScore(g.game_id), getBoxScore(g.game_id)]);
      if (my !== gen) return;
      if (ls) { last = ls.state; view.board.setLineScore(ls); }
      if (box) {
        const state = ls ? ls.state : box.state;
        view.board.setCatchers(box);
        view.pitchers.away.setLine(box.away, state);
        view.pitchers.home.setLine(box.home, state);
      }
      const next = nextDelay(g, ls);
      if (next === null) return;
      due = { my, g, view };
      if (!document.hidden) timer = setTimeout(fire, next);
    }

    async function loadSeries(my, g, view) {
      const s = await getSeries(g.game_id);
      if (my === gen && s) view.board.setSeries(s);
    }

    // Hidden tab: no requests. Visible again: refresh at once, then carry on.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { clearTimeout(timer); timer = null; } else if (due && timer === null) fire();
    });

    return {
      // view: { board, pitchers: { away, home } }
      show(g, view) {
        gen += 1;
        clearTimeout(timer);
        timer = null;
        due = null;
        last = null;
        load(gen, g, view);
        loadSeries(gen, g, view);
      },
    };
  }

  // ---------- page ----------
  function renderPage(app, page, site) {
    const games = page.games;
    const picks = Array.isArray(page.beat_the_streak) ? page.beat_the_streak.slice(0, 5) : [];
    const replay = page.note === 'replay';
    let sel = games.length ? games[0].game_id : null;
    const score = liveScore();

    const upd = page.updated_at && stamp(page.updated_at) ? 'Updated ' + stamp(page.updated_at) + '.' : '';
    const head = [h('h1', null, replay ? (site.replay_title || 'MLB predictions') : (longDate(page.date) || 'MLB predictions'))];
    if (replay) {
      if (site.replay_headline) head.push(h('p', { class: 'lede' }, site.replay_headline));
      if (page.label) head.push(h('p', { class: 'notice' }, page.label));
      if (upd) head.push(h('p', { class: 'upd' }, upd));
    } else {
      head.push(h('p', { class: 'upd' }, [games.length + (games.length === 1 ? ' game.' : ' games.'), upd].filter(Boolean).join(' ')));
    }

    const boardSlot = h('div', { class: 'board-slot' });
    const mainCol = h('div', { class: 'maincol' });

    function renderGame() {
      const g = games.find((x) => x.game_id === sel);
      if (!g) {
        boardSlot.replaceChildren();
        mainCol.replaceChildren(h('p', { class: 'msg' }, 'No games are scheduled for this date.'));
        return;
      }
      const b = board(g, page.date);
      const pitchers = { away: pitcher(g, 'away'), home: pitcher(g, 'home') };
      boardSlot.replaceChildren(b);
      mainCol.replaceChildren(...[conditions(g), totals(g),
        section('pitchers', 'Starting pitchers', null,
          h('div', { class: 'pits' }, pitchers.away, pitchers.home)),
        section('lineups', 'Lineups and player props', null,
          lineupTable(g, 'away'), lineupTable(g, 'home')),
      ].filter(Boolean));
      score.show(g, { board: b, pitchers });
    }

    const kids = [...head];
    if (games.length > 1) {
      const oot = outOfTown(games, sel, (id) => {
        sel = id;
        for (const b of oot.querySelectorAll('.gm')) b.setAttribute('aria-pressed', String(b.getAttribute('data-id') === String(id)));
        renderGame();
      });
      kids.push(oot);
    }
    kids.push(boardSlot, h('div', { class: 'cols' },
      mainCol,
      h('aside', { class: 'rail', 'aria-label': 'Beat the Streak and cut-offs' }, beatTheStreak(picks, games), cutoffs())));
    app.replaceChildren(...kids);
    renderGame();
  }

  async function main() {
    const app = document.getElementById('app');
    let page;
    let site = {};
    try {
      page = await getJSON('data/page.json');
      if (!page || !Array.isArray(page.games)) throw new Error('page.json has no games list');
    } catch (e) {
      app.replaceChildren(h('p', { class: 'msg', role: 'alert' }, 'The predictions did not load. Try reloading the page.'));
      return;
    }
    try { site = (await getJSON('data/site.json')) || {}; } catch (e) { site = {}; }
    renderBanner(site);
    try {
      renderPage(app, page, site);
    } catch (e) {
      app.replaceChildren(h('p', { class: 'msg', role: 'alert' }, 'The predictions loaded but could not be shown. Try reloading the page.'));
      if (window.console) console.error(e);
    }
  }

  main();
})();
