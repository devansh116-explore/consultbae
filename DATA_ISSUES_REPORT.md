# Data Issues Report

Generated from `data_issues_log.json` (produced automatically by `db/merge_pipeline.py`
on each run) plus manual review of the three source files. Every issue below was
handled in code, not by hand-editing the CSVs — re-running the pipeline reproduces
the same result.

## 1. Structural / file-format issues

| # | Issue | Where | How handled |
|---|-------|-------|-------------|
| 1 | Fully blank row (`,,,,,`) | `source2_gig_workers.csv`, line 12 | Detected and dropped (all cells empty) |
| 2 | Column-shifted junk row — the row `"react, javascript, mysql",ISHA.CHOPRA95@...,Isha Chopra,1406/hr,Pune,active` has its 6 values rotated one column to the left (skills where email should be) | `source2`, line 20 | Detected because the `email_id` field had no `@` in it, and dropped. A correctly-formed row for the same person (Isha Chopra) already exists earlier in the file, so nothing was lost. |
| 3 | Embedded duplicate header row — the file is two separate exports concatenated, so the header line (`Name,Phone Number,City,Verified,Projects Completed`) appears again mid-file as if it were a data row | `source3_cbnexus_contacts.csv`, line 16 | Detected (row equals header) and skipped, rather than being ingested as a fake "person" named "Name" |

## 2. Field-format inconsistencies (same field, different formats across/within files)

| # | Issue | Example | How handled |
|---|-------|---------|-------------|
| 4 | Phone numbers in at least 4 different formats: bare 10-digit, `+91` prefix, `0` prefix, `91` prefix with no separator, `+91-` with a dash | `9000000237`, `+919000000254`, `09000000287`, `919000000231`, `+91-9000000131` | Normalized to the last 10 digits everywhere (`normalize_phone`), used as the join key between `source1` and `source3` |
| 5 | Applied Date in 4 different formats | `24-07-2026`, `2026-08-08`, `7 Jul 2026`, `07/13/2026` | Parsed against all 4 known formats, stored as ISO `YYYY-MM-DD` |
| 6 | Current CTC mixes absolute rupees and lakhs with no unit column | `417964` vs `4.2` (both are "current CTC") | Heuristic: any value under 1000 is treated as lakhs and multiplied by 100,000. Flagged in code as an approximation — a real fix would ask the data owner to confirm units rather than infer them. |
| 7 | Gig-worker rate mixes hourly and monthly pay with no unit column | `1415/hr` vs `72k/month` | Both converted to an estimated annual figure (`hourly × 8h × 22 days × 12`, `monthly × 12`) so gig pay is at least comparable to CTC. Documented as an estimate, not an exact figure — hours/days worked isn't in the data. |
| 8 | City names inconsistent: casing, aliasing, and trailing whitespace | `GURGAON` / `Gurgaon` / `gurugram` / `gurugram ` (trailing space) all mean the same city; `Bangalore` vs `Bengaluru`; `New Delhi` / `Delhi NCR` / `Delhi` | Normalized via a case-insensitive alias map + `.strip()` before storage and before being used in name+city fallback matching |
| 9 | `Verified` field uses 3 different boolean spellings | `Y`/`N`, `yes`/`no`, `Yes`/`No` | Normalized to a single boolean |
| 10 | `status` (gig workers) inconsistent casing | `Active`, `active`, `ACTIVE`, `paused` | Title-cased on ingest |
| 11 | Email casing inconsistent (some fully uppercase) | `ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG` vs `isha.chopra95@mailtest.example.org` | Lowercased before being used as a join key — otherwise these two would have failed to match as the same person |
| 12 | Skills / skill_tags casing and spacing inconsistent between files | `"n8n, LangChain, REST APIs"` vs `"n8n, langchain, rest apis"` | Lowercased and trimmed on ingest so the same skill from two sources counts as one skill in the union, not two |

## 3. Entity-resolution issues (the actual hard part of this assignment)

No single ID is shared across all three files. The join keys available are:

- `source1` (naukri) has **both** email and phone
- `source2` (gig_workers) has **only** email
- `source3` (cbnexus) has **only** phone

So `source2` and `source3` can only be linked to each other **transitively**, through a
`source1` record that matches both — there's no direct shared field between them.

| # | Issue | How handled |
|---|-------|-------------|
| 13 | **In-file duplicate**, `source1`: "R. Verma" and "Rohit Verma" — different name spelling, identical email/phone/city/CTC | Same-source dedup keyed on phone before cross-file matching even starts. Kept the fuller name. |
| 14 | **In-file duplicate**, `source1`: `nikhil.chopra70@example.com` and `alt.nikhil.chopra70@example.com` — same person, same phone, an "alt." email variant | Deduped by phone; second email kept as an `alt_emails` field rather than discarded |
| 15 | **Two different real people share the exact same name** — "Arjun Mehta" in Noida. `source1` has one (phone `...131`, email `arjun.mehta9@...`); `source3` independently lists **two** "Arjun Mehta" rows in Noida with two different phone numbers (`...131` and `...272`); `source2` has an `arjun.mehta77@...` gig-worker record with **no phone at all**, so there's no way to tell which of the two real Arjun Mehtas it belongs to. | This is the case worth calling out explicitly: a naive name+city fallback match would have **silently merged the wrong two records together** (confirmed while testing — an early version of the pipeline did exactly this). The fix: before doing any name+city fallback matching, the pipeline first checks whether a name+city combination maps to more than one distinct phone number anywhere in the raw data. If it does, **no fallback match is attempted** — both people are kept as separate, unlinked records, logged as `ambiguous_match`, and flagged for manual review (in practice: call the gig worker and ask for their phone number). Guessing wrong here would silently corrupt someone's CTC/status/verification data with someone else's. |
| 16 | Same name, different person, different city — "Deepak Nair" appears in `source2` twice: once matching `source1`'s Bengaluru-based Deepak Nair by email, and once as a second person in New Delhi with a different email domain and no phone to check against. | Because the city differs, this one *isn't* ambiguous — it's just two different people who happen to share a name, correctly kept as two separate records with no forced match. |
| 17 | 5 people (`Manish Bhatia`, `Divya Chopra`, `Karan Chopra`, `Vikram Mehta`, plus the correctly-matched half of the Arjun Mehta case) exist **only** in `source2` + `source3` — never in `source1`, so there's no email *and* no phone bridge between them. They can only be linked by name+city. | Accepted **only** where exactly one candidate exists for that name+city combination (logged as `fallback_match`, marked `match_confidence = 'low'` in the DB so downstream consumers know it's a weaker match than an email/phone join) |
| 18 | 26 people appear in only one source with nothing to cross-check against (e.g. gig workers like Pooja Reddy, Tanvi Sharma, Nikhil Nair who never applied via Naukri and aren't CBNexus contacts) | Kept as single-source person records, `match_confidence = 'unmatched'`. This is correct behavior, not an error — some people genuinely only exist in one system. |

## Summary

- **100** total input rows across the 3 files → **55** unique people after cleaning + merge
- Match confidence: **25 high** (linked by email or phone), **5 low** (linked by name+city fallback, single candidate only), **25 unmatched** (single source only)
- **1** deliberately-ambiguous case (two different "Arjun Mehta") correctly refused rather than guessed
- Full machine-readable log of every decision: `data_issues_log.json` (regenerated each pipeline run)
- Full audit trail of which raw row(s) fed each person record: `source_records` table in `consultbae.db`
