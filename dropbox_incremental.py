#!/usr/bin/env python3
"""Run one safe incremental import pass for one approved Dropbox collection.

The GitHub Actions job holds the source-specific concurrency lock across all
size tiers. Every three hours it checks new recordings, preserving the
permanent 2,301-ID skip history even when Drive media has been deleted.

Each size tier is isolated: a failed tier cannot starve the others. An
interrupted >7 GB recording resumes from its Drive rendition checkpoints at
the next three-hour check. No publishing to the live website is performed.
"""
import argparse
import math
import subprocess
import sys
import time
from pathlib import Path

from dropbox_import_ledger import frozen_initial_ids
from dropbox_over7 import SOURCE_LABELS

ROOT = Path(__file__).resolve().parent
MAX_WORK_MINUTES = 330  # 350-minute GitHub job, with 20 minutes of headroom.


def stage_commands(source_key, oversized_budget_minutes=300):
    """Import one >7GB recording, then modest small/medium batches."""
    if source_key not in SOURCE_LABELS:
        raise ValueError("Unapproved Dropbox source")
    label = SOURCE_LABELS[source_key]
    return [
        ("over-7GB", [sys.executable, "-u", str(ROOT / "dropbox_over7.py"),
                      "--source-key", source_key, "--mode", "import",
                      "--confirm", "IMPORT_OVER_7_GB",
                      "--budget-minutes", str(oversized_budget_minutes),
                      "--max-renditions", "4"], 55),
        ("up-to-2.2GB", [sys.executable, "-u", str(ROOT / "dropbox_import.py"),
                         "--mode", "import", "--source-label", label,
                         "--max-files", "3", "--max-source-bytes", "2200000000",
                         "--reserve-gib", "68"], 30),
        ("2.2-to-7GB", [sys.executable, "-u", str(ROOT / "dropbox_import.py"),
                        "--mode", "import", "--large-mode",
                        "--source-label", label, "--max-files", "2",
                        "--min-source-bytes", "2200000000",
                        "--max-source-bytes", "7000000000",
                        "--reserve-gib", "68"], 40),
    ]


def run(source_key, work_minutes=MAX_WORK_MINUTES):
    if source_key not in SOURCE_LABELS:
        raise ValueError("Source must be gamliel, leil-shishi or short-word")
    if not 70 <= work_minutes <= MAX_WORK_MINUTES:
        raise ValueError("Unsafe GitHub job time budget")
    # Fail closed BEFORE starting any network or media transfers if history
    # is damaged, accidentally deleted or shrunk.
    frozen = frozen_initial_ids()
    print(f"PERMANENT HISTORY | {len(frozen)} original recordings will never be re-imported",
          flush=True)
    started = time.monotonic()
    failed = []
    for stage, command, min_time in stage_commands(source_key):
        left = work_minutes - (time.monotonic() - started) / 60
        if left < min_time:
            print(f"::notice::Deferred {stage}: only {left:.1f} minutes left in "
                  "this run. Next scheduled check will try again.", flush=True)
            continue
        if stage == "over-7GB":
            # A rendition may be paused safely before the next one begins.
            budget = min(300, math.floor(left - 10))
            if budget < 45:
                print("::notice::Not enough time left for an oversized stage", flush=True)
                continue
            at = command.index("--budget-minutes") + 1
            command[at] = str(budget)
        print(f"INCREMENTAL CHECK | {source_key} | {stage} | "
              f"{left:.1f} minutes available", flush=True)
        # Leave five minutes to collect stage results and stop before the
        # GitHub runner's hard timeout. A timed-out stage is retried later.
        max_seconds = max(60, math.floor((left - 5) * 60))
        try:
            result = subprocess.run(command, cwd=str(ROOT), check=False,
                                    timeout=max_seconds)
            if result.returncode:
                failed.append(stage)
                print(f"::error::Incremental {stage} import failed with exit code "
                      f"{result.returncode}; other sizes will still be checked", flush=True)
        except subprocess.TimeoutExpired:
            failed.append(stage)
            print(f"::error::Incremental {stage} timed out. Completed Drive "
                  "checkpoints remain intact; retry at next scheduled check", flush=True)
    if failed:
        raise RuntimeError("Incremental Dropbox importer failures: " + ", ".join(failed))
    print(f"INCREMENTAL CHECK COMPLETE | {source_key} | no failed size tiers", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-key", choices=tuple(SOURCE_LABELS), required=True)
    parser.add_argument("--work-minutes", type=int, default=MAX_WORK_MINUTES)
    args = parser.parse_args()
    run(args.source_key, args.work_minutes)
