# Snapchat Memories Saver

**Local backup auditing for already-extracted Snapchat My Data exports.**

Version 0.1 is a non-destructive, macOS-focused Python 3.9+ standard-library tool. You download your own Snapchat My Data ZIPs in your browser and extract them with macOS Archive Utility or Finder. This project only audits one or more extracted export directories.

It does **not** sign in, download, open ZIPs, extract archives, copy or organize media, modify an export, or overwrite an earlier report.

## Install as a Hermes skill

After the public repository is available, inspect the skill before installing it:

```bash
hermes skills inspect \
  https://raw.githubusercontent.com/ahya0/snapchat-memories-saver/main/SKILL.md
```

Then install the reviewed skill and its referenced support files:

```bash
hermes skills install \
  https://raw.githubusercontent.com/ahya0/snapchat-memories-saver/main/SKILL.md
```

You can also clone the repository and run the audit directly. The project has no third-party Python dependencies.

## Usage

From the repository/skill root, choose a new report directory whose parent already exists:

```bash
python3 scripts/audit_export.py \
  --export "$HOME/Downloads/snapchat-export-1" \
  --export "$HOME/Downloads/snapchat-export-2" \
  --output "$HOME/Documents/snapchat-audit-2026-11-02"
```

Pass every extracted ZIP-part directory from the same Snapchat export. Only one
part needs to contain `memories_history.json`; media-only parts are supported.
Pass later retry-export parts in the same command.

Location history is omitted by default. Include it only when you explicitly need it:

```bash
python3 scripts/audit_export.py \
  --export "$HOME/Downloads/snapchat-export" \
  --output "$HOME/Documents/snapchat-audit-with-location" \
  --include-location
```

Every run requires a `--output` path that does not exist. The tool builds all four files in a private staging directory, then publishes the complete mode-`0700` directory atomically. A write failure does not leave an authoritative-looking partial report.

```text
summary.json
manifest.csv
missing_history_records.csv
report.txt
```

The output path may not be inside an input export. Existing paths, symlinks, and symlinked ancestors are rejected. Reports are never replaced.

Example successful summary from the synthetic fixture:

```json
{
  "status": "complete",
  "history_record_count": 1,
  "main_media_file_count": 1,
  "missing_history_record_count": 0,
  "unmatched_main_file_count": 0,
  "html_missing_reference_count": 0,
  "html_unexpected_reference_count": 0,
  "html_unreferenced_media_count": 0
}
```

## What the audit checks

- Walks inputs read-only with descriptor-relative, no-follow filesystem operations.
- Rejects symlinks, sockets, devices, FIFOs, unsafe path components, excessive depth, excessive entry counts, and oversized JSON/HTML metadata.
- Discovers `memories_history.json`, `memories/memories.html`, and observed `YYYY-MM-DD_<UUID>-main.jpg`, `-main.mp4`, and `-overlay.png` media names.
- Streams SHA-256 for media without loading complete media files into memory.
- Merges retry-export history with multiset-maximum semantics: repeated exports do not multiply the same rows, while repeated identical rows within one export remain distinct.
- Treats identical same-name/hash media across exports as one logical file. Conflicting bytes or NFC/case-folded filename aliases fail closed.
- Reconciles history to main media by media type and timestamp using deterministic iterative maximum-cardinality matching. Filename identifiers are not used as history identifiers.
- Separately reports missing HTML references, unexpected HTML references, and media not referenced by HTML.
- Writes only relative labels such as `export-1/memories/...`; source root names and absolute paths are not stored.

## Completion and exit status

- `0`: the audit completed and history/files/HTML evidence reconciled.
- `3`: the audit completed, but reconciliation or evidence is incomplete.
- `2`: unsafe, invalid, malformed, or conflicting input/output state was rejected.

Controlled failures print a generic message and do not echo sensitive paths or values.

## Privacy

Reports omit `Location` unless `--include-location` is passed. History-derived report fields are allowlisted to `history_index`, `Date`, `Media Type`, `reason`, and optional `Location`. Arbitrary history keys are discarded, URL-like values and unsafe control characters are rejected, and formula-leading CSV cells are neutralized before report creation.

Keep exports and reports private. Reports contain filenames, timestamps, byte counts, and hashes, which can still be sensitive. Do not attach real exports, reports, usernames, paths, or location history to bug reports.

## Honest limitations

- v0.1 does not automate Snapchat sign-in, export requests, downloads, or extraction.
- It does not read or verify the original ZIP, so ZIP integrity is not tested.
- SHA-256 and nonempty checks do not prove that every image pixel or video frame decodes.
- HTML and history formats are observed export behavior, not a guarantee from Snap Inc.
- The tool audits local evidence; it cannot prove that Snapchat supplied every Memory.

## Conservative limits

Defaults are 20,000 filesystem entries across all supplied export parts, path depth 32, 16 MiB per history JSON file, 16 MiB per Memories HTML file, 64 MiB total metadata, 500 GiB total media bytes, 20,000 history records, and 2,000,000 candidate matching edges. They can be lowered for a run:

```bash
python3 scripts/audit_export.py \
  --export ./extracted-export \
  --output ./new-report \
  --max-files 10000 \
  --max-depth 24 \
  --max-json-bytes 8388608 \
  --max-html-bytes 8388608 \
  --max-total-metadata-bytes 33554432 \
  --max-total-media-bytes 107374182400 \
  --max-history-records 10000 \
  --max-candidate-edges 1000000
```

The output parent should be on a normal macOS APFS or HFS+ volume that supports hard links and atomic directory renames.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py tests/*.py
python3 tests/run_fixture.py
```

Tests use synthetic data only.

The public test suite never requires or includes anyone's Snapchat export, account data, filenames, location history, or media.

## License

MIT. Snapchat is a trademark of Snap Inc. This project is not affiliated with or endorsed by Snap Inc.
