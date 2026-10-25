"""Public Series and Dropbox import allowlist.

Only these two speakers are approved for the TEST Series page and the
recurring Dropbox import. This does not delete files, change the regular
Shiurim library, or modify any Dropbox sharing permissions.
"""
ALLOWED_SERIES_SPEAKERS = frozenset((
    "Reb Gamliel Rabinowitz",
    "Rabbi Meilech Biderman",
))
ALLOWED_DROPBOX_SOURCES = frozenset((
    "Reb Gamliel Rabinowitz",
    "Reb Mielech - Leil Shishi",
    "Reb Mielech - Short Word for Shabbos",
))


def permitted_series_speaker(name):
    return isinstance(name, str) and name in ALLOWED_SERIES_SPEAKERS


def filter_series_maps(public, private):
    """Filter both public and private Series maps so forbidden IDs can't play.

    Do not retain extra speakers from pre-existing folder listings, and keep
    protected Drive media IDs only for visible speakers in the private map.
    """
    pub = {**public, "items": [
        item for item in public.get("items", [])
        if permitted_series_speaker(item.get("speaker"))
    ]}
    priv = {**private, "items": [
        item for item in private.get("items", [])
        if permitted_series_speaker(item.get("speaker"))
    ]}
    pub["speakers"] = sorted(
        {item["speaker"] for item in pub["items"]}, key=str.casefold
    )
    pub["speakerPhotos"] = [
        name for name in public.get("speakerPhotos", [])
        if permitted_series_speaker(name)
    ]
    if isinstance(private.get("speakerPhotos"), dict):
        priv["speakerPhotos"] = {
            name: data for name, data in private["speakerPhotos"].items()
            if permitted_series_speaker(name)
        }
    return pub, priv
