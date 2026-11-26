"""Browser tests for Diamond Metrics (run: python tests/test_site.py; needs `pip install playwright` and `playwright install chromium`).
Expected values are computed here from page.json and the fixtures with separate code (Decimal rounding, not the page's JS),
then compared with what the browser actually shows. The test server (tests/serve.py) mimics the site's routing but has no
Worker, so /api/* is answered with page.route from tests/fixtures/. tests/test_routes.py checks the real routing with
wrangler dev. Full-page screenshots go to tests/screens/ (ignored by git)."""
import copy, json, os, shutil, socket, subprocess, sys, tempfile, time
from decimal import Decimal, ROUND_HALF_UP
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.abspath(os.environ.get("SITE_DIR") or os.path.join(HERE, "..", "public"))
SCREENS = os.path.join(HERE, "screens")
FIXTURES = os.path.join(HERE, "fixtures")
PORT_MAIN, PORT_SEASON, PORT_ERR = 8765, 8766, 8767
RESULTS = []

YELLOW, GREEN = "rgb(242, 193, 46)", "rgb(31, 74, 56)"
WHITE, HIT, MISS, UNLIT = "rgb(244, 245, 240)", "rgb(124, 240, 166)", "rgb(255, 107, 94)", "rgb(22, 54, 40)"
DIM = "rgb(147, 171, 158)"
ICON_LABEL = {"hit": "got a hit", "tb": "two or more bases", "hr": "homered", "rbi": "drove in a run", "sb": "stole a base"}
BTS_SENTENCE = "Beat the Streak is MLB's free contest: pick a batter to get a hit on the days you play, and 57 correct picks in a row win the grand prize."


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  -> " + str(detail)))


# ---------- independent expectations ----------
def HU(d, q="1"):
    return Decimal(d).quantize(Decimal(q), rounding=ROUND_HALF_UP)


def W(p):
    return int(HU(Decimal(str(p)) * 100))


def WP(r):
    return "<1%" if r <= 0 else ">99%" if r >= 100 else f"{r}%"


def PCT(p):
    return WP(W(p))


# thresholds copied from the original app.py (PROP_THRESHOLDS, K_PROP_THRESHOLDS, GAME_THRESHOLDS)
TH = {"hit": (70, 60), "tb": (45, 35), "hr": (18, 12), "rbi": (35, 25), "sb": (15, 8),
      "k3": (70, 55), "k4": (60, 45), "k5": (50, 35), "k6": (35, 22), "k7": (22, 12),
      "ml": (58, 50), "f5": (55, 48), "nrfi": (58, 50)}


def TIER(p, key):
    r = W(p)
    hi, mid = TH[key]
    return "p-strong" if r >= hi else "p-watch" if r >= mid else "p-low"


def BT(p, key):
    return "b-" + TIER(p, key)[2:]


