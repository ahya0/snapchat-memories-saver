---
name: snapchat-memories-saver
description: Check that a Snapchat Memories export saved correctly.
version: 0.2.0
author: Andrew J. Harris, Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [snapchat, memories, save, backup, privacy]
    related_skills: []
---

# Snapchat Memories Saver

Help the user preserve an official Snapchat export, then check that its history, media files, and HTML references agree. Version 0.2 checks already-extracted exports and does not create a separate deduplicated media library. The skill cannot detect a Memory that Snapchat omitted from all export evidence. Keep the process simple and never ask for credentials or upload the user's Memories.

## Simple flow

1. Open [Snapchat My Data](https://accounts.snapchat.com/v2/download-my-data). Ask the user to request **Memories** for **all time**.
2. Before extraction, make sure the destination has enough free space for the ZIP files and their extracted contents. The user downloads every ZIP part and extracts each one with Finder or Archive Utility.
3. From the trusted project root (the folder containing `README.md` and `scripts/`), run:

   ```bash
   python3 scripts/audit_export.py \
     --export <extracted-export-1> \
     --export <extracted-export-2> \
     --output "/Users/Shared/<new-report-name>"
   ```

4. Tell the user to open `start_here.html` locally in the new report folder. Do not read or summarize it through the AI by default.

## If Memories are missing

Tell the user clearly. `start_here.html` lists dates and media types for unmatched history rows, shows issue counts for other export gaps, and links to Snapchat's official export and help pages.

Ask the user to request another Memories-only, all-time export. Run the saver again with both the original and retry export folders. Repeated files are counted once.

If a retry still misses an item, use its date to find it in Snapchat and save it manually. Do not copy temporary or signed per-Memory download links into reports.

## Privacy

- Read extracted exports only. Never change, move, delete, or upload them.
- Never handle passwords, verification codes, cookies, or export URLs.
- Omit location by default. Add `--include-location` only when the user asks.
- Use a new report folder for every run.
- Create the report as a direct child of `/Users/Shared`. Home-folder, nested, user-owned, or extended-ACL output parents are rejected because they can be relocated during the audit.
- Tell the user not to move or edit selected exports while the check runs. The report describes the evidence as read during the audit; it is not an atomic filesystem snapshot.
- Keep the original ZIPs, extracted export folders, and report in a permanent private location.
- Keep detailed reports out of AI context by default. Use only the process status, fixed `SMS-E-*` code, or a user-reviewed `support.txt`. Read filenames, dates, hashes, CSV files, `summary.json`, or `start_here.html` only after the user explicitly accepts the model or workspace privacy boundary.

Exit `0` means the supplied export looks complete. Exit `3` means some evidence is missing. Safe failure statuses are `64` for command usage, `65` for rejected input, `73` for unavailable output, `74` for local I/O failure, and `70` for a contained internal error.

The double-click launcher returns `130` when the user cancels a folder chooser. Cancellation is not an audit failure and creates no report.

The `.command` wrapper returns `69` when Python 3.9 or newer is unavailable and shows a local setup message.

Implementation: project-root `scripts/audit_export.py`. Technical details: project-root `references/export-format.md`.
