import os
import requests
import libsql
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

FINLAND_TZ = ZoneInfo("Europe/Helsinki")
TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")
MIN_SAMPLE_FOR_ADJUSTMENT = 50


def is_biweekly_run_week():
    """Tarkistaa onko kyseessä joka toinen viikko (parillinen ISO-viikkonumero),
    jotta raportti lähtee vain kahden viikon välein, ei joka sunnuntai."""
    week_number = datetime.now(FINLAND_TZ).isocalendar()[1]
    return week_number % 2 == 0


def get_connection():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)


def get_cumulative_stats():
    conn = get_connection()
    cursor = conn.execute("""
        SELECT tier, SUM(hit), COUNT(*)
        FROM sent_picks
        WHERE checked = 1
        GROUP BY tier
    """)
    return cursor.fetchall()


def escape_markdown(text):
    special_chars = r"_*[]()~`>#+-=|{}.!"
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


def send_telegram_message(text):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    response = requests.post(url, data={
        "chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2",
    })
    response.raise_for_status()


if __name__ == "__main__":
    if not is_biweekly_run_week():
        print("Ei kahden viikon välein osuva viikko - ohitetaan tämä ajo.")
        exit(0)

    stats = get_cumulative_stats()

    lines = ["🔧 *Kahden viikon tarkistus*", ""]

    if not stats:
        lines.append("Ei vielä kertynyttä dataa\\.")
    else:
        total_checked = sum(row[2] for row in stats)
        if total_checked < MIN_SAMPLE_FOR_ADJUSTMENT:
            lines.append(
                escape_markdown(
                    f"Kertynyt {total_checked} tarkistettua poimintaa "
                    f"(tavoite {MIN_SAMPLE_FOR_ADJUSTMENT}+ ennen säätöjä)."
                )
            )
            lines.append("")
            lines.append("*Nykyiset osumaprosentit:*")
            for tier, hits, total in stats:
                hits = hits or 0
                pct = (hits / total * 100) if total else 0
                lines.append(escape_markdown(f"{tier}: {hits}/{total} ({pct:.0f}%)"))
            lines.append("")
            lines.append("_Ei tehty muutoksia logiikkaan \\- data vielä liian pieni\\._")
        else:
            lines.append(escape_markdown(f"Kertynyt {total_checked} tarkistettua poimintaa."))
            lines.append("")
            for tier, hits, total in stats:
                hits = hits or 0
                pct = (hits / total * 100) if total else 0
                lines.append(escape_markdown(f"{tier}: {hits}/{total} ({pct:.0f}%)"))
            lines.append("")
            lines.append("_Automaattinen MIN\\_EDGE\\-säätö ei ole vielä käytössä \\- rakennetaan kun dataa on riittävästi validointiin\\._")

    message = "\n".join(lines)
    send_telegram_message(message)
    print("Recalibration-raportti lähetetty.")
    print(message)