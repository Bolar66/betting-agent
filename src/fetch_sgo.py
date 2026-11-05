import os
import sys
import requests
from collections import Counter
from datetime import datetime, timezone

from common import get_connection, init_state_table, get_state, set_state, insert_many
from night_window import get_now_utc, get_next_night_window, parse_utc, today_fi

API_KEY = os.getenv("SGO_API_KEY")
BASE = "https://api.sportsgameodds.com/v2"

PAGE_LIMIT = 20
MAX_PAGES = 3   # suojaa kiintiötä, jos aikarajaus ei toimikaan
FORCE = os.getenv("SGO_FORCE") == "1"

# SGO:n pelaajatason statID -> meidän nimi. HUOM: SGO:n pelaajan "points" = maalit.
PLAYER_STAT_NAMES = {
    "points": "goals",
    "goals+assists": "points",
    "assists": "assists",
    "shots_onGoal": "shots_on_goal",
    "powerPlay_goals+assists": "pp_points",
    "firstToScore": "first_goal",
    "lastToScore": "last_goal",
}

COLUMNS = ("event_id", "home_team", "away_team", "commence_time", "market_key", "subject",
           "subject_team", "side", "point", "price", "fair_price", "n_books", "fetched_at")


def init_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sgo_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            market_key TEXT,
            subject TEXT,
            subject_team TEXT,
            side TEXT,
            point REAL,
            price REAL,
            fair_price REAL,
            n_books INTEGER,
            fetched_at TEXT
        )
    """)
    conn.commit()
    init_state_table(conn)


def american_to_decimal(value):
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("even", "ev"):
        return 2.0
    try:
        v = float(s)
    except ValueError:
        return None
    if v > 0:
        return round(1 + v / 100, 4)
    if v < 0:
        return round(1 + 100 / abs(v), 4)
    return None


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_start(ev):
    """Ottelun alkamisaika (varmistettu: status.startsAt)."""
    for path in (("status", "startsAt"), ("info", "startsAt"), ("status", "startTime")):
        cur = ev
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if cur:
            return cur
    return None


def get_usage():
    try:
        r = requests.get(f"{BASE}/account/usage", params={"apiKey": API_KEY}, timeout=30)
        r.raise_for_status()
        return r.json()["data"]["rateLimits"]["per-month"]["current-entities"]
    except Exception:
        return None


def fetch_events(window_start, window_end):
    events, cursor = [], None
    for page in range(MAX_PAGES):
        params = {
            "apiKey": API_KEY, "leagueID": "NHL", "oddsAvailable": "true", "limit": PAGE_LIMIT,
            "startsAfter": window_start.isoformat(), "startsBefore": window_end.isoformat(),
        }
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{BASE}/events", params=params, timeout=60)
        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.text[:300]}")
            sys.exit(1)
        data = r.json()
        if page == 0 and data.get("notice"):
            print("SGO notice:", data["notice"])
        events.extend(data.get("data", []))
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return events


def event_teams(ev):
    """(koti, vieras, {teamID: koko nimi})"""
    home = ev["teams"]["home"]["names"]["long"]
    away = ev["teams"]["away"]["names"]["long"]
    team_names = {ev["teams"][s]["teamID"]: ev["teams"][s]["names"]["long"] for s in ("home", "away")}
    return home, away, team_names


def count_books(odd):
    return sum(1 for b in (odd.get("byBookmaker") or {}).values() if b.get("available"))


def classify_odd(ev, odd, home, away, team_names, closing=False):
    """Luokittelee SGO-kertoimen meidän markkinaksi.
    Palauttaa (market_key, subject, subject_team, side, point) tai None.
    closing=True käyttää sulkeutumislinjaa (jos SGO antaa) ja sallii alkaneet ottelut."""
    if odd.get("periodID") != "game" or odd.get("cancelled"):
        return None
    if not closing and (not odd.get("bookOddsAvailable") or odd.get("started")):
        return None

    stat, ent = odd.get("statID"), odd.get("statEntityID")
    bet, side = odd.get("betTypeID"), odd.get("sideID")

    if bet == "ou":
        point = to_float((closing and odd.get("closeBookOverUnder")) or odd.get("bookOverUnder"))
    elif bet == "sp":
        point = to_float((closing and odd.get("closeBookSpread")) or odd.get("bookSpread"))
    else:
        point = None

    if ent in ("home", "away", "all"):
        subject = home if ent == "home" else away if ent == "away" else "all"
        subject_team = subject if ent != "all" else None
        if stat == "points":
            key = {"ml": "h2h", "sp": "spreads",
                   "ou": "totals" if ent == "all" else "team_totals"}.get(bet)
            if key is None:
                return None
        else:
            key = f"team_{stat}_{bet}"
    else:
        player = (ev.get("players") or {}).get(ent)
        if not player:
            return None
        if bet == "yn" and side != "yes":
            return None  # yes/no-markkinoista vain "yes"
        subject = player.get("name")
        subject_team = team_names.get(player.get("teamID"))
        key = f"player_{PLAYER_STAT_NAMES.get(stat, stat)}_{bet}"
    return key, subject, subject_team, side, point


def odd_to_row(ev, odd, home, away, team_names, fetched_at, commence):
    price = american_to_decimal(odd.get("bookOdds"))
    if not price:
        return None
    info = classify_odd(ev, odd, home, away, team_names)
    if info is None:
        return None
    key, subject, subject_team, side, point = info
    fair = american_to_decimal(odd.get("fairOdds"))
    return (ev["eventID"], home, away, commence, key, subject, subject_team,
            side, point, price, fair, count_books(odd), fetched_at)


if __name__ == "__main__":
    if not API_KEY:
        print("SGO_API_KEY puuttuu - ohitetaan.")
        sys.exit(0)

    conn = get_connection()
    init_tables(conn)

    today = today_fi().isoformat()
    if not FORCE and get_state(conn, "sgo_last_fetch_date") == today:
        print("SGO-haku jo tehty tänään - ohitetaan.")
        sys.exit(0)

    now = get_now_utc()
    window_start, window_end = get_next_night_window(now)
    used_before = get_usage()

    events = fetch_events(window_start, window_end)
    fetched_at = datetime.now(timezone.utc).isoformat()

    rows, tonight, outside, no_start = [], 0, 0, 0
    for ev in events:
        start = get_start(ev)
        if start is None:
            no_start += 1
            continue
        if not (window_start <= parse_utc(start) <= window_end):
            outside += 1
            continue
        tonight += 1
        home, away, team_names = event_teams(ev)
        for odd in ev.get("odds", {}).values():
            row = odd_to_row(ev, odd, home, away, team_names, fetched_at, start)
            if row:
                rows.append(row)

    insert_many(conn, "sgo_odds", COLUMNS, rows)
    set_state(conn, "sgo_last_fetch_date", today)
    used_after = get_usage()

    print(f"Otteluita: {len(events)} | ikkunassa: {tonight} | ulkopuolella: {outside} | "
          f"ilman alkamisaikaa: {no_start}")
    print(f"Tallennettu {len(rows)} riviä tauluun sgo_odds.")
    print(f"Kiintiö: {used_before} -> {used_after} / 2500")

    counts = Counter(r[4] for r in rows)
    print("Markkinat:", ", ".join(f"{k} {n}" for k, n in counts.most_common(10)))