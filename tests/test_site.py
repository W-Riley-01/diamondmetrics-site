"""Browser tests for the static site (run: python tests/test_site.py; needs `pip install playwright` and `playwright install chromium`).
Expected values are computed here from page.json and the line-score fixtures with separate code (Decimal rounding, not the
page's JS), then compared with what the browser actually shows. The static test server has no Worker, so /api/linescore/*
is answered with page.route from tests/fixtures/. Full-page screenshots go to tests/screens/ (ignored by git)."""
import copy, json, os, shutil, subprocess, sys, tempfile, time
from decimal import Decimal, ROUND_HALF_UP
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.abspath(os.environ.get("SITE_DIR") or os.path.join(HERE, "..", "public"))
SCREENS = os.path.join(HERE, "screens")
FIXTURES = os.path.join(HERE, "fixtures")
PORT_MAIN, PORT_SEASON, PORT_ERR = 8765, 8766, 8767
RESULTS = []

YELLOW, GREEN = "rgb(242, 193, 46)", "rgb(31, 74, 56)"


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
        "Athletics": "Athletics", "Seattle Mariners": "Mariners"}
KEY_LINE = "Yellow numbers on a green plate clear the strong cut-off, plain numbers clear the watch line, and grey numbers fall below it."


def norm(s):
    return " ".join(s.split())


def start_server(root, port, spa=False):
    cmd = [sys.executable, os.path.join(HERE, "serve.py"), root, "--port", str(port)] + (["--spa"] if spa else [])
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    return p


def new_ctx(browser, w=1280, h=900, tz="America/New_York"):
    return browser.new_context(viewport={"width": w, "height": h}, timezone_id=tz, locale="en-US", device_scale_factor=1)


class Api:
    """Answers /api/linescore/{gamePk} from a table: a fixture name, an HTTP status (int), or "hang" (never answers).
    The table can be changed while the page is open; every request is counted per gamePk."""

    def __init__(self, table):
        self.table = dict(table)
        self.calls = []

    def count(self, pk):
        return self.calls.count(str(pk))

    def handle(self, route):
        pk = route.request.url.rstrip("/").rsplit("/", 1)[-1]
        self.calls.append(pk)
        spec = self.table.get(pk, 404)
        if spec == "hang":
            return  # never fulfilled: the page's own timeout has to give up
        if isinstance(spec, int):
            route.fulfill(status=spec, content_type="application/json; charset=utf-8", body=json.dumps({"error": str(spec)}))
        else:
            route.fulfill(status=200, content_type="application/json; charset=utf-8", body=open(os.path.join(FIXTURES, spec), encoding="utf-8").read())


def open_page(ctx, url, fixed="2026-11-18T17:00:00Z", api=None, install=False):
    """Open the page with the clock pinned (fixed time) or installed (time moves only when the test says so)."""
    pg = ctx.new_page()
    log = {"console": [], "errors": [], "requests": [], "responses": []}
    pg.on("console", lambda m: log["console"].append((m.type, m.text)))
    pg.on("pageerror", lambda e: log["errors"].append(str(e)))
    pg.on("request", lambda r: log["requests"].append(r.url))
    pg.on("response", lambda r: log["responses"].append((r.url, r.status, r.headers)))
    api = api or Api({"849799": "linescore-849799.json"})
    pg.route("**/api/linescore/*", api.handle)
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
    """Expected text of each row and the number of check marks, from the box score."""
    rows, checks = [], 0
    for row in g["lineups"][side]:
        b = by_id.get(row["id"])
        cells = [str(row["slot"]), row["name"]]
        for k, rk, need in (("hit", "h", 1), ("tb2", "tb", 2), ("hr", "hr", 1), ("rbi", "rbi", 1), ("sb", "sb", 1)):
            yes = b is not None and b[rk] >= need
            checks += yes
            cells.append(PCT(row[k]) + (" happened" if yes else ""))
        cells.append(b["line"] if b else "–")
        rows.append(cells)
    return rows, checks


def fi_expect(fi):
    out = [f"NRFI record{fi['scoreless']}-{fi['not_scoreless']}", f"ERA{FIX2(fi['era'])}", f"OBP{DOT3(fi['opp_obp'])}"]
    if fi.get("whip") is not None:
        out.append(f"WHIP{FIX2(fi['whip'])}")
    return out


def pick_line(pick, games):
    """The pick's stat line from a final box score, found by game_id (if given), team and name; else None."""
    for g in games:
        if g["status"] != "final" or not g.get("results") or (pick.get("game_id") is not None and g["game_id"] != pick["game_id"]):
            continue
        side = "away" if g["away_team"] == pick["team"] else "home" if g["home_team"] == pick["team"] else None
        if not side:
            continue
        ids = [r["id"] for r in g["lineups"][side] if r["name"] == pick["name"]]
        hits = [b for b in g["results"]["batters"][side] if ids and b["id"] == ids[0]]
        if hits:
            return hits[0]["line"]
    return None


SWEEP = sorted(set(range(360, 1441, 40)) | {853})

# Every scroll wrapper: [class, scrollWidth, clientWidth, scrollHeight, clientHeight, the table's natural (unwrapped) width].
WRAPPERS = """() => [...document.querySelectorAll('.ls-scroll, .scroll')].map(s => {
    const t = s.querySelector('table'), keep = t.style.width;
    t.style.width = 'min-content';
    const natural = Math.ceil(t.getBoundingClientRect().width);
    t.style.width = keep;
    return [s.className, s.scrollWidth, s.clientWidth, s.scrollHeight, s.clientHeight, natural]; })"""


def sweep(pg, label):
    """At every width: no wrapper scrolls vertically, and none scrolls sideways when its table fits."""
    for w in SWEEP:
        pg.set_viewport_size({"width": w, "height": 900})
        settle(pg, 120)
        rows = pg.evaluate(WRAPPERS)
        bad = [r for r in rows if r[3] > r[4] or (r[5] <= r[2] and r[1] > r[2])]
        check(f"{label} {w}: no scroll wrapper overflows unless its table can't fit", rows and not bad and no_hscroll(pg), bad or rows)


def plate_tops_aligned(pg):
    """Every model plate in a team row starts at the same height."""
    return pg.evaluate("""() => [...document.querySelectorAll('.bd tbody tr')].every(tr => {
        const tops = [...tr.querySelectorAll('.plate')].map(p => p.getBoundingClientRect().top);
        return Math.max(...tops) - Math.min(...tops) < 1; })""")


