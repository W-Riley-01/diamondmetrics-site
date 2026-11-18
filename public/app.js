/* diamondmetrics: MLB predictions.
   Reads data/page.json (and optionally data/site.json) and builds the page with DOM calls.
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
  const code = (name) => TEAM_CODES[name] || String(name || '?').split(' ').pop().slice(0, 3).toUpperCase();

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
    p.setAttribute('d', 'M3.5 8.5l3 3 6-7');
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke', '#15803D');
    p.setAttribute('stroke-width', '2');
    p.setAttribute('stroke-linecap', 'round');
    p.setAttribute('stroke-linejoin', 'round');
    s.append(p);
    return s;
  }

  // ---------- number and date formatting ----------
  // Whole percent, rounded half up on the decimal value (toPrecision drops binary float noise such as 28.499999999999996).
  const whole = (p) => Math.round(Number((p * 100).toPrecision(12)));

  function pct(p) {
    if (typeof p !== 'number' || !Number.isFinite(p)) return '–';
    const r = whole(p);
    if (r <= 0) return '<1%';
    if (r >= 100) return '>99%';
    return r + '%';
  }

  function tier(p, key) {
    const t = THRESH[key];
    if (!t || typeof p !== 'number' || !Number.isFinite(p)) return 'p-low';
    const r = whole(p);
    if (r >= whole(t[0])) return 'p-strong';
    if (r >= whole(t[1])) return 'p-watch';
    return 'p-low';
  }

  const pcell = (p, key) => h('span', { class: 'p ' + tier(p, key) }, pct(p));
  const num1 = (x) => (typeof x === 'number' ? x.toFixed(1) : '–');
  const dot3 = (x) => (typeof x === 'number' ? x.toFixed(3).replace(/^0\./, '.') : '–');

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

  // ---------- banner ----------
  function renderBanner(site) {
    const el = document.getElementById('banner');
    if (!el || !site || !site.opening_day) return;
    const n = daysUntil(site.opening_day);
    if (n === null || n < 0) return;
    const year = ymd(site.opening_day)[0];
    const label = site.opening_label || longDate(site.opening_day);
    const count = n === 0
      ? h('div', { class: 'count' }, h('span', { class: 'count-t' }, h('b', null, 'Opening Day ' + year + ' is today'), ' · ' + label))
      : h('div', { class: 'count' },
        h('span', { class: 'count-n' }, String(n)),
        h('span', { class: 'count-t' }, h('b', null, n === 1 ? 'day' : 'days'), ' until Opening Day ' + year + ' · ' + label));
    el.replaceChildren(h('div', { class: 'wrap' }, count, site.opening_note ? h('p', { class: 'banner-note' }, site.opening_note) : null));
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

  function pickResult(pick, games) {
    for (const g of games) {
      if (g.status !== 'final' || !g.results || !g.results.batters) continue;
      if (pick.game_id !== undefined && pick.game_id !== null && g.game_id !== pick.game_id) continue;
      const side = g.away_team === pick.team ? 'away' : g.home_team === pick.team ? 'home' : null;
      if (!side) continue;
      const row = ((g.lineups && g.lineups[side]) || []).find((r) => r.name === pick.name);
      if (!row) continue;
      const res = (g.results.batters[side] || []).find((r) => r.id === row.id);
      if (res) return res.h >= 1;
    }
    return null;
  }

  // ---------- cards ----------
  function scoreboard(g) {
    if (g.status === 'final') {
      return h('div', { class: 'score' },
        h('span', { class: 'state' }, 'Final'),
        h('span', { class: 'sc' }, code(g.away_team) + ' ' + g.away_score + ' · ' + code(g.home_team) + ' ' + g.home_score));
    }
    if (g.status === 'live') {
      const half = g.inning_half ? String(g.inning_half).charAt(0).toUpperCase() + String(g.inning_half).slice(1).toLowerCase() : '';
      const where = g.inning ? [half, ordinal(g.inning)].filter(Boolean).join(' ') : '';
      const outs = typeof g.outs === 'number' ? ', ' + g.outs + (g.outs === 1 ? ' out' : ' outs') : '';
      return h('div', { class: 'score live' },
        h('span', { class: 'state' }, h('span', { class: 'dot' }), 'Live' + (where ? ' · ' + where + outs : '')),
        h('span', { class: 'sc' }, code(g.away_team) + ' ' + (g.away_score ?? 0) + ' · ' + code(g.home_team) + ' ' + (g.home_score ?? 0)));
    }
    return null;
  }

  function tile(label, value, act) {
    return h('div', { class: 'tile' },
      h('div', { class: 'lbl' }, label),
      h('div', { class: 'val' }, value),
      act ? h('div', { class: 'act' }, act) : null);
  }

  function gameCard(g) {
    const m = g.markets || {};
    const fin = g.status === 'final';
    const res = g.results || null;
    const A = code(g.away_team);
    const H = code(g.home_team);
    const awayWon = fin && g.away_score > g.home_score;
    const homeWon = fin && g.home_score > g.away_score;
    const total = fin ? (res && typeof res.total_runs === 'number' ? res.total_runs : g.away_score + g.home_score) : null;

    const winAct = (won) => (fin ? (won ? [check(), 'Won'] : 'Lost') : null);
    const nrfiAct = fin && res ? (res.run_in_first ? 'A run scored in the 1st' : [check(), 'No run scored in the 1st']) : null;

    const first = clock(g.start_time_utc);
    const sub = [g.park, first ? 'First pitch ' + first : null].filter(Boolean).join(' · ');

    const kids = [
      h('div', { class: 'gc-head' },
        h('div', null,
          h('h2', { class: 'gc-title' }, g.away_team + ' at ' + g.home_team),
          h('p', { class: 'gc-sub' }, sub)),
        scoreboard(g)),
      h('div', { class: 'tiles' },
        tile(A + ' win', pcell(m.away_win, 'ml'), winAct(awayWon)),
        tile(H + ' win', pcell(m.home_win, 'ml'), winAct(homeWon)),
        tile('Median total runs', num1(m.median_total), fin ? 'Actual: ' + total + (total === 1 ? ' run' : ' runs') : null),
        tile('No run in the 1st', pcell(m.no_run_first, 'nrfi'), nrfiAct)),
    ];

    const lines = Object.keys(m.total_over || {}).filter((k) => k.endsWith('.5')).sort((a, b) => Number(a) - Number(b));
    if (lines.length) {
      kids.push(h('div', { class: 'over' },
        h('div', { class: 'lbl' }, 'Chance the game total goes over'),
        h('div', { class: 'chips' }, lines.map((k) => {
          const went = fin && total > Number(k);
          return h('div', { class: 'chip' },
            h('div', { class: 'l' }, 'Over ' + k),
            h('div', { class: 'v' }, h('span', { class: 'p p-watch' }, pct(m.total_over[k])), went ? [check(), h('span', { class: 'sr' }, ' went over')] : null));
        }))));
    }

    if (typeof m.f5_away === 'number') {
      const led = fin && res && res.led_after_5
        ? ' Actual after five: ' + (res.led_after_5 === 'away' ? A + ' ahead.' : res.led_after_5 === 'home' ? H + ' ahead.' : 'tied.')
        : '';
      kids.push(h('p', { class: 'f5' },
        'First 5 innings: ', A + ' ahead ', pcell(m.f5_away, 'f5'), ' · tied ' + pct(m.f5_tie) + ' · ', H + ' ahead ', pcell(m.f5_home, 'f5'), led));
    }
    return h('section', { class: 'card', 'aria-label': 'Game predictions' }, kids);
  }

  function conditionsCard(g) {
    const w = g.weather;
    const head = h('div', { class: 'cond-head' }, h('h2', null, 'First-pitch conditions'), g.park ? h('span', { class: 'hint' }, g.park) : null);
    if (!w) {
      return h('section', { class: 'card' }, head, h('p', { class: 'cond-note' }, 'The forecast for this park was not available when the prediction ran.'));
    }
    const indoor = g.roof === 'dome' || w.wind_label === 'Indoors';
    const kids = [head];
    if (indoor) {
      kids.push(h('p', { class: 'cond-note' }, h('b', null, 'Dome. '), 'The model treats ' + (g.park || 'this park') + ' as indoors, so weather is not a factor.'));
    } else {
      const toCF = w.wind_label || '–';
      const wind = !w.wind_mph ? 'Calm' : Math.round(w.wind_mph) + ' mph' + (w.wind_from && w.wind_from !== '—' ? ' from ' + w.wind_from : '');
      const cr = typeof w.carry_ft === 'number' ? Math.round(w.carry_ft) : null;
      const carry = cr === null ? '–' : (cr > 0 ? '+' : cr < 0 ? '−' : '') + Math.abs(cr) + ' ft';
      const cell = (k, v) => h('div', null, h('div', { class: 'k' }, k), h('div', { class: 'v' }, v));
      const ok = (x) => typeof x === 'number' && Number.isFinite(x);
      kids.push(h('div', { class: 'wx' },
        cell('Temperature', ok(w.temp_f) ? Math.round(w.temp_f) + '°F' : '–'),
        cell('Humidity', ok(w.humidity_pct) ? Math.round(w.humidity_pct) + '%' : '–'),
        cell('Pressure', ok(w.pressure_inhg) ? w.pressure_inhg.toFixed(2) + ' inHg' : '–'),
        cell('Wind', wind),
        cell('Toward center field', toCF),
        cell('Carry change', carry)));
    }
    if (typeof w.hr_score === 'number') {
      const score = Math.max(0, Math.min(100, w.hr_score));
      const fill = h('div', { class: 'fill' + (score >= 55 ? ' hot' : '') });
      fill.style.width = score + '%';
      kids.push(h('div', { class: 'hrbox' },
        h('div', { class: 'hrrow' },
          h('span', null, 'Home run conditions: ', h('b', null, w.hr_label || '')),
          h('span', null, h('span', { class: 'hrnum' }, String(Math.round(score))), ' out of 100')),
        h('div', { class: 'track', 'aria-hidden': 'true' }, fill)));
    }
    return h('section', { class: 'card' }, kids);
  }

  function pitcherCard(g, side) {
    const p = g.starters && g.starters[side];
    const team = side === 'away' ? g.away_team : g.home_team;
    if (!p) return h('div', { class: 'pit' }, h('div', { class: 'who' }, code(team) + ' starter'), h('div', { class: 'nm' }, 'Not announced'));
    const fin = g.status === 'final' && g.results && g.results.starter_k;
    const actual = fin ? g.results.starter_k[side] : null;
    const ladder = Object.keys(p.k_over || {}).sort((a, b) => Number(a) - Number(b));
    const f = p.first_inning;
    return h('div', { class: 'pit' },
      h('div', { class: 'who' }, code(team) + ' starter'),
      h('div', { class: 'nm' }, p.name),
      h('div', { class: 'ks' },
        h('span', null, 'Median strikeouts ', h('b', null, num1(p.median_k).replace(/\.0$/, ''))),
        typeof actual === 'number' ? h('span', null, 'Actual ', h('b', null, String(actual))) : null),
      h('div', { class: 'chips' }, ladder.map((k) => h('div', { class: 'chip' },
        h('div', { class: 'l' }, k + '+ K'),
        h('div', { class: 'v' }, pcell(p.k_over[k], 'k' + k), typeof actual === 'number' && actual >= Number(k) ? [check(), h('span', { class: 'sr' }, ' reached')] : null)))),
      h('p', { class: 'first' }, f
        ? 'First inning, ' + f.starts + ' starts: ERA ' + f.era.toFixed(2) + ' · scoreless in ' + f.scoreless + ' of ' + f.starts + ' · opponent OBP ' + dot3(f.opp_obp)
        : 'First-inning record not shown (fewer than 5 starts).'));
  }

  function pitchersCard(g) {
    return h('section', { class: 'card' },
      h('h2', null, 'Starting pitchers'),
      h('div', { class: 'pits' }, pitcherCard(g, 'away'), pitcherCard(g, 'home')));
  }

  function lineupTable(g, side) {
    const team = side === 'away' ? g.away_team : g.home_team;
    const rows = (g.lineups && g.lineups[side]) || [];
    const fin = g.status === 'final' && g.results && g.results.batters;
    const byId = {};
    if (fin) for (const r of g.results.batters[side] || []) byId[r.id] = r;
    const thead = h('thead', null, h('tr', null,
      h('th', { class: 'o', scope: 'col' }, '#'),
      h('th', { class: 'l', scope: 'col' }, 'Player'),
      ['1+ H', '2+ TB', '1+ HR', '1+ RBI', '1+ SB'].map((c) => h('th', { scope: 'col' }, c)),
      fin ? h('th', { class: 'o', scope: 'col' }, 'Result') : null));
    const tbody = h('tbody', null, rows.map((r) => {
      const res = byId[r.id];
      return h('tr', null,
        h('td', { class: 'o' }, String(r.slot)),
        h('td', { class: 'n' }, r.name),
        h('td', null, pcell(r.hit, 'hit')),
        h('td', null, pcell(r.tb2, 'tb')),
        h('td', null, pcell(r.hr, 'hr')),
        h('td', null, pcell(r.rbi, 'rbi')),
        h('td', null, pcell(r.sb, 'sb')),
        fin ? h('td', { class: 'rs' }, res ? res.line : '–') : null);
    }));
    const table = h('table', { class: 't' + (fin ? ' fin' : '') }, h('caption', { class: 'sr' }, team + ' batting order'), thead, tbody);
    return h('div', { class: 'lu' },
      h('h3', null, team + ' batting order'),
      h('div', { class: 'scroll', tabindex: '0', role: 'region', 'aria-label': team + ' batting order table' }, table));
  }

  function lineupsCard(g) {
    return h('section', { class: 'card' },
      h('h2', null, 'Lineups and player props'),
      h('p', { class: 'hint' }, 'Chance of at least one hit, two or more total bases, a home run, an RBI and a stolen base.'),
      h('div', { class: 'lus' }, lineupTable(g, 'away'), lineupTable(g, 'home')));
  }

  // ---------- reading guide ----------
  function readingGuide() {
    const rows = [
      ['Win chance', 'ml'], ['Ahead after 5 innings', 'f5'], ['No run in the 1st', 'nrfi'],
      ['Starter 3+ strikeouts', 'k3'], ['Starter 4+ strikeouts', 'k4'], ['Starter 5+ strikeouts', 'k5'],
      ['Starter 6+ strikeouts', 'k6'], ['Starter 7+ strikeouts', 'k7'],
      ['Batter 1+ hit', 'hit'], ['Batter 2+ total bases', 'tb'], ['Batter 1+ home run', 'hr'],
      ['Batter 1+ RBI', 'rbi'], ['Batter 1+ stolen base', 'sb'],
    ];
    const cut = (x) => whole(x) + '%';
    return h('details', { class: 'how' },
      h('summary', null, 'How to read the numbers'),
      h('p', null, 'Every number is a probability from simulating the game many times. Blue cells are strong, dark cells are worth watching, and grey cells are below that. The cut-offs are below.'),
      h('table', null,
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Market'), h('th', { scope: 'col', class: 'r' }, 'Strong'), h('th', { scope: 'col', class: 'r' }, 'Watch'))),
        h('tbody', null, rows.map(([label, key]) => h('tr', null,
          h('td', null, label),
          h('td', { class: 'r' }, cut(THRESH[key][0]) + '+'),
          h('td', { class: 'r' }, cut(THRESH[key][1]) + '+'))))),
      h('p', null, 'Home run conditions score, 0 to 100: 65 and up Very HR Friendly, 55 to 64 HR Friendly, 45 to 54 Neutral, 35 to 44 HR Suppressed, under 35 Very HR Suppressed. One point is about one foot of expected carry change. Indoor parks are scored a neutral 50.'));
  }

  // ---------- page ----------
  function renderPage(app, page, site) {
    const games = page.games;
    const picks = Array.isArray(page.beat_the_streak) ? page.beat_the_streak.slice(0, 5) : [];
    const replay = page.note === 'replay';
    let sel = games.length ? games[0].game_id : null;

    const detail = h('div', { class: 'detail' });
    const list = h('div', { class: 'list' });

    function renderDetail() {
      const g = games.find((x) => x.game_id === sel);
      detail.replaceChildren(...(g ? [gameCard(g), conditionsCard(g), pitchersCard(g), lineupsCard(g)] : []));
    }

    function select(id) {
      sel = id;
      for (const b of list.children) b.setAttribute('aria-pressed', String(b.getAttribute('data-id') === String(id)));
      renderDetail();
    }

    for (const g of games) {
      const m = g.markets || {};
      list.append(h('button', {
        type: 'button', class: 'game-btn', 'data-id': g.game_id, 'aria-pressed': String(g.game_id === sel),
        onclick: () => select(g.game_id),
      },
      h('span', { class: 'r1' }, h('span', null, code(g.away_team) + ' @ ' + code(g.home_team)), h('span', { class: 'st' }, statusText(g))),
      h('span', { class: 'r2' }, 'Win ' + code(g.away_team) + ' ' + pct(m.away_win) + ', ' + code(g.home_team) + ' ' + pct(m.home_win) + ' · Total ' + num1(m.median_total))));
    }

    const headline = replay && site.replay_headline ? site.replay_headline : longDate(page.date);
    const hint = [replay && site.replay_headline ? longDate(page.date) : null, page.updated_at ? 'Updated ' + stamp(page.updated_at) : null].filter(Boolean).join(' · ');

    const btsList = picks.length
      ? h('ol', { class: 'bts' }, picks.map((p, i) => {
        const r = pickResult(p, games);
        return h('li', null,
          h('div', null,
            h('div', { class: 'meta' }, '#' + (i + 1) + ' · ' + code(p.team) + (p.home ? ' vs ' : ' @ ') + code(p.opponent)),
            h('div', { class: 'name' }, p.name),
            r === null ? null : h('div', { class: 'res ' + (r ? 'yes' : 'no') }, r ? 'Got a hit' : 'No hit')),
          h('div', { class: 'pv' }, pct(p.hit)));
      }))
      : h('p', { class: 'msg' }, 'Picks appear once lineups are posted.');

    const gamesBlock = games.length
      ? h('div', { class: 'cols' },
        h('aside', { class: 'side', 'aria-label': 'Games' }, h('h2', null, games.length === 1 ? '1 game' : games.length + ' games'), list, readingGuide()),
        detail)
      : h('p', { class: 'msg' }, 'No games are scheduled for this date.');

    app.replaceChildren(
      h('h1', null, 'MLB predictions'),
      h('p', { class: 'sub' }, headline),
      hint ? h('p', { class: 'hint' }, hint) : null,
      replay && page.label ? h('p', { class: 'notice' }, page.label) : null,
      h('section', { id: 'bts', 'aria-labelledby': 'bts-h' },
        h('div', { class: 'section-head' }, h('h2', { id: 'bts-h' }, 'Beat the Streak: top 5'), h('span', { class: 'hint' }, 'Highest chance of at least one hit')),
        btsList),
      gamesBlock);
    renderDetail();
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
