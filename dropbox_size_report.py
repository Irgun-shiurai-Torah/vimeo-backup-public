"""Read-only 2.2 / 5 / 7 GB Dropbox inventory for approved TEST Series sources."""
import os

from dropbox_import import DropboxLinks, sources_from_env
from dropbox_large import size_buckets
from dropbox_media_policy import is_excluded_nonlecture
from series_visibility import ALLOWED_DROPBOX_SOURCES


def format_size_table(sources, list_tree):
    totals = {name: 0 for name in (
        "normalUpTo2_2GB", "oversize2_2To5GB", "oversize5To7GB",
        "above5GB", "above7GB"
    )}
    lines = [
        "## Dropbox recording sizes (read-only)",
        "",
        "These are file-size counts from the three approved shared folders; "
        "not a count of unique shiurim. Files already imported are also included.",
        "",
        "| Collection | Up to 2.2 GB | 2.2–5 GB | 5–7 GB | Over 7 GB |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for source in sources:
        if source["label"] not in ALLOWED_DROPBOX_SOURCES:
            raise ValueError("Refusing to inspect an unapproved Dropbox source")
        _, files = list_tree(source)
        allowed = [item for item in files if not is_excluded_nonlecture(source, item)]
        c = size_buckets([(source, item) for item in allowed])
        for field in totals:
            totals[field] += c[field]
        lines.append("| " + " | ".join((
            source["label"],
            str(c["normalUpTo2_2GB"]),
            str(c["oversize2_2To5GB"]),
            str(c["oversize5To7GB"]),
            str(c["above7GB"]),
        )) + " |")
    lines.append("| **TOTAL** | " + " | ".join(
        "**" + str(totals[name]) + "**" for name in
        ("normalUpTo2_2GB", "oversize2_2To5GB", "oversize5To7GB", "above7GB")
    ) + " |")
    lines.extend([
        "",
        f"**Above 5 GB:** {totals['above5GB']} recordings. "
        f"**Above 7 GB:** {totals['above7GB']} recordings.",
        "",
        "Only files above 2.2 GB and no larger than the configured 5 or 7 GB "
        "maximum are candidates for the separate large-file importer. "
        "Files without enough temporary disk space are deferred, not forced.",
        "",
    ])
    return "\n".join(lines)


def main():
    sources = [source for source in sources_from_env()
               if source["label"] in ALLOWED_DROPBOX_SOURCES]
    if len(sources) != 3 or len({s["label"] for s in sources}) != 3:
        raise ValueError("Expected exactly three approved Dropbox Series collections")
    dbx = DropboxLinks(os.environ.get("DROPBOX_APP_KEY"), os.environ.get("DROPBOX_APP_SECRET"))
    message = format_size_table(sources, dbx.list_tree)
    print(message, flush=True)
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(message + "\n")


if __name__ == "__main__":
    main()