def RUNS(x):
    d = Decimal(str(x))
    s = str(int(d)) if d == d.to_integral_value() else str(d.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return s + (" run" if d == 1 else " runs")


def OU(total_over):
    """Board total: the .5 line whose over chance is closest to 0.5 (ties go to the higher line), as
    [label over, label under], [over shown, under shown]; the pair adds to 100."""
    cands = [(abs(Decimal(str(p)) - Decimal("0.5")), -Decimal(k), k, p) for k, p in total_over.items() if k.endswith(".5")]
    _, _, k, p = min(cands)
    over = W(p)
    return [f"Over {k}", f"Under {k}"], [WP(over), WP(100 - over)]


def PICKS(g):
    """Expected result lamps, one per board market: (visible label, lamp) where lamp is None (no lamp), 'unlit', 'hit' or 'miss'.
    The pick is the likelier side on unrounded values; exactly even means no pick. Results come from page.json only."""
    D, half = (lambda x: Decimal(str(x))), Decimal("0.5")
    m = g["markets"]
    fin = g["status"] == "final"
    res = g.get("results") if fin else None
    got = lambda k: res.get(k) if res else None

    def lamp(pick, hit):
        if pick is None:
            return None
        if not fin:
            return "unlit"
        return None if hit is None else ("hit" if hit else "miss")

    out = []
    aw, hw = D(m["away_win"]), D(m["home_win"])
    pick = None if aw == hw or half in (aw, hw) else ("away" if aw > hw else "home")
    a, h = got("away_score"), got("home_score")
    hit = (("away" if a > h else "home") == pick) if pick and a is not None and h is not None and a != h else None
    out.append((f"Winner {NICK[g[pick + '_team']] if pick else 'even'}", lamp(pick, hit)))

    f5 = {"away": D(m["f5_away"]), "home": D(m["f5_home"]), "tie": D(m["f5_tie"])}
    leaders = [k for k, v in f5.items() if v == max(f5.values())]
    pick = leaders[0] if len(leaders) == 1 else None
    led = got("led_after_5")
    hit = (led == pick) if pick and led in ("away", "home", "tie") else None
    out.append((f"First 5 {'Tie' if pick == 'tie' else NICK[g[pick + '_team']] if pick else 'even'}", lamp(pick, hit)))

    _, _, k, p = min((abs(D(v) - half), -D(kk), kk, v) for kk, v in m["total_over"].items() if kk.endswith(".5"))
    p = D(p)
    pick = "over" if p > half else "under" if p < half else None
    t = got("total_runs")
    hit = ((Decimal(t) > D(k)) == (pick == "over")) if pick and t is not None else None
    out.append((f"Total {'Over' if pick == 'over' else 'Under'} {k}" if pick else f"Total {k} even", lamp(pick, hit)))

    p = D(m["no_run_first"])
    pick = "nrfi" if p > half else "yrfi" if p < half else None
    rif = got("run_in_first")
    hit = ((not rif) if pick == "nrfi" else rif) if pick and isinstance(rif, bool) else None
    out.append((f"YRFI {WP(100 - W(p))}" if pick == "yrfi" else f"NRFI {PCT(p)}", lamp(pick, hit)))
    return out


# The lamp rows: [text without the lamp and screen-reader words, lamp state or None, screen-reader words] per item.
LAMPS_JS = """s => [...document.querySelectorAll(s)].map(li => {
    const l = li.querySelector('.lamp'), sr = li.querySelector('.sr');
    const text = [...li.childNodes].filter(n => !(n.classList && (n.classList.contains('lamp') || n.classList.contains('sr'))))
      .map(n => n.textContent).join('').replace(/\\s+/g, ' ').trim();
    const state = !l ? null : l.classList.contains('hit') ? 'hit' : l.classList.contains('miss') ? 'miss' : l.classList.contains('on') ? 'on' : 'unlit';
    return [text, state, sr ? sr.textContent : ''];
})"""


def lamps(pg, sel=".board .lr li"):
    return [tuple(x) for x in pg.evaluate(LAMPS_JS, sel)]


def lamps_expect(g):
    words = {"hit": " Model's pick came true.", "miss": " Model's pick missed."}
    return [(t, s, words.get(s, "")) for t, s in PICKS(g)]


def DEW(temp_f, rh):
    """Dew point in whole °F by Magnus with the engine's constants (7.5, 237.3), all in Decimal."""
    t = (Decimal(str(temp_f)) - 32) * 5 / 9
    g = (Decimal(str(rh)) / 100).log10() + Decimal("7.5") * t / (t + Decimal("237.3"))
    td = Decimal("237.3") * g / (Decimal("7.5") - g)
    return int(HU(td * 9 / 5 + 32))


# elevation_m from STADIUM_INFO in MLB_Analysis/weather.py, for the parks the tests use
ELEV_M = {"BOS": 6, "TB": 13, "SEA": 6}


def FT(m):
    return int(HU(Decimal(m) * Decimal("3.28084") / 10)) * 10


def DOT3(x):
    return str(HU(Decimal(str(x)), "0.001"))[1:]


def FIX2(x):
    return str(HU(Decimal(str(x)), "0.01"))


STOPPED = ("Postponed", "Cancelled", "Suspended")


def LS(fx, away_code, home_code):
    """Expected line score rows from a fixture: [code, [inning plates], [R, H, E]] per team.
    Empty for innings not played; X on the home team's last inning of a final it didn't need to bat in."""
    if fx is None:
        return [[c, [""] * 9, ["–"] * 3] for c in (away_code, home_code)]
    inns = {i["n"]: i for i in fx["innings"]}
    n = max([9, len(fx["innings"]), fx.get("inning") or 0])
    stopped = fx["detail"].startswith(STOPPED)
    last = fx["innings"][-1]["n"] if fx["innings"] else None
    rows = []
    for side, c in (("away", away_code), ("home", home_code)):
        cells = []
        for i in range(1, n + 1):
            v = inns.get(i, {}).get(side)
            t = "" if v is None else str(v)
            if fx["state"] == "final" and not stopped and side == "home" and i == last and last >= 9 and v is None:
                t = "X"
            cells.append(t)
        rows.append([c, cells, ["–" if fx[side][k] is None else str(fx[side][k]) for k in ("r", "h", "e")]])
    return rows


def fixture(name):
    return json.load(open(os.path.join(FIXTURES, name), encoding="utf-8"))


CODE = {"Milwaukee Brewers": "MIL", "Tampa Bay Rays": "TB", "New York Yankees": "NYY", "Boston Red Sox": "BOS",
        "Athletics": "ATH", "Seattle Mariners": "SEA"}
NICK = {"Milwaukee Brewers": "Brewers", "Tampa Bay Rays": "Rays", "New York Yankees": "Yankees", "Boston Red Sox": "Red Sox",
        "Athletics": "Athletics", "Seattle Mariners": "Mariners", "Detroit Tigers": "Tigers", "Baltimore Orioles": "Orioles",
        "Minnesota Twins": "Twins"}
KEY_LINE = "Yellow numbers on a green plate clear the strong cut-off, plain numbers clear the watch line, and grey numbers fall below it."


def norm(s):
    return " ".join(s.split())


class Server:
    """The test server. On Windows a venv's python.exe is a launcher that starts the real interpreter as a child, so
    stopping it means stopping the whole process tree; otherwise an orphan keeps the port and serves an old folder."""

    def __init__(self, proc):
        self.proc = proc

    def terminate(self):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self.proc.terminate()
        self.proc.wait(timeout=10)


def start_server(root, port):
    with socket.socket() as s:  # a server already on this port would answer for the wrong folder
        if s.connect_ex(("127.0.0.1", port)) == 0:
            sys.exit(f"port {port} is already in use (a test server left over from an earlier run?); stop it and run again")
    cmd = [sys.executable, os.path.join(HERE, "serve.py"), root, "--port", str(port)]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    return Server(p)


def new_ctx(browser, w=1280, h=900, tz="America/New_York", **kw):
    return browser.new_context(viewport={"width": w, "height": h}, timezone_id=tz, locale="en-US", device_scale_factor=1, **kw)


WS5_API = {"linescore/849799": "linescore-849799.json", "boxscore/849799": "boxscore-849799.json", "series/849799": "series-849799.json",
           "standings/849799": "standings-849799.json", "battery/849799": "battery-849799.json", "slate/2026-10-28": "slate-2026-10-28.json"}


class Api:
    """Answers /api/{endpoint}/{key} from a table keyed "endpoint/key" (a bare gamePk means the line score): a fixture
    name, an HTTP status (int), or "hang" (never answers). Anything not in the table is a 404. The table can be changed
    while the page is open. Every request is counted: `calls` lists the line-score requests, `every` all of them as
    (endpoint, key)."""

    def __init__(self, table):
        self.table = {(k if "/" in k else "linescore/" + k): v for k, v in table.items()}
        self.calls = []
        self.every = []

    def count(self, pk, ep="linescore"):
        return self.every.count((ep, str(pk)))

    def handle(self, route):
        ep, pk = route.request.url.split("?")[0].rstrip("/").rsplit("/", 2)[-2:]
        self.every.append((ep, pk))
        if ep == "linescore":
            self.calls.append(pk)
        spec = self.table.get(ep + "/" + pk, 404)
        if spec == "hang":
            return  # never fulfilled: the page's own timeout has to give up
        if isinstance(spec, int):
            route.fulfill(status=spec, content_type="application/json; charset=utf-8", body=json.dumps({"error": str(spec)}))
        else:
            route.fulfill(status=200, content_type="application/json; charset=utf-8", body=open(os.path.join(FIXTURES, spec), encoding="utf-8").read())


def open_page(ctx, url, fixed="2026-11-18T17:00:00Z", api=None, install=False):
    """Open a page with the clock pinned (fixed time) or installed (time moves only when the test says so)."""
    pg = ctx.new_page()
    log = {"console": [], "errors": [], "requests": [], "responses": []}
    pg.on("console", lambda m: log["console"].append((m.type, m.text)))
    pg.on("pageerror", lambda e: log["errors"].append(str(e)))
    pg.on("request", lambda r: log["requests"].append(r.url))
    pg.on("response", lambda r: log["responses"].append((r.url, r.status, r.headers)))
    api = api or Api(WS5_API)
    pg.route("**/api/*/*", api.handle)
    if install:
        pg.clock.install(time=fixed)
    else:
        pg.clock.set_fixed_time(fixed)
    pg.goto(url)
    pg.api = api
    return pg, log


def settle(pg, ms=250):
    pg.wait_for_timeout(ms)  # real time, so route handlers and fetch promises finish


def adv(pg, ms):
    pg.clock.run_for(ms)
    settle(pg)


def set_hidden(pg, hidden):
    pg.evaluate("""h => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => h });
        Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (h ? 'hidden' : 'visible') });
        document.dispatchEvent(new Event('visibilitychange')); }""", hidden)
    settle(pg)


def tc(pg, sel):
    """textContent of every match, whitespace collapsed (includes screen-reader-only text)."""
    return [norm(x) for x in pg.locator(sel).all_text_contents()]


def classes(pg, sel):
    return pg.eval_on_selector_all(sel, "els => els.map(e => e.className)")


def css(pg, sel, prop):
    return pg.evaluate("([s, p]) => getComputedStyle(document.querySelector(s))[p]", [sel, prop])


def no_hscroll(pg):
    return pg.evaluate("document.documentElement.scrollWidth <= innerWidth")


def ls_rows(pg):
    """The line score as shown: [code, [inning plates], [R, H, E]] per team (visible text only)."""
    return pg.evaluate("""() => [...document.querySelectorAll('.ls tbody tr')].map(tr => [
        tr.querySelector('th').textContent.trim(),
        [...tr.querySelectorAll('td:not(.rhe) .ip')].map(p => p.firstChild && p.firstChild.nodeType === 3 ? p.firstChild.textContent : ''),
        [...tr.querySelectorAll('td.rhe .ip')].map(p => p.textContent)])""")


def ls_head(pg):
    return pg.evaluate("[...document.querySelectorAll('.ls thead th')].map(th => (th.querySelector('[aria-hidden]') || th).textContent.trim())")


def focus_outline(pg, sel):
    """Keyboard-style focus on sel, then its computed outline (style, width, colour)."""
    pg.keyboard.press("Tab")
    pg.focus(sel)
    return pg.evaluate("s => { const c = getComputedStyle(document.querySelector(s)); return [c.outlineStyle, c.outlineWidth, c.outlineColor]; }", sel)


def screenshot(pg, name):
    os.makedirs(SCREENS, exist_ok=True)
    pg.screenshot(path=os.path.join(SCREENS, name), full_page=True)


def lineup_expect(g, side, by_id):
    """Expected text of each row, the result icons' labels in order, and the icon count, from the box score."""
    rows, icons, n = [], [], 0
    for row in g["lineups"][side]:
        b = by_id.get(row["id"])
        cells, labels = [str(row["slot"]), row["name"]], []
        for k, rk, need, kind in (("hit", "h", 1, "hit"), ("tb2", "tb", 2, "tb"), ("hr", "hr", 1, "hr"), ("rbi", "rbi", 1, "rbi"), ("sb", "sb", 1, "sb")):
            yes = b is not None and b[rk] >= need
            n += yes
            cells.append(PCT(row[k]))
            if yes:
                labels.append(ICON_LABEL[kind])
        cells.append(b["line"] if b else "–")
        rows.append(cells)
        icons.append(labels)
    return rows, icons, n


def fi_expect(fi):
    out = [f"NRFI record{fi['scoreless']}-{fi['not_scoreless']}", f"ERA{FIX2(fi['era'])}", f"OBP{DOT3(fi['opp_obp'])}"]
    if fi.get("whip") is not None:
        out.append(f"WHIP{FIX2(fi['whip'])}")
    return out


def ROW(pick, g):
    """The pick's box-score row in game g once it is final (found by team and name), else None."""
    if g["status"] != "final" or not g.get("results"):
        return None
    side = "away" if g["away_team"] == pick["team"] else "home" if g["home_team"] == pick["team"] else None
    if not side:
        return None
    ids = [r["id"] for r in g["lineups"][side] if r["name"] == pick["name"]]
    rows = [b for b in g["results"]["batters"][side] if ids and b["id"] == ids[0]]
    return rows[0] if rows else None


def PICK_GAME(pick, games):
    """The pick's game: by game_id if given, else the team's game (the final one he played in, on a doubleheader)."""
    if pick.get("game_id") is not None:
        return next((g for g in games if g["game_id"] == pick["game_id"]), None)
    mine = [g for g in games if pick["team"] in (g["away_team"], g["home_team"])]
    return next((g for g in mine if ROW(pick, g)), mine[0] if mine else None)


def pick_line(pick, games):
    g = PICK_GAME(pick, games)
    r = ROW(pick, g) if g else None
    return r["line"] if r else None


SWEEP = sorted(set(range(360, 1441, 40)) | {853})

# Every scroll wrapper: [class, scrollWidth, clientWidth, scrollHeight, clientHeight, the table's natural (unwrapped) width].
WRAPPERS = """() => [...document.querySelectorAll('.ls-scroll, .scroll')].map(s => {
    const t = s.querySelector('table'), keep = t.style.width;
    t.style.width = 'min-content';
    const natural = Math.ceil(t.getBoundingClientRect().width);
    t.style.width = keep;
    return [s.className, s.scrollWidth, s.clientWidth, s.scrollHeight, s.clientHeight, natural]; })"""


def sweep(pg, label):
    """At every width from 360 to 1440: no horizontal page scroll, no wrapper scrolls vertically, and none scrolls
    sideways when its table fits."""
    for w in SWEEP:
        pg.set_viewport_size({"width": w, "height": 900})
        settle(pg, 120)
        rows = pg.evaluate(WRAPPERS)
        bad = [r for r in rows if r[3] > r[4] or (r[5] <= r[2] and r[1] > r[2])]
        check(f"{label} {w}: no horizontal page scroll, and no scroll wrapper overflows unless its table can't fit",
              not bad and no_hscroll(pg), bad or pg.evaluate("document.documentElement.scrollWidth"))


DIRT, CHALK, TRACK, POLE_SHADOW = "rgb(217, 195, 160)", "rgb(251, 248, 241)", "rgb(232, 217, 190)", "rgb(242, 193, 46) 4px 0px 0px 0px inset, rgb(242, 193, 46) -4px 0px 0px 0px inset"
MUTED, OK_GREEN = "rgb(74, 61, 46)", "rgb(26, 94, 55)"
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def DRANGE(a, b):
    (_, m1, d1), (_, m2, d2) = (map(int, a.split("-")), map(int, b.split("-")))
    if (m1, d1) == (m2, d2):
        return f"{MONTHS[m1 - 1]} {d1}"
    return f"{MONTHS[m1 - 1]} {d1}–" + ("" if m1 == m2 else MONTHS[m2 - 1] + " ") + str(d2)


def SERIES_LINES(g, s):
    """Expected text of the season series strip, from a /api/series answer."""
    r, other = s["regular"], {"away": "home", "home": "away"}
    T = lambda side: NICK[g[side + "_team"]]
    w = {"away": r["awayWins"], "home": r["homeWins"]}
    if r["played"] == 0:
        main = "first meeting"
    elif w["away"] == w["home"]:
        main = f"tied {w['away']}–{w['home']}"
    else:
        lead = "away" if w["away"] > w["home"] else "home"
        main = f"{T(lead)} {w[lead]}–{w[other[lead]]}"
    out = [f"Season series: {main}"]
    out += [f"{T(sw['winner'])} swept a {sw['games']}-game series, {DRANGE(sw['startDate'], sw['endDate'])}." for sw in r["sweeps"]]
    if r["seasonSweep"]:
        W = r["seasonSweep"]
        out.append(f"{T(W)} swept the season series {w[W]}–0." if r["left"] == 0 else
                   f"{T(other[W])} haven't beaten the {T(W)} this season (0–{w[W]}, {r['left']} game{'' if r['left'] == 1 else 's'} left).")
    ps = s["postseason"]
    if ps:
        res = ps["result"]
        score = f"{res['wins']}–{res['losses']}"
        out.append(f"{ps['description']}: " + (f"{T(res['leader'])} {'win' if res['over'] else 'lead'} {score}." if res["leader"] else f"tied {score}."))
    return out


def plain(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").strip().lower()


def BATTERY(g, side, box, state):
    """Expected battery for one team: [SP line and C line (on the board, under the team name),
    'Started:' note or None and the starter's line stat blocks or None (in the starting pitchers section)]."""
    p = g["starters"][side]
    t = box[side] if box else None
    sp = f"SP {p['name']}" if p else "SP not announced"
    c = f"C {t['catcher']['name']}" if t and t["catcher"] else "C not announced"
    started, line = None, None
    if t and t["pitcher"] and t["pitcher"]["line"]["ip"] is not None and state in ("live", "final"):
        L = t["pitcher"]["line"]
        line = [f"IP{L['ip']}", f"H{L['h']}", f"BB{L['bb']}", f"SO{L['so']}"]
        if not p or plain(t["pitcher"]["name"]) != plain(p["name"]):
            started = f"Started: {t['pitcher']['name']}"
    return [sp, c, started, line]


BATTERY_JS = """() => {
    const teams = [...document.querySelectorAll('.bd th.team')], pits = [...document.querySelectorAll('.pit')];
    const t = (root, s) => { const e = root && root.querySelector(s); return e ? e.textContent.replace(/\\s+/g, ' ').trim() : null; };
    return [0, 1].map(i => {
        const line = pits[i] && pits[i].querySelector('.pline');
        return [t(teams[i], '.bl-sp'), t(teams[i], '.bl-c'), t(pits[i], '.started'),
                line ? [...line.querySelectorAll('.stats > div')].map(d => d.textContent.trim()) : null];
    });
}"""


def battery(pg):
    return pg.evaluate(BATTERY_JS)


def ORD(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def GAMES(n):
    s = str(int(n)) if float(n).is_integer() else str(n)
    return s + (" game" if n == 1 else " games")


def STAND(t):
    """Expected [record, place line, division-record line] for one team from /api/standings (None where hidden)."""
    if not t:
        return [None, None, None]
    s = ("Tied for " if t["tied"] else "") + ORD(t["place"]) + " in the " + t["division"]
    if t["place"] == 1 and not t["tied"] and t["lead"]:
        s += f", {GAMES(t['lead'])} up"
    elif t["place"] > 1:
        s += f", {GAMES(t['gamesBack'])} back"
    dr = t["divisionRecord"]
    return [f"{t['wins']}–{t['losses']}", s + ".", f"{dr['w']}–{dr['l']} in the division." if dr else None]


def BAT_LINE(tg):
    if tg is None:
        return None
    return "First start together this season" if tg["w"] + tg["l"] == 0 else f"Battery {tg['w']}–{tg['l']} this season"


def RUN(p, kind):
    """Expected stolen-base row: the name, then the numbers."""
    if kind == "pitcher":
        s = f"{p['sb']} stolen, {p['cs']} caught, {p['pickoffs']} {'pickoff' if p['pickoffs'] == 1 else 'pickoffs'}"
    elif p["sb"] + p["cs"] == 0:
        s = "no attempts"
    else:
        s = f"{p['sb']} stolen, {p['cs']} caught, {int(HU(Decimal(p['cs']) * 100 / Decimal(p['sb'] + p['cs'])))}%"
    return f"{p['name']} {s}" + (f" ({p['teams']} teams)" if p["teams"] > 1 else "")


def EXTRAS(s, b):
    """Expected board extras per team: [record, place line, division-record line, battery line] (None where hidden)."""
    return [STAND(s[side] if s else None) + [BAT_LINE(b[side]["together"]) if b else None] for side in ("away", "home")]


def RUNNING(b):
    """Expected stolen-base rows per team (None when the block is hidden)."""
    out = []
    for side in ("away", "home"):
        rows = [RUN(b[side][k], k) for k in ("pitcher", "catcher") if b and b[side][k]] if b else []
        out.append(rows or None)
    return out


EXTRAS_JS = """() => [...document.querySelectorAll('.bd th.team')].map(th => [
    ...['.rec', '.bl-st', '.bl-dr', '.bl-bat'].map(s => { const e = th.querySelector(s); return !e || e.hidden ? null : e.textContent.replace(/\\s+/g, ' ').trim(); })])"""
RUNNING_JS = """() => [...document.querySelectorAll('.pit')].map(p => { const r = p.querySelector('.rg');
    return !r || r.hidden ? null : [...r.querySelectorAll('.rg-row')].map(x => x.textContent.replace(/\\s+/g, ' ').trim()); })"""


def extras(pg):
    return pg.evaluate(EXTRAS_JS)


# The K marks under the strikeout ladder: per pitcher block, per rung, [visible K or '', screen-reader text or ''].
KMARKS_JS = """() => [...document.querySelectorAll('.pit')].map(p => [...p.querySelectorAll('.rung')].map(r => {
    const k = r.querySelector('.kmark'), s = r.querySelector('.ck .sr');
    return [k ? k.textContent : '', s ? s.textContent : ''];
}))"""


def kmarks(pg):
    return pg.evaluate(KMARKS_JS)


def KMARKS(g, side):
    """Expected K marks for one starter: a K with "Reached N strikeouts." under each total he reached (final games)."""
    p = g["starters"][side]
    res = g.get("results") if g["status"] == "final" else None
    threw = res["starter_k"][side] if res and res.get("starter_k") else None
    return [["K", f"Reached {k} strikeouts."] if threw is not None and threw >= int(k) else ["", ""] for k in sorted(p["k_over"], key=int)]


def single_lines(pg, sel):
    """True when every visible match is a single line of text."""
    return pg.evaluate("""s => [...document.querySelectorAll(s)].filter(e => !e.hidden).every(e => {
        const lh = parseFloat(getComputedStyle(e).lineHeight); return e.getBoundingClientRect().height < lh * 1.5; })""", sel)


def running(pg):
    return pg.evaluate(RUNNING_JS)


def strip(pg):
    """The series strip's lines, or None when it is hidden."""
    return pg.evaluate("(() => { const s = document.querySelector('.ser'); return !s || s.hidden ? null : [...s.querySelectorAll('p')].map(p => p.textContent.replace(/\\s+/g, ' ').trim()); })()")


# ---------- home page cards ----------
CARDS_JS = """() => [...document.querySelectorAll('.games .grp')].map(g => [g.querySelector('h3').textContent,
    [...g.querySelectorAll('a.card')].map(c => {
        const t = e => e ? e.textContent.replace(/\\s+/g, ' ').trim() : null;
        const foot = [...c.querySelectorAll('.cd-foot .lr li')].map(li => {
            const l = li.querySelector('.lamp'), sr = [...li.querySelectorAll('.sr')].map(s => s.textContent).join('');
            const text = [...li.childNodes].filter(n => !(n.classList && (n.classList.contains('lamp') || n.classList.contains('sr'))))
              .map(n => n.textContent).join('').replace(/\\s+/g, ' ').trim();
            const state = !l ? null : l.classList.contains('hit') ? 'hit' : l.classList.contains('miss') ? 'miss' : 'unlit';
            return [text, state, sr, li.textContent.replace(/\\s+/g, ' ').trim()];
        });
        return { href: c.getAttribute('href'), label: c.getAttribute('aria-label'), st: t(c.querySelector('.cd-st')),
                 live: !!c.querySelector('.cd-st .lamp.on'), park: t(c.querySelector('.cd-park')),
                 teams: [...c.querySelectorAll('.cd-team')].map(r => [t(r.querySelector('.cd-nick')), t(r.querySelector('.cd-rec')),
                     t(r.querySelector('.mini')), r.querySelector('.mini').className, t(r.querySelector('.cd-runs')), !!r.querySelector('.cd-runs.lose')]),
                 foot, more: t(c.querySelector('.cd-more')) };
    })])"""


def cards(pg):
    return pg.evaluate(CARDS_JS)


# Cards whose footer (each lamp label and 'Full game') wraps onto a second row: every item overlaps every other vertically.
ONE_ROW_JS = """[...document.querySelectorAll('a.card')].flatMap((c, i) => {
    const r = [...c.querySelectorAll('.cd-foot .lr li, .cd-foot .cd-more')].map(e => e.getBoundingClientRect());
    return Math.max(...r.map(b => b.top)) < Math.min(...r.map(b => b.bottom)) ? [] : [i + ': ' + r.map(b => Math.round(b.top)).join('/')]; })"""


def CARD(g, st, label, recs=None, runs=None, live=False, foot="pre"):
    """Expected card: the status text, the accessible name, records and runs (or None), and the footer: 'pre' (the
    board's total line and NRFI label, no lamps) or 'final' (the board's lamps, graded on page.json; a game page.json
    doesn't have as final shows the labels with no lamps)."""
    m = g["markets"]
    fin = foot == "final"
    lose = (0 if runs[0] < runs[1] else 1) if fin and runs and runs[0] != runs[1] else None
    teams = [[NICK[g[s + "_team"]], recs[i] if recs else None, PCT(m[s + "_win"]), f"mini {BT(m[s + '_win'], 'ml')}",
              str(runs[i]) if runs else None, lose == i] for i, s in enumerate(("away", "home"))]
    if fin:
        # final cards show only the market (Winner, First 5, Total, NRFI); the pick and the result are screen-reader text
        graded = lamps_expect(g) if g["status"] == "final" else [(t, None, "") for t, _ in PICKS(g)]
        ft = []
        for t, s, word in graded:
            short = "First 5" if t.startswith("First 5") else t.split(" ")[0]
            ft.append([short, s, " " + t[len(short) + 1:] + word, (t + word).strip()])
    else:
        ft = [[x, None, "", x] for x in (f"Total O/U {OU(m['total_over'])[0][0][5:]}", PICKS(g)[3][0])]
    return {"href": f"/game/{g['game_id']}", "label": label, "st": st, "live": live, "park": g["park"], "teams": teams, "foot": ft, "more": "Full game"}


# ---------- Beat the Streak lists ----------
BTS_TOP_JS = """() => [...document.querySelectorAll('.rail .bts li')].map(li => { const a = li.querySelector('.meta a'), p = li.querySelector('.p');
    return [li.querySelector('.rk').textContent, li.querySelector('.name').textContent, li.querySelector('.meta').textContent.trim(),
            a ? a.getAttribute('href') : null, p.textContent, p.className]; })"""
BTS_JS = """() => [...document.querySelectorAll('.picks .bts li')].map(li => { const a = li.querySelector('.meta a'), line = li.querySelector('.line'), p = li.querySelector('.p');
    return [li.querySelector('.rk').textContent, li.querySelector('.name').textContent, li.querySelector('.meta').textContent.replace(/\\s+/g, ' ').trim(),
            a ? a.textContent : null, a ? a.getAttribute('href') : null, line ? line.textContent.trim() : null,
            [...li.querySelectorAll('.line svg')].map(s => s.getAttribute('aria-label')), p.textContent, p.className]; })"""


def BTS_ROWS(picks, games):
    """Expected rows of the full Beat the Streak list."""
    out = []
    for i, p in enumerate(picks):
        g = PICK_GAME(p, games)
        r = ROW(p, g) if g else None
        vs = None if not g else (f"at {NICK[g['home_team']]}" if g["away_team"] == p["team"] else f"vs {NICK[g['away_team']]}")
        meta = NICK[p["team"]] + (" " + vs if vs else "") + (r["line"] if r else "")
        out.append([str(i + 1), p["name"], meta, vs, f"/game/{g['game_id']}" if g else None, r["line"] if r else None,
                    ["got a hit"] if r and r["h"] >= 1 else [], PCT(p["hit"]), f"p {TIER(p['hit'], 'hit')}"])
    return out


# WCAG contrast of every visible element with its own text inside .infield whose nearest background is the dirt.
CONTRAST_JS = """() => {
    const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    const lum = c => { const [r, g, b] = c.match(/[\\d.]+/g).slice(0, 3).map(Number).map(lin); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
    const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
    const bgOf = e => { for (let n = e; n; n = n.parentElement) { const b = getComputedStyle(n).backgroundColor; if (b !== 'rgba(0, 0, 0, 0)' && b !== 'transparent') return b; } return null; };
    const DIRT = 'rgb(217, 195, 160)';
    const bad = [];
    let n = 0;
    for (const e of document.querySelectorAll('.infield *')) {
        if (e.closest('.sr')) continue;
        if (![...e.childNodes].some(t => t.nodeType === 3 && t.textContent.trim())) continue;
        const r = e.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        if (bgOf(e) !== DIRT) continue;
        n += 1;
        const c = getComputedStyle(e).color, cr = ratio(c, DIRT);
        if (cr < 4.5) bad.push([e.tagName.toLowerCase() + '.' + e.className, e.textContent.trim().slice(0, 30), c, cr.toFixed(2)]);
    }
    return [n, bad];
}"""

INFIELD_JS = """() => {
    const i = document.querySelector('.infield'), r = i.getBoundingClientRect(), c = getComputedStyle(i), banner = document.getElementById('banner');
    return { body: getComputedStyle(document.body).backgroundColor, bg: c.backgroundColor, poles: c.boxShadow,
             left: r.left, width: r.width, client: document.documentElement.clientWidth,
             navBottom: document.querySelector('.top').getBoundingClientRect().bottom, top: r.top,
             footerTop: document.querySelector('footer').getBoundingClientRect().top, bottom: r.bottom,
             holds: i.contains(document.querySelector('main')) && (!banner || i.contains(banner)) };
}"""

BANDS_JS = """() => [...document.querySelectorAll('.infield .sec > h2')].map(h => {
    const c = getComputedStyle(h), next = h.nextElementSibling;
    return [h.textContent, c.backgroundColor, c.color, c.borderBottomWidth + ' ' + c.borderBottomStyle + ' ' + c.borderBottomColor, c.padding,
            next ? Math.round(next.getBoundingClientRect().top - h.getBoundingClientRect().bottom) : 99];
})"""


def check_infield(pg, label, min_texts=20, bands=True):
    i = pg.evaluate(INFIELD_JS)
    width = min(1280, i["client"])
    check(f"{label}: grass sides (body is board green), dirt column, poles 4px yellow on both edges",
          i["body"] == GREEN and i["bg"] == DIRT and i["poles"] == POLE_SHADOW, i)
    check(f"{label}: the column is {width}px, centred, holding main (and the banner), from the nav to the footer",
          abs(i["width"] - width) < 1 and abs(i["left"] - (i["client"] - width) / 2) < 1 and i["holds"]
          and abs(i["top"] - i["navBottom"]) < 1 and abs(i["footerTop"] - i["bottom"]) < 1, i)
    b = pg.evaluate(BANDS_JS)
    check(f"{label}: every section heading on the dirt is a green band with chalk text and at least 12px under it",
          (bool(b) or not bands) and all(x[1] == GREEN and x[2] == CHALK and x[3] == f"3px solid {CHALK}" and x[4] == "10px 16px 8px" and x[5] >= 12 for x in b), b)
    pg.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
    n, bad = pg.evaluate(CONTRAST_JS)
    check(f"{label}: every text on the dirt has at least 4.5:1 contrast ({n} elements checked)", n >= min_texts and not bad, (n, bad[:5]))
    check(f"{label}: no 'null', 'undefined' or 'NaN' written into the page", stray_words(pg) == [], stray_words(pg))


# Yellow (#F2C12E) is allowed in exactly these places: strong numbers on the board (.b-strong) and in the tables (.p-strong),
# the hot home-run bar (.fill.hot), and keyboard focus rings. The brand mark's yellow second base is the logo and is exempt too.
YELLOW_JS = """() => {
    const Y = '242, 193, 46';
    const allowed = '.b-strong, .b-strong *, .p-strong, .p-strong *, .fill.hot, .brand .mark, .brand .mark *, .infield';
    return [...document.querySelectorAll('body *')].filter(e => {
        if (e.matches(allowed)) return false;
        const c = getComputedStyle(e);
        const vals = [c.color, c.backgroundColor, c.borderTopColor, c.borderRightColor, c.borderBottomColor, c.borderLeftColor,
                      c.boxShadow, c.textDecorationColor, c.fill, c.stroke];
        if (!e.matches(':focus-visible')) vals.push(c.outlineStyle === 'none' ? '' : c.outlineColor);
        return vals.some(v => v.includes(Y));
    }).map(e => e.tagName.toLowerCase() + (e.className && e.className.baseVal === undefined ? '.' + e.className.split(' ').join('.') : ''));
}"""


def yellow_offenders(pg):
    pg.evaluate("document.activeElement && document.activeElement.blur()")
    return pg.evaluate(YELLOW_JS)


def stray_words(pg):
    """'null', 'undefined' or 'NaN' written into the page: each text node on its own (body.textContent would join
    neighbouring nodes, so a stray "null" next to "IP" reads "nullIP" and slips past a word boundary), plus the
    aria-label, title and alt attributes. Screen-reader text is included."""
    return pg.evaluate("""(() => {
        const bad = /^(null|undefined|NaN)$|\\b(null|undefined|NaN)\\b/;
        const out = [];
        const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        for (let n = w.nextNode(); n; n = w.nextNode()) {
            const t = n.nodeValue.trim();
            if (t && bad.test(t)) out.push('text in ' + (n.parentElement ? n.parentElement.tagName.toLowerCase() + '.' + n.parentElement.className : '?') + ': ' + JSON.stringify(t));
        }
        for (const a of ['aria-label', 'title', 'alt']) {
            for (const e of document.body.querySelectorAll('[' + a + ']')) {
                const v = e.getAttribute(a).trim();
                if (bad.test(v)) out.push(a + ' on ' + e.tagName.toLowerCase() + ': ' + JSON.stringify(v));
            }
        }
        return out;
    })()""")


def plate_tops_aligned(pg):
    """Every model plate in a team row starts at the same height."""
    return pg.evaluate("""() => [...document.querySelectorAll('.bd tbody tr')].every(tr => {
        const tops = [...tr.querySelectorAll('.plate')].map(p => p.getBoundingClientRect().top);
        return Math.max(...tops) - Math.min(...tops) < 1; })""")


def SECURITY():
    """The headers public/_headers sets for every page (the /* rule), lower-case names."""
    out, cur = {}, None
    for raw in open(os.path.join(SITE, "_headers"), encoding="utf-8"):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            cur = line.strip()
        elif cur == "/*":
            k, v = line.strip().split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


NAV_JS = "[...document.querySelectorAll('.links a')].map(a => [a.textContent.trim(), a.getAttribute('href'), a.getAttribute('aria-current')])"


def common(pg, log, url, label, current, title, og, status=200):
    """What every page shares: the name, the nav and its aria-current, no 'How the model works', the title and og:title,
    the security headers on the page itself, same-origin requests only, no page errors and one h1."""
    check(f"{label}: the brand reads 'Diamond Metrics' and links to /", tc(pg, ".brand") == ["Diamond Metrics"] and pg.get_attribute(".brand", "href") == "/", tc(pg, ".brand"))
    nav = pg.evaluate(NAV_JS)
    exp = [["Games", "/", "page" if current == "games" else None], ["Beat the Streak", "/beat-the-streak", "page" if current == "bts" else None]]
    check(f"{label}: nav is Games and Beat the Streak, aria-current on {current or 'neither'}", nav == exp, nav)
    html = pg.evaluate("document.documentElement.outerHTML")
    check(f"{label}: no 'How the model works' and no link off the site", "How the model works" not in html and "github.com" not in html
          and pg.evaluate("[...document.querySelectorAll('a[href]')].every(a => a.getAttribute('href').startsWith('/') || a.getAttribute('href').startsWith('#'))"))
    text = pg.evaluate("document.body.innerText")
    check(f"{label}: the old lower-case name appears nowhere", "diamondmetrics" not in text and "diamondmetrics" not in pg.title())
    check(f"{label}: title '{title}' and og:title '{og}'", pg.title() == title and pg.get_attribute("meta[property='og:title']", "content") == og,
          (pg.title(), pg.get_attribute("meta[property='og:title']", "content")))
    doc = [x for x in log["responses"] if x[0] == url]
    sec = SECURITY()
    check(f"{label}: the page is {status} with every security header from _headers", len(doc) == 1 and doc[0][1] == status
          and all(doc[0][2].get(k) == v for k, v in sec.items()), doc[:1])
    origin = url.split("/", 3)[:3]
    check(f"{label}: every request is same-origin and every asset path is absolute",
          all(u.startswith("/".join(origin)) for u in log["requests"])
          and pg.evaluate("[...document.querySelectorAll('link[href], script[src]')].every(e => (e.getAttribute('href') || e.getAttribute('src')).startsWith('/'))"),
          [u for u in log["requests"] if not u.startswith("/".join(origin))])
    check(f"{label}: no page errors and no CSP violations", not log["errors"] and not [c for c in log["console"] if "Content Security Policy" in c[1]], (log["errors"], log["console"]))
    check(f"{label}: exactly one h1", pg.locator("h1").count() == 1, pg.locator("h1").count())
    check(f"{label}: html lang and viewport", pg.evaluate("document.documentElement.lang") == "en" and pg.locator("meta[name=viewport]").count() == 1)


def ready(pg, sel="h1"):
    pg.wait_for_selector(sel)
    pg.evaluate("document.fonts.ready")
    settle(pg, 300)


# =====================================================================
def test_real(browser, base):
    page_json = json.load(open(os.path.join(SITE, "data", "page.json"), encoding="utf-8"))
    site_json = json.load(open(os.path.join(SITE, "data", "site.json"), encoding="utf-8"))
    g = page_json["games"][0]
    m, res = g["markets"], g["results"]
    ws5 = fixture("linescore-849799.json")
    game_url = base + "game/849799"

    # the captured line score must agree with the frozen box score
    check("fixture: WS5 line score R totals equal page.json's results (4 and 1)",
          (ws5["away"]["r"], ws5["home"]["r"]) == (res["away_score"], res["home_score"]) == (4, 1) and str(g["game_id"]) == "849799",
          (ws5["away"], ws5["home"], res.get("away_score"), res.get("home_score")))
    check("fixture: inning runs add up to R", all(sum(i[s] or 0 for i in ws5["innings"]) == ws5[s]["r"] for s in ("away", "home")))
    check("fixture: the WS5 slate (real, trimmed by the Worker) has no records in the postseason",
          fixture("slate-2026-10-28.json")["games"]["849799"]["away"] == {"runs": 4, "wins": None, "losses": None})

    # ================= the game page (replay) =================
    ctx = new_ctx(browser)
    pg, log = open_page(ctx, game_url)
    ready(pg)

    pg.keyboard.press("Tab")
    check("a11y: skip link is the first focusable element and becomes visible", pg.evaluate("document.activeElement.className") == "skip" and pg.evaluate("document.activeElement.getBoundingClientRect().left >= 0"))
    pg.evaluate("document.activeElement.blur()")
    common(pg, log, game_url, "game page", "games", "Brewers at Rays, October 28: Diamond Metrics", "MLB game: Diamond Metrics")
    check("game page: no console messages at all", not log["console"], log["console"])
    check("game page: no failed responses", all(s < 400 for _, s, _ in log["responses"]), [(u, s) for u, s, _ in log["responses"] if s >= 400])
    check("game page: each game endpoint is requested once, for WS5; no slate (no other games)",
          sorted(pg.api.every) == [(e, "849799") for e in ("battery", "boxscore", "linescore", "series", "standings")], pg.api.every)
    fonts_loaded = sorted(u.rsplit("/", 1)[1] for u in log["requests"] if u.endswith(".woff2"))
    check("game page: both self-hosted fonts requested from /fonts/", fonts_loaded == ["big-shoulders-latin-wght-normal.woff2", "libre-franklin-latin-wght-normal.woff2"]
          and all("/fonts/" in u for u in log["requests"] if u.endswith(".woff2")), fonts_loaded)
    check("game page: fonts actually usable", pg.evaluate("document.fonts.check('800 20px \"Big Shoulders\"') && document.fonts.check('16px \"Libre Franklin\"')"))
    check("game page: theme-color meta is board green", pg.get_attribute("meta[name=theme-color]", "content") == "#1F4A38")
    check("game page: no countdown banner (it is on the home page only)", pg.locator("#banner").count() == 0)

    # title block
    check("title: h1 is replay_title", tc(pg, "h1") == [site_json["replay_title"]], tc(pg, "h1"))
    check("title: headline from site.json", tc(pg, ".lede") == [site_json["replay_headline"]], tc(pg, ".lede"))
    check("title: replay notice is the label verbatim", tc(pg, ".notice") == [page_json["label"]], tc(pg, ".notice"))
    # 2026-11-18T17:13:27Z is 12:13 PM EST, weeks after the Oct 28 game: the date stays, the year goes
    check("title: updated line keeps its date (not the game's day)", tc(pg, ".upd") == ["Updated Nov 18, 12:13 PM EST."], tc(pg, ".upd"))
    check("game page: 'All games' links back to /, between the title block and the board",
          tc(pg, ".back a") == ["All games"] and pg.get_attribute(".back a", "href") == "/"
          and pg.evaluate("document.querySelector('.back').nextElementSibling.classList.contains('board') && document.querySelector('.back').previousElementSibling.classList.contains('upd')"))
    check("game page: no out-of-town board, no Other games (one game on the slate), no Beat the Streak",
          pg.locator(".oot, .gm, .others, .bts").count() == 0 and "Other games" not in pg.evaluate("document.body.textContent"))

    # ---------- the board: line score ----------
    check("board: top line (park and game date with no timezone shift, no 'indoors')", tc(pg, ".bd-where") == ["Tropicana Field. Wednesday, October 28, 2026."], tc(pg, ".bd-where"))
    check("board: 'indoors' appears nowhere on the board", "indoors" not in pg.evaluate("document.querySelector('.board').textContent"))
    check("board: no live line on a final game", pg.locator(".bd-live").count() == 0)
    check("line score: header 1 to 9, then R H E", ls_head(pg) == ["Team", "1", "2", "3", "4", "5", "6", "7", "8", "9", "R", "H", "E"], ls_head(pg))
    exp_ls = LS(ws5, "MIL", "TB")
    check("line score: every inning plate and R, H, E from the API", ls_rows(pg) == exp_ls, (ls_rows(pg), exp_ls))
    check("line score: the Rays batted in the 9th, so no X", "X" not in ls_rows(pg)[1][1])
    check("line score: a real table with caption and headers", pg.locator(".ls caption").count() == 1 and pg.locator(".ls th[scope=col]").count() == 13 and pg.locator(".ls th[scope=row]").count() == 2)
    check("line score: header 13px #C9D6CE, plates board-plate green in Big Shoulders 700",
          [css(pg, ".ls thead th", "fontSize"), css(pg, ".ls thead th", "color"), css(pg, ".ls .ip", "backgroundColor"), css(pg, ".ls .ip", "fontWeight"), css(pg, ".ls .ip", "fontFamily").split(",")[0].strip('"')]
          == ["13px", "rgb(201, 214, 206)", "rgb(22, 54, 40)", "700", "Big Shoulders"])
    sz = pg.evaluate("(() => { const r = document.querySelector('.ls .ip').getBoundingClientRect(), b = document.querySelector('.ls .ip.big').getBoundingClientRect(); return [r.width, r.height, b.width, b.height, parseFloat(getComputedStyle(document.querySelector('.ls .ip')).fontSize)]; })()")
    check("line score: inning plates about 34x40 with 28px numerals; R, H, E larger", sz[:2] == [34, 40] and sz[2] > sz[0] and sz[3] > sz[1] and sz[4] == 28, sz)
    check("line score: R set off by a 2px seam", css(pg, ".ls tbody td.rhe.first", "borderLeftWidth") == "2px" and css(pg, ".ls tbody td.rhe.first", "borderLeftColor") == "rgb(43, 91, 70)")
    check("line score: no current-inning outline on a final", pg.locator(".ls .ip.cur").count() == 0)

    # ---------- the board: season series strip ----------
    ser = fixture("series-849799.json")
    check("series fixture: WS5 regular season Brewers 2-1 (3 played, none left), World Series 4-1",
          ser["regular"] == {"awayWins": 2, "homeWins": 1, "played": 3, "left": 0, "sweeps": [], "seasonSweep": None}
          and ser["postseason"]["result"] == {"leader": "away", "wins": 4, "losses": 1, "over": True}, ser)
    check("series strip: WS5 lines", strip(pg) == SERIES_LINES(g, ser) == ["Season series: Brewers 2–1", "World Series: Brewers win 4–1."], (strip(pg), SERIES_LINES(g, ser)))
    check("series strip: between the line score and the model section", pg.evaluate("(() => { const s = document.querySelector('.ser'); return s.previousElementSibling.classList.contains('ls-scroll') && s.nextElementSibling.classList.contains('model'); })()"))
    check("series strip: 15px #F4F5F0 under a 2px #2B5B46 rule, labels #C9D6CE",
          [css(pg, ".ser", "fontSize"), css(pg, ".ser", "color"), css(pg, ".ser", "borderTopWidth"), css(pg, ".ser", "borderTopColor"), css(pg, ".ser .lab", "color")]
          == ["15px", WHITE, "2px", "rgb(43, 91, 70)", "rgb(201, 214, 206)"])
    check("series strip: the labels are 'Season series:' and 'World Series:'", tc(pg, ".ser .lab") == ["Season series:", "World Series:"], tc(pg, ".ser .lab"))
    check("board: model section below a 2px #2B5B46 rule", css(pg, ".board .model", "borderTopWidth") == "2px" and css(pg, ".board .model", "borderTopColor") == "rgb(43, 91, 70)")

    # ---------- the board: model numbers ----------
    heads = tc(pg, ".bd thead th")
    check("board: column headers (no Final column)", heads == ["Team", "Odds to win", "First 5 winner", "Total runs O/U"], heads)
    check("board: caption, column and row headers", pg.locator(".bd caption").count() == 1 and pg.locator(".bd th[scope=col]").count() == 4 and pg.locator(".bd th[scope=row]").count() == 2)
    teams = [tc(pg, ".bd th.team .nick"), tc(pg, ".bd th.team .bl-sp"), tc(pg, ".bd th.team .bl-c")]
    check("board: team cells (nickname, then SP from page.json and C from the boxscore)",
          teams == [["Brewers", "Rays"], ["SP Jacob Misiorowski", "SP Drew Rasmussen"], ["C William Contreras", "C Liam Hicks"]], teams)
    check("board: no 'pitching' line any more", "pitching" not in pg.evaluate("document.querySelector('.board').textContent"))
    check("board: SP and C labels #C9D6CE, names #F4F5F0, 14px like the old pitcher line",
          [css(pg, ".bd .bl-sp .pos", "color"), css(pg, ".bd .bl-c .pos", "color"), css(pg, ".bd .bl-sp .who", "color"), css(pg, ".bd .bl-c .who", "color"), css(pg, ".bd .bl-sp", "fontSize"), css(pg, ".bd .bl-c", "fontSize")]
          == ["rgb(201, 214, 206)", "rgb(201, 214, 206)", WHITE, WHITE, "14px", "14px"])
    ou_labels, ou_vals = OU(m["total_over"])
    check("board O/U: WS5 picks the 6.5 line (over chance 0.546, closest to 50%)", ou_labels == ["Over 6.5", "Under 6.5"] and ou_vals == ["55%", "45%"], (ou_labels, ou_vals))
    for i, side in enumerate(("away", "home")):
        row = pg.locator(".bd tbody tr").nth(i)
        plates = [x.strip() for x in row.locator(".plate").all_text_contents()]
        exp = [PCT(m[side + "_win"]), PCT(m["f5_" + side]), ou_vals[i]]
        check(f"board {side}: plate numbers", plates == exp, (plates, exp))
        cls = row.locator(".plate").evaluate_all("els => els.map(e => e.className)")
        expc = [f"plate {BT(m[side+'_win'], 'ml')}", f"plate {BT(m['f5_'+side], 'f5')}", "plate b-plain"]
        check(f"board {side}: plate tier classes (totals have no tier)", cls == expc, (cls, expc))
        caps = [norm(x) for x in row.locator(".cap").all_text_contents()]
        check(f"board {side}: the only caption is the O/U label, under its plate", caps == [ou_labels[i]] and row.locator("td[data-label='Total runs O/U'] .plate + .cap").count() == 1, caps)
    check("board O/U: caption 13px #C9D6CE", css(pg, ".bd .cap", "fontSize") == "13px" and css(pg, ".bd .cap", "color") == "rgb(201, 214, 206)")
    check("board: every plate in a row lines up at the top", plate_tops_aligned(pg))
    check("board: the % sign is its own smaller span", pg.locator(".bd .plate .pc").count() == 6)

    # result lamps: one per board market, graded on page.json's results
    exp_l = lamps_expect(g)
    check("lamps: WS5 picks worked out in the test", [x[:2] for x in exp_l] == [("Winner Brewers", "hit"), ("First 5 Brewers", "hit"), ("Total Over 6.5", "miss"), ("NRFI 55%", "hit")], exp_l)
    check("lamps: WS5 labels, colours and screen-reader words", lamps(pg) == exp_l, (lamps(pg), exp_l))
    check("lamps: NRFI number keeps its tier", classes(pg, ".lr-nr .num") == [f"num {BT(m['no_run_first'], 'nrfi')}"], classes(pg, ".lr-nr .num"))
    cols = pg.evaluate("[...document.querySelectorAll('.lr .lamp')].map(l => getComputedStyle(l).backgroundColor)")
    check("lamps: green #7CF0A6 when the pick came true, red #FF6B5E when it missed", cols == [HIT, HIT, MISS, HIT], cols)
    check("lamps: green and red lamps glow", all("rgba" in pg.evaluate("e => getComputedStyle(e).boxShadow", el) for el in pg.query_selector_all(".lr .lamp")))
    check("lamps: hidden from screen readers (the words carry the meaning)", all(a == "true" for a in pg.eval_on_selector_all(".lamp", "els => els.map(e => e.getAttribute('aria-hidden'))")))

    # ---------- home run conditions (dome) ----------
    check("conditions: heading, no park line, no separate Dome line", tc(pg, ".cond h2") == ["Home run conditions"] and pg.locator(".cond .subl, .cond .cond-note").count() == 0)
    check("conditions: no outdoor readings for a dome", pg.locator(".wx").count() == 0)
    check("conditions: bar label without the heading's words", tc(pg, ".hrbox .hrrow > *") == [g["weather"]["hr_label"], f"{g['weather']['hr_score']} out of 100"] and tc(pg, ".hrbox .hrrow > *")[0] == "Neutral (Dome)", tc(pg, ".hrbox .hrrow > *"))
    check("conditions: bar width = score (CSSOM style works under the CSP), not hot at 50", pg.evaluate("Math.abs(document.querySelector('.hrbox .fill').getBoundingClientRect().width - document.querySelector('.hrbox .track').getBoundingClientRect().width * 0.5) < 1") and pg.evaluate("document.querySelector('.hrbox .fill').style.width") == "50%" and pg.locator(".fill.hot").count() == 0)

    # ---------- total runs ----------
    lines = sorted((k for k in m["total_over"] if k.endswith(".5")), key=float)
    over = sum(res["total_runs"] > float(k) for k in lines)
    exp_sub = f"Chance the game goes over each line. It finished with {RUNS(res['total_runs'])}, " + ("under all of them." if over == 0 else f"over {over} of them.")
    check("totals: sub line with the final total", tc(pg, ".totals .subl") == [exp_sub], (tc(pg, ".totals .subl"), exp_sub))
    check("totals: half-point lines only", tc(pg, ".tot-l") == [f"Over {k}" for k in lines], tc(pg, ".tot-l"))
    check("totals: values", tc(pg, ".tot-v") == [PCT(m["total_over"][k]) for k in lines], tc(pg, ".tot-v"))
    widths = pg.eval_on_selector_all(".bar-fill", "els => els.map(e => e.style.width)")
    check("totals: bar widths are the whole percent shown", widths == [f"{W(m['total_over'][k])}%" for k in lines], widths)
    geo = pg.eval_on_selector_all(".bar", "els => els.map(e => e.firstChild.getBoundingClientRect().width / e.getBoundingClientRect().width)")
    check("totals: bars drawn at those widths", all(abs(a - W(m["total_over"][k]) / 100) < 0.01 for a, k in zip(geo, lines)), geo)
    check("totals: no check marks (5 runs went over none of the lines)", pg.locator(".tot svg").count() == over == 0)

    # ---------- pitchers ----------
    check("pitchers: heading 'Starting pitchers' on the replay", tc(pg, ".pitchers h2") == ["Starting pitchers"], tc(pg, ".pitchers h2"))
    check("pitchers: no 'Battery' heading and no sub line", pg.locator(".pitchers .subl").count() == 0 and not any("attery" in t for t in tc(pg, "h1, h2, h3")), tc(pg, "h2"))
    check("pitchers: no SP or C lines in the section (they are on the board)", pg.locator(".pit .bl, .pit .pos").count() == 0
          and not any(t.startswith(("SP ", "C ")) for t in tc(pg, ".pit div, .pit p")))
    check("pitchers: the strikeout O/U is gone", pg.locator(".kou, .kou-l, .kou-v").count() == 0 and "Strikeouts O/U" not in pg.evaluate("document.body.textContent"))
    pits = pg.locator(".pit")
    check("pitchers: two blocks", pits.count() == 2)
    for i, side in enumerate(("away", "home")):
        p = g["starters"][side]
        blk = pits.nth(i)
        t = lambda sel: [norm(x) for x in blk.locator(sel).all_text_contents()]
        threw = res["starter_k"][side]
        check(f"pitchers {side}: team and starter's name", t(".who") == [NICK[g[side + "_team"]]] and t(".nm") == [p["name"]], (t(".who"), t(".nm")))
        ks = sorted(p["k_over"], key=int)
        check(f"pitchers {side}: ladder labels", t(".rung .l") == [f"{k}+ K" for k in ks], t(".rung .l"))
        check(f"pitchers {side}: ladder values", t(".rung .v") == [PCT(p["k_over"][k]) for k in ks], t(".rung .v"))
        got_c = blk.locator(".rung .p").evaluate_all("els => els.map(e => e.className)")
        check(f"pitchers {side}: ladder chip classes", got_c == [f"p {TIER(p['k_over'][k], 'k'+k)}" for k in ks], got_c)
        check(f"pitchers {side}: a K under each total reached, with 'Reached N strikeouts.' for screen readers",
              kmarks(pg)[i] == KMARKS(g, side), (kmarks(pg)[i], KMARKS(g, side)))
        marks = [blk.locator(".rung").nth(j).locator(".ck .kmark").count() == 1 for j in range(len(ks))]
        check(f"pitchers {side}: the K sits in the old check slot, under its own column", marks == [threw >= int(k) for k in ks], marks)
        exp_fi = fi_expect(p["first_inning"])
        check(f"pitchers {side}: first-inning label and stat blocks (no WHIP in the data)", t(".fi-l") == ["First inning"] and t(".fi .stats > div") == exp_fi and len(exp_fi) == 3, (t(".fi .stats > div"), exp_fi))
    check("pitchers: Rasmussen gets a K under 3+ and 4+, Misiorowski none (the old check counts)",
          [[r[0] for r in blk] for blk in kmarks(pg)] == [["", "", "", "", ""], ["K", "K", "", "", ""]], kmarks(pg))
    check("pitchers: the K is bold display type in --ok, hidden from screen readers (the sentence carries it)",
          [css(pg, ".kmark", "fontFamily").split(",")[0].strip('"'), css(pg, ".kmark", "fontWeight"), css(pg, ".kmark", "color"), pg.get_attribute(".kmark", "aria-hidden")]
          == ["Big Shoulders", "800", OK_GREEN, "true"])
    check("pitchers: no check marks left in the K row", pg.locator(".rung svg").count() == 0)
    box = fixture("boxscore-849799.json")
    exp_b = [BATTERY(g, s, box, "final") for s in ("away", "home")]
    check("battery: WS5 SP and C on the board, and each starter's line (IP, H, BB, SO) in the section",
          battery(pg) == exp_b == [["SP Jacob Misiorowski", "C William Contreras", None, ["IP5.2", "H2", "BB1", "SO2"]],
                                   ["SP Drew Rasmussen", "C Liam Hicks", None, ["IP4.0", "H2", "BB0", "SO4"]]], (battery(pg), exp_b))
    stf, btf = fixture("standings-849799.json"), fixture("battery-849799.json")
    exp_x = EXTRAS(stf, btf)
    check("board: records, standings lines and battery records",
          extras(pg) == exp_x == [["103–59", "1st in the NL Central, 14 games up.", "35–17 in the division.", "Battery 20–7 this season"],
                                  ["98–64", "1st in the AL East, 4.5 games up.", "28–24 in the division.", "Battery 8–1 this season"]], (extras(pg), exp_x))
    check("board at 1280: each standings line fits on one line", single_lines(pg, ".bd .bl-st") and single_lines(pg, ".bd .bl-dr"))
    exp_r = RUNNING(btf)
    check("starting pitchers: stolen-base rows (stolen, caught, pickoffs / caught-stealing %, '(2 teams)')",
          running(pg) == exp_r == [["Jacob Misiorowski 2 stolen, 1 caught, 0 pickoffs", "William Contreras 48 stolen, 25 caught, 34%"],
                                   ["Drew Rasmussen 23 stolen, 6 caught, 0 pickoffs", "Liam Hicks 58 stolen, 11 caught, 16% (2 teams)"]], (running(pg), exp_r))
    check("starting pitchers: no 'Running game' label", pg.locator(".pit .rg-l").count() == 0 and "Running game" not in pg.evaluate("document.body.textContent"))

    # ---------- lineups ----------
    by_id = {r["id"]: r for side in ("away", "home") for r in res["batters"][side]}
    tabs = pg.locator("table.t")
    check("lineups: two tables with a Result column", tabs.count() == 2 and tabs.nth(0).locator("thead th").count() == 8)
    check("lineups: column headers", tc(pg, "table.t >> nth=0 >> thead th") == ["#", "Player", "1+ hit", "2+ bases", "Homer", "RBI", "Steal", "Result"], tc(pg, "table.t >> nth=0 >> thead th"))
    check("lineups: headings are just the nicknames", tc(pg, ".lu h3") == ["Brewers", "Rays"], tc(pg, ".lu h3"))
    exp_checks = {}
    for i, side in enumerate(("away", "home")):
        rows = tabs.nth(i).locator("tbody tr")
        check(f"lineups {side}: nine rows", rows.count() == 9)
        exp_rows, exp_icons, exp_checks[side] = lineup_expect(g, side, by_id)
        ok_text, ok_cls, ok_icons, bad = True, True, True, []
        for r, row in enumerate(g["lineups"][side]):
            cells = [norm(x) for x in rows.nth(r).locator("td").all_text_contents()]
            if cells != exp_rows[r]:
                ok_text = False; bad.append((cells, exp_rows[r]))
            cls = rows.nth(r).locator(".p").evaluate_all("els => els.map(e => e.className)")
            expc = [f"p {TIER(row[k], key)}" for k, key in (("hit", "hit"), ("tb2", "tb"), ("hr", "hr"), ("rbi", "rbi"), ("sb", "sb"))]
            if cls != expc:
                ok_cls = False; bad.append((cls, expc))
            icons = rows.nth(r).locator("svg").evaluate_all("els => els.map(e => e.getAttribute('aria-label'))")
            if icons != exp_icons[r]:
                ok_icons = False; bad.append((row["name"], icons, exp_icons[r]))
        check(f"lineups {side}: all cell text and result lines", ok_text, bad[:2])
        check(f"lineups {side}: all chip classes", ok_cls, bad[:2])
        check(f"lineups {side}: a result icon, with its label, after every prop that came true", ok_icons, bad[:2])
    check("lineups: WS5 has 13 Brewers icons and 6 Rays icons", (exp_checks["away"], exp_checks["home"]) == (13, 6), exp_checks)
    ic = pg.evaluate("""[...document.querySelectorAll('table.t svg')].map(s => { const c = getComputedStyle(s.querySelector('path, circle'));
        return [s.getAttribute('role'), s.getAttribute('width'), s.getAttribute('height'), c.fill !== 'none' ? c.fill : c.stroke, s.closest('.ck') !== null, s.getAttribute('aria-hidden')]; })""")
    check("lineups: icons are 14px images in --ok (#1A5E37) with labels, in the check slot", ic and all(x == ["img", "14", "14", OK_GREEN, True, None] for x in ic), ic[:3])
    tb = pg.evaluate("""(() => { const d = s => s.querySelector('path').getAttribute('d');
        const tb = [...document.querySelectorAll('table.t svg')].filter(x => x.getAttribute('aria-label') === 'two or more bases');
        const hit = document.querySelector('table.t svg[aria-label="got a hit"]');
        return [tb.length, tb.every(x => d(x) === d(hit) && x.querySelector('path').getAttribute('transform') === 'translate(8 8) rotate(-45)')]; })()""")
    check("lineups: 2+ bases is the bat, the same drawing as 1+ hit, labelled 'two or more bases'", tb[0] > 0 and tb[1], tb)
    check("lineups: no check marks left in the lineups", pg.evaluate("[...document.querySelectorAll('table.t svg path')].every(p => p.getAttribute('d') !== 'M3 8.5l3.2 3.2L13 4.5')"))
    check("lineups: scroll regions are keyboard focusable and named", pg.locator(".scroll[tabindex='0'][role=region][aria-label]").count() == 2)

    # ---------- rail: cut-offs only ----------
    check("rail: the cut-offs disclosure is all there is on the replay (no other games)", pg.evaluate("[...document.querySelector('.rail').children].map(e => e.tagName + '.' + e.className)") == ["DETAILS.how"])
    check("cut-offs: 3px chalk top rule", css(pg, ".how", "borderTopWidth") == "3px" and css(pg, ".how", "borderTopColor") == CHALK)
    pg.click(".how summary")
    check("cut-offs: key line comes first, before the table", tc(pg, ".how p.key") == [KEY_LINE] and pg.evaluate("document.querySelector('.how p.key').nextElementSibling.tagName") == "TABLE")
    rows = tc(pg, ".how tbody tr")
    exp = {"Odds to win": "ml", "First 5 winner": "f5", "NRFI": "nrfi", "Starter 3+ strikeouts": "k3",
           "Starter 4+ strikeouts": "k4", "Starter 5+ strikeouts": "k5", "Starter 6+ strikeouts": "k6", "Starter 7+ strikeouts": "k7",
           "Batter 1+ hit": "hit", "Batter 2+ total bases": "tb", "Batter 1+ home run": "hr", "Batter 1+ RBI": "rbi", "Batter 1+ stolen base": "sb"}
    check("cut-offs: table equals the original app's cut-offs", rows == [f"{k}{TH[v][0]}%+{TH[v][1]}%+" for k, v in exp.items()], rows)

    # ---------- structure, accessibility, copy ----------
    check("footer text", tc(pg, "footer p") == ["Predictions come from simulating each game thousands of times. Probabilities, not guarantees, and not betting advice.", "Not affiliated with Major League Baseball."])
    body = pg.evaluate("document.body.innerText")
    check("copy: no middle dots and no arrows", "·" not in body and not any(c in body for c in "→↗›»"))
    out = focus_outline(pg, ".links a[href='/beat-the-streak']")
    check("a11y: focus outline on green is 3px yellow", out == ["solid", "3px", YELLOW], out)
    check("nav: the current-page underline is white", css(pg, ".links a[aria-current]", "textDecorationColor") == WHITE)
    check("game page (replay): yellow only on strong numbers, the hot HR bar and focus rings", yellow_offenders(pg) == [], yellow_offenders(pg))
    out = focus_outline(pg, ".back a")
    check("a11y: focus outline on the ground is 3px board green", out == ["solid", "3px", GREEN], out)
    check("a11y: nav links, the brand and 'All games' are at least 44px tall", pg.evaluate("[...document.querySelectorAll('.links a, .brand, .back a')].every(a => a.getBoundingClientRect().height >= 44)"))
    for w in (768, 1440):
        pg.set_viewport_size({"width": w, "height": 900})
        settle(pg, 150)
        plates_in = pg.evaluate("(() => { const b = document.querySelector('.board').getBoundingClientRect(); return [...document.querySelectorAll('.board .plate, .board .ip')].every(p => p.getBoundingClientRect().right <= b.right - 8); })()")
        check(f"board at {w}: every plate inside the board, tops lined up", plates_in and plate_tops_aligned(pg))
    check("scrollbars: the browser runs with classic scrollbars that take up space, as on Windows",
          pg.evaluate("innerWidth - document.documentElement.clientWidth") > 0, pg.evaluate("innerWidth - document.documentElement.clientWidth"))
    pg.set_viewport_size({"width": 853, "height": 900})
    settle(pg, 150)
    w853 = pg.evaluate(WRAPPERS)
    check("853: the line score has no vertical scroll and both lineups fit without a scrollbar", all(r[3] <= r[4] and r[1] <= r[2] for r in w853), w853)
    sweep(pg, "game page (replay) sweep")
    pg.set_viewport_size({"width": 1440, "height": 900})
    settle(pg, 150)
    pg.evaluate("document.activeElement.blur()")
    check_infield(pg, "game page (replay) 1440")
    pg.evaluate("document.querySelectorAll('details').forEach(d => d.open = false)")
    screenshot(pg, "game-replay-1440.png")
    ctx.close()

    # ---------- game page on a phone ----------
    ctx = new_ctx(browser, 390, 844)
    pg, log = open_page(ctx, game_url)
    ready(pg, ".ls .ip")
    check("game page 390: no horizontal page scroll", no_hscroll(pg), pg.evaluate("document.documentElement.scrollWidth"))
    check("game page 390: nine innings plus R, H, E fit without scrolling", pg.evaluate("(() => { const s = document.querySelector('.ls-scroll'); return s.scrollWidth <= s.clientWidth; })()"))
    psz = pg.evaluate("(() => { const p = document.querySelector('.ls .ip'); return [p.getBoundingClientRect().width, parseFloat(getComputedStyle(p).fontSize)]; })()")
    check("game page 390: inning plates about 20px wide with 16px numerals", psz == [20, 16], psz)
    check("game page 390: line score values still right", ls_rows(pg) == exp_ls)
    wide = pg.evaluate("[...document.querySelectorAll('.scroll')].map(e => e.scrollWidth > e.clientWidth)")
    check("game page 390: lineup tables scroll inside their own box", wide == [True, True], wide)
    labels = pg.evaluate("[...document.querySelectorAll('.bd tbody tr:first-child td')].map(td => getComputedStyle(td, '::before').content)")
    check("game page 390: board cells labelled from data-label", labels == ['"Odds to win"', '"First 5 winner"', '"Total runs O/U"'], labels)
    sizes = pg.evaluate("(() => { const p = document.querySelector('.plate'); return [parseFloat(getComputedStyle(p).fontSize), parseFloat(getComputedStyle(p.querySelector('.pc')).fontSize), parseFloat(getComputedStyle(document.querySelector('h1')).fontSize)]; })()")
    check("game page 390: plates 36-40px with an 18-20px % sign, h1 about 48px", 36 <= sizes[0] <= 40 and 18 <= sizes[1] <= 20 and sizes[2] == 48, sizes)
    check("game page 390: each team is one block (name row on top, plates below)", pg.evaluate("[...document.querySelectorAll('.bd tbody tr')].every(tr => { const th = tr.querySelector('th').getBoundingClientRect(); return [...tr.querySelectorAll('td')].every(td => td.getBoundingClientRect().top >= th.bottom); })"))
    check("game page 390: plates inside the board, tops lined up", plate_tops_aligned(pg) and pg.evaluate("(() => { const b = document.querySelector('.board').getBoundingClientRect(); return [...document.querySelectorAll('.board .plate, .board .ip')].every(p => p.getBoundingClientRect().right <= b.right); })()"))
    check_infield(pg, "game page (replay) 390")
    pg.evaluate("document.querySelectorAll('details').forEach(d => d.open = false)")
    screenshot(pg, "game-replay-390.png")
    ctx.close()

    # ---------- the game's endpoints can't be fetched: same empty board, the rest of the page intact ----------
    for label, spec in (("500", 500), ("timeout", "hang")):
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, game_url, api=Api({f"{e}/849799": spec for e in ("linescore", "boxscore", "series", "standings", "battery")}), install=True)
        pg.wait_for_selector(".board")
        if spec == "hang":
            adv(pg, 10500)  # the page gives up after 10 seconds
        settle(pg, 300)
        check(f"all endpoints {label}: empty plates and dashes, no error banner", ls_rows(pg) == LS(None, "MIL", "TB") and pg.locator("[role=alert]").count() == 0, ls_rows(pg))
        check(f"all endpoints {label}: no series strip, catchers not announced, no starter's line, page.json starters kept",
              strip(pg) is None and battery(pg) == [BATTERY(g, s, None, "final") for s in ("away", "home")], battery(pg))
        check(f"all endpoints {label}: no record, standings lines, battery record or stolen-base rows", extras(pg) == [[None] * 4] * 2 and running(pg) == [None, None], (extras(pg), running(pg)))
        check(f"all endpoints {label}: the rest of the page is unaffected",
              tc(pg, "h1") == [site_json["replay_title"]] and pg.locator(".bd .plate").count() == 6 and pg.locator("table.t tbody tr").count() == 18
              and tc(pg, ".bd-where") == ["Tropicana Field. Wednesday, October 28, 2026."] and pg.locator(".pit .rung").count() == 10 and pg.locator(".lr li").count() == 4)
        check(f"all endpoints {label}: no page errors", not log["errors"], log["errors"])
        ctx.close()
    for ep, wait, test in (
            ("boxscore", ".ser:not([hidden])", lambda pg: ls_rows(pg) == LS(ws5, "MIL", "TB") and strip(pg) == SERIES_LINES(g, ser) and [b[1:] for b in battery(pg)] == [["C not announced", None, None]] * 2),
            ("series", ".pline", lambda pg: strip(pg) is None and ls_rows(pg) == LS(ws5, "MIL", "TB") and battery(pg)[0][1] == "C William Contreras"),
            ("standings", ".rg:not([hidden])", lambda pg: [x[:3] for x in extras(pg)] == [[None, None, None]] * 2 and [x[3] for x in extras(pg)] == ["Battery 20–7 this season", "Battery 8–1 this season"]),
            ("battery", ".bl-st:not([hidden])", lambda pg: [x[3] for x in extras(pg)] == [None, None] and running(pg) == [None, None] and battery(pg)[1][1] == "C Liam Hicks")):
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, game_url, api=Api({**WS5_API, f"{ep}/849799": 500}))
        pg.wait_for_selector(wait)
        settle(pg, 300)
        check(f"{ep} 500 alone: only its part is missing", test(pg), (extras(pg), battery(pg), strip(pg)))
        ctx.close()

    # ---------- a final game never polls ----------
    ctx = new_ctx(browser)
    pg, _ = open_page(ctx, game_url, install=True)
    pg.wait_for_selector(".ls .ip")
    settle(pg)
    adv(pg, 10 * 60000)
    check("polling: a final game is fetched once and never again", pg.api.count(849799) == 1, pg.api.calls)
    ctx.close()

    # ================= the home page (replay) =================
    ctx = new_ctx(browser)
    pg, log = open_page(ctx, base, install=True)
    ready(pg, "a.card")
    common(pg, log, base, "home (replay)", "games", "Diamond Metrics: MLB predictions", "Diamond Metrics: MLB predictions")
    check("home (replay): no console messages", not log["console"], log["console"])
    # countdown (fixed clock 2026-11-18 -> Opening Day 2027-03-24 is 126 days)
    check("countdown: one plate per digit", tc(pg, "#banner .dg") == ["1", "2", "6"], tc(pg, "#banner .dg"))
    check("countdown: digits are one image named '126 days'", pg.get_attribute("#banner .digits", "role") == "img" and pg.get_attribute("#banner .digits", "aria-label") == "126 days")
    check("countdown: text", tc(pg, "#banner .cd-t") == ["days to Opening Day 2027, Wednesday, March 24."], tc(pg, "#banner .cd-t"))
    check("countdown: note matches site.json", tc(pg, "#banner .cd-note") == [site_json["opening_note"]])
    check("countdown: region with its label", pg.get_attribute("#banner", "role") == "region" and pg.get_attribute("#banner", "aria-label") == "Opening Day countdown")
    check("home (replay): title, headline, label and updated line as the game page shows them",
          [tc(pg, "h1"), tc(pg, ".lede"), tc(pg, ".notice"), tc(pg, ".upd")] == [[site_json["replay_title"]], [site_json["replay_headline"]], [page_json["label"]], ["Updated Nov 18, 12:13 PM EST."]],
          [tc(pg, "h1"), tc(pg, ".lede"), tc(pg, ".upd")])
    check("home (replay): a 'Games' band over the cards (not 'Today's games')", tc(pg, ".games > h2") == ["Games"], tc(pg, ".games > h2"))
    exp_card = CARD(g, "Final", "Brewers at Rays, final, 4 to 1. Full game.", runs=[4, 1], foot="final")
    check("home (replay): one Final group with one card: final, park, no records (postseason), plates, 4 to 1 (the 1 dimmed), the board's four lamps, Full game",
          cards(pg) == [["Final", [exp_card]]], (cards(pg), exp_card))
    check("home (replay): the card is a single link to /game/849799 and nothing in it is a link of its own",
          pg.locator("a.card").count() == 1 and pg.locator("a.card a, a.card button").count() == 0 and pg.get_attribute("a.card", "href") == "/game/849799")
    check("home (replay): the card shows short lamp labels; the full picks stay in its text for screen readers",
          [f[0] for f in exp_card["foot"]] == ["Winner", "First 5", "Total", "NRFI"]
          and pg.evaluate("[...document.querySelectorAll('.card .lr li')].map(li => li.textContent.replace(/\\s+/g, ' ').trim())")
          == [t + w for t, _, w in lamps_expect(g)], pg.evaluate("[...document.querySelectorAll('.card .lr li')].map(li => li.textContent)"))
    check("home (replay): the card's pick text is screen-reader only (clipped to 1px)",
          pg.evaluate("[...document.querySelectorAll('.card .lr li .sr')].every(s => s.getBoundingClientRect().width <= 1)"))
    wrapped = {}
    for w in sorted(set(SWEEP) | {374, 375, 390}):
        pg.set_viewport_size({"width": w, "height": 900})
        settle(pg, 60)
        if pg.evaluate(ONE_ROW_JS):
            wrapped[w] = pg.evaluate(ONE_ROW_JS)
    check("home (replay) 360-1440: the final card's four lamps and 'Full game' sit on one row at every width", not wrapped, wrapped)
    pg.set_viewport_size({"width": 1440, "height": 900})
    settle(pg, 150)
    check("home (replay): the lamps' colours are the board's", pg.evaluate("[...document.querySelectorAll('.card .lamp')].map(l => getComputedStyle(l).backgroundColor)") == [HIT, HIT, MISS, HIT])
    check("home (replay): the losing score is dimmed (#93AB9E), the winning score white",
          pg.evaluate("[...document.querySelectorAll('.cd-runs')].map(r => getComputedStyle(r).color)") == [WHITE, DIM])
    check("home (replay): the slate is fetched once for page.json's date", pg.api.every == [("slate", "2026-10-28")], pg.api.every)
    adv(pg, 10 * 60000)
    check("home (replay): every game final, so the slate is never fetched again", pg.api.every == [("slate", "2026-10-28")], pg.api.every)
    picks = page_json["beat_the_streak"]
    top = pg.evaluate(BTS_TOP_JS)
    check("home rail (replay): Beat the Streak band, 'The best chances of a hit.' (no 'today'), the top three, each linking to its game",
          tc(pg, ".rail h2") == ["Beat the Streak"] and tc(pg, ".rail .subl") == ["The best chances of a hit."]
          and top == [[str(i + 1), p["name"], "Brewers at Rays", "/game/849799", PCT(p["hit"]), f"p {TIER(p['hit'], 'hit')}"] for i, p in enumerate(picks[:3])], top)
    check("home rail: 'All picks' links to /beat-the-streak", tc(pg, ".rail .more a") == ["All picks"] and pg.get_attribute(".rail .more a", "href") == "/beat-the-streak")
    check("home (replay): yellow only on strong numbers and focus rings", yellow_offenders(pg) == [], yellow_offenders(pg))
    out = focus_outline(pg, "a.card")
    check("home: a focused card gets a 3px board-green ring on the dirt", out == ["solid", "3px", GREEN], out)
    check("home: the card is at least 44px tall and keyboard focusable", pg.evaluate("document.activeElement.matches('a.card') && document.activeElement.getBoundingClientRect().height >= 44"))
    pg.evaluate("document.activeElement.blur()")
    sweep(pg, "home (replay) sweep")
    pg.set_viewport_size({"width": 1440, "height": 900})
    settle(pg, 150)
    check_infield(pg, "home (replay) 1440", min_texts=8)
    screenshot(pg, "home-replay-1440.png")
    pg.set_viewport_size({"width": 390, "height": 844})
    settle(pg, 150)
    check_infield(pg, "home (replay) 390", min_texts=8)
    screenshot(pg, "home-replay-390.png")
    ctx.close()

    # ================= the Beat the Streak page (replay) =================
    bts_url = base + "beat-the-streak"
    ctx = new_ctx(browser)
    pg, log = open_page(ctx, bts_url)
    ready(pg, ".picks")
    common(pg, log, bts_url, "Beat the Streak (replay)", "bts", "Beat the Streak: Diamond Metrics", "Beat the Streak: Diamond Metrics")
    check("Beat the Streak: no console messages, no API requests", not log["console"] and pg.api.every == [], (log["console"], pg.api.every))
    check("Beat the Streak: heading, the replay title under it, and the sentence about the contest",
          [tc(pg, "h1"), tc(pg, ".upd"), tc(pg, ".lede")] == [["Beat the Streak"], [site_json["replay_title"]], [BTS_SENTENCE]], [tc(pg, "h1"), tc(pg, ".upd"), tc(pg, ".lede")])
    rows_b = pg.evaluate(BTS_JS)
    exp_b = BTS_ROWS(picks, page_json["games"])
    check("Beat the Streak: all five picks worked out in the test (Brewers 'at Rays', Rays 'vs Brewers', each with a line)",
          [r[3] for r in exp_b] == ["at Rays"] * 3 + ["vs Brewers"] * 2 and all(r[5] for r in exp_b), exp_b)
    check("Beat the Streak: every pick ranked, with team, the link to the game, the night's line, the bat after a hit, and the hit chance", rows_b == exp_b, (rows_b, exp_b))
    check("Beat the Streak: the bat is a 14px image in --ok", pg.evaluate("[...document.querySelectorAll('.picks svg')].every(s => s.getAttribute('width') === '14' && getComputedStyle(s.querySelector('path')).fill === 'rgb(26, 94, 55)')"))
    check("Beat the Streak: no countdown, no game board", pg.locator("#banner, .board, .card").count() == 0)
    sweep(pg, "Beat the Streak (replay) sweep")
    pg.set_viewport_size({"width": 1440, "height": 900})
    settle(pg, 150)
    check_infield(pg, "Beat the Streak 1440", bands=False)
    check("Beat the Streak: yellow only on strong numbers and focus rings", yellow_offenders(pg) == [], yellow_offenders(pg))
    screenshot(pg, "bts-replay-1440.png")
    pg.set_viewport_size({"width": 390, "height": 844})
    settle(pg, 150)
    check_infield(pg, "Beat the Streak 390", bands=False)
    screenshot(pg, "bts-replay-390.png")
    ctx.close()

    # ================= the 404 page =================
    for path in ("nope", "game/abc", "game", "beat-the-streak/x"):
        ctx = new_ctx(browser)
        url = base + path
        pg, log = open_page(ctx, url)
        ready(pg)
        common(pg, log, url, f"404 (/{path})", None, "Page not found: Diamond Metrics", "Page not found: Diamond Metrics", status=404)
        check(f"404 (/{path}): 'Page not found.' and a link to all games", tc(pg, "h1") == ["Page not found."] and tc(pg, "main a") == ["See all games"] and pg.get_attribute("main a", "href") == "/")
        check(f"404 (/{path}): no script, no API requests", pg.locator("script").count() == 0 and pg.api.every == [])
        if path == "nope":
            sweep(pg, "404 sweep")
            pg.set_viewport_size({"width": 1440, "height": 900})
            settle(pg, 150)
            check_infield(pg, "404 1440", min_texts=2, bands=False)
            screenshot(pg, "404-1440.png")
            pg.set_viewport_size({"width": 390, "height": 844})
            settle(pg, 150)
            check_infield(pg, "404 390", min_texts=2, bands=False)
            screenshot(pg, "404-390.png")
        ctx.close()

    # ---------- timezone: the game date must not shift for a viewer far from the US ----------
    ctx = new_ctx(browser, tz="Pacific/Auckland")
    pg, log = open_page(ctx, game_url, fixed="2026-11-18T21:00:00Z")  # 10 AM, Nov 19 in Auckland
    ready(pg)
    check("timezone Auckland: game date on the board unchanged", tc(pg, ".bd-where") == ["Tropicana Field. Wednesday, October 28, 2026."], tc(pg, ".bd-where"))
    check("timezone Auckland: updated line in local time, with its local date", tc(pg, ".upd")[0].startswith("Updated Nov 19, 6:13 AM"), tc(pg, ".upd"))
    check("timezone Auckland: the page title keeps the game's date", pg.title() == "Brewers at Rays, October 28: Diamond Metrics", pg.title())
    pg.goto(base)
    ready(pg, "a.card")
    check("timezone Auckland: countdown uses local date (125 days)", tc(pg, "#banner .dg") == ["1", "2", "5"] and pg.get_attribute("#banner .digits", "aria-label") == "125 days", tc(pg, "#banner .dg"))
    ctx.close()

    # ---------- countdown edge cases ----------
    for fixed, exp_digits, exp_txt, hidden in (("2027-03-23T16:00:00Z", ["1"], "day to Opening Day 2027, Wednesday, March 24.", False),
                                               ("2027-03-24T16:00:00Z", [], "Opening Day 2027 is today, Wednesday, March 24.", False),
                                               ("2027-03-25T16:00:00Z", None, None, True)):
        ctx = new_ctx(browser)
        pg, _ = open_page(ctx, base, fixed=fixed)
        pg.wait_for_selector("h1")
        settle(pg)
        vis = pg.is_visible("#banner")
        t = tc(pg, "#banner .cd-t")
        d = tc(pg, "#banner .dg")
        aria = pg.get_attribute("#banner .digits", "aria-label") if d else None
        ok = (vis != hidden) and (hidden or (t == [exp_txt] and d == exp_digits and (not d or aria == "1 day")))
        check(f"countdown at {fixed[:10]}", ok, (vis, t, d, aria))
        ctx.close()


# =====================================================================
def season_data():
    real = json.load(open(os.path.join(SITE, "data", "page.json"), encoding="utf-8"))
    base = real["games"][0]

    def mk(gid, away, home, status, **kw):
        g = copy.deepcopy(base)
        g.update({"game_id": gid, "away_team": away, "home_team": home, "status": status})
        g.update(kw)
        return g

    outdoor = {"temp_f": 61.4, "humidity_pct": 58, "pressure_inhg": 30.11, "wind_mph": 11.6, "wind_from": "SW", "wind_out_mph": 7.2,
               "wind_label": "Out to CF (7 mph)", "carry_ft": 6.4, "hr_score": 63, "hr_label": "HR Friendly"}
    g1 = mk(1001, "New York Yankees", "Boston Red Sox", "scheduled", park="Fenway Park", roof="open", weather=outdoor,
            start_time_utc="2026-04-14T23:10:00Z", away_score=None, home_score=None, results=None)
    g1["markets"].update({"median_total": 8.5})
    g2 = mk(1002, "Athletics", "Seattle Mariners", "live", park="T-Mobile Park", roof="dome", weather=None,
            start_time_utc="2026-04-14T20:10:00Z", away_score=2, home_score=3, inning=5, inning_half="bottom", outs=2, results=None)
    # 7.5 and 8.5 are equally far from 50% (0.55 and 0.45): the higher line wins
    g2["markets"].update({"away_win": 0.4123, "home_win": 0.5877, "f5_away": 0.3041, "f5_home": 0.5512, "f5_tie": 0.1447, "no_run_first": 0.6233,
                          "total_over": {"6.5": 0.66, "7.0": 0.6, "7.5": 0.55, "8.5": 0.45, "9.5": 0.31}})
    g2["starters"]["away"] = None
    g2["starters"]["home"]["first_inning"] = None
    g2["starters"]["home"]["k_over"] = {"3": 0.9, "4": 0.75, "5": 0.55, "6": 0.45, "7": 0.2}
    # final, open air, with the file's own dew point and elevation (they win over the computed ones)
    g3 = mk(1003, "New York Yankees", "Boston Red Sox", "final", park="Fenway Park", roof="open",
            weather=dict(outdoor, dew_point_f=50.6, elevation_ft=5204),
            start_time_utc="2026-04-14T17:05:00Z", away_score=6, home_score=5)
    g3["markets"]["total_over"] = {"6.5": 0.7, "7.5": 0.6, "8.5": 0.52, "9.5": 0.47, "10.5": 0.35, "11.5": 0.25}
    g3["starters"]["away"]["first_inning"]["whip"] = 1.2345
    g3["results"]["total_runs"] = 11
    g3["results"]["run_in_first"] = True
    g3["results"]["led_after_5"] = "home"
    g3["results"]["away_score"], g3["results"]["home_score"] = 6, 5
    g3["results"]["batters"]["away"][0].update({"h": 2, "tb": 5, "hr": 1, "rbi": 2, "sb": 1})  # every icon in one row
    # Yankees lineup ids in g3 come from the Brewers copy; use the real names so picks can find them
    picks = [
        {"name": "Christian Yelich", "team": "New York Yankees", "opponent": "Boston Red Sox", "home": False, "hit": 0.9966, "game_id": 1001},
        {"name": "Jackson Chourio", "team": "New York Yankees", "opponent": "Boston Red Sox", "home": False, "hit": 0.7314, "game_id": 1003},
        {"name": "Yandy Díaz", "team": "Boston Red Sox", "opponent": "New York Yankees", "home": True, "hit": 0.7148, "game_id": 1003},
        {"name": "Chandler Simpson", "team": "Boston Red Sox", "opponent": "New York Yankees", "home": True, "hit": 0.7052},
        {"name": "Nobody Atall", "team": "Seattle Mariners", "opponent": "Athletics", "home": True, "hit": 0.004},
    ]
    return {"date": "2026-04-14", "updated_at": "2026-04-14T15:00:00Z", "model_version": "v4", "note": "live", "games": [g1, g2, g3], "beat_the_streak": picks}


SEASON_API = {"1001": "linescore-preview.json", "1002": "linescore-live.json", "1003": "linescore-final-11.json",
              "boxscore/1001": "boxscore-preview.json", "boxscore/1002": "boxscore-live.json", "boxscore/1003": "boxscore-final-opener.json",
              "series/1001": "series-first.json", "series/1002": "series-tied.json", "series/1003": "series-season-sweep.json",
              "standings/1001": "standings-third.json", "standings/1002": "standings-tied.json", "standings/1003": "standings-missing.json",
              "battery/1001": "battery-no-catcher.json", "battery/1002": "battery-live.json", "battery/1003": "battery-final-opener.json",
              "slate/2026-04-14": "slate-season.json"}


def board_plates(pg):
    return [[x.strip() for x in pg.locator(".bd tbody tr").nth(i).locator(".plate").all_text_contents()] for i in range(2)]


def readings(w, home_code):
    """The six open-air readings as textContent: label, value, and the wind note."""
    dew = int(HU(Decimal(str(w["dew_point_f"])))) if w.get("dew_point_f") is not None else DEW(w["temp_f"], w["humidity_pct"])
    elev = int(HU(Decimal(str(w["elevation_ft"])))) if w.get("elevation_ft") is not None else FT(ELEV_M[home_code])
    return [f"Temperature{int(HU(Decimal(str(w['temp_f']))))}°F", f"Humidity{w['humidity_pct']}%", f"Dew point{dew}°F",
            f"Wind{int(HU(Decimal(str(w['wind_mph']))))} mph from {w['wind_from']}{w['wind_label']}", f"Elevation{elev:,} ft",
            f"Carry+{int(HU(Decimal(str(w['carry_ft']))))} ft"]


def write_page(pj, data):
    json.dump(data, open(pj, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def season_cards(g1, g2, g3, final=False):
    """Expected home cards for slate-season.json (final=True: slate-season-final.json, where the A's game has ended 4-3)."""
    live = ["In progress", [CARD(g2, "Top 6th, 1 out", "Athletics at Mariners, top 6th, 1 out, 2 to 3. Full game.", ["6–10", "11–5"], [2, 3], live=True)]]
    later = ["Later today", [CARD(g1, "7:10 PM EDT", "Yankees at Red Sox, first pitch 7:10 PM EDT. Full game.", ["9–7", "10–6"])]]
    fin3 = CARD(g3, "Final", "Yankees at Red Sox, final, 6 to 5. Full game.", ["9–7", "10–6"], [6, 5], foot="final")
    if final:
        fin2 = CARD(g2, "Final", "Athletics at Mariners, final, 4 to 3. Full game.", ["7–10", "11–6"], [4, 3], foot="final")
        return [later, ["Final", [fin3, fin2]]]
    return [live, later, ["Final", [fin3]]]


def test_season(browser):
    root = os.path.join(tempfile.mkdtemp(prefix="dm_season_"), "site")
    shutil.copytree(SITE, root)
    os.remove(os.path.join(root, "data", "site.json"))  # in season: no countdown file
    os.chmod(os.path.join(root, "data"), 0o755)
    pj = os.path.join(root, "data", "page.json")
    os.chmod(pj, 0o644)
    d = season_data()
    write_page(pj, d)
    g1, g2, g3 = d["games"]
    srv = start_server(root, PORT_SEASON)
    try:
        base = f"http://127.0.0.1:{PORT_SEASON}/"
        game = lambda gid: f"{base}game/{gid}"

        # ================= home, in season =================
        ctx = new_ctx(browser, 1440, 900)
        pg, log = open_page(ctx, base, fixed="2026-04-14T14:00:00Z", api=Api(SEASON_API))
        ready(pg, "a.card")
        common(pg, log, base, "home (season)", "games", "Diamond Metrics: MLB predictions", "Diamond Metrics: MLB predictions")
        check("home (season): countdown hidden when site.json is missing (the 404 page answers for it)", not pg.is_visible("#banner"))
        check("home (season): h1 is the long date without the year; games and the updated time (same day: no date)",
              tc(pg, "h1") == ["Tuesday, April 14"] and tc(pg, ".upd") == ["3 games. Updated 11:00 AM EDT."] and pg.locator(".notice, .lede").count() == 0, [tc(pg, "h1"), tc(pg, ".upd")])
        got = cards(pg)
        exp = season_cards(g1, g2, g3)
        check("home (season): In progress, Later today, Final in that order; each card's status, park, records, plates, runs, footer and name",
              got == exp, (got, exp))
        check("home (season): the live card has a lit white lamp", pg.evaluate("[...document.querySelectorAll('.cd-st .lamp.on')].map(l => getComputedStyle(l).backgroundColor)") == [WHITE])
        check("home (season): a game on the slate that isn't in page.json gets no card", pg.locator("a.card").count() == 3 and pg.locator("a.card[href='/game/9999']").count() == 0)
        check("home (season): the win-chance minis carry the board tier; the final game's lamps are the board's",
              classes(pg, ".card .mini") == [f"mini {BT(gg['markets'][k], 'ml')}" for gg in (g2, g1, g3) for k in ("away_win", "home_win")])
        check("home (season): the 'Today's games' band and 'The best chances of a hit today.' in the rail",
              tc(pg, ".games > h2") == ["Today's games"] and tc(pg, ".rail .subl") == ["The best chances of a hit today."], [tc(pg, ".games > h2"), tc(pg, ".rail .subl")])
        top = pg.evaluate(BTS_TOP_JS)
        check("home rail (season): the top three link to their own games", [r[2:4] for r in top] == [["Yankees at Red Sox", "/game/1001"], ["Yankees at Red Sox", "/game/1003"], ["Yankees at Red Sox", "/game/1003"]], top)
        check("home (season): the slate is requested once on load", pg.api.every == [("slate", "2026-04-14")], pg.api.every)
        check("home (season): yellow only on strong numbers and focus rings", yellow_offenders(pg) == [], yellow_offenders(pg))
        fits = """() => [...document.querySelectorAll('a.card')].flatMap((c, i) => { const r = c.getBoundingClientRect(), bad = [];
            if (c.scrollWidth > c.clientWidth) bad.push(i + ': scrollWidth');
            for (const k of c.querySelectorAll('*')) { if (k.closest('.sr')) continue; const b = k.getBoundingClientRect(); if (b.width && (b.left < r.left - 0.5 || b.right > r.right + 0.5)) bad.push(i + ': ' + k.className); }
            return bad; })"""
        # each group's grid: how many columns it lays out (every group has one card here, so count the tracks)
        cols_js = "[...new Set([...document.querySelectorAll('.cards')].map(c => getComputedStyle(c).gridTemplateColumns.split(' ').length))].join()"
        for w, n in ((1440, 2), (1280, 2), (768, 1), (390, 1), (360, 1)):
            pg.set_viewport_size({"width": w, "height": 900})
            settle(pg, 150)
            check(f"home (season) {w}: {n} card(s) across, nothing overflows a card", pg.evaluate(cols_js) == str(n) and not pg.evaluate(fits), (pg.evaluate(cols_js), pg.evaluate(fits)))
            check(f"home (season) {w}: every card's footer (lamps or lines, and 'Full game') is one row", pg.evaluate(ONE_ROW_JS) == [], pg.evaluate(ONE_ROW_JS))
        sweep(pg, "home (season) sweep")
        pg.set_viewport_size({"width": 1440, "height": 900})
        settle(pg, 150)
        check_infield(pg, "home (season) 1440", min_texts=8)
        screenshot(pg, "home-season-1440.png")
        pg.set_viewport_size({"width": 390, "height": 844})
        settle(pg, 150)
        screenshot(pg, "home-season-390.png")
        ctx.close()

        # the slate refresh: every 30 s while a game is live, paused while hidden; a live game that ends moves to Final
        # (focus goes with the card); then quiet until 15 minutes before the next first pitch
        ctx = new_ctx(browser)
        api = Api(SEASON_API)
        pg, _ = open_page(ctx, base, fixed="2026-04-14T21:00:00Z", api=api, install=True)
        pg.wait_for_selector("a.card")
        settle(pg)
        n = lambda: api.count("2026-04-14", "slate")
        c0 = n()
        adv(pg, 29000)
        c1 = n()
        adv(pg, 1500)
        c2 = n()
        set_hidden(pg, True)
        adv(pg, 5 * 60000)
        c3 = n()
        set_hidden(pg, False)
        c4 = n()
        check("home slate: on load, then every 30 seconds while a game is live; none while hidden; at once when visible again",
              (c0, c1, c2, c3, c4) == (1, 1, 2, 2, 3), (c0, c1, c2, c3, c4))
        pg.focus("a.card[href='/game/1002']")
        api.table["slate/2026-04-14"] = "slate-season-final.json"
        adv(pg, 30500)
        got = cards(pg)
        check("home slate: the A's game ended, so its card moves to Final with the result (no lamps: page.json still says live)",
              got == season_cards(g1, g2, g3, final=True), got)
        check("home slate: keyboard focus stays on the card that moved", pg.evaluate("document.activeElement.getAttribute('href')") == "/game/1002")
        c5 = n()
        adv(pg, 60 * 60000)
        check("home slate: nothing live (the slate's own extra live game doesn't count), so no refresh for an hour", n() == c5, (c5, n()))
        ctx.close()

        # only a preview game: quiet until 15 minutes before first pitch, then every 30 seconds
        dd = season_data()
        dd["games"] = [copy.deepcopy(g1)]
        write_page(pj, dd)
        ctx = new_ctx(browser)
        api = Api({"slate/2026-04-14": "slate-season.json"})
        pg, _ = open_page(ctx, base, fixed="2026-04-14T22:40:00Z", api=api, install=True)  # first pitch 23:10
        pg.wait_for_selector("a.card")
        settle(pg)
        n = lambda: api.count("2026-04-14", "slate")
        p0 = n()
        adv(pg, 14 * 60000)
        p1 = n()          # 22:54
        adv(pg, 61000)
        p2 = n()          # 22:55:01, the window opened
        adv(pg, 30500)
        p3 = n()
        check("home slate: one preview game: nothing until 15 minutes before first pitch, then every 30 seconds", (p0, p1, p2, p3) == (1, 1, 2, 3), (p0, p1, p2, p3))
        ctx.close()

        # a failed slate fetch leaves the cards as page.json describes them, and the live game keeps the refresh going
        write_page(pj, season_data())
        ctx = new_ctx(browser)
        api = Api({**SEASON_API, "slate/2026-04-14": 500})
        pg, log = open_page(ctx, base, fixed="2026-04-14T21:00:00Z", api=api, install=True)
        pg.wait_for_selector("a.card")
        settle(pg)
        got = cards(pg)
        exp = [["In progress", [CARD(g2, "Bottom 5th, 2 outs", "Athletics at Mariners, bottom 5th, 2 outs, 2 to 3. Full game.", None, [2, 3], live=True)]],
               ["Later today", [CARD(g1, "7:10 PM EDT", "Yankees at Red Sox, first pitch 7:10 PM EDT. Full game.")]],
               ["Final", [CARD(g3, "Final", "Yankees at Red Sox, final, 6 to 5. Full game.", None, [6, 5], foot="final")]]]
        check("home slate 500: the cards are page.json's (live inning and scores from the file, no records), no error", got == exp and pg.locator("[role=alert]").count() == 0 and not log["errors"], (got, exp))
        adv(pg, 30500)
        api.table["slate/2026-04-14"] = "slate-season.json"
        adv(pg, 30500)
        check("home slate 500: retried every 30 seconds (a game is live in page.json); the next good answer fills the cards",
              api.count("2026-04-14", "slate") == 3 and cards(pg) == season_cards(g1, g2, g3), api.every)
        ctx.close()

        # an update from the day before keeps its date
        dd = season_data()
        dd["updated_at"] = "2026-04-13T22:00:00Z"
        write_page(pj, dd)
        ctx = new_ctx(browser)
        pg, _ = open_page(ctx, base, fixed="2026-04-14T14:00:00Z", api=Api(SEASON_API))
        ready(pg, "a.card")
        check("home (season): an update made the day before the slate's date keeps its date", tc(pg, ".upd") == ["3 games. Updated Apr 13, 6:00 PM EDT."], tc(pg, ".upd"))
        ctx.close()
        write_page(pj, season_data())

        # ================= the game pages, in season =================
        ctx = new_ctx(browser, 1440, 900)
        url = game(1001)
        pg, log = open_page(ctx, url, fixed="2026-04-14T14:00:00Z", api=Api(SEASON_API), install=True)
        ready(pg, ".og li")
        common(pg, log, url, "game page (season)", "games", "Yankees at Red Sox, April 14: Diamond Metrics", "MLB game: Diamond Metrics")
        check("game page (season): h1 is the matchup, with the long date (no year) and the updated time under it",
              tc(pg, "h1") == ["Yankees at Red Sox"] and tc(pg, ".upd") == ["Tuesday, April 14. Updated 11:00 AM EDT."], [tc(pg, "h1"), tc(pg, ".upd")])
        og = pg.evaluate("[...document.querySelectorAll('.og a')].map(a => [a.getAttribute('href'), a.querySelector('.og-t').textContent, a.querySelector('.og-st').textContent])")
        check("game page (season): Other games: every other game by first pitch, teams and the slate's score, each a link to its page",
              tc(pg, ".others h2") == ["Other games"] and og == [["/game/1003", "Yankees at Red Sox", "Final 6–5"], ["/game/1002", "Athletics at Mariners", "Live 2–3"]], og)
        check("game page (season): the rail is Other games, then the cut-offs; no Beat the Streak",
              pg.evaluate("[...document.querySelector('.rail').children].map(e => e.tagName + '.' + e.className)") == ["SECTION.sec others", "DETAILS.how"] and pg.locator(".bts").count() == 0)
        check("game page (season): the game's five endpoints once each, and the slate once",
              sorted(pg.api.every) == sorted([(e, "1001") for e in ("battery", "boxscore", "linescore", "series", "standings")] + [("slate", "2026-04-14")]), pg.api.every)
        adv(pg, 10 * 60000)
        check("game page (season): the slate is not polled", pg.api.count("2026-04-14", "slate") == 1, pg.api.every)
        ctx.close()
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, url, fixed="2026-04-14T14:00:00Z", api=Api({**SEASON_API, "slate/2026-04-14": 500}))
        ready(pg, ".og li")
        og = pg.evaluate("[...document.querySelectorAll('.og a')].map(a => a.querySelector('.og-st').textContent)")
        check("game page (season): slate 500: each other game shows what page.json has (first pitch before a game)", og == ["Final 6–5", "Live 2–3"] and not log["errors"], og)
        ctx.close()
        dd = season_data()
        dd["games"][1]["status"] = "scheduled"
        dd["games"][2]["status"] = "scheduled"
        write_page(pj, dd)
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, url, fixed="2026-04-14T14:00:00Z", api=Api({**SEASON_API, "slate/2026-04-14": 500}))
        ready(pg, ".og li")
        og = pg.evaluate("[...document.querySelectorAll('.og a')].map(a => a.querySelector('.og-st').textContent)")
        check("game page (season): slate 500 with every other game scheduled in page.json: their first-pitch times", og == ["1:05 PM EDT", "4:10 PM EDT"], og)
        ctx.close()
        write_page(pj, season_data())

        ctx = new_ctx(browser, 1440, 900)
        pg, log = open_page(ctx, game(1001), fixed="2026-04-14T14:00:00Z", api=Api(SEASON_API))
        ready(pg, ".board")
        # game 1: preview, outdoor weather (dew point and elevation computed)
        check("season game 1: top line gives first pitch", tc(pg, ".bd-where") == ["Fenway Park. First pitch 7:10 PM EDT."], tc(pg, ".bd-where"))
        check("season game 1: before the game, empty plates and dashes", ls_rows(pg) == LS(fixture("linescore-preview.json"), "NYY", "BOS") == LS(None, "NYY", "BOS"), ls_rows(pg))
        check("season game 1: no live line, no outline", pg.locator(".bd-live").count() == 0 and pg.locator(".ls .ip.cur").count() == 0)
        l1, v1 = OU(g1["markets"]["total_over"])
        check("season game 1: board numbers", board_plates(pg) == [[PCT(g1["markets"]["away_win"]), PCT(g1["markets"]["f5_away"]), v1[0]], [PCT(g1["markets"]["home_win"]), PCT(g1["markets"]["f5_home"]), v1[1]]], board_plates(pg))
        bx1 = fixture("boxscore-preview.json")
        check("season game 1: the catcher shows before the game; 'C not announced' where the lineup isn't in; no line yet",
              battery(pg) == [BATTERY(g1, s, bx1, "preview") for s in ("away", "home")] == [["SP Jacob Misiorowski", "C not announced", None, None], ["SP Drew Rasmussen", "C Connor Wong", None, None]], battery(pg))
        s1, b1 = fixture("standings-third.json"), fixture("battery-no-catcher.json")
        check("season game 1: 3rd and 11 games back; 1 game up; no battery line without a catcher; a first start together",
              extras(pg) == EXTRAS(s1, b1) == [["87–75", "3rd in the AL East, 11 games back.", "30–22 in the division.", None],
                                                ["92–70", "1st in the AL East, 1 game up.", "40–12 in the division.", "First start together this season"]], (extras(pg), EXTRAS(s1, b1)))
        check("season game 1: stolen-base rows with the probable pitchers", running(pg) == RUNNING(b1), running(pg))
        check("season game 1: first meeting", strip(pg) == SERIES_LINES(g1, fixture("series-first.json")) == ["Season series: first meeting"], strip(pg))
        check("season game 1: four unlit lamps before the game", lamps(pg) == lamps_expect(g1) and [s for _, s, _ in lamps(pg)] == ["unlit"] * 4, (lamps(pg), lamps_expect(g1)))
        check("season game 1: dew point by Magnus is 47°F; Fenway's 6 m is 20 ft", DEW(61.4, 58) == 47 and FT(ELEV_M["BOS"]) == 20)
        wx = tc(pg, ".wx > div")
        check("season game 1: six readings, computed dew point and elevation", wx == readings(g1["weather"], "BOS"), (wx, readings(g1["weather"], "BOS")))
        check("season game 1: bar label, hot bar in yellow", tc(pg, ".hrbox .hrrow > *") == ["HR Friendly", "63 out of 100"] and css(pg, ".fill.hot", "backgroundColor") == YELLOW, tc(pg, ".hrbox .hrrow > *"))
        check("season game 1 (preview, hot bar): yellow only on strong numbers, the hot HR bar and focus rings", yellow_offenders(pg) == [], yellow_offenders(pg))
        check("season game 1: totals sub line without a result; lineups without a Result column or icons",
              tc(pg, ".totals .subl") == ["Chance the game goes over each line."] and pg.locator("table.t").first.locator("thead th").count() == 7 and pg.locator("table.t svg").count() == 0)
        check("season game 1: no 'null', 'undefined' or 'NaN' in the page", stray_words(pg) == [], stray_words(pg))
        ctx.close()

        # game 2: live from the API (top of the 6th; page.json still says bottom 5th), dome, missing weather and starter
        url = game(1002)
        ctx = new_ctx(browser, 1440, 900)
        pg, log = open_page(ctx, url, fixed="2026-04-14T14:00:00Z", api=Api(SEASON_API))
        ready(pg, ".bd-live")
        live = fixture("linescore-live.json")
        check("season game 2: the page title is the matchup and the date", pg.title() == "Athletics at Mariners, April 14: Diamond Metrics", pg.title())
        check("season game 2: line score from the API", ls_rows(pg) == LS(live, "ATH", "SEA"), (ls_rows(pg), LS(live, "ATH", "SEA")))
        cur = pg.evaluate("(() => { const c = document.querySelectorAll('.ls .ip.cur'); if (c.length !== 1) return c.length; const td = c[0].closest('td'), tr = td.closest('tr'); const cs = getComputedStyle(c[0]); return [tr.querySelector('th').textContent, [...tr.querySelectorAll('td')].indexOf(td) + 1, cs.outlineStyle, cs.outlineWidth, cs.outlineColor]; })()")
        check("season game 2: the half being played has a 2px white outline (ATH, 6th)", cur == ["ATH", 6, "solid", "2px", WHITE], cur)
        check("season game 2: live line from the API, with a lit lamp", tc(pg, ".bd-live") == ["Live, top 6th, 1 out"] and pg.locator(".bd-live .lamp.on").count() == 1, tc(pg, ".bd-live"))
        check("season game 2: top line gives the date for a live game", tc(pg, ".bd-where") == ["T-Mobile Park. Tuesday, April 14, 2026."], tc(pg, ".bd-where"))
        l2, v2 = OU(g2["markets"]["total_over"])
        check("season game 2: O/U tie goes to the higher line (8.5, over 45, under 55)", l2 == ["Over 8.5", "Under 8.5"] and v2 == ["45%", "55%"] and tc(pg, ".bd .cap") == l2, (l2, v2, tc(pg, ".bd .cap")))
        check("season game 2: board numbers", board_plates(pg) == [[PCT(0.4123), PCT(0.3041), v2[0]], [PCT(0.5877), PCT(0.5512), v2[1]]], board_plates(pg))
        check("season game 2: unlit lamps while live (Mariners, Mariners, Under 8.5, NRFI)", [t for t, _, _ in lamps(pg)] == ["Winner Mariners", "First 5 Mariners", "Total Under 8.5", "NRFI 62%"]
              and [s for _, s, _ in lamps(pg)] == ["unlit"] * 4, lamps(pg))
        check("season game 2: no K marks while the game is live", kmarks(pg) == [[], KMARKS(g2, "home")] and pg.locator(".kmark").count() == 0, kmarks(pg))
        check("season game 2: forecast missing message", tc(pg, ".cond-note") == ["The forecast for this park was not available when the prediction ran."], tc(pg, ".cond-note"))
        check("season game 2: starter not announced + first-inning fallback", tc(pg, ".pit .nm") == ["Starter not announced", "Drew Rasmussen"] and tc(pg, ".fi-none") == ["First-inning record not shown (fewer than 5 starts)."])
        bx2 = fixture("boxscore-live.json")
        check("season game 2: live lines; the actual starter is named where page.json had none",
              battery(pg) == [BATTERY(g2, s, bx2, "live") for s in ("away", "home")]
              == [["SP not announced", "C Shea Langeliers", "Started: JP Sears", ["IP5.1", "H4", "BB2", "SO6"]], ["SP Drew Rasmussen", "C Cal Raleigh", None, ["IP6.0", "H3", "BB1", "SO7"]]], battery(pg))
        s2, b2 = fixture("standings-tied.json"), fixture("battery-live.json")
        check("season game 2: tied for 3rd (2.5 back) and tied for 1st; battery records",
              extras(pg) == EXTRAS(s2, b2) == [["80–82", "Tied for 3rd in the AL West, 2.5 games back.", "20–25 in the division.", "Battery 3–4 this season"],
                                                ["85–77", "Tied for 1st in the AL West.", "28–24 in the division.", "Battery 12–9 this season"]], (extras(pg), EXTRAS(s2, b2)))
        check("season game 2 at 1440: the longest standings line ('Tied for 3rd …, 2.5 games back.') fits on one line", single_lines(pg, ".bd .bl-st") and single_lines(pg, ".bd .bl-dr"))
        check("season game 2: no attempts against a catcher; '1 pickoff' and '(2 teams)' for a pitcher", running(pg) == RUNNING(b2), running(pg))
        check("season game 2: tied series", strip(pg) == SERIES_LINES(g2, fixture("series-tied.json")) == ["Season series: tied 1–1"], strip(pg))
        og = pg.evaluate("[...document.querySelectorAll('.og a')].map(a => a.getAttribute('href'))")
        check("season game 2: Other games lists the other two, by first pitch", og == ["/game/1003", "/game/1001"], og)
        check("season game 2 (live): yellow only on strong numbers, the hot HR bar and focus rings", yellow_offenders(pg) == [], yellow_offenders(pg))
        check("season game 2: no 'null', 'undefined' or 'NaN' in the page", stray_words(pg) == [], stray_words(pg))
        sweep(pg, "game page (season, live) sweep")
        pg.set_viewport_size({"width": 1440, "height": 900})
        settle(pg, 150)
        check_infield(pg, "game page (season, live) 1440")
        pg.evaluate("document.querySelectorAll('details').forEach(d => d.open = false)")
        screenshot(pg, "game-season-1440.png")
        pg.set_viewport_size({"width": 390, "height": 844})
        settle(pg, 150)
        check("game page (season) 390: nine innings fit without scrolling (live game)", pg.evaluate("(() => { const s = document.querySelector('.ls-scroll'); return s.scrollWidth <= s.clientWidth; })()"))
        screenshot(pg, "game-season-390.png")
        ctx.close()

        # game 3: an 11-inning final, file dew point and elevation, WHIP, an opener, every icon
        ctx = new_ctx(browser, 1440, 900)
        pg, log = open_page(ctx, game(1003), fixed="2026-04-14T14:00:00Z", api=Api(SEASON_API))
        ready(pg, "table.t svg")
        f11 = fixture("linescore-final-11.json")
        r3 = g3["results"]
        check("season game 3: eleven innings, header 1 to 11", ls_head(pg) == ["Team"] + [str(i) for i in range(1, 12)] + ["R", "H", "E"], ls_head(pg))
        check("season game 3: every plate of the 11-inning final", ls_rows(pg) == LS(f11, "NYY", "BOS"), (ls_rows(pg), LS(f11, "NYY", "BOS")))
        l3, v3 = OU(g3["markets"]["total_over"])
        check("season game 3: O/U line 8.5 (0.52), over 52 and under 48", l3 == ["Over 8.5", "Under 8.5"] and v3 == ["52%", "48%"] and [p[2] for p in board_plates(pg)] == v3, (l3, v3, board_plates(pg)))
        bx3 = fixture("boxscore-final-opener.json")
        check("season game 3: an opener started for the Red Sox, so his line is labelled 'Started: Kevin Kelly'",
              battery(pg) == [BATTERY(g3, s, bx3, "final") for s in ("away", "home")]
              == [["SP Jacob Misiorowski", "C Austin Wells", None, ["IP6.0", "H5", "BB2", "SO8"]], ["SP Drew Rasmussen", "C Carlos Narváez", "Started: Kevin Kelly", ["IP1.0", "H0", "BB0", "SO1"]]], battery(pg))
        s3, b3 = fixture("standings-missing.json"), fixture("battery-final-opener.json")
        check("season game 3: no division record means only the first standings line; a team missing from the standings shows none",
              extras(pg) == EXTRAS(s3, b3) == [["87–75", "2nd in the AL East, 5 games back.", None, "Battery 5–2 this season"], [None, None, None, "First start together this season"]], (extras(pg), EXTRAS(s3, b3)))
        check("season game 3: a K under each total reached (the old check counts)",
              kmarks(pg) == [KMARKS(g3, "away"), KMARKS(g3, "home")] and pg.locator(".kmark").count() == 2, kmarks(pg))
        burst = pg.evaluate("""(() => { const s = document.querySelector('table.t svg[aria-label="homered"]'); if (!s) return null;
            const a = s.getAnimations(), t = a.length ? a[0].effect.getTiming() : {};
            return [s.classList.contains('pop'), getComputedStyle(s.querySelector('path')).stroke, a.length, t.duration, t.iterations, getComputedStyle(s).animationName]; })()""")
        check("season game 3: the homer burst, labelled 'homered' in --ok, pops once (one 500 ms animation, one iteration)", burst == [True, OK_GREEN, 1, 500, 1, "burst-pop"], burst)
        settle(pg, 700)
        done = pg.evaluate("document.querySelector('table.t svg[aria-label=\"homered\"]').getAnimations().map(a => a.playState)")
        check("season game 3: the pop has finished (it ran once and stopped)", done == ["finished"], done)
        check("season game 3: the opener's stolen-base row is his own", running(pg) == RUNNING(b3) and running(pg)[1][0] == "Kevin Kelly 1 stolen, 0 caught, 0 pickoffs", running(pg))
        check("season game 3: completed season sweep and its series sweep",
              strip(pg) == SERIES_LINES(g3, fixture("series-season-sweep.json")) == ["Season series: Red Sox 3–0", "Red Sox swept a 3-game series, June 30–July 2.", "Red Sox swept the season series 3–0."], strip(pg))
        check("season game 3: lamps graded on page.json", lamps(pg) == lamps_expect(g3) and [x[:2] for x in lamps(pg)] == [("Winner Yankees", "hit"), ("First 5 Yankees", "miss"), ("Total Over 8.5", "hit"), ("NRFI 55%", "miss")], lamps(pg))
        by3 = {r["id"]: r for s in ("away", "home") for r in g3["results"]["batters"][s]}
        icons3 = [l for s in ("away", "home") for row in lineup_expect(g3, s, by3)[1] for l in row]
        shown3 = pg.evaluate("[...document.querySelectorAll('table.t svg')].map(s => s.getAttribute('aria-label'))")
        check("season game 3: every result icon, in order, with all five kinds", shown3 == icons3 and set(shown3) == set(ICON_LABEL.values()), (shown3, icons3))
        shapes = pg.evaluate("""Object.fromEntries([...document.querySelectorAll('table.t svg')].map(s => [s.getAttribute('aria-label'),
            [...s.children].map(c => c.tagName + (c.getAttribute('d') || '')).join('|')]))""")
        check("season game 3: four drawings for five labels", len(shapes) == 5 and shapes["got a hit"] == shapes["two or more bases"] and len(set(shapes.values())) == 4, shapes)
        burst_shape = pg.evaluate("""(() => { const s = document.querySelector('table.t svg[aria-label="homered"]');
            const dot = s.querySelector('circle'), rays = [...s.querySelectorAll('path')].flatMap(p => p.getAttribute('d').match(/M[^M]+/g));
            const len = r => { const n = r.match(/-?[\\d.]+/g).map(Number); return Math.hypot(n[2] - n[0], n[3] - n[1]) || Math.abs(n[2] - n[1]); };
            const start = r => { const n = r.match(/-?[\\d.]+/g).map(Number); return Math.hypot(n[0] - 8, n[1] - 8); };
            return [dot ? Number(dot.getAttribute('r')) : 0, rays.length, Math.min(...rays.map(start)), [...new Set(rays.map(r => Math.round(len(r) * 2) / 2))].length]; })()""")
        check("season game 3: the burst is a centre dot and ten rays starting clear of it, in two lengths", burst_shape[0] > 0 and burst_shape[1] == 10 and burst_shape[2] > 3 and burst_shape[3] == 2, burst_shape)
        icon_row = pg.locator("table.t").first.locator("tbody tr").first
        check("season game 3: one lineup row shows every icon (bat, bat, burst, plate, base)",
              icon_row.locator("svg").evaluate_all("els => els.map(e => e.getAttribute('aria-label'))") == ["got a hit", "two or more bases", "homered", "drove in a run", "stole a base"])
        os.makedirs(SCREENS, exist_ok=True)
        icon_row.screenshot(path=os.path.join(SCREENS, "icons-row-1x.png"))
        wx3 = tc(pg, ".wx > div")
        check("season game 3: the file's dew point and elevation win", wx3 == readings(g3["weather"], "BOS") and "Dew point51°F" in wx3 and "Elevation5,204 ft" in wx3, wx3)
        st3 = [[norm(x) for x in pg.locator(".pit").nth(i).locator(".fi .stats > div").all_text_contents()] for i in range(2)]
        check("season game 3: WHIP shows when the data has it", st3[0][-1] == "WHIP1.23" and len(st3[1]) == 3, st3)
        lines = sorted((k for k in g3["markets"]["total_over"] if k.endswith(".5")), key=float)
        marks = [pg.locator(".tot-row").nth(i).locator("svg").count() == 1 for i in range(len(lines))]
        check("season game 3: a check after each total-runs line that went over (the checks stay)", marks == [r3["total_runs"] > float(k) for k in lines] and sum(marks) == 5, marks)
        check("season game 3: no console errors", not log["errors"], log["errors"])
        sweep(pg, "11-inning sweep")
        pg.set_viewport_size({"width": 390, "height": 844})
        settle(pg, 150)
        sc = pg.evaluate("(() => { const s = document.querySelector('.ls-scroll'); return [s.scrollWidth > s.clientWidth, s.tabIndex, s.getAttribute('role'), s.getAttribute('aria-label')]; })()")
        check("season 390: extra innings scroll inside a focusable wrapper", sc == [True, 0, "region", "Line score"], sc)
        ctx.close()

        # the live refresh never redraws the lineups, so the burst doesn't pop again
        ctx = new_ctx(browser)
        pg, _ = open_page(ctx, game(1003), fixed="2026-04-14T21:00:00Z", api=Api({**SEASON_API, "1003": "linescore-live.json"}), install=True)
        pg.wait_for_selector("table.t svg[aria-label='homered']")
        settle(pg)
        pg.evaluate("document.querySelector('table.t svg[aria-label=\"homered\"]').dataset.first = 'yes'")
        adv(pg, 30500)
        adv(pg, 30500)
        again = pg.evaluate("(() => { const s = document.querySelectorAll('table.t svg[aria-label=\"homered\"]'); return [s.length, s[0].dataset.first || null]; })()")
        check("burst: two 30-second refreshes later it is the same element (drawn once, so it popped once)", again == [1, "yes"] and pg.api.count(1003) == 3, (again, pg.api.count(1003)))
        ctx.close()

        # reduced motion: the burst is drawn with no animation at all
        rctx = new_ctx(browser, reduced_motion="reduce")
        rpg, _ = open_page(rctx, game(1003), fixed="2026-04-14T14:00:00Z", api=Api({"1003": "linescore-final-x.json"}))
        rpg.wait_for_selector("table.t svg[aria-label='homered']")
        settle(rpg, 300)
        still = rpg.evaluate("(() => { const s = document.querySelector('table.t svg[aria-label=\"homered\"]'); return [s.classList.contains('pop'), getComputedStyle(s).animationName, s.getAnimations().length]; })()")
        check("burst under prefers-reduced-motion: reduce: no animation at all", still == [True, "none", 0], still)
        rctx.close()

        # one game at a time, each with its own line-score fixture
        def one(gm, spec, fixed="2026-04-14T14:00:00Z", install=False, box=None, series=None, more=None):
            dd = season_data()
            dd["games"] = [copy.deepcopy(gm)]
            write_page(pj, dd)
            c = new_ctx(browser)
            pk = str(gm["game_id"])
            table = {pk: spec}
            if box:
                table["boxscore/" + pk] = box
            if series:
                table["series/" + pk] = series
            for ep, name in (more or {}).items():
                table[ep + "/" + pk] = name
            p, lg = open_page(c, game(pk), fixed=fixed, api=Api(table), install=install)
            p.wait_for_selector(".board")
            settle(p, 300)
            return c, p, lg

        for name, exp in (("series-sweep.json", ["Season series: Yankees 4–2", "Red Sox swept a 2-game series, June 30–July 1.", "Yankees swept a 3-game series, July 7–9."]),
                          ("series-winless.json", ["Season series: Red Sox 2–0", "Yankees haven't beaten the Red Sox this season (0–2, 1 game left)."]),
                          ("series-postseason.json", ["Season series: tied 3–3", "ALCS: Red Sox lead 3–2."]),
                          ("series-postseason-tied.json", ["Season series: tied 3–3", "NLCS: tied 2–2."])):
            ctx, pg, _ = one(g3, "linescore-final-x.json", series=name)
            check(f"series strip {name[7:-5]}: worked out in the test and shown", strip(pg) == SERIES_LINES(g3, fixture(name)) == exp, (strip(pg), SERIES_LINES(g3, fixture(name))))
            ctx.close()

        ctx, pg, _ = one(g3, "linescore-final-x.json")
        fx = fixture("linescore-final-x.json")
        check("X: the home team's 9th shows X, and only one X", ls_rows(pg) == LS(fx, "NYY", "BOS") and ls_rows(pg)[1][1][8] == "X" and sum(c == "X" for r in ls_rows(pg) for c in r[1]) == 1, ls_rows(pg))
        ctx.close()

        ev = copy.deepcopy(g3)
        ev["markets"].update({"away_win": 0.5, "home_win": 0.5, "f5_away": 0.4, "f5_home": 0.4, "f5_tie": 0.2, "no_run_first": 0.5,
                              "total_over": {"6.5": 0.7, "7.5": 0.5, "8.5": 0.3}})
        ctx, pg, _ = one(ev, "linescore-final-x.json")
        check("even: 'Winner even', 'First 5 even', 'Total 7.5 even', 'NRFI 50%', no lamps", [t for t, _ in PICKS(ev)] == ["Winner even", "First 5 even", "Total 7.5 even", "NRFI 50%"]
              and lamps(pg) == lamps_expect(ev) and pg.locator(".lr .lamp").count() == 0, lamps(pg))
        ctx.close()
        ev2 = copy.deepcopy(g2)
        ev2["markets"].update({"f5_away": 0.3, "f5_home": 0.3, "f5_tie": 0.3})
        ctx, pg, _ = one(ev2, "linescore-live.json")
        check("even: a tie for the top First 5 outcome has no pick and no lamp; the others stay unlit", lamps(pg) == lamps_expect(ev2) and lamps(pg)[1] == ("First 5 even", None, ""), lamps(pg))
        ctx.close()
        yr = copy.deepcopy(g3)
        yr["markets"].update({"no_run_first": 0.45, "f5_away": 0.3, "f5_home": 0.3, "f5_tie": 0.4})
        yr["results"].update({"away_score": None, "total_runs": None, "led_after_5": "tie", "run_in_first": True})
        ctx, pg, _ = one(yr, "linescore-final-x.json")
        check("YRFI and unknowns: 'YRFI 55%' green, 'First 5 Tie' green, no lamp where the result is null",
              PICKS(yr) == [("Winner Yankees", None), ("First 5 Tie", "hit"), ("Total Over 8.5", None), ("YRFI 55%", "hit")] and lamps(pg) == lamps_expect(yr)
              and classes(pg, ".lr-nr .num") == ["num b-plain"], (lamps(pg), lamps_expect(yr)))
        ctx.close()

        ctx, pg, _ = one(g2, 500)
        check("fallback: a live game whose score can't be fetched keeps page.json's live line", tc(pg, ".bd-live") == ["Live, bottom 5th, 2 outs"] and ls_rows(pg) == LS(None, "ATH", "SEA"), tc(pg, ".bd-live"))
        ctx.close()

        # postponed and suspended: the detail text in the top line, plates as given, no X, no lamps, no polling
        for name, detail in (("linescore-postponed.json", "Postponed"), ("linescore-suspended.json", "Suspended: Rain")):
            for gm in (g3, g1):
                ctx, pg, _ = one(gm, name, install=True)
                fx = fixture(name)
                tag = f"{detail} ({gm['status']} in page.json)"
                check(f"{tag}: the top line says so", tc(pg, ".bd-where") == [f"Fenway Park. {detail}."] and pg.locator(".bd-live").count() == 0, tc(pg, ".bd-where"))
                check(f"{tag}: plates as the API gives them, no X, no outline", ls_rows(pg) == LS(fx, "NYY", "BOS") and "X" not in str(ls_rows(pg)) and pg.locator(".ls .ip.cur").count() == 0, ls_rows(pg))
                check(f"{tag}: no lamps at all (the detailed state comes first), the four labels stay",
                      pg.locator(".board .lamp").count() == 0 and [t for t, _, _ in lamps(pg)] == [t for t, _ in PICKS(gm)], lamps(pg))
                adv(pg, 60 * 60000)
                check(f"{tag}: never polls", pg.api.count(gm["game_id"]) == 1, pg.api.calls)
                ctx.close()

        # ---------- game page polling ----------
        ctx, pg, _ = one(g2, "linescore-live.json", install=True, box="boxscore-live.json", series="series-tied.json")
        both = lambda: (pg.api.count(1002), pg.api.count(1002, "boxscore"))
        n0 = both()
        adv(pg, 29000)
        n1 = both()
        adv(pg, 1500)
        n2 = both()
        adv(pg, 30000)
        n3 = both()
        check("polling live: first fetch on render, then one every 30 seconds; the boxscore rides the same timer",
              (n0, n1, n2, n3) == ((1, 1), (1, 1), (2, 2), (3, 3)), (n0, n1, n2, n3))
        pg.api.table["linescore/1002"] = "linescore-final-x.json"
        adv(pg, 30000)
        n4 = pg.api.count(1002)
        adv(pg, 10 * 60000)
        check("polling live: stops once the game is final", (n4, pg.api.count(1002), pg.api.count(1002, "boxscore")) == (4, 4, 4), pg.api.every)
        check("polling live: the series is fetched once, not on the timer", pg.api.count(1002, "series") == 1, pg.api.every)
        check("polling live: the board shows the final line score", ls_rows(pg) == LS(fixture("linescore-final-x.json"), "ATH", "SEA") and pg.locator(".bd-live").count() == 0)
        ctx.close()

        ctx, pg, _ = one(g2, "linescore-live.json", install=True, box="boxscore-live.json", more={"battery": "battery-live.json", "standings": "standings-tied.json"})
        adv(pg, 30500)
        adv(pg, 30500)
        same = (pg.api.count(1002, "boxscore"), pg.api.count(1002, "battery"))
        pg.api.table["boxscore/1002"] = "boxscore-live-new-catcher.json"
        pg.api.table["battery/1002"] = "battery-live-new-catcher.json"
        adv(pg, 30500)
        settle(pg, 300)
        changed = (pg.api.count(1002, "boxscore"), pg.api.count(1002, "battery"))
        check("polling battery: once while the battery stays the same, again when the catcher changes; standings once",
              same == (3, 1) and changed == (4, 2) and pg.api.count(1002, "standings") == 1, (same, changed, pg.api.every))
        check("polling battery: the new catcher and his record with the pitcher show without a reload",
              battery(pg)[1][1] == "C Harry Ford" and extras(pg)[1][3] == "Battery 2–0 this season" and running(pg)[1][1] == "Harry Ford 9 stolen, 3 caught, 25%",
              (battery(pg), extras(pg), running(pg)))
        ctx.close()

        ctx, pg, _ = one(g2, "linescore-live.json", install=True)
        set_hidden(pg, True)
        adv(pg, 5 * 60000)
        hidden_n = pg.api.count(1002)
        set_hidden(pg, False)
        back_n = pg.api.count(1002)
        adv(pg, 30500)
        check("polling hidden: paused while hidden, refreshes on return, then resumes", (hidden_n, back_n, pg.api.count(1002)) == (1, 2, 3), (hidden_n, back_n, pg.api.count(1002)))
        ctx.close()

        ctx, pg, _ = one(g1, "linescore-preview.json", fixed="2026-04-14T22:40:00Z", install=True)  # first pitch 23:10
        p0 = pg.api.count(1001)
        adv(pg, 14 * 60000)
        p1 = pg.api.count(1001)          # 22:54
        adv(pg, 61000)
        p2 = pg.api.count(1001)          # 22:55:01, the wake-up ran
        adv(pg, 4 * 60000 + 30000)
        p2b = pg.api.count(1001)         # 22:59:31, nothing yet
        adv(pg, 31000)
        p3 = pg.api.count(1001)          # 23:00:02
        check("polling preview: none until 15 minutes before, then every 5 minutes", (p0, p1, p2, p2b, p3) == (1, 1, 2, 2, 3), (p0, p1, p2, p2b, p3))
        pg.api.table["linescore/1001"] = "linescore-live.json"
        adv(pg, 5 * 60000)
        p4 = pg.api.count(1001)
        live_now = tc(pg, ".bd-live")
        adv(pg, 30500)
        check("polling preview: goes live without a reload, then every 30 seconds", (p4, pg.api.count(1001)) == (4, 5) and live_now == ["Live, top 6th, 1 out"], (p4, pg.api.count(1001), live_now))
        ctx.close()

        ctx, pg, _ = one(g2, "hang", fixed="2026-04-14T21:00:00Z", install=True)
        adv(pg, 10500)
        after_abort = (pg.api.count(1002), tc(pg, ".bd-live"))
        pg.api.table["linescore/1002"] = "linescore-live.json"
        adv(pg, 30500)
        check("polling timeout: a timed-out fetch keeps page.json's line, then the next poll fills the board",
              after_abort == (1, ["Live, bottom 5th, 2 outs"]) and pg.api.count(1002) == 2 and tc(pg, ".bd-live") == ["Live, top 6th, 1 out"], (after_abort, pg.api.calls))
        ctx.close()

        ctx, pg, _ = one(g1, "linescore-preview.json", fixed="2026-04-14T14:00:00Z", install=True)
        adv(pg, 8 * 60 * 60000)
        check("polling preview: nothing hours before first pitch", pg.api.count(1001) == 1, pg.api.calls)
        ctx.close()

        # ================= Beat the Streak, in season =================
        write_page(pj, season_data())
        url = base + "beat-the-streak"
        ctx = new_ctx(browser, 1440, 900)
        pg, log = open_page(ctx, url, fixed="2026-04-14T14:00:00Z", api=Api(SEASON_API))
        ready(pg, ".picks")
        common(pg, log, url, "Beat the Streak (season)", "bts", "Beat the Streak: Diamond Metrics", "Beat the Streak: Diamond Metrics")
        check("Beat the Streak (season): the long date under the heading, then the sentence", tc(pg, ".upd") == ["Tuesday, April 14"] and tc(pg, ".lede") == [BTS_SENTENCE], tc(pg, ".upd"))
        rows_b = pg.evaluate(BTS_JS)
        exp_b = BTS_ROWS(d["beat_the_streak"], d["games"])
        check("Beat the Streak (season): worked out in the test: 'at Red Sox' for a Yankee, 'vs Yankees' for a Red Sox, the doubleheader pick without game_id goes to the game he played, lines only from the final",
              [r[3:6] for r in exp_b] == [["at Red Sox", "/game/1001", None], ["at Red Sox", "/game/1003", exp_b[1][5]], ["vs Yankees", "/game/1003", exp_b[2][5]],
                                          ["vs Yankees", "/game/1003", exp_b[3][5]], ["vs Athletics", "/game/1002", None]] and all(exp_b[i][5] for i in (1, 2, 3)), exp_b)
        check("Beat the Streak (season): every pick as worked out", rows_b == exp_b, (rows_b, exp_b))
        check("Beat the Streak (season): tiny probability shows <1%, near-certain >99%", rows_b[4][7] == "<1%" and rows_b[0][7] == ">99%", [r[7] for r in rows_b])
        sweep(pg, "Beat the Streak (season) sweep")
        pg.set_viewport_size({"width": 1440, "height": 900})
        settle(pg, 150)
        screenshot(pg, "bts-season-1440.png")
        ctx.close()

        # ================= a day with rainouts (the real 23 May slate) =================
        may = season_data()
        mk = lambda gid, away, home, start, status="scheduled": dict(copy.deepcopy(g1), game_id=gid, away_team=away, home_team=home, start_time_utc=start, status=status, park="Somewhere Park")
        may.update({"date": "2026-05-23", "updated_at": "2026-05-23T14:00:00Z", "beat_the_streak": [],
                    "games": [mk(823543, "Tampa Bay Rays", "New York Yankees", "2026-05-23T17:05:00Z"), mk(824840, "Detroit Tigers", "Baltimore Orioles", "2026-05-23T20:05:00Z"),
                              mk(824760, "Minnesota Twins", "Boston Red Sox", "2026-05-23T20:10:00Z")]})
        write_page(pj, may)
        ctx = new_ctx(browser)
        api = Api({"slate/2026-05-23": "slate-2026-05-23.json"})
        pg, log = open_page(ctx, base, fixed="2026-05-24T02:00:00Z", api=api, install=True)
        ready(pg, "a.card")
        rays, tigers, twins = may["games"]
        exp = [["Final", [CARD(twins, "Final", "Twins at Red Sox, final, 4 to 2. Full game.", ["25–27", "22–29"], [4, 2], foot="final")]],
               ["Postponed", [CARD(rays, "Postponed", "Rays at Yankees, postponed. Full game.", ["95–61", "90–66"]),
                              CARD(tigers, "Postponed", "Tigers at Orioles, postponed. Full game.", ["20–33", "23–29"])]]]
        got = cards(pg)
        check("rainouts (real slate): 'Final' upstream, but the detailed state puts them under Postponed (last), with no lamps and no runs; the slate's final is Final",
              got == exp, (got, exp))
        check("rainouts: the Postponed group comes after Final, and no lamp is lit anywhere", [g[0] for g in got] == ["Final", "Postponed"] and pg.locator(".card .lamp").count() == 0)
        adv(pg, 30 * 60000)
        check("rainouts: every game over, so the slate is not fetched again", api.count("2026-05-23", "slate") == 1, api.every)
        ctx.close()
    finally:
        srv.terminate()


def test_errors(browser):
    root = os.path.join(tempfile.mkdtemp(prefix="dm_err_"), "site")
    shutil.copytree(SITE, root)
    os.chmod(os.path.join(root, "data"), 0o755)
    pj = os.path.join(root, "data", "page.json")
    os.chmod(pj, 0o644)
    srv = start_server(root, PORT_ERR)
    try:
        base = f"http://127.0.0.1:{PORT_ERR}/"
        # empty slate
        d = json.load(open(os.path.join(SITE, "data", "page.json"), encoding="utf-8"))
        d["games"], d["beat_the_streak"], d["note"] = [], [], "live"
        json.dump(d, open(pj, "w", encoding="utf-8"))
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, base, install=True)
        ready(pg)
        check("no games, home: 'No games are scheduled for this date.' under Today's games; the rail says picks come later",
              tc(pg, ".games .msg") == ["No games are scheduled for this date."] and tc(pg, ".rail .msg") == ["Picks appear once lineups are posted."], tc(pg, "#app"))
        adv(pg, 10 * 60000)
        check("no games, home: no cards, no slate request, no errors", pg.locator("a.card").count() == 0 and pg.api.every == [] and not log["errors"], (pg.api.every, log["errors"]))
        ctx.close()
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, base + "beat-the-streak")
        ready(pg)
        check("no picks, Beat the Streak: 'Picks appear once lineups are posted.'", tc(pg, ".picks") == ["Picks appear once lineups are posted."] and not log["errors"], tc(pg, "#app"))
        ctx.close()
        ctx = new_ctx(browser)
        url = base + "game/849799"
        pg, log = open_page(ctx, url)
        pg.wait_for_selector(".msg")
        settle(pg)
        check("a gamePk not in page.json: 'This game isn't on today's slate.' and a link to all games, no requests to the API",
              tc(pg, "#app .msg") == ["This game isn't on today's slate."] and tc(pg, ".back a") == ["All games"] and pg.get_attribute(".back a", "href") == "/"
              and pg.api.every == [] and pg.locator(".board").count() == 0 and pg.title() == "MLB game: Diamond Metrics", (tc(pg, "#app"), pg.title()))
        ctx.close()
        for label, body in (("broken page.json", "{ not json"), ("page.json without games", '{"date":"2026-04-14"}')):
            open(pj, "w", encoding="utf-8").write(body)
            for path in ("", "game/849799", "beat-the-streak"):
                ctx = new_ctx(browser)
                pg, log = open_page(ctx, base + path)
                pg.wait_for_selector("#app [role=alert]")
                check(f"{label} on /{path}: friendly error with role=alert", tc(pg, "#app [role=alert]") == ["The predictions did not load. Try reloading the page."], tc(pg, "#app"))
                ctx.close()
        open(pj, "w", encoding="utf-8").write('{"date":"2026-04-14","games":[null]}')
        for path in ("", "game/849799"):
            ctx = new_ctx(browser)
            pg, log = open_page(ctx, base + path)
            pg.wait_for_selector("#app [role=alert]")
            check(f"page.json that cannot be shown on /{path}: second error message", tc(pg, "#app [role=alert]") == ["The predictions loaded but could not be shown. Try reloading the page."], tc(pg, "#app"))
            ctx.close()
    finally:
        srv.terminate()


if __name__ == "__main__":
    srv = start_server(SITE, PORT_MAIN)
    try:
        with sync_playwright() as p:
            # headless Chromium hides scrollbars by default; keep them, so the layout is what Windows users get
            b = p.chromium.launch(ignore_default_args=["--hide-scrollbars"])
            test_real(b, f"http://127.0.0.1:{PORT_MAIN}/")
            test_season(b)
            test_errors(b)
            b.close()
    finally:
        srv.terminate()
    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)} of {len(RESULTS)} checks passed")
    sys.exit(1 if bad else 0)
