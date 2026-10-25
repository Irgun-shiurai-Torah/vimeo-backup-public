# Dropbox imports and automatic Series publishing (TEST website)

The website owner has requested **automatic publication of lectures from all five approved Dropbox shared folders**. The original Dropbox links are held in the GitHub Actions `DROPBOX_SOURCES_JSON` secret, never committed to a public repository.

## Import workflow

`.github/workflows/dropbox-all-series.yml` runs every hour at 17 minutes past the hour (and can be started manually). Each batch selects at most one pending media file from each of the following sources:

1. Reb Gamliel Rabinowitz
2. Reb Nachum Cohen
3. Reb Yitzchok Ahron Goldhirsch
4. Reb Mielech — Leil Shishi
5. Reb Mielech — Short Word for Shabbos

The import starts with smaller files, serializes private Drive map writes, keeps at least a 50 GiB Drive storage reserve, and defers originals larger than 2.2 GB for high-capacity/manual handling. Original recordings stay in Dropbox and are copied to private Google Drive staging. The system generates HLS, extracted MP3 and a poster; no media is published before required processing completes.

## Automatic website visibility

The hourly test-only Series catalog scanner reads the private import manifest. When a recording comes from one of the five known source labels, all necessary Google Drive media assets exist, and the source is not expressly disabled, it automatically appears on **test.irgunshiuraitorah.com/series**. A separate approval per shiur is no longer required.

Speaker and series labels come from the approved source. The display title comes from the filename or its containing folder. Known Hebrew years are parsed from the title/path; where no Hebrew year is present the year remains unset rather than inventing a value. Parsha and holiday tags follow existing Series classification. A standalone audio file is not shown as a duplicate when the matching video already provides an MP3 version.

Never publish:
- Folders or links that are not among the five approved source labels
- Reb Yitzchok Ahron Goldhirsch’s `Shinyev Yurtziet` event footage, which is not a shiur
- An explicitly disabled record (`approved: false` or `autoPublish: false`)
- Videos lacking finished HLS, audio or poster assets

## Hiding or restoring in Series Admin

Open **test.irgunshiuraitorah.com/series-admin.html**, select a lecture and check **Hide this recording from the Series page**. Saving stores the visibility override under the stable Series ID in the website's Durable Object. This hides it from the Series catalog, including after subsequent Dropbox imports and hourly map refreshes. Uncheck and save to restore it.

Hiding does **not** delete the original Dropbox file, the private Google Drive copy, or existing imported media. Hiding is preferable to deleting source recordings because future imports must not unexpectedly recreate a removed item. Recordings remain available for admin management.

Only the test website is configured with this Dropbox Series pipeline. Do not imply the main live website or native apps have been deployed until they have been separately verified.