# =====================================================================
def test_real(browser, base):
    page_json = json.load(open(os.path.join(SITE, "data", "page.json"), encoding="utf-8"))
    site_json = json.load(open(os.path.join(SITE, "data", "site.json"), encoding="utf-8"))
    g = page_json["games"][0]
    m, res = g["markets"], g["results"]
    ws5 = fixture("linescore-849799.json")

    # the captured line score must agree with the frozen box score
    check("fixture: WS5 line score R totals equal page.json's results (4 and 1)",
          (ws5["away"]["r"], ws5["home"]["r"]) == (res["away_score"], res["home_score"]) == (4, 1) and str(g["game_id"]) == "849799",
          (ws5["away"], ws5["home"], res.get("away_score"), res.get("home_score")))
    check("fixture: inning runs add up to R", all(sum(i[s] or 0 for i in ws5["innings"]) == ws5[s]["r"] for s in ("away", "home")))

    ctx = new_ctx(browser)
    pg, log = open_page(ctx, base)
    pg.wait_for_selector("h1")
    pg.evaluate("document.fonts.ready")
    settle(pg, 300)

    pg.keyboard.press("Tab")
    check("a11y: skip link is the first focusable element and becomes visible", pg.evaluate("document.activeElement.className") == "skip" and pg.evaluate("document.activeElement.getBoundingClientRect().left >= 0"))
    pg.evaluate("document.activeElement.blur()")
    check("a11y: exactly one h1", pg.locator("h1").count() == 1)
    check("real: no console messages or page errors (includes CSP violations)", not log["console"] and not log["errors"], (log["console"], log["errors"]))
    origin = base.rstrip("/")
    check("real: every request is same-origin", all(u.startswith(origin) for u in log["requests"]), [u for u in log["requests"] if not u.startswith(origin)])
    check("real: every static response has the CSP header", all("content-security-policy" in h for u, _, h in log["responses"] if "/api/" not in u))
    check("real: no failed responses", all(s < 400 for _, s, _ in log["responses"]), [(u, s) for u, s, _ in log["responses"] if s >= 400])
    check("real: the line score is requested once, for the WS5 game", pg.api.calls == ["849799"], pg.api.calls)
    fonts_loaded = sorted(u.rsplit("/", 1)[1] for u in log["requests"] if u.endswith(".woff2"))
    check("real: both self-hosted fonts requested", fonts_loaded == ["big-shoulders-latin-wght-normal.woff2", "libre-franklin-latin-wght-normal.woff2"], fonts_loaded)
    check("real: fonts actually usable", pg.evaluate("document.fonts.check('800 20px \"Big Shoulders\"') && document.fonts.check('16px \"Libre Franklin\"')"))
    check("real: theme-color meta is board green", pg.get_attribute("meta[name=theme-color]", "content") == "#1F4A38")

    # countdown (fixed clock 2026-11-18 -> Opening Day 2027-03-24 is 126 days)
    check("countdown: one plate per digit", tc(pg, "#banner .dg") == ["1", "2", "6"], tc(pg, "#banner .dg"))
    check("countdown: digits are one image named '126 days'", pg.get_attribute("#banner .digits", "role") == "img" and pg.get_attribute("#banner .digits", "aria-label") == "126 days")
    check("countdown: text", tc(pg, "#banner .cd-t") == ["days to Opening Day 2027, Wednesday, March 24."], tc(pg, "#banner .cd-t"))
    check("countdown: note matches site.json", tc(pg, "#banner .cd-note") == [site_json["opening_note"]])
    check("countdown: region with its label", pg.get_attribute("#banner", "role") == "region" and pg.get_attribute("#banner", "aria-label") == "Opening Day countdown")

    # title block
    check("title: h1 is replay_title", tc(pg, "h1") == [site_json["replay_title"]], tc(pg, "h1"))
    check("title: headline from site.json", tc(pg, ".lede") == [site_json["replay_headline"]], tc(pg, ".lede"))
    check("title: replay notice is the label verbatim", tc(pg, ".notice") == [page_json["label"]], tc(pg, ".notice"))
    # 2026-11-18T17:13:27Z is 12:13 PM EST
    check("title: updated line", tc(pg, ".upd") == ["Updated Nov 18, 2026, 12:13 PM EST."], tc(pg, ".upd"))

    # one game: no out-of-town board
    check("one game: no out-of-town board", pg.locator(".oot").count() == 0 and pg.locator(".gm").count() == 0)

    # ---------- the board: line score ----------
    check("board: has id today", pg.locator("section#today.board").count() == 1)
    check("board: top line (park, indoors, game date with no timezone shift)", tc(pg, ".bd-where") == ["Tropicana Field, indoors. Wednesday, October 28, 2026."], tc(pg, ".bd-where"))
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
    check("board: model section below a 2px #2B5B46 rule", css(pg, ".board .model", "borderTopWidth") == "2px" and css(pg, ".board .model", "borderTopColor") == "rgb(43, 91, 70)")

    # ---------- the board: model numbers ----------
    heads = tc(pg, ".bd thead th")
    check("board: column headers (no Final column)", heads == ["Team", "Odds to win", "First 5 winner", "Total runs O/U"], heads)
    check("board: caption, column and row headers", pg.locator(".bd caption").count() == 1 and pg.locator(".bd th[scope=col]").count() == 4 and pg.locator(".bd th[scope=row]").count() == 2)
    teams = [tc(pg, ".bd th.team .nick"), tc(pg, ".bd th.team .pitch")]
    check("board: team cells (nickname + starter's last name)", teams == [["Brewers", "Rays"], ["Misiorowski pitching", "Rasmussen pitching"]], teams)
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
    check("board: no 'led after 5' caption and no score column", "led after 5" not in tc(pg, ".board") and pg.locator(".bd .last").count() == 0)
    check("board: the % sign is its own smaller span", pg.locator(".bd .plate .pc").count() == 6)

    # lamp row: NRFI only (WS5: no run in the 1st -> lit)
    nr_lit = res["run_in_first"] is False
    check("lamps: NRFI is the only item, with its tier", tc(pg, ".lamprow") == [f"NRFI {PCT(m['no_run_first'])}" + (" It happened." if nr_lit else "")] and classes(pg, ".lamprow .num") == [f"num {BT(m['no_run_first'], 'nrfi')}"], (tc(pg, ".lamprow"), classes(pg, ".lamprow .num")))
    check("lamps: NRFI lamp is lit", pg.locator(".lr-nr .lamp.on").count() == 1 and pg.locator(".lamprow .lamp").count() == 1 and nr_lit)
    check("lamps: no visible lamp note", "A lit lamp" not in pg.evaluate("document.body.innerText"))
    check("lamps: hidden from screen readers", all(a == "true" for a in pg.eval_on_selector_all(".lamp", "els => els.map(e => e.getAttribute('aria-hidden'))")))

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
    check("pitchers: sub line", tc(pg, ".pitchers .subl") == ["Chance each starter reaches a strikeout total."], tc(pg, ".pitchers .subl"))
    pits = pg.locator(".pit")
    check("pitchers: two blocks", pits.count() == 2)
    for i, side in enumerate(("away", "home")):
        p = g["starters"][side]
        blk = pits.nth(i)
        t = lambda sel: [norm(x) for x in blk.locator(sel).all_text_contents()]
        threw = res["starter_k"][side]
        check(f"pitchers {side}: team and name", t(".who") == [NICK[g[side + "_team"]]] and t(".nm") == [p["name"]], (t(".who"), t(".nm")))
        check(f"pitchers {side}: median and thrown", t(".ks") == [f"Median {RUNS(p['median_k']).split()[0]} strikeouts. Threw {threw}."], t(".ks"))
        ks = sorted(p["k_over"], key=int)
        check(f"pitchers {side}: ladder labels", t(".rung .l") == [f"{k}+ K" for k in ks], t(".rung .l"))
        check(f"pitchers {side}: ladder values", t(".rung .v") == [PCT(p["k_over"][k]) for k in ks], t(".rung .v"))
        got_c = blk.locator(".rung .p").evaluate_all("els => els.map(e => e.className)")
        check(f"pitchers {side}: ladder chip classes", got_c == [f"p {TIER(p['k_over'][k], 'k'+k)}" for k in ks], got_c)
        marks = [blk.locator(".rung").nth(j).locator("svg").count() == 1 for j in range(len(ks))]
        check(f"pitchers {side}: check under each total reached", marks == [threw >= int(k) for k in ks], marks)
        check(f"pitchers {side}: check marks carry words", t(".rung .ck") == [("reached" if threw >= int(k) else "") for k in ks], t(".rung .ck"))
        exp_fi = fi_expect(p["first_inning"])
        check(f"pitchers {side}: first-inning label and stat blocks (no WHIP in the data)", t(".fi-l") == ["First inning"] and t(".stats > div") == exp_fi and len(exp_fi) == 3, (t(".stats > div"), exp_fi))
    check("pitchers: Rasmussen's 3+ and 4+ checked, nothing for Misiorowski", pits.nth(0).locator("svg").count() == 0 and pits.nth(1).locator("svg").count() == 2)

    # ---------- lineups: every cell, every chip class, every check ----------
    check("lineups: sub line without the check sentence", tc(pg, ".lineups .subl") == ["Chance of at least one hit, two total bases, a homer, an RBI and a steal."], tc(pg, ".lineups .subl"))
    by_id = {r["id"]: r for side in ("away", "home") for r in res["batters"][side]}
    tabs = pg.locator("table.t")
    check("lineups: two tables with a Result column", tabs.count() == 2 and tabs.nth(0).locator("thead th").count() == 8)
    check("lineups: column headers", tc(pg, "table.t >> nth=0 >> thead th") == ["#", "Player", "1+ hit", "2+ bases", "Homer", "RBI", "Steal", "Result"], tc(pg, "table.t >> nth=0 >> thead th"))
    check("lineups: headings are just the nicknames", tc(pg, ".lu h3") == ["Brewers", "Rays"], tc(pg, ".lu h3"))
    check("lineups: tables keep accessible captions", tc(pg, "table.t caption") == ["Milwaukee Brewers batting order", "Tampa Bay Rays batting order"], tc(pg, "table.t caption"))
    exp_checks = {}
    for i, side in enumerate(("away", "home")):
        rows = tabs.nth(i).locator("tbody tr")
        check(f"lineups {side}: nine rows", rows.count() == 9)
        exp_rows, exp_checks[side] = lineup_expect(g, side, by_id)
        ok_text, ok_cls, bad = True, True, []
        for r, row in enumerate(g["lineups"][side]):
            cells = [norm(x) for x in rows.nth(r).locator("td").all_text_contents()]
            if cells != exp_rows[r]:
                ok_text = False; bad.append((cells, exp_rows[r]))
            cls = rows.nth(r).locator(".p").evaluate_all("els => els.map(e => e.className)")
            expc = [f"p {TIER(row[k], key)}" for k, key in (("hit", "hit"), ("tb2", "tb"), ("hr", "hr"), ("rbi", "rbi"), ("sb", "sb"))]
            if cls != expc:
                ok_cls = False; bad.append((cls, expc))
        check(f"lineups {side}: all cell text, checks and result lines", ok_text, bad[:2])
        check(f"lineups {side}: all chip classes", ok_cls, bad[:2])
        n = tabs.nth(i).locator("svg").count()
        check(f"lineups {side}: check-mark count matches the box score", n == exp_checks[side], (n, exp_checks[side]))
    check("lineups: WS5 has 13 Brewers checks and 6 Rays checks", (exp_checks["away"], exp_checks["home"]) == (13, 6), exp_checks)
    check("lineups: scroll regions are keyboard focusable and named", pg.locator(".scroll[tabindex='0'][role=region][aria-label]").count() == 2)
    check("lineups: rows at least 44px tall", pg.evaluate("[...document.querySelectorAll('table.t tbody tr')].every(r => r.getBoundingClientRect().height >= 44)"))

    # ---------- Beat the Streak ----------
    picks = page_json["beat_the_streak"]
    exp_lines = [pick_line(p, page_json["games"]) for p in picks]
    check("BTS: every WS5 pick has a stat line", all(exp_lines), exp_lines)
    check("BTS: ranks", tc(pg, ".bts .rk") == ["1", "2", "3", "4", "5"], tc(pg, ".bts .rk"))
    check("BTS: names", tc(pg, ".bts .name") == [p["name"] for p in picks])
    check("BTS: team nickname then the night's stat line", tc(pg, ".bts .meta") == [NICK[p["team"]] + l for p, l in zip(picks, exp_lines)], tc(pg, ".bts .meta"))
    check("BTS: stat lines from the box score", tc(pg, ".bts .line") == exp_lines, (tc(pg, ".bts .line"), exp_lines))
    check("BTS: team 14px muted, line 14px ink semibold after a 10px gap",
          [css(pg, ".bts .meta", "fontSize"), css(pg, ".bts .meta", "color"), css(pg, ".bts .line", "fontSize"), css(pg, ".bts .line", "color"), css(pg, ".bts .line", "fontWeight"), css(pg, ".bts .line", "marginLeft")]
          == ["14px", "rgb(78, 95, 86)", "14px", "rgb(19, 36, 28)", "600", "10px"])
    check("BTS: hit chances", tc(pg, ".bts .p") == [PCT(p["hit"]) for p in picks], tc(pg, ".bts .p"))
    check("BTS: chip classes", classes(pg, ".bts .p") == [f"p {TIER(p['hit'], 'hit')}" for p in picks], classes(pg, ".bts .p"))
    check("BTS: section is the #bts anchor", pg.locator("section#bts h2").count() == 1)

    # ---------- cut-offs ----------
    check("cut-offs: its own disclosure right under Beat the Streak", pg.evaluate("document.querySelector('#bts').nextElementSibling.matches('details.how')"))
    check("cut-offs: 3px board-green top rule", css(pg, ".how", "borderTopWidth") == "3px" and css(pg, ".how", "borderTopColor") == GREEN)
    ind = "() => { const s = document.querySelector('.how summary'), b = getComputedStyle(s, '::before'); return [b.content, b.borderLeftWidth, b.transform, s.getBoundingClientRect().height >= 44]; }"
    closed = pg.evaluate(ind)
    check("cut-offs: summary shows an open indicator, 44px tall", closed[0] == '""' and closed[1] == "8px" and closed[2] == "none" and closed[3], closed)
    pg.click(".how summary")
    opened = pg.evaluate(ind)
    check("cut-offs: indicator turns when open", opened[2] != "none" and opened[3], opened)
    check("cut-offs: key line comes first, before the table", tc(pg, ".how p.key") == [KEY_LINE] and pg.evaluate("document.querySelector('.how p.key').nextElementSibling.tagName") == "TABLE")
    rows = tc(pg, ".how tbody tr")
    exp = {"Odds to win": "ml", "First 5 winner": "f5", "NRFI": "nrfi", "Starter 3+ strikeouts": "k3",
           "Starter 4+ strikeouts": "k4", "Starter 5+ strikeouts": "k5", "Starter 6+ strikeouts": "k6", "Starter 7+ strikeouts": "k7",
           "Batter 1+ hit": "hit", "Batter 2+ total bases": "tb", "Batter 1+ home run": "hr", "Batter 1+ RBI": "rbi", "Batter 1+ stolen base": "sb"}
    check("cut-offs: table equals the original app's cut-offs, rows named as on the board", rows == [f"{k}{TH[v][0]}%+{TH[v][1]}%+" for k, v in exp.items()], rows)
    guide_txt = norm(" ".join(tc(pg, ".how")))
    check("cut-offs: HR tiers as in the original colour guide", all(s in guide_txt for s in ["65 and up Very HR Friendly", "55 to 64 HR Friendly", "45 to 54 Neutral", "35 to 44 HR Suppressed", "under 35 Very HR Suppressed", "One point is about one foot"]), guide_txt)

    # ---------- structure, accessibility, copy ----------
    check("a11y: html lang and viewport", pg.evaluate("document.documentElement.lang") == "en" and pg.locator("meta[name=viewport]").count() == 1)
    check("a11y: lineup tables have captions and column headers", pg.locator("table.t caption").count() == 2 and pg.locator("table.t th[scope=col]").count() == 16)
    check("nav: links and current page", tc(pg, ".links a") == ["Games", "Beat the Streak", "How the model works"] and pg.get_attribute(".links a >> nth=0", "aria-current") == "page")
    check("footer text", tc(pg, "footer p") == ["Predictions come from simulating each game thousands of times. Probabilities, not guarantees, and not betting advice.", "Not affiliated with Major League Baseball."])
    body = pg.evaluate("document.body.innerText")
    check("copy: no middle dots and no arrows", "·" not in body and not any(c in body for c in "→↗›»"))
    out = focus_outline(pg, ".links a[href='#bts']")
    check("a11y: focus outline on green is 3px yellow", out == ["solid", "3px", YELLOW], out)
    out = focus_outline(pg, ".scroll")
    check("a11y: focus outline on the ground is 3px board green", out == ["solid", "3px", GREEN], out)
    check("a11y: nav links are at least 44px tall", pg.evaluate("[...document.querySelectorAll('.links a, .brand')].every(a => a.getBoundingClientRect().height >= 44)"))
    check("page: no horizontal scroll at 1280", no_hscroll(pg))
    for w in (768, 1440):
        pg.set_viewport_size({"width": w, "height": 900})
        settle(pg, 150)
        check(f"page: no horizontal scroll at {w}", no_hscroll(pg), pg.evaluate("document.documentElement.scrollWidth"))
        plates_in = pg.evaluate("(() => { const b = document.querySelector('.board').getBoundingClientRect(); return [...document.querySelectorAll('.board .plate, .board .ip')].every(p => p.getBoundingClientRect().right <= b.right - 8); })()")
        check(f"board at {w}: every plate inside the board", plates_in)
        check(f"board at {w}: plates line up at the top", plate_tops_aligned(pg))
    check("scrollbars: the browser runs with classic scrollbars that take up space, as on Windows",
          pg.evaluate("innerWidth - document.documentElement.clientWidth") > 0, pg.evaluate("innerWidth - document.documentElement.clientWidth"))
    pg.set_viewport_size({"width": 853, "height": 900})
    settle(pg, 150)
    w853 = pg.evaluate(WRAPPERS)
    check("853 (the reported width): the line score has no vertical scroll and both lineups fit without a scrollbar",
          all(r[3] <= r[4] and r[1] <= r[2] for r in w853), w853)
    sweep(pg, "replay sweep")
    pg.set_viewport_size({"width": 1440, "height": 900})
    settle(pg, 150)
    pg.evaluate("document.activeElement.blur()")
    screenshot(pg, "replay-1440.png")
    ctx.close()

    # ---------- phone ----------
    ctx = new_ctx(browser, 390, 844)
    pg, log = open_page(ctx, base)
    pg.wait_for_selector(".ls .ip")
    pg.evaluate("document.fonts.ready")
    settle(pg, 300)
    check("phone 390: no horizontal page scroll", no_hscroll(pg), pg.evaluate("document.documentElement.scrollWidth"))
    check("phone 390: no console messages", not log["console"] and not log["errors"], log["console"])
    check("phone 390: nine innings plus R, H, E fit without scrolling", pg.evaluate("(() => { const s = document.querySelector('.ls-scroll'); return s.scrollWidth <= s.clientWidth; })()"), pg.evaluate("[document.querySelector('.ls-scroll').scrollWidth, document.querySelector('.ls-scroll').clientWidth]"))
    psz = pg.evaluate("(() => { const p = document.querySelector('.ls .ip'); return [p.getBoundingClientRect().width, parseFloat(getComputedStyle(p).fontSize)]; })()")
    check("phone 390: inning plates about 20px wide with 16px numerals", psz == [20, 16], psz)
    check("phone 390: line score values still right", ls_rows(pg) == exp_ls)
    wide = pg.evaluate("[...document.querySelectorAll('.scroll')].map(e => e.scrollWidth > e.clientWidth)")
    check("phone 390: lineup tables scroll inside their own box", wide == [True, True], wide)
    labels = pg.evaluate("[...document.querySelectorAll('.bd tbody tr:first-child td')].map(td => getComputedStyle(td, '::before').content)")
    check("phone 390: board cells labelled from data-label", labels == ['"Odds to win"', '"First 5 winner"', '"Total runs O/U"'], labels)
    sizes = pg.evaluate("(() => { const p = document.querySelector('.plate'); return [parseFloat(getComputedStyle(p).fontSize), parseFloat(getComputedStyle(p.querySelector('.pc')).fontSize), parseFloat(getComputedStyle(document.querySelector('h1')).fontSize)]; })()")
    check("phone 390: plates 36-40px with an 18-20px % sign, h1 about 48px", 36 <= sizes[0] <= 40 and 18 <= sizes[1] <= 20 and sizes[2] == 48, sizes)
    check("phone 390: each team is one block (name row on top, plates below)", pg.evaluate("[...document.querySelectorAll('.bd tbody tr')].every(tr => { const th = tr.querySelector('th').getBoundingClientRect(); return [...tr.querySelectorAll('td')].every(td => td.getBoundingClientRect().top >= th.bottom); })"))
    check("phone 390: plates line up at the top", plate_tops_aligned(pg))
    check("phone 390: plates inside the board", pg.evaluate("(() => { const b = document.querySelector('.board').getBoundingClientRect(); return [...document.querySelectorAll('.board .plate, .board .ip')].every(p => p.getBoundingClientRect().right <= b.right); })()"))
    check("phone 390: team names on one line", pg.evaluate("[...document.querySelectorAll('.nick')].every(n => n.getBoundingClientRect().height < parseFloat(getComputedStyle(n).fontSize) * 1.5)"))
    screenshot(pg, "replay-390.png")
    ctx.close()

    # ---------- the score can't be fetched: same empty board, the rest of the page intact ----------
    for label, spec in (("500", 500), ("timeout", "hang")):
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, base, api=Api({"849799": spec}), install=True)
        pg.wait_for_selector(".board")
        if spec == "hang":
            adv(pg, 10500)  # the page gives up after 10 seconds
        settle(pg, 300)
        check(f"line score {label}: empty plates and dashes, no error banner", ls_rows(pg) == LS(None, "MIL", "TB") and pg.locator("[role=alert]").count() == 0, ls_rows(pg))
        check(f"line score {label}: the rest of the page is unaffected",
              tc(pg, "h1") == [site_json["replay_title"]] and pg.locator(".bd .plate").count() == 6 and pg.locator("table.t tbody tr").count() == 18
              and pg.locator(".bts li").count() == 5 and tc(pg, ".bd-where") == ["Tropicana Field, indoors. Wednesday, October 28, 2026."])
        check(f"line score {label}: no page errors", not log["errors"], log["errors"])
        ctx.close()

    # ---------- timezone: the game date must not shift for a viewer far from the US ----------
    ctx = new_ctx(browser, tz="Pacific/Auckland")
    pg, log = open_page(ctx, base, fixed="2026-11-18T21:00:00Z")  # 10 AM, Nov 19 in Auckland
    pg.wait_for_selector("h1")
    settle(pg)
    check("timezone Auckland: game date on the board unchanged", tc(pg, ".bd-where") == ["Tropicana Field, indoors. Wednesday, October 28, 2026."], tc(pg, ".bd-where"))
    check("timezone Auckland: updated line in local time", tc(pg, ".upd")[0].startswith("Updated Nov 19, 2026"), tc(pg, ".upd"))
    check("timezone Auckland: countdown uses local date (125 days)", tc(pg, "#banner .dg") == ["1", "2", "5"] and pg.get_attribute("#banner .digits", "aria-label") == "125 days", tc(pg, "#banner .dg"))
    ctx.close()

    # ---------- countdown edge cases ----------
    for fixed, exp_digits, exp_txt, hidden in (("2027-03-23T16:00:00Z", ["1"], "day to Opening Day 2027, Wednesday, March 24.", False),
                                               ("2027-03-24T16:00:00Z", [], "Opening Day 2027 is today, Wednesday, March 24.", False),
                                               ("2027-03-25T16:00:00Z", None, None, True)):
        ctx = new_ctx(browser)
        pg, _ = open_page(ctx, base, fixed=fixed)
        pg.wait_for_selector("h1")
        vis = pg.is_visible("#banner")
        t = tc(pg, "#banner .cd-t")
        d = tc(pg, "#banner .dg")
        aria = pg.get_attribute("#banner .digits", "aria-label") if d else None
        ok = (vis != hidden) and (hidden or (t == [exp_txt] and d == exp_digits and (not d or aria == "1 day")))
        check(f"countdown at {fixed[:10]}", ok, (vis, t, d, aria))
        ctx.close()

    # ---------- a final game never polls ----------
    ctx = new_ctx(browser)
    pg, _ = open_page(ctx, base, install=True)
    pg.wait_for_selector(".ls .ip")
    settle(pg)
    adv(pg, 10 * 60000)
    check("polling: a final game is fetched once and never again", pg.api.count(849799) == 1, pg.api.calls)
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
    # Yankees lineup ids in g3 come from the Brewers copy; use the real names so picks can find them
    picks = [
        {"name": "Christian Yelich", "team": "New York Yankees", "opponent": "Boston Red Sox", "home": False, "hit": 0.9966, "game_id": 1001},
        {"name": "Jackson Chourio", "team": "New York Yankees", "opponent": "Boston Red Sox", "home": False, "hit": 0.7314, "game_id": 1003},
        {"name": "Yandy Díaz", "team": "Boston Red Sox", "opponent": "New York Yankees", "home": True, "hit": 0.7148, "game_id": 1003},
        {"name": "Chandler Simpson", "team": "Boston Red Sox", "opponent": "New York Yankees", "home": True, "hit": 0.7052},
        {"name": "Nobody Atall", "team": "Seattle Mariners", "opponent": "Athletics", "home": True, "hit": 0.004},
    ]
    return {"date": "2026-04-14", "updated_at": "2026-04-14T15:00:00Z", "model_version": "v4", "note": "live", "games": [g1, g2, g3], "beat_the_streak": picks}


