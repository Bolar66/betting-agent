"""Vie Turso-kannan sisällön paikalliseksi SQLite-tiedostoksi ja/tai CSV-tiedostoiksi.

Käyttö:
    python export_db.py                  # sekä SQLite että CSV
    python export_db.py --format csv
    python export_db.py --format sqlite
"""
import argparse
import csv
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import libsql
from dotenv import load_dotenv

load_dotenv()

PAGE_SIZE = 5000
SKIP_PREFIXES = ("sqlite_", "libsql_", "_")

# Päivämääräsarakkeet yhteenvetoa varten
DATE_COLUMNS = {
    "odds_snapshots": "fetched_at",
    "historical_games": "date",
    "sgo_odds": "fetched_at",
    "sgo_closing": "collected_at",
    "player_game_stats": "game_date",
    "sent_picks": "sent_date",
}


def connect():
    url = os.getenv("TURSO_DATABASE_URL")
    token = os.getenv("TURSO_AUTH_TOKEN")
    if not url or not token:
        raise SystemExit("TURSO_DATABASE_URL tai TURSO_AUTH_TOKEN puuttuu .env-tiedostosta.")
    return libsql.connect(url, auth_token=token)


def list_tables(conn):
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [(name, sql) for name, sql in rows if not name.startswith(SKIP_PREFIXES)]


def column_names(conn, table):
    try:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        if cols:
            return cols
    except Exception:
        pass
    cursor = conn.execute(f'SELECT * FROM "{table}" LIMIT 1')
    return [d[0] for d in cursor.description]


def iter_rows(conn, table):
    """Hakee rivit sivuittain, jotta isot taulut eivät ylitä vastauksen kokorajaa."""
    last = 0
    while True:
        rows = conn.execute(
            f'SELECT rowid, * FROM "{table}" WHERE rowid > ? ORDER BY rowid LIMIT {PAGE_SIZE}',
            (last,),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            yield tuple(row[1:])
        last = rows[-1][0]


def print_summary(conn, tables):
    print("\n--- Yhteenveto ---")
    for name, _ in tables:
        col = DATE_COLUMNS.get(name)
        if not col:
            continue
        n, first, last = conn.execute(
            f'SELECT COUNT(*), MIN("{col}"), MAX("{col}") FROM "{name}"'
        ).fetchone()
        print(f"{name}: {n} riviä, {str(first)[:10]} ... {str(last)[:10]}")

    if any(name == "sent_picks" for name, _ in tables):
        fake = conn.execute(
            "SELECT COUNT(*) FROM sent_picks WHERE game_id LIKE 'fake%'"
        ).fetchone()[0]
        print(f"Testirivejä (game_id LIKE 'fake%') sent_picks-taulussa: {fake}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["sqlite", "csv", "both"], default="both")
    args = parser.parse_args()

    out_dir = Path("export") / datetime.now().strftime("%Y%m%d_%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = connect()
    tables = list_tables(conn)
    print(f"Tauluja: {len(tables)}")

    local = sqlite3.connect(out_dir / "nhl_bot.sqlite") if args.format in ("sqlite", "both") else None

    for name, create_sql in tables:
        cols = column_names(conn, name)
        print(f"\n{name}: {', '.join(cols)}")

        csv_file = writer = None
        if args.format in ("csv", "both"):
            csv_file = open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8-sig")
            writer = csv.writer(csv_file)
            writer.writerow(cols)

        if local is not None and create_sql:
            local.execute(create_sql)
        placeholders = ",".join("?" * len(cols))

        count, batch = 0, []
        for row in iter_rows(conn, name):
            count += 1
            if writer:
                writer.writerow(row)
            if local is not None:
                batch.append(row)
                if len(batch) >= PAGE_SIZE:
                    local.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', batch)
                    batch.clear()
        if local is not None and batch:
            local.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', batch)

        if csv_file:
            csv_file.close()
        print(f"  -> {count} riviä")

    if local is not None:
        local.commit()
        local.close()

    print_summary(conn, tables)
    print(f"\nValmis: {out_dir.resolve()}")


if __name__ == "__main__":
    main()