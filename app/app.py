"""
Task 3 — mini audio collection app.

Two views:
  GET  /               submission form (name, phone, record-or-upload)
  POST /submit         saves the audio file, extracts features, writes a
                        row into audio_submissions, tries to link it to an
                        existing `people` record from Task 1 by phone
  GET  /submissions     list of everything submitted, with play button
                        and extracted properties

Run: python3 app.py   (serves http://localhost:5000)
"""

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, render_template, redirect, url_for, send_from_directory, flash, jsonify

from audio_analysis import analyze_audio

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "consultbae.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = "dev-only-secret-change-me"  # fine for a local demo, NOT for prod


@app.before_request
def check_database():
    """Check if database exists before handling requests."""
    if not DB_PATH.exists():
        return jsonify({
            "error": "Application database not initialized",
            "message": "The database is being initialized. Please refresh the page in a moment.",
            "status": 503
        }), 503


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def norm_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/submit", methods=["POST"])
def submit():
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    audio_file = request.files.get("audio")

    if not name or not phone:
        flash("Name and phone are required.")
        return redirect(url_for("index"))
    if not audio_file or audio_file.filename == "":
        flash("Please record or upload an audio clip.")
        return redirect(url_for("index"))

    # save raw upload with a unique filename, keep original extension if any
    ext = Path(audio_file.filename).suffix or ".webm"
    filename = f"{uuid.uuid4().hex}{ext}"
    save_path = UPLOAD_DIR / filename
    audio_file.save(save_path)

    try:
        features = analyze_audio(str(save_path))
    except Exception as e:
        flash(f"Could not analyze audio ({e}). File was still saved.")
        features = {
            "duration_sec": None, "sample_rate_hz": None,
            "bitrate_kbps": None, "loudness_dbfs": None,
            "quality_estimate": "unknown (analysis failed)",
        }

    conn = get_conn()
    # try to link to an existing person from the Task 1 merge by phone
    phone_norm = norm_phone(phone)
    person_row = conn.execute(
        "SELECT person_id FROM people WHERE primary_phone = ?", (phone_norm,)
    ).fetchone()
    person_id = person_row["person_id"] if person_row else None

    conn.execute(
        """INSERT INTO audio_submissions
           (person_id, name, phone, filename, duration_sec, sample_rate_hz,
            bitrate_kbps, loudness_dbfs, quality_estimate, submitted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            person_id, name, phone, filename,
            features["duration_sec"], features["sample_rate_hz"],
            features["bitrate_kbps"], features["loudness_dbfs"],
            features["quality_estimate"], datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    flash("Submitted — thanks!")
    return redirect(url_for("submissions"))


@app.route("/submissions")
def submissions():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM audio_submissions ORDER BY submitted_at DESC"
    ).fetchall()
    conn.close()
    return render_template("submissions.html", rows=rows)


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ---------------------------------------------------------------------------
# Task 2 support — tiny JSON API the n8n workflow talks to.
# n8n has no first-class SQLite node, so rather than fight that, the
# automation talks HTTP to this Flask app (same pattern you'd use against
# any real internal service). Two endpoints: pull people who still need a
# skill-category tag, and write the tag back after the LLM step decides it.
# ---------------------------------------------------------------------------

@app.route("/api/people/untagged")
def api_untagged_people():
    conn = get_conn()
    rows = conn.execute(
        "SELECT person_id, canonical_name, skills FROM people "
        "WHERE skills != '[]' AND (skill_category IS NULL OR skill_category = '')"
    ).fetchall()
    conn.close()
    return jsonify([
        {"person_id": r["person_id"], "name": r["canonical_name"], "skills": json.loads(r["skills"])}
        for r in rows
    ])


@app.route("/api/people/<int:person_id>/tag", methods=["POST"])
def api_tag_person(person_id):
    category = (request.json or {}).get("skill_category", "").strip()
    if not category:
        return jsonify({"error": "skill_category is required"}), 400
    conn = get_conn()
    conn.execute("UPDATE people SET skill_category = ? WHERE person_id = ?", (category, person_id))
    conn.commit()
    conn.close()
    return jsonify({"person_id": person_id, "skill_category": category})


if __name__ == "__main__":
    import os

    if not DB_PATH.exists():
        print(f"Error: consultbae.db not found at {DB_PATH}")
        print("Run db/merge_pipeline.py first (Task 1).")
        import sys
        sys.exit(1)
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"  # set FLASK_DEBUG=0 in prod
    app.run(debug=debug, host="0.0.0.0", port=port)