SEASON_API = {"1001": "linescore-preview.json", "1002": "linescore-live.json", "1003": "linescore-final-11.json"}


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
    srv = start_server(root, PORT_SEASON, spa=True)  # spa=True: a host answering index.html for a missing site.json
    try:
        base = f"http://127.0.0.1:{PORT_SEASON}/"
        ctx = new_ctx(browser, 1440, 900)
        pg, log = open_page(ctx, base, fixed="2026-04-14T14:00:00Z", api=Api(SEASON_API))
        pg.wait_for_selector("h1")
        pg.evaluate("document.fonts.ready")
        settle(pg)
        check("season: no console errors even though site.json falls back to index.html", not log["errors"] and not [c for c in log["console"] if c[0] == "error" and "CSP" in c[1]], log)
        check("season: countdown hidden when site.json is missing", not pg.is_visible("#banner"))
        check("season: no replay notice", pg.locator(".notice").count() == 0 and pg.locator(".lede").count() == 0)
        check("season: h1 is the long date, only one h1", tc(pg, "h1") == ["Tuesday, April 14, 2026"], tc(pg, "h1"))
        check("season: games and updated line", tc(pg, ".upd") == ["3 games. Updated Apr 14, 2026, 11:00 AM EDT."], tc(pg, ".upd"))
        gap = pg.evaluate("getComputedStyle(document.querySelector('h1 + .upd')).marginTop")
        check("season: updated line sits 14px under the title, as the replay subline does", gap == "14px", gap)

        # out-of-town board (page.json only; only the selected game's line score is requested)
        check("season: out-of-town board with its heading", tc(pg, ".oot h2") == ["Today's games"])
        check("season: three game buttons, first pressed", pg.locator(".oot button.gm").count() == 3 and [pg.get_attribute(f".gm >> nth={i}", "aria-pressed") for i in range(3)] == ["true", "false", "false"])
        st = tc(pg, ".gm .st")
        check("season: status text (scheduled shows local time, live, final)", st == ["7:10 PM EDT", "Live", "Final 6–5"], st)
        gm = tc(pg, ".gm")
        exp_gm = [f"{CODE[g['away_team']]}{PCT(g['markets']['away_win'])}at{CODE[g['home_team']]}{PCT(g['markets']['home_win'])}{s}" for g, s in zip((g1, g2, g3), st)]
        check("season: each button shows both codes and win chances", [x.replace(" ", "") for x in gm] == [x.replace(" ", "") for x in exp_gm], (gm, exp_gm))
        check("season: game buttons at least 44px tall", pg.evaluate("[...document.querySelectorAll('.gm')].every(b => b.getBoundingClientRect().height >= 44)"))
        mini_cls = classes(pg, ".gm .mini")
        check("season: button win chances carry the board tier", mini_cls == [f"mini {BT(g['markets'][k], 'ml')}" for g in (g1, g2, g3) for k in ("away_win", "home_win")], mini_cls)
        check("season: only the selected game's line score is requested", pg.api.calls == ["1001"], pg.api.calls)

        # BTS: team nickname; a stat line only where a final box score has the player (game_id picks the game)
        exp_lines = [pick_line(p, d["games"]) for p in d["beat_the_streak"]]
        check("season BTS: lines only for picks in the final game", [l is not None for l in exp_lines] == [False, True, True, True, False], exp_lines)
        meta = tc(pg, ".bts .meta")
        check("season BTS: team nickname alone before results, plus the stat line after", meta == [NICK[p["team"]] + (l or "") for p, l in zip(d["beat_the_streak"], exp_lines)], meta)
        check("season BTS: stat lines", tc(pg, ".bts .line") == [l for l in exp_lines if l], tc(pg, ".bts .line"))
        check("season BTS: tiny probability shows <1%, near-certain >99%", tc(pg, ".bts .p")[4] == "<1%" and tc(pg, ".bts .p")[0] == ">99%", tc(pg, ".bts .p"))

        # game 1: preview, outdoor weather (dew point and elevation computed)
        check("season game 1: top line gives first pitch", tc(pg, ".bd-where") == ["Fenway Park. First pitch 7:10 PM EDT."], tc(pg, ".bd-where"))
        check("season game 1: before the game, empty plates and dashes", ls_rows(pg) == LS(fixture("linescore-preview.json"), "NYY", "BOS") == LS(None, "NYY", "BOS"), ls_rows(pg))
        check("season game 1: no live line, no outline", pg.locator(".bd-live").count() == 0 and pg.locator(".ls .ip.cur").count() == 0)
        l1, v1 = OU(g1["markets"]["total_over"])
        check("season game 1: board numbers", board_plates(pg) == [[PCT(g1["markets"]["away_win"]), PCT(g1["markets"]["f5_away"]), v1[0]], [PCT(g1["markets"]["home_win"]), PCT(g1["markets"]["f5_home"]), v1[1]]], board_plates(pg))
        check("season game 1: O/U captions", tc(pg, ".bd .cap") == l1, tc(pg, ".bd .cap"))
        check("season game 1: NRFI without a lamp before the game", tc(pg, ".lamprow") == [f"NRFI {PCT(g1['markets']['no_run_first'])}"] and pg.locator(".lamprow .lamp").count() == 0, tc(pg, ".lamprow"))
        check("season game 1: dew point by Magnus is 47°F", DEW(61.4, 58) == 47)
        check("season game 1: Fenway's 6 m is 20 ft", FT(ELEV_M["BOS"]) == 20)
        wx = tc(pg, ".wx > div")
        check("season game 1: six readings, computed dew point and elevation", wx == readings(g1["weather"], "BOS"), (wx, readings(g1["weather"], "BOS")))
        check("season game 1: no pressure, wind note 13px muted under the wind", "Pressure" not in " ".join(wx) and tc(pg, ".wx .note") == ["Out to CF (7 mph)"] and css(pg, ".wx .note", "fontSize") == "13px" and css(pg, ".wx .note", "color") == "rgb(78, 95, 86)")
        check("season game 1: bar label without the heading's words, hot bar", tc(pg, ".hrbox .hrrow > *") == ["HR Friendly", "63 out of 100"] and pg.locator(".fill.hot").count() == 1, tc(pg, ".hrbox .hrrow > *"))
        check("season game 1: totals sub line without a result", tc(pg, ".totals .subl") == ["Chance the game goes over each line."])
        check("season game 1: lineup table has no Result column and no checks", pg.locator("table.t").first.locator("thead th").count() == 7 and pg.locator("table.t svg").count() == 0)
        g1_plates = board_plates(pg)

        # game 2: live from the API (top of the 6th; page.json still says bottom 5th), dome, missing weather and starter, O/U tie
        pg.click(".gm >> nth=1")
        settle(pg)
        live = fixture("linescore-live.json")
        check("season game 2: selection moves", [pg.get_attribute(f".gm >> nth={i}", "aria-pressed") for i in range(3)] == ["false", "true", "false"])
        check("season game 2: focus stays on the button that was clicked", pg.evaluate("document.activeElement.getAttribute('data-id')") == "1002")
        check("season game 2: line score from the API", ls_rows(pg) == LS(live, "ATH", "SEA"), (ls_rows(pg), LS(live, "ATH", "SEA")))
        cur = pg.evaluate("(() => { const c = document.querySelectorAll('.ls .ip.cur'); if (c.length !== 1) return c.length; const td = c[0].closest('td'), tr = td.closest('tr'); const cs = getComputedStyle(c[0]); return [tr.querySelector('th').textContent, [...tr.querySelectorAll('td')].indexOf(td) + 1, cs.outlineStyle, cs.outlineWidth, cs.outlineColor]; })()")
        check("season game 2: the half being played has a 2px yellow outline (ATH, 6th)", cur == ["ATH", 6, "solid", "2px", YELLOW], cur)
        check("season game 2: live line from the API, with a lit lamp", tc(pg, ".bd-live") == ["Live, top 6th, 1 out"] and pg.locator(".bd-live .lamp.on").count() == 1, tc(pg, ".bd-live"))
        check("season game 2: top line gives the date for a live game", tc(pg, ".bd-where") == ["T-Mobile Park, indoors. Tuesday, April 14, 2026."], tc(pg, ".bd-where"))
        l2, v2 = OU(g2["markets"]["total_over"])
        check("season game 2: O/U tie goes to the higher line (8.5, over 45, under 55)", l2 == ["Over 8.5", "Under 8.5"] and v2 == ["45%", "55%"] and tc(pg, ".bd .cap") == l2, (l2, v2, tc(pg, ".bd .cap")))
        check("season game 2: board numbers changed to game 2", board_plates(pg) != g1_plates and board_plates(pg) == [[PCT(0.4123), PCT(0.3041), v2[0]], [PCT(0.5877), PCT(0.5512), v2[1]]], board_plates(pg))
        check("season game 2: plate tiers follow the new numbers", classes(pg, ".bd tbody tr >> nth=1 >> .plate") == [f"plate {BT(0.5877, 'ml')}", f"plate {BT(0.5512, 'f5')}", "plate b-plain"], classes(pg, ".bd tbody tr >> nth=1 >> .plate"))
        teams = [tc(pg, ".bd th.team .nick"), tc(pg, ".bd th.team .pitch")]
        check("season game 2: starter not announced on the board", teams == [["Athletics", "Mariners"], ["Starter not announced", "Rasmussen pitching"]], teams)
        check("season game 2: no lamps while live", pg.locator(".lamprow .lamp").count() == 0)
        check("season game 2: forecast missing message under the new heading", tc(pg, ".cond h2") == ["Home run conditions"] and tc(pg, ".cond-note") == ["The forecast for this park was not available when the prediction ran."], tc(pg, ".cond-note"))
        pits = tc(pg, ".pit")
        check("season game 2: starter not announced + first-inning fallback", "Starter not announced" in pits[0] and tc(pg, ".fi-none") == ["First-inning record not shown (fewer than 5 starts)."] and pg.locator(".stats").count() == 0, pits)

        # game 3: an 11-inning final, file dew point and elevation, WHIP
        pg.click(".gm >> nth=2")
        settle(pg)
        f11 = fixture("linescore-final-11.json")
        r3 = g3["results"]
        check("season game 3: eleven innings, header 1 to 11", ls_head(pg) == ["Team"] + [str(i) for i in range(1, 12)] + ["R", "H", "E"], ls_head(pg))
        check("season game 3: every plate of the 11-inning final", ls_rows(pg) == LS(f11, "NYY", "BOS"), (ls_rows(pg), LS(f11, "NYY", "BOS")))
        check("season game 3: R matches the final score in page.json", [r[2][0] for r in ls_rows(pg)] == [str(g3["away_score"]), str(g3["home_score"])])
        check("season game 3: no X (the home team batted in the 11th), no outline", "X" not in ls_rows(pg)[1][1] and pg.locator(".ls .ip.cur").count() == 0)
        l3, v3 = OU(g3["markets"]["total_over"])
        check("season game 3: O/U line 8.5 (0.52), over 52 and under 48", l3 == ["Over 8.5", "Under 8.5"] and v3 == ["52%", "48%"] and tc(pg, ".bd .cap") == l3 and [p[2] for p in board_plates(pg)] == v3, (l3, v3, board_plates(pg)))
        check("season game 3: no 'led after 5' caption any more", "led after 5" not in tc(pg, ".board"))
        check("season game 3: NRFI lamp unlit (a run scored in the 1st)", pg.locator(".lr-nr .lamp").count() == 1 and pg.locator(".lr-nr .lamp.on").count() == 0 and "It happened" not in tc(pg, ".lr-nr")[0])
        wx3 = tc(pg, ".wx > div")
        check("season game 3: the file's dew point and elevation win", wx3 == readings(g3["weather"], "BOS") and "Dew point51°F" in wx3 and "Elevation5,204 ft" in wx3, wx3)
        st3 = [[norm(x) for x in pg.locator(".pit").nth(i).locator(".stats > div").all_text_contents()] for i in range(2)]
        check("season game 3: WHIP shows when the data has it", st3 == [fi_expect(g3["starters"]["away"]["first_inning"]), fi_expect(g3["starters"]["home"]["first_inning"])] and st3[0][-1] == "WHIP1.23" and len(st3[1]) == 3, st3)
        lines = sorted((k for k in g3["markets"]["total_over"] if k.endswith(".5")), key=float)
        over = sum(r3["total_runs"] > float(k) for k in lines)
        check("season game 3: totals sub line counts the lines it went over", tc(pg, ".totals .subl") == [f"Chance the game goes over each line. It finished with {RUNS(r3['total_runs'])}, over {over} of them."], tc(pg, ".totals .subl"))
        marks = [pg.locator(".tot-row").nth(i).locator("svg").count() == 1 for i in range(len(lines))]
        check("season game 3: a check after each line that went over", marks == [r3["total_runs"] > float(k) for k in lines] and sum(marks) == 5, marks)
        check("season: no console errors after clicking around", not log["errors"], log["errors"])
        for w in (1440, 1280, 768, 390):
            pg.set_viewport_size({"width": w, "height": 900})
            settle(pg, 150)
            check(f"season {w}: no horizontal page scroll with the 11-inning line score", no_hscroll(pg), pg.evaluate("document.documentElement.scrollWidth"))
        sc = pg.evaluate("(() => { const s = document.querySelector('.ls-scroll'); return [s.scrollWidth > s.clientWidth, s.tabIndex, s.getAttribute('role'), s.getAttribute('aria-label')]; })()")
        check("season 390: extra innings scroll inside a focusable wrapper", sc == [True, 0, "region", "Line score"], sc)
        sweep(pg, "11-inning sweep")
        pg.set_viewport_size({"width": 1440, "height": 900})
        pg.click(".gm >> nth=1")
        settle(pg)
        sweep(pg, "live sweep")
        pg.set_viewport_size({"width": 1440, "height": 900})
        pg.click(".gm >> nth=1")
        settle(pg)
        screenshot(pg, "season-1440.png")
        fits = """() => [...document.querySelectorAll('.oot .gm')].flatMap((b, i) => {
            const r = b.getBoundingClientRect(), bad = [];
            if (b.scrollWidth > b.clientWidth) bad.push(i + ': scrollWidth ' + b.scrollWidth + ' > ' + b.clientWidth);
            for (const c of b.children) { const k = c.getBoundingClientRect();
              if (k.left < r.left - 0.5 || k.right > r.right + 0.5 || k.top < r.top - 0.5 || k.bottom > r.bottom + 0.5) bad.push(i + ': ' + c.className + ' outside'); }
            return bad; })"""
        for w in (1440, 1280, 768, 390):
            pg.set_viewport_size({"width": w, "height": 900})
            settle(pg, 150)
            bad = pg.evaluate(fits)
            check(f"season {w}: nothing overflows its game button", not bad, bad)
            check(f"season {w}: no horizontal page scroll (live game)", no_hscroll(pg), pg.evaluate("document.documentElement.scrollWidth"))
        pg.set_viewport_size({"width": 390, "height": 844})
        settle(pg, 150)
        check("season 390: nine innings fit without scrolling (live game)", pg.evaluate("(() => { const s = document.querySelector('.ls-scroll'); return s.scrollWidth <= s.clientWidth; })()"))
        screenshot(pg, "season-live-390.png")
        ctx.close()

        # one game at a time, each with its own line-score fixture
        def one(game, spec, fixed="2026-04-14T14:00:00Z", install=False):
            dd = season_data()
            dd["games"] = [copy.deepcopy(game)]
            write_page(pj, dd)
            c = new_ctx(browser)
            p, lg = open_page(c, base, fixed=fixed, api=Api({str(game["game_id"]): spec}), install=install)
            p.wait_for_selector(".board")
            settle(p, 300)
            return c, p, lg

        # home team didn't bat in the 9th
        ctx, pg, _ = one(g3, "linescore-final-x.json")
        fx = fixture("linescore-final-x.json")
        check("X: the home team's 9th shows X", ls_rows(pg) == LS(fx, "NYY", "BOS") and ls_rows(pg)[1][1][8] == "X", ls_rows(pg))
        check("X: only one X", sum(c == "X" for r in ls_rows(pg) for c in r[1]) == 1)
        ctx.close()

        # a failed fetch for a live game keeps page.json's top line
        ctx, pg, _ = one(g2, 500)
        check("fallback: a live game whose score can't be fetched keeps page.json's live line", tc(pg, ".bd-live") == ["Live, bottom 5th, 2 outs"] and ls_rows(pg) == LS(None, "ATH", "SEA"), tc(pg, ".bd-live"))
        ctx.close()

        # postponed and suspended: the detail text in the top line, plates as given, no X, no polling
        for name, detail, codes in (("linescore-postponed.json", "Postponed", ("NYY", "BOS")), ("linescore-suspended.json", "Suspended: Rain", ("NYY", "BOS"))):
            ctx, pg, _ = one(g3, name, install=True)
            fx = fixture(name)
            check(f"{detail}: the top line says so", tc(pg, ".bd-where") == [f"Fenway Park. {detail}."] and pg.locator(".bd-live").count() == 0, tc(pg, ".bd-where"))
            check(f"{detail}: plates as the API gives them, no X, no outline", ls_rows(pg) == LS(fx, *codes) and "X" not in str(ls_rows(pg)) and pg.locator(".ls .ip.cur").count() == 0, ls_rows(pg))
            adv(pg, 60 * 60000)
            check(f"{detail}: never polls", pg.api.count(1003) == 1, pg.api.calls)
            ctx.close()

        # ---------- polling ----------
        # live: every 30 seconds, then stops once the API says final
        ctx, pg, _ = one(g2, "linescore-live.json", install=True)
        n0 = pg.api.count(1002)
        adv(pg, 29000)
        n1 = pg.api.count(1002)
        adv(pg, 1500)
        n2 = pg.api.count(1002)
        adv(pg, 30000)
        n3 = pg.api.count(1002)
        check("polling live: first fetch on render, then one every 30 seconds", (n0, n1, n2, n3) == (1, 1, 2, 3), (n0, n1, n2, n3))
        pg.api.table["1002"] = "linescore-final-x.json"
        adv(pg, 30000)
        n4 = pg.api.count(1002)
        adv(pg, 10 * 60000)
        check("polling live: stops once the game is final", (n4, pg.api.count(1002)) == (4, 4), pg.api.calls)
        check("polling live: the board shows the final line score", ls_rows(pg) == LS(fixture("linescore-final-x.json"), "ATH", "SEA") and pg.locator(".bd-live").count() == 0)
        ctx.close()

        # live, tab hidden: no requests; visible again: one at once, then every 30 seconds
        ctx, pg, _ = one(g2, "linescore-live.json", install=True)
        set_hidden(pg, True)
        adv(pg, 5 * 60000)
        hidden_n = pg.api.count(1002)
        set_hidden(pg, False)
        back_n = pg.api.count(1002)
        adv(pg, 30500)
        check("polling hidden: paused while hidden, refreshes on return, then resumes", (hidden_n, back_n, pg.api.count(1002)) == (1, 2, 3), (hidden_n, back_n, pg.api.count(1002)))
        ctx.close()

        # switching games stops the old game's polling
        write_page(pj, season_data())
        ctx = new_ctx(browser)
        api = Api(SEASON_API)
        pg, _ = open_page(ctx, base, fixed="2026-04-14T14:00:00Z", api=api, install=True)
        pg.wait_for_selector(".board")
        settle(pg)
        pg.click(".gm >> nth=1")
        settle(pg)
        adv(pg, 30500)
        live_n = api.count(1002)
        pg.click(".gm >> nth=2")
        settle(pg)
        adv(pg, 5 * 60000)
        check("polling switch: the live game polls while selected and stops when another game is picked", (live_n, api.count(1002), api.count(1003)) == (2, 2, 1), api.calls)
        ctx.close()

        # preview: quiet until 15 minutes before first pitch, then every 5 minutes; switches to 30 s once live
        ctx, pg, _ = one(g1, "linescore-preview.json", fixed="2026-04-14T22:40:00Z", install=True)  # first pitch 23:10
        # each step lands just after the expected fetch, so the mocked answer arrives before the clock moves on
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
        pg.api.table["1001"] = "linescore-live.json"
        adv(pg, 5 * 60000)
        p4 = pg.api.count(1001)          # 23:05:02, now live
        live_now = tc(pg, ".bd-live")
        adv(pg, 30500)
        check("polling preview: goes live without a reload, then every 30 seconds", (p4, pg.api.count(1001)) == (4, 5) and live_now == ["Live, top 6th, 1 out"], (p4, pg.api.count(1001), live_now))
        ctx.close()

        # a live game whose fetch times out keeps polling and picks the score up on the next try
        ctx, pg, _ = one(g2, "hang", fixed="2026-04-14T21:00:00Z", install=True)
        adv(pg, 10500)
        after_abort = (pg.api.count(1002), tc(pg, ".bd-live"))
        pg.api.table["1002"] = "linescore-live.json"
        adv(pg, 30500)
        check("polling timeout: a timed-out fetch keeps page.json's line, then the next poll fills the board",
              after_abort == (1, ["Live, bottom 5th, 2 outs"]) and pg.api.count(1002) == 2 and tc(pg, ".bd-live") == ["Live, top 6th, 1 out"]
              and ls_rows(pg) == LS(fixture("linescore-live.json"), "ATH", "SEA"), (after_abort, pg.api.calls, tc(pg, ".bd-live")))
        ctx.close()

        # a preview game far from first pitch: one fetch, nothing more for hours before the window
        ctx, pg, _ = one(g1, "linescore-preview.json", fixed="2026-04-14T14:00:00Z", install=True)
        adv(pg, 8 * 60 * 60000)  # 22:00, still more than 15 minutes out
        check("polling preview: nothing hours before first pitch", pg.api.count(1001) == 1, pg.api.calls)
        ctx.close()
    finally:
        srv.terminate()


