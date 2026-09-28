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


def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def nhl_date_for(commence_iso):
    """NHL:n score-endpoint käyttää Pohjois-Amerikan paikallista pelipäivää.
    Vähennetään 8 h UTC-alkamisajasta, jolloin ilta-ottelut osuvat oikealle
    päivälle (myös Suomen aikaan yöllä alkavat)."""
    return (parse_utc(commence_iso) - timedelta(hours=8)).date().isoformat()


def get_nhl_scores_for_date(date_str):
    url = f"https://api-web.nhle.com/v1/score/{date_str}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json().get("games", [])


def player_key(name):
    """Yhtenäistää pelaajan nimen vertailua varten: 'Connor McDavid' (Odds API)
    ja 'C. McDavid' (NHL boxscore) -> 'c mcdavid'."""
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


def fetch_game_players(nhl_game_id):
    """Palauttaa (kaikki_pelaajat, maalintekijät) ottelun boxscoresta."""
    url = f"https://api-web.nhle.com/v1/gamecenter/{nhl_game_id}/boxscore"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    stats = response.json().get("playerByGameStats", {})

    roster, scorers = set(), set()
    for side in ("homeTeam", "awayTeam"):
        for group in ("forwards", "defense"):
            for p in stats.get(side, {}).get(group, []):
                name = (p.get("name") or {}).get("default")
                if not name:
                    continue
                key = player_key(name)
                roster.add(key)
                if p.get("goals", 0) > 0:
                    scorers.add(key)
    return roster, scorers


def evaluate_selection(sel, home_ab, away_ab, home_score, away_score, get_players):
    """Palauttaa True/False, tai None jos valintaa ei voi (vielä) tarkistaa."""
    kind = sel.get("type")
    if kind == "h2h":
        winner = home_ab if home_score > away_score else away_ab
        return TEAM_ABBREV_MAP.get(sel["team"]) == winner
    if kind == "spread":
        ab = TEAM_ABBREV_MAP.get(sel["team"])
        if ab == home_ab:
            margin = home_score - away_score
        elif ab == away_ab:
            margin = away_score - home_score
        else:
            return None
        return margin + sel["point"] > 0
    if kind == "total":
        over = (home_score + away_score) > sel["point"]
        return over if sel.get("side") == "over" else (not over)
    if kind == "prop":
        roster, scorers = get_players()
        key = player_key(sel["player"])
        if key not in roster:
            print(f"  Pelaajaa ei löytynyt boxscoresta: {sel['player']} (avain '{key}')")
            return None
        return key in scorers
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

    players_cache = {}

    def get_players_for(nhl_game_id):
        if nhl_game_id not in players_cache:
            players_cache[nhl_game_id] = fetch_game_players(nhl_game_id)
        return players_cache[nhl_game_id]

    for date_str, picks in by_date.items():
        try:
            games = get_nhl_scores_for_date(date_str)
        except Exception as e:
            print(f"Tulosten haku päivälle {date_str} epäonnistui: {e}")
            continue

        finals = {}
        for g in games:
            if g.get("gameState") not in ("OFF", "FINAL"):
                continue
            finals[(g["homeTeam"]["abbrev"], g["awayTeam"]["abbrev"])] = {
                "id": g["id"],
                "home": g["homeTeam"]["score"],
                "away": g["awayTeam"]["score"],
            }

        for row_id, home, away, _, sel_json in picks:
            home_ab = TEAM_ABBREV_MAP.get(home)
            away_ab = TEAM_ABBREV_MAP.get(away)
            info = finals.get((home_ab, away_ab))
            if info is None:
                continue

            results = []
            for s in json.loads(sel_json):
                try:
                    results.append(evaluate_selection(
                        s, home_ab, away_ab, info["home"], info["away"],
                        lambda gid=info["id"]: get_players_for(gid),
                    ))
                except Exception as e:
                    print(f"  Valinnan tarkistus epäonnistui ({home} vs {away}): {e}")
                    results.append(None)

            if any(r is False for r in results):
                hit = 0          # yksikin hävinnyt valinta tekee legistä hävityn
            elif any(r is None for r in results):
                continue         # ei voida vielä ratkaista
            else:
                hit = 1
            conn.execute("UPDATE sent_picks SET checked = 1, hit = ? WHERE id = ?", (hit, row_id))
    conn.commit()


def get_results_summary():
    conn = get_connection()
    yesterday = (get_now_utc().astimezone(FINLAND_TZ).date() - timedelta(days=1)).isoformat()
    cursor = conn.execute("""
        SELECT tier, SUM(hit), COUNT(*)
        FROM sent_picks
        WHERE sent_date = ? AND checked = 1
        GROUP BY tier
    """, (yesterday,))
    return cursor.fetchall()


if __name__ == "__main__":
    check_and_update_results()
    for tier, hits, total in get_results_summary():
        print(f"{tier}: {hits or 0}/{total} osui")