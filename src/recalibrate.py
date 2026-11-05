"""Kahden viikon raportti: osumat tasoittain ja CLV (kerroin vs. sulkeutumiskerroin).

CLV kertoo nopeimmin, onko valinnoissa todellista etua: jos saamme jatkuvasti
parempia kertoimia kuin markkina juuri ennen ottelua, valinnat ovat oikeaan suuntaan,
vaikka laput eivät vielä osuisikaan.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common import get_connection, escape_markdown, send_telegram_message

FINLAND_TZ = ZoneInfo("Europe/Helsinki")
REPORT_DAYS = 14
FORCE = os.getenv("RECALIBRATE_FORCE") == "1"
DRY_RUN = os.getenv("DRY_RUN") == "1"

TYPE_TO_MARKET = {"h2h": "h2h", "spread": "spreads", "total": "totals",
                  "team_total": "team_totals", "btts": "team_bothTeamsScored_yn"}


def is_biweekly_run_week():
    """Raportti vain parillisilla ISO-viikoilla (workflow ajetaan joka sunnuntai)."""
    return datetime.now(FINLAND_TZ).isocalendar()[1] % 2 == 0


def market_key(sel):
    if sel.get("type") == "player":
        return f"player_{sel['stat']}_{'yn' if sel.get('point') is None else 'ou'}"
    return TYPE_TO_MARKET.get(sel.get("type"))


def odds_key(key, subject, side, point):
    """Hakuavain: aihe tarvitaan vain pelaajille ja joukkueen maalimäärälle."""
    subj = subject if key == "team_totals" or key.startswith("player_") else None
    return key, subj, side, None if point is None else float(point)


def sel_key(sel):
    key = market_key(sel)
    if key is None:
        return None
    subject = sel.get("player") if sel["type"] == "player" else sel.get("team")
    return odds_key(key, subject, sel.get("side"), sel.get("point"))


def load_event_odds(conn, table, price_col, event_id, time_col):
    """{hakuavain: kerroin} ottelun viimeisimmästä tallennuksesta."""
    rows = conn.execute(f"""
        SELECT market_key, subject, side, point, {price_col}, {time_col} FROM {table}
        WHERE event_id = ?
    """, (event_id,)).fetchall()
    if not rows:
        return {}
    latest = max(r[5] for r in rows)
    return {odds_key(k, subj, side, pt): price
            for k, subj, side, pt, price, t in rows if price and t == latest}


def tier_stats(conn, since):
    """(taso, osuneet pelit, pelit, läpi menneet laput, laput)"""
    rows = conn.execute("""
        SELECT tier, sent_date, SUM(hit), COUNT(*) FROM sent_picks
        WHERE checked = 1 AND sent_date >= ? GROUP BY tier, sent_date
    """, (since,)).fetchall()
    out = {}
    for tier, _, hits, n in rows:
        t = out.setdefault(tier, [0, 0, 0, 0])
        t[0] += hits or 0
        t[1] += n
        t[2] += 1 if hits == n else 0
        t[3] += 1
    return out


def clv_stats(conn, since):
    """{taso: [CLV-arvot]} lähetetyille valinnoille, joille sulkeutumiskerroin löytyy."""
    rows = conn.execute("""
        SELECT tier, game_id, selections_json FROM sent_picks
        WHERE sent_date >= ? AND selections_json IS NOT NULL
    """, (since,)).fetchall()
    closing, sent = {}, {}
    result = {}
    for tier, event_id, sel_json in rows:
        if event_id not in closing:
            closing[event_id] = load_event_odds(conn, "sgo_closing", "close_price", event_id, "collected_at")
        for sel in json.loads(sel_json):
            k = sel_key(sel)
            close = closing[event_id].get(k) if k else None
            if not close:
                continue
            price = sel.get("price")
            if not price:  # vanhat valinnat: kerroin lähetyshetken haun mukaan
                if event_id not in sent:
                    sent[event_id] = load_event_odds(conn, "sgo_odds", "price", event_id, "fetched_at")
                price = sent[event_id].get(k)
            if price:
                result.setdefault(tier, []).append(price / close - 1)
    return result


def build_report(conn):
    today = datetime.now(FINLAND_TZ).date()
    since = (today - timedelta(days=REPORT_DAYS)).isoformat()
    recent, total = tier_stats(conn, since), tier_stats(conn, "0000")
    clv = clv_stats(conn, since)

    lines = ["🔧 *Kahden viikon raportti*", ""]
    if not total:
        return "\n".join(lines + [escape_markdown("Ei vielä tarkistettua dataa.")])

    lines.append(f"*{escape_markdown(f'Osumat, viimeiset {REPORT_DAYS} pv (koko historia)')}*")
    for tier in sorted(total, key=lambda t: -total[t][1]):
        h, n, sh, sn = recent.get(tier, (0, 0, 0, 0))
        H, N, SH, SN = total[tier]
        pct = f"{h / n * 100:.0f} %" if n else "-"
        lines.append(escape_markdown(
            f"{tier}: pelit {pct} ({h}/{n}), laput {sh}/{sn}  ({H / N * 100:.0f} %, laput {SH}/{SN})"))
    lines.append("")

    lines.append("*" + escape_markdown("CLV (kertoimemme vs. sulkeutumiskerroin)") + "*")
    all_vals = [v for vals in clv.values() for v in vals]
    if not all_vals:
        lines.append(escape_markdown("Ei vielä vertailukelpoisia valintoja."))
    else:
        for tier, vals in sorted(clv.items()):
            beat = sum(v > 0 for v in vals) / len(vals) * 100
            lines.append(escape_markdown(
                f"{tier}: {sum(vals) / len(vals) * 100:+.1f} % keskimäärin, "
                f"parempi kuin sulkeutuminen {beat:.0f} %:ssa ({len(vals)} valintaa)"))
        avg = sum(all_vals) / len(all_vals) * 100
        verdict = ("Valinnat ovat keskimäärin markkinaa edellä." if avg > 0.5 else
                   "Valinnat ovat markkinan tasolla." if avg > -0.5 else
                   "Markkina liikkuu valintojamme vastaan.")
        lines.append("")
        lines.append("_" + escape_markdown(f"Yhteensä {avg:+.1f} %. {verdict}") + "_")
    return "\n".join(lines)


if __name__ == "__main__":
    if not FORCE and not is_biweekly_run_week():
        print("Ei kahden viikon välein osuva viikko - ohitetaan tämä ajo.")
        sys.exit(0)
    message = build_report(get_connection())
    print(message.replace("\\", ""))
    if not DRY_RUN:
        send_telegram_message(message)
        print("Raportti lähetetty.")
