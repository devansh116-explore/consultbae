"""
WSGI entry point for Gunicorn - handles database initialization.
This allows Gunicorn to boot even if the DB doesn't exist yet.
"""

import sys
from pathlib import Path

# Ensure the app can be imported
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

# Initialize database if needed
DB_PATH = BASE_DIR / "consultbae.db"
UPLOADS_DIR = BASE_DIR / "uploads"

try:
    UPLOADS_DIR.mkdir(exist_ok=True)
    
    # Only run merge_pipeline if DB doesn't exist
    if not DB_PATH.exists():
        print("[WSGI] Database not found, initializing from merge pipeline...")
        try:
            from db.merge_pipeline import main as run_merge_pipeline
            run_merge_pipeline()
            print("[WSGI] Database initialized successfully")
        except Exception as e:
            print(f"[WSGI] Warning: Database initialization failed: {e}")
            print("[WSGI] App will continue, but may have limited functionality")
    else:
        print(f"[WSGI] Database found at {DB_PATH}")
        
except Exception as e:
    print(f"[WSGI] Error during startup: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc()

# Now import and expose the Flask app
from app.app import app

if __name__ == "__main__":
    app.run()
