#!/usr/bin/env python3
"""Audit already-extracted Snapchat Memories exports without modifying them."""

from __future__ import annotations

import argparse
import bisect
import csv
import ctypes
import datetime as dt
import errno
import hashlib
import html
import io
import json
import os
import re
import stat
import sys
import unicodedata
import urllib.parse
import uuid
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, NoReturn, Optional, Sequence, Set, Tuple

MEDIA_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})_([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})-(main|overlay)\.(jpg|mp4|png)$"
)
URL_RE = re.compile(
    r"(?:\b[A-Za-z][A-Za-z0-9+.-]*://|\b(?:about|blob|data|file|javascript|mailto):|www\.)",
    re.IGNORECASE,
)
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
DEFAULT_MAX_FILES = 20_000
DEFAULT_MAX_DEPTH = 32
DEFAULT_MAX_JSON_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_HTML_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TOTAL_METADATA_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_MEDIA_BYTES = 500 * 1024 * 1024 * 1024
DEFAULT_MAX_HISTORY_RECORDS = 20_000
DEFAULT_MAX_HTML_REFERENCES = 50_000
MAX_HISTORY_RECORD_BYTES = 64 * 1024
DEFAULT_MAX_CANDIDATE_EDGES = 2_000_000
TOOL_NAME = "snapchat-memories-saver"
TOOL_VERSION = "0.2.0"
REPORT_SCHEMA_NAME = "snapchat-memories-audit"
REPORT_SCHEMA_VERSION = 1
EVIDENCE_LIMITATION = (
    "This result covers only the supplied export evidence; it cannot detect a Memory "
    "omitted from every supplied artifact."
)
INPUT_SNAPSHOT_LIMITATION = (
    "This report describes the supplied export evidence as read during the audit; "
    "do not move or edit selected exports while it runs."
)
REPORT_SPACE_RESERVE = 1024 * 1024
HASH_CHUNK = 1024 * 1024
TIMESTAMP_TOLERANCE = 2.1
FORMULA_PREFIXES = ("=", "+", "-", "@")
SNAPCHAT_EXPORT_URL = "https://accounts.snapchat.com/v2/download-my-data"
SNAPCHAT_HELP_URL = (
    "https://help.snapchat.com/hc/en-us/articles/"
    "7012305371156-How-do-I-download-my-data-from-Snapchat"
)
BIDI_CONTROLS = {
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
}
_RENAME_EXCL = 0x00000004
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEATX_NP = getattr(_LIBC, "renameatx_np", None)
_ACL_GET_FD_NP = getattr(_LIBC, "acl_get_fd_np", None)
_ACL_FREE = getattr(_LIBC, "acl_free", None)
_ACL_TYPE_EXTENDED = 0x00000100
if _RENAMEATX_NP is not None:
    _RENAMEATX_NP.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEATX_NP.restype = ctypes.c_int
if _ACL_GET_FD_NP is not None:
    _ACL_GET_FD_NP.argtypes = [ctypes.c_int, ctypes.c_int]
    _ACL_GET_FD_NP.restype = ctypes.c_void_p
if _ACL_FREE is not None:
    _ACL_FREE.argtypes = [ctypes.c_void_p]
    _ACL_FREE.restype = ctypes.c_int


class AuditError(Exception):
    """A controlled, privacy-safe audit failure."""

    def __init__(self, message: str, code: str = "UNSAFE_INPUT") -> None:
        super().__init__(message)
        self.code = code


class UsageError(Exception):
    """Privacy-safe argparse rejection."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def _safe_failure(exc: Exception) -> Tuple[int, str, str]:
    if isinstance(exc, AuditError):
        messages = {
            "OUTPUT_ALREADY_EXISTS": "choose a new report folder",
            "OUTPUT_UNAVAILABLE": "the report destination is unavailable",
            "RESOURCE_LIMIT_REACHED": "the export is larger than the configured safety limit",
            "INSUFFICIENT_SPACE": "the report destination does not have enough free space",
            "UNSUPPORTED_SYSTEM": "this macOS filesystem cannot publish the report safely",
            "INVALID_EXPORT": "the export could not be checked safely",
            "INVALID_ARGUMENT": "one or more command options were invalid",
            "IO_ERROR": "a local file operation failed safely",
            "UNSAFE_INPUT": "the selected files or folders were rejected safely",
        }
        message = messages.get(exc.code, messages["UNSAFE_INPUT"])
        if exc.code == "INVALID_ARGUMENT":
            return 64, "SMS-E-USAGE", message
        if exc.code in {
            "OUTPUT_ALREADY_EXISTS",
            "OUTPUT_UNAVAILABLE",
            "INSUFFICIENT_SPACE",
            "UNSUPPORTED_SYSTEM",
        }:
            return 73, "SMS-E-OUTPUT", message
        if exc.code == "IO_ERROR":
            return 74, "SMS-E-IO", message
        return 65, "SMS-E-INPUT", message
    if isinstance(exc, OSError) and exc.errno in {
        errno.ENOSPC,
        getattr(errno, "EDQUOT", errno.ENOSPC),
    }:
        return 73, "SMS-E-OUTPUT", "the report destination does not have enough free space"
    if isinstance(exc, OSError):
        return 74, "SMS-E-IO", "a local file operation failed safely"
    return 70, "SMS-E-INTERNAL", "an unexpected internal error was contained"


def _available_bytes(directory_fd: int) -> Tuple[int, int]:
    info = os.fstatvfs(directory_fd)
    fragment = info.f_frsize or info.f_bsize or 4096
    return max(0, info.f_bavail) * fragment, fragment


def _require_free_space(directory_fd: int, required: int) -> None:
    available, _ = _available_bytes(directory_fd)
    if available < required:
        raise AuditError("insufficient output space", "INSUFFICIENT_SPACE")


def _require_payload_space(directory_fd: int, payloads: dict) -> None:
    available, fragment = _available_bytes(directory_fd)
    encoded = sum(
        ((len(payload) + fragment - 1) // fragment) * fragment
        for payload in payloads.values()
    )
    required = encoded + REPORT_SPACE_RESERVE + fragment * 4
    if available < required:
        raise AuditError("insufficient output space", "INSUFFICIENT_SPACE")


@dataclass
class ResourceBudget:
    entries_remaining: int
    metadata_bytes_remaining: int
    media_bytes_remaining: int
    history_records_remaining: int


@dataclass(frozen=True)
class MediaFile:
    export_index: int
    relative: Tuple[str, ...]
    label: str
    filename: str
    role: str
    suffix: str
    size: int
    mtime: float
    sha256: str


@dataclass
class ExportScan:
    index: int
    histories: List[List[dict]]
    media: List[MediaFile]
    html_documents: List[Tuple[Tuple[str, ...], str, str]]
    regular_files: Set[Tuple[str, ...]]
    root: Path = Path("/")
    root_identity: Tuple[int, int] = (0, 0)
    snapshot: Dict[
        Tuple[str, ...], Tuple[int, int, int, int, int, int]
    ] = field(default_factory=dict)


def alias(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _validate_component(value: str) -> None:
    if (
        value in {"", ".", ".."}
        or "/" in value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in BIDI_CONTROLS for character in value)
    ):
        raise AuditError("unsafe path component")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _open_absolute_directory(path: Path) -> int:
    absolute = _absolute(path)
    if not absolute.is_absolute():
        raise AuditError("directory path must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            _validate_component(component)
            try:
                child = os.open(component, flags, dir_fd=current)
            except OSError as exc:
                raise AuditError("directory is missing or has an unsafe ancestor") from exc
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _identity(info: os.stat_result) -> Tuple[int, int]:
    return info.st_dev, info.st_ino


def _stable_file_signature(info: os.stat_result) -> Tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _entry_signature(info: os.stat_result) -> Tuple[int, int, int, int, int, int]:
    return (
        stat.S_IFMT(info.st_mode),
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _directory_or_ancestor_matches(directory_fd: int, identities: Set[Tuple[int, int]]) -> bool:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.dup(directory_fd)
    try:
        while True:
            current_identity = _identity(os.fstat(current))
            if current_identity in identities:
                return True
            parent = os.open("..", flags, dir_fd=current)
            try:
                parent_identity = _identity(os.fstat(parent))
            except BaseException:
                os.close(parent)
                raise
            if parent_identity == current_identity:
                os.close(parent)
                return False
            os.close(current)
            current = parent
    finally:
        os.close(current)


def _directory_has_extended_acl(directory_fd: int) -> bool:
    if _ACL_GET_FD_NP is None or _ACL_FREE is None:
        raise AuditError("ACL inspection is unavailable", "UNSUPPORTED_SYSTEM")
    ctypes.set_errno(0)
    acl = _ACL_GET_FD_NP(directory_fd, _ACL_TYPE_EXTENDED)
    if not acl:
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            return False
        raise AuditError("ACL inspection failed", "OUTPUT_UNAVAILABLE")
    try:
        return True
    finally:
        if _ACL_FREE(acl) != 0:
            raise AuditError("ACL inspection cleanup failed", "OUTPUT_UNAVAILABLE")


def _assert_output_parent_not_movable(parent_fd: int) -> None:
    parent = os.fstat(parent_fd)
    if parent.st_uid != 0 or _directory_has_extended_acl(parent_fd):
        raise AuditError(
            "output parent can be relocated during the audit",
            "OUTPUT_UNAVAILABLE",
        )
    grandparent_fd = os.open(
        "..",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        grandparent = os.fstat(grandparent_fd)
        if (
            grandparent.st_uid != 0
            or _directory_has_extended_acl(grandparent_fd)
            or os.access(
            ".",
            os.W_OK | os.X_OK,
            dir_fd=grandparent_fd,
            effective_ids=True,
            follow_symlinks=False,
            )
        ):
            raise AuditError(
                "output parent can be relocated during the audit",
                "OUTPUT_UNAVAILABLE",
            )
    finally:
        os.close(grandparent_fd)


def _prepare_output_parent(
    output: Path,
    exports: Sequence[Path],
) -> Tuple[int, str, Path, Tuple[Tuple[int, int], ...], Tuple[int, int]]:
    absolute = _absolute(output)
    if absolute == Path("/"):
        raise AuditError("unsafe output path")
    for export in exports:
        source = _absolute(export)
        try:
            if os.path.commonpath((os.fspath(source), os.fspath(absolute))) == os.fspath(source):
                raise AuditError("output must not be inside an input", "OUTPUT_UNAVAILABLE")
        except ValueError as exc:
            raise AuditError("unsafe output path") from exc
    name = absolute.name
    _validate_component(name)
    try:
        parent_fd = _open_absolute_directory(absolute.parent)
    except AuditError as exc:
        raise AuditError("output parent is unavailable", "OUTPUT_UNAVAILABLE") from exc
    try:
        export_identities = []
        for export in exports:
            export_fd = _open_absolute_directory(export)
            try:
                export_identities.append(_identity(os.fstat(export_fd)))
            finally:
                os.close(export_fd)
        if _directory_or_ancestor_matches(parent_fd, set(export_identities)):
            raise AuditError("output must not be inside an input", "OUTPUT_UNAVAILABLE")
        _assert_output_parent_not_movable(parent_fd)
        parent_identity = _identity(os.fstat(parent_fd))
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return (
                parent_fd,
                name,
                absolute,
                tuple(export_identities),
                parent_identity,
            )
        raise AuditError("output already exists", "OUTPUT_ALREADY_EXISTS")
    except BaseException:
        os.close(parent_fd)
        raise


def _assert_output_parent_stable(
    parent_fd: int,
    output: Path,
    expected_parent_identity: Tuple[int, int],
    export_identities: Sequence[Tuple[int, int]],
) -> None:
    if _identity(os.fstat(parent_fd)) != expected_parent_identity:
        raise AuditError("output parent changed", "OUTPUT_UNAVAILABLE")
    _assert_output_parent_not_movable(parent_fd)
    try:
        reopened = _open_absolute_directory(output.parent)
    except AuditError as exc:
        raise AuditError("output parent changed", "OUTPUT_UNAVAILABLE") from exc
    try:
        if _identity(os.fstat(reopened)) != expected_parent_identity:
            raise AuditError("output parent changed", "OUTPUT_UNAVAILABLE")
    finally:
        os.close(reopened)
    if _directory_or_ancestor_matches(parent_fd, set(export_identities)):
        raise AuditError("output must not be inside an input", "OUTPUT_UNAVAILABLE")


def _open_regular_at(directory_fd: int, name: str) -> Tuple[int, os.stat_result]:
    _validate_component(name)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError as exc:
        raise AuditError("unsafe or unreadable regular file") from exc
    try:
        info = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise AuditError("non-regular file rejected")
    if info.st_nlink != 1:
        os.close(fd)
        raise AuditError("multiply linked file rejected")
    return fd, info


def _read_bounded_at(
    directory_fd: int,
    name: str,
    maximum: int,
    budget: ResourceBudget,
    signature_out: Optional[List[Tuple[int, int, int, int, int, int]]] = None,
) -> str:
    fd, info = _open_regular_at(directory_fd, name)
    try:
        if info.st_size > maximum:
            raise AuditError("metadata file exceeds configured limit", "RESOURCE_LIMIT_REACHED")
        if info.st_size > budget.metadata_bytes_remaining:
            raise AuditError("total metadata exceeds configured limit", "RESOURCE_LIMIT_REACHED")
        chunks = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        extra = os.read(fd, 1)
        final_info = os.fstat(fd)
        if (
            remaining
            or extra
            or _stable_file_signature(final_info) != _stable_file_signature(info)
        ):
            raise AuditError("input file changed while being checked", "INVALID_EXPORT")
        data = b"".join(chunks)
        budget.metadata_bytes_remaining -= info.st_size
        if signature_out is not None:
            signature_out.append(_entry_signature(final_info))
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AuditError("metadata is not valid UTF-8", "INVALID_EXPORT") from exc
    finally:
        os.close(fd)


def _hash_at(
    directory_fd: int,
    name: str,
    budget: ResourceBudget,
    signature_out: Optional[List[Tuple[int, int, int, int, int, int]]] = None,
) -> Tuple[str, int, float]:
    fd, info = _open_regular_at(directory_fd, name)
    try:
        if info.st_size > budget.media_bytes_remaining:
            raise AuditError("total media bytes exceed configured limit", "RESOURCE_LIMIT_REACHED")
        digest = hashlib.sha256()
        total = 0
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(HASH_CHUNK, remaining))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            remaining -= len(chunk)
        extra = os.read(fd, 1)
        final_info = os.fstat(fd)
        if (
            remaining
            or extra
            or _stable_file_signature(final_info) != _stable_file_signature(info)
        ):
            raise AuditError("input file changed while being checked", "INVALID_EXPORT")
        budget.media_bytes_remaining -= info.st_size
        if signature_out is not None:
            signature_out.append(_entry_signature(final_info))
        return digest.hexdigest(), total, info.st_mtime
    finally:
        os.close(fd)


def _scalar(value, field: str) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
        if (
            any(ord(character) < 32 or ord(character) == 127 for character in text)
            or any(character in BIDI_CONTROLS for character in text)
        ):
            raise AuditError("history contains unsafe text", "INVALID_EXPORT")
        return text
    raise AuditError("history contains an invalid allowed field", "INVALID_EXPORT")


def _skip_json_space(text: str, position: int) -> int:
    while position < len(text) and text[position] in " \t\r\n":
        position += 1
    return position


def _json_container_end(text: str, position: int) -> int:
    if position >= len(text) or text[position] not in "{[":
        raise AuditError("history JSON has an invalid shape", "INVALID_EXPORT")
    stack = [text[position]]
    position += 1
    quoted = False
    escaped = False
    while position < len(text):
        character = text[position]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "{[":
            stack.append(character)
            if len(stack) > DEFAULT_MAX_DEPTH:
                raise AuditError("history JSON is too deeply nested", "INVALID_EXPORT")
        elif character in "}]":
            expected = "{" if character == "}" else "["
            if not stack or stack[-1] != expected:
                raise AuditError("history JSON is malformed", "INVALID_EXPORT")
            stack.pop()
            if not stack:
                return position + 1
        position += 1
    raise AuditError("history JSON is malformed", "INVALID_EXPORT")


def _parse_history(text: str, max_records: int) -> List[dict]:
    decoder = json.JSONDecoder()
    position = _skip_json_space(text, 0)
    if position >= len(text) or text[position] != "{":
        raise AuditError("history JSON has an invalid shape", "INVALID_EXPORT")
    position += 1
    records = []
    found_saved_media = False
    while True:
        position = _skip_json_space(text, position)
        if position < len(text) and text[position] == "}":
            position += 1
            break
        try:
            key, position = decoder.raw_decode(text, position)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise AuditError("history JSON is malformed", "INVALID_EXPORT") from exc
        if not isinstance(key, str):
            raise AuditError("history JSON has an invalid shape", "INVALID_EXPORT")
        position = _skip_json_space(text, position)
        if position >= len(text) or text[position] != ":":
            raise AuditError("history JSON is malformed", "INVALID_EXPORT")
        position = _skip_json_space(text, position + 1)
        if key != "Saved Media" or found_saved_media:
            raise AuditError("history JSON has an invalid shape", "INVALID_EXPORT")
        found_saved_media = True
        if position >= len(text) or text[position] != "[":
            raise AuditError("history JSON has an invalid shape", "INVALID_EXPORT")
        position += 1
        while True:
            position = _skip_json_space(text, position)
            if position < len(text) and text[position] == "]":
                position += 1
                break
            if len(records) >= max_records:
                raise AuditError(
                    "history record count exceeds configured limit",
                    "RESOURCE_LIMIT_REACHED",
                )
            end = _json_container_end(text, position)
            if end - position > MAX_HISTORY_RECORD_BYTES:
                raise AuditError(
                    "history record exceeds configured limit",
                    "RESOURCE_LIMIT_REACHED",
                )
            try:
                item = decoder.decode(text[position:end])
            except (json.JSONDecodeError, RecursionError) as exc:
                raise AuditError("history JSON is malformed", "INVALID_EXPORT") from exc
            position = end
            if not isinstance(item, dict):
                raise AuditError("history record has an invalid shape", "INVALID_EXPORT")
            record = {}
            for field in ("Date", "Media Type", "Location"):
                value = _scalar(item.get(field), field)
                if value is not None:
                    record[field] = value
            records.append(record)
            position = _skip_json_space(text, position)
            if position < len(text) and text[position] == ",":
                position += 1
                if _skip_json_space(text, position) < len(text) and text[
                    _skip_json_space(text, position)
                ] == "]":
                    raise AuditError("history JSON is malformed", "INVALID_EXPORT")
                continue
            if position < len(text) and text[position] == "]":
                continue
            raise AuditError("history JSON is malformed", "INVALID_EXPORT")
        position = _skip_json_space(text, position)
        if position < len(text) and text[position] == ",":
            position += 1
            if _skip_json_space(text, position) < len(text) and text[
                _skip_json_space(text, position)
            ] == "}":
                raise AuditError("history JSON is malformed", "INVALID_EXPORT")
            continue
        if position < len(text) and text[position] == "}":
            position += 1
            break
        raise AuditError("history JSON is malformed", "INVALID_EXPORT")
    if not found_saved_media or _skip_json_space(text, position) != len(text):
        raise AuditError("history JSON has an invalid shape", "INVALID_EXPORT")
    return records


def _scan_export(
    root: Path,
    index: int,
    max_files: int,
    max_depth: int,
    max_json_bytes: int,
    max_html_bytes: int,
    budget: ResourceBudget,
) -> ExportScan:
    root = _absolute(root)
    root_fd = _open_absolute_directory(root)
    root_identity = _identity(os.fstat(root_fd))
    histories: List[List[dict]] = []
    media: List[MediaFile] = []
    html_documents: List[Tuple[Tuple[str, ...], str, str]] = []
    regular_files: Set[Tuple[str, ...]] = set()
    snapshot: Dict[Tuple[str, ...], Tuple[int, int, int, int, int, int]] = {}
    spellings: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
    stack: List[Tuple[int, Tuple[str, ...]]] = [(root_fd, ())]
    try:
        while stack:
            directory_fd, prefix = stack.pop()
            try:
                directory_before = _entry_signature(os.fstat(directory_fd))
                if directory_before[0] != stat.S_IFDIR:
                    raise AuditError("input directory changed while being checked")
                try:
                    names = sorted(os.listdir(directory_fd), key=lambda item: (alias(item), item))
                except OSError as exc:
                    raise AuditError("input directory could not be listed") from exc
                child_directories = []
                for name in names:
                    _validate_component(name)
                    relative = prefix + (name,)
                    depth = len(relative)
                    if depth > max_depth:
                        raise AuditError(
                            "input path depth exceeds configured limit",
                            "RESOURCE_LIMIT_REACHED",
                        )
                    budget.entries_remaining -= 1
                    if budget.entries_remaining < 0:
                        raise AuditError(
                            "input file count exceeds configured limit",
                            "RESOURCE_LIMIT_REACHED",
                        )
                    key = tuple(alias(part) for part in relative)
                    previous = spellings.get(key)
                    if previous is not None and previous != relative:
                        raise AuditError("input paths collide after Unicode/case normalization")
                    spellings[key] = relative
                    try:
                        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except OSError as exc:
                        raise AuditError("input entry could not be inspected") from exc
                    if stat.S_ISLNK(info.st_mode):
                        raise AuditError("symlinks are not allowed")
                    if stat.S_ISDIR(info.st_mode):
                        child_directories.append((name, relative))
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        raise AuditError("special files are not allowed")
                    if info.st_nlink != 1:
                        raise AuditError("multiply linked input files are not allowed")
                    regular_files.add(relative)
                    file_signature = _entry_signature(info)
                    label = "export-{}/{}".format(index, "/".join(relative))
                    if name == "memories_history.json":
                        signatures = []
                        records = _parse_history(
                            _read_bounded_at(
                                directory_fd,
                                name,
                                max_json_bytes,
                                budget,
                                signatures,
                            ),
                            budget.history_records_remaining,
                        )
                        file_signature = signatures[0]
                        budget.history_records_remaining -= len(records)
                        histories.append(records)
                    if len(relative) >= 2 and relative[-2:] == ("memories", "memories.html"):
                        signatures = []
                        html_documents.append(
                            (
                                relative,
                                label,
                                _read_bounded_at(
                                    directory_fd,
                                    name,
                                    max_html_bytes,
                                    budget,
                                    signatures,
                                ),
                            )
                        )
                        file_signature = signatures[0]
                    match = MEDIA_RE.match(name)
                    if match is not None:
                        _, _, role, suffix = match.groups()
                        if (role == "main" and suffix not in {"jpg", "mp4"}) or (
                            role == "overlay" and suffix != "png"
                        ):
                            snapshot[relative] = file_signature
                            continue
                        signatures = []
                        digest, size, mtime = _hash_at(
                            directory_fd, name, budget, signatures
                        )
                        file_signature = signatures[0]
                        media.append(
                            MediaFile(
                                export_index=index,
                                relative=relative,
                                label=label,
                                filename=name,
                                role=role,
                                suffix=suffix,
                                size=size,
                                mtime=mtime,
                                sha256=digest,
                            )
                        )
                    snapshot[relative] = file_signature
                for child_name, child_relative in reversed(child_directories):
                    try:
                        child_fd = os.open(
                            child_name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory_fd,
                        )
                    except OSError as exc:
                        raise AuditError("unsafe input directory") from exc
                    stack.append((child_fd, child_relative))
                directory_after = _entry_signature(os.fstat(directory_fd))
                if directory_after != directory_before:
                    raise AuditError(
                        "input directory changed while being checked",
                        "INVALID_EXPORT",
                    )
                snapshot[prefix] = directory_after
            finally:
                os.close(directory_fd)
    except BaseException:
        for directory_fd, _ in stack:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        raise
    return ExportScan(
        index=index,
        histories=histories,
        media=media,
        html_documents=html_documents,
        regular_files=regular_files,
        root=root,
        root_identity=root_identity,
        snapshot=snapshot,
    )


def _capture_export_snapshot(
    root: Path,
    max_depth: int,
    entries_remaining: List[int],
) -> Tuple[Tuple[int, int], Dict[Tuple[str, ...], Tuple[int, int, int, int, int, int]]]:
    root_fd = _open_absolute_directory(root)
    root_identity = _identity(os.fstat(root_fd))
    snapshot: Dict[Tuple[str, ...], Tuple[int, int, int, int, int, int]] = {}
    spellings: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
    stack: List[Tuple[int, Tuple[str, ...]]] = [(root_fd, ())]
    try:
        while stack:
            directory_fd, prefix = stack.pop()
            try:
                directory_before = _entry_signature(os.fstat(directory_fd))
                if directory_before[0] != stat.S_IFDIR:
                    raise AuditError("input directory changed while being checked")
                names = sorted(
                    os.listdir(directory_fd), key=lambda item: (alias(item), item)
                )
                child_directories = []
                for name in names:
                    _validate_component(name)
                    relative = prefix + (name,)
                    if len(relative) > max_depth:
                        raise AuditError(
                            "input path depth exceeds configured limit",
                            "RESOURCE_LIMIT_REACHED",
                        )
                    entries_remaining[0] -= 1
                    if entries_remaining[0] < 0:
                        raise AuditError(
                            "input file count exceeds configured limit",
                            "RESOURCE_LIMIT_REACHED",
                        )
                    key = tuple(alias(part) for part in relative)
                    previous = spellings.get(key)
                    if previous is not None and previous != relative:
                        raise AuditError(
                            "input paths collide after Unicode/case normalization"
                        )
                    spellings[key] = relative
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise AuditError("symlinks are not allowed")
                    if stat.S_ISDIR(info.st_mode):
                        child_directories.append((name, relative))
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        raise AuditError("special files are not allowed")
                    file_fd, file_info = _open_regular_at(directory_fd, name)
                    try:
                        snapshot[relative] = _entry_signature(file_info)
                    finally:
                        os.close(file_fd)
                for child_name, child_relative in reversed(child_directories):
                    child_fd = os.open(
                        child_name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    stack.append((child_fd, child_relative))
                directory_after = _entry_signature(os.fstat(directory_fd))
                if directory_after != directory_before:
                    raise AuditError(
                        "input directory changed while being checked",
                        "INVALID_EXPORT",
                    )
                snapshot[prefix] = directory_after
            finally:
                os.close(directory_fd)
    except BaseException:
        for directory_fd, _ in stack:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        raise
    return root_identity, snapshot


def _revalidate_scans(
    scans: Sequence[ExportScan],
    max_files: int,
    max_depth: int,
) -> None:
    entries_remaining = [max_files]
    for scan in scans:
        root_identity, snapshot = _capture_export_snapshot(
            scan.root,
            max_depth,
            entries_remaining,
        )
        if root_identity != scan.root_identity or snapshot != scan.snapshot:
            raise AuditError(
                "input changed after it was checked",
                "INVALID_EXPORT",
            )


def _merge_history(scans: Sequence[ExportScan]) -> List[dict]:
    maximum = Counter()
    templates = {}
    order = []
    for scan in scans:
        for records in scan.histories:
            counts = Counter()
            for record in records:
                key = (
                    record.get("Date", ""),
                    record.get("Media Type", ""),
                    record.get("Location", ""),
                )
                counts[key] += 1
                if key not in templates:
                    templates[key] = dict(record)
                    order.append(key)
            for key, count in counts.items():
                maximum[key] = max(maximum[key], count)
    merged = []
    for key in order:
        merged.extend(dict(templates[key]) for _ in range(maximum[key]))
    return merged


def _logical_media(scans: Sequence[ExportScan]) -> Tuple[List[dict], int]:
    groups = defaultdict(list)
    spellings = {}
    for scan in scans:
        for item in scan.media:
            key = alias(item.filename)
            previous = spellings.get(key)
            if previous is not None and previous != item.filename:
                raise AuditError("media filenames collide after Unicode/case normalization")
            spellings[key] = item.filename
            groups[key].append(item)
    logical = []
    duplicate_count = 0
    for key in sorted(groups):
        items = sorted(groups[key], key=lambda item: item.label)
        signatures = {(item.sha256, item.size) for item in items}
        if len(signatures) != 1:
            raise AuditError("conflicting bytes share a media filename", "INVALID_EXPORT")
        duplicate_count += len(items) - 1
        selected = items[0]
        logical.append(
            {
                "key": key,
                "file": selected,
                "source_count": len(items),
                "empty": selected.size == 0,
            }
        )
    return logical, duplicate_count


def _history_timestamp(record: dict, offset_hours: int) -> float:
    parsed = dt.datetime.strptime(record["Date"], "%Y-%m-%d %H:%M:%S UTC").replace(
        tzinfo=dt.timezone.utc
    )
    return (parsed + dt.timedelta(hours=offset_hours)).timestamp()


def _media_type(item: dict) -> Optional[str]:
    media = item["file"]
    if media.role != "main":
        return None
    if media.suffix == "jpg":
        return "Image"
    if media.suffix == "mp4":
        return "Video"
    return None


def _choose_offset(history: Sequence[dict], main: Sequence[dict]) -> int:
    file_times = defaultdict(list)
    history_times = defaultdict(list)
    for item in main:
        kind = _media_type(item)
        if kind:
            file_times[kind].append(item["file"].mtime)
    for values in file_times.values():
        values.sort()
    for record in history:
        try:
            timestamp = _history_timestamp(record, 0)
        except (KeyError, TypeError, ValueError):
            continue
        history_times[record.get("Media Type", "")].append(timestamp)
    for values in history_times.values():
        values.sort()

    scores = {}
    for offset in range(-14, 15):
        shift = offset * 3600
        score = 0
        for kind, base_targets in history_times.items():
            files = file_times.get(kind, [])
            history_index = 0
            file_index = 0
            while history_index < len(base_targets) and file_index < len(files):
                target = base_targets[history_index] + shift
                media_time = files[file_index]
                if media_time < target - TIMESTAMP_TOLERANCE:
                    file_index += 1
                elif media_time > target + TIMESTAMP_TOLERANCE:
                    history_index += 1
                else:
                    score += 1
                    history_index += 1
                    file_index += 1
        scores[offset] = score
    return sorted(scores, key=lambda value: (-scores[value], abs(value), value))[0]


def _maximum_matching(adjacency: Dict[int, List[int]]) -> Tuple[Dict[int, int], Dict[int, int]]:
    pair_left: Dict[int, int] = {}
    pair_right: Dict[int, int] = {}
    for start in sorted(adjacency):
        if start in pair_left:
            continue
        queue = deque([start])
        visited_left = {start}
        visited_right = set()
        parent_right: Dict[int, int] = {}
        parent_left: Dict[int, int] = {}
        free_right = None
        while queue and free_right is None:
            left = queue.popleft()
            for right in adjacency[left]:
                if right in visited_right:
                    continue
                visited_right.add(right)
                parent_right[right] = left
                owner = pair_right.get(right)
                if owner is None:
                    free_right = right
                    break
                if owner not in visited_left:
                    visited_left.add(owner)
                    parent_left[owner] = right
                    queue.append(owner)
        if free_right is None:
            continue
        right = free_right
        while True:
            left = parent_right[right]
            old_right = pair_left.get(left)
            pair_left[left] = right
            pair_right[right] = left
            if old_right is None:
                break
            right = parent_left[left]
    return pair_left, pair_right


def _match_history(
    history: Sequence[dict],
    logical: Sequence[dict],
    max_candidate_edges: int = DEFAULT_MAX_CANDIDATE_EDGES,
):
    main = [item for item in logical if _media_type(item) is not None]
    main.sort(key=lambda item: (item["file"].mtime, item["file"].filename, item["file"].label))
    offset = _choose_offset(history, main)
    by_type = defaultdict(list)
    type_times = defaultdict(list)
    for index, item in enumerate(main):
        kind = _media_type(item)
        by_type[kind].append((item["file"].mtime, index))
    for kind, values in by_type.items():
        values.sort(key=lambda value: (value[0], main[value[1]]["file"].label))
        type_times[kind] = [value[0] for value in values]
    adjacency = {}
    invalid = {}
    candidate_edges = 0
    for history_index, record in enumerate(history):
        try:
            target = _history_timestamp(record, offset)
        except (KeyError, TypeError, ValueError):
            invalid[history_index] = "invalid or missing Date field"
            continue
        kind = record.get("Media Type", "")
        values = by_type.get(kind, [])
        times = type_times.get(kind, [])
        left = bisect.bisect_left(times, target - TIMESTAMP_TOLERANCE)
        right = bisect.bisect_right(times, target + TIMESTAMP_TOLERANCE)
        candidates = values[left:right]
        candidate_edges += len(candidates)
        if candidate_edges > max_candidate_edges:
            raise AuditError(
                "history matching exceeds configured candidate limit",
                "RESOURCE_LIMIT_REACHED",
            )
        adjacency[history_index] = [
            item_index
            for _, item_index in sorted(
                candidates,
                key=lambda value: (
                    abs(value[0] - target),
                    main[value[1]]["file"].filename,
                    main[value[1]]["file"].label,
                ),
            )
        ]
    pair_left, pair_right = _maximum_matching(adjacency)
    missing = []
    for index, record in enumerate(history):
        if index in pair_left:
            continue
        missing.append(
            {
                "history_index": index,
                "Date": record.get("Date", ""),
                "Media Type": record.get("Media Type", ""),
                "Location": record.get("Location", ""),
                "reason": invalid.get(index, "no exported media file matched timestamp/type"),
            }
        )
    matched_by_key = {}
    for history_index, main_index in pair_left.items():
        matched_by_key[main[main_index]["key"]] = (history_index, history[history_index])
    unmatched = [main[index] for index in range(len(main)) if index not in pair_right]
    return offset, matched_by_key, missing, unmatched


class _SourceParser(HTMLParser):
    def __init__(self, on_source: Callable[[str], None]) -> None:
        super().__init__(convert_charrefs=True)
        self.on_source = on_source

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() not in {"img", "video", "source"}:
            return
        for key, value in attrs:
            if key.casefold() == "src" and value is not None:
                self.on_source(value)


def _safe_reference(value: str) -> Optional[Tuple[str, ...]]:
    if not value or "\x00" in value or "\\" in value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        return None
    pure = PurePosixPath(parsed.path)
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        return None
    try:
        for part in parts:
            _validate_component(part)
    except AuditError:
        return None
    return parts


def _check_html(
    scans: Sequence[ExportScan],
    logical: Sequence[dict],
    max_html_references: int = DEFAULT_MAX_HTML_REFERENCES,
) -> dict:
    media_keys = {item["key"] for item in logical}
    referenced = set()
    missing = []
    unexpected = []
    reference_count = 0
    html_count = 0
    for scan in scans:
        scan_media_paths = {
            item.relative: alias(item.filename) for item in scan.media
        }
        for document_ordinal, (relative, _html_label, text) in enumerate(
            scan.html_documents, 1
        ):
            html_count += 1
            document_reference_count = 0

            def handle_source(raw: str) -> None:
                nonlocal document_reference_count, reference_count
                if reference_count >= max_html_references:
                    raise AuditError(
                        "HTML reference count exceeds configured limit",
                        "RESOURCE_LIMIT_REACHED",
                    )
                reference_count += 1
                document_reference_count += 1
                parts = _safe_reference(raw)
                evidence = "export-{}--html-{}-reference-{}".format(
                    scan.index,
                    document_ordinal,
                    document_reference_count,
                )
                if parts is None:
                    unexpected.append(evidence)
                    return
                target = relative[:-1] + parts
                filename_key = alias(parts[-1])
                if target in scan_media_paths and scan_media_paths[target] == filename_key:
                    referenced.add(filename_key)
                elif target in scan.regular_files:
                    unexpected.append(evidence)
                else:
                    missing.append(evidence)

            parser = _SourceParser(handle_source)
            try:
                parser.feed(text)
            except AuditError:
                raise
            except Exception as exc:
                raise AuditError("HTML metadata could not be parsed", "INVALID_EXPORT") from exc
    unreferenced = [item["file"].label for item in logical if item["key"] not in referenced]
    return {
        "html_file_count": html_count,
        "reference_count": reference_count,
        "missing_references": sorted(set(missing)),
        "unexpected_references": sorted(set(unexpected)),
        "unreferenced_media": sorted(unreferenced),
    }


def _validate_report_value(value) -> None:
    if isinstance(value, str):
        stripped = value.lstrip()
        if URL_RE.search(value) or SCHEME_RE.match(stripped) or stripped.startswith("//"):
            raise AuditError("URL-like report value rejected")
        if stripped.startswith("/"):
            raise AuditError("absolute report path rejected")
    elif isinstance(value, dict):
        for key, item in value.items():
            _validate_report_value(key)
            _validate_report_value(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_report_value(item)


def _csv_payload(rows: Iterable[dict], fields: Sequence[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(fields), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    sanitized_rows = []
    for row in rows:
        sanitized = {}
        for field in fields:
            value = row.get(field, "")
            text = str(value)
            if text.lstrip().startswith(FORMULA_PREFIXES):
                text = "'" + text
            sanitized[field] = text
        sanitized_rows.append(sanitized)
    writer.writerows(sanitized_rows)
    return output.getvalue().encode("utf-8")


def _start_here_payload(summary: dict, missing_rows: Sequence[dict]) -> bytes:
    incomplete = summary["status"] != "complete"
    history_complete = bool(
        summary.get(
            "history_file_reconciliation_complete",
            not (
                int(summary.get("missing_history_record_count", 0))
                or int(summary.get("unmatched_main_file_count", 0))
            ),
        )
    )
    html_complete = bool(summary.get("html_evidence_complete", not incomplete))
    media_complete = bool(
        summary.get(
            "media_nonempty_check_complete",
            int(summary.get("empty_media_file_count", 0)) == 0,
        )
    )
    html_only = incomplete and history_complete and media_complete and not html_complete
    issues = []
    issue_fields = (
        ("missing_history_record_count", "History entries without matching files"),
        ("unmatched_main_file_count", "Saved files without matching history entries"),
        ("empty_media_file_count", "Empty media files"),
        ("html_missing_reference_count", "HTML references to missing files"),
        ("html_unexpected_reference_count", "Unsupported HTML references"),
        ("html_unreferenced_media_count", "Saved files not referenced by HTML"),
    )
    for key, label in issue_fields:
        count = int(summary.get(key, 0))
        if count:
            issues.append("<li>{}: {}</li>".format(label, count))
    if incomplete and int(summary.get("html_file_count", 0)) == 0:
        issues.append("<li>No Memories HTML file was found.</li>")
    issues_html = (
        "<h2>What needs attention</h2><ul>{}</ul>".format("".join(issues))
        if issues
        else ""
    )
    if html_only:
        title = "The export evidence needs another check"
        intro = (
            "The history entries still match the saved media, but the export's "
            "HTML evidence is missing or does not agree. Your files were not changed."
        )
        steps = """
<ol>
  <li>Re-extract every original ZIP part with Finder or Archive Utility.</li>
  <li>Run the saver again on every newly extracted part.</li>
  <li>If the HTML evidence still does not agree, request another <strong>Memories-only, all-time</strong> export and include both attempts.</li>
</ol>
"""
        actions = """
<div class="actions">
  <a class="button" href="{export_url}" target="_blank" rel="noreferrer noopener">Request another export</a>
  <a class="button secondary" href="{help_url}" target="_blank" rel="noreferrer noopener">Snapchat help</a>
</div>
""".format(export_url=SNAPCHAT_EXPORT_URL, help_url=SNAPCHAT_HELP_URL)
        privacy_note = (
            '<p class="note">Direct links from Snapchat export pages are not copied '
            "into this report because they can be temporary and private.</p>"
        )
    elif incomplete:
        title = "Some Memories may be missing"
        intro = (
            "The export did not fully reconcile. "
            "Your existing files were not changed."
        )
        steps = """
<ol>
  <li>Request another <strong>Memories-only, all-time</strong> export from Snapchat.</li>
  <li>Download and extract every ZIP part from the retry export.</li>
  <li>Run the saver again with the original and retry export folders. Repeated files are counted once.</li>
</ol>
"""
        actions = """
<div class="actions">
  <a class="button" href="{export_url}" target="_blank" rel="noreferrer noopener">Request another export</a>
  <a class="button secondary" href="{help_url}" target="_blank" rel="noreferrer noopener">Snapchat help</a>
</div>
""".format(export_url=SNAPCHAT_EXPORT_URL, help_url=SNAPCHAT_HELP_URL)
        privacy_note = (
            '<p class="note">Direct links to individual Memories are not copied into '
            "this report because Snapchat export links can be temporary and private. "
            "Use the dates above to find an item in Snapchat if a retry export still "
            "misses it.</p>"
        )
    else:
        title = "Your export check looks complete"
        intro = (
            "Every Snapchat history entry matched a saved photo or video, "
            "and the export's HTML references matched the files. "
            + EVIDENCE_LIMITATION
            + " "
            + INPUT_SNAPSHOT_LIMITATION
        )
        steps = (
            "<p>Keep the original ZIP files, extracted export folders, and this "
            "report in a permanent private location. Do not leave your only copy "
            "in Downloads.</p>"
        )
        actions = ""
        privacy_note = ""

    rows = []
    for row in missing_rows[:500]:
        rows.append(
            "<tr><td>{}</td><td>{}</td></tr>".format(
                html.escape(str(row.get("Date", ""))),
                html.escape(str(row.get("Media Type", ""))),
            )
        )
    missing_table = ""
    if rows:
        remainder = len(missing_rows) - len(rows)
        remainder_text = (
            "<p>{} more missing items are listed in "
            "<code>missing_history_records.csv</code>.</p>".format(remainder)
            if remainder
            else ""
        )
        missing_table = """
<h2>Items to look for</h2>
<table>
  <thead><tr><th>Date</th><th>Type</th></tr></thead>
  <tbody>{}</tbody>
</table>
{}
""".format("".join(rows), remainder_text)

    page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Snapchat Memories Saver</title>
<style>
body{{font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#171717;max-width:760px;margin:48px auto;padding:0 20px}}
h1{{font-size:2rem;line-height:1.15}} h2{{margin-top:2rem}} .status{{padding:18px;border-radius:14px;background:{status_color}}}
.actions{{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0}} a.button{{background:#fffc00;color:#111;padding:11px 16px;border-radius:10px;text-decoration:none;font-weight:700}}
a.secondary{{background:#eee}} table{{width:100%;border-collapse:collapse}} th,td{{padding:9px;text-align:left;border-bottom:1px solid #ddd}} code{{font-size:.9em}}
.note{{color:#555;font-size:.92rem}}
</style>
</head>
<body>
<main>
<div class="status"><h1>{title}</h1><p>{intro}</p></div>
<p><strong>{history_count}</strong> history entries checked. <strong>{missing_count}</strong> did not match a saved file.</p>
{issues_html}
{steps}
{actions}
{missing_table}
{privacy_note}
<p class="note">Snapchat Memories Saver {tool_version}</p>
</main>
</body>
</html>
""".format(
        status_color="#fffbd1" if incomplete else "#e8f7e8",
        title=title,
        intro=intro,
        history_count=summary["history_record_count"],
        missing_count=summary["missing_history_record_count"],
        issues_html=issues_html,
        steps=steps,
        actions=actions,
        missing_table=missing_table,
        privacy_note=privacy_note,
        tool_version=html.escape(str(summary.get("tool_version", TOOL_VERSION))),
    )
    return page.encode("utf-8")


def _rename_directory_exclusive(
    source_parent_fd: int,
    source: str,
    destination_parent_fd: int,
    destination: str,
    guard: Optional[Callable[[], None]] = None,
    pre_rename: Optional[Callable[[], None]] = None,
) -> None:
    if _RENAMEATX_NP is None:
        raise AuditError("exclusive report publication is unavailable", "UNSUPPORTED_SYSTEM")
    if guard is not None:
        guard()
    if pre_rename is not None:
        pre_rename()
    result = _RENAMEATX_NP(
        source_parent_fd,
        os.fsencode(source),
        destination_parent_fd,
        os.fsencode(destination),
        _RENAME_EXCL,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise AuditError("output already exists", "OUTPUT_ALREADY_EXISTS")
        raise OSError(error, os.strerror(error))


def _close_after_interrupt(file_fd: int) -> None:
    interrupted: Optional[KeyboardInterrupt] = None
    for _ in range(2):
        try:
            os.close(file_fd)
        except KeyboardInterrupt as exc:
            if interrupted is None:
                interrupted = exc
            continue
        except OSError as exc:
            if interrupted is not None and exc.errno == errno.EBADF:
                raise interrupted from exc
            raise
        if interrupted is not None:
            raise interrupted
        return
    assert interrupted is not None
    raise interrupted


def _remove_report_stage_once(
    parent_fd: int,
    stage_name: str,
    names: Sequence[str],
    expected_identity: Optional[Tuple[int, int]] = None,
) -> None:
    try:
        stage_fd = os.open(
            stage_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return
    try:
        opened_identity = _identity(os.fstat(stage_fd))
        if expected_identity is not None and opened_identity != expected_identity:
            raise AuditError("report cleanup target changed", "IO_ERROR")
        allowed = set(names)
        removable = [
            name
            for name in os.listdir(stage_fd)
            if name in allowed or re.fullmatch(r"\.tmp-[0-9a-f]{32}", name) is not None
        ]
        first_error = None
        for name in removable:
            try:
                file_fd = os.open(
                    name,
                    os.O_WRONLY | os.O_NOFOLLOW,
                    dir_fd=stage_fd,
                )
            except FileNotFoundError:
                continue
            try:
                info = os.fstat(file_fd)
                if not stat.S_ISREG(info.st_mode):
                    raise AuditError("report cleanup file changed", "IO_ERROR")
                os.ftruncate(file_fd, 0)
                os.fsync(file_fd)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            finally:
                _close_after_interrupt(file_fd)
        for name in removable:
            try:
                os.unlink(name, dir_fd=stage_fd)
            except FileNotFoundError:
                continue
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
    finally:
        _close_after_interrupt(stage_fd)
    try:
        current_identity = _identity(
            os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
        )
    except FileNotFoundError:
        return
    if current_identity != opened_identity:
        raise AuditError("report cleanup target changed", "IO_ERROR")
    try:
        os.rmdir(stage_name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except BaseException as exc:
        if first_error is None:
            first_error = exc
    if first_error is not None:
        raise first_error


def _remove_report_stage(
    parent_fd: int,
    stage_name: str,
    names: Sequence[str],
    expected_identity: Optional[Tuple[int, int]] = None,
) -> None:
    interrupted: Optional[KeyboardInterrupt] = None
    for _ in range(2):
        try:
            _remove_report_stage_once(
                parent_fd,
                stage_name,
                names,
                expected_identity=expected_identity,
            )
        except KeyboardInterrupt as exc:
            if interrupted is None:
                interrupted = exc
            continue
        except BaseException as exc:
            if interrupted is not None:
                raise interrupted from exc
            raise
        if interrupted is not None:
            raise interrupted
        return
    assert interrupted is not None
    raise interrupted


def _raise_output_context(exc: OSError) -> None:
    if exc.errno in {
        errno.EACCES,
        errno.EROFS,
        errno.EXDEV,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }:
        raise AuditError("output destination is unavailable", "OUTPUT_UNAVAILABLE") from exc
    raise exc


def _write_exclusive_atomic(
    directory_fd: int,
    name: str,
    payload: bytes,
    guard: Optional[Callable[[], None]] = None,
) -> None:
    _validate_component(name)
    if guard is not None:
        guard()
    temporary = ".tmp-{}".format(uuid.uuid4().hex)
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    installed = False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        try:
            _close_after_interrupt(fd)
        except KeyboardInterrupt:
            fd = -1
            raise
        else:
            fd = -1
        _rename_directory_exclusive(
            directory_fd,
            temporary,
            directory_fd,
            name,
            guard=guard,
        )
        installed = True
        os.fsync(directory_fd)
    except FileExistsError as exc:
        raise AuditError("report file already exists", "OUTPUT_ALREADY_EXISTS") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
    if not installed:
        raise AuditError("report file could not be installed", "IO_ERROR")


def audit_exports(
    exports: Sequence[Path],
    output: Path,
    include_location: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_json_bytes: int = DEFAULT_MAX_JSON_BYTES,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    max_total_metadata_bytes: int = DEFAULT_MAX_TOTAL_METADATA_BYTES,
    max_total_media_bytes: int = DEFAULT_MAX_TOTAL_MEDIA_BYTES,
    max_history_records: int = DEFAULT_MAX_HISTORY_RECORDS,
    max_candidate_edges: int = DEFAULT_MAX_CANDIDATE_EDGES,
) -> dict:
    if not exports:
        raise AuditError("at least one export is required", "INVALID_ARGUMENT")
    limits = (
        max_files,
        max_depth,
        max_json_bytes,
        max_html_bytes,
        max_total_metadata_bytes,
        max_total_media_bytes,
        max_history_records,
        max_candidate_edges,
    )
    if any(not isinstance(value, int) or value <= 0 for value in limits):
        raise AuditError("limits must be positive integers", "INVALID_ARGUMENT")
    (
        parent_fd,
        output_name,
        absolute_output,
        export_identities,
        parent_identity,
    ) = _prepare_output_parent(Path(output), [Path(item) for item in exports])
    published = False
    try:
        _require_free_space(parent_fd, REPORT_SPACE_RESERVE)
        budget = ResourceBudget(
            entries_remaining=max_files,
            metadata_bytes_remaining=max_total_metadata_bytes,
            media_bytes_remaining=max_total_media_bytes,
            history_records_remaining=max_history_records,
        )
        scans = [
            _scan_export(
                Path(root),
                index,
                max_files,
                max_depth,
                max_json_bytes,
                max_html_bytes,
                budget,
            )
            for index, root in enumerate(exports, 1)
        ]
        if tuple(scan.root_identity for scan in scans) != export_identities:
            raise AuditError("input roots changed while being checked", "INVALID_EXPORT")
        if not any(records for scan in scans for records in scan.histories):
            raise AuditError("no memories history was found", "INVALID_EXPORT")
        history = _merge_history(scans)
        logical, duplicate_count = _logical_media(scans)
        offset, matched, missing, unmatched = _match_history(
            history, logical, max_candidate_edges=max_candidate_edges
        )
        html = _check_html(scans, logical)
        empty_count = sum(item["empty"] for item in logical)
        history_file_complete = not (missing or unmatched)
        html_complete = not (
            html["html_file_count"] == 0
            or html["missing_references"]
            or html["unexpected_references"]
            or html["unreferenced_media"]
        )
        media_nonempty_complete = empty_count == 0
        incomplete = not (
            history_file_complete and html_complete and media_nonempty_complete
        )
        summary = {
            "tool_name": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "report_schema_name": REPORT_SCHEMA_NAME,
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": "incomplete" if incomplete else "complete",
            "history_file_reconciliation_complete": history_file_complete,
            "html_evidence_complete": html_complete,
            "media_nonempty_check_complete": media_nonempty_complete,
            "export_count": len(scans),
            "history_record_count": len(history),
            "logical_media_file_count": len(logical),
            "main_media_file_count": sum(_media_type(item) is not None for item in logical),
            "overlay_file_count": sum(item["file"].role == "overlay" for item in logical),
            "identical_repeated_media_count": duplicate_count,
            "matched_history_record_count": len(matched),
            "missing_history_record_count": len(missing),
            "unmatched_main_file_count": len(unmatched),
            "empty_media_file_count": empty_count,
            "timestamp_offset_hours": offset,
            "html_file_count": html["html_file_count"],
            "html_reference_count": html["reference_count"],
            "html_missing_reference_count": len(html["missing_references"]),
            "html_unexpected_reference_count": len(html["unexpected_references"]),
            "html_unreferenced_media_count": len(html["unreferenced_media"]),
            "html_missing_references": html["missing_references"],
            "html_unexpected_references": html["unexpected_references"],
            "html_unreferenced_media": html["unreferenced_media"],
            "limitations": [
                EVIDENCE_LIMITATION,
                INPUT_SNAPSHOT_LIMITATION,
                "SHA-256 hashes and nonempty-file checks do not prove every image pixel or video frame decodes.",
                "ZIP integrity was not tested because this tool audits only already-extracted directories.",
                "History matching uses media type and timestamp only; filename identifiers are not matching evidence.",
            ],
        }
        manifest = []
        for sequence, item in enumerate(sorted(logical, key=lambda value: value["file"].label), 1):
            media = item["file"]
            row = {
                "sequence": sequence,
                "input_label": media.label,
                "filename": media.filename,
                "role": media.role,
                "media_type": _media_type(item) or "Overlay",
                "bytes": media.size,
                "sha256": media.sha256,
                "modified_utc": dt.datetime.fromtimestamp(
                    media.mtime, tz=dt.timezone.utc
                ).isoformat(),
                "identical_source_count": item["source_count"],
                "history_index": "",
                "Date": "",
                "Media Type": "",
            }
            history_match = matched.get(item["key"])
            if history_match is not None:
                history_index, record = history_match
                row.update(
                    {
                        "history_index": history_index,
                        "Date": record.get("Date", ""),
                        "Media Type": record.get("Media Type", ""),
                    }
                )
                if include_location:
                    row["Location"] = record.get("Location", "")
            elif include_location:
                row["Location"] = ""
            manifest.append(row)
        missing_rows = []
        for row in missing:
            sanitized = {
                "history_index": row["history_index"],
                "Date": row["Date"],
                "Media Type": row["Media Type"],
                "reason": row["reason"],
            }
            if include_location:
                sanitized["Location"] = row.get("Location", "")
            missing_rows.append(sanitized)
        manifest_fields = [
            "sequence",
            "input_label",
            "filename",
            "role",
            "media_type",
            "bytes",
            "sha256",
            "modified_utc",
            "identical_source_count",
            "history_index",
            "Date",
            "Media Type",
        ]
        missing_fields = ["history_index", "Date", "Media Type", "reason"]
        if include_location:
            manifest_fields.append("Location")
            missing_fields.insert(3, "Location")
        status_text = summary["status"].upper()
        report = "\n".join(
            [
                "Snapchat Memories Saver {} backup check".format(TOOL_VERSION),
                "================================================",
                "Status: {}".format(status_text),
                "History/file reconciliation complete: {}".format(
                    "yes" if summary["history_file_reconciliation_complete"] else "no"
                ),
                "HTML evidence complete: {}".format(
                    "yes" if summary["html_evidence_complete"] else "no"
                ),
                "History records: {}".format(summary["history_record_count"]),
                "Logical media files: {}".format(summary["logical_media_file_count"]),
                "Missing history records: {}".format(summary["missing_history_record_count"]),
                "Unmatched main files: {}".format(summary["unmatched_main_file_count"]),
                "HTML missing references: {}".format(summary["html_missing_reference_count"]),
                "HTML unexpected references: {}".format(summary["html_unexpected_reference_count"]),
                "HTML unreferenced media: {}".format(summary["html_unreferenced_media_count"]),
                EVIDENCE_LIMITATION,
                INPUT_SNAPSHOT_LIMITATION,
                "SHA-256 hashes and nonempty checks do not prove every image pixel or video frame decodes.",
                "ZIP integrity was not tested by this tool; it audits extracted directories only.",
                "No identifier matching is claimed; history matching uses media type and timestamp only.",
                "",
            ]
        )
        support = "\n".join(
            [
                "Snapchat Memories Saver privacy-reduced support summary",
                "========================================================",
                "Tool version: {}".format(summary["tool_version"]),
                "Report schema: {}".format(summary["report_schema_version"]),
                "Status: {}".format(status_text),
                "Export folders checked: {}".format(summary["export_count"]),
                "History records: {}".format(summary["history_record_count"]),
                "Logical media files: {}".format(summary["logical_media_file_count"]),
                "Repeated identical source files: {}".format(
                    summary["identical_repeated_media_count"]
                ),
                "Missing history records: {}".format(
                    summary["missing_history_record_count"]
                ),
                "Unmatched main files: {}".format(summary["unmatched_main_file_count"]),
                "Empty media files: {}".format(summary["empty_media_file_count"]),
                "HTML missing references: {}".format(
                    summary["html_missing_reference_count"]
                ),
                "HTML unexpected references: {}".format(
                    summary["html_unexpected_reference_count"]
                ),
                "HTML unreferenced media: {}".format(
                    summary["html_unreferenced_media_count"]
                ),
                EVIDENCE_LIMITATION,
                INPUT_SNAPSHOT_LIMITATION,
                "",
                "This file omits paths, filenames, hashes, timestamps from Memories, locations, and media.",
                "Its aggregate counts may still be sensitive; review this file before sharing it.",
                "If you choose to ask for help, share this file instead of the full report.",
                "",
            ]
        )
        _validate_report_value(summary)
        _validate_report_value(manifest)
        _validate_report_value(missing_rows)
        _validate_report_value(report)
        _validate_report_value(support)
        payloads = {
            "start_here.html": _start_here_payload(summary, missing_rows),
            "summary.json": (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            "manifest.csv": _csv_payload(manifest, manifest_fields),
            "missing_history_records.csv": _csv_payload(missing_rows, missing_fields),
            "report.txt": report.encode("utf-8"),
            "support.txt": support.encode("utf-8"),
        }
        _require_payload_space(parent_fd, payloads)
        _assert_output_parent_stable(
            parent_fd,
            absolute_output,
            parent_identity,
            export_identities,
        )
        _revalidate_scans(scans, max_files, max_depth)
        report_names = (
            "start_here.html",
            "summary.json",
            "manifest.csv",
            "missing_history_records.csv",
            "report.txt",
            "support.txt",
        )
        stage_name = ".audit-stage-{}".format(uuid.uuid4().hex)
        guard = lambda: _assert_output_parent_stable(
            parent_fd,
            absolute_output,
            parent_identity,
            export_identities,
        )
        stage_created = False
        stage_identity = None
        failure = None
        try:
            guard()
            os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
            stage_created = True
            guard()
            output_fd = os.open(
                stage_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                stage_identity = _identity(os.fstat(output_fd))
                os.fchmod(output_fd, 0o700)
                for name in report_names:
                    _write_exclusive_atomic(
                        output_fd,
                        name,
                        payloads[name],
                        guard=guard,
                    )
                os.fsync(output_fd)
            finally:
                os.close(output_fd)
            guard()
            os.fsync(parent_fd)
            _rename_directory_exclusive(
                parent_fd,
                stage_name,
                parent_fd,
                output_name,
                guard=guard,
                pre_rename=lambda: _revalidate_scans(scans, max_files, max_depth),
            )
            published = True
        except OSError as exc:
            try:
                _raise_output_context(exc)
            except Exception as mapped:
                failure = mapped
        except BaseException as exc:
            failure = exc
        if failure is not None:
            if stage_created and not published:
                try:
                    _remove_report_stage(
                        parent_fd,
                        stage_name,
                        report_names,
                        expected_identity=stage_identity,
                    )
                except BaseException as cleanup_failure:
                    raise failure from cleanup_failure
            raise failure
        if not published:
            if stage_created:
                _remove_report_stage(parent_fd, stage_name, report_names)
            raise AuditError("report was not published", "IO_ERROR")
        return summary
    finally:
        active_failure = sys.exc_info()[0] is not None
        try:
            os.close(parent_fd)
        except BaseException:
            if not published and not active_failure:
                raise


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--export", dest="exports", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-location", action="store_true")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--max-json-bytes", type=int, default=DEFAULT_MAX_JSON_BYTES)
    parser.add_argument("--max-html-bytes", type=int, default=DEFAULT_MAX_HTML_BYTES)
    parser.add_argument(
        "--max-total-metadata-bytes",
        type=int,
        default=DEFAULT_MAX_TOTAL_METADATA_BYTES,
    )
    parser.add_argument(
        "--max-total-media-bytes",
        type=int,
        default=DEFAULT_MAX_TOTAL_MEDIA_BYTES,
    )
    parser.add_argument(
        "--max-history-records", type=int, default=DEFAULT_MAX_HISTORY_RECORDS
    )
    parser.add_argument(
        "--max-candidate-edges", type=int, default=DEFAULT_MAX_CANDIDATE_EDGES
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        summary = audit_exports(
            args.exports,
            args.output,
            include_location=args.include_location,
            max_files=args.max_files,
            max_depth=args.max_depth,
            max_json_bytes=args.max_json_bytes,
            max_html_bytes=args.max_html_bytes,
            max_total_metadata_bytes=args.max_total_metadata_bytes,
            max_total_media_bytes=args.max_total_media_bytes,
            max_history_records=args.max_history_records,
            max_candidate_edges=args.max_candidate_edges,
        )
    except UsageError:
        print("audit failed [SMS-E-USAGE]: invalid command usage", file=sys.stderr)
        return 64
    except (AuditError, OSError) as exc:
        status, code, message = _safe_failure(exc)
        print("audit failed [{}]: {}".format(code, message), file=sys.stderr)
        return status
    except Exception as exc:
        status, code, message = _safe_failure(exc)
        print("audit failed [{}]: {}".format(code, message), file=sys.stderr)
        return status
    print("audit status: {}".format(summary["status"]))
    if summary["status"] == "complete":
        print("Your export check looks complete. Open start_here.html in the report folder.")
        print(EVIDENCE_LIMITATION)
        print(INPUT_SNAPSHOT_LIMITATION)
    else:
        print("Some Memories may be missing. Open start_here.html for recovery steps.")
    return 0 if summary["status"] == "complete" else 3


if __name__ == "__main__":
    raise SystemExit(main())
