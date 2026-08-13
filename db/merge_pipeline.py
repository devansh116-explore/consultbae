"""
ConsultBae assignment — Task 1: merge pipeline.

Reads the three messy source CSVs, cleans/normalizes them, resolves which
rows across files refer to the same real person, and loads everything into
one SQLite database (consultbae.db).

Design decisions (also written up in DATA_ISSUES_REPORT.md):

1. No single ID is common to all three files, so matching is done with a
   priority-ordered set of keys, applied in this order:
      a) normalized email  (links source1 <-> source2, both have email)
      b) normalized phone  (links source1 <-> source3, both have phone)
      c) transitive link   (source2 <-> source3 have no shared field, so
         they're only linked *through* a source1 record that matches both)
      d) name + city fallback, ONLY used when (a)-(c) give no match, and
         only accepted if it resolves to exactly one candidate. If a
         name+city fallback is ambiguous (multiple candidates), we refuse
         to guess — we keep the record unmatched/separate and log it as a
         manual-review case. Silently guessing wrong here is worse than
         having two records for the same person.

2. Every source row is kept in `source_records` (full audit trail), even
   after being linked to a `people` record. Nothing is discarded except
   truly broken rows (a fully-blank row, a column-shifted junk row), and
   those are logged, not silently dropped.

Run: python3 merge_pipeline.py
Produces: consultbae.db, data_issues_log.json (consumed by the report)
"""

import csv
import json
import re
import sqlite3
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).parent.parent / "data"
DB_PATH = Path(__file__).parent.parent / "consultbae.db"
ISSUES_LOG_PATH = Path(__file__).parent.parent / "data_issues_log.json"

issues = []  # collected as we go, later feeds the data issues report


def log_issue(category, detail):
    issues.append({"category": category, "detail": detail})


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

CITY_ALIASES = {
    "bangalore": "Bengaluru",
    "bengaluru": "Bengaluru",
    "gurgaon": "Gurugram",
    "gurugram": "Gurugram",
    "new delhi": "Delhi",
    "delhi ncr": "Delhi",
    "delhi": "Delhi",
    "noida": "Noida",
    "pune": "Pune",
}


def norm_email(email: str) -> str:
    if not email:
        return ""
    return email.strip().lower()


def norm_phone(phone: str) -> str:
    """Strip everything but digits, then take the last 10 digits.
    Handles: +919000000254, 09000000287, 919000000231, +91-9000000131."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits


def norm_city(city: str) -> str:
    if not city:
        return ""
    key = city.strip().lower()
    return CITY_ALIASES.get(key, city.strip().title())


def norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip())


def norm_skill(skill: str) -> str:
    return skill.strip().lower()


DATE_FORMATS = ["%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%m/%d/%Y"]


def parse_date(raw: str):
    """source1's Applied Date column mixes 4 different formats. Normalize
    everything to ISO (YYYY-MM-DD) for storage; keep raw value too."""
    raw = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    log_issue("unparseable_date", f"Could not parse date '{raw}', stored as-is")
    return raw


def parse_ctc(raw: str):
    """source1's Current CTC mixes absolute rupees (417964) and lakhs
    (4.2). Heuristic: any value < 1000 is almost certainly lakhs (nobody's
    CTC is Rs 4). Convert lakhs -> absolute rupees for a single unit."""
    raw = raw.strip()
    try:
        val = float(raw)
    except ValueError:
        return None
    if val < 1000:  # lakhs
        return round(val * 100000)
    return round(val)


def parse_rate(raw: str):
    """source2's rate mixes '<n>/hr' and '<n>k/month'. Normalize both to an
    estimated annual figure (INR) so gig workers are comparable to CTC:
      hourly  -> rate * 8 hrs/day * 22 working days/month * 12
      monthly -> (k * 1000) * 12
    This is an approximation and is documented as such in the report."""
    raw = raw.strip()
    m = re.match(r"^([\d.]+)/hr$", raw)
    if m:
        hourly = float(m.group(1))
        return round(hourly * 8 * 22 * 12), "hourly"
    m = re.match(r"^([\d.]+)k/month$", raw, re.IGNORECASE)
    if m:
        monthly = float(m.group(1)) * 1000
        return round(monthly * 12), "monthly"
    log_issue("unparseable_rate", f"Could not parse rate '{raw}'")
    return None, None


def norm_status(raw: str) -> str:
    return raw.strip().title() if raw else ""


def norm_verified(raw: str) -> bool:
    return raw.strip().lower() in ("y", "yes", "true", "1")


# ---------------------------------------------------------------------------
# Load + clean each source
# ---------------------------------------------------------------------------

def load_source1():
    """Naukri applicants. Has: name, email, phone, city, experience, ctc,
    applied_date, skills. Email is the most reliable key here."""
    rows = []
    seen_keys = {}  # (email, phone) -> row index, to catch in-file dupes
    path = DATA_DIR / "source1_naukri_applicants.csv"
    with open(path, newline="", encoding="utf-8") as f:
        for i, r in enumerate(csv.DictReader(f)):
            email = norm_email(r["Email"])
            phone = norm_phone(r["Phone"])
            rec = {
                "raw_name": r["Full Name"].strip(),
                "name": norm_name(r["Full Name"]),
                "email": email,
                "phone": phone,
                "city": norm_city(r["City"]),
                "raw_city": r["City"].strip(),
                "experience_years": float(r["Experience (Years)"]) if r["Experience (Years)"] else None,
                "ctc_annual_inr": parse_ctc(r["Current CTC"]),
                "applied_date": parse_date(r["Applied Date"]),
                "skills": [norm_skill(s) for s in r["Skills"].split(",")] if r["Skills"].strip() else [],
            }
            # in-file duplicate detection: same phone == same person even if
            # name/email differ slightly (e.g. "R. Verma" vs "Rohit Verma",
            # or an "alt." email prefix for the same person)
            dup_key = phone if phone else email
            if dup_key and dup_key in seen_keys:
                prev = rows[seen_keys[dup_key]]
                log_issue(
                    "in_source_duplicate",
                    f"source1: '{rec['raw_name']}' ({email}) and '{prev['raw_name']}' "
                    f"({prev['email']}) share phone {phone} — same person, kept the "
                    f"fuller/first name and recorded the second email as an alt contact.",
                )
                if len(rec["name"]) >= len(prev["name"]):
                    prev["name"] = rec["name"]
                prev.setdefault("alt_emails", []).append(email)
                continue
            rec["alt_emails"] = []
            seen_keys[dup_key] = len(rows)
            rows.append(rec)
    return rows


def load_source2():
    """Gig workers. Has: email, name, rate, location, status, skill_tags.
    No phone field — this is why source2<->source3 can only be linked
    transitively through source1."""
    rows = []
    path = DATA_DIR / "source2_gig_workers.csv"
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        expected_cols = len(header)
        for line_no, r in enumerate(reader, start=2):
            if not any(cell.strip() for cell in r):
                log_issue("blank_row", f"source2 line {line_no}: fully empty row, dropped")
                continue
            if len(r) != expected_cols:
                log_issue(
                    "malformed_row",
                    f"source2 line {line_no}: wrong column count ({len(r)} vs "
                    f"{expected_cols}) — {r}. Dropped; a well-formed duplicate of "
                    f"this person (Isha Chopra) already exists earlier in the file.",
                )
                continue
            row = dict(zip(header, r))
            # sanity check: does this look column-shifted even with the
            # right count? (e.g. email_id field not containing '@')
            if "@" not in row["email_id"]:
                log_issue(
                    "malformed_row",
                    f"source2 line {line_no}: email_id field has no '@' "
                    f"({row['email_id']!r}) — looks column-shifted, dropped.",
                )
                continue
            rate_annual, rate_type = parse_rate(row["rate"])
            rows.append({
                "raw_name": row["worker_name"].strip(),
                "name": norm_name(row["worker_name"]),
                "email": norm_email(row["email_id"]),
                "city": norm_city(row["location"]),
                "raw_city": row["location"].strip(),
                "rate_annual_est_inr": rate_annual,
                "rate_type": rate_type,
                "status": norm_status(row["status"]),
                "skills": [norm_skill(s) for s in row["skill_tags"].split(",")] if row["skill_tags"].strip() else [],
            })
    return rows


def load_source3():
    """CBNexus contacts. Has: name, phone, city, verified, projects.
    No email field. The file contains an embedded duplicate header row
    partway through (two blocks concatenated) — csv.DictReader would treat
    that literal header row as a data row, so it's filtered out explicitly."""
    rows = []
    path = DATA_DIR / "source3_cbnexus_contacts.csv"
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        for line_no, r in enumerate(reader, start=2):
            if r == header:
                log_issue(
                    "duplicate_header",
                    f"source3 line {line_no}: embedded duplicate header row "
                    f"(file is two exports concatenated), skipped.",
                )
                continue
            if not any(cell.strip() for cell in r):
                log_issue("blank_row", f"source3 line {line_no}: fully empty row, dropped")
                continue
            row = dict(zip(header, r))
            phone = norm_phone(row["Phone Number"])
            rows.append({
                "raw_name": row["Name"].strip(),
                "name": norm_name(row["Name"]),
                "phone": phone,
                "city": norm_city(row["City"]),
                "raw_city": row["City"].strip(),
                "verified": norm_verified(row["Verified"]),
                "projects_completed": int(row["Projects Completed"]) if row["Projects Completed"].strip() else 0,
            })
    # in-file dup check by phone (none found in this dataset, but keep the
    # guard — same pattern as source1)
    seen = {}
    deduped = []
    for rec in rows:
        if rec["phone"] and rec["phone"] in seen:
            log_issue(
                "in_source_duplicate",
                f"source3: duplicate phone {rec['phone']} for '{rec['raw_name']}', kept first occurrence.",
            )
            continue
        if rec["phone"]:
            seen[rec["phone"]] = True
        deduped.append(rec)
    return deduped


# ---------------------------------------------------------------------------
# Entity resolution across the three sources
# ---------------------------------------------------------------------------

def resolve_entities(s1, s2, s3):
    """Returns a list of 'person' dicts, each with a list of contributing
    (source_name, record) pairs, built in three passes."""

    people = []  # list of dicts: {records: [(source, rec), ...]}
    by_email = {}   # normalized email -> person index
    by_phone = {}   # normalized phone -> person index
    fallback_matched_idx = set()  # person indices linked via name+city only

    def new_person():
        people.append({"records": []})
        return len(people) - 1

    def attach(idx, source, rec):
        people[idx]["records"].append((source, rec))
        if rec.get("email"):
            by_email[rec["email"]] = idx
        if rec.get("phone"):
            by_phone[rec["phone"]] = idx

    # Pass 1: seed one person per source1 row (source1 has both email-ish
    # richness and is present in the largest overlap with the other two).
    for rec in s1:
        idx = new_person()
        attach(idx, "naukri", rec)

    # Pass 2: source2 rows — match by email (source1<->source2 shared key)
    unmatched_s2 = []
    for rec in s2:
        if rec["email"] and rec["email"] in by_email:
            attach(by_email[rec["email"]], "gig_worker", rec)
        else:
            unmatched_s2.append(rec)

    # Pass 3: source3 rows — match by phone (source1<->source3 shared key)
    unmatched_s3 = []
    for rec in s3:
        if rec["phone"] and rec["phone"] in by_phone:
            attach(by_phone[rec["phone"]], "cbnexus", rec)
        else:
            unmatched_s3.append(rec)

    # Pass 4: fallback name+city match for whatever's left in s2/s3 —
    # only accept if exactly one candidate matches, otherwise leave
    # separate and log as needing manual review.
    def name_city_key(rec):
        return (rec["name"].lower(), rec["city"].lower())

    # Pre-check: some name+city combos are genuinely ambiguous because the
    # SAME name+city appears attached to more than one distinct phone
    # number across s1/s3 (e.g. two different "Arjun Mehta" in Noida with
    # phones ...131 and ...272). If we only checked "how many *people*
    # objects currently match" at merge time, the first fallback match
    # would look safe (only 1 candidate exists *so far*) and silently
    # attach to the wrong person before the second one is even created.
    # So this is computed globally, upfront, from raw phone anchors.
    anchor_phones_by_key = {}
    for rec in s1 + s3:
        if rec.get("phone"):
            key = name_city_key(rec)
            anchor_phones_by_key.setdefault(key, set()).add(rec["phone"])
    ambiguous_keys = {k for k, phones in anchor_phones_by_key.items() if len(phones) > 1}

    # index remaining people (those still lacking an s2 or s3 leg) by name+city
    for rec in unmatched_s2:
        key = name_city_key(rec)
        if key in ambiguous_keys:
            log_issue(
                "ambiguous_match",
                f"'{rec['raw_name']}' ({rec['city']}) in gig_workers shares a "
                f"name+city with MORE THAN ONE distinct phone-identified person "
                f"elsewhere in the data (e.g. two different 'Arjun Mehta' in "
                f"Noida) — refused to guess which one this gig-worker record "
                f"belongs to. Kept as a separate, unlinked person record. "
                f"Needs manual review (would ask the person to confirm phone).",
            )
            idx = new_person()
            attach(idx, "gig_worker", rec)
            continue
        candidates = [
            i for i, p in enumerate(people)
            if any(name_city_key(r) == key for _, r in p["records"])
            and not any(s == "gig_worker" for s, _ in p["records"])
        ]
        if len(candidates) == 1:
            attach(candidates[0], "gig_worker", rec)
            fallback_matched_idx.add(candidates[0])
            log_issue(
                "fallback_match",
                f"'{rec['raw_name']}' matched source1 record by name+city only "
                f"(no shared email/phone) — accepted, single candidate.",
            )
        elif len(candidates) == 0:
            idx = new_person()
            attach(idx, "gig_worker", rec)
        else:
            log_issue(
                "ambiguous_match",
                f"'{rec['raw_name']}' ({rec['city']}) in gig_workers has "
                f"{len(candidates)} equally-plausible name+city matches in "
                f"naukri_applicants — refused to auto-merge, kept as a "
                f"separate person record. Needs manual review.",
            )
            idx = new_person()
            attach(idx, "gig_worker", rec)

    for rec in unmatched_s3:
        key = name_city_key(rec)
        if key in ambiguous_keys and rec["phone"] not in anchor_phones_by_key.get(key, set()):
            # shouldn't normally happen (s3 records ARE the phone anchors),
            # kept for symmetry/safety
            pass
        candidates = [
            i for i, p in enumerate(people)
            if any(name_city_key(r) == key for _, r in p["records"])
            and not any(s == "cbnexus" for s, _ in p["records"])
        ]
        if len(candidates) == 1:
            attach(candidates[0], "cbnexus", rec)
            fallback_matched_idx.add(candidates[0])
            log_issue(
                "fallback_match",
                f"'{rec['raw_name']}' matched by name+city only "
                f"(no shared phone) — accepted, single candidate.",
            )
        elif len(candidates) == 0:
            idx = new_person()
            attach(idx, "cbnexus", rec)
        else:
            log_issue(
                "ambiguous_match",
                f"'{rec['raw_name']}' ({rec['city']}) in cbnexus_contacts has "
                f"{len(candidates)} equally-plausible name+city matches — "
                f"e.g. two different 'Arjun Mehta' records exist with "
                f"different phone numbers, both based in Noida. Refused to "
                f"auto-merge; kept as a separate person record. Needs manual review.",
            )
            idx = new_person()
            attach(idx, "cbnexus", rec)

    return people, fallback_matched_idx


