# diamondmetrics-site

The site for `app.diamondmetrics.dev`: MLB game predictions from a Monte Carlo simulation of each game (odds to win, first five innings, totals, starter strikeouts, lineups, player props, home run conditions, and a Beat the Streak top 5), with a live line score from the MLB Stats API.

No build step. The `public/` folder is served as it is; one small Worker script answers `/api/*`.

```
public/
  index.html, styles.css, app.js   the page
  _headers                         security headers and cache rules for the static files
  favicon.svg                      the brand mark: a white diamond with a yellow second base on board green
  data/page.json                   the predictions the page shows
  data/site.json                   Opening Day countdown text and the replay title and headline
  fonts/                           Big Shoulders (display) and Libre Franklin (text), variable latin subsets
                                   from @fontsource-variable 5.3.0, SIL Open Font License, licences included
worker/index.js                    the Worker: GET /api/linescore/{gamePk}
wrangler.jsonc                     Worker and static-assets config
package.json, package-lock.json    wrangler, pinned (a dev tool only; nothing from npm is served)
tests/
  test_site.py, serve.py           browser tests (Playwright)
  worker.test.mjs                  Worker tests (node --test)
  fixtures/                        line-score fixtures, and one raw upstream response
  screens/                         screenshots the tests save (not committed)
```

## Data

`public/data/page.json` holds the predictions. Right now it is the replay of World Series Game 5 from `analysis/out/site/ws5_page.json` in the `mlb-market-engine` repo, copied byte for byte. Its SHA-256 is `e19e9611202f8ed1d62d796491a403cd1ac2eb0cb5b4767bbd6872a22ca4b458`. Do not edit it by hand: it is the committed output of a frozen run, and the page label says so.

In season the engine writes this file on a schedule and the page shows whatever is in it. The page reads these fields and nothing else:

- top level: `date`, `updated_at`, `note` (`"replay"` shows the replay title and notice), `label`, `games`, `beat_the_streak`
- each game: `game_id` (the MLB `gamePk`, used for the line score), `away_team`, `home_team`, `start_time_utc`, `park`, `roof`, `status` (`scheduled`, `live` or `final`), `away_score`, `home_score`, `inning`, `inning_half`, `outs`, `markets`, `weather` (may be `null`), `starters` (each side may be `null`), `lineups`, `results` (final games only)
- `weather`: `temp_f`, `humidity_pct`, `wind_mph`, `wind_from`, `wind_label`, `carry_ft`, `hr_score`, `hr_label`, and optionally `dew_point_f` and `elevation_ft`
- each starter's `first_inning`: `scoreless`, `not_scoreless`, `era`, `opp_obp`, and optionally `whip`
- `results.batters`: per side, `id`, `h`, `tb`, `hr`, `rbi`, `sb` and the stat `line` (for example `1-4, 2B, RBI`)
- each Beat the Streak pick: `name`, `team`, `hit`, and optionally `game_id` (use it on doubleheaders so the result is read from the right game)

`public/data/site.json` is optional:

- `opening_day` (YYYY-MM-DD) turns the countdown on until that date passes. `opening_label` and `opening_note` are its text. After the labor deal is settled, update all three.
- `replay_title` is the page title while `note` is `"replay"` (without it the title is "MLB predictions"). In season the title is the long date of `date`.
- `replay_headline` is the line under the replay title.

## The line score (Worker)

The line score at the top of the board comes from the MLB Stats API, through a same-origin endpoint so the page's Content-Security-Policy can stay `connect-src 'self'`.

`GET /api/linescore/{gamePk}` (also `HEAD`) is answered by `worker/index.js`. It fetches `https://statsapi.mlb.com/api/v1/schedule?sportId=1&hydrate=linescore&gamePk={gamePk}` and returns only this:

