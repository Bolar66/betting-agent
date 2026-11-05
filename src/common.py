"""Yhteiset apufunktiot: tietokantayhteys, bottitila, Telegram ja joukkuenimet."""
import os

import libsql
import requests
from dotenv import load_dotenv

load_dotenv()

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

# Joukkueen koko nimi (SGO) -> NHL:n lyhenne
TEAM_ABBREV_MAP = {
    "Anaheim Ducks": "ANA", "Boston Bruins": "BOS", "Buffalo Sabres": "BUF",
    "Calgary Flames": "CGY", "Carolina Hurricanes": "CAR", "Chicago Blackhawks": "CHI",
    "Colorado Avalanche": "COL", "Columbus Blue Jackets": "CBJ", "Dallas Stars": "DAL",
    "Detroit Red Wings": "DET", "Edmonton Oilers": "EDM", "Florida Panthers": "FLA",
    "Los Angeles Kings": "LAK", "Minnesota Wild": "MIN", "Montréal Canadiens": "MTL",
    "Montreal Canadiens": "MTL", "Nashville Predators": "NSH", "New Jersey Devils": "NJD",
    "New York Islanders": "NYI", "New York Rangers": "NYR", "Ottawa Senators": "OTT",
    "Philadelphia Flyers": "PHI", "Pittsburgh Penguins": "PIT", "San Jose Sharks": "SJS",
    "Seattle Kraken": "SEA", "St Louis Blues": "STL", "St. Louis Blues": "STL",
    "Tampa Bay Lightning": "TBL", "Toronto Maple Leafs": "TOR", "Utah Mammoth": "UTA",
    "Vancouver Canucks": "VAN", "Vegas Golden Knights": "VGK", "Washington Capitals": "WSH",
    "Winnipeg Jets": "WPG",
}


# ---------- tietokanta ----------

def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def init_state_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()


def get_state(conn, key):
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn, key, value):
    conn.execute("""
        INSERT INTO bot_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, value))
    conn.commit()


def insert_many(conn, table, columns, rows, chunk=50):
    """Monirivinen INSERT paloina (Turso-kutsuja vähemmän kuin rivi kerrallaan).
    columns on sarakenimien tuple."""
    if not rows:
        return
    col_sql = "(" + ", ".join(columns) + ")"
    ph = "(" + ",".join(["?"] * len(columns)) + ")"
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        params = tuple(v for row in part for v in row)
        conn.execute(f"INSERT INTO {table} {col_sql} VALUES {','.join([ph] * len(part))}", params)
    conn.commit()


# ---------- Telegram ----------

def escape_markdown(text):
    for char in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(char, f"\\{char}")
    return text


def _telegram_url():
    return f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage"


def send_telegram_message(text, markdown=True):
    """Lähettää viestin; pilkkoo yli 3500 merkin viestit kappaleiden rajoilta."""
    chunks, current = [], ""
    for block in text.split("\n\n"):
        if current and len(current) + len(block) + 2 > 3500:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)

    for chunk in chunks:
        data = {"chat_id": os.getenv("TELEGRAM_CHAT_ID"), "text": chunk}
        if markdown:
            data["parse_mode"] = "MarkdownV2"
        requests.post(_telegram_url(), data=data, timeout=30).raise_for_status()


def send_error_alert(error_text):
    try:
        send_telegram_message(f"⚠️ Botti epäonnistui tänään:\n\n{error_text[:500]}", markdown=False)
    except Exception:
        pass
