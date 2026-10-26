"""Live, secret-free progress markers for Dropbox Series GitHub Actions runs.

Progress is written after each stage so an interrupted job retains its latest
checkpoint in the runner. Source URLs, passwords and Drive IDs never enter the
report; the GitHub Actions job logs are the live view.
"""
import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ImportProgress:
    def __init__(self, source, batch_limit, directory=None):
        self.started = time.monotonic()
        self.directory = directory if directory is not None else os.getenv("DROPBOX_PROGRESS_DIR", "")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        self.filename = "dropbox-" + digest + ".json"
        self.data = {
            "source": source,
            "status": "running",
            "stage": "Starting source scan",
            "batchLimit": batch_limit,
            "eligibleRemaining": None,
            "oversizedDeferred": 0,
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "currentRecording": None,
            "currentMediaId": None,
            "lastErrorType": None,
        }
        self.update("Starting source scan")

    def update(self, stage, **changes):
        self.data.update(changes)
        self.data["stage"] = stage
        self.data["updatedAt"] = utcnow()
        self.data["elapsedSeconds"] = round(time.monotonic() - self.started)
        current = self.data.get("currentRecording") or "no recording selected"
        print(
            "DROPBOX PROGRESS | {source} | {completed}/{batchLimit} done "
            "({failed} failed) | {stage} | {current}".format(
                **self.data, current=current
            ),
            flush=True,
        )
        if not self.directory:
            return
        directory = Path(self.directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / self.filename
        # Atomic replacement avoids a corrupt report if a job is interrupted.
        fd, temporary = tempfile.mkstemp(prefix=".dropbox-status-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _cell(value):
    return str(value if value is not None else "-").replace("|", "&#124;").replace("\n", " ")


def summary(directory):
    statuses = []
    for path in sorted(Path(directory).glob("dropbox-*.json")) if directory else []:
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("source"), str):
                statuses.append(data)
        except (OSError, ValueError):
            continue
    lines = [
        "## Dropbox import progress",
        "",
        "The job log shows live stage updates. This summary contains the last "
        "checkpoint for each approved source, including interrupted/failed batches.",
        "",
        "| Source | Status | Completed | Failed | Eligible at start | Over 2.2 GB deferred | Last stage |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in statuses:
        lines.append("| " + " | ".join(_cell(value) for value in (
            item.get("source"),
            item.get("status"),
            str(item.get("completed", 0)) + "/" + str(item.get("batchLimit", 0)),
            item.get("failed", 0),
            item.get("eligibleRemaining"),
            item.get("oversizedDeferred", 0),
            item.get("stage"),
        )) + " |")
        if item.get("currentRecording") and item.get("status") != "completed":
            lines.append("")
            lines.append(
                "Last recording for **{}**: \u0060{}\u0060 (updated {}).".format(
                    _cell(item["source"]),
                    _cell(item["currentRecording"]).replace("`", "'"),
                    _cell(item.get("updatedAt")),
                )
            )
    for item in statuses:
        if "oversize2_2To5GB" in item:
            lines.extend([
                "",
                "**{} — remaining size tiers:** 2.2–5 GB: **{}**; "
                "5–7 GB: **{}**; over 7 GB: **{}**; "
                "unable to fit runner disk this batch: **{}**.".format(
                    _cell(item["source"]),
                    item.get("oversize2_2To5GB", 0),
                    item.get("oversize5To7GB", 0),
                    item.get("above7GB", 0),
                    item.get("diskDeferred", 0),
                ),
            ])
    if not statuses:
        lines.append("| No progress report | - | - | - | - | - | Job did not reach the importer |")
    lines.append("")
    result = "\n".join(lines) + "\n"
    print(result, flush=True)
    github_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if github_summary:
        with open(github_summary, "a", encoding="utf-8") as handle:
            handle.write(result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    if args.summary:
        summary(os.getenv("DROPBOX_PROGRESS_DIR", ""))
    else:
        parser.error("Pass --summary to render the import progress table")
