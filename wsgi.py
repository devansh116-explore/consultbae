"""
WSGI entry point for Gunicorn - handles database initialization without blocking.
"""

import sys
import sqlite3
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

DB_PATH = BASE_DIR / "consultbae.db"
UPLOADS_DIR = BASE_DIR / "uploads"
DATA_DIR = BASE_DIR / "data"

print("[WSGI] ===== APP STARTUP =====")
print(f"[WSGI] BASE_DIR: {BASE_DIR}")
print(f"[WSGI] DB_PATH: {DB_PATH}")
print(f"[WSGI] DATA_DIR: {DATA_DIR}")

print("[WSGI] Creating upload directory...")
try:
    UPLOADS_DIR.mkdir(exist_ok=True)
    print(f"[WSGI] ✓ Upload directory ready at {UPLOADS_DIR}")
except Exception as e:
    print(f"[WSGI] ✗ Failed to create uploads dir: {e}")

# Try to initialize database only if it doesn't exist
if not DB_PATH.exists():
    print("[WSGI] Database not found, attempting to initialize...")
    try:
        # Try the full merge pipeline
        print("[WSGI] Importing merge_pipeline...")
        from db.merge_pipeline import main as run_merge_pipeline
        print("[WSGI] ✓ Merge pipeline imported")
        print("[WSGI] Running merge pipeline (this may take 30-60 seconds)...")
        run_merge_pipeline()
        print("[WSGI] ✓ Database initialized successfully from merge pipeline")
    except ImportError as ie:
        print(f"[WSGI] ✗ Import error in merge_pipeline: {ie}")
        print("[WSGI] Creating empty database schema as fallback...")
        try:
            create_empty_db()
            print("[WSGI] ✓ Empty database created (merge pipeline can be run manually later)")
        except Exception as fallback_error:
            print(f"[WSGI] ✗ Failed to create empty database: {fallback_error}")
            print("[WSGI] ⚠ App will start but may have errors")
    except Exception as e:
        print(f"[WSGI] ✗ Merge pipeline failed: {e}")
        print("[WSGI] Creating empty database schema as fallback...")
        try:
            create_empty_db()
            print("[WSGI] ✓ Empty database created (merge pipeline can be run manually later)")
        except Exception as fallback_error:
            print(f"[WSGI] ✗ Failed to create empty database: {fallback_error}")
            print("[WSGI] ⚠ App will start but may have errors")
else:
    print(f"[WSGI] ✓ Database found at {DB_PATH}")


def create_empty_db():
    """Create an empty database with the correct schema."""
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    
    # Create tables with the same schema as merge_pipeline.py
    cur.execute("""
        CREATE TABLE IF NOT EXISTS people (
            person_id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL,
            email TEXT,
            primary_phone TEXT,
            city TEXT,
            experience_years REAL,
            ctc_annual_inr INTEGER,
            skills TEXT DEFAULT '[]',
            skill_category TEXT
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS source_records (
            source_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            source_name TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audio_submissions (
            submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            filename TEXT NOT NULL,
            duration_sec REAL,
            sample_rate_hz INTEGER,
            bitrate_kbps INTEGER,
            loudness_dbfs REAL,
            quality_estimate TEXT,
            submitted_at TEXT NOT NULL,
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        )
    """)
    
    conn.commit()
    conn.close()


# Import Flask app
print("[WSGI] Importing Flask app...")
try:
    from app.app import app
    print("[WSGI] ✓ Flask app imported successfully")
except ImportError as ie:
    print(f"[WSGI] ✗ FATAL: Failed to import Flask app (ImportError): {ie}")
    import traceback
    traceback.print_exc()
    raise SystemExit(f"Cannot import Flask app: {ie}")
except Exception as e:
    print(f"[WSGI] ✗ FATAL: Failed to import Flask app: {e}")
    import traceback
    traceback.print_exc()
    raise SystemExit(f"Cannot import Flask app: {e}")

print("[WSGI] ===== APP READY =====")

if __name__ == "__main__":
    app.run()


