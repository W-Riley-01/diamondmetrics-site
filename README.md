# Diamond Metrics

The site for `diamondmetrics.dev` (and `app.diamondmetrics.dev`): MLB game predictions from a Monte Carlo simulation of each game (odds to win, first five innings, totals, starter strikeouts, lineups, player props, home run conditions, and Beat the Streak picks), with live scores from the MLB Stats API.

No build step and nothing from npm is served. The `public/` folder is served as it is; one small Worker script answers `/api/*` and serves the game pages.

## Pages and URLs

| URL | Page | Served by |
|---|---|---|
| `/` | Home: today's games as cards, and the top three Beat the Streak picks | `public/index.html` (assets) |
| `/game/{gamePk}` | One game: the board, home run conditions, total runs, starting pitchers and lineups | the Worker, which serves `public/game.html` from the assets for a 1 to 8 digit id |
| `/beat-the-streak` | Every Beat the Streak pick | `public/beat-the-streak.html` (assets) |
| anything else | "Page not found." with a link to all games, status 404 | `public/404.html` (assets `not_found_handling: "404-page"`) |

- `html_handling` is the default, `auto-trailing-slash`: `/beat-the-streak` serves `beat-the-streak.html`, and `/beat-the-streak.html` and `/beat-the-streak/` redirect (307) to it. `/index.html` redirects to `/`.
- `run_worker_first` is `["/api/*", "/game", "/game/*"]`. The Worker answers `/game/{1-8 digits}` with `game.html` (fetched from the assets as `/game`) and every other `/game` path (`/game`, `/game/`, `/game/abc`, `/game/123456789`, `/game/1/x`) with the 404 page and status 404. `/game` is in the list so the bare page shell is never served on its own; `/game.html` redirects to `/game`, which is the 404 page. Other methods than GET and HEAD are 405.
- A gamePk that is a number but isn't in `page.json` gets the game page, which says "This game isn't on today's slate." with a link to all games.
- Every asset path is absolute (`/styles.css`, `/app.js`, `/fonts/…`, `/data/page.json`), since game pages are one level down.
- Every page gets the same security headers from `public/_headers`, including the game pages and the 404 page the Worker returns (it returns the assets' own response, which carries the `_headers` rules; `tests/test_routes.py` checks this with `wrangler dev`).

```
public/
  index.html, game.html,           the four pages (each a small shell; app.js draws the content, picked by
  beat-the-streak.html, 404.html   <body data-page="home|game|bts">; the 404 page has no script)
  app.js, styles.css               one script and one stylesheet for every page
  _headers                         security headers and cache rules for the static files
  favicon.svg                      the brand mark: a white diamond with a yellow second base on board green
  data/page.json                   the predictions the pages show
  data/site.json                   Opening Day countdown text and the replay title and headline
  fonts/                           Big Shoulders (display) and Libre Franklin (text), variable latin subsets
                                   from @fontsource-variable 5.3.0, SIL Open Font License, licences included
worker/index.js                    the Worker: the /api/* endpoints and the /game routes
wrangler.jsonc                     Worker and static-assets config
package.json, package-lock.json    wrangler, pinned (a dev tool only; nothing from npm is served)
tests/
  test_site.py, serve.py           browser tests (Playwright) and the test server that mimics the routing
  test_routes.py                   routing tests against the real wrangler dev
  worker.test.mjs                  Worker tests (node --test)
  fixtures/                        API fixtures, and raw upstream responses
  screens/                         screenshots the tests save (not committed)
```

## Data

`public/data/page.json` holds the predictions. Right now it is the replay of World Series Game 5 from `analysis/out/site/ws5_page.json` in the `mlb-market-engine` repo, copied byte for byte. Its SHA-256 is `e19e9611202f8ed1d62d796491a403cd1ac2eb0cb5b4767bbd6872a22ca4b458`. Do not edit it by hand: it is the committed output of a frozen run, and the page label says so.

In season the engine writes this file on a schedule and the pages show whatever is in it. They read these fields and nothing else:

- top level: `date`, `updated_at`, `note` (`"replay"` shows the replay title and notice), `label`, `games`, `beat_the_streak`
- each game: `game_id` (the MLB `gamePk`: the game page's URL and the key for the live data), `away_team`, `home_team`, `start_time_utc`, `park`, `roof`, `status` (`scheduled`, `live` or `final`), `away_score`, `home_score`, `inning`, `inning_half`, `outs`, `markets`, `weather` (may be `null`), `starters` (each side may be `null`), `lineups`, `results` (final games only)
- `markets`: `away_win`, `home_win`, `f5_away`, `f5_home`, `f5_tie`, `total_over` (line -> chance of going over), `no_run_first`
- `weather`: `temp_f`, `humidity_pct`, `wind_mph`, `wind_from`, `wind_label`, `carry_ft`, `hr_score`, `hr_label`, and optionally `dew_point_f` and `elevation_ft`
- each starter: `name`, `k_over` (n -> chance of n or more strikeouts), `first_inning` (`scoreless`, `not_scoreless`, `era`, `opp_obp`, and optionally `whip`)
- `results`: `away_score`, `home_score`, `total_runs`, `led_after_5` (`away`, `home` or `tie`), `run_in_first`, `starter_k`, and `batters`: per side, `id`, `h`, `tb`, `hr`, `rbi`, `sb` and the stat `line` (for example `1-4, 2B, RBI`). A `null` means the result isn't known.
- each Beat the Streak pick, in ranked order: `name`, `team`, `hit`, and optionally `game_id` (use it on doubleheaders; without it the pick goes to his team's game, or on a doubleheader the final game he has a box-score row in)

`public/data/site.json` is optional:

- `opening_day` (YYYY-MM-DD) turns the home page's countdown on until that date passes. `opening_label` and `opening_note` are its text. After the labor deal is settled, update all three.
- `replay_title` is the title while `note` is `"replay"` (without it the title is "MLB predictions"). `replay_headline` is the line under it.

## Live data (Worker)

Live data comes from the MLB Stats API through same-origin endpoints, so the pages' Content-Security-Policy can stay `connect-src 'self'`. `worker/index.js` answers six endpoints, `GET` and `HEAD` only. Each returns only the trimmed JSON below; nothing from upstream is passed through as it is.

**`/api/slate/{date}`**: every game on a date, from `schedule?sportId=1&date={date}&hydrate=linescore`. `{date}` must be a real `YYYY-MM-DD` date (`2026-02-30` is a 400).

```
{"date":"2026-04-14",
 "games":{"824853":{"state":"final","detail":"Final","inning":9,"half":"bottom","outs":3,
                    "away":{"runs":4,"wins":10,"losses":8},"home":{"runs":2,"wins":9,"losses":9}}, ...}}
```

- `state` is `preview`, `live` or `final` (from `abstractGameState`), `detail` is `detailedState`. A rainout is `"Final"` upstream (abstractGameState `Final`, codedGameState `D`, detailedState `Postponed`; 823543, Rays at Yankees on 2026-05-23, rescheduled to 2026-09-22), so everything that reads the slate checks `detail` first: Postponed, Cancelled or Suspended is a stopped game, whatever `state` says.
- `runs` comes from the line score (null before first pitch). `wins` and `losses` are `teams.{side}.leagueRecord`, for regular-season games (`gameType` `R`) only and `null` otherwise: in the postseason `leagueRecord` is the series record (4–1 on WS5). For a postponed game listed on its original date, the API's record is the one for the rescheduled game.
- A gamePk listed twice on one date (a suspended game resumed the same day) keeps the entry that isn't stopped.
- A date with no games is `{"date": …, "games": {}}`.

**`/api/linescore/{gamePk}`**: the game's state and line score, from `schedule?sportId=1&hydrate=linescore&gamePk={gamePk}`.

```
{"state":"final","detail":"Final","inning":9,"half":"bottom","outs":3,
 "innings":[{"n":1,"away":0,"home":0}, ...],
 "away":{"r":4,"h":8,"e":2},"home":{"r":1,"h":4,"e":0}}
```

`half` is `top`, `bottom`, `middle` or `end`. A half-inning not batted, or a missing total, is `null`.

**`/api/boxscore/{gamePk}`**: each team's starting pitcher with his line, and its starting catcher, from `game/{gamePk}/boxscore` (plus the schedule call for the game's state, because the boxscore has none and answers 200 even for unknown games).

```
{"state":"final",
 "away":{"pitcher":{"id":694819,"name":"Jacob Misiorowski","line":{"ip":"5.2","h":2,"bb":1,"so":2}},
         "catcher":{"id":661388,"name":"William Contreras"}},
 "home":{...}}
```

The starting pitcher is the first id in the team's `pitchers` list; the line is `inningsPitched`, `hits`, `baseOnBalls`, `strikeOuts`. The starting catcher is the starter in the batting order (code `n00`; substitutes have codes like `701`) whose first position was C. Either is `null` until known.

**`/api/series/{gamePk}`**: the regular-season series between the two teams and, for a postseason game, the postseason series. From `schedule?sportId=1&hydrate=seriesStatus&gamePk={gamePk}`, then `schedule?sportId=1&season={season}&gameType=R&teamId={away}&opponentId={home}`.

```
{"season":2026,
 "regular":{"awayWins":2,"homeWins":1,"played":3,"left":0,
            "sweeps":[{"winner":"away","games":3,"startDate":"2026-07-07","endDate":"2026-07-09"}],
            "seasonSweep":null},
 "postseason":{"description":"World Series","result":{"leader":"away","wins":4,"losses":1,"over":true}}}
```

- Counting is by start time (`gameDate`, UTC), so doubleheaders come out right. `played`, the wins, `sweeps` and `seasonSweep` use only final games (with a winner) that started no later than this game, this game included once it is final. `left` is the rest of the season's meetings: each `gamePk` once, cancelled games left out.
- A sweep is a series (grouped by `seriesGameNumber`) of 2 or more games, all played, all won by one team. `seasonSweep` is the side that has won every game played, or `null`.
- `postseason` is `null` for a regular-season game. `result.leader` is `null` while the series is tied.

**`/api/standings/{gamePk}`**: each team's record and division standing going into the game. From the game (`schedule?…&hydrate=probablePitcher&gamePk=`) and `standings?leagueId=103,104&season={season}&standingsTypes=regularSeason&hydrate=division`, with `date=` the day before the game's date for a regular-season game and no date for a postseason game (the final regular-season table).

```
{"season":2026,"asOf":"final",
 "away":{"wins":103,"losses":59,"division":"NL Central","place":1,"tied":false,"gamesBack":0,"lead":14,"divisionRecord":{"w":35,"l":17}},
 "home":{...}}
```

`place` comes from games back, not `divisionRank` (which never ties). `lead` is set only for a sole division leader. A team not in the standings is `null`.

**`/api/battery/{gamePk}`**: each team's battery, their record together this season, and stolen bases against each. The pitcher is the boxscore's first pitcher once the game has started, the probable pitcher before; the catcher follows the `/api/boxscore` rule. Then each player's game log (`people/{id}/stats?stats=gameLog&group=pitching|fielding&season={season}&gameType=R,F,D,L,W`) and one call for all four players' season stats (`people?personIds=…&hydrate=stats(group=[pitching,fielding],type=[season],season={season})`).

```
{"season":2026,
 "away":{"pitcher":{"id":694819,"name":"Jacob Misiorowski","sb":2,"cs":1,"pickoffs":0,"teams":1},
         "catcher":{"id":661388,"name":"William Contreras","sb":48,"cs":25,"teams":1},
         "together":{"w":20,"l":7}},
 "home":{...}}
```

- A start together is the same `gamePk` where the pitcher's split is a start, the catcher's split is a start at C, and both splits are for the same team. The date is only used for "before this game": an earlier date, or the same date with a lower game number. Wins and losses are the team's result (`isWin`).
- Stolen bases are this regular season's: for a player who played for more than one team, the season total (`teams` says how many). The catcher's come from his C split only.
- No catcher yet: `catcher` and `together` are `null`.

**Rules for all six**

- `gamePk` must be 1 to 8 digits, the date a real `YYYY-MM-DD` (400 otherwise). A game the API doesn't know is 404. An upstream error, bad JSON or no answer within 5 seconds (each upstream call) is 502. Other methods are 405. Other `/api/*` paths are 404.
- Caching uses the Cache API:

  | Endpoint | Edge keeps | Browser `max-age` |
  |---|---|---|
  | `/api/slate` | 15 s while any game is live; 24 h once every game is over (final, postponed, cancelled or suspended); otherwise (and a date with no games) 5 min | 10; 86400; 300 |
  | `/api/linescore`, `/api/boxscore` | final 24 h, live 15 s, preview 5 min | 86400, 10, 300 |
  | `/api/series` | 1 h; 24 h once the teams have no meetings left | 3600; 86400 |
  | `/api/standings` | 1 h for a regular-season game; 24 h for a postseason game | 3600; 86400 |
  | `/api/battery` | 1 h; 24 h once the game is final | 3600; 86400 |

  The browser gets its own `max-age` again when the answer comes from the edge cache (stored with the copy as `X-Browser-Max-Age`). Errors are `no-store` and not cached.
- The API's JSON responses set their own headers: `Content-Type: application/json; charset=utf-8`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'`, `Referrer-Policy: strict-origin-when-cross-origin`.

**How the pages use them**

- Home: `/api/slate/{date of page.json}` on load, then every 30 seconds while any game in `page.json` is live or within 15 minutes of first pitch, on one timer. With nothing that close, it wakes when the next game's 15-minute window opens; with every game over, it stops. Nothing is fetched while the tab is hidden (it refreshes as soon as the tab is visible again). Only games in `page.json` are updated; other games on the slate are ignored. A failed fetch leaves the cards as `page.json` describes them.
- Game page: the game's five endpoints when the page opens (so the catcher shows even hours before first pitch). The series and the standings once; the battery again only when the boxscore's starters or catchers change. One timer refreshes the line score and the boxscore together: every 30 seconds while the API says the game is live, every 5 minutes from 15 minutes before `start_time_utc`, nothing while the tab is hidden, and it stops when the game is final or its `detail` starts with Postponed, Cancelled or Suspended. The slate is fetched once (no polling) for the Other games list.
- If an endpoint can't be fetched (an error, or no answer within 10 seconds), its part stays as `page.json` has it and nothing else changes. None of them grade the model: the result lamps and icons use `page.json` only, and `page.json` stays the source for the starters' names and every prediction.

## Preview on your computer

```
npm ci
npx wrangler dev
```

Then open http://localhost:8787. This serves `public/` and runs the Worker for `/api/*` and `/game/*`, as in production.

## Deploy (Cloudflare Workers, static assets)

The site runs on Cloudflare Workers with static assets, not Pages. The Worker `diamondmetrics-site` is connected to this repo's `main` branch; each push to `main` deploys. `wrangler.jsonc` sets the Worker script, the `public/` assets with `run_worker_first: ["/api/*", "/game", "/game/*"]` and `not_found_handling: "404-page"`, and keeps `workers_dev` and `preview_urls` off; the custom domains are set in the dashboard, not in the config. The build installs the pinned wrangler from `package-lock.json` and deploys with `npx wrangler deploy`.

## The pages

**Every page**: the nav has the brand "Diamond Metrics" (a link to `/`), "Games" (`/`) and "Beat the Streak" (`/beat-the-streak`), with `aria-current="page"` on the page you're on ("Games" on a game page, neither on the 404 page). Titles: "Diamond Metrics: MLB predictions" (home), "Brewers at Rays, October 28: Diamond Metrics" (a game; the static `og:title` of `game.html` is "MLB game: Diamond Metrics", since link previews don't run the script), "Beat the Streak: Diamond Metrics", "Page not found: Diamond Metrics".

**Dates**: the game date comes from `date` in the file and is not shifted by the viewer's time zone; headings use the long date without the year ("Tuesday, April 14"). First pitch and "Updated" are in the viewer's time zone. "Updated 10:00 AM CDT." when the update happened on the slate's date; otherwise it keeps its date: "Updated Nov 18, 12:13 PM EST." (the replay ran weeks after its game).

**Home (`/`)**

- Heading: in season the long date with "15 games. Updated 10:00 AM CDT." under it; on the replay "World Series Game 5", the headline, the replay label and the updated line. The Opening Day countdown sits above it while `site.json` turns it on.
- Under a "Today's games" band ("Games" on the replay), the games grouped under "In progress", "Later today", "Final" and "Postponed" (postponed, cancelled or suspended), in that order, each sorted by first pitch. Empty groups don't show. "No games are scheduled for this date." when there are none.
- One card per game, at least 360px wide (two across on a wide desktop, otherwise one); the whole card is a single link to `/game/{gamePk}`:
  - top line: the status on the left (a lit white lamp and "Top 6th, 1 out" while live, the local first-pitch time before the game, "Final", or the stopped status) and the park on the right;
  - two team rows: the nickname, the team's W–L record from the slate beside it (regular season only), the win chance as a mini plate in its tier colour, and the runs once the game has started (the losing score dimmed once final);
  - a footer: the board's total runs line ("Total O/U 8.5") and its NRFI label ("NRFI 52%", or "YRFI 55%" as on the board); on a final game the four result lamps instead, the same picks and colours as the board but labelled only Winner, First 5, Total and NRFI (the full pick, such as "Winner Brewers", stays as screen-reader text), so the lamps and "Full game" fit one row; a postponed game shows no lamps; and "Full game" at the right;
  - an accessible name that reads the card in one sentence: "Brewers at Rays, final, 4 to 1. Full game."
- A card changes group when the slate says its game has moved on; keyboard focus stays on it.
- Side rail, under a "Beat the Streak" band: "The best chances of a hit today." ("The best chances of a hit." on the replay), the top three picks (rank, name, "Brewers at Rays" linking to that game, the hit chance) and an "All picks" link.

**Game (`/game/{gamePk}`)**

- Heading: "Brewers at Rays" with the long date and the updated time under it in season; on the replay "World Series Game 5", the headline, the label and the updated line. "All games" (back to `/`) sits above the board.
- The board, home run conditions, total runs, starting pitchers and lineups, with the live refresh above. The rail has "Other games" (each other game on the slate as one line, teams and score or first-pitch time, linking to its page; the status comes from the slate, fetched once; without it each line shows what `page.json` has) and then "Cut-off for every market". With no other games the list is left out.

**Beat the Streak (`/beat-the-streak`)**

- "Beat the Streak", the date (or the replay title) under it, and "Beat the Streak is MLB's free contest: pick a batter to get a hit on the days you play, and 57 correct picks in a row win the grand prize."
- Every pick in `page.json`, ranked: name, team, "at Rays" or "vs Brewers" linking to that game, the hit chance, and once his game is final the batter's line, with the bat icon when he got a hit. "Picks appear once lineups are posted." when there are none.

**404**: "Page not found." and a link to all games.

## Rules the pages follow

- The strong and watch cut-offs are the ones from the original Streamlit app (listed under "Cut-off for every market"). A value is judged by the whole percent it shows, so 17.54% that displays as 18% counts as 18%.
- Yellow means only "strong": yellow numerals on a green plate. On the board and the cards, strong numbers are yellow, worth-watching numbers white, and low numbers dim green (`b-strong`, `b-watch`, `b-low`; plates with no cut-off are `b-plain`). In the tables, strong values are a yellow-on-green chip (`p-strong`), worth-watching values dark ink (`p-watch`), low values dark brown (`p-low`). Selection markers are white: the half-inning being played, the current page in the nav, and the live lamp. Yellow appears in exactly four other places: the home-run bar at 55 or more, keyboard focus rings on green areas, the brand mark's second base, and the two foul poles. Yellow text never sits on the dirt. A test checks every element against that list.
- The infield: the page sides are grass (`--board`); the content column (`.infield`, max 1280px, from the nav to the footer) is infield dirt (`--dirt` #D9C3A0) with a 4px yellow foul pole down each edge. The nav, the board, the cards and the footer stay green. Section headings on the dirt are green bands with chalk text and a 3px chalk bottom line; row rules are `--rule`; bars run in chalked lanes. Text on the dirt uses `--ink`, `--board`, `--muted` and `--low`, all at least 4.5:1, which a test checks on every page.
- The board's top line is the park and the date (or first pitch, or a stopped game's status), with a white lamp and "Live, bottom 5th, 2 outs" while the game is on. Then the line score (a plate per inning, at least nine, then R, H and E; X where the home team didn't bat in the last inning of a final), the season series strip, and the model numbers: Odds to win, First 5 winner and Total runs O/U (the away row shows the over and the home row the under for the `.5` line closest to 50%, the pair adding to 100), then the result lamps.
- Result lamps show whether the model's pick came true. The pick is the likelier side on unrounded values: Winner, First 5 (away, home or tie), Total (Over or Under on the board's line), NRFI (or YRFI with the other side of the number). Exactly even is no pick and no lamp. Lamps are unlit before and during the game; on a final game green when the pick came true and red when it missed, with words for screen readers; no lamp where the result is `null`. A postponed, cancelled or suspended game (by its detailed state, before anything else) has no lamps at all.
- Each team on the board: the record beside the nickname, the standing on two lines ("1st in the NL Central, 14 games up." then "35–17 in the division."; the second left out when the division record is missing), then the battery: "SP" from `page.json`, "C" from `/api/boxscore` ("C not announced" until the lineup is in), and their record together.
- Starting pitchers: each team's starter, his line once the game has started (with "Started: [name]" when the API's starter isn't `page.json`'s), the stolen bases against the pitcher and the catcher this season, the 3+ to 7+ strikeout ladder with a bold K in `--ok` under each total reached once the game is final ("Reached 4 strikeouts." for screen readers), and the first-inning record.
- Once a game is final, the lineup tables mark each batter prop that came true with a 14px icon in `--ok`: a bat for 1+ hit ("got a hit") and 2+ total bases ("two or more bases"), a firework burst for a homer ("homered"), home plate for an RBI, a base for a steal. The burst pops once (about 500 ms, scaling up from 60% as it fades in) when the lineups are drawn; the live refresh never redraws them, and there's no animation under `prefers-reduced-motion: reduce`. Check marks follow each total-runs line the game went over.
- Home run conditions: the score bar's label and the score out of 100; an open-air park also shows temperature, humidity, dew point (the file's, or Magnus with the engine's constants), wind, elevation (the file's, or the park table from the engine's `weather.py`, rounded to 10 ft) and carry.
- A probability that rounds to 0 shows as `<1%` and one that rounds to 100 as `>99%`.
- Tables have captions and headers. On phones nine innings plus R, H and E fit at 390px; extra innings and wide lineups scroll inside their own focusable box. Scroll boxes only scroll sideways, and only when their table can't fit.
- Text is added to the page as text, never as HTML. `_headers` sets a strict Content-Security-Policy: no inline scripts, no inline styles, and no request to any other host. Bar widths are set through the CSSOM (`el.style.width`), which the policy allows.

## Tests

```
python -m venv .venv
.venv\Scripts\python -m pip install playwright
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python tests/test_site.py
.venv\Scripts\python tests/test_routes.py
node --test tests/worker.test.mjs
```

- `test_site.py`: browser tests. `tests/serve.py` serves `public/` with the rules from `_headers` and the site's routing (`/game/{id}` -> `game.html`, other `/game` paths and unknown paths -> `404.html` with 404); the API is answered from `tests/fixtures/` with Playwright's `page.route`. Every page is checked for the name, the nav and `aria-current`, no "How the model works", the title, the security headers, same-origin requests, stray "null"/"undefined" text, contrast on the dirt, and the 360 to 1440px width sweep with classic scrollbars (as on Windows). The real `page.json` is checked at desktop and phone width against values worked out separately in the test; an in-season file made up for the test covers preview, live and final games, an 11-inning final, postponed and suspended games, even picks, YRFI, unknown results, failures and timeouts, and polling with Playwright's clock (the home page's slate refresh and the game page's line score). The rainout cards use the real 2026-05-23 slate. Screenshots of every page at 1440 and 390, the 404 page, and a 1x close-up of a lineup row with every icon go to `tests/screens/`.
- `test_routes.py`: starts the pinned `wrangler dev` and checks every page URL, the 307 redirects, the 404 page for unknown and bad `/game` paths, the methods on `/game`, the assets at absolute paths, and that every HTML response (including the Worker's) carries every security header in `_headers`. It makes no request to the MLB Stats API.
- `worker.test.mjs`: stubs `fetch`, the Cache API and the `ASSETS` binding, so it makes no network requests. `tests/fixtures/upstream-slate-*.json` are real schedule responses for 2026-04-14, 2026-05-23 (two rainouts) and 2026-10-28 (World Series Game 5); `slate-2026-05-23.json` and `slate-2026-10-28.json` are the Worker's own trims of them, used by the browser tests.

The test servers are stopped as a whole process tree (on Windows the venv's `python.exe`, and wrangler, start child processes), and a run refuses to start if a test port is already in use.

Not affiliated with Major League Baseball.
