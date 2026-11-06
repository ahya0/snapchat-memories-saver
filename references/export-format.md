# Observed Snapchat Memories extracted-export format

These notes describe formats observed in user-downloaded Snapchat My Data exports. They are not guarantees from Snap Inc.

## Acquisition boundary

The skill begins with one or more directories that the user already extracted with macOS Archive Utility or Finder. It does not accept URLs, open ZIPs, download data, or extract archives. Because the original ZIP is outside the tool's scope, ZIP integrity is not tested.

A multi-part export may place `memories_history.json` in only one extracted
part. Pass every part as a separate `--export`; media-only parts are valid as
long as at least one supplied part contains a Memories history file.

## Observed files

An extracted export may include:

```text
json/memories_history.json
memories/memories.html
memories/YYYY-MM-DD_<UUID>-main.jpg
memories/YYYY-MM-DD_<UUID>-main.mp4
memories/YYYY-MM-DD_<UUID>-overlay.png
```

The audit accepts the observed UUID-shaped hexadecimal filename component. It uses that component only as part of a filename. It does not claim identifier-based history matching.

Media may be nested elsewhere in an export; discovery is bounded and recursive. Reports replace the real source root with `export-N/` labels.

## History JSON

`memories_history.json` is expected to be a JSON object with a `Saved Media` array of objects. Only these values are read for reconciliation:

- `Date`
- `Media Type`
- `Location`

Unknown keys are not copied. Report history fields are limited to `history_index`, `Date`, `Media Type`, `reason`, and optional `Location` when `--include-location` is used.

### Multiple-export merge

Each history file is treated as a multiset of `(Date, Media Type, Location)` tuples. For each tuple, the merged history keeps the largest occurrence count found in any one history file.

Example:

- Export A contains row X twice.
- Retry export B contains row X twice and row Y once.
- Merged history contains X twice and Y once.

This prevents retries from multiplying repeated rows while preserving genuinely repeated identical rows within an export.

## Media identity and conflicts

Media filename conflict keys use Unicode NFC followed by case folding.

- Exact same filename, byte count, and SHA-256 across exports: one logical media file.
- Same normalized filename with different spelling: rejected.
- Same exact filename with different bytes: rejected.

SHA-256 is streamed from an open regular-file descriptor. Complete media files are not loaded into memory.

## History matching

Only `-main.jpg` (`Image`) and `-main.mp4` (`Video`) participate in history matching. Overlays do not consume history rows.

Matching uses:

1. Parsed UTC history timestamps.
2. Cached filesystem modification timestamps.
3. An inferred integer-hour offset from -14 through +14.
4. A small timestamp tolerance.
5. Deterministic iterative maximum-cardinality bipartite matching.

Filename identifiers are not matching evidence. Invalid dates and unmatched records appear in `missing_history_records.csv` with generic reasons.

## Memories HTML evidence

The audit parses `img`, `video`, and `source` `src` attributes without fetching anything. References are checked against the already-scanned file inventory and logical media names.

It resolves references against the exact relative media path within the same supplied export part and reports separate counters and relative evidence for:

- Missing references: a safe relative reference has no corresponding exported file/media.
- Unexpected references: an unsafe, URL-like, traversal, or existing non-media reference.
- Unreferenced media: discovered media not referenced by any Memories HTML document.

No raw unsafe reference value is written to the report.

## Filesystem and resource boundaries

The walker rejects:

- Symlinks, including input/output path ancestors.
- Sockets, devices, FIFOs, and other special files.
- NUL, dot, dot-dot, excessive-depth, and normalized-alias paths.
- Excessive entry counts.
- Oversized history JSON or Memories HTML files.

Defaults are 20,000 entries across all supplied parts, depth 32, 16 MiB per history JSON file, 16 MiB per Memories HTML file, 64 MiB total metadata, 500 GiB total media bytes, 20,000 history records, and 2,000,000 candidate matching edges.

CSV output neutralizes cells whose first non-whitespace character is `=`, `+`, `-`, or `@`. Unsafe control and bidirectional-override characters are rejected from input path components and allowed history scalars.

## Output interpretation

A complete report means the supplied extracted directories reconcile internally under these rules. It does not prove that Snapchat included every Memory, that the original ZIP was intact, or that every image pixel/video frame decodes. Hashes and nonempty checks are byte-level evidence only.

The private report directory contains six files:

- `start_here.html`: a simple success or recovery page. For incomplete exports it lists dates/media types for unmatched history rows, shows counts for other gaps, and links only to Snapchat's official export and help pages.
- `summary.json`: machine-readable counts and status.
- `manifest.csv`: the local media inventory and matches.
- `missing_history_records.csv`: history entries without matching media.
- `report.txt`: a concise text summary.
- `support.txt`: tool version and aggregate counts without paths, filenames, hashes, Memory timestamps, locations, or media.

The recovery page does not retain per-Memory download URLs. Export links may be temporary and private. Retry exports should be supplied together with the original parts; repeated identical files are counted once.

Before scanning, the tool checks a fixed free-space reserve on the report destination filesystem. After building and privacy-validating all encoded payloads in memory, it checks their block-rounded size plus a reserve again before creating the private staging directory. These checks cannot reserve disk space, so ENOSPC and other write failures still abort publication and clean the unpublished stage.

Output containment is verified with directory device/inode identities, not path spelling alone, so case-only or Unicode-normalization aliases on macOS cannot place a report inside an export. The report parent and its parent must be system-owned and have no extended ACL. The parent of the report parent must also be non-writable by the current user. The guided launcher therefore publishes directly under `/Users/Shared`; home-folder and nested output parents are rejected. These checks close ordinary mode-bit and ACL-based same-user relocation of the output parent. Metadata and media reads are bounded to their accepted sizes and compare device, inode, link count, size, modification time, and change time before and after reading. Changes detected during reading or final revalidation are rejected. The report describes the evidence as read during the audit. It is point-in-time validation, not an atomic filesystem snapshot, so selected exports must not be modified concurrently.

The CLI uses stable privacy-safe statuses and `SMS-E-*` codes: `0` complete, `3` incomplete, `64` usage, `65` rejected input, `73` unavailable output, `74` local I/O failure, and `70` contained internal failure. The guided launcher also returns `130` for user cancellation without creating a report. The `.command` wrapper returns `69` when Python 3.9 or newer is unavailable. Argument parser errors and exceptions never echo the supplied value or underlying exception text.

Input regular files with more than one hard link are rejected so a selected export cannot silently reference bytes outside its root. History records are parsed one at a time and HTML references have a global count limit before report evidence is retained.

The report becomes authoritative at the final exclusive directory rename. All source and destination checks, input revalidation, file synchronization, and parent synchronization happen before that rename. A parent-descriptor close follows publication, but any close error is contained so it cannot turn a successfully published report into a reported failure. Interrupted unpublished cleanup retries once, scrubs staged regular-file bytes before unlinking, and preserves the primary audit failure. Unsupported destination filesystems fail with `SMS-E-OUTPUT` before a report is published.
