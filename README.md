# Snapchat Memories Saver

**Make sure your Snapchat Memories export saved correctly and flag gaps.**

Snapchat Memories Saver walks you through Snapchat's official My Data export, then checks whether the exported history, photos, videos, and HTML agree. It runs locally, leaves the export untouched, and gives you a simple `start_here.html` result. Version 0.2 checks already-extracted exports; it does not yet copy the media into a separate deduplicated library.

## Use it with an AI agent

Give a local agent access to this repository and say:

> Use the Snapchat Memories Saver skill to help me preserve and check my Snapchat Memories export. Keep detailed reports out of AI context unless I explicitly approve that privacy boundary; use only the status, fixed error code, or a reviewed support.txt by default.

For pre-release Hermes testing, use the exact supplied project folder. It includes the discoverable project skill at `.hermes/skills/snapchat-memories-saver/SKILL.md`. From that folder, run `hermes skills trust .`, then start a new Hermes session in the same project and verify that the `snapchat-memories-saver` skill is listed before using it. The root `SKILL.md` and project-local copy are test-enforced to remain identical. Do not use an immutable `v0.2.0` installation URL until that tag has been published and verified, and never install mutable `main`.

Some Claude and Grok tools also support Agent Skills, but their file access and privacy boundaries differ. Basic ChatGPT, Claude, or Grok chats can use [`PROMPT.md`](PROMPT.md) as a walkthrough, but they cannot inspect folders on your computer unless they have local file and command access. Do not upload private Memories to a chatbot just to run this tool. See [`COMPATIBILITY.md`](COMPATIBILITY.md).

On a Mac, someone who does not want to use a terminal can double-click `Run Snapchat Memories Saver.command`. It asks them to select every extracted export folder, creates a uniquely named private report directly in `/Users/Shared`, runs the same audited command, and opens `start_here.html`. The report itself is mode `0700`; other users cannot read it. The launcher only collects arguments; it cannot bypass the audit's validation or privacy defaults.

### Mac setup without a terminal workflow

1. For a released version, download the pinned source ZIP from the project's [Releases page](https://github.com/ahya0/snapchat-memories-saver/releases), then double-click the ZIP to extract it. Pre-release testers should use only the exact project folder supplied for the test.
2. Keep the complete project folder together; do not move only the `.command` file.
3. If Python 3.9 or newer is not installed, download the macOS installer from [python.org](https://www.python.org/downloads/macos/) and run the signed `.pkg`. The launcher will show a fixed message and stop safely when Python is missing or too old.
4. Double-click `Run Snapchat Memories Saver.command`.
5. If macOS blocks the unsigned launcher, Control-click it, choose **Open**, then confirm **Open**. Do not disable Gatekeeper globally.
6. Select every extracted Snapchat ZIP-part folder. The launcher creates the private report directly in `/Users/Shared` and opens it when the check finishes.
7. Read the fixed `SMS-E-*` code in the dialog if the check fails. Closing the export-folder chooser cancels safely and changes nothing.

## Simple flow

1. Request a **Memories-only, all-time** export from [Snapchat My Data](https://accounts.snapchat.com/v2/download-my-data).
2. Check that the destination has enough free space for both the ZIPs and extracted files. Download every ZIP part and extract each one with Finder or Archive Utility.
3. From this repository, run:

```bash
python3 scripts/audit_export.py \
  --export "$HOME/Downloads/snapchat-export-1" \
  --export "$HOME/Downloads/snapchat-export-2" \
  --output "/Users/Shared/snapchat-report"
```

4. Open `start_here.html` in the new report folder.

Pass every extracted ZIP-part directory. Pass retry exports in the same command. The saver recognizes repeated files instead of counting them twice.

For filesystem safety, use a new report name directly in `/Users/Shared`. The output parent and its parent must be system-owned and free of extended ACLs that could authorize relocation. Home-folder and nested destinations are rejected because a same-user process could move or re-permission their parent during the audit.

Do not move or edit the selected export folders while the check is running. The tool rejects changes it detects during reading and final revalidation, but it does not create an atomic filesystem snapshot of the export.

The report folder is private and contains:

```text
start_here.html
summary.json
manifest.csv
missing_history_records.csv
report.txt
support.txt
```

Keep the original ZIPs, extracted export folders, and report in a permanent private location. Do not leave your only copy in Downloads.

## If something is missing

The command returns exit status `3` and says that some Memories may be missing. `start_here.html` lists dates and media types when history rows lack files, shows issue counts for other export gaps, and includes links to request another export or open Snapchat's help page.

Request a new export, extract it, then run the saver with both the original and retry folders. If Snapchat still omits an item, use its date to find it in the Snapchat app and save it manually.

The report does not keep direct per-Memory download links. Those links may be temporary and private.

## What it checks

- Walks inputs read-only with descriptor-relative, no-follow filesystem operations.
- Rejects symlinks, sockets, devices, FIFOs, unsafe path components, excessive depth, excessive entry counts, and oversized JSON/HTML metadata.
- Discovers `memories_history.json`, `memories/memories.html`, and observed `YYYY-MM-DD_<UUID>-main.jpg`, `-main.mp4`, and `-overlay.png` media names.
- Streams SHA-256 for media without loading complete media files into memory, bounds each read to its accepted size, and rejects files that change while being checked.
- Merges retry-export history with multiset-maximum semantics: repeated exports do not multiply the same rows, while repeated identical rows within one export remain distinct.
- Treats identical same-name/hash media across exports as one logical file. Conflicting bytes or NFC/case-folded filename aliases fail closed.
- Reconciles history to main media by media type and timestamp using deterministic iterative maximum-cardinality matching. Filename identifiers are not used as history identifiers.
- Separately reports missing HTML references, unexpected HTML references, and media not referenced by HTML.
- Writes only relative labels such as `export-1/memories/...`; source root names and absolute paths are not stored.

## Result

- `0`: the supplied export looks complete.
- `3`: some Memories or export evidence may be missing.
- `64`: invalid command usage.
- `65`: the input or export was rejected safely.
- `73`: the report destination is unavailable, already exists, or lacks space.
- `74`: a local file operation failed safely.
- `70`: an unexpected internal error was contained without exposing details.
- `69`: the `.command` wrapper could not find Python 3.9 or newer; install Python and try again.
- `130`: the user cancelled a folder chooser; no report was created.

Controlled failures print a fixed `SMS-E-*` code and do not echo sensitive paths or values. `support.txt` contains version and aggregate count information that is safer to share when asking for help; aggregate counts can still be sensitive, so review it before sharing and do not share the full report.

Some removable or network filesystems may not support the exclusive rename operation required for safe publication. If that happens, use a new report name directly in `/Users/Shared`; the tool returns `73` without publishing a report.

## Privacy

Reports omit `Location` unless `--include-location` is passed. History-derived report fields are allowlisted to `history_index`, `Date`, `Media Type`, `reason`, and optional `Location`. Arbitrary history keys are discarded, URL-like values and unsafe control characters are rejected, and formula-leading CSV cells are neutralized before report creation.

Keep exports and reports private. Reports contain filenames, timestamps, byte counts, and hashes, which can still be sensitive. Do not attach real exports, reports, usernames, paths, or location history to bug reports.

## Limits

- The user signs in to Snapchat and downloads the official export. The skill never handles credentials.
- It does not read or verify the original ZIP, so ZIP integrity is not tested.
- SHA-256 and nonempty checks do not prove that every image pixel or video frame decodes.
- HTML and history formats are observed export behavior, not a guarantee from Snap Inc.
- The tool checks the evidence Snapchat supplied. It cannot detect a Memory omitted from both the export history and media files.
- The report describes the supplied evidence as read during the audit. Do not move or edit selected exports while it runs; the tool does not create an atomic filesystem snapshot.

The current release is macOS-focused and requires Python 3.9+. Technical limits and matching details are in [`references/export-format.md`](references/export-format.md).

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py tests/*.py
python3 tests/run_fixture.py
```

Tests use synthetic data only.

The public test suite never requires or includes anyone's Snapchat export, account data, filenames, location history, or media.

Friends testing an unreleased build should follow [`BETA.md`](BETA.md) and share only its privacy-safe feedback fields.

The reviewed path from this skill to a unique local library, duplicate cleanup, friends beta, and future iPhone/Android apps is documented in [`ROADMAP.md`](ROADMAP.md).

## License

MIT. Snapchat is a trademark of Snap Inc. This project is not affiliated with or endorsed by Snap Inc.
