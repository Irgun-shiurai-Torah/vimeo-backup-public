"""Private staging rules for Dropbox non-lecture recordings.

A source folder can contain footage from events as well as actual shiurim.
Only explicitly identified non-lecture folders are excluded; exclusions apply
to both download selection and website publication.
"""

EXCLUDED_BY_SPEAKER = {
    "Reb Yitzchok Ahron Goldhirsch": frozenset(
        {"shinyev yurtziet", "shinyev yahrzeit"}
    )
}


def is_excluded_nonlecture(source, item):
    label = str(source.get("label") or source.get("sourceLabel") or "").strip()
    excluded = EXCLUDED_BY_SPEAKER.get(label)
    if not excluded:
        return False
    path = str(item.get("path") or item.get("sourcePath") or "").replace("\\", "/")
    first_folder = path.casefold().strip("/").split("/", 1)[0].strip()
    return first_folder in excluded
