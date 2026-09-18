import os
import libsql
from dotenv import load_dotenv

load_dotenv()

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

conn = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)
conn.execute("DELETE FROM bot_state WHERE key = 'last_sent_date'")
conn.commit()
print("Nollattu")