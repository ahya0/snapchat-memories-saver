#!/usr/bin/env python3
"""Guided macOS launcher for the local Snapchat Memories audit."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import List, NoReturn, Optional, Sequence

SCRIPT = Path(__file__).resolve().with_name("audit_export.py")
DEFAULT_REPORT_PARENT = Path("/Users/Shared")
SAFE_CODE_BY_STATUS = {
    64: "SMS-E-USAGE",
    65: "SMS-E-INPUT",
    70: "SMS-E-INTERNAL",
    73: "SMS-E-OUTPUT",
    74: "SMS-E-IO",
}
SAFE_MESSAGE_BY_STATUS = {
    73: (
        "The report is created directly in /Users/Shared. Make sure it has enough "
        "free space, the report name does not already exist, and the destination is "
        "a compatible local Mac filesystem."
    ),
}
KNOWN_AUDIT_STATUSES = {0, 3, 64, 65, 70, 73, 74}


class UserCancelled(Exception):
    """The user closed a macOS chooser without selecting anything."""


class UsageError(Exception):
    """Privacy-safe command usage rejection."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def _run_jxa(source: str):
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", source],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if "(-128)" in result.stderr or "User canceled" in result.stderr:
            raise UserCancelled()
        raise RuntimeError("macOS folder chooser failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("macOS folder chooser returned invalid data") from exc


def choose_export_folders() -> List[Path]:
    source = r'''
const app = Application.currentApplication();
app.includeStandardAdditions = true;
const chosen = app.chooseFolder({
  withPrompt: "Select every extracted Snapchat export folder",
  multipleSelectionsAllowed: true
});
JSON.stringify(chosen.map(item => Path(item).toString()));
'''
    values = _run_jxa(source)
    if not isinstance(values, list) or not values or not all(isinstance(item, str) for item in values):
        raise RuntimeError("macOS folder chooser returned invalid selections")
    return [Path(item) for item in values]


def default_output(parent: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return parent / "Snapchat Memories Report {}".format(stamp)


def build_audit_command(exports: Sequence[Path], output: Path) -> List[str]:
    command = [sys.executable, str(SCRIPT)]
    for export in exports:
        command.extend(["--export", str(export)])
    command.extend(["--output", str(output)])
    return command


def run_audit(exports: Sequence[Path], output: Path, *, open_result: bool = True) -> int:
    result = subprocess.run(build_audit_command(exports, output), text=True)
    if result.returncode not in KNOWN_AUDIT_STATUSES:
        return 70
    if result.returncode in {0, 3} and open_result:
        subprocess.run(["open", str(output / "start_here.html")], check=False)
    return result.returncode


def _show_message(message: str) -> None:
    source = r'''
const app = Application.currentApplication();
app.includeStandardAdditions = true;
app.displayDialog(%s, {buttons: ["OK"], defaultButton: "OK"});
''' % json.dumps(message)
    try:
        subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", source],
            capture_output=True,
            text=True,
        )
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--export", dest="exports", action="append", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-open", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except UsageError:
        print("audit failed [SMS-E-USAGE]: invalid command usage", file=sys.stderr)
        return 64
    if bool(args.exports) != bool(args.output):
        print("audit failed [SMS-E-USAGE]: invalid command usage", file=sys.stderr)
        return 64
    try:
        if args.exports:
            return run_audit(args.exports, args.output, open_result=not args.no_open)
        exports = choose_export_folders()
        output = default_output(DEFAULT_REPORT_PARENT)
        result = run_audit(exports, output, open_result=not args.no_open)
    except UserCancelled:
        return 130
    except RuntimeError:
        print(
            "audit failed [SMS-E-INTERNAL]: an unexpected internal error was contained",
            file=sys.stderr,
        )
        _show_message("The folder chooser failed safely. No files were changed.")
        return 70
    except OSError:
        print(
            "audit failed [SMS-E-IO]: a local file operation failed safely",
            file=sys.stderr,
        )
        return 74
    except Exception:
        print(
            "audit failed [SMS-E-INTERNAL]: an unexpected internal error was contained",
            file=sys.stderr,
        )
        return 70
    if result not in {0, 3}:
        safe_code = SAFE_CODE_BY_STATUS.get(result, "SMS-E-INTERNAL")
        corrective_action = SAFE_MESSAGE_BY_STATUS.get(
            result,
            "Review the fixed error code and try again without sharing private report files.",
        )
        _show_message(
            "The export could not be checked safely [{}]. {} No export files were changed.".format(
                safe_code,
                corrective_action,
            )
        )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
