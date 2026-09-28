import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

FINLAND_TZ = ZoneInfo("Europe/Helsinki")
NIGHT_START_HOUR = 18
NIGHT_END_HOUR = 6


def get_now_utc():
    """Nykyhetki UTC:na. Testausta varten ympäristömuuttuja FAKE_NOW
    (esim. 2026-09-29T09:00:00+00:00) korvaa oikean kellon, jolloin
    koodiin ei tarvitse tehdä väliaikaisia muutoksia."""
    override = os.getenv("FAKE_NOW")
    if override:
        return datetime.fromisoformat(override).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def today_fi():
    return get_now_utc().astimezone(FINLAND_TZ).date()


def parse_utc(iso_string):
    return datetime.fromisoformat(iso_string.replace("Z", "+00:00"))


def get_next_night_window(now_utc):
    """Seuraava/käynnissä oleva klo 18-06 -ikkuna (Suomen aikaa), UTC:na."""
    now_fi = now_utc.astimezone(FINLAND_TZ)
    today_night_start = now_fi.replace(hour=NIGHT_START_HOUR, minute=0, second=0, microsecond=0)

    if now_fi.hour < NIGHT_END_HOUR:
        night_start = today_night_start - timedelta(days=1)
    else:
        night_start = today_night_start

    night_end = (night_start + timedelta(days=1)).replace(
        hour=NIGHT_END_HOUR, minute=0, second=0, microsecond=0
    )
    return night_start.astimezone(timezone.utc), night_end.astimezone(timezone.utc)