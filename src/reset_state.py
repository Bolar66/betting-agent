"""Nollaa "lähetetty tänään" -tilan, jotta build_combo.py voi lähettää uudelleen."""
from common import get_connection

conn = get_connection()
conn.execute("DELETE FROM bot_state WHERE key = 'last_sent_date'")
conn.commit()
print("Nollattu")
