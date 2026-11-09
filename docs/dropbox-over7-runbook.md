# Manual-only Dropbox imports for 7-20 GB recordings

This is an **opt-in**, test-site-only extension. It does **not** modify the
existing normal or 2.2-7 GB importer and does **not** schedule imports. No
Dropbox originals are changed or deleted.

## Before any import

1. Merge the reviewed pull request only after CI passes. The workflow has no
   scheduled or push trigger; merging it does **not** start an import.
2. In GitHub Actions, open **Dropbox OVER 7 GB one-file import**. Select a
   source and run with `mode=plan` (the default). Review its exact candidate
   counts and stable IDs; plan mode does not open Drive or upload anything.
3. For an approved import, run one source at a time with `mode=import` and
   `confirm=IMPORT_OVER_7_GB`. The workflow imports **one** pending >7-20 GB
   video per dispatch, processing the smallest video first.
4. Review the logs. `OVERSIZE COMPLETE` means the full ladder was committed to
   the private Drive source map. `OVERSIZE PAUSED` means its Drive progress
   checkpoint is intact and another manual dispatch can resume the same item.
5. Repeat until no eligible files remain. Validate the resulting Test Series
   recordings, playback and extracted audio before enabling public visibility
   anywhere else. Larger than 20 GB files require separate handling.

## Safety and resumption

- Shares the per-source concurrency lock with the regular importers.
- Enforces source allowlist and a strict 7-20 GB size window, before download.
- Requires an exact confirmation phrase for an actual import.
- Uses the existing Drive storage reserve (68 GiB plus 4x media size) and
  conservative local disk preflight before download and each new rendition.
- Saves unchanged originals in private Drive, creates MP3, poster, and the
  same 360/480/720/1080p ladder the video resolution supports.
- Stores `oversize-progress.json` in the individual private processed-media
  folder after **each completed asset or quality**, not in the published map.
  On a resumed run it verifies Drive asset IDs and skips saved qualities.
- Writes a normal source checkpoint only when all expected rendition assets,
  extracted audio (if the source has audio), poster, and HLS master exist.
- If a job is interrupted during an individual FFmpeg pass, previously
  completed qualities are preserved; the interrupted quality must start again.
  If a single quality cannot finish within GitHub's 350-minute job timeout,
  use a longer-running, larger runner or adapt the encoder to chunk that one
  quality. No claim is made that FFmpeg resumes within a partially encoded
  rendition.

**No unreviewed automatic import is enabled by this PR.**