```
{"state":"final","detail":"Final","inning":9,"half":"bottom","outs":3,
 "innings":[{"n":1,"away":0,"home":0}, ...],
 "away":{"r":4,"h":8,"e":2},"home":{"r":1,"h":4,"e":0}}
```

- `state` is `preview`, `live` or `final` (from `abstractGameState`); `detail` is `detailedState` (for example `In Progress`, `Postponed`, `Suspended: Rain`); `half` is `top`, `bottom`, `middle` or `end`. A half-inning not batted, or a missing total, is `null`.
- `gamePk` must be 1 to 8 digits (400 otherwise). A game the API doesn't know is 404. An upstream error, bad JSON or no answer within 5 seconds is 502. Other methods are 405. Other `/api/*` paths are 404.
- Caching uses the Cache API. The edge keeps a final game 24 hours, a live game 15 seconds and a preview 5 minutes. The browser is told `max-age=86400`, `10` and `300`, also when the answer comes from the edge cache. Errors are `no-store` and not cached.
- Worker responses don't get `_headers`, so the Worker sets its own: `Content-Type: application/json; charset=utf-8`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'`, `Referrer-Policy: strict-origin-when-cross-origin`.

The page fetches the selected game's line score when its board renders. While the API says the game is live it fetches again every 30 seconds; from 15 minutes before `start_time_utc` a preview game is fetched every 5 minutes, so it goes live without a reload. Nothing is fetched while the tab is hidden (it refreshes as soon as the tab is visible again), and polling stops when the game is final, when `detail` starts with Postponed, Cancelled or Suspended, or when another game is picked. The out-of-town board uses page.json only.

If the line score can't be fetched (an error, or no answer within 10 seconds), the board shows empty plates and dashes, keeps page.json's top line, and shows no error. The rest of the page doesn't depend on it.

## Preview on your computer

```
npm ci
npx wrangler dev
```

Then open http://localhost:8787. This serves `public/` and runs the Worker for `/api/*`, as in production. (`python -m http.server 8000 --directory public` still works for the page alone, without the line score.)

## Deploy (Cloudflare Workers, static assets)

The site runs on Cloudflare Workers with static assets, not Pages. The Worker `diamondmetrics-site` is connected to this repo's `main` branch; each push to `main` deploys. `wrangler.jsonc` sets the Worker script, the `public/` assets with `run_worker_first: ["/api/*"]` (static files never run the script), and keeps `workers_dev` and `preview_urls` off; the custom domains are set in the dashboard, not in the config. The build installs the pinned wrangler from `package-lock.json` (Workers Builds installs dependencies automatically; don't set the `SKIP_DEPENDENCY_INSTALL` build variable) and deploys with `npx wrangler deploy`. Static files get their headers from `public/_headers`.

## Rules the page follows

- The strong and watch cut-offs are the ones from the original Streamlit app (listed under "Cut-off for every market", under Beat the Streak). A value is judged by the whole percent it shows, so a value of 17.54% that displays as 18% counts as 18%. The original compared the unrounded value and displayed one decimal.
- One meaning for yellow: yellow numerals on a green plate mean strong. On the board, strong numbers are yellow, worth-watching numbers white, and low numbers dim green (each plate carries the class `b-strong`, `b-watch` or `b-low`; plates with no cut-off, such as totals, are `b-plain`). In the tables, strong values are a yellow-on-green chip (`p-strong`), worth-watching values are dark ink (`p-watch`), and low values are grey (`p-low`). On the line score, a yellow outline marks the half-inning being played.
- The board starts with the line score: a plate per inning (at least nine, more for extra innings), then R, H and E behind a seam. Innings not played are empty plates; before the game R, H and E are dashes. In a final where the home team didn't bat in the last inning, that plate shows X. A postponed, cancelled or suspended game shows its status in the top line and its plates as the API gives them, with no X.
- Under a rule, the model numbers: Odds to win, First 5 winner and Total runs O/U, then the NRFI lamp row.
- Total runs O/U is laid out the way a sportsbook lists a total: the away row shows the over and the home row the under, for the same line, with "Over 6.5" / "Under 6.5" as the caption under each plate. The line is the `.5` line in `markets.total_over` whose unrounded over chance is closest to 50%; on a tie, the higher line. The over shows its whole percent and the under shows 100 minus that, so the pair always adds to 100.
- The lamp row has one item, NRFI (no run in the 1st). On a final game a lamp sits in front of it: lit means no run scored in the 1st, unlit means one did, and no lamp means the result is not in page.json. A lit lamp also says "It happened." to screen readers.
- A check mark marks what happened once a game is final: after each total-runs line the game went over, under each strikeout total the starter reached, and after each batter prop that came true (1+ hit, 2+ total bases, 1+ homer, 1+ RBI, 1+ steal, read from `results.batters` by player id). Every check also has words for screen readers.
- Home run conditions: the score bar's label is the tier (for example "Neutral (Dome)") and the score out of 100. An open-air park also shows temperature, humidity, dew point, wind (with `wind_label` under it), elevation and carry. Dew point is `dew_point_f` when present, otherwise Magnus from temperature and humidity with the engine's constants (6.1078, 7.5, 237.3), rounded to a whole °F. Elevation is `elevation_ft` when present, otherwise the home park's `elevation_m` from `STADIUM_INFO` in the engine's `weather.py`, converted to feet and rounded to the nearest 10. A park not in that table (right now the Athletics, code `ATH`) shows no elevation. When the engine's table changes, copy the new values into `ELEV_M` in `app.js`.
- Each starter's first-inning record shows as stat blocks: NRFI record (scoreless-not scoreless), ERA, OBP, and WHIP when the data has it. With no first-inning data, a sentence says so.
- Beat the Streak shows each pick's team nickname and, once his game is final, his stat line for the night.
- The line score and the model numbers are real tables with captions and row and column headers. On phones (560px and below) nine innings plus R, H and E fit at 390px; extra innings scroll inside the line score's own focusable box. Each model team becomes one block, and each plate gets its label from the cell's `data-label` through CSS.
- With more than one game, the out-of-town board lists them as buttons; the pressed one is shown on the main board and everything below it.
- A probability that rounds to 0 shows as `<1%` and one that rounds to 100 shows as `>99%`.
- The game date comes from `date` in the file and is not shifted by the viewer's time zone. First pitch and "Updated" are shown in the viewer's time zone.
- Text is added to the page as text, never as HTML. `_headers` sets a strict Content-Security-Policy: no inline scripts, no inline styles, and no request to any other host. Do not add any of those, or the page will break. Bar widths are set through the CSSOM (`el.style.width`), which the policy allows.

## Tests

```
python -m venv .venv
.venv\Scripts\python -m pip install playwright
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python tests/test_site.py
node --test tests/worker.test.mjs
```

The browser tests serve `public/` with the rules from `_headers` (there is no Worker in that server, so `/api/linescore/*` is answered from `tests/fixtures/` with Playwright's `page.route`). They load the real `page.json` at desktop and phone width and compare every number on the page with the file and the fixtures (the expected values, including the O/U line, dew point, elevation and the line score, are worked out separately in the test). They also run an in-season file made up for the test (preview, live and final games, an 11-inning final, a home team that didn't bat in the 9th, postponed and suspended games, a 500 and a timeout, open-air and dome parks, missing weather and starters), check polling with Playwright's clock, and check the error messages. They save full-page screenshots to `tests/screens/`.

`tests/fixtures/linescore-849799.json` is the Worker's real answer for WS5, captured through `npx wrangler dev`; its R totals (4 and 1) match page.json's results. `tests/fixtures/upstream-849799.json` is the raw MLB Stats API response the Worker tests trim.

The Worker tests stub `fetch` and the Cache API, so they make no network requests.

Not affiliated with Major League Baseball.
