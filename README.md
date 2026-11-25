# diamondmetrics-site

The static site for `app.diamondmetrics.dev`: MLB game predictions from a Monte Carlo simulation of each game (odds to win, first five innings, totals, starter strikeouts, lineups, player props, home run conditions, and a Beat the Streak top 5).

No build step. The `public/` folder is served as it is.

```
public/
  index.html, styles.css, app.js   the page
  _headers                         security headers and cache rules
  favicon.svg                      the brand mark: a white diamond with a yellow second base on board green
  data/page.json                   the predictions the page shows
  data/site.json                   Opening Day countdown text and the replay title and headline
  fonts/                           Big Shoulders (display) and Libre Franklin (text), variable latin subsets
                                   from @fontsource-variable 5.3.0, SIL Open Font License, licences included
tests/
  test_site.py, serve.py           browser tests (Playwright)
  screens/                         screenshots the tests save (not committed)
```

## Data

`public/data/page.json` is the only file the page needs to show predictions. Right now it is the replay of World Series Game 5 from `analysis/out/site/ws5_page.json` in the `mlb-market-engine` repo, copied byte for byte. Its SHA-256 is `e19e9611202f8ed1d62d796491a403cd1ac2eb0cb5b4767bbd6872a22ca4b458`. Do not edit it by hand: it is the committed output of a frozen run, and the page label says so.

In season the engine writes this file on a schedule and the page shows whatever is in it. The page reads these fields and nothing else:

- top level: `date`, `updated_at`, `note` (`"replay"` shows the replay title and notice), `label`, `games`, `beat_the_streak`
- each game: `game_id`, `away_team`, `home_team`, `start_time_utc`, `park`, `roof`, `status` (`scheduled`, `live` or `final`), `away_score`, `home_score`, `inning`, `inning_half`, `outs`, `markets`, `weather` (may be `null`), `starters` (each side may be `null`), `lineups`, `results` (final games only)
- `weather`: `temp_f`, `humidity_pct`, `wind_mph`, `wind_from`, `wind_label`, `carry_ft`, `hr_score`, `hr_label`, and optionally `dew_point_f` and `elevation_ft`
- each starter's `first_inning`: `scoreless`, `not_scoreless`, `era`, `opp_obp`, and optionally `whip`
- `results.batters`: per side, `id`, `h`, `tb`, `hr`, `rbi`, `sb` and the stat `line` (for example `1-4, 2B, RBI`)
- each Beat the Streak pick: `name`, `team`, `hit`, and optionally `game_id` (use it on doubleheaders so the result is read from the right game)

`public/data/site.json` is optional:

- `opening_day` (YYYY-MM-DD) turns the countdown on until that date passes. `opening_label` and `opening_note` are its text. After the labor deal is settled, update all three.
- `replay_title` is the page title while `note` is `"replay"` (without it the title is "MLB predictions"). In season the title is the long date of `date`.
- `replay_headline` is the line under the replay title.

## Preview on your computer

The page loads its data with `fetch`, which browsers block for files opened from disk, so use a local server:

```
python -m http.server 8000 --directory public
```

Then open http://localhost:8000.

## Deploy (Cloudflare Workers, static assets)

The site runs on Cloudflare Workers with static assets, not Pages. The Worker `diamondmetrics-site` is connected to this repo's `main` branch, with output directory `public` and no build command. Each push to `main` deploys. The Worker serves `_headers` from `public/`.

## Rules the page follows

- The strong and watch cut-offs are the ones from the original Streamlit app (listed under "Cut-off for every market", under Beat the Streak). A value is judged by the whole percent it shows, so a value of 17.54% that displays as 18% counts as 18%. The original compared the unrounded value and displayed one decimal.
- One meaning for yellow: yellow numerals on a green plate mean strong. On the board, strong numbers are yellow, worth-watching numbers white, and low numbers dim green (each plate carries the class `b-strong`, `b-watch` or `b-low`; plates with no cut-off, such as totals and scores, are `b-plain`). In the tables, strong values are a yellow-on-green chip (`p-strong`), worth-watching values are dark ink (`p-watch`), and low values are grey (`p-low`).
- The board columns are Odds to win, First 5 winner and Total runs O/U, then Final or Runs once a game has started. "led after 5" under a team's First 5 winner plate marks the team that led after five innings.
- Total runs O/U is laid out the way a sportsbook lists a total: the away row shows the over and the home row the under, for the same line. The line is the `.5` line in `markets.total_over` whose unrounded over chance is closest to 50%; on a tie, the higher line. The over shows its whole percent and the under shows 100 minus that, so the pair always adds to 100.
- The lamp row under the board has one item, NRFI (no run in the 1st). On a final game a lamp sits in front of it: lit means no run scored in the 1st, unlit means one did, and no lamp means the result is not in the file. A lit lamp also says "It happened." to screen readers.
- A check mark marks what happened once a game is final: after each total-runs line the game went over, under each strikeout total the starter reached, and after each batter prop that came true (1+ hit, 2+ total bases, 1+ homer, 1+ RBI, 1+ steal, read from `results.batters` by player id). Every check also has words for screen readers.
- Home run conditions: a dome shows "Dome" and the score bar. An open-air park shows temperature, humidity, dew point, wind (with `wind_label` under it), elevation and carry, then the score bar. Dew point is `dew_point_f` when present, otherwise Magnus from temperature and humidity with the engine's constants (6.1078, 7.5, 237.3), rounded to a whole °F. Elevation is `elevation_ft` when present, otherwise the home park's `elevation_m` from `STADIUM_INFO` in the engine's `weather.py`, converted to feet and rounded to the nearest 10. A park not in that table (right now the Athletics, code `ATH`) shows no elevation. When the engine's table changes, copy the new values into `ELEV_M` in `app.js`.
- Each starter's first-inning record shows as stat blocks: NRFI record (scoreless-not scoreless), ERA, OBP, and WHIP when the data has it. With no first-inning data, a sentence says so.
- Beat the Streak shows each pick's team nickname and, once his game is final, his stat line for the night.
- The board is a real table with a caption and row and column headers. On phones (560px and below) each team becomes one block, and each plate gets its label from the cell's `data-label` through CSS.
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
```

The tests serve `public/` with the rules from `_headers`, load the real `page.json` at desktop and phone width, and compare every number on the page with the file (the expected values, including the O/U line, dew point and elevation, are worked out separately in the test). They also run an in-season file made up for the test (scheduled, live and final games, open-air and dome parks, missing weather, missing starters, an O/U tie, a tie after five, unknown results) and check the error messages. They save full-page screenshots to `tests/screens/`.

Not affiliated with Major League Baseball.
