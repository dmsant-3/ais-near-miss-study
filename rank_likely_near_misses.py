#!/usr/bin/env python3
"""Rank AIS encounter evidence using a transparent likely-near-miss heuristic.

Input is the evidence-review CSV produced by confirm_near_misses.py. The
heuristic prioritizes cases for human review; it does not confirm near misses.
"""
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
import re

NM_M = 1852.0


def key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def number(row: dict[str, str], fields: dict[str, str], name: str) -> float | None:
    value = row.get(fields.get(key(name), ""), "")
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def truth(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes", "y"}


def score_row(row: dict[str, str], fields: dict[str, str]) -> dict[str, object]:
    """Return score, component evidence, and heuristic class for one event."""
    observed = number(row, fields, "observed_min_separation_in_window_m")
    domain = number(row, fields, "distance_threshold_used_m")
    dcpa = number(row, fields, "minimum_dcpa_m")
    tcpa = number(row, fields, "tcpa_at_minimum_dcpa_s")
    course_a = number(row, fields, "max_course_change_a_deg")
    course_b = number(row, fields, "max_course_change_b_deg")
    speed_a = number(row, fields, "max_speed_drop_a_knots")
    speed_b = number(row, fields, "max_speed_drop_b_knots")
    maneuver = any(truth(row.get(fields.get(key(n), ""), "")) for n in
                   ("maneuver_evidence_a", "maneuver_evidence_b"))
    # Also support older evidence files where only the measurements are present.
    maneuver = maneuver or any(x is not None and x >= 15.0 for x in (course_a, course_b))
    baseline = number(row, fields, "synchronized_baseline_samples")
    encounter = number(row, fields, "synchronized_encounter_samples")
    coverage = (baseline is not None and encounter is not None and
                baseline >= 3 and encounter >= 3)

    points = 0
    reasons = []
    if observed is not None and domain is not None and observed <= domain:
        points += 2
        reasons.append("observed separation within size-adjusted screening domain")
    elif observed is not None and observed <= 0.25 * NM_M:
        points += 1
        reasons.append("observed separation within 0.25 nautical mile")

    if dcpa is not None and dcpa <= 0.1 * NM_M:
        points += 2
        reasons.append("predicted DCPA within 0.1 nautical mile")
    elif dcpa is not None and dcpa <= 0.25 * NM_M:
        points += 1
        reasons.append("predicted DCPA within 0.25 nautical mile")

    if tcpa is not None and 0 < tcpa <= 360:
        points += 1
        reasons.append("positive TCPA within six minutes")
    if maneuver:
        points += 2
        reasons.append("course or speed change consistent with avoidance")
    close = observed is not None and domain is not None and observed <= domain
    imminent = dcpa is not None and dcpa <= 0.25 * NM_M and tcpa is not None and 0 < tcpa <= 360
    course_difference = number(row, fields, "baseline_course_difference_deg")
    if course_difference is not None and course_difference > 20:
        points += 1
        reasons.append("pre-encounter courses differ by more than 20 degrees")
    if any(truth(row.get(fields.get(field, ''), ''))
           for field in ('involvesexcludedvessel', 'involvestugboat')):
        label = 'excluded_barge_or_tug'
    elif not coverage:
        label = "excluded_insufficient_track_coverage"
    elif points >= 8 and close and imminent and maneuver:
        label = "likely_near_miss_for_human_review"
    elif points >= 5:
        label = "elevated_priority_candidate"
    else:
        label = "lower_priority_candidate"
    return {
        "likely_near_miss_score_8": points,
        "likely_near_miss_classification": label,
        "likely_near_miss_evidence": "; ".join(reasons),
        "likely_near_miss_human_review": "required" if label == "likely_near_miss_for_human_review" else "recommended",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="Evidence-review CSV from confirm_near_misses.py")
    parser.add_argument("--output", type=Path, required=True,
                        help="New scored CSV; existing files are not overwritten")
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(f"Refusing to overwrite {args.output}")
    with args.input.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        original_fields = reader.fieldnames or []
        fields = {key(name): name for name in original_fields}
        required = ("observed_min_separation_in_window_m", "minimum_dcpa_m",
                    "tcpa_at_minimum_dcpa_s", "synchronized_baseline_samples",
                    "synchronized_encounter_samples")
        missing = [name for name in required if key(name) not in fields]
        if missing:
            raise ValueError("Input is missing required evidence columns: " + ", ".join(missing))
        rows = list(reader)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    added = ["likely_near_miss_score_8", "likely_near_miss_classification",
             "likely_near_miss_evidence", "likely_near_miss_human_review"]
    counts: dict[str, int] = {}
    with args.output.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=original_fields + added, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            result = score_row(row, fields)
            counts[result["likely_near_miss_classification"]] = counts.get(result["likely_near_miss_classification"], 0) + 1
            writer.writerow({**row, **result})
    print(f"Scored {len(rows):,} events; wrote {args.output.resolve()}")
    print("Classifications: " + ", ".join(f"{label}={count:,}" for label, count in sorted(counts.items())))
    print("Likely labels are heuristic priorities for human review, not confirmed near misses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

