import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from common import get_connection, escape_markdown, send_telegram_message

FINLAND_TZ = ZoneInfo("Europe/Helsinki")
MIN_SAMPLE_FOR_ADJUSTMENT = 50


def is_biweekly_run_week():
    """Tarkistaa onko kyseessä joka toinen viikko (parillinen ISO-viikkonumero),
    jotta raportti lähtee vain kahden viikon välein, ei joka sunnuntai."""
    week_number = datetime.now(FINLAND_TZ).isocalendar()[1]
    return week_number % 2 == 0


def get_cumulative_stats():
    conn = get_connection()
    cursor = conn.execute("""
        SELECT tier, SUM(hit), COUNT(*)
        FROM sent_picks
        WHERE checked = 1
        GROUP BY tier
    """)
    return cursor.fetchall()


if __name__ == "__main__":
    if not is_biweekly_run_week():
        print("Ei kahden viikon välein osuva viikko - ohitetaan tämä ajo.")
        sys.exit(0)

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