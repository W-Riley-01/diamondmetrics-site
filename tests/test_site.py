"""Browser tests for the static site (run: python tests/test_site.py; needs `pip install playwright` and `playwright install chromium`). Expected values are computed here from page.json with separate code
(Decimal rounding, not the page's JS), then compared with what the browser actually shows."""
import copy, json, os, shutil, subprocess, sys, tempfile, time
from decimal import Decimal, ROUND_HALF_UP
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.abspath(os.environ.get("SITE_DIR") or os.path.join(HERE, "..", "public"))
PORT_MAIN, PORT_SEASON, PORT_ERR = 8765, 8766, 8767
RESULTS = []


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


CODE = {"Milwaukee Brewers": "MIL", "Tampa Bay Rays": "TB", "New York Yankees": "NYY", "Boston Red Sox": "BOS",
        "Athletics": "ATH", "Seattle Mariners": "SEA"}


def norm(s):
    return " ".join(s.split())


def start_server(root, port, spa=False):
    cmd = [sys.executable, os.path.join(HERE, "serve.py"), root, "--port", str(port)] + (["--spa"] if spa else [])
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    return p


def new_ctx(browser, w=1280, h=900, tz="America/New_York"):
    return browser.new_context(viewport={"width": w, "height": h}, timezone_id=tz, locale="en-US", device_scale_factor=1)


def open_page(ctx, url, fixed="2026-11-18T12:00:00"):
    pg = ctx.new_page()
    log = {"console": [], "errors": [], "requests": [], "responses": []}
    pg.on("console", lambda m: log["console"].append((m.type, m.text)))
    pg.on("pageerror", lambda e: log["errors"].append(str(e)))
    pg.on("request", lambda r: log["requests"].append(r.url))
    pg.on("response", lambda r: log["responses"].append((r.url, r.status, r.headers)))
    pg.clock.set_fixed_time(fixed)
    pg.goto(url)
    return pg, log


JOIN_JS = ("e => { const w = document.createTreeWalker(e, NodeFilter.SHOW_TEXT); const out = []; let n; "
           "while ((n = w.nextNode())) { const t = n.nodeValue.trim(); if (t) out.push(t); } "
           "return out.join(' ').replace(/\\s+/g, ' ').trim(); }")


def texts(pg, sel):
    return pg.eval_on_selector_all(sel, "els => els.map(" + JOIN_JS + ")")


def classes(pg, sel):
    return pg.eval_on_selector_all(sel, "els => els.map(e => e.className)")


# =====================================================================
def test_real(browser, base):
    page_json = json.load(open(os.path.join(SITE, "data", "page.json"), encoding="utf-8"))
    site_json = json.load(open(os.path.join(SITE, "data", "site.json"), encoding="utf-8"))
    g = page_json["games"][0]
    m, res = g["markets"], g["results"]
    A, H = "MIL", "TB"

    ctx = new_ctx(browser)
    pg, log = open_page(ctx, base)
    pg.wait_for_selector("h1")
    pg.evaluate("document.fonts.ready")
    pg.wait_for_timeout(300)

    pg.keyboard.press("Tab")
    check("a11y: skip link is the first focusable element and becomes visible", pg.evaluate("document.activeElement.className") == "skip" and pg.evaluate("document.activeElement.getBoundingClientRect().left >= 0"))
    check("a11y: exactly one h1", pg.locator("h1").count() == 1)
    pg.evaluate("document.activeElement.blur()")
    check("real: no console messages or page errors (includes CSP violations)", not log["console"] and not log["errors"], (log["console"], log["errors"]))
    origin = base.rstrip("/")
    check("real: every request is same-origin", all(u.startswith(origin) for u in log["requests"]), [u for u in log["requests"] if not u.startswith(origin)])
    check("real: every response has the CSP header", all("content-security-policy" in h for _, _, h in log["responses"]))
    check("real: no failed responses", all(s < 400 for _, s, _ in log["responses"]), [(u, s) for u, s, _ in log["responses"] if s >= 400])
    fonts_loaded = [u for u in log["requests"] if u.endswith(".woff2")]
    check("real: both self-hosted fonts requested", len(fonts_loaded) == 2, fonts_loaded)
    check("real: fonts actually usable", pg.evaluate("document.fonts.check('16px Inter') && document.fonts.check('700 20px \"Space Grotesk\"')"))

    # banner (fixed clock 2026-11-18 -> Opening Day 2027-03-24 is 126 days)
    check("banner: number is 126", texts(pg, "#banner .count-n") == ["126"], texts(pg, "#banner .count-n"))
    check("banner: text", texts(pg, "#banner .count-t") == ["days until Opening Day 2027 · Wednesday, March 24"], texts(pg, "#banner .count-t"))
    check("banner: note matches site.json", texts(pg, "#banner .banner-note") == [site_json["opening_note"]])

    # header block
    check("h1", texts(pg, "h1") == ["MLB predictions"])
    check("headline from site.json", texts(pg, "p.sub") == [site_json["replay_headline"]], texts(pg, "p.sub"))
    # 2026-11-18T17:13:27Z is 12:13 PM EST
    check("date + updated line (date has no timezone shift)", texts(pg, "main p.hint")[0] == "Wednesday, October 28, 2026 · Updated Nov 18, 2026, 12:13 PM EST", texts(pg, "main p.hint")[0])
    check("replay notice is the label verbatim", texts(pg, ".notice") == [page_json["label"]], texts(pg, ".notice"))

    # Beat the Streak, with results computed from the box-score data in the file
    by_id = {}
    for side in ("away", "home"):
        for r in res["batters"][side]:
            by_id[r["id"]] = r
    name_to_id = {r["name"]: r["id"] for side in ("away", "home") for r in g["lineups"][side]}
    exp_meta, exp_name, exp_pv, exp_res = [], [], [], []
    for i, p in enumerate(page_json["beat_the_streak"]):
        exp_meta.append(f"#{i+1} · {CODE[p['team']]} {'vs' if p['home'] else '@'} {CODE[p['opponent']]}")
        exp_name.append(p["name"])
        exp_pv.append(PCT(p["hit"]))
        exp_res.append("Got a hit" if by_id[name_to_id[p["name"]]]["h"] >= 1 else "No hit")
    check("BTS: matchup lines", texts(pg, ".bts .meta") == exp_meta, texts(pg, ".bts .meta"))
    check("BTS: names", texts(pg, ".bts .name") == exp_name)
    check("BTS: percents", texts(pg, ".bts .pv") == exp_pv, texts(pg, ".bts .pv"))
    check("BTS: got a hit / no hit from the box score", texts(pg, ".bts .res") == exp_res, texts(pg, ".bts .res"))

    # game list
    check("list: one button, pressed", pg.locator(".game-btn").count() == 1 and pg.get_attribute(".game-btn", "aria-pressed") == "true")
    check("list: status text", texts(pg, ".game-btn .r1") == [f"{A} @ {H} Final {g['away_score']}–{g['home_score']}"], texts(pg, ".game-btn .r1"))
    check("list: summary line", texts(pg, ".game-btn .r2") == [f"Win {A} {PCT(m['away_win'])}, {H} {PCT(m['home_win'])} · Total {m['median_total']:.1f}"], texts(pg, ".game-btn .r2"))

    # game card
    check("card: title", texts(pg, ".gc-title") == [f"{g['away_team']} at {g['home_team']}"])
    check("card: sub line (viewer-local first pitch)", texts(pg, ".gc-sub") == ["Tropicana Field · First pitch 8:00 PM EDT"], texts(pg, ".gc-sub"))
    check("card: scoreboard", texts(pg, ".score") == [f"Final {A} {g['away_score']} · {H} {g['home_score']}"], texts(pg, ".score"))
    tiles_lbl = texts(pg, ".tile .lbl")
    tiles_val = texts(pg, ".tile .val")
    tiles_act = texts(pg, ".tile .act")
    check("tiles: labels", tiles_lbl == [f"{A} win", f"{H} win", "Median total runs", "No run in the 1st"], tiles_lbl)
    check("tiles: values", tiles_val == [PCT(m["away_win"]), PCT(m["home_win"]), f"{m['median_total']:.1f}", PCT(m["no_run_first"])], tiles_val)
    check("tiles: actual results", tiles_act == ["Won", "Lost", f"Actual: {res['total_runs']} runs", "No run scored in the 1st"], tiles_act)
    tcls = classes(pg, ".tile .val .p")
    check("tiles: highlight classes", tcls == [f"p {TIER(m['away_win'],'ml')}", f"p {TIER(m['home_win'],'ml')}", f"p {TIER(m['no_run_first'],'nrfi')}"], tcls)

    lines = [k for k in m["total_over"] if k.endswith(".5")]
    check("over chips: half-point lines only", texts(pg, ".over .chip .l") == [f"Over {k}" for k in lines], texts(pg, ".over .chip .l"))
    check("over chips: values", texts(pg, ".over .chip .v") == [PCT(m["total_over"][k]) for k in lines], texts(pg, ".over .chip .v"))
    check("over chips: no check marks (5 runs went over none of them)", pg.locator(".over .chip svg").count() == 0)
    f5 = texts(pg, ".f5")[0]
    exp_f5 = norm(f"First 5 innings: {A} ahead {PCT(m['f5_away'])} · tied {PCT(m['f5_tie'])} · {H} ahead {PCT(m['f5_home'])} Actual after five: {A} ahead.")
    check("first-5 line", norm(f5) == exp_f5, (f5, exp_f5))

    # conditions (dome)
    check("conditions: dome message", norm(texts(pg, ".cond-note")[0]) == "Dome. The model treats Tropicana Field as indoors, so weather is not a factor.", texts(pg, ".cond-note"))
    check("conditions: no outdoor weather tiles for a dome", pg.locator(".wx").count() == 0)
    hr = norm(texts(pg, ".hrbox")[0])
    check("conditions: HR box text", hr == f"Home run conditions: {g['weather']['hr_label']} {g['weather']['hr_score']} out of 100", hr)
    check("conditions: bar width = score (CSSOM style works under the CSP)", pg.evaluate("Math.abs(document.querySelector('.fill').getBoundingClientRect().width - document.querySelector('.track').getBoundingClientRect().width * 0.5) < 1") and pg.evaluate("document.querySelector('.fill').style.width") == "50%")

    # pitchers
    pits = pg.locator(".pit")
    check("pitchers: two cards", pits.count() == 2)
    for i, side in enumerate(("away", "home")):
        p = g["starters"][side]
        card = pits.nth(i)
        t = lambda sel: norm(card.locator(sel).first.evaluate(JOIN_JS))
        check(f"pitchers {side}: who + name", t(".who") == f"{CODE[g[side+'_team']]} starter" and t(".nm") == p["name"], (t(".who"), t(".nm")))
        check(f"pitchers {side}: median and actual Ks", t(".ks") == f"Median strikeouts {int(p['median_k'])} Actual {res['starter_k'][side]}", t(".ks"))
        ks = sorted(p["k_over"], key=int)
        got_l = [norm(x) for x in card.locator(".chip .l").all_text_contents()]
        got_v = [norm(x) for x in card.locator(".chip .v").all_text_contents()]
        exp_v = [PCT(p["k_over"][k]) + (" reached" if res["starter_k"][side] >= int(k) else "") for k in ks]
        check(f"pitchers {side}: ladder labels", got_l == [f"{k}+ K" for k in ks], got_l)
        check(f"pitchers {side}: ladder values and reached marks", got_v == exp_v, (got_v, exp_v))
        got_c = card.locator(".chip .p").evaluate_all("els => els.map(e => e.className)")
        check(f"pitchers {side}: ladder highlight classes", got_c == [f"p {TIER(p['k_over'][k], 'k'+k)}" for k in ks], got_c)
        fi = p["first_inning"]
        exp_fi = f"First inning, {fi['starts']} starts: ERA {fi['era']:.2f} · scoreless in {fi['scoreless']} of {fi['starts']} · opponent OBP {fi['opp_obp']:.3f}".replace("OBP 0.", "OBP .")
        check(f"pitchers {side}: first-inning line", t(".first") == exp_fi, (t(".first"), exp_fi))

    # lineups: every cell and every highlight class
    tabs = pg.locator("table.t")
    check("lineups: two tables with Result column", tabs.count() == 2 and tabs.nth(0).locator("thead th").count() == 8)
    for i, side in enumerate(("away", "home")):
        rows = tabs.nth(i).locator("tbody tr")
        check(f"lineups {side}: nine rows", rows.count() == 9)
        ok_text, ok_cls, bad = True, True, []
        for r, row in enumerate(g["lineups"][side]):
            cells = [norm(x) for x in rows.nth(r).locator("td").all_text_contents()]
            exp = [str(row["slot"]), row["name"], PCT(row["hit"]), PCT(row["tb2"]), PCT(row["hr"]), PCT(row["rbi"]), PCT(row["sb"]), by_id[row["id"]]["line"]]
            if cells != exp:
                ok_text = False; bad.append((cells, exp))
            cls = rows.nth(r).locator(".p").evaluate_all("els => els.map(e => e.className)")
            expc = [f"p {TIER(row[k], key)}" for k, key in (("hit", "hit"), ("tb2", "tb"), ("hr", "hr"), ("rbi", "rbi"), ("sb", "sb"))]
            if cls != expc:
                ok_cls = False; bad.append((cls, expc))
        check(f"lineups {side}: all cell text (incl. result lines)", ok_text, bad[:2])
        check(f"lineups {side}: all highlight classes", ok_cls, bad[:2])
    check("lineups: scroll regions are keyboard focusable", pg.locator(".scroll[tabindex='0']").count() == 2)

    # reading guide
    pg.click(".how summary")
    rows = [norm(x) for x in texts(pg, ".how tbody tr")]
    exp = {"Win chance": "ml", "Ahead after 5 innings": "f5", "No run in the 1st": "nrfi", "Starter 3+ strikeouts": "k3",
           "Starter 4+ strikeouts": "k4", "Starter 5+ strikeouts": "k5", "Starter 6+ strikeouts": "k6", "Starter 7+ strikeouts": "k7",
           "Batter 1+ hit": "hit", "Batter 2+ total bases": "tb", "Batter 1+ home run": "hr", "Batter 1+ RBI": "rbi", "Batter 1+ stolen base": "sb"}
    exp_rows = [f"{k} {TH[v][0]}%+ {TH[v][1]}%+" for k, v in exp.items()]
    check("guide: thresholds table equals the original app's cut-offs", rows == exp_rows, (rows, exp_rows))
    guide_txt = norm(texts(pg, ".how")[0])
    check("guide: HR tiers as in the original colour guide", all(s in guide_txt for s in ["65 and up Very HR Friendly", "55 to 64 HR Friendly", "45 to 54 Neutral", "35 to 44 HR Suppressed", "under 35 Very HR Suppressed", "One point is about one foot"]), guide_txt)

    # structure / accessibility basics
    check("a11y: html lang and viewport", pg.evaluate("document.documentElement.lang") == "en" and pg.locator("meta[name=viewport]").count() == 1)
    check("a11y: tables have captions and column headers", pg.locator("table.t caption").count() == 2 and pg.locator("table.t th[scope=col]").count() == 16)
    check("page: no horizontal scroll at 1280", pg.evaluate("document.documentElement.scrollWidth <= innerWidth"))
    ctx.close()

    # phone
    ctx = new_ctx(browser, 390, 844)
    pg, log = open_page(ctx, base)
    pg.wait_for_selector("h1")
    pg.wait_for_timeout(300)
    check("phone 390: no horizontal page scroll", pg.evaluate("document.documentElement.scrollWidth <= innerWidth"), pg.evaluate("document.documentElement.scrollWidth"))
    check("phone 390: no console messages", not log["console"] and not log["errors"], log["console"])
    wide = pg.evaluate("[...document.querySelectorAll('.scroll')].map(e => e.scrollWidth > e.clientWidth)")
    check("phone 390: lineup tables scroll inside their own box", wide == [True, True], wide)
    ctx.close()

    # timezone: the game date must not shift for a viewer far from the US
    ctx = new_ctx(browser, tz="Pacific/Auckland")
    pg, log = open_page(ctx, base, fixed="2026-11-19T09:00:00")
    pg.wait_for_selector("h1")
    check("timezone Auckland: game date text unchanged", texts(pg, "main p.hint")[0].startswith("Wednesday, October 28, 2026 · Updated Nov 19, 2026"), texts(pg, "main p.hint"))
    check("timezone Auckland: countdown uses local date (125 days)", texts(pg, "#banner .count-n") == ["125"], texts(pg, "#banner .count-n"))
    ctx.close()

    # countdown edge cases
    for fixed, exp_n, exp_txt, hidden in (("2027-03-23T12:00:00", "1", "day until Opening Day 2027 · Wednesday, March 24", False),
                                          ("2027-03-24T12:00:00", None, "Opening Day 2027 is today · Wednesday, March 24", False),
                                          ("2027-03-25T12:00:00", None, None, True)):
        ctx = new_ctx(browser)
        pg, _ = open_page(ctx, base, fixed=fixed)
        pg.wait_for_selector("h1")
        vis = pg.is_visible("#banner")
        t = texts(pg, "#banner .count-t")
        ok = (vis != hidden) and (hidden or (t == [exp_txt] and (exp_n is None or texts(pg, "#banner .count-n") == [exp_n])))
        check(f"countdown at {fixed[:10]}", ok, (vis, t, texts(pg, "#banner .count-n")))
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
    g2 = mk(1002, "Athletics", "Seattle Mariners", "live", park="T-Mobile Park", roof="dome", weather=None,
            start_time_utc="2026-04-14T20:10:00Z", away_score=2, home_score=3, inning=5, inning_half="bottom", outs=2, results=None)
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


def test_season(browser):
    root = os.path.join(tempfile.mkdtemp(prefix="dm_season_"), "site")
    shutil.copytree(SITE, root)
    os.remove(os.path.join(root, "data", "site.json"))  # in season: no countdown file
    os.chmod(os.path.join(root, "data"), 0o755)
    pj = os.path.join(root, "data", "page.json")
    os.chmod(pj, 0o644)
    json.dump(season_data(), open(pj, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    srv = start_server(root, PORT_SEASON, spa=True)  # spa=True mimics Pages answering index.html for a missing site.json
    try:
        base = f"http://127.0.0.1:{PORT_SEASON}/"
        d = season_data()
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, base, fixed="2026-04-14T10:00:00")
        pg.wait_for_selector("h1")
        check("season: no console errors even though site.json falls back to index.html", not log["errors"] and not [c for c in log["console"] if c[0] == "error" and "CSP" in c[1]], log)
        check("season: banner hidden when site.json is missing", not pg.is_visible("#banner"))
        check("season: no replay notice", pg.locator(".notice").count() == 0)
        check("season: headline is the long date", texts(pg, "p.sub") == ["Tuesday, April 14, 2026"], texts(pg, "p.sub"))
        check("season: three game buttons, first pressed", pg.locator(".game-btn").count() == 3 and pg.get_attribute(".game-btn >> nth=0", "aria-pressed") == "true")
        r1 = texts(pg, ".game-btn .r1")
        check("season: list statuses (scheduled shows local time, live, final)", r1 == ["NYY @ BOS 7:10 PM EDT", "ATH @ SEA Live", "NYY @ BOS Final 6–5"], r1)

        # BTS: scheduled and live games have no result; a pick with game_id finds the right game; unknown name shows nothing
        res = texts(pg, ".bts li")
        check("season BTS: result only where a final box score has the player (scheduled game_id pick and unknown player show none)", [("Got a hit" in x, "No hit" in x) for x in res] == [(False, False), (True, False), (False, True), (True, False), (False, False)], res)
        check("season BTS: tiny probability shows <1%", texts(pg, ".bts .pv")[4] == "<1%" and texts(pg, ".bts .pv")[0] == ">99%", texts(pg, ".bts .pv"))

        # scheduled game with outdoor weather
        check("season game 1: sub line", texts(pg, ".gc-sub") == ["Fenway Park · First pitch 7:10 PM EDT"], texts(pg, ".gc-sub"))
        check("season game 1: no scoreboard, no actual-result lines", pg.locator(".score").count() == 0 and pg.locator(".tile .act").count() == 0)
        wx = [norm(x) for x in pg.locator(".wx > div").all_text_contents()]
        check("season game 1: six weather tiles", wx == ["Temperature61°F", "Humidity58%", "Pressure30.11 inHg", "Wind12 mph from SW", "Toward center fieldOut to CF (7 mph)", "Carry change+6 ft"], wx)
        check("season game 1: HR box and hot bar", norm(texts(pg, ".hrbox")[0]) == "Home run conditions: HR Friendly 63 out of 100" and pg.locator(".fill.hot").count() == 1)
        check("season game 1: lineup table has no Result column", pg.locator("table.t").first.locator("thead th").count() == 7)

        # live game, missing weather, missing starter, missing first-inning record
        pg.click(".game-btn >> nth=1")
        check("season game 2: selection moves", pg.get_attribute(".game-btn >> nth=1", "aria-pressed") == "true" and pg.get_attribute(".game-btn >> nth=0", "aria-pressed") == "false")
        check("season game 2: live scoreboard", norm(texts(pg, ".score")[0]) == "Live · Bottom 5th, 2 outs ATH 2 · SEA 3", texts(pg, ".score"))
        check("season game 2: forecast missing message", norm(texts(pg, ".cond-note")[0]) == "The forecast for this park was not available when the prediction ran.", texts(pg, ".cond-note"))
        pits = texts(pg, ".pit")
        check("season game 2: starter not announced + first-inning fallback", "Not announced" in pits[0] and "First-inning record not shown (fewer than 5 starts)." in pits[1], pits)

        # final with a run in the first and over lines that were exceeded
        pg.click(".game-btn >> nth=2")
        acts = texts(pg, ".tile .act")
        check("season game 3: actual lines", acts == ["Won", "Lost", "Actual: 11 runs", "A run scored in the 1st"], acts)
        check("season game 3: over chips checked for lines below 11 runs", pg.locator(".over .chip svg").count() == 5, pg.locator(".over .chip svg").count())
        check("season game 3: led after five", "Actual after five: BOS ahead." in norm(texts(pg, ".f5")[0]), texts(pg, ".f5"))
        check("season: no console errors after clicking around", not log["errors"], log["errors"])
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
        check("empty slate: message shown, no crash", "No games are scheduled for this date." in " ".join(texts(pg, "#app")) and "Picks appear once lineups are posted." in " ".join(texts(pg, "#app")), texts(pg, "#app"))
        check("empty slate: no errors", not log["errors"], log["errors"])
        ctx.close()
        # broken page.json (the server answers index.html, like Pages does for a missing file)
        open(pj, "w", encoding="utf-8").write("{ not json")
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, base)
        pg.wait_for_selector("#app [role=alert]")
        check("broken page.json: friendly error with role=alert", "did not load" in texts(pg, "#app [role=alert]")[0], texts(pg, "#app"))
        ctx.close()
        # no games key
        open(pj, "w", encoding="utf-8").write('{"date":"2026-04-14"}')
        ctx = new_ctx(browser)
        pg, log = open_page(ctx, base)
        pg.wait_for_selector("#app [role=alert]")
        check("page.json without games: friendly error", "did not load" in texts(pg, "#app [role=alert]")[0])
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
