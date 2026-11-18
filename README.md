# diamondmetrics-site

The static site for `app.diamondmetrics.dev`: MLB game predictions from a Monte Carlo simulation of each game (win chances, first five innings, totals, starter strikeouts, lineups, player props, first-pitch weather, and a Beat the Streak top 5).

No build step. Cloudflare Pages serves the `public/` folder as it is.

```
public/
  index.html, styles.css, app.js   the page
  _headers                         security headers and cache rules (Cloudflare Pages reads this file)
  favicon.svg
  data/page.json                   the predictions the page shows
  data/site.json                   Opening Day countdown text and the replay headline
  fonts/                           Inter and Space Grotesk (SIL Open Font License, licences included)
tests/
  test_site.py, serve.py           browser tests (Playwright)
```

## Data

`public/data/page.json` is the only file the page needs to show predictions. Right now it is the replay of World Series Game 5 from `analysis/out/site/ws5_page.json` in the `mlb-market-engine` repo, copied byte for byte. Its SHA-256 is `e19e9611202f8ed1d62d796491a403cd1ac2eb0cb5b4767bbd6872a22ca4b458`. Do not edit it by hand: it is the committed output of a frozen run, and the page label says so.

In season the engine writes this file on a schedule and the page shows whatever is in it. The page reads these fields and nothing else:

- top level: `date`, `updated_at`, `note` (`"replay"` shows the replay notice), `label`, `games`, `beat_the_streak`
- each game: `game_id`, `away_team`, `home_team`, `start_time_utc`, `park`, `roof`, `status` (`scheduled`, `live` or `final`), `away_score`, `home_score`, `inning`, `inning_half`, `outs`, `markets`, `weather` (may be `null`), `starters` (each side may be `null`), `lineups`, `results` (final games only)
- each Beat the Streak pick: `name`, `team`, `opponent`, `home`, `hit`, and optionally `game_id` (use it on doubleheaders so the result is read from the right game)

`public/data/site.json` is optional. `opening_day` (YYYY-MM-DD) turns the countdown banner on until that date passes. `opening_label` and `opening_note` are its text. `replay_headline` is the line under the title while `note` is `"replay"`. After the labor deal is settled, update `opening_day`, `opening_label` and `opening_note`.

## Preview on your computer

The page loads its data with `fetch`, which browsers block for files opened from disk, so use a local server:

```
python -m http.server 8000 --directory public
```

Then open http://localhost:8000.

## Deploy (Cloudflare Pages)

Framework preset: None. Build command: leave empty. Build output directory: `public`. Production branch: `main`.

## Rules the page follows

- The strong and watch cut-offs are the ones from the original Streamlit app (listed in the "How to read the numbers" panel). A cell is judged by the whole percent it shows, so a value of 17.54% that displays as 18% counts as 18%. The original compared the unrounded value and displayed one decimal.
- A probability that rounds to 0 shows as `<1%` and one that rounds to 100 shows as `>99%`.
- The game date comes from `date` in the file and is not shifted by the viewer's time zone. First pitch and "Updated" are shown in the viewer's time zone.
- Text is added to the page as text, never as HTML. `_headers` sets a strict Content-Security-Policy: no inline scripts, no inline styles, and no request to any other host. Do not add any of those, or the page will break.

## Tests

```
pip install playwright
playwright install chromium
python tests/test_site.py
```

The tests serve `public/` with the rules from `_headers`, load the real `page.json` at desktop and phone width, and compare every number on the page with the file (the expected values are worked out separately in the test). They also run an in-season file made up for the test (scheduled, live and final games, missing weather, missing starters) and check the error messages.

Not affiliated with Major League Baseball.
