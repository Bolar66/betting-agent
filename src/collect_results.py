import os
import sys
import requests
from datetime import datetime, timezone, timedelta

from common import (get_connection, init_state_table, get_state, set_state, insert_many,
                    TEAM_ABBREV_MAP)
from night_window import get_now_utc, get_next_night_window, today_fi
from check_results import nhl_date_for, get_nhl_scores_for_date, player_key, fetch_boxscore
from fetch_sgo import (API_KEY, BASE, american_to_decimal, get_start, get_usage,
                       classify_odd, event_teams, count_books)

FORCE = os.getenv("COLLECT_FORCE") == "1"

CLOSE_COLUMNS = ("event_id", "home_team", "away_team", "commence_time", "market_key", "subject",
                 "subject_team", "side", "point", "close_price", "close_fair", "n_books",
                 "collected_at")
STAT_COLUMNS = ("nhl_game_id", "event_id", "game_date", "team", "player_name", "player_key",
                "position", "goals", "assists", "points", "sog", "blocks", "hits", "pp_goals",
                "saves", "starter")


def init_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sgo_closing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL, home_team TEXT, away_team TEXT, commence_time TEXT,
            market_key TEXT, subject TEXT, subject_team TEXT, side TEXT, point REAL,
            close_price REAL, close_fair REAL, n_books INTEGER, collected_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_game_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nhl_game_id INTEGER NOT NULL, event_id TEXT, game_date TEXT, team TEXT,
            player_name TEXT, player_key TEXT, position TEXT,
            goals INTEGER, assists INTEGER, points INTEGER, sog INTEGER, blocks INTEGER,
            hits INTEGER, pp_goals INTEGER, saves INTEGER, starter INTEGER
        )
    """)
    # Tarkistukset "onko jo tallennettu" käyttävät näitä
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sgo_closing_event ON sgo_closing(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pgs_game ON player_game_stats(nhl_game_id)")
    conn.commit()
    init_state_table(conn)


def fetch_ended_events(window_start, window_end):
    r = requests.get(f"{BASE}/events", params={
        "apiKey": API_KEY, "leagueID": "NHL", "limit": 30,
        "startsAfter": window_start.isoformat(), "startsBefore": window_end.isoformat(),
    }, timeout=60)
    r.raise_for_status()
    events = r.json().get("data", [])
    return [e for e in events if (e.get("status") or {}).get("completed")]


def closing_rows(ev, collected_at):
    home, away, team_names = event_teams(ev)
    commence = get_start(ev)
    rows = []
    for odd in (ev.get("odds") or {}).values():
        price = american_to_decimal(odd.get("closeBookOdds"))
        if not price:
            continue
        info = classify_odd(ev, odd, home, away, team_names, closing=True)
        if info is None:
            continue
        key, subject, subject_team, side, point = info
        fair = american_to_decimal(odd.get("closeFairOdds"))
        rows.append((ev["eventID"], home, away, commence, key, subject, subject_team,
                     side, point, price, fair, count_books(odd), collected_at))
    return rows


def player_rows(ev, nhl_games_by_date):
    """Hakee ottelun boxscoren NHL:stä ja palauttaa pelaajarivit, tai None."""
    home_ab = TEAM_ABBREV_MAP.get(ev["teams"]["home"]["names"]["long"])
    away_ab = TEAM_ABBREV_MAP.get(ev["teams"]["away"]["names"]["long"])
    game_date = nhl_date_for(get_start(ev))

    if game_date not in nhl_games_by_date:
        nhl_games_by_date[game_date] = get_nhl_scores_for_date(game_date)
    match = next((g for g in nhl_games_by_date[game_date]
                  if g["homeTeam"]["abbrev"] == home_ab and g["awayTeam"]["abbrev"] == away_ab
                  and g.get("gameState") in ("OFF", "FINAL")), None)
    if match is None:
        return None

    gid = match["id"]
    box = fetch_boxscore(gid)
    stats = box.get("playerByGameStats", {})
    rows = []
    for side_key, ab in (("homeTeam", home_ab), ("awayTeam", away_ab)):
        for group in ("forwards", "defense", "goalies"):
            for p in stats.get(side_key, {}).get(group, []):
                name = (p.get("name") or {}).get("default")
                if not name:
                    continue
                is_goalie = group == "goalies"
                rows.append((
                    gid, ev["eventID"], game_date, ab, name, player_key(name), p.get("position"),
                    p.get("goals"), p.get("assists"), p.get("points"), p.get("sog"),
                    p.get("blockedShots"), p.get("hits"), p.get("powerPlayGoals"),
                    p.get("saves") if is_goalie else None,
                    (1 if p.get("starter") else 0) if is_goalie else None,
                ))
    return gid, rows


if __name__ == "__main__":
    if not API_KEY:
        print("SGO_API_KEY puuttuu - ohitetaan.")
        sys.exit(0)

    conn = get_connection()
    init_tables(conn)

    today = today_fi().isoformat()
    if not FORCE and get_state(conn, "collect_last_date") == today:
        print("Tulokset jo kerätty tänään - ohitetaan.")
        sys.exit(0)

    window_start, window_end = get_next_night_window(get_now_utc() - timedelta(days=1))
    used_before = get_usage()
    events = fetch_ended_events(window_start, window_end)
    collected_at = datetime.now(timezone.utc).isoformat()
    print(f"Päättyneitä otteluita eilisyöltä: {len(events)}")

    nhl_games_by_date = {}
    n_close, n_players, unmatched = 0, 0, []
    for ev in events:
        eid = ev["eventID"]
        match = f"{ev['teams']['away']['names']['long']} @ {ev['teams']['home']['names']['long']}"

        if conn.execute("SELECT 1 FROM sgo_closing WHERE event_id = ? LIMIT 1", (eid,)).fetchone() is None:
            rows = closing_rows(ev, collected_at)
            insert_many(conn, "sgo_closing", CLOSE_COLUMNS, rows)
            n_close += len(rows)

        try:
            result = player_rows(ev, nhl_games_by_date)
        except Exception as e:
            print(f"  Boxscore epäonnistui ({match}): {e}")
            result = None
        if result is None:
            unmatched.append(match)
            continue
        gid, rows = result
        if conn.execute("SELECT 1 FROM player_game_stats WHERE nhl_game_id = ? LIMIT 1", (gid,)).fetchone() is None:
            insert_many(conn, "player_game_stats", STAT_COLUMNS, rows)
            n_players += len(rows)

    set_state(conn, "collect_last_date", today)
    print(f"Sulkeutumiskertoimia tallennettu: {n_close}")
    print(f"Pelaajarivejä tallennettu: {n_players}")
    if unmatched:
        print("NHL-ottelua ei löytynyt:", ", ".join(unmatched))
    print(f"SGO-kiintiö: {used_before} -> {get_usage()} / 2500")