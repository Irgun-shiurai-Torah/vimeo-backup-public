# Dropbox imports: staging and publication policy

This pipeline copies media **from approved Dropbox shared links into private Google Drive storage**. Source links are stored in the GitHub Actions secret `DROPBOX_SOURCES_JSON`, never in repository files. Existing source files in the owner's Dropbox are never edited or deleted.

## First-sample verification

One test recording from each of these five shared-folder collections is permitted in private Drive staging:

1. Reb Gamliel Rabinowitz — first sample imported before this change
2. Reb Nachum Cohen — one private sample
3. Reb Yitzchok Ahron Goldhirsch — one private sample
4. Reb Mielech — Leil Shishi — one private sample
5. Reb Mielech — Short Word for Shabbos — one private sample

Each is converted into an original copy, an MP3 audio extraction where available, a poster and HLS renditions. None of the four additional samples is automatically approved or published.

## Recurring import scope

**Until the content owner confirms which collections to publish, only the two Reb Meilech Biderman collections receive further automatic imports.** The recurring workflow is `.github/workflows/dropbox-biderman-private.yml`.

Safety limits:

- Two source labels are hardcoded; other speakers are not included.
- Imports run every four hours, handling at most one pending file per Biderman collection each run.
- Source files over 750 MB are deferred for manual/high-capacity processing.
- The backup Drive account must retain at least 50 GiB of free space *plus* a four-times-original-size allowance for each import.
- A shared concurrency group serializes all Dropbox imports so private map writes do not overlap.
- Every import is tracked by Dropbox source and file ID/revision to avoid reprocessing unchanged recordings.
- Imported entries are **unapproved by default**, even for Biderman. No bulk automatic public publishing is enabled.
- The original five Dropbox links can remain stored in the private GitHub secret. The obsolete `DROPBOX_SHARED_LINK` is not required.

## Website publication

The Series catalog accepts only an individual recording with `approved: true` and reviewed speaker, title, Hebrew year and complete media assets. The existing test-site record for Reb Gamliel was approved explicitly. **No new speaker's recordings appear on the public website through this scheduled importer**.

After the family decides whether all five collections can be published, review the permissions and metadata before enabling other sources. Do not add the other speaker labels to the recurring job solely because they are present in the secret.