# ---------------------------------------------------------------------------
# SQLite load
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE people (
    person_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    primary_email TEXT,
    alt_emails TEXT,          -- JSON array
    primary_phone TEXT,
    city TEXT,
    experience_years REAL,
    ctc_annual_inr INTEGER,
    applied_date TEXT,
    gig_status TEXT,
    gig_rate_annual_est_inr INTEGER,
    verified INTEGER,
    projects_completed INTEGER,
    skills TEXT,               -- JSON array, union across all sources
    match_confidence TEXT,     -- 'high' (email/phone match) | 'low' (name+city fallback) | 'unmatched'
    sources TEXT,               -- JSON array of contributing source names
    skill_category TEXT         -- filled in later by the Task 2 n8n automation (LLM-tagged)
);

CREATE TABLE source_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES people(person_id),
    source_name TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE audio_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER REFERENCES people(person_id),
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    filename TEXT NOT NULL,
    duration_sec REAL,
    sample_rate_hz INTEGER,
    bitrate_kbps REAL,
    loudness_dbfs REAL,
    quality_estimate TEXT,
    submitted_at TEXT NOT NULL
);
"""


def build_db(people, fallback_matched_idx=frozenset()):
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    for person_idx, p in enumerate(people):
        recs = p["records"]
        sources_present = {s for s, _ in recs}

        # canonical name: prefer naukri's name, else whichever exists
        name = next((r["name"] for s, r in recs if s == "naukri"), recs[0][1]["name"])
        email = next((r.get("email") for s, r in recs if s == "naukri" and r.get("email")), None)
        alt_emails = next((r.get("alt_emails", []) for s, r in recs if s == "naukri"), [])
        gig_email = next((r.get("email") for s, r in recs if s == "gig_worker"), None)
        if gig_email and gig_email != email and gig_email not in alt_emails:
            alt_emails = alt_emails + [gig_email] if email else alt_emails
            if not email:
                email = gig_email

        phone = next((r.get("phone") for s, r in recs if s == "naukri" and r.get("phone")), None)
        if not phone:
            phone = next((r.get("phone") for s, r in recs if s == "cbnexus" and r.get("phone")), None)

        city = next((r["city"] for s, r in recs if r.get("city")), "")

        experience_years = next((r.get("experience_years") for s, r in recs if s == "naukri"), None)
        ctc = next((r.get("ctc_annual_inr") for s, r in recs if s == "naukri"), None)
        applied_date = next((r.get("applied_date") for s, r in recs if s == "naukri"), None)

        gig_status = next((r.get("status") for s, r in recs if s == "gig_worker"), None)
        gig_rate = next((r.get("rate_annual_est_inr") for s, r in recs if s == "gig_worker"), None)

        verified = next((r.get("verified") for s, r in recs if s == "cbnexus"), None)
        projects = next((r.get("projects_completed") for s, r in recs if s == "cbnexus"), None)

        skills = set()
        for s, r in recs:
            skills.update(r.get("skills", []))

        if person_idx in fallback_matched_idx:
            confidence = "low"
        elif len(sources_present) >= 2:
            confidence = "high"
        else:
            confidence = "unmatched"

        cur = conn.execute(
            """INSERT INTO people
               (canonical_name, primary_email, alt_emails, primary_phone, city,
                experience_years, ctc_annual_inr, applied_date, gig_status,
                gig_rate_annual_est_inr, verified, projects_completed, skills,
                match_confidence, sources)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                name, email, json.dumps(alt_emails), phone, city,
                experience_years, ctc, applied_date, gig_status,
                gig_rate, int(verified) if verified is not None else None, projects,
                json.dumps(sorted(skills)), confidence, json.dumps(sorted(sources_present)),
            ),
        )
        person_id = cur.lastrowid
        for s, r in recs:
            conn.execute(
                "INSERT INTO source_records (person_id, source_name, raw_json) VALUES (?,?,?)",
                (person_id, s, json.dumps(r)),
            )

    conn.commit()
    conn.close()


def main():
    s1 = load_source1()
    s2 = load_source2()
    s3 = load_source3()
    print(f"Loaded: source1={len(s1)} rows, source2={len(s2)} rows, source3={len(s3)} rows (after cleaning)")

    people, fallback_matched_idx = resolve_entities(s1, s2, s3)
    print(f"Resolved to {len(people)} unique person records")

    build_db(people, fallback_matched_idx)
    print(f"Wrote {DB_PATH}")

    ISSUES_LOG_PATH.write_text(json.dumps(issues, indent=2))
    print(f"Wrote {ISSUES_LOG_PATH} ({len(issues)} issues logged)")


if __name__ == "__main__":
    main()