def test_errors(browser):
    root = os.path.join(tempfile.mkdtemp(prefix="dm_err_"), "site")
    shutil.copytree(SITE, root)
    os.chmod(os.path.join(root, "data"), 0o755)
    pj = os.path.join(root, "data", "page.json")
    os.chmod(pj, 0o644)
    srv = start_server(root, PORT_ERR, spa=True)
    try:
        base = f"http://127.0.0.1:{PORT_ERR}/"
        # empty slate
        d = json.load(open(os.path.join(SITE, "data", "page.json"), encoding="utf-8"))
        d["games"], d["beat_the_streak"], d["note"] = [], [], "live"
        json.dump(d, open(pj, "w", encoding="utf-8"))
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, base)
        pg.wait_for_selector("h1")
        app = " ".join(tc(pg, "#app"))
        check("empty slate: messages shown, no crash", "No games are scheduled for this date." in app and "Picks appear once lineups are posted." in app, app)
        check("empty slate: no board, no out-of-town board, no line score request", pg.locator(".board").count() == 0 and pg.locator(".oot").count() == 0 and pg.api.calls == [])
        check("empty slate: no errors", not log["errors"], log["errors"])
        ctx.close()
        # broken page.json (the server answers index.html, like a host does for a missing file)
        open(pj, "w", encoding="utf-8").write("{ not json")
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, base)
        pg.wait_for_selector("#app [role=alert]")
        check("broken page.json: friendly error with role=alert", tc(pg, "#app [role=alert]") == ["The predictions did not load. Try reloading the page."], tc(pg, "#app"))
        ctx.close()
        # no games key
        open(pj, "w", encoding="utf-8").write('{"date":"2026-04-14"}')
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, base)
        pg.wait_for_selector("#app [role=alert]")
        check("page.json without games: friendly error", tc(pg, "#app [role=alert]") == ["The predictions did not load. Try reloading the page."])
        ctx.close()
        # games that cannot be drawn
        open(pj, "w", encoding="utf-8").write('{"date":"2026-04-14","games":[null]}')
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, base)
        pg.wait_for_selector("#app [role=alert]")
        check("page.json that cannot be shown: second error message", tc(pg, "#app [role=alert]") == ["The predictions loaded but could not be shown. Try reloading the page."], tc(pg, "#app"))
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
