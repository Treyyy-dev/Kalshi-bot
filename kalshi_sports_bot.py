#!/usr/bin/env python3
"""
Kalshi Sports Intel Bot
=======================
Scans every OPEN sports market on Kalshi, then gathers outside info for each one:
  * Kalshi market data   (price, bid/ask, volume, open interest)
  * News headlines       (Google News RSS, last ~2 days)
  * Social media         (Reddit search; X/Twitter if you add a bearer token)
  * Team records         (ESPN public scoreboard -> log5 head-to-head odds)

It blends those into an estimated win % and shows it next to Kalshi's implied %,
so you can see where the outside info agrees or disagrees with the market.

No installs needed -- Python 3.9+ standard library only.
This bot only READS data. It never places trades.

Usage:
    python kalshi_sports_bot.py                    # one scan, prints table + writes report.html
    python kalshi_sports_bot.py --sport nfl        # only NFL (nfl, nba, mlb, nhl, ncaaf, ncaab, wnba, soccer, ufc, golf, tennis...)
    python kalshi_sports_bot.py --watch 300        # rescan every 5 minutes
    python kalshi_sports_bot.py --min-edge 5       # only show/alert gaps of 5+ points

Optional environment variables:
    X_BEARER_TOKEN       X/Twitter API v2 bearer token (paid tier) for tweet sentiment
    DISCORD_WEBHOOK_URL  post alerts when a gap >= --min-edge shows up
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = "Mozilla/5.0 (KalshiSportsIntelBot/1.0)"

# Kalshi series-ticker fragment -> (ESPN sport, ESPN league, friendly name)
LEAGUES = {
    "NFL": ("football", "nfl", "nfl"),
    "NCAAF": ("football", "college-football", "ncaaf"),
    "NBA": ("basketball", "nba", "nba"),
    "WNBA": ("basketball", "wnba", "wnba"),
    "NCAAMB": ("basketball", "mens-college-basketball", "ncaab"),
    "NCAAB": ("basketball", "mens-college-basketball", "ncaab"),
    "NCAAWB": ("basketball", "womens-college-basketball", "ncaawb"),
    "MLB": ("baseball", "mlb", "mlb"),
    "NHL": ("hockey", "nhl", "nhl"),
    "EPL": ("soccer", "eng.1", "soccer"),
    "LALIGA": ("soccer", "esp.1", "soccer"),
    "SERIEA": ("soccer", "ita.1", "soccer"),
    "BUNDESLIGA": ("soccer", "ger.1", "soccer"),
    "LIGUE1": ("soccer", "fra.1", "soccer"),
    "UCL": ("soccer", "uefa.champions", "soccer"),
    "MLS": ("soccer", "usa.1", "soccer"),
}
EXTRA_SPORT_WORDS = ["ufc", "mma", "golf", "pga", "tennis", "atp", "wta", "f1", "nascar",
                     "boxing", "cricket", "soccer"]

# ---------------------------------------------------------------- sentiment
POS = {
    "win": 1, "wins": 1, "won": 1, "beat": 1, "beats": 1, "dominant": 2, "dominate": 2,
    "streak": 1, "hot": 1, "healthy": 2, "returns": 2, "return": 1, "back": 1, "cleared": 2,
    "upgraded": 2, "favorite": 1, "favored": 1, "surge": 1, "rolling": 1, "unbeaten": 2,
    "undefeated": 2, "clinch": 2, "clinched": 2, "boost": 2, "elite": 1, "best": 1,
    "momentum": 1, "comeback": 1, "activated": 2, "star": 1, "lock": 1, "confident": 1,
}
NEG = {
    "loss": 1, "loses": 1, "lost": 1, "injury": 2, "injured": 2, "out": 1, "ruled": 1,
    "questionable": 1, "doubtful": 2, "suspended": 3, "suspension": 3, "torn": 3, "acl": 3,
    "concussion": 2, "sidelined": 2, "struggle": 1, "struggling": 1, "slump": 2, "skid": 2,
    "fired": 2, "benched": 2, "ill": 1, "illness": 1, "sprain": 2, "fracture": 3, "surgery": 3,
    "underdog": 1, "collapse": 2, "turmoil": 2, "drama": 1, "trade": 1, "ir": 2,
    "downgraded": 2, "miss": 2, "misses": 2, "missing": 1, "limited": 1, "scratched": 2,
}
NEGATORS = {"not", "no", "never", "without", "isn't", "won't", "didn't"}


def sentiment(text):
    """Returns a score in [-1, 1] for one headline/post using a sports-specific word list."""
    words = re.findall(r"[a-z']+", text.lower())
    score, hits = 0.0, 0
    for i, w in enumerate(words):
        v = POS.get(w, 0) - NEG.get(w, 0)
        if v:
            if i > 0 and words[i - 1] in NEGATORS:
                v = -v
            score += v
            hits += 1
    if not hits:
        return 0.0
    return max(-1.0, min(1.0, score / (hits * 2)))


# ---------------------------------------------------------------- http helpers
def get(url, headers=None, timeout=15, as_json=True):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(data) if as_json else data


def safe(fn, *a, default=None, **k):
    try:
        return fn(*a, **k)
    except Exception as e:  # network hiccups shouldn't kill the scan
        if os.environ.get("BOT_DEBUG"):
            print(f"  [warn] {fn.__name__}: {e}", file=sys.stderr)
        return default


# ---------------------------------------------------------------- Kalshi
def price_cents(m, key):
    """Kalshi returns prices either as cents ints (yes_bid) or dollar strings (yes_bid_dollars)."""
    d = m.get(f"{key}_dollars")
    if d not in (None, ""):
        try:
            return float(d) * 100
        except ValueError:
            pass
    v = m.get(key)
    return float(v) if v not in (None, "") else None


def market_prob(m):
    bid, ask, last = price_cents(m, "yes_bid"), price_cents(m, "yes_ask"), price_cents(m, "last_price")
    if bid and ask and 0 < bid <= ask < 100:
        return (bid + ask) / 2
    return last if last else (ask or bid)


GAME_SERIES_FALLBACK = [
    "KXNFLGAME", "KXNCAAFGAME", "KXNBAGAME", "KXWNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME",
    "KXMLBGAME", "KXNHLGAME", "KXEPLGAME", "KXUCLGAME", "KXLALIGAGAME", "KXSERIEAGAME",
    "KXBUNDESLIGAGAME", "KXLIGUE1GAME", "KXMLSGAME", "KXUFCFIGHT", "KXATPMATCH", "KXWTAMATCH",
]
GAME_HINTS = ("GAME", "FIGHT", "MATCH", "BOUT")


def events_for(params, pages=10):
    out, cursor = [], ""
    for _ in range(pages):
        q = {"status": "open", "with_nested_markets": "true", "limit": 200, **params}
        if cursor:
            q["cursor"] = cursor
        data = get(f"{KALSHI}/events?{urllib.parse.urlencode(q)}")
        out.extend(data.get("events", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def liquid(m):
    bid, ask = price_cents(m, "yes_bid"), price_cents(m, "yes_ask")
    if bid and ask and 0 < bid <= ask < 100:
        return ask - bid <= 25
    return bool(price_cents(m, "last_price")) and num(m.get("volume") or m.get("volume_fp")) > 0


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def ev_volume(ev):
    return sum(num(m.get("volume") or m.get("volume_fp")) for m in ev.get("markets", []))


def fetch_sports_events(limit_events):
    """Games first (every open game/fight/match series), then the most-traded sports futures."""
    series = safe(get, f"{KALSHI}/series?category=Sports", default={}) or {}
    game_series = {s.get("ticker", "") for s in series.get("series", []) or []
                   if any(h in (s.get("ticker") or "").upper() for h in GAME_HINTS)}
    game_series |= set(GAME_SERIES_FALLBACK)

    games, seen = [], set()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for evs in pool.map(lambda t: safe(events_for, {"series_ticker": t}, 3, default=[]), sorted(game_series)):
            for ev in evs or []:
                ev["_game"] = True
                ev.setdefault("category", "Sports")
                if ev.get("event_ticker") not in seen:
                    seen.add(ev.get("event_ticker"))
                    games.append(ev)
    futures = [e for e in safe(events_for, {}, 40, default=[]) or []
               if (e.get("category") or "").lower() == "sports" and e.get("event_ticker") not in seen]

    for ev in games + futures:
        ev["markets"] = [m for m in ev.get("markets", []) if liquid(m)]
    games = [e for e in games if e["markets"]]
    futures = sorted([e for e in futures if e["markets"]], key=ev_volume, reverse=True)
    major = ["NFL", "NBA", "NCAAF", "NHL", "MLB", "NCAAMB", "NCAAB", "WNBA", "EPL", "UCL", "MLS"]
    games.sort(key=lambda e: (major.index(league_of(e)) if league_of(e) in major else len(major),
                              e.get("markets")[0].get("expected_expiration_time")
                              or e.get("markets")[0].get("close_time") or ""))
    counts = {}
    for e in games:
        counts[league_of(e)] = counts.get(league_of(e), 0) + 1
    print(f"  games by league: {counts}")
    n_fut = max(10, limit_events // 4)
    print(f"  found {len(games)} live games/fights and {len(futures)} liquid futures")
    return games[: limit_events] + futures[:n_fut]


def league_of(ev):
    st = (ev.get("series_ticker") or ev.get("event_ticker") or "").upper()
    for frag in sorted(LEAGUES, key=len, reverse=True):
        if frag in st:
            return frag
    title = (ev.get("title") or "").lower()
    for w in EXTRA_SPORT_WORDS:
        if re.search(rf"\b{w}\b", title) or st.startswith(f"KX{w.upper()}"):
            return w.upper()
    return "OTHER"


def sport_matches(ev, sport):
    if not sport:
        return True
    sport = sport.lower()
    lg = league_of(ev)
    friendly = LEAGUES.get(lg, (None, None, lg.lower()))[2]
    return sport in (lg.lower(), friendly) or sport in (ev.get("title") or "").lower()


# ---------------------------------------------------------------- outside info
def google_news(query, max_items=15):
    url = ("https://news.google.com/rss/search?" +
           urllib.parse.urlencode({"q": f"{query} when:2d", "hl": "en-US", "gl": "US", "ceid": "US:en"}))
    root = ET.fromstring(get(url, as_json=False))
    out = []
    for item in root.iter("item"):
        out.append({"src": "news", "title": item.findtext("title", ""),
                    "link": item.findtext("link", ""), "date": item.findtext("pubDate", "")})
        if len(out) >= max_items:
            break
    return out


def reddit(query, max_items=20):
    url = "https://www.reddit.com/search.json?" + urllib.parse.urlencode(
        {"q": query, "sort": "new", "t": "week", "limit": max_items})
    data = get(url)
    out = []
    for c in data.get("data", {}).get("children", []):
        d = c.get("data", {})
        out.append({"src": "reddit", "title": f"{d.get('title', '')} {d.get('selftext', '')[:200]}",
                    "link": "https://reddit.com" + d.get("permalink", ""),
                    "date": datetime.fromtimestamp(d.get("created_utc", 0), timezone.utc).isoformat(),
                    "weight": 1 + min(3, (d.get("score", 0) or 0) / 200)})
    return out


def bluesky(query, max_items=25):
    url = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?" + urllib.parse.urlencode(
        {"q": query, "limit": max_items, "sort": "latest"})
    data = get(url)
    return [{"src": "bluesky", "title": p.get("record", {}).get("text", ""),
             "date": p.get("record", {}).get("createdAt", ""),
             "link": "https://bsky.app/profile/%s/post/%s" % (p.get("author", {}).get("handle", ""),
                                                             p.get("uri", "").rsplit("/", 1)[-1]),
             "weight": 1 + min(3, (p.get("likeCount", 0) or 0) / 100)} for p in data.get("posts", [])]


def x_search(query, max_items=25):
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        return []
    url = "https://api.x.com/2/tweets/search/recent?" + urllib.parse.urlencode(
        {"query": f"({query}) lang:en -is:retweet", "max_results": max(10, min(100, max_items)),
         "tweet.fields": "public_metrics,created_at"})
    data = get(url, headers={"Authorization": f"Bearer {token}"})
    out = []
    for t in data.get("data", []):
        likes = t.get("public_metrics", {}).get("like_count", 0)
        out.append({"src": "x", "title": t.get("text", ""), "date": t.get("created_at", ""),
                    "link": f"https://x.com/i/status/{t.get('id')}", "weight": 1 + min(3, likes / 500)})
    return out


_espn_cache = {}


def _add_team(teams, t, pct, rec):
    names = {t.get(k, "").lower() for k in
             ("displayName", "shortDisplayName", "name", "location", "abbreviation", "nickname") if t.get(k)}
    for n in names:
        teams.setdefault(n, {"pct": pct, "record": rec, "name": t.get("displayName", "")})


def _walk_standings(node, teams):
    for e in (node.get("standings") or {}).get("entries", []) or []:
        st = {x.get("name"): x.get("value") for x in e.get("stats", [])}
        w, l, t = num(st.get("wins")), num(st.get("losses")), num(st.get("ties")) + num(st.get("otLosses"))
        if w + l + t:
            rec = f"{int(w)}-{int(l)}" + (f"-{int(t)}" if t else "")
            _add_team(teams, e.get("team", {}), (w + 0.5 * t) / (w + l + t), rec)
    for c in node.get("children", []) or []:
        _walk_standings(c, teams)


def espn_teams(league_key):
    """team-name -> season win pct, from ESPN standings (all teams) + scoreboard (fills gaps)."""
    if league_key in _espn_cache:
        return _espn_cache[league_key]
    teams = {}
    if league_key in LEAGUES:
        sport, lg, _ = LEAGUES[league_key]
        st = safe(get, f"https://site.api.espn.com/apis/v2/sports/{sport}/{lg}/standings", default={}) or {}
        _walk_standings(st, teams)
        extra = "?groups=50&limit=400" if "college" in lg else ""
        data = safe(get, f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/scoreboard{extra}",
                    default={}) or {}
        for e in data.get("events", []):
            for comp in e.get("competitions", []):
                for c in comp.get("competitors", []):
                    rec = (c.get("records") or [{}])[0].get("summary", "")
                    _add_team(teams, c.get("team", {}), record_pct(rec), rec)
    _espn_cache[league_key] = teams
    if league_key in LEAGUES:
        print(f"    [espn] {league_key}: {len(teams)} team names loaded, e.g. {list(teams)[:4]}")
    return teams


def record_pct(rec):
    nums = [int(x) for x in re.findall(r"\d+", rec or "")]
    if len(nums) < 2 or sum(nums) == 0:
        return None
    w, l = nums[0], nums[1]
    ties = nums[2] if len(nums) > 2 else 0
    return (w + 0.5 * ties) / (w + l + ties)


def find_team(name, teams):
    n = (name or "").lower().strip()
    if not n:
        return None
    if n in teams:
        return teams[n]
    for k, v in teams.items():
        if len(k) > 3 and (k in n or n in k):
            return v
    return None


def log5(a, b):
    a, b = min(max(a, .02), .98), min(max(b, .02), .98)
    return (a - a * b) / (a + b - 2 * a * b)


# ---------------------------------------------------------------- the model
def mentions(text, name):
    text = text.lower()
    toks = [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(t) > 2]
    return bool(toks) and any(t in text for t in toks)


def analyze_event(ev):
    title = ev.get("title", "")
    markets = [m for m in ev.get("markets", []) if (m.get("status") in (None, "active", "open"))]
    query = re.sub(r"\b(will|win|the|game|match|winner|\?)\b", " ", title, flags=re.I)
    query = re.sub(r"\s+", " ", query).strip() or title

    posts = (safe(google_news, query, default=[]) + safe(reddit, query, default=[]) + safe(bluesky, query, default=[]) +
             safe(x_search, query, default=[]))
    lg = league_of(ev)
    teams = espn_teams(lg)
    if ev.get("_game") and lg in LEAGUES and not teams:
        print(f"    [espn] no teams loaded for {lg}")

    # Head-to-head games usually have 2 markets (one per team) -> log5 from records works
    team_info = {m.get("ticker"): find_team(m.get("yes_sub_title") or m.get("subtitle"), teams) for m in markets}
    if ev.get("_game") and teams and not any(team_info.values()) and lg in ("NBA", "NFL", "NHL"):
        print(f"    [espn-miss] {lg} {[m.get('yes_sub_title') for m in markets]} n={len(markets)}")

    rows = []
    for m in markets:
        side = m.get("yes_sub_title") or m.get("subtitle") or m.get("title", "")
        mp = market_prob(m)
        if mp is None:
            continue
        mp /= 100

        # sentiment for this side minus the opponent's sentiment
        own = [p for p in posts if mentions(p["title"], side)]
        own_s = wavg([(sentiment(p["title"]), p.get("weight", 1)) for p in own])
        if not own and len(markets) != 2:
            own, own_s = posts, 0.0  # prop/futures line: show the buzz, don't guess a direction
        others = [p for p in posts if not mentions(p["title"], side)]
        opp_s = wavg([(sentiment(p["title"]), p.get("weight", 1)) for p in others]) if len(markets) == 2 else 0
        net_sent = own_s - opp_s

        # record-based probability (2-way games only)
        rec_p, rec_txt = None, ""
        ti = team_info.get(m.get("ticker"))
        if ti and ti["pct"] is not None and len(markets) == 2:
            opp = next((team_info[x.get("ticker")] for x in markets if x.get("ticker") != m.get("ticker")), None)
            if opp and opp["pct"] is not None:
                rec_p = log5(ti["pct"], opp["pct"])
                rec_txt = f"{ti['record']} vs {opp['record']}"

        # blend: market is the anchor (it's usually the sharpest number), outside info nudges it
        est = mp
        if rec_p is not None:
            est = 0.80 * mp + 0.20 * rec_p
        info_weight = min(1.0, len(own) / 6)          # more coverage = trust sentiment more
        est += 0.06 * net_sent * info_weight          # max +/-6 pts from news/social
        est = min(max(est, 0.01), 0.99)

        ask = price_cents(m, "yes_ask")
        edge = est * 100 - (ask if ask else mp * 100)
        conf = "high" if (rec_p is not None and len(own) >= 6) else "med" if (rec_p is not None or len(own) >= 3) else "low"

        rows.append({
            "league": lg, "event": title, "is_game": bool(ev.get("_game")), "side": side, "ticker": m.get("ticker"),
            "kalshi_pct": round(mp * 100, 1), "est_pct": round(est * 100, 1),
            "edge_pts": round(edge, 1), "yes_ask": ask, "yes_bid": price_cents(m, "yes_bid"),
            "volume": num(m.get("volume") or m.get("volume_fp")),
            "open_interest": num(m.get("open_interest") or m.get("open_interest_fp")),
            "record_pct": round(rec_p * 100, 1) if rec_p is not None else None, "records": rec_txt,
            "sentiment": round(net_sent, 2), "mentions": len(own), "confidence": conf,
            "close": m.get("close_time") or m.get("expected_expiration_time") or "",
            "headlines": [{"src": p["src"], "title": p["title"][:160], "link": p.get("link", ""),
                           "s": round(sentiment(p["title"]), 2)} for p in own[:8]],
        })
    return rows


def wavg(pairs):
    tw = sum(w for _, w in pairs)
    return sum(s * w for s, w in pairs) / tw if tw else 0.0


# ---------------------------------------------------------------- output
def print_table(rows, min_edge):
    shown = [r for r in rows if abs(r["edge_pts"]) >= min_edge] if min_edge else rows
    print(f"\n{'LEAGUE':7} {'PICK':24} {'KALSHI':>7} {'EST':>6} {'GAP':>6} {'REC':>6} {'SENT':>5} {'#':>3} CONF  EVENT")
    print("-" * 110)
    for r in shown[:80]:
        rec = f"{r['record_pct']:.0f}%" if r["record_pct"] is not None else "-"
        print(f"{r['league'][:7]:7} {r['side'][:24]:24} {r['kalshi_pct']:6.1f}% {r['est_pct']:5.1f}% "
              f"{r['edge_pts']:+6.1f} {rec:>6} {r['sentiment']:+5.2f} {r['mentions']:3} {r['confidence']:5} {r['event'][:40]}")
    print(f"\n{len(shown)} of {len(rows)} markets shown. GAP = est% minus Kalshi YES ask (cents).")


def write_html(rows, path):
    data = json.dumps(rows).replace("</", "<\\/")
    stamp = datetime.now().strftime("%b %d %Y %I:%M %p")
    page = HTML_TEMPLATE.replace("__DATA__", data).replace("__STAMP__", html.escape(stamp))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def discord_alert(rows, min_edge, sent):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    for r in rows:
        key = (r["ticker"], round(r["edge_pts"]))
        if abs(r["edge_pts"]) < min_edge or r["ticker"] in sent and time.time() - sent[r["ticker"]] < 3600:
            continue
        msg = (f"**{r['league']} | {r['side']}** ({r['event']})\nKalshi {r['kalshi_pct']}% -> est "
               f"{r['est_pct']}%  (gap {r['edge_pts']:+} pts, conf {r['confidence']})\n"
               f"Records {r['records'] or 'n/a'} | sentiment {r['sentiment']:+} from {r['mentions']} posts")
        body = json.dumps({"content": msg}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
        safe(urllib.request.urlopen, req, timeout=10)
        sent[r["ticker"]] = time.time()


def scan(args):
    print(f"[{datetime.now():%H:%M:%S}] Pulling open Kalshi sports markets...")
    events = [e for e in fetch_sports_events(args.max_events) if sport_matches(e, args.sport)]
    print(f"  {len(events)} sports events. Gathering news / social / records (this takes a minute)...")
    rows = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for res in pool.map(lambda e: safe(analyze_event, e, default=[]), events):
            rows.extend(res or [])
    rows.sort(key=lambda r: (not r["is_game"], -abs(r["edge_pts"])))
    src = {}
    for r in rows:
        for h in r["headlines"]:
            src[h["src"]] = src.get(h["src"], 0) + 1
    g = [r for r in rows if r["is_game"]]
    print(f"  {len(rows)} markets analyzed ({len(g)} game lines, {sum(1 for r in g if r['records'])} with records); "
          f"posts matched by source: {src or 'none'}")
    shown = {}
    for r in g:
        if shown.get(r["league"], 0) >= 2 or len(shown) > 6:
            continue
        shown[r["league"]] = shown.get(r["league"], 0) + 1
        print(f"    {r['league']} {r['side']} | {r['event']} | kalshi {r['kalshi_pct']} est {r['est_pct']} "
              f"gap {r['edge_pts']:+} | rec {r['records'] or '-'} | posts {r['mentions']}")
    return rows


def main():
    ap = argparse.ArgumentParser(description="Kalshi sports intel bot (read-only)")
    ap.add_argument("--sport", help="nfl, nba, mlb, nhl, ncaaf, ncaab, wnba, soccer, ufc, golf, tennis...")
    ap.add_argument("--max-events", type=int, default=150, help="max games to analyze (plus top futures)")
    ap.add_argument("--min-edge", type=float, default=0, help="only show gaps of at least N points")
    ap.add_argument("--watch", type=int, default=0, help="rescan every N seconds")
    ap.add_argument("--out", default="report.html")
    ap.add_argument("--serve", action="store_true",
                    help="web mode (Render): rescan every --watch seconds and serve the report on $PORT")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    if args.serve:
        return serve(args)

    sent = {}
    while True:
        rows = scan(args)
        print_table(rows, args.min_edge)
        write_html(rows, args.out)
        with open(os.path.splitext(args.out)[0] + ".json", "w") as f:
            json.dump(rows, f, indent=1)
        print(f"Report saved -> {os.path.abspath(args.out)}")
        discord_alert(rows, args.min_edge or 5, sent)
        if not args.watch:
            break
        time.sleep(args.watch)


def serve(args):
    """Runs scans in a background thread and serves the latest report.html over HTTP."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    interval = args.watch or 600
    state = {"html": LOADING_PAGE, "json": "[]"}

    def loop():
        sent = {}
        while True:
            try:
                rows = scan(args)
                write_html(rows, args.out)
                with open(args.out, encoding="utf-8") as f:
                    state["html"] = f.read()
                state["json"] = json.dumps(rows)
                discord_alert(rows, args.min_edge or 5, sent)
            except Exception as e:
                print(f"scan failed: {e}", file=sys.stderr)
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api"):
                body, ctype = state["json"], "application/json"
            elif self.path.startswith("/robots.txt"):
                body, ctype = "User-agent: *\nAllow: /\n", "text/plain"
            elif self.path.startswith("/health"):
                body, ctype = "ok", "text/plain"
            else:
                body, ctype = state["html"], "text/html; charset=utf-8"
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    port = int(os.environ.get("PORT", 10000))
    print(f"Serving report on port {port}, rescanning every {interval}s")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


