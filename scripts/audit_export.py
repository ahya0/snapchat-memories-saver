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
import io
import json
import os
import re
import stat
import sys
import unicodedata
import uuid
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

MEDIA_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})_([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})-(main|overlay)\.(jpg|mp4|png)$"
)
URL_RE = re.compile(
    r"(?:\b[A-Za-z][A-Za-z0-9+.-]{1,31}://|(?:data|mailto):|www\.)",
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
DEFAULT_MAX_CANDIDATE_EDGES = 2_000_000
HASH_CHUNK = 1024 * 1024
TIMESTAMP_TOLERANCE = 2.1
FORMULA_PREFIXES = ("=", "+", "-", "@")
BIDI_CONTROLS = {
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
}
_RENAME_EXCL = 0x00000004
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEATX_NP = getattr(_LIBC, "renameatx_np", None)
if _RENAMEATX_NP is not None:
    _RENAMEATX_NP.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEATX_NP.restype = ctypes.c_int


class AuditError(Exception):
    """A controlled, privacy-safe audit failure."""


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
    except Exception:
        os.close(current)
        raise


def _prepare_output_parent(output: Path, exports: Sequence[Path]) -> Tuple[int, str, Path]:
    absolute = _absolute(output)
    if absolute == Path("/"):
        raise AuditError("unsafe output path")
    for export in exports:
        source = _absolute(export)
        try:
            if os.path.commonpath((os.fspath(source), os.fspath(absolute))) == os.fspath(source):
                raise AuditError("output must not be inside an input")
        except ValueError as exc:
            raise AuditError("unsafe output path") from exc
    name = absolute.name
    _validate_component(name)
    parent_fd = _open_absolute_directory(absolute.parent)
    try:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return parent_fd, name, absolute
        raise AuditError("output already exists")
    except Exception:
        os.close(parent_fd)
        raise


def _open_regular_at(directory_fd: int, name: str) -> Tuple[int, os.stat_result]:
    _validate_component(name)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError as exc:
        raise AuditError("unsafe or unreadable regular file") from exc
    try:
        info = os.fstat(fd)
    except Exception:
        os.close(fd)
        raise
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise AuditError("non-regular file rejected")
    return fd, info


def _read_bounded_at(
    directory_fd: int,
    name: str,
    maximum: int,
    budget: ResourceBudget,
) -> str:
    fd, info = _open_regular_at(directory_fd, name)
    try:
        if info.st_size > maximum:
            raise AuditError("metadata file exceeds configured limit")
        if info.st_size > budget.metadata_bytes_remaining:
            raise AuditError("total metadata exceeds configured limit")
        budget.metadata_bytes_remaining -= info.st_size
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise AuditError("metadata file exceeds configured limit")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AuditError("metadata is not valid UTF-8") from exc
    finally:
        os.close(fd)


def _hash_at(
    directory_fd: int,
    name: str,
    budget: ResourceBudget,
) -> Tuple[str, int, float]:
    fd, info = _open_regular_at(directory_fd, name)
    try:
        if info.st_size > budget.media_bytes_remaining:
            raise AuditError("total media bytes exceed configured limit")
        budget.media_bytes_remaining -= info.st_size
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
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
            raise AuditError("history contains unsafe text")
        return text
    raise AuditError("history contains an invalid allowed field")


def _parse_history(text: str) -> List[dict]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AuditError("history JSON is malformed") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("Saved Media"), list):
        raise AuditError("history JSON has an invalid shape")
    records = []
    for item in payload["Saved Media"]:
        if not isinstance(item, dict):
            raise AuditError("history record has an invalid shape")
        record = {}
        for field in ("Date", "Media Type", "Location"):
            value = _scalar(item.get(field), field)
            if value is not None:
                record[field] = value
        records.append(record)
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
    root_fd = _open_absolute_directory(root)
    histories: List[List[dict]] = []
    media: List[MediaFile] = []
    html_documents: List[Tuple[Tuple[str, ...], str, str]] = []
    regular_files: Set[Tuple[str, ...]] = set()
    spellings: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
    stack: List[Tuple[int, Tuple[str, ...]]] = [(root_fd, ())]
    try:
        while stack:
            directory_fd, prefix = stack.pop()
            try:
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
                        raise AuditError("input path depth exceeds configured limit")
                    budget.entries_remaining -= 1
                    if budget.entries_remaining < 0:
                        raise AuditError("input file count exceeds configured limit")
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
                    regular_files.add(relative)
                    label = "export-{}/{}".format(index, "/".join(relative))
                    if name == "memories_history.json":
                        records = _parse_history(
                            _read_bounded_at(
                                directory_fd, name, max_json_bytes, budget
                            )
                        )
                        if len(records) > budget.history_records_remaining:
                            raise AuditError("history record count exceeds configured limit")
                        budget.history_records_remaining -= len(records)
                        histories.append(records)
                    if len(relative) >= 2 and relative[-2:] == ("memories", "memories.html"):
                        html_documents.append(
                            (
                                relative,
                                label,
                                _read_bounded_at(
                                    directory_fd, name, max_html_bytes, budget
                                ),
                            )
                        )
                    match = MEDIA_RE.match(name)
                    if match is not None:
                        _, _, role, suffix = match.groups()
                        if (role == "main" and suffix not in {"jpg", "mp4"}) or (
                            role == "overlay" and suffix != "png"
                        ):
                            continue
                        digest, size, mtime = _hash_at(directory_fd, name, budget)
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
            finally:
                os.close(directory_fd)
    except Exception:
        for directory_fd, _ in stack:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        raise
    return ExportScan(index, histories, media, html_documents, regular_files)


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
            raise AuditError("conflicting bytes share a media filename")
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
            raise AuditError("history matching exceeds configured candidate limit")
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
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() not in {"img", "video", "source"}:
            return
        for key, value in attrs:
            if key.casefold() == "src" and value is not None:
                self.sources.append(value)


