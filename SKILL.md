---
name: snapchat-memories-saver
description: Audit already-extracted Snapchat Memories backups locally.
version: 0.1.0
author: Andrew J. Harris, Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [snapchat, memories, backup, audit, privacy]
    related_skills: []
---

# Snapchat Memories Saver

Audit a user's own already-extracted Snapchat My Data exports without changing or reorganizing them. Version 0.1 is intentionally narrow: the user signs in, requests and downloads the export in a browser, then extracts it with macOS Archive Utility or Finder.

Run the Python 3.9+ commands from the installed skill/repository root. The output parent should use a normal macOS APFS or HFS+ volume.

Implementation: [`scripts/audit_export.py`](scripts/audit_export.py). Observed export-layout notes: [`references/export-format.md`](references/export-format.md).

## Do not use this skill to

- Sign in to Snapchat, handle credentials, or bypass verification.
- Accept download URLs or fetch export data.
- Open or extract ZIP files.
- Copy, hardlink, move, delete, rename, or organize media.
- Audit another person's data.
- Reuse or overwrite an earlier report directory.

## Procedure

1. Ask the user to download their own official My Data ZIP parts in their browser.
2. Ask the user to extract each ZIP with macOS Archive Utility or Finder. Keep every part; only one part needs to contain `memories_history.json`.
3. Choose one new private report path. Its parent must already exist, and the report path must not exist or be inside an input export.
4. Run:

   ```bash
   python3 scripts/audit_export.py \
     --export <extracted-export-1> \
     --export <extracted-export-2> \
     --output <new-report-directory>
   ```

5. Pass every extracted part, including later retry-export parts, as a separate `--export` argument. Omit location history by default. Add `--include-location` only after the user explicitly requests location values in the report.
6. Interpret exit status `0` as complete reconciliation, `3` as a completed but incomplete audit, and `2` as a controlled rejection.
7. Read `summary.json` and `report.txt`. Do not claim full backup completeness when status is `incomplete`.

## Safety model

- Inputs are opened read-only, component by component, without following symlinks.
- The walker rejects symlinks and all non-regular/non-directory entries, including sockets, devices, and FIFOs.
- A global entry-count limit across all supplied parts, plus path-depth, per-file metadata, total metadata, total media-byte, history-record, and matching-edge limits, bounds work.
- Media is SHA-256 hashed in chunks. Complete media files are not buffered in memory.
- The input tree is never written.
- The output is a single new mode-`0700` directory. Its four report files are built in a private staging directory and atomically published together; they are never replacements.
- Output is created only after input scanning, reconciliation, report construction, and privacy validation succeed.

## Reconciliation model

Histories are merged with multiset-maximum semantics per allowed `(Date, Media Type, Location)` tuple. If one export contains two identical rows and a retry contains the same two rows, the merged history contains two, not four. If a retry adds a third identical row, the merged count becomes three.

Observed media names are normalized with Unicode NFC plus case folding for conflict detection. Exact same-spelling files with the same SHA-256 and byte count are one logical file. Normalized spelling aliases or conflicting bytes fail closed.

Main files are matched to history by **media type and timestamp only**. The matcher infers an integer-hour timestamp offset, builds bounded timestamp candidates once, and uses deterministic iterative maximum-cardinality matching. Do not claim that the UUID-like filename component is a Snapchat history identifier.

## Report privacy

The only input references written are generated labels such as `export-1/memories/file.jpg`. Never add source root names or absolute paths to reports.

History-derived fields are restricted to:

- `history_index`
- `Date`
- `Media Type`
- `reason`
- `Location` only with `--include-location`

Arbitrary history keys are discarded. Report payloads are recursively checked for URL-like values and absolute paths, unsafe control text is rejected, and formula-leading CSV cells are neutralized before publication.

## Completion standard

A `complete` audit means every merged history row matched one main file, every main file matched one history row, media files were nonempty, at least one Memories HTML file was found, and HTML missing/unexpected/unreferenced counters are zero.

This is evidence reconciliation, not media decoding or proof of Snapchat-side completeness. SHA-256 and nonempty checks do not prove every image pixel or video frame decodes. ZIP integrity is not tested because the tool never opens ZIPs.

## Validation

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py tests/*.py
python3 tests/run_fixture.py
```

Use only synthetic fixtures in tests and issue reports.
