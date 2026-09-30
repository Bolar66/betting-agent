import os
import json
import unicodedata
import requests
import libsql
from datetime import timedelta
from dotenv import load_dotenv

from night_window import FINLAND_TZ, get_now_utc, parse_utc
from win_model import TEAM_ABBREV_MAP

load_dotenv()

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

IGNORED_NAME_TOKENS = {"jr", "sr", "ii", "iii"}

# Meidän tilastonimi -> NHL-boxscoren kenttä
STAT_FIELDS = {
    "goals": "goals",
    "points": "points",
    "assists": "assists",
    "shots_on_goal": "sog",
    "goalie_saves": "saves",
}


def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def nhl_date_for(commence_iso):
    """NHL:n score-endpoint käyttää Pohjois-Amerikan paikallista pelipäivää."""
    return (parse_utc(commence_iso) - timedelta(hours=8)).date().isoformat()


def get_nhl_scores_for_date(date_str):
    url = f"https://api-web.nhle.com/v1/score/{date_str}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json().get("games", [])


def player_key(name):
    """'Connor McDavid' ja 'C. McDavid' -> 'c mcdavid'."""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c)).lower()
    for ch in ".-'":
        n = n.replace(ch, " ")
    tokens = [t for t in n.split() if t not in IGNORED_NAME_TOKENS]
    if not tokens:
        return n.strip()
    if len(tokens) == 1:
        return tokens[0]
    return f"{tokens[0][0]} {tokens[-1]}"


def fetch_game_stats(nhl_game_id):
    """Palauttaa {(joukkue, pelaaja-avain): tilastot} ottelun boxscoresta."""
    url = f"https://api-web.nhle.com/v1/gamecenter/{nhl_game_id}/boxscore"
    box = requests.get(url, timeout=30).json()
    stats = box.get("playerByGameStats", {})
    players = {}
    for side in ("homeTeam", "awayTeam"):
        abbrev = (box.get(side) or {}).get("abbrev")
        for group in ("forwards", "defense", "goalies"):
            for p in stats.get(side, {}).get(group, []):
                name = (p.get("name") or {}).get("default")
                if not name:
                    continue
                players[(abbrev, player_key(name))] = {k: p.get(v) for k, v in STAT_FIELDS.items()}
    return players


def find_player(players, team_full, name):
    key = player_key(name)
    abbrev = TEAM_ABBREV_MAP.get(team_full) if team_full else None
    if abbrev and (abbrev, key) in players:
        return players[(abbrev, key)]
    matches = [v for (ab, k), v in players.items() if k == key]
    return matches[0] if len(matches) == 1 else None


def evaluate_selection(sel, home_ab, away_ab, hs, as_, get_stats):
    """True/False, tai None jos valintaa ei voi (vielä) ratkaista."""
    kind = sel.get("type")
    side = sel.get("side")
    point = sel.get("point")

    if kind == "h2h":
        winner = home_ab if hs > as_ else away_ab
        return TEAM_ABBREV_MAP.get(sel["team"]) == winner

    if kind == "spread":
        ab = TEAM_ABBREV_MAP.get(sel["team"])
        if ab == home_ab:
            margin = hs - as_
        elif ab == away_ab:
            margin = as_ - hs
        else:
            return None
        return margin + point > 0

    if kind == "total":
        over = (hs + as_) > point
        return over if side == "over" else not over

    if kind == "team_total":
        ab = TEAM_ABBREV_MAP.get(sel["team"])
        goals = hs if ab == home_ab else as_ if ab == away_ab else None
        if goals is None:
            return None
        return goals > point if side == "over" else goals < point

    if kind == "btts":
        both = hs > 0 and as_ > 0
        return both if side == "yes" else not both

    if kind in ("player", "prop"):
        stat = sel.get("stat") or "goals"
        entry = find_player(get_stats(), sel.get("team"), sel["player"])
        if entry is None:
            print(f"  Pelaajaa ei löytynyt boxscoresta: {sel['player']}")
            return None
        value = entry.get(stat)
        if value is None:
            return None
        if kind == "prop" or side == "yes":
            return value >= 1
        if side == "over":
            return value > point
        if side == "under":
            return value < point
    return None


def check_and_update_results():
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, home_team, away_team, commence_time, selections_json
        FROM sent_picks
        WHERE checked = 0 AND selections_json IS NOT NULL
    """).fetchall()

    now = get_now_utc()
    by_date = {}
    for row in rows:
        if parse_utc(row[3]) > now - timedelta(hours=4):
            continue  # ottelu tuskin vielä päättynyt
        by_date.setdefault(nhl_date_for(row[3]), []).append(row)

    stats_cache = {}

    def get_stats_for(gid):
        if gid not in stats_cache:
            stats_cache[gid] = fetch_game_stats(gid)
        return stats_cache[gid]

    for date_str, picks in by_date.items():
        try:
            games = get_nhl_scores_for_date(date_str)
        except Exception as e:
            print(f"Tulosten haku päivälle {date_str} epäonnistui: {e}")
            continue

        finals = {}
        for g in games:
            if g.get("gameState") in ("OFF", "FINAL"):
                finals[(g["homeTeam"]["abbrev"], g["awayTeam"]["abbrev"])] = (
                    g["id"], g["homeTeam"]["score"], g["awayTeam"]["score"]
                )

        for row_id, home, away, _, sel_json in picks:
            home_ab = TEAM_ABBREV_MAP.get(home)
            away_ab = TEAM_ABBREV_MAP.get(away)
            info = finals.get((home_ab, away_ab))
            if info is None:
                continue
            gid, hs, as_ = info

            results = []
            for s in json.loads(sel_json):
                try:
                    results.append(evaluate_selection(
                        s, home_ab, away_ab, hs, as_, lambda g=gid: get_stats_for(g)))
                except Exception as e:
                    print(f"  Valinnan tarkistus epäonnistui ({home} vs {away}): {e}")
                    results.append(None)

            if any(r is False for r in results):
                hit = 0
            elif any(r is None for r in results):
                continue
            else:
                hit = 1
            conn.execute("UPDATE sent_picks SET checked = 1, hit = ? WHERE id = ?", (hit, row_id))
    conn.commit()


def get_results_summary():
    """(taso, osuneet, tarkistetut, kaikki) eilen lähetetyille lapuille."""
    conn = get_connection()
    yesterday = (get_now_utc().astimezone(FINLAND_TZ).date() - timedelta(days=1)).isoformat()
    return conn.execute("""
        SELECT tier,
               SUM(CASE WHEN checked = 1 THEN hit ELSE 0 END),
               SUM(checked),
               COUNT(*)
        FROM sent_picks
        WHERE sent_date = ?
        GROUP BY tier
    """, (yesterday,)).fetchall()


if __name__ == "__main__":
    check_and_update_results()
    for tier, hits, checked, total in get_results_summary():
        print(f"{tier}: {hits or 0}/{total} osui ({checked or 0} tarkistettu)")