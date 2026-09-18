# src/cleanup_test_data.py
import os
import libsql
from dotenv import load_dotenv

load_dotenv()
conn = libsql.connect(os.getenv("TURSO_DATABASE_URL"), auth_token=os.getenv("TURSO_AUTH_TOKEN"))
conn.execute("DELETE FROM sent_picks WHERE game_id LIKE 'fake%'")
conn.commit()
print("Siivottu")