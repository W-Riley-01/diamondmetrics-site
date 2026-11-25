/* diamondmetrics: MLB predictions.
   Reads data/page.json (and optionally data/site.json) and builds the page with DOM calls.
   The selected game's line score comes from /api/linescore/{game_id} (the same-origin Worker).
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

  // Line score refresh: every 30 s while live; every 5 min for a game that starts within 15 min.
  const LIVE_MS = 30000;
  const PRE_MS = 300000;
  const PRE_WINDOW_MS = 15 * 60000;
  const FETCH_TIMEOUT_MS = 10000;
  const STOPPED = /^(Postponed|Cancelled|Suspended)/;

  function lastName(name) {
    const parts = String(name || '').trim().split(/\s+/);
    while (parts.length > 1 && /^(jr\.?|sr\.?|ii|iii|iv)$/i.test(parts[parts.length - 1])) parts.pop();
    return parts[parts.length - 1] || '';
  }

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

  function check() {
    const s = document.createElementNS(SVGNS, 'svg');
    s.setAttribute('viewBox', '0 0 16 16');
    s.setAttribute('width', '16');
    s.setAttribute('height', '16');
    s.setAttribute('aria-hidden', 'true');
    s.setAttribute('focusable', 'false');
    const p = document.createElementNS(SVGNS, 'path');
    p.setAttribute('d', 'M3 8.5l3.2 3.2L13 4.5');
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke', '#2E7A4F');
    p.setAttribute('stroke-width', '2.4');
    p.setAttribute('stroke-linecap', 'round');
    p.setAttribute('stroke-linejoin', 'round');
    s.append(p);
    return s;
  }

  // A check mark with words for screen readers, in a fixed-width slot so numbers stay aligned.
  const mark = (yes, words) => h('span', { class: 'ck' }, yes ? [check(), h('span', { class: 'sr' }, ' ' + words)] : null);

  // A board lamp. Decorative: the text next to it carries the meaning.
  const lamp = (on) => h('span', { class: 'lamp' + (on ? ' on' : ''), 'aria-hidden': 'true' });

  // ---------- number and date formatting ----------
  const ok = (x) => typeof x === 'number' && Number.isFinite(x);
  // Whole percent, rounded half up on the decimal value (toPrecision drops binary float noise such as 28.499999999999996).
  const whole = (p) => Math.round(Number((p * 100).toPrecision(12)));
  const wholePct = (r) => (r <= 0 ? '<1%' : r >= 100 ? '>99%' : r + '%');
  const pct = (p) => (ok(p) ? wholePct(whole(p)) : '–');

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
  const num1 = (x) => (ok(x) ? x.toFixed(1) : '–');
  const trim1 = (x) => num1(x).replace(/\.0$/, '');
  const fix2 = (x) => (ok(x) ? x.toFixed(2) : '–');
  const dot3 = (x) => (ok(x) ? x.toFixed(3).replace(/^0\./, '.') : '–');
  const runs = (x) => (ok(x) ? trim1(x) + (x === 1 ? ' run' : ' runs') : '–');

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

  // The line score from the Worker, or null on any failure (the board then stays empty, without an error).
  async function getLineScore(gamePk) {
    if (!/^\d{1,8}$/.test(String(gamePk))) return null;
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), FETCH_TIMEOUT_MS);
    try {
      const r = await fetch('/api/linescore/' + gamePk, { signal: ctl.signal });
      if (!r.ok) return null;
      const ls = await r.json();
      return ls && typeof ls.state === 'string' && Array.isArray(ls.innings) ? ls : null;
    } catch (e) {
      return null;
    } finally {
      clearTimeout(t);
    }
  }

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

  // The total-runs line shown on the board: the .5 line whose over chance is closest to 50%; on a tie, the higher line.
  function ouLine(over) {
    let best = null;
    for (const k of Object.keys(over || {})) {
      const p = over[k];
      if (!k.endsWith('.5') || !ok(p)) continue;
      const d = Number(Math.abs(p - 0.5).toPrecision(12)); // so 0.45 and 0.55 count as the same distance
      if (!best || d < best.d || (d === best.d && Number(k) > Number(best.k))) best = { k, d, over: whole(p) };
    }
    return best;
  }

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
    const indoor = g.roof === 'dome' || (g.weather && g.weather.wind_label === 'Indoors');
    const where = g.park ? g.park + (indoor ? ', indoors.' : '.') : null;
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
      live ? h('p', { class: 'bd-live' }, lamp(true), live) : null];
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

  function board(g, date) {
    const m = g.markets || {};
    const res = finalResults(g);
    const ou = ouLine(m.total_over);

    // Sportsbook layout: the away row shows the over, the home row the under, for the same line; the pair adds to 100.
    function ouCell(side) {
      if (!ou) return plate('–', 'b-plain');
      const over = side === 'away';
      return plate(wholePct(over ? ou.over : 100 - ou.over), 'b-plain', (over ? 'Over ' : 'Under ') + ou.k);
    }

    function row(side) {
      const team = g[side + '_team'];
      const st = g.starters && g.starters[side];
      return h('tr', null,
        h('th', { scope: 'row', class: 'team' },
          h('span', { class: 'nick' }, nick(team)),
          h('span', { class: 'pitch' }, st && st.name ? lastName(st.name) + ' pitching' : 'Starter not announced')),
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

    // The lamp shows only where the result is a known yes or no.
    const nrLit = res && typeof res.run_in_first === 'boolean' ? !res.run_in_first : null;
    const lamps = h('div', { class: 'lamprow' },
      h('p', { class: 'lr-nr' },
        nrLit === null ? null : lamp(nrLit),
        'NRFI ', num(pct(m.no_run_first), btier(m.no_run_first, 'nrfi')),
        nrLit ? h('span', { class: 'sr' }, ' It happened.') : null));

    const top = h('div', { class: 'bd-top' }, topLine(g, date, null));
    // The scroll box stays in place across refreshes, so keyboard focus on it is kept.
    const lsBox = h('div', { class: 'ls-scroll', tabindex: '0', role: 'region', 'aria-label': 'Line score' }, lineScoreTable(g, null));
    const el = h('section', { id: 'today', class: 'board', 'aria-labelledby': 'board-h' },
      h('h2', { id: 'board-h', class: 'sr' }, g.away_team + ' at ' + g.home_team),
      top,
      lsBox,
      h('div', { class: 'model' }, table, lamps));
    el.setLineScore = (ls) => {
      top.replaceChildren(...topLine(g, date, ls).filter(Boolean));
      lsBox.replaceChildren(lineScoreTable(g, ls));
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

  function pitcher(g, side) {
    const p = g.starters && g.starters[side];
    const team = g[side + '_team'];
    if (!p) return h('div', { class: 'pit' }, h('div', { class: 'who' }, nick(team)), h('div', { class: 'nm' }, 'Starter not announced'));
    const res = finalResults(g);
    const threw = res && res.starter_k && ok(res.starter_k[side]) ? res.starter_k[side] : null;
    const ladder = Object.keys(p.k_over || {}).sort((a, b) => Number(a) - Number(b));
    const f = p.first_inning;
    return h('div', { class: 'pit' },
      h('div', { class: 'who' }, nick(team)),
      h('div', { class: 'nm' }, p.name),
      h('p', { class: 'ks' },
        'Median ', h('b', null, trim1(p.median_k)), p.median_k === 1 ? ' strikeout.' : ' strikeouts.',
        threw !== null ? [' Threw ', h('b', null, String(threw)), '.'] : null),
      h('div', { class: 'ladder' }, ladder.map((k) => h('div', { class: 'rung' },
        h('div', { class: 'l' }, k + '+ K'),
        h('div', { class: 'v' }, pcell(p.k_over[k], 'k' + k)),
        mark(threw !== null && threw >= Number(k), 'reached')))),
      f
        ? h('div', { class: 'fi' },
          h('div', { class: 'fi-l' }, 'First inning'),
          h('div', { class: 'stats' },
            stat('NRFI record', (ok(f.scoreless) ? f.scoreless : '–') + '-' + (ok(f.not_scoreless) ? f.not_scoreless : '–')),
            stat('ERA', fix2(f.era)),
            stat('OBP', dot3(f.opp_obp)),
            ok(f.whip) ? stat('WHIP', fix2(f.whip)) : null))
        : h('p', { class: 'fi-none' }, 'First-inning record not shown (fewer than 5 starts).'));
  }

  function lineupTable(g, side) {
    const team = g[side + '_team'];
    const rows = (g.lineups && g.lineups[side]) || [];
    const res = finalResults(g);
    const fin = !!(res && res.batters);
    const byId = {};
    if (fin) for (const r of res.batters[side] || []) byId[r.id] = r;
    const cell = (p, key, yes) => h('td', null, h('span', { class: 'pv' }, pcell(p, key), fin ? mark(yes, 'happened') : null));
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
        cell(r.hit, 'hit', b && b.h >= 1),
        cell(r.tb2, 'tb', b && b.tb >= 2),
        cell(r.hr, 'hr', b && b.hr >= 1),
        cell(r.rbi, 'rbi', b && b.rbi >= 1),
        cell(r.sb, 'sb', b && b.sb >= 1),
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

  // ---------- live line score for the selected game ----------
  // One game at a time. gen changes on every game switch, so a late answer for the old game is dropped.
  function liveScore() {
    let gen = 0;
    let timer = null;
    let due = null;  // the next refresh, kept while the tab is hidden: { my, g, el }
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
      if (d && d.my === gen) load(d.my, d.g, d.el);
    }

    async function load(my, g, el) {
      const ls = await getLineScore(g.game_id);
      if (my !== gen) return;
      if (ls) { last = ls.state; el.setLineScore(ls); }
      const next = nextDelay(g, ls);
      if (next === null) return;
      due = { my, g, el };
      if (!document.hidden) timer = setTimeout(fire, next);
    }

    // Hidden tab: no requests. Visible again: refresh at once, then carry on.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { clearTimeout(timer); timer = null; } else if (due && timer === null) fire();
    });

    return {
      show(g, el) {
        gen += 1;
        clearTimeout(timer);
        timer = null;
        due = null;
        last = null;
        load(gen, g, el);
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
      boardSlot.replaceChildren(b);
      mainCol.replaceChildren(...[conditions(g), totals(g),
        section('pitchers', 'Starting pitchers', 'Chance each starter reaches a strikeout total.',
          h('div', { class: 'pits' }, pitcher(g, 'away'), pitcher(g, 'home'))),
        section('lineups', 'Lineups and player props', 'Chance of at least one hit, two total bases, a homer, an RBI and a steal.',
          lineupTable(g, 'away'), lineupTable(g, 'home')),
      ].filter(Boolean));
      score.show(g, b);
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
