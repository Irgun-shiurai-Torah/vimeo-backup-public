# Vimeo Backup

Public runner code for the Irgun Shiurai Torah Vimeo backup pipeline.

Automatic schedule:
- Regular Vimeo backup: every 3 hours at minute 17.
- HLS backup: every 3 hours at minute 47, up to 15 pending Vimeo lectures per run.
- After each scheduled HLS backup, process one new or changed MP4 in the
  `Video Shiurim` Drive folder. Its city and year come from its parent folders;
  the job creates HLS, an MP3, and a poster image. Manual HLS runs continue to
  target Vimeo lectures only.

The actual backup media and authoritative maps remain in Google Drive.
