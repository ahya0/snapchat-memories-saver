# Private beta guide

Use this only after the maintainer has personally tested the same pinned version.

## Before testing

- Use a Mac with Python 3.9 or newer.
- Keep every original Snapchat ZIP unchanged.
- Make sure there is enough free space for the ZIPs and extracted files.
- Never send your export, report, photos, videos, account details, or location history to the maintainer or an AI chat.

## Test

1. Nontechnical testers should follow the README's seven-step Mac launcher section. The private report is created directly in `/Users/Shared`. Terminal testers may use the separate command-line flow.
2. Open `start_here.html`.
3. Check whether the result and next step are understandable without technical help.
4. Keep the ZIPs, extracted export folders, and report in a permanent private location.
5. If the command fails, record only the fixed error code printed in brackets.

## Privacy-safe feedback

Copy this template. Do not attach the export or full report.

```text
Tool version:
macOS version (optional):
Python version (optional):
Result: complete / incomplete / failed
Safe error code, if any:
Approximate number of Memories: under 1,000 / 1,000-10,000 / over 10,000
Number of ZIP parts:
Was any step confusing? If yes, which step?
Did start_here.html make the next action clear?
Anything you expected that was missing?
```

If a completed report exists, `support.txt` is the only report file intended for support sharing. It omits paths, filenames, hashes, Memory dates, locations, and URLs, but its aggregate counts can still be sensitive. Review it before sending.

## Stop conditions

Stop and report the safe error code if the tool asks for credentials, tries to upload data, modifies an export, follows a symlink, or creates an incomplete-looking report directory. Those behaviors are not expected.
