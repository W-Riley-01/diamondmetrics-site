"""Browser tests for the static site (run: python tests/test_site.py; needs `pip install playwright` and `playwright install chromium`).
Expected values are computed here from page.json with separate code (Decimal rounding, not the page's JS), then compared with
what the browser actually shows. Full-page screenshots go to tests/screens/ (ignored by git)."""
import copy, json, os, shutil, subprocess, sys, tempfile, time
from decimal import Decimal, ROUND_HALF_UP
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.abspath(os.environ.get("SITE_DIR") or os.path.join(HERE, "..", "public"))
SCREENS = os.path.join(HERE, "screens")
PORT_MAIN, PORT_SEASON, PORT_ERR = 8765, 8766, 8767
RESULTS = []

YELLOW, GREEN = "rgb(242, 193, 46)", "rgb(31, 74, 56)"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  -> " + str(detail)))


# ---------- independent expectations ----------
def W(p):
    return int((Decimal(str(p)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def PCT(p):
    r = W(p)
    return "<1%" if r <= 0 else ">99%" if r >= 100 else f"{r}%"


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


CODE = {"Milwaukee Brewers": "MIL", "Tampa Bay Rays": "TB", "New York Yankees": "NYY", "Boston Red Sox": "BOS",
        "Athletics": "ATH", "Seattle Mariners": "SEA"}
NICK = {"Milwaukee Brewers": "Brewers", "Tampa Bay Rays": "Rays", "New York Yankees": "Yankees", "Boston Red Sox": "Red Sox",
        "Athletics": "Athletics", "Seattle Mariners": "Mariners"}


def norm(s):
    return " ".join(s.split())


def start_server(root, port, spa=False):
    cmd = [sys.executable, os.path.join(HERE, "serve.py"), root, "--port", str(port)] + (["--spa"] if spa else [])
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    return p


def new_ctx(browser, w=1280, h=900, tz="America/New_York"):
    return browser.new_context(viewport={"width": w, "height": h}, timezone_id=tz, locale="en-US", device_scale_factor=1)


def open_page(ctx, url, fixed="2026-11-18T17:00:00Z"):  # clock times are UTC, so the result doesn't depend on this machine's zone
    pg = ctx.new_page()
    log = {"console": [], "errors": [], "requests": [], "responses": []}
    pg.on("console", lambda m: log["console"].append((m.type, m.text)))
    pg.on("pageerror", lambda e: log["errors"].append(str(e)))
    pg.on("request", lambda r: log["requests"].append(r.url))
    pg.on("response", lambda r: log["responses"].append((r.url, r.status, r.headers)))
    pg.clock.set_fixed_time(fixed)
    pg.goto(url)
    return pg, log


def page_msgs(log):
    """Every console message; any one fails the check."""
    return log["console"]


def tc(pg, sel):
    """textContent of every match, whitespace collapsed (includes screen-reader-only text)."""
    return [norm(x) for x in pg.locator(sel).all_text_contents()]


def classes(pg, sel):
    return pg.eval_on_selector_all(sel, "els => els.map(e => e.className)")


def no_hscroll(pg):
    return pg.evaluate("document.documentElement.scrollWidth <= innerWidth")


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


# =====================================================================
def test_real(browser, base):
    page_json = json.load(open(os.path.join(SITE, "data", "page.json"), encoding="utf-8"))
    site_json = json.load(open(os.path.join(SITE, "data", "site.json"), encoding="utf-8"))
    g = page_json["games"][0]
    m, res = g["markets"], g["results"]

    ctx = new_ctx(browser)
    pg, log = open_page(ctx, base)
    pg.wait_for_selector("h1")
    pg.evaluate("document.fonts.ready")
    pg.wait_for_timeout(300)

    pg.keyboard.press("Tab")
    check("a11y: skip link is the first focusable element and becomes visible", pg.evaluate("document.activeElement.className") == "skip" and pg.evaluate("document.activeElement.getBoundingClientRect().left >= 0"))
    pg.evaluate("document.activeElement.blur()")
    check("a11y: exactly one h1", pg.locator("h1").count() == 1)
    check("real: no console messages or page errors (includes CSP violations)", not page_msgs(log) and not log["errors"], (log["console"], log["errors"]))
    origin = base.rstrip("/")
    check("real: every request is same-origin", all(u.startswith(origin) for u in log["requests"]), [u for u in log["requests"] if not u.startswith(origin)])
    check("real: every response has the CSP header", all("content-security-policy" in h for _, _, h in log["responses"]))
    check("real: no failed responses", all(s < 400 for _, s, _ in log["responses"]), [(u, s) for u, s, _ in log["responses"] if s >= 400])
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

    # ---------- the board ----------
    check("board: has id today", pg.locator("section#today.board").count() == 1)
    check("board: top line (park, indoors, game date with no timezone shift)", tc(pg, ".bd-where") == ["Tropicana Field, indoors. Wednesday, October 28, 2026."], tc(pg, ".bd-where"))
    check("board: no live line on a final game", pg.locator(".bd-live").count() == 0)
    heads = tc(pg, ".bd thead th")
    check("board: column headers", heads == ["Team", "Win chance", "Ahead after 5", "Starter strikeouts", "Final"], heads)
    check("board: Final header in the separated last column", classes(pg, ".bd thead th")[-1] == "last")
    check("board: caption, column and row headers", pg.locator(".bd caption").count() == 1 and pg.locator(".bd th[scope=col]").count() == 5 and pg.locator(".bd th[scope=row]").count() == 2)
    teams = [tc(pg, ".bd th.team .nick"), tc(pg, ".bd th.team .pitch")]
    check("board: team cells (nickname + starter's last name)", teams == [["Brewers", "Rays"], ["Misiorowski pitching", "Rasmussen pitching"]], teams)
    for i, side in enumerate(("away", "home")):
        row = pg.locator(".bd tbody tr").nth(i)
        plates = [x.strip() for x in row.locator(".plate").all_text_contents()]
        st = g["starters"][side]
        exp = [PCT(m[side + "_win"]), PCT(m["f5_" + side]), RUNS(st["median_k"]).split()[0], str(g[side + "_score"])]
        check(f"board {side}: plate numbers", plates == exp, (plates, exp))
        cls = row.locator(".plate").evaluate_all("els => els.map(e => e.className)")
        expc = [f"plate {BT(m[side+'_win'], 'ml')}", f"plate {BT(m['f5_'+side], 'f5')}", "plate b-plain", "plate b-plain"]
        check(f"board {side}: plate tier classes", cls == expc, (cls, expc))
        f5cap = [norm(x) for x in row.locator("td[data-label='Ahead after 5'] .cap").all_text_contents()]
        check(f"board {side}: 'led after 5' only under the team that led", f5cap == (["led after 5"] if res["led_after_5"] == side else []), f5cap)
        kcap = [norm(x) for x in row.locator("td[data-label='Starter strikeouts'] .cap").all_text_contents()]
        check(f"board {side}: strikeout caption with the actual count", kcap == [f"median, threw {res['starter_k'][side]}"], kcap)
    check("board: the % sign is its own smaller span", pg.locator(".bd .plate .pc").count() == 4)

    # lamp row (WS5: no run in the 1st -> lit; Brewers led after 5 -> tied lamp unlit; median total has no lamp)
    nr_lit = res["run_in_first"] is False
    tie_lit = res["led_after_5"] not in ("away", "home")
    check("lamps: no-run text and tier", tc(pg, ".lr-nr") == [f"No run in the 1st: {PCT(m['no_run_first'])}" + (" It happened." if nr_lit else "")] and classes(pg, ".lr-nr .num") == [f"num {BT(m['no_run_first'], 'nrfi')}"], (tc(pg, ".lr-nr"), classes(pg, ".lr-nr .num")))
    check("lamps: lamp-row values are bold numerals, no plates", pg.locator(".lamprow .mini, .lamprow .plate").count() == 0 and pg.locator(".lamprow b.num").count() == 3)
    check("lamps: no-run lamp is lit", pg.locator(".lr-nr .lamp.on").count() == 1 and nr_lit)
    check("lamps: tied-after-5 text", tc(pg, ".lr-tie") == [f"Tied after 5: {PCT(m['f5_tie'])}" + (" It happened." if tie_lit else "")], tc(pg, ".lr-tie"))
    check("lamps: tied-after-5 lamp is present and unlit", pg.locator(".lr-tie .lamp").count() == 1 and pg.locator(".lr-tie .lamp.on").count() == 0 and not tie_lit)
    check("lamps: median total, no lamp", tc(pg, ".lr-tot") == [f"Median total {RUNS(m['median_total'])}; {res['total_runs']} scored"] and pg.locator(".lr-tot .lamp").count() == 0, tc(pg, ".lr-tot"))
    check("lamps: legend", tc(pg, ".lr-note") == ["A lit lamp means it happened."])
    check("lamps: hidden from screen readers", all(a == "true" for a in pg.eval_on_selector_all(".lamp", "els => els.map(e => e.getAttribute('aria-hidden'))")))

    # ---------- conditions (dome) ----------
    check("conditions: dome message", tc(pg, ".cond-note") == ["Dome. The model treats Tropicana Field as indoors, so weather is not a factor."], tc(pg, ".cond-note"))
    check("conditions: no outdoor readings for a dome", pg.locator(".wx").count() == 0)
    check("conditions: HR box text", tc(pg, ".hrbox .hrrow > span") == [f"Home run conditions: {g['weather']['hr_label']}", f"{g['weather']['hr_score']} out of 100"], tc(pg, ".hrbox .hrrow > span"))
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
        fi = p["first_inning"]
        exp_fi = (f"First inning over {fi['starts']} starts: {Decimal(str(fi['era'])).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} ERA, "
                  f"scoreless {fi['scoreless']} times, {str(Decimal(str(fi['opp_obp'])).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP))[1:]} opponent OBP.")
        check(f"pitchers {side}: first-inning line", t(".first") == [exp_fi], (t(".first"), exp_fi))
    check("pitchers: Rasmussen's 3+ and 4+ checked, nothing for Misiorowski", pits.nth(0).locator("svg").count() == 0 and pits.nth(1).locator("svg").count() == 2)

    # ---------- lineups: every cell, every chip class, every check ----------
    by_id = {r["id"]: r for side in ("away", "home") for r in res["batters"][side]}
    tabs = pg.locator("table.t")
    check("lineups: two tables with a Result column", tabs.count() == 2 and tabs.nth(0).locator("thead th").count() == 8)
    check("lineups: column headers", tc(pg, "table.t >> nth=0 >> thead th") == ["#", "Player", "1+ hit", "2+ bases", "Homer", "RBI", "Steal", "Result"], tc(pg, "table.t >> nth=0 >> thead th"))
    check("lineups: headings use nicknames", tc(pg, ".lu h3") == ["Brewers batting order", "Rays batting order"], tc(pg, ".lu h3"))
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
    name_to_id = {r["name"]: r["id"] for side in ("away", "home") for r in g["lineups"][side]}
    picks = page_json["beat_the_streak"]
    exp_meta = [f"{NICK[p['team']]} {'vs' if p['home'] else 'at'} {NICK[p['opponent']]}. " + ("Got a hit." if by_id[name_to_id[p["name"]]]["h"] >= 1 else "No hit.") for p in picks]
    check("BTS: ranks", tc(pg, ".bts .rk") == ["1", "2", "3", "4", "5"], tc(pg, ".bts .rk"))
    check("BTS: names", tc(pg, ".bts .name") == [p["name"] for p in picks])
    check("BTS: matchup and result lines from the box score", tc(pg, ".bts .meta") == exp_meta, (tc(pg, ".bts .meta"), exp_meta))
    check("BTS: hit chances", tc(pg, ".bts .p") == [PCT(p["hit"]) for p in picks], tc(pg, ".bts .p"))
    check("BTS: chip classes", classes(pg, ".bts .p") == [f"p {TIER(p['hit'], 'hit')}" for p in picks], classes(pg, ".bts .p"))
    check("BTS: section is the #bts anchor", pg.locator("section#bts h2").count() == 1)

    # ---------- reading guide ----------
    legend = [tc(pg, ".legend .p"), tc(pg, ".legend li > span:last-child")]
    check("guide: legend always visible", legend == [["74%", "65%", "33%"], ["Strong. It clears the cut-off for that market.", "Worth watching.", "Below the watch line."]] and pg.is_visible(".legend"), legend)
    check("guide: legend chips use the table tiers", classes(pg, ".legend .p") == ["p p-strong", "p p-watch", "p p-low"])
    ind = "() => { const s = document.querySelector('.how summary'), b = getComputedStyle(s, '::before'); return [b.content, b.borderLeftWidth, b.transform, s.getBoundingClientRect().height >= 44]; }"
    closed = pg.evaluate(ind)
    check("guide: summary shows an open indicator, 44px tall", closed[0] == '""' and closed[1] == "8px" and closed[2] == "none" and closed[3], closed)
    pg.click(".how summary")
    opened = pg.evaluate(ind)
    check("guide: indicator turns when open", opened[2] != "none" and opened[3], opened)
    rows = tc(pg, ".how tbody tr")
    exp = {"Win chance": "ml", "Ahead after 5 innings": "f5", "No run in the 1st": "nrfi", "Starter 3+ strikeouts": "k3",
           "Starter 4+ strikeouts": "k4", "Starter 5+ strikeouts": "k5", "Starter 6+ strikeouts": "k6", "Starter 7+ strikeouts": "k7",
           "Batter 1+ hit": "hit", "Batter 2+ total bases": "tb", "Batter 1+ home run": "hr", "Batter 1+ RBI": "rbi", "Batter 1+ stolen base": "sb"}
    check("guide: thresholds table equals the original app's cut-offs", rows == [f"{k}{TH[v][0]}%+{TH[v][1]}%+" for k, v in exp.items()], rows)
    guide_txt = norm(" ".join(tc(pg, ".how")))
    check("guide: HR tiers as in the original colour guide", all(s in guide_txt for s in ["65 and up Very HR Friendly", "55 to 64 HR Friendly", "45 to 54 Neutral", "35 to 44 HR Suppressed", "under 35 Very HR Suppressed", "One point is about one foot"]), guide_txt)

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
        pg.wait_for_timeout(150)
        check(f"page: no horizontal scroll at {w}", no_hscroll(pg), pg.evaluate("document.documentElement.scrollWidth"))
        plates_in = pg.evaluate("(() => { const b = document.querySelector('.board').getBoundingClientRect(); return [...document.querySelectorAll('.board .plate')].every(p => p.getBoundingClientRect().right <= b.right - 8); })()")
        check(f"board at {w}: every plate inside the board", plates_in)
    pg.evaluate("document.activeElement.blur()")
    screenshot(pg, "replay-1440.png")
    ctx.close()

    # ---------- phone ----------
    ctx = new_ctx(browser, 390, 844)
    pg, log = open_page(ctx, base)
    pg.wait_for_selector("h1")
    pg.evaluate("document.fonts.ready")
    pg.wait_for_timeout(300)
    check("phone 390: no horizontal page scroll", no_hscroll(pg), pg.evaluate("document.documentElement.scrollWidth"))
    check("phone 390: no console messages", not page_msgs(log) and not log["errors"], log["console"])
    wide = pg.evaluate("[...document.querySelectorAll('.scroll')].map(e => e.scrollWidth > e.clientWidth)")
    check("phone 390: lineup tables scroll inside their own box", wide == [True, True], wide)
    labels = pg.evaluate("[...document.querySelectorAll('.bd tbody tr:first-child td')].map(td => getComputedStyle(td, '::before').content)")
    check("phone 390: board cells labelled from data-label", labels == ['"Win chance"', '"Ahead after 5"', '"Starter strikeouts"', '"Final"'], labels)
    sizes = pg.evaluate("(() => { const p = document.querySelector('.plate'); return [parseFloat(getComputedStyle(p).fontSize), parseFloat(getComputedStyle(p.querySelector('.pc')).fontSize), parseFloat(getComputedStyle(document.querySelector('h1')).fontSize)]; })()")
    check("phone 390: plates 36-40px with an 18-20px % sign, h1 about 48px", 36 <= sizes[0] <= 40 and 18 <= sizes[1] <= 20 and sizes[2] == 48, sizes)
    check("phone 390: each team is one block (name row on top, plates below)", pg.evaluate("[...document.querySelectorAll('.bd tbody tr')].every(tr => { const th = tr.querySelector('th').getBoundingClientRect(); return [...tr.querySelectorAll('td')].every(td => td.getBoundingClientRect().top >= th.bottom); })"))
    check("phone 390: plates inside the board", pg.evaluate("(() => { const b = document.querySelector('.board').getBoundingClientRect(); return [...document.querySelectorAll('.board .plate')].every(p => p.getBoundingClientRect().right <= b.right); })()"))
    check("phone 390: team names on one line", pg.evaluate("[...document.querySelectorAll('.nick')].every(n => n.getBoundingClientRect().height < parseFloat(getComputedStyle(n).fontSize) * 1.5)"))
    screenshot(pg, "replay-390.png")
    ctx.close()

    # ---------- timezone: the game date must not shift for a viewer far from the US ----------
    ctx = new_ctx(browser, tz="Pacific/Auckland")
    pg, log = open_page(ctx, base, fixed="2026-11-18T21:00:00Z")  # 10 AM, Nov 19 in Auckland
    pg.wait_for_selector("h1")
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
    g2["markets"].update({"away_win": 0.4123, "home_win": 0.5877, "f5_away": 0.3041, "f5_home": 0.5512, "f5_tie": 0.1447, "no_run_first": 0.6233})
    g2["starters"]["away"] = None
    g2["starters"]["home"]["first_inning"] = None
    g3 = mk(1003, "New York Yankees", "Boston Red Sox", "final", park="Fenway Park", roof="open", weather=outdoor,
            start_time_utc="2026-04-14T17:05:00Z", away_score=6, home_score=5)
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


def board_plates(pg):
    return [[x.strip() for x in pg.locator(".bd tbody tr").nth(i).locator(".plate").all_text_contents()] for i in range(2)]


def test_season(browser):
    root = os.path.join(tempfile.mkdtemp(prefix="dm_season_"), "site")
    shutil.copytree(SITE, root)
    os.remove(os.path.join(root, "data", "site.json"))  # in season: no countdown file
    os.chmod(os.path.join(root, "data"), 0o755)
    pj = os.path.join(root, "data", "page.json")
    os.chmod(pj, 0o644)
    d = season_data()
    json.dump(d, open(pj, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    g1, g2, g3 = d["games"]
    srv = start_server(root, PORT_SEASON, spa=True)  # spa=True: a host answering index.html for a missing site.json
    try:
        base = f"http://127.0.0.1:{PORT_SEASON}/"
        ctx = new_ctx(browser, 1440, 900)
        pg, log = open_page(ctx, base, fixed="2026-04-14T14:00:00Z")
        pg.wait_for_selector("h1")
        pg.evaluate("document.fonts.ready")
        check("season: no console errors even though site.json falls back to index.html", not log["errors"] and not [c for c in log["console"] if c[0] == "error" and "CSP" in c[1]], log)
        check("season: countdown hidden when site.json is missing", not pg.is_visible("#banner"))
        check("season: no replay notice", pg.locator(".notice").count() == 0 and pg.locator(".lede").count() == 0)
        check("season: h1 is the long date, only one h1", tc(pg, "h1") == ["Tuesday, April 14, 2026"], tc(pg, "h1"))
        check("season: games and updated line", tc(pg, ".upd") == ["3 games. Updated Apr 14, 2026, 11:00 AM EDT."], tc(pg, ".upd"))
        gap = pg.evaluate("getComputedStyle(document.querySelector('h1 + .upd')).marginTop")
        check("season: updated line sits 14px under the title, as the replay subline does", gap == "14px", gap)

        # out-of-town board
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

        # BTS: scheduled and live games have no result; a pick with game_id finds the right game; unknown name shows nothing
        meta = tc(pg, ".bts .meta")
        check("season BTS: result only where a final box score has the player", [("Got a hit" in x, "No hit" in x) for x in meta] == [(False, False), (True, False), (False, True), (True, False), (False, False)], meta)
        check("season BTS: matchup lines use nicknames", meta[0] == "Yankees at Red Sox." and meta[2] == "Red Sox vs Yankees. No hit." and meta[4] == "Mariners vs Athletics.", meta)
        check("season BTS: tiny probability shows <1%, near-certain >99%", tc(pg, ".bts .p")[4] == "<1%" and tc(pg, ".bts .p")[0] == ">99%", tc(pg, ".bts .p"))

        # game 1: scheduled, outdoor weather
        check("season game 1: top line gives first pitch", tc(pg, ".bd-where") == ["Fenway Park. First pitch 7:10 PM EDT."], tc(pg, ".bd-where"))
        check("season game 1: no last column, no live line", pg.locator(".bd th[scope=col]").count() == 4 and pg.locator(".bd .last").count() == 0 and pg.locator(".bd-live").count() == 0)
        check("season game 1: board numbers", board_plates(pg) == [[PCT(g1["markets"]["away_win"]), PCT(g1["markets"]["f5_away"]), "6"], [PCT(g1["markets"]["home_win"]), PCT(g1["markets"]["f5_home"]), "6"]], board_plates(pg))
        check("season game 1: captions say median only, nobody led after 5", tc(pg, ".bd .cap") == ["median", "median"], tc(pg, ".bd .cap"))
        check("season game 1: lamp row without lamps or legend", pg.locator(".lamprow .lamp").count() == 0 and pg.locator(".lr-note").count() == 0)
        check("season game 1: median total not whole shows one decimal, nothing scored yet", tc(pg, ".lr-tot") == [f"Median total {RUNS(g1['markets']['median_total'])}"], tc(pg, ".lr-tot"))
        wx = tc(pg, ".wx > div")
        check("season game 1: six weather readings", wx == ["Temperature61°F", "Humidity58%", "Pressure30.11 inHg", "Wind12 mph from SW", "Toward center fieldOut to CF (7 mph)", "Carry change+6 ft"], wx)
        check("season game 1: HR box and hot bar", tc(pg, ".hrbox .hrrow > span") == ["Home run conditions: HR Friendly", "63 out of 100"] and pg.locator(".fill.hot").count() == 1, tc(pg, ".hrbox .hrrow > span"))
        check("season game 1: totals sub line without a result", tc(pg, ".totals .subl") == ["Chance the game goes over each line."])
        check("season game 1: lineup table has no Result column and no checks", pg.locator("table.t").first.locator("thead th").count() == 7 and pg.locator("table.t svg").count() == 0)
        g1_plates = board_plates(pg)

        # game 2: live, dome, missing weather, missing starter, missing first-inning record
        pg.click(".gm >> nth=1")
        check("season game 2: selection moves", [pg.get_attribute(f".gm >> nth={i}", "aria-pressed") for i in range(3)] == ["false", "true", "false"])
        check("season game 2: focus stays on the button that was clicked", pg.evaluate("document.activeElement.getAttribute('data-id')") == "1002")
        check("season game 2: board numbers changed to game 2", board_plates(pg) != g1_plates and board_plates(pg) == [[PCT(0.4123), PCT(0.3041), "–", "2"], [PCT(0.5877), PCT(0.5512), "6", "3"]], board_plates(pg))
        check("season game 2: plate tiers follow the new numbers", classes(pg, ".bd tbody tr >> nth=1 >> .plate")[:2] == [f"plate {BT(0.5877, 'ml')}", f"plate {BT(0.5512, 'f5')}"], classes(pg, ".bd tbody tr >> nth=1 >> .plate"))
        check("season game 2: live line with a lit lamp", tc(pg, ".bd-live") == ["Live, bottom 5th, 2 outs"] and pg.locator(".bd-live .lamp.on").count() == 1, tc(pg, ".bd-live"))
        check("season game 2: top line gives the date for a live game", tc(pg, ".bd-where") == ["T-Mobile Park, indoors. Tuesday, April 14, 2026."], tc(pg, ".bd-where"))
        check("season game 2: last column is Runs", tc(pg, ".bd thead th.last") == ["Runs"])
        teams = [tc(pg, ".bd th.team .nick"), tc(pg, ".bd th.team .pitch")]
        check("season game 2: starter not announced on the board", teams == [["Athletics", "Mariners"], ["Starter not announced", "Rasmussen pitching"]], teams)
        check("season game 2: no 'led after 5' and no lamps while live", "led after 5" not in tc(pg, ".bd .cap") and pg.locator(".lamprow .lamp").count() == 0)
        check("season game 2: forecast missing message", tc(pg, ".cond-note") == ["The forecast for this park was not available when the prediction ran."], tc(pg, ".cond-note"))
        pits = tc(pg, ".pit")
        check("season game 2: starter not announced + first-inning fallback", "Starter not announced" in pits[0] and "First-inning record not shown (fewer than 5 starts)." in pits[1], pits)

        # game 3: final, a run in the 1st, Red Sox led after 5, over lines exceeded
        pg.click(".gm >> nth=2")
        r3 = g3["results"]
        caps = [[norm(x) for x in pg.locator(".bd tbody tr").nth(i).locator("td[data-label='Ahead after 5'] .cap").all_text_contents()] for i in range(2)]
        check("season game 3: 'led after 5' under the Red Sox plate only", caps == [[], ["led after 5"]], caps)
        check("season game 3: final score plates", [p[-1] for p in board_plates(pg)] == ["6", "5"] and tc(pg, ".bd thead th.last") == ["Final"])
        check("season game 3: no-run lamp unlit (a run scored in the 1st)", pg.locator(".lr-nr .lamp").count() == 1 and pg.locator(".lr-nr .lamp.on").count() == 0 and "It happened" not in tc(pg, ".lr-nr")[0])
        check("season game 3: tied lamp unlit (Red Sox led)", pg.locator(".lr-tie .lamp").count() == 1 and pg.locator(".lr-tie .lamp.on").count() == 0)
        check("season game 3: median total with runs scored", tc(pg, ".lr-tot") == [f"Median total {RUNS(g3['markets']['median_total'])}; {r3['total_runs']} scored"], tc(pg, ".lr-tot"))
        lines = sorted((k for k in g3["markets"]["total_over"] if k.endswith(".5")), key=float)
        over = sum(r3["total_runs"] > float(k) for k in lines)
        check("season game 3: totals sub line counts the lines it went over", tc(pg, ".totals .subl") == [f"Chance the game goes over each line. It finished with {RUNS(r3['total_runs'])}, over {over} of them."], tc(pg, ".totals .subl"))
        marks = [pg.locator(".tot-row").nth(i).locator("svg").count() == 1 for i in range(len(lines))]
        check("season game 3: a check after each line that went over", marks == [r3["total_runs"] > float(k) for k in lines] and sum(marks) == 5, marks)
        check("season: no console errors after clicking around", not log["errors"], log["errors"])
        check("season: no horizontal scroll at 1440", no_hscroll(pg))
        pg.click(".gm >> nth=0")
        screenshot(pg, "season-1440.png")
        fits = """() => [...document.querySelectorAll('.oot .gm')].flatMap((b, i) => {
            const r = b.getBoundingClientRect(), bad = [];
            if (b.scrollWidth > b.clientWidth) bad.push(i + ': scrollWidth ' + b.scrollWidth + ' > ' + b.clientWidth);
            for (const c of b.children) { const k = c.getBoundingClientRect();
              if (k.left < r.left - 0.5 || k.right > r.right + 0.5 || k.top < r.top - 0.5 || k.bottom > r.bottom + 0.5) bad.push(i + ': ' + c.className + ' outside'); }
            return bad; })"""
        for w in (1440, 1280, 768, 390):
            pg.set_viewport_size({"width": w, "height": 900})
            pg.wait_for_timeout(150)
            bad = pg.evaluate(fits)
            check(f"season {w}: nothing overflows its game button", not bad, bad)
        pg.set_viewport_size({"width": 390, "height": 844})
        pg.wait_for_timeout(150)
        check("season: no horizontal scroll at 390", no_hscroll(pg), pg.evaluate("document.documentElement.scrollWidth"))
        pg.click(".gm >> nth=1")
        screenshot(pg, "season-live-390.png")
        ctx.close()

        # tied after 5 lights the lamp; an unknown result shows no lamp at all
        d2 = season_data()
        d2["games"] = [d2["games"][2]]
        d2["games"][0]["results"]["led_after_5"] = "tie"
        d2["games"][0]["results"]["run_in_first"] = None
        json.dump(d2, open(pj, "w", encoding="utf-8"), ensure_ascii=False)
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, base, fixed="2026-04-14T14:00:00Z")
        pg.wait_for_selector(".lamprow")
        check("lamps: a tie lights the tied lamp and says so", pg.locator(".lr-tie .lamp.on").count() == 1 and tc(pg, ".lr-tie")[0].endswith("It happened."), tc(pg, ".lr-tie"))
        check("lamps: run_in_first not a boolean means no no-run lamp", pg.locator(".lr-nr .lamp").count() == 0)
        check("lamps: a tie puts 'led after 5' under neither team", "led after 5" not in tc(pg, ".bd .cap"))
        ctx.close()
        d2["games"][0]["results"]["led_after_5"] = None
        json.dump(d2, open(pj, "w", encoding="utf-8"), ensure_ascii=False)
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, base, fixed="2026-04-14T14:00:00Z")
        pg.wait_for_selector(".lamprow")
        check("lamps: led_after_5 null means no tied lamp and no lamp legend", pg.locator(".lamprow .lamp").count() == 0 and pg.locator(".lr-note").count() == 0)
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
        check("empty slate: no board, no out-of-town board", pg.locator(".board").count() == 0 and pg.locator(".oot").count() == 0)
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
            b = p.chromium.launch()
            test_real(b, f"http://127.0.0.1:{PORT_MAIN}/")
            test_season(b)
            test_errors(b)
            b.close()
    finally:
        srv.terminate()
    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)} of {len(RESULTS)} checks passed")
    sys.exit(1 if bad else 0)
