#!/usr/bin/env python3
"""Ad-hoc synthetic end-to-end runner for the audit-only CLI."""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "audit_export.py"
IMAGE = "2024-01-01_AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA-main.jpg"
OVERLAY = "2024-01-01_AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA-overlay.png"


def main() -> int:
    with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory(
        prefix="sms-v02-fixture-report-",
        dir="/private/tmp",
    ) as output_directory:
        base = Path(os.path.realpath(directory))
        output = Path(os.path.realpath(output_directory))
        output.rmdir()
        export = base / "synthetic-export"
        memories = export / "memories"
        history_dir = export / "json"
        memories.mkdir(parents=True)
        history_dir.mkdir(parents=True)
        (history_dir / "memories_history.json").write_text(
            json.dumps(
                {
                    "Saved Media": [
                        {
                            "Date": "2024-01-01 00:00:00 UTC",
                            "Media Type": "Image",
                            "Location": "not included by default",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (memories / IMAGE).write_bytes(b"synthetic-image")
        (memories / OVERLAY).write_bytes(b"synthetic-overlay")
        timestamp = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp()
        os.utime(memories / IMAGE, (timestamp, timestamp))
        os.utime(memories / OVERLAY, (timestamp, timestamp))
        (memories / "memories.html").write_text(
            f'<img src=".//{IMAGE}"><img src=".//{OVERLAY}">', encoding="utf-8"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--export",
                str(export),
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(result.stdout + result.stderr, file=sys.stderr)
            return result.returncode
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        expected = {
            "start_here.html",
            "summary.json",
            "manifest.csv",
            "missing_history_records.csv",
            "report.txt",
            "support.txt",
        }
        if summary.get("status") != "complete" or {path.name for path in output.iterdir()} != expected:
            print("fixture audit did not produce the expected complete report", file=sys.stderr)
            return 1
        if output.stat().st_mode & 0o777 != 0o700:
            print("fixture report directory mode was not 0700", file=sys.stderr)
            return 1
        print("synthetic audit fixture: complete")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
