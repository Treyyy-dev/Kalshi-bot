# Kalshi Sports Intel Bot

Scans every open Kalshi **sports** market and shows an estimated win % next to Kalshi's price, using:
- Kalshi price, bid/ask, volume, open interest
- News headlines (Google News, last 2 days)
- Reddit posts (last week, weighted by upvotes)
- X/Twitter posts (optional, needs paid API token)
- ESPN team records -> head-to-head odds

## Run it
Needs Python 3.9+. No installs.

```
python kalshi_sports_bot.py                 # scan everything
python kalshi_sports_bot.py --sport nba     # one league
python kalshi_sports_bot.py --watch 300     # rescan every 5 min
python kalshi_sports_bot.py --min-edge 5    # only gaps of 5+ points
```
Open `report.html` in your browser (phone-friendly). `report.json` has the raw data.

## Optional
- `X_BEARER_TOKEN=...` adds X/Twitter sentiment
- `DISCORD_WEBHOOK_URL=...` sends alerts for big gaps (1 hr cooldown per market)
- `BOT_DEBUG=1` prints network warnings

## How the estimate works
Kalshi price is the anchor (80%), team-record odds 20% for 2-team games, then news/social
sentiment nudges it up to ±6 points. "Gap" = estimate minus the YES ask. Read-only; never trades.

## Render
Web service · runtime Python · build `pip install -r requirements.txt` ·
start `python kalshi_sports_bot.py --serve --watch 900` · open the service URL for the live report (`/api` = JSON).

**Note:** ESPN and Reddit block requests from cloud servers like Render, so on Render the bot uses
Kalshi + Google News + Bluesky. Run it on your own phone/computer to also get ESPN records and Reddit.
