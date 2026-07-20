import sqlite3
from math import prod
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

DB_PATH = "data/odds.db"

MAX_LEGS = 20
MAX_ODDS = 250_000
TARGET_MIN_ODDS = 10_000

FINLAND_TZ = ZoneInfo("Europe/Helsinki")
NIGHT_START_HOUR = 18  # klo 18 Suomen aikaa
NIGHT_END_HOUR = 6     # klo 06 Suomen aikaa (seuraavana aamuna)


def get_latest_odds():
    """Hakee jokaiselle ottelulle vain uusimman snapshotin."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        WITH latest AS (
            SELECT game_id, MAX(fetched_at) AS max_fetched
            FROM odds_snapshots
            GROUP BY game_id
        )
        SELECT o.game_id, o.home_team, o.away_team, o.commence_time,
               o.outcome_name, AVG(o.price) AS avg_price
        FROM odds_snapshots o
        JOIN latest l ON o.game_id = l.game_id AND o.fetched_at = l.max_fetched
        GROUP BY o.game_id, o.outcome_name
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows


def build_h2h_selections(rows):
    """Muodostaa moneyline-valinnan (suosikki) per ottelu,
    ryhmiteltynä game_id:n mukaan bet builderia varten."""
    games = {}
    for game_id, home, away, commence, outcome_name, price in rows:
        games.setdefault(game_id, {
            "home": home, "away": away, "commence": commence, "outcomes": []
        })
        games[game_id]["outcomes"].append((outcome_name, price))

    selections_by_game = {}
    for game_id, g in games.items():
        best_outcome, best_price = min(g["outcomes"], key=lambda x: x[1])
        selections_by_game[game_id] = {
            "match": f"{g['home']} vs {g['away']}",
            "commence": g["commence"],
            "selections": [
                {"description": f"{best_outcome} voittaa", "market_type": "h2h", "price": best_price}
            ],
        }
    return selections_by_game


def add_mock_player_props(selections_by_game):
    """MOCK-DATA: lisää player prop -valintoja olemassa oleviin
    otteluihin. Korvataan myöhemmin oikealla fetch_player_props.py
    -kutsulla kun kausi alkaa ja propsit ovat saatavilla."""
    mock_props = {
        "9de33ce1013f2a3375dbded2c9fbc7d6": [
            {"description": "Sebastian Aho tekee maalin", "market_type": "player_goal_scorer_anytime", "price": 2.20},
        ],
        "485b295347cb22f002e014cb87813ed7": [
            {"description": "Auston Matthews yli 0.5 pistettä", "market_type": "player_points", "price": 1.75},
        ],
        "ac3cb11040dbdb0d2ccfa53e7d6b6cda": [
            {"description": "David Pastrnak tekee maalin", "market_type": "player_goal_scorer_anytime", "price": 2.05},
        ],
    }
    for game_id, props in mock_props.items():
        if game_id in selections_by_game:
            selections_by_game[game_id]["selections"].extend(props)
    return selections_by_game


def build_bet_builder_legs(selections_by_game):
    """Yhdistää jokaisen ottelun kaikki valinnat yhdeksi 'bet builder'
    -legiksi: yhteiskerroin = valintojen kertoimien tulo.
    HUOM: olettaa valinnat tilastollisesti riippumattomiksi, mikä
    todennäköisesti aliarvioi todellista osumatodennäköisyyttä
    (esim. joukkueen voitto ja oman pelaajan maalinteko korreloivat)."""
    legs = []
    for game_id, g in selections_by_game.items():
        combined_price = prod(sel["price"] for sel in g["selections"])
        description = " + ".join(sel["description"] for sel in g["selections"])
        legs.append({
            "game_id": game_id,
            "match": g["match"],
            "commence": g["commence"],
            "pick": description,
            "price": combined_price,
            "implied_prob": 1 / combined_price,
        })
    return legs


def get_next_night_window(now_utc):
    """Laskee seuraavan/käynnissä olevan 18:00-06:00 -ikkunan
    (Suomen aikaa) alku- ja loppuajan UTC:na."""
    now_fi = now_utc.astimezone(FINLAND_TZ)

    today_night_start = now_fi.replace(hour=NIGHT_START_HOUR, minute=0, second=0, microsecond=0)

    if now_fi.hour < NIGHT_END_HOUR:
        # Ollaan jo yön puolella (esim. klo 03:00) -> yö alkoi eilen klo 18
        night_start = today_night_start - timedelta(days=1)
    else:
        # Joko yö on jo käynnissä (klo >= 18) tai ei vielä alkanut tänään
        night_start = today_night_start

    night_end = (night_start + timedelta(days=1)).replace(
        hour=NIGHT_END_HOUR, minute=0, second=0, microsecond=0
    )

    return night_start.astimezone(timezone.utc), night_end.astimezone(timezone.utc)


def filter_to_next_night(legs):
    """Rajaa legit lähimpään tulevaan/käynnissä olevaan NHL-yöhön
    (klo 18-06 Suomen aikaa)."""
    now_utc = datetime.now(timezone.utc)
    window_start, window_end = get_next_night_window(now_utc)

    in_window = []
    for leg in legs:
        commence = datetime.fromisoformat(leg["commence"].replace("Z", "+00:00"))
        if window_start <= commence <= window_end and commence > now_utc:
            in_window.append(leg)

    return in_window, window_start, window_end


def build_slip(legs):
    """Rakentaa lapun annetuista legeistä:
    - Käyttää enintään MAX_LEGS legiä
    - Ei koskaan ylitä MAX_ODDS:ia (250k)
    - Pyrkii saavuttamaan TARGET_MIN_ODDS:in (10k) jos mahdollista
    - Hiljaisina öinä palauttaa silti parhaan mahdollisen lapun
      sen sijaan että palauttaisi tyhjää."""
    sorted_legs = sorted(legs, key=lambda x: -x["implied_prob"])

    selected = []
    total_odds = 1.0

    for leg in sorted_legs:
        if len(selected) >= MAX_LEGS:
            break
        candidate_odds = total_odds * leg["price"]
        if candidate_odds > MAX_ODDS:
            continue
        selected.append(leg)
        total_odds = candidate_odds
        if total_odds >= TARGET_MIN_ODDS:
            break

    if not selected:
        return None

    total_prob = prod(leg["implied_prob"] for leg in selected)
    reached_target = total_odds >= TARGET_MIN_ODDS

    return {
        "legs": selected,
        "total_odds": total_odds,
        "total_prob": total_prob,
        "reached_target": reached_target,
    }


if __name__ == "__main__":
    rows = get_latest_odds()
    selections_by_game = build_h2h_selections(rows)
    selections_by_game = add_mock_player_props(selections_by_game)  # TODO: korvaa oikealla haulla

    legs = build_bet_builder_legs(selections_by_game)
    print(f"Muodostettu {len(legs)} bet builder -legiä (per ottelu).")

    legs, window_start, window_end = filter_to_next_night(legs)
    window_start_fi = window_start.astimezone(FINLAND_TZ)
    window_end_fi = window_end.astimezone(FINLAND_TZ)
    print(f"Yö-ikkuna (Suomen aikaa): {window_start_fi.strftime('%d.%m.%Y %H:%M')} - {window_end_fi.strftime('%d.%m.%Y %H:%M')}")
    print(f"Näistä {len(legs)} legiä osuu tähän ikkunaan.\n")

    result = build_slip(legs)
    if result is None:
        print("Ei yhtään ottelua/legiä saatavilla tälle yölle.")
    else:
        status = "TAVOITE SAAVUTETTU (10k-250k)" if result["reached_target"] else "HILJAINEN YÖ - alle tavoitteen"
        print(f"[{status}]")
        print(f"Lappu ({len(result['legs'])} ottelua):")
        for leg in result["legs"]:
            print(f"  {leg['match']}")
            print(f"    -> {leg['pick']} @ {leg['price']:.2f}")
        print(f"\nYhteiskerroin: {result['total_odds']:.1f}")
        print(f"Arvioitu osumatodennäköisyys (bookmakerin implisiittinen): {result['total_prob']*100:.6f}%")