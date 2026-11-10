# Dropbox recordings larger than 7 GB - recovery runbook

**Automatic operation:** New videos over 7 GB are handled by the unified
three-hour GitHub Actions workflow. See
[Automatic incremental imports](dropbox-incremental-3h.md).

**Manual recovery:** `.github/workflows/dropbox-over7-manual.yml` remains
available for isolated one-file operations on `gamliel`, `leil-shishi`,
or `short-word`.

1. In GitHub Actions open **Dropbox OVER 7 GB one-file import** and select a
   source with `mode=plan` (default). This lists source candidates without
   accessing Google Drive or downloading media.
2. To import a single pending file, choose `mode=import` and enter the exact
   confirmation `IMPORT_OVER_7_GB`.
3. Check the logs: `OVERSIZE COMPLETE` means the original, audio if present,
   poster, complete HLS quality ladder and private source map were saved.
   `OVERSIZE PAUSED` means intermediate progress is available for the next
   attempt. The original frozen 2,301 IDs are never eligible again.
4. Only operate on the TEST/private Drive area. The workflow never publishes
   to the live IST website.

## Safeguards

- A source-specific lock prevents simultaneous writes with the routine
  three-hour checks and the smaller-file importers.
- No fixed upper size limit: available local runner disk, Drive quota and
  the GitHub 350-minute runner limit are checked before upload/conversion.
  Exceptional files may require a larger runner.
- A checkpoint is written after the original, MP3, poster and **each completed
  rendition** in the private processed-media folder. The full private source
  map is written only after all expected assets are complete.
- On a retry, saved completed renditions are verified in Drive and skipped.
  An interrupted individual FFmpeg rendition must be encoded again; encoding
  cannot resume part-way through that rendition.
- Completed imported files are identified by their stable Dropbox IDs,
  regardless of revision. The initial 2,301 IDs are also stored in a
  separate frozen GitHub manifest and remain excluded even if private Drive
  content or maps are deleted.
