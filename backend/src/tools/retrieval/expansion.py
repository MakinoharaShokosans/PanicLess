"""从索引快照扩展命中上下文；先分配命中正文，再分配邻域字符预算。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from baseagent_config.models import ContextExpansionConfig
from tools.retrieval.records import Chunk, ContextBlock, Document, Hit


@dataclass(slots=True)
class _Span:
    document: Document
    start: int
    end: int
    ranks: set[int]


def _key(document: Document) -> tuple[str, str, str, str]:
    return document.workspace_id, document.source_id, document.doc_id, document.revision


def _merge(spans: list[_Span], maximum: int) -> list[_Span]:
    ordered = sorted(spans, key=lambda s: (_key(s.document), s.start, s.end))
    result: list[_Span] = []
    for span in ordered:
        if (
            result
            and _key(result[-1].document) == _key(span.document)
            and span.start <= result[-1].end
            and max(result[-1].end, span.end) - result[-1].start <= maximum
        ):
            result[-1].end = max(result[-1].end, span.end)
            result[-1].ranks.update(span.ranks)
        else:
            result.append(span)
    return result


def _desired(
    hit: Hit, siblings: list[Chunk], config: ContextExpansionConfig
) -> tuple[int, int, str, bool]:
    anchor = hit.chunk
    if not config.enabled:
        return anchor.char_start, anchor.char_end, "disabled", False
    section = next((s for s in hit.document.sections if s.section_id == anchor.section_id), None)
    if section is None:
        return anchor.char_start, anchor.char_end, "unavailable", True
    fallback = False
    if config.mode == "section":
        if section.char_end - section.char_start <= config.max_chars_per_block:
            return section.char_start, section.char_end, "section", False
        fallback = True
    selected = [anchor]
    for candidate in siblings:
        if not anchor.ordinal - config.before <= candidate.ordinal <= anchor.ordinal + config.after:
            continue
        if config.same_section_only and candidate.section_id != anchor.section_id:
            continue
        selected.append(candidate)
    return (
        min(c.char_start for c in selected),
        max(c.char_end for c in selected),
        "adjacent",
        fallback,
    )


def expand_context(
    hits: list[Hit], chunks: tuple[Chunk, ...], config: ContextExpansionConfig
) -> tuple[list[ContextBlock], bool]:
    """不改变命中排序/分数；context_block_id 指向含该命中最多正文的块。

    大合并区间分成有界块；同一个命中横跨块时，每个块保留该 anchor 的引用。
    正文预算不足时保留空块和原命中身份，实际字符范围为空。
    """
    if not hits:
        return [], False
    desired: dict[int, tuple[int, int, str, bool]] = {}
    siblings: dict[tuple[str, str], list[Chunk]] = {}
    for chunk in chunks:
        siblings.setdefault((chunk.doc_id, chunk.revision), []).append(chunk)
    for values in siblings.values():
        values.sort(key=lambda c: c.ordinal)
    cores: list[_Span] = []
    unavailable: set[int] = set()
    for rank, hit in enumerate(hits):
        anchor, document = hit.chunk, hit.document
        if (
            anchor.char_start < 0
            or anchor.char_end > len(document.text)
            or anchor.char_end <= anchor.char_start
            or document.text[anchor.char_start : anchor.char_end] != anchor.text
        ):
            unavailable.add(rank)
            desired[rank] = (anchor.char_start, anchor.char_end, "unavailable", True)
            continue
        desired[rank] = _desired(hit, siblings.get((anchor.doc_id, anchor.revision), []), config)
        cores.append(_Span(document, anchor.char_start, anchor.char_end, {rank}))

    remaining = config.max_total_chars
    selected: list[_Span] = []
    # 依命中排名分配原始正文，减去已展示区间；低排名的前缀不能挤掉高排名正文。
    for span in cores:
        uncovered = [(span.start, span.end)]
        for present in selected:
            if _key(present.document) != _key(span.document):
                continue
            segments = []
            for start, end in uncovered:
                if present.end <= start or present.start >= end:
                    segments.append((start, end))
                else:
                    if start < present.start:
                        segments.append((start, present.start))
                    if end > present.end:
                        segments.append((present.end, end))
            uncovered = segments
        for start, end in uncovered:
            while start < end and remaining:
                stop = min(end, start + config.max_chars_per_block, start + remaining)
                selected.append(_Span(span.document, start, stop, set(span.ranks)))
                remaining -= stop - start
                start = stop
    selected = _merge(selected, config.max_chars_per_block)

    # 旧索引缺少快照信息时，仅使用保存的命中正文，不去读磁盘。
    legacy: list[tuple[int, str]] = []
    for rank in sorted(unavailable):
        content = hits[rank].chunk.text[: min(config.max_chars_per_block, remaining)]
        legacy.append((rank, content))
        remaining -= len(content)

    # 所有原始命中已获得预算后，按排名、距命中边界的字符距离扩展。
    for rank, _hit in enumerate(hits):
        if rank in unavailable or remaining <= 0:
            continue
        left, right, _, _ = desired[rank]
        for span in [s for s in selected if rank in s.ranks]:
            room = min(config.max_chars_per_block - (span.end - span.start), remaining)
            if room <= 0:
                continue
            others = [
                s for s in selected if s is not span and _key(s.document) == _key(span.document)
            ]
            floor = max([left, *[s.end for s in others if s.end <= span.start]])
            ceiling = min([right, *[s.start for s in others if s.start >= span.end]])
            before = max(0, span.start - floor)
            after = max(0, ceiling - span.end)
            take_before = min(before, (room + 1) // 2)
            take_after = min(after, room - take_before)
            take_before = min(before, room - take_after)
            span.start -= take_before
            span.end += take_after
            remaining -= take_before + take_after
    selected = _merge(selected, config.max_chars_per_block)
    selected.sort(key=lambda s: (min(s.ranks), s.start))
    blocks: list[ContextBlock] = []
    for span in selected:
        doc = span.document
        ranks = sorted(
            r
            for r, h in enumerate(hits)
            if _key(h.document) == _key(doc)
            and h.chunk.char_start < span.end
            and h.chunk.char_end > span.start
        )
        modes = {desired[r][2] for r in ranks}
        truncated = any(span.start > desired[r][0] or span.end < desired[r][1] for r in ranks)
        included = [
            c.chunk_id
            for c in siblings.get((doc.doc_id, doc.revision), [])
            if c.char_start < span.end and c.char_end > span.start
        ]
        identity = hashlib.sha256(
            f"{doc.doc_id}:{doc.revision}:{span.start}:{span.end}".encode()
        ).hexdigest()[:24]
        blocks.append(
            ContextBlock(
                block_id=identity,
                doc_id=doc.doc_id,
                revision=doc.revision,
                source_id=doc.source_id,
                relative_path=doc.relative_path,
                anchor_chunk_ids=[hits[r].chunk.chunk_id for r in ranks],
                included_chunk_ids=included,
                char_start=span.start,
                char_end=span.end,
                line_start=doc.text.count("\n", 0, span.start) + 1,
                line_end=doc.text.count("\n", 0, span.end - 1) + 1,
                text=doc.text[span.start : span.end],
                expansion_mode=next(iter(modes)) if len(modes) == 1 else "mixed",
                truncated=truncated,
                expansion_fallback=any(desired[r][3] for r in ranks),
            )
        )
    for rank, content in legacy:
        hit = hits[rank]
        anchor, doc = hit.chunk, hit.document
        blocks.append(
            ContextBlock(
                block_id=f"unavailable_{anchor.chunk_id}",
                doc_id=doc.doc_id,
                revision=doc.revision,
                source_id=doc.source_id,
                relative_path=doc.relative_path,
                anchor_chunk_ids=[anchor.chunk_id],
                included_chunk_ids=[anchor.chunk_id] if content else [],
                char_start=anchor.char_start,
                char_end=anchor.char_start + len(content),
                line_start=anchor.line_start,
                line_end=anchor.line_start + content.rstrip("\n").count("\n"),
                text=content,
                expansion_mode="unavailable",
                truncated=len(content) < len(anchor.text),
                expansion_fallback=True,
            )
        )
    for rank, hit in enumerate(hits):
        matches = [b for b in blocks if hit.chunk.chunk_id in b.anchor_chunk_ids]
        if not matches:
            anchor, doc = hit.chunk, hit.document
            block = ContextBlock(
                block_id=f"omitted_{anchor.chunk_id}",
                doc_id=doc.doc_id,
                revision=doc.revision,
                source_id=doc.source_id,
                relative_path=doc.relative_path,
                anchor_chunk_ids=[anchor.chunk_id],
                included_chunk_ids=[],
                char_start=anchor.char_start,
                char_end=anchor.char_start,
                line_start=anchor.line_start,
                line_end=anchor.line_start,
                text="",
                expansion_mode=desired[rank][2],
                truncated=True,
                expansion_fallback=desired[rank][3],
            )
            blocks.append(block)
            matches = [block]
        best = max(
            matches,
            key=lambda b: max(
                0, min(b.char_end, hit.chunk.char_end) - max(b.char_start, hit.chunk.char_start)
            ),
        )
        hit.context_block_id = best.block_id
    return blocks, any(block.truncated for block in blocks)