LOADING_PAGE = """<!DOCTYPE html><html><head><meta http-equiv="refresh" content="20">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="background:#0d1117;color:#e6edf3;font-family:system-ui;padding:40px;text-align:center">
<h2>Kalshi Sports Intel</h2><p>First scan running... this page refreshes on its own.</p></body></html>"""


HTML_TEMPLATE = r"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kalshi Sports Intel</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--txt:#e6edf3;--mut:#8b949e;--g:#3fb950;--r:#f85149;--b:#58a6ff}
*{box-sizing:border-box}body{margin:0;font:14px system-ui,sans-serif;background:var(--bg);color:var(--txt)}
header{padding:16px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:2}
h1{margin:0 0 4px;font-size:20px}.mut{color:var(--mut);font-size:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
input,select{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:8px}
main{padding:12px;display:grid;gap:10px;max-width:900px;margin:auto}
.c{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
.top{display:flex;justify-content:space-between;gap:8px}.side{font-weight:700;font-size:16px}
.lg{font-size:11px;background:#21262d;border-radius:6px;padding:2px 6px;color:var(--b)}
.nums{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:10px 0}
.n{background:#0d1117;border-radius:8px;padding:8px;text-align:center}.n b{display:block;font-size:18px}
.meter{height:8px;background:#21262d;border-radius:4px;position:relative;overflow:hidden}
.meter i{position:absolute;top:0;bottom:0;left:0;background:var(--b)}
.meter s{position:absolute;top:-2px;bottom:-2px;width:3px;background:#fff}
.pos{color:var(--g)}.neg{color:var(--r)}details{margin-top:8px}summary{cursor:pointer;color:var(--mut)}
.h{font-size:12px;padding:4px 0;border-top:1px solid var(--line)}.h a{color:var(--txt);text-decoration:none}
.tag{font-size:10px;padding:1px 5px;border-radius:4px;background:#21262d;margin-right:4px}
footer{padding:20px;text-align:center}
</style></head><body>
<header><h1>Kalshi Sports Intel</h1><div class="mut">Updated __STAMP__ · estimates blend Kalshi price, team records, news & social sentiment · not financial advice</div>
<div class="bar"><input id="q" placeholder="Search team / event"><select id="lg"><option value="">All leagues</option></select>
<select id="sort"><option value="gap">Biggest gap</option><option value="est">Highest est %</option><option value="vol">Most volume</option><option value="buzz">Most buzz</option></select></div></header>
<main id="list"></main>
<footer class="mut">White line = Kalshi implied %. Blue bar = bot estimate. Gap = estimate minus YES ask.</footer>
<script>
const D=__DATA__;const $=s=>document.querySelector(s);
[...new Set(D.map(r=>r.league))].sort().forEach(l=>$('#lg').innerHTML+=`<option>${l}</option>`);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function draw(){const q=$('#q').value.toLowerCase(),lg=$('#lg').value,so=$('#sort').value;
let r=D.filter(x=>(!lg||x.league==lg)&&(!q||(x.side+' '+x.event).toLowerCase().includes(q)));
const k={gap:x=>-Math.abs(x.edge_pts),est:x=>-x.est_pct,vol:x=>-x.volume,buzz:x=>-x.mentions}[so];r.sort((a,b)=>k(a)-k(b));
$('#list').innerHTML=r.slice(0,300).map(x=>`<div class="c"><div class="top"><div><div class="side">${esc(x.side)}</div>
<div class="mut">${esc(x.event)}</div></div><span class="lg">${esc(x.league)}</span></div>
<div class="nums"><div class="n"><span class="mut">Kalshi</span><b>${x.kalshi_pct}%</b></div>
<div class="n"><span class="mut">Bot est.</span><b>${x.est_pct}%</b></div>
<div class="n"><span class="mut">Gap</span><b class="${x.edge_pts>=0?'pos':'neg'}">${x.edge_pts>0?'+':''}${x.edge_pts}</b></div></div>
<div class="meter"><i style="width:${x.est_pct}%"></i><s style="left:${x.kalshi_pct}%"></s></div>
<div class="mut" style="margin-top:8px">Records: ${x.records?esc(x.records)+' ('+x.record_pct+'%)':'n/a'} · Sentiment <span class="${x.sentiment>=0?'pos':'neg'}">${x.sentiment}</span> from ${x.mentions} posts · Conf ${x.confidence} · Vol ${x.volume}</div>
${x.headlines.length?`<details><summary>What people are saying (${x.headlines.length})</summary>${x.headlines.map(h=>`<div class="h"><span class="tag">${h.src}</span><a href="${esc(h.link)}" target="_blank" rel="noopener">${esc(h.title)}</a> <span class="${h.s>=0?'pos':'neg'}">${h.s}</span></div>`).join('')}</details>`:''}
</div>`).join('')||'<p class="mut">No markets match.</p>';}
['#q','#lg','#sort'].forEach(s=>$(s).addEventListener('input',draw));draw();
</script></body></html>"""

if __name__ == "__main__":
    main()
