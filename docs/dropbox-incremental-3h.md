# Automatic incremental Dropbox imports - every three hours

**Destination:** IST Dropbox Imports (Private) on Google Drive for the TEST Series.
**Not published:** this workflow does not modify or publish the live website.

## What runs

GitHub Actions workflow: `.github/workflows/dropbox-incremental-3h.yml`

- Runs at **00:11, 03:11, 06:11, 09:11, 12:11, 15:11, 18:11, 21:11 UTC**.
  GitHub Actions is best-effort and may start later.
- Checks the three explicitly approved collections: Reb Gamliel Rabinowitz,
  Reb Mielech - Leil Shishi, Reb Mielech - Short Word for Shabbos.
- Each collection holds its own concurrency lock for the entire check, so a
  manual importer cannot overwrite that source's private map mid-import.
- A run attempts at most **one video above 7 GB**, then up to **three files
  at or below 2.2 GB**, then up to **two 2.2-7 GB files**, per collection.
  More pending files are picked up on future checks.
- A failing size tier is reported but does not block checking the other
  tiers. Every complete file is checkpointed independently.
- Over-7 GB files are resumable by finished asset and finished quality. A
  single interrupted quality is recomputed on the next scheduled run.

The previous hourly and two-hourly workflows are now manual-only. The
manual over-7 GB workflow remains available for recovery.

## Important: files that must NEVER be re-imported

The **original 2,301 completed recording IDs** are frozen in the GitHub
repository file `data/dropbox-initial-imported-2301.json`.

- This history is independent of the Google Drive files and map.
- Deleting a recording from private Drive **will not cause it to be imported
  again** if its stable Dropbox ID was among those original 2,301.
- Renaming or editing a Dropbox recording with the same Dropbox file ID also
  will **not** re-import it.
- For files imported *after* that frozen baseline, the private per-source
  map records their completion and blocks future imports, including after
  physical Drive media deletion. **Keep the private Maps folder intact.**
- If a recording is copied/re-uploaded into Dropbox with a **new Dropbox
  file ID**, it is treated as a genuinely new recording, even if its contents
  resemble an old video.
- The frozen manifest is checksum-validated. If lost or corrupted, the
  importer refuses to upload rather than accidentally re-import old media.

## File size and resource safety

- Files through 2.2 GB: regular importer.
- Files above 2.2 GB through 7 GB: large importer.
- Video files **above 7 GB**: checkpointed oversized importer.
- No artificial 20 GB cutoff remains. Instead, free local runner disk,
  Drive space, required conversion output room and the GitHub job limit
  determine which huge files can actually complete. If an individual video
  or rendition is too large for the runner, the job reports an error and
  will require a higher-capacity runner or a chunked encoder.
- Existing originals, MP3 extraction, poster and supported HLS playback
  qualities are preserved. The private source map is updated only when a
  recording is complete.

## Verify and troubleshoot

1. Open GitHub Actions and select **Dropbox incremental import every 3 hours
   (TEST Series only)**.
2. Each run has independent jobs for gamliel, leil-shishi and short-word.
   Look for `INCREMENTAL CHECK` and `INCREMENTAL CHECK COMPLETE` lines.
3. Confirm completed records in the private Google Drive source maps, not
   just from the GitHub "success" badge. An idle run succeeds with zero new.
4. If a job fails, inspect its particular size tier. Subsequent checks will
   automatically retry incomplete items. Never remove IDs from the frozen
   2,301-item history to force a re-import.
5. A manual `workflow_dispatch` on the new workflow starts an extra check.
   Avoid triggering two manual checks for the same source concurrently.
