import unicodedata
import requests

from win_model import TEAM_ABBREV_MAP

_cache = {}


def norm_player(name):
    """Yhtenäistää nimen vertailua varten: aksentit, isot kirjaimet,
    väliviivat ja pisteet pois ('Charles-Alexis Legault' = 'Charles Alexis Legault')."""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c)).lower()
    for ch in ".-'":
        n = n.replace(ch, " ")
    return " ".join(n.split())


def team_players(team_full_name):
    """Palauttaa joukkueen kenttäpelaajien normalisoidut nimet NHL:n
    rosterista, tai None jos haku ei onnistu. Tulos välimuistissa ajon ajan."""
    abbrev = TEAM_ABBREV_MAP.get(team_full_name)
    if abbrev is None:
        return None
    if abbrev not in _cache:
        try:
            r = requests.get(f"https://api-web.nhle.com/v1/roster/{abbrev}/current", timeout=30)
            r.raise_for_status()
            data = r.json()
            names = set()
            for group in ("forwards", "defensemen"):
                for p in data.get(group, []):
                    try:
                        names.add(norm_player(f"{p['firstName']['default']} {p['lastName']['default']}"))
                    except (KeyError, TypeError):
                        continue
            _cache[abbrev] = names
        except Exception as e:
            print(f"Rosterin haku epäonnistui ({abbrev}): {e}")
            _cache[abbrev] = None
    return _cache[abbrev]