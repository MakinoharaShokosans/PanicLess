"""受来源边界约束的 UTF-8 文档扫描、快照与标题/段落切块。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from baseagent_config.models import KnowledgeSource, RetrievalConfig
from tools.retrieval.records import KNOWLEDGE_INDEX, Chunk, Document, Section

_FORMATS = frozenset({".md", ".markdown", ".txt"})
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


@dataclass(frozen=True, slots=True)
class ScanResult:
    documents: tuple[Document, ...]
    chunks: tuple[Chunk, ...]
    errors: tuple[str, ...]
    scanned_paths: tuple[str, ...]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identity(*parts: str | int) -> str:
    return _hash(json.dumps(parts, ensure_ascii=False, separators=(",", ":")))


def _files(root: Path, maximum: int) -> tuple[list[Path], list[str]]:
    """规范路径去重；任何遍历失败都使调用方禁止发布和 prune。"""
    found: set[Path] = set()
    visited: set[Path] = set()
    errors: list[str] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        if directory in visited:
            continue
        visited.add(directory)
        try:
            with os.scandir(directory) as entries:
                ordered = sorted(entries, key=lambda entry: entry.name)
            for entry in ordered:
                path = Path(entry.path)
                try:
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(root):
                        if entry.is_dir() or path.suffix.lower() in _FORMATS:
                            errors.append(f"{path.relative_to(root).as_posix()}: source_boundary")
                        continue
                    if entry.is_dir():
                        pending.append(resolved)
                    elif path.suffix.lower() in _FORMATS:
                        if resolved.suffix.lower() not in _FORMATS:
                            errors.append(
                                f"{path.relative_to(root).as_posix()}: unsupported_target_format"
                            )
                            continue
                        if not entry.is_file():
                            errors.append(f"{path.relative_to(root).as_posix()}: not_regular_file")
                            continue
                        found.add(resolved)
                        if len(found) > maximum:
                            errors.append(f"source: max_files_exceeded ({maximum})")
                            return sorted(found), errors
                except (OSError, RuntimeError):
                    errors.append(f"{path.relative_to(root).as_posix()}: scan_failed")
        except OSError:
            errors.append(f"{directory.relative_to(root).as_posix()}: scan_failed")
    return sorted(found), errors


def _heading_marks(text: str) -> list[tuple[int, int, str]]:
    """只识别围栏外的 ATX/Setext 标题；偏移以规范化快照为准。"""
    lines = text.splitlines(keepends=True)
    offsets = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    marks: list[tuple[int, int, str]] = []
    fence: tuple[str, int] | None = None
    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\n")
        marker = _FENCE.match(line)
        if fence is not None:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not marker[2].strip()
            ):
                fence = None
            index += 1
            continue
        if marker:
            fence = (marker[1][0], len(marker[1]))
            index += 1
            continue
        atx = _ATX.match(line)
        if atx:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", atx[2] or "").strip()
            marks.append((offsets[index], len(atx[1]), title))
        elif line.strip() and not line.startswith(("    ", "\t")) and index + 1 < len(lines):
            underline = _SETEXT.match(lines[index + 1].rstrip("\n"))
            if underline:
                marks.append((offsets[index], 1 if underline[1][0] == "=" else 2, line.strip()))
                index += 1
        index += 1
    return marks


def _sections(text: str, doc_id: str, markdown: bool) -> tuple[Section, ...]:
    marks = _heading_marks(text) if markdown else []
    sections = []
    stack: list[tuple[int, str]] = []
    boundaries: list[tuple[int, tuple[str, ...]]] = []
    if not marks or marks[0][0] > 0:
        boundaries.append((0, ()))
    for start, level, title in marks:
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        boundaries.append((start, tuple(name for _, name in stack)))
    for ordinal, (start, heading_path) in enumerate(boundaries):
        end = boundaries[ordinal + 1][0] if ordinal + 1 < len(boundaries) else len(text)
        sections.append(Section(_identity(doc_id, "section", ordinal), heading_path, start, end))
    return tuple(sections)


def _blocks(text: str, section: Section, markdown: bool) -> list[tuple[int, int]]:
    """连续段落和完整围栏组成原文区间，空白仍留在快照坐标中。"""
    blocks: list[tuple[int, int]] = []
    start = offset = section.char_start
    fence: tuple[str, int] | None = None
    for raw in text[section.char_start : section.char_end].splitlines(keepends=True):
        line = raw.rstrip("\n")
        marker = _FENCE.match(line) if markdown else None
        if fence is not None:
            offset += len(raw)
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not marker[2].strip()
            ):
                blocks.append((start, offset))
                start = offset
                fence = None
            continue
        if marker:
            if offset > start:
                blocks.append((start, offset))
            start = offset
            fence = (marker[1][0], len(marker[1]))
        offset += len(raw)
        if fence is None and not line.strip():
            blocks.append((start, offset))
            start = offset
    if offset > start:
        blocks.append((start, offset))
    return blocks


def _core_ranges(
    text: str, blocks: list[tuple[int, int]], size: int, overlap: int
) -> list[tuple[int, int]]:
    """先生成不重叠正文区间；可容纳的段落/短代码块不从中间拆开。"""
    target = size - overlap
    atoms = []
    for start, end in blocks:
        if end - start <= size:
            atoms.append((start, end))
            continue
        while start < end:
            stop = min(start + target, end)
            if stop < end:
                newline = text.rfind("\n", start, stop)
                if newline >= start:
                    stop = newline + 1
            atoms.append((start, stop))
            start = stop
    ranges: list[tuple[int, int]] = []
    for start, end in atoms:
        if ranges and end - ranges[-1][0] <= target:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    return ranges


def _chunks(document: Document, config: RetrievalConfig, markdown: bool) -> list[Chunk]:
    chunks = []
    line_starts = [0, *[i + 1 for i, char in enumerate(document.text) if char == "\n"]]
    for section in document.sections:
        ranges = _core_ranges(
            document.text,
            _blocks(document.text, section, markdown),
            config.chunk_chars,
            config.overlap_chars,
        )
        for core_start, end in ranges:
            if not document.text[core_start:end].strip():
                continue
            prefix = min(
                config.overlap_chars,
                config.chunk_chars - (end - core_start),
                core_start - section.char_start,
            )
            start = core_start - prefix
            content = document.text[start:end]
            ordinal = len(chunks)
            chunks.append(
                Chunk(
                    chunk_id=_identity(
                        document.doc_id, document.revision, section.section_id, ordinal
                    ),
                    doc_id=document.doc_id,
                    revision=document.revision,
                    ordinal=ordinal,
                    section_id=section.section_id,
                    heading_path=section.heading_path,
                    line_start=bisect_right(line_starts, start),
                    line_end=bisect_right(line_starts, end - 1),
                    char_start=start,
                    char_end=end,
                    text=content,
                    text_hash=_hash(content),
                )
            )
    return chunks


def scan_source(source: KnowledgeSource, config: RetrievalConfig) -> ScanResult:
    """读取完整来源；errors 非空时调用方必须拒绝新代发布和缺失文件清理。"""
    try:
        root = Path(source.root_path).expanduser().resolve(strict=True)
        if root.is_dir():
            files, errors = _files(root, source.max_files)
            base = root
        elif root.is_file() and root.suffix.lower() in _FORMATS:
            files, errors = [root], []
            base = root.parent
        else:
            return ScanResult((), (), ("source: unsupported_or_nonregular_root",), ())
    except (OSError, RuntimeError):
        return ScanResult((), (), ("source: missing_or_inaccessible_root",), ())
    scanned = tuple(path.relative_to(base).as_posix() for path in files)
    documents: list[Document] = []
    chunks: list[Chunk] = []
    for path in files[: source.max_files]:
        relative = path.relative_to(base).as_posix()
        try:
            with path.open("rb") as handle:
                raw = handle.read(source.max_file_bytes + 1)
                modified = os.fstat(handle.fileno()).st_mtime
            if len(raw) > source.max_file_bytes:
                errors.append(f"{relative}: max_file_bytes_exceeded ({source.max_file_bytes})")
                continue
            text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            if any(ord(char) < 32 and char not in "\n\t" for char in text):
                errors.append(f"{relative}: binary_content")
                continue
        except UnicodeDecodeError:
            errors.append(f"{relative}: invalid_utf8")
            continue
        except OSError:
            errors.append(f"{relative}: read_failed")
            continue
        doc_id = _identity(KNOWLEDGE_INDEX, source.source_id, relative)
        content_hash = _hash(text)
        markdown = path.suffix.lower() != ".txt"
        sections = _sections(text, doc_id, markdown)
        title = next((s.heading_path[-1] for s in sections if s.heading_path), path.stem)
        document = Document(
            doc_id=doc_id,
            workspace_id=KNOWLEDGE_INDEX,
            source_id=source.source_id,
            relative_path=relative,
            source_root=str(root),
            title=title,
            content_hash=content_hash,
            revision=content_hash,
            text=text,
            sections=sections,
            updated_at=modified,
        )
        document_chunks = _chunks(document, config, markdown)
        if len(chunks) + len(document_chunks) > config.max_chunks:
            errors.append(f"{relative}: max_chunks_exceeded ({config.max_chunks})")
            break
        documents.append(document)
        chunks.extend(document_chunks)
    return ScanResult(tuple(documents), tuple(chunks), tuple(errors), scanned)