def _safe_reference(value: str) -> Optional[Tuple[str, ...]]:
    if not value or "\x00" in value or "\\" in value:
        return None
    if value.startswith("/") or value.startswith("//") or SCHEME_RE.match(value):
        return None
    pure = PurePosixPath(value)
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        return None
    return parts


def _check_html(scans: Sequence[ExportScan], logical: Sequence[dict]) -> dict:
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
        for relative, html_label, text in scan.html_documents:
            html_count += 1
            parser = _SourceParser()
            try:
                parser.feed(text)
            except Exception as exc:
                raise AuditError("HTML metadata could not be parsed") from exc
            for ordinal, raw in enumerate(parser.sources, 1):
                reference_count += 1
                parts = _safe_reference(raw)
                evidence = "{}::reference-{}".format(html_label, ordinal)
                if parts is None:
                    unexpected.append(evidence)
                    continue
                target = relative[:-1] + parts
                filename_key = alias(parts[-1])
                if target in scan_media_paths and scan_media_paths[target] == filename_key:
                    referenced.add(filename_key)
                elif target in scan.regular_files:
                    unexpected.append(evidence)
                else:
                    missing.append("export-{}/{}".format(scan.index, "/".join(target)))
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
        if URL_RE.search(value):
            raise AuditError("URL-like report value rejected")
        if value.startswith("/"):
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


def _rename_directory_exclusive(
    source_parent_fd: int,
    source: str,
    destination_parent_fd: int,
    destination: str,
) -> None:
    if _RENAMEATX_NP is None:
        raise AuditError("exclusive report publication is unavailable")
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
            raise AuditError("output already exists")
        raise OSError(error, os.strerror(error))


def _remove_report_stage(parent_fd: int, stage_name: str, names: Sequence[str]) -> None:
    try:
        stage_fd = os.open(
            stage_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return
    try:
        for name in names:
            try:
                os.unlink(name, dir_fd=stage_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(stage_fd)
    try:
        os.rmdir(stage_name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _write_exclusive_atomic(directory_fd: int, name: str, payload: bytes) -> None:
    _validate_component(name)
    temporary = ".tmp-{}".format(uuid.uuid4().hex)
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    linked = False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.link(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        linked = True
        os.fsync(directory_fd)
    except FileExistsError as exc:
        raise AuditError("report file already exists") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
    if not linked:
        raise AuditError("report file could not be installed")


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
        raise AuditError("at least one export is required")
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
        raise AuditError("limits must be positive integers")
    parent_fd, output_name, _ = _prepare_output_parent(Path(output), [Path(item) for item in exports])
    try:
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
        if not any(scan.histories for scan in scans):
            raise AuditError("no memories history was found")
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
                "Snapchat Memories Saver v0.1 local backup audit",
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
                "SHA-256 hashes and nonempty checks do not prove every image pixel or video frame decodes.",
                "ZIP integrity was not tested by this tool; it audits extracted directories only.",
                "No identifier matching is claimed; history matching uses media type and timestamp only.",
                "",
            ]
        )
        payloads = {
            "summary.json": (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            "manifest.csv": _csv_payload(manifest, manifest_fields),
            "missing_history_records.csv": _csv_payload(missing_rows, missing_fields),
            "report.txt": report.encode("utf-8"),
        }
        _validate_report_value(summary)
        _validate_report_value(manifest)
        _validate_report_value(missing_rows)
        _validate_report_value(report)
        report_names = (
            "summary.json",
            "manifest.csv",
            "missing_history_records.csv",
            "report.txt",
        )
        stage_name = ".audit-stage-{}".format(uuid.uuid4().hex)
        os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
        published = False
        try:
            output_fd = os.open(
                stage_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                os.fchmod(output_fd, 0o700)
                for name in report_names:
                    _write_exclusive_atomic(output_fd, name, payloads[name])
                os.fsync(output_fd)
            finally:
                os.close(output_fd)
            _rename_directory_exclusive(
                parent_fd, stage_name, parent_fd, output_name
            )
            published = True
            os.fsync(parent_fd)
        finally:
            if not published:
                _remove_report_stage(parent_fd, stage_name, report_names)
        return summary
    finally:
        os.close(parent_fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args(argv)
    try:
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
    except (AuditError, OSError, ValueError):
        print("audit failed: unsafe or invalid input/output", file=sys.stderr)
        return 2
    print("audit status: {}".format(summary["status"]))
    return 0 if summary["status"] == "complete" else 3


if __name__ == "__main__":
    raise SystemExit(main())
