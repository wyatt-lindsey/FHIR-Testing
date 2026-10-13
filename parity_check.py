"""Phase 4, Step 6: check the CDS Hooks service agrees with guideline.py.

For each patient it:
  1. builds the prefetch an EHR would send, by running the service's own
     PREFETCH templates against HAPI (one page each, like a real EHR),
  2. POSTs a patient-view hook with that prefetch to the running service,
  3. turns the cards back into a status (no card = NOT_APPLICABLE,
     "Consider..." = RECOMMEND, "...unclear..." = INSUFFICIENT_DATA),
  4. compares it with guideline.py run directly on the same patient today.

The CLI answer is recomputed rather than read from cohort.csv, because both
sides must use the same evaluation date. cohort.csv and the truth table in
criteria.md are shown alongside for reference.

No fhirServer is sent, so an incomplete prefetch (C1) shows up as a mismatch
instead of being quietly fixed by the fallback fetch. Use --fallback to send it.

Needs HAPI and the service both running. Usage:
    python parity_check.py                      # truth-table patients
    python parity_check.py --all                # every patient in cohort.csv
    python parity_check.py --ids 310873 244324
    python parity_check.py --all --out parity.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

import guideline
from cds_service import PREFETCH, SERVICE_ID

STATUSES = (guideline.RECOMMEND, guideline.NOT_APPLICABLE, guideline.INSUFFICIENT_DATA)
HUMAN = {"recommend": guideline.RECOMMEND, "not applicable": guideline.NOT_APPLICABLE,
         "insufficient data": guideline.INSUFFICIENT_DATA}


def truth_table(path="criteria.md"):
    """{patient_id: status} from the hand-checked table in criteria.md."""
    out, in_table = {}, False
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_table = line.startswith("## Truth table")
            continue
        m = re.match(r"\|\s*(\d+)\s*\|[^|]*\|\s*([^|]+?)\s*\|", line) if in_table else None
        if m and m.group(2).lower() in HUMAN:
            out[m.group(1)] = HUMAN[m.group(2).lower()]
    return out


def cohort(path="cohort.csv"):
    """{patient_id: status} from the last guideline.py --csv run."""
    if not Path(path).exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {r["patient_id"]: r["status"] for r in csv.DictReader(f)}


def build_prefetch(session, base, pid):
    """Run each PREFETCH template against HAPI. Returns (prefetch, paged_keys)."""
    prefetch, paged = {}, []
    for key, template in PREFETCH.items():
        url = f"{base}/{template.replace('{{context.patientId}}', pid)}"
        r = session.get(url, timeout=60)
        r.raise_for_status()
        res = r.json()
        if any(l.get("relation") == "next" for l in res.get("link", [])):
            paged.append(key)  # more pages exist; the service should treat this as incomplete
        prefetch[key] = res
    return prefetch, paged


def status_from_cards(cards):
    if not cards:
        return guideline.NOT_APPLICABLE
    if len(cards) > 1:
        return f"{len(cards)} CARDS?"
    summary = cards[0].get("summary", "")
    if summary.startswith("Consider"):
        return guideline.RECOMMEND
    if "unclear" in summary:
        return guideline.INSUFFICIENT_DATA
    return "UNKNOWN CARD"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Compare cds_service.py with guideline.py")
    ap.add_argument("--base", default="http://localhost:8080/fhir", help="HAPI base URL")
    ap.add_argument("--service", default=f"http://localhost:8000/cds-services/{SERVICE_ID}")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="every patient in cohort.csv")
    group.add_argument("--ids", nargs="+", help="specific patient IDs")
    ap.add_argument("--fallback", action="store_true",
                    help="also send fhirServer, so incomplete prefetch is fetched by the service")
    ap.add_argument("--out", help="write results to this CSV")
    args = ap.parse_args(argv)

    base = args.base.rstrip("/")
    truth, last_run = truth_table(), cohort()
    ids = args.ids or (sorted(last_run) if args.all else sorted(truth))
    if not ids:
        sys.exit("No patients: add rows to the truth table in criteria.md, or use --all / --ids")

    hapi = requests.Session()
    hapi.headers["Accept"] = "application/fhir+json"
    fhir = guideline.Fhir(base)
    today = date.today()

    # Fail early with a clear message if either server is down.
    for name, url in (("HAPI", f"{base}/metadata"), ("CDS service", args.service.rsplit("/", 1)[0])):
        try:
            requests.get(url, timeout=10).raise_for_status()
        except requests.RequestException as e:
            sys.exit(f"Can't reach {name} at {url}: {e}")

    rows = []
    print(f"Comparing {len(ids)} patients as of {today}\n")
    print(f"  {'patient':8}  {'guideline.py':18} {'service':18} {'ms':>5}  note")
    for pid in ids:
        row = {"patient_id": pid, "truth_table": truth.get(pid, ""), "cohort_csv": last_run.get(pid, "")}
        try:
            row["guideline"] = guideline.evaluate(guideline.fetch_patient_data(fhir, pid), today).status
            prefetch, paged = build_prefetch(hapi, base, pid)
            body = {
                "hook": "patient-view",
                "hookInstance": f"parity-{pid}-{int(time.time())}",
                "context": {"userId": "Practitioner/parity-check", "patientId": pid},
                "prefetch": prefetch,
            }
            if args.fallback:
                body["fhirServer"] = base
            start = time.perf_counter()
            r = requests.post(args.service, json=body, timeout=60)
            row["ms"] = round((time.perf_counter() - start) * 1000)
            r.raise_for_status()
            row["service"] = status_from_cards(r.json().get("cards", []))
            row["prefetch_paged"] = ",".join(paged)
        except Exception as e:  # keep going; report the patient as an error
            row.setdefault("guideline", "")
            row["service"] = "ERROR"
            row["note"] = f"{type(e).__name__}: {e}"[:120]
        row["agree"] = row["guideline"] == row["service"]

        notes = [row.get("note", "")]
        if row.get("prefetch_paged"):
            notes.append(f"prefetch has more pages: {row['prefetch_paged']}")
        if row["truth_table"] and row["truth_table"] != row["guideline"]:
            notes.append(f"truth table says {row['truth_table']}")
        if row["cohort_csv"] and row["cohort_csv"] != row["guideline"]:
            notes.append(f"cohort.csv said {row['cohort_csv']} (date or data changed?)")
        row["note"] = "; ".join(n for n in notes if n)
        mark = "  " if row["agree"] else "X "
        print(f"{mark}{pid:8}  {row['guideline']:18} {row['service']:18} {row.get('ms', ''):>5}  {row['note']}")
        rows.append(row)

    mismatches = [r for r in rows if not r["agree"]]
    timings = sorted(r["ms"] for r in rows if "ms" in r)
    print(f"\n== {len(rows) - len(mismatches)} of {len(rows)} agree ==")
    for s in STATUSES:
        print(f"  {s:18} guideline.py {sum(r['guideline'] == s for r in rows):3}   "
              f"service {sum(r['service'] == s for r in rows):3}")
    if timings:
        print(f"  service time: median {timings[len(timings) // 2]} ms, max {timings[-1]} ms")

    if args.out:
        fields = ["patient_id", "guideline", "service", "agree", "truth_table", "cohort_csv",
                  "ms", "prefetch_paged", "note"]
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {args.out}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
