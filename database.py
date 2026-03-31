import sqlite3
DB_PATH = "/data/cat.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS raw_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp INTEGER,
        weight REAL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_time INTEGER,
        end_time INTEGER,
        duration INTEGER,
        total_diff REAL,
        max_slope REAL,
        mean_slope REAL,
        variance REAL,
        vibration_count INTEGER,
        label TEXT
    )
    """)
    conn.commit()
    conn.close()
