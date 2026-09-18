import os
import requests
import libsql
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

FINLAND_TZ = ZoneInfo("Europe/Helsinki")
TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")


def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def get_nhl_scores_for_date(date_str):
    """Hakee kaikki NHL-ottelut ja niiden tulokset annetulta päivältä
    (muoto YYYY-MM-DD). Käyttää NHL:n virallista score-endpointtia."""
    url = f"https://api-web.nhle.com/v1/score/{date_str}"
    response = requests.get(url)
    response.raise_for_status()
    return response.json().get("games", [])


def find_winner(games, home_team, away_team):
    """Etsii ottelun joukkueiden nimien perusteella ja palauttaa
    voittaneen joukkueen nimen, tai None jos ottelua ei löydy
    tai sitä ei ole vielä pelattu loppuun.

    HUOM: Odds API käyttää täysiä joukkuenimiä (esim. 'Edmonton Oilers'),
    NHL API käyttää usein commonName/placeName-yhdistelmiä tai lyhenteitä
    - tätä vertailua PITÄÄ testata oikealla datalla ennen luottamista,
    koska en pysty varmistamaan täsmääkö nimimuoto ilman testiä."""
    for game in games:
        if game.get("gameState") not in ("OFF", "FINAL"):
            continue
        away_name = f"{game['awayTeam']['placeName']['default']} {game['awayTeam']['commonName']['default']}"
        home_name = f"{game['homeTeam']['placeName']['default']} {game['homeTeam']['commonName']['default']}"

        if away_name == away_team and home_name == home_team:
            home_score = game["homeTeam"]["score"]
            away_score = game["awayTeam"]["score"]
            return home_team if home_score > away_score else away_team
    return None


def check_and_update_results():
    """Käy läpi eilen lähetetyt, vielä tarkistamattomat poiminnat,
    ja päivittää hit/miss-tiedon."""
    conn = get_connection()
    yesterday_fi = (datetime.now(FINLAND_TZ) - timedelta(days=1)).date()

    cursor = conn.execute("""
        SELECT id, home_team, away_team, pick_description, commence_time
        FROM sent_picks
        WHERE checked = 0
    """)
    rows = cursor.fetchall()

    # Ryhmitellään ottelun pelipäivän mukaan (NHL API vaatii päivämäärän per haku)
    games_by_date = {}
    for row_id, home, away, pick, commence in rows:
        game_date = datetime.fromisoformat(commence.replace("Z", "+00:00")).astimezone(FINLAND_TZ).date()
        date_str = game_date.strftime("%Y-%m-%d")
        games_by_date.setdefault(date_str, []).append((row_id, home, away, pick))

    for date_str, picks in games_by_date.items():
        nhl_games = get_nhl_scores_for_date(date_str)
        for row_id, home, away, pick in picks:
            winner = find_winner(nhl_games, home, away)
            if winner is None:
                continue  # ottelua ei löytynyt tai ei vielä pelattu loppuun
            hit = 1 if winner in pick else 0
            conn.execute("""
                UPDATE sent_picks SET checked = 1, hit = ? WHERE id = ?
            """, (hit, row_id))
    conn.commit()


def get_results_summary():
    """Palauttaa eilisen tulosten yhteenvedon per taso."""
    conn = get_connection()
    yesterday = (datetime.now(FINLAND_TZ) - timedelta(days=1)).date().isoformat()

    cursor = conn.execute("""
        SELECT tier, SUM(hit), COUNT(*)
        FROM sent_picks
        WHERE sent_date = ? AND checked = 1
        GROUP BY tier
    """, (yesterday,))
    return cursor.fetchall()


if __name__ == "__main__":
    check_and_update_results()
    summary = get_results_summary()
    for tier, hits, total in summary:
        print(f"{tier}: {hits}/{total} osui")