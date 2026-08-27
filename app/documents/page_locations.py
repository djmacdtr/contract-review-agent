from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from app.core.errors import WorkflowError
from app.documents.models import DocumentBlock, DocumentLocation, ParsedDocument
from app.documents.normalization import normalize_text

LocationKey = tuple[int | None, int | None, int | None, int | None]


@dataclass(frozen=True)
class _Unit:
    kind: str
    text: str
    location: DocumentLocation
    pages: tuple[int, ...] = ()


@dataclass(frozen=True)
class DocxPageLocationSidecar:
    """Internal DOCX logical-location to rendered-page mapping.

    The sidecar keeps the logical-to-physical binding separate from public
    result serialization. The binding is copied onto parsed structures before
    comparison; the sidecar remains available for any secondary evidence.
    """

    file_id: str
    page_count: int
    mappings: dict[LocationKey, tuple[int, ...]]
    required_location_count: int
    candidate_mapping_count: int
    local_structure_count: int
    external_structure_count: int
    external_detail_page_count: int
    structure_mappings: dict[str, tuple[int, ...]] = field(default_factory=dict)
    unmapped_structures: tuple[dict[str, Any], ...] = ()

    @property
    def mapped_location_count(self) -> int:
        return len(self.mappings)

    @property
    def coverage(self) -> float:
        if not self.required_location_count:
            return 1.0
        return self.mapped_location_count / self.required_location_count

    @property
    def unmapped_location_count(self) -> int:
        return len(self.unmapped_structures) or max(
            0, self.required_location_count - self.mapped_location_count
        )

    def pages_for(self, location: DocumentLocation | dict[str, Any]) -> tuple[int, ...] | None:
        if isinstance(location, dict):
            location = DocumentLocation.model_validate(location)
        if location.physical_pages:
            return location.physical_pages
        structure_id = _structure_id(location)
        if structure_id and structure_id in self.structure_mappings:
            return self.structure_mappings[structure_id]
        return self.mappings.get(_location_key(location))

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "source": "EXTERNAL_DOCUMENT_PARSER",
            "page_count": self.page_count,
            "mapped_location_count": self.mapped_location_count,
            "required_location_count": self.required_location_count,
            "unmapped_location_count": self.unmapped_location_count,
            "candidate_mapping_count": self.candidate_mapping_count,
            "local_structure_count": self.local_structure_count,
            "external_structure_count": self.external_structure_count,
            "external_detail_page_count": self.external_detail_page_count,
            "coverage": round(self.coverage, 6),
        }


def _location_key(location: DocumentLocation) -> LocationKey:
    # Page is intentionally excluded: this key identifies the local DOCX
    # logical unit before physical pagination is added.
    return (
        location.paragraph_index,
        location.table_index,
        location.row,
        location.column,
    )


def _structure_id(location: DocumentLocation) -> str | None:
    if location.structure_id:
        return location.structure_id
    if location.table_index is not None:
        if location.row is not None and location.column is not None:
            return f"table_cell:{location.table_index}:{location.row}:{location.column}"
        return f"table:{location.table_index}"
    if location.paragraph_index is not None:
        return f"paragraph:{location.paragraph_index}"
    return None


def _structure_kind(location: DocumentLocation) -> str:
    if location.table_index is not None and location.row is not None:
        return "TABLE_CELL"
    if location.table_index is not None:
        return "TABLE"
    if location.paragraph_index is not None:
        return "PARAGRAPH"
    return "UNKNOWN"


def _structure_descriptor(
    location: DocumentLocation,
    *,
    candidate_count: int = 0,
    candidate_pages: tuple[int, ...] = (),
    diagnosis: str = "UNCLASSIFIED",
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "structure_id": _structure_id(location),
        "kind": _structure_kind(location),
        "candidate_count": candidate_count,
        "candidate_pages": list(candidate_pages),
        "diagnosis": diagnosis,
    }
    if location.paragraph_index is not None and location.table_index is None:
        descriptor["paragraph_index"] = location.paragraph_index
    if location.table_index is not None:
        descriptor["table_index"] = location.table_index
    if location.row is not None:
        descriptor["row"] = location.row
    if location.column is not None:
        descriptor["column"] = location.column
    return descriptor


def _fail(
    failure_stage: str,
    failure_code: str,
    **details: Any,
) -> None:
    raise WorkflowError(
        "DOCX_PAGE_LOCATION_INCOMPLETE",
        "DOCX 真实页码解析或映射未能可靠完成",
        details={
            "failure_stage": failure_stage,
            "failure_code": failure_code,
            **details,
        },
    )


def _mapping_failure_details(sidecar: DocxPageLocationSidecar) -> dict[str, Any]:
    """Return only safe, structural diagnostics for a public mapping failure."""

    return {
        "page_count": sidecar.page_count,
        "local_structure_count": sidecar.local_structure_count,
        "external_structure_count": sidecar.external_structure_count,
        "external_detail_page_count": sidecar.external_detail_page_count,
        "candidate_mapping_count": sidecar.candidate_mapping_count,
        "mapped_location_count": sidecar.mapped_location_count,
        "unmapped_location_count": sidecar.unmapped_location_count,
        "unmapped_structures": [dict(item) for item in sidecar.unmapped_structures],
    }


def _compact(value: str) -> str:
    return normalize_text(value).replace(" ", "")


def _table_text(block: DocumentBlock) -> str:
    if block.table is None:
        return block.raw_text
    return "\n".join(
        "\t".join(cell.raw_text for cell in row.cells) for row in block.table.rows
    )


def page_location_structure_count(document: ParsedDocument) -> int:
    return len(document.blocks) + sum(
        len(row.cells)
        for block in document.blocks
        if block.table is not None
        for row in block.table.rows
    )


def _logical_blocks(document: ParsedDocument) -> list[DocumentBlock]:
    return [
        block
        for block in sorted(document.blocks, key=lambda item: item.order)
        if block.type in {"PARAGRAPH", "TABLE"}
        and (block.type == "TABLE" or block.normalized_text)
    ]


def _local_units(document: ParsedDocument) -> list[_Unit]:
    units: list[_Unit] = []
    for block in _logical_blocks(document):
        if block.type == "PARAGRAPH" and block.normalized_text:
            units.append(_Unit("PARAGRAPH", block.normalized_text, block.location))
        elif block.type == "TABLE" and block.table is not None:
            units.append(_Unit("TABLE", normalize_text(_table_text(block)), block.location))
    return units


def _external_units(document: ParsedDocument) -> list[_Unit]:
    units: list[_Unit] = []
    for block in _logical_blocks(document):
        if block.type == "PARAGRAPH" and block.normalized_text:
            page = block.location.page
            units.append(
                _Unit(
                    "PARAGRAPH",
                    block.normalized_text,
                    block.location,
                    (page,) if page is not None else (),
                )
            )
        elif block.type == "TABLE" and block.table is not None:
            page = block.location.page
            units.append(
                _Unit(
                    "TABLE",
                    normalize_text(_table_text(block)),
                    block.location,
                    (page,) if page is not None else (),
                )
            )
    return units


def _similarity(left: str, right: str) -> float:
    left_compact = _compact(left)
    right_compact = _compact(right)
    if not left_compact or not right_compact:
        return 0.0
    if left_compact == right_compact:
        return 1.0
    if left_compact in right_compact or right_compact in left_compact:
        return min(len(left_compact), len(right_compact)) / max(
            len(left_compact), len(right_compact)
        )
    return SequenceMatcher(None, left_compact, right_compact, autojunk=False).ratio()


def _compatible(left: _Unit, right: _Unit) -> bool:
    if left.kind != right.kind:
        return False
    score = _similarity(left.text, right.text)
    if score >= 0.82:
        return True
    # A long local paragraph can be split into multiple external page details.
    # The span matcher below evaluates the combined text, so this relaxed
    # single-unit threshold is intentionally not used for final spans.
    return len(_compact(left.text)) >= 16 and score >= 0.72


def _candidate_spans(local: _Unit, external: list[_Unit], start: int) -> list[tuple[int, float]]:
    if start >= len(external) or external[start].kind != local.kind:
        return []
    candidates: list[tuple[int, float]] = []
    combined: list[str] = []
    for end in range(start, min(len(external), start + 8)):
        current = external[end]
        if current.kind != local.kind:
            break
        combined.append(current.text)
        candidate = _Unit(local.kind, " ".join(combined), current.location)
        score = _similarity(local.text, candidate.text)
        if (end == start and _compatible(local, current)) or score >= 0.82:
            candidates.append((end + 1, score))
    return candidates


@dataclass(frozen=True)
class _Alignment:
    pairs: list[tuple[int, int, int]]
    candidate_mapping_count: int
    unmapped_local_count: int


_DP_STATE_LIMIT = 200_000


def _page_candidates(
    local: _Unit,
    external: list[_Unit],
) -> list[tuple[int, int, float, tuple[int, ...]]]:
    candidates: list[tuple[int, int, float, tuple[int, ...]]] = []
    for start in range(len(external)):
        for end, score in _candidate_spans(local, external, start):
            pages = tuple(
                sorted(
                    {
                        page
                        for unit in external[start:end]
                        for page in unit.pages
                    }
                )
            )
            if pages:
                candidates.append((start, end, score, pages))
    return candidates


def _overlaps(
    start: int,
    end: int,
    ranges: list[tuple[int, int]],
) -> bool:
    return any(start < other_end and end > other_start for other_start, other_end in ranges)


def _deterministic_fallback(
    local: list[_Unit],
    external: list[_Unit],
    alignment: _Alignment,
) -> _Alignment:
    """Add only candidates whose exact physical page set is unambiguous.

    This intentionally accepts multiple text candidates when they all resolve
    to the same real page set. It never invents a page from sequence position,
    document length, or a neighboring page.
    """

    pairs = list(alignment.pairs)
    matched_local = {local_index for local_index, _start, _end in pairs}
    occupied = [(start, end) for _local, start, end in pairs]
    fallback_count = 0
    for local_index, unit in enumerate(local):
        if local_index in matched_local:
            continue
        candidates = [
            candidate
            for candidate in _page_candidates(unit, external)
            if not _overlaps(candidate[0], candidate[1], occupied)
        ]
        page_sets = {candidate[3] for candidate in candidates}
        if len(page_sets) != 1:
            continue
        selected = sorted(
            candidates,
            key=lambda candidate: (-candidate[2], candidate[1] - candidate[0], candidate[0]),
        )[0]
        pairs.append((local_index, selected[0], selected[1]))
        occupied.append((selected[0], selected[1]))
        fallback_count += 1
    pairs.sort(key=lambda pair: pair[0])
    return _Alignment(
        pairs=pairs,
        candidate_mapping_count=alignment.candidate_mapping_count + fallback_count,
        unmapped_local_count=len(local) - len(pairs),
    )


def _neighbor_anchor_fallback(
    local_blocks: list[DocumentBlock],
    mappings: dict[LocationKey, tuple[int, ...]],
) -> dict[LocationKey, tuple[int, ...]]:
    """Map only a structure enclosed by deterministic physical-page anchors.

    The first rule handles a structure between two already mapped structures on
    one page. The second handles the explicit page-boundary shape observed in
    DOCX: a short paragraph immediately before a uniquely mapped table on the
    next physical page. Both rules consume only pages returned by OCR; neither
    derives a page from a ratio, position, or document length.
    """

    additions: dict[LocationKey, tuple[int, ...]] = {}
    for index, block in enumerate(local_blocks):
        key = _location_key(block.location)
        if key in mappings:
            continue
        if index == 0 or index + 1 >= len(local_blocks):
            continue
        previous_pages = mappings.get(_location_key(local_blocks[index - 1].location))
        next_pages = mappings.get(_location_key(local_blocks[index + 1].location))
        if (
            previous_pages
            and next_pages
            and len(previous_pages) == 1
            and previous_pages == next_pages
        ):
            additions[key] = previous_pages
            continue
        if (
            block.type == "PARAGRAPH"
            and local_blocks[index + 1].type == "TABLE"
            and previous_pages
            and next_pages
            and len(previous_pages) == 1
            and len(next_pages) == 1
            and next_pages[0] == previous_pages[0] + 1
        ):
            additions[key] = next_pages
    return additions


def _linear_align_units(local: list[_Unit], external: list[_Unit]) -> _Alignment:
    exact_index: dict[tuple[str, str], list[int]] = {}
    for external_index, unit in enumerate(external):
        exact_index.setdefault((unit.kind, _compact(unit.text)), []).append(external_index)

    candidate_mapping_count = 0
    for unit in local:
        candidate_mapping_count += len(exact_index.get((unit.kind, _compact(unit.text)), []))

    pairs: list[tuple[int, int, int]] = []
    external_index = 0
    for local_index, unit in enumerate(local):
        exact_starts = [
            start
            for start in exact_index.get((unit.kind, _compact(unit.text)), [])
            if start >= external_index
        ]
        candidates: list[tuple[int, int, float]] = []
        if exact_starts:
            candidates = [(start, start + 1, 1.0) for start in exact_starts]
        else:
            for start in range(external_index, len(external)):
                candidates.extend(
                    (start, end, score)
                    for end, score in _candidate_spans(unit, external, start)
                )
        if not candidates:
            continue
        top_score = max(score for _start, _end, score in candidates)
        top = sorted(
            {
                (start, end)
                for start, end, score in candidates
                if abs(score - top_score) < 1e-9
            }
        )
        if len(top) > 1 and local_index + 1 < len(local):
            future = []
            for _start, end in top:
                future.append(
                    any(
                        _candidate_spans(local[local_index + 1], external, next_start)
                        for next_start in range(end, len(external))
                    )
                )
            if sum(future) == 1:
                top = [top[future.index(True)]]
        if len(top) != 1:
            continue
        start, end = top[0]
        if start < external_index:
            continue
        pairs.append((local_index, start, end))
        external_index = end
    return _Alignment(
        pairs=pairs,
        candidate_mapping_count=candidate_mapping_count,
        unmapped_local_count=len(local) - len(pairs),
    )


def _align_units(
    local: list[_Unit],
    external: list[_Unit],
    *,
    failure_stage: str,
) -> _Alignment:
    """Keep only high-confidence, monotonic and uniquely selected matches.

    Unmatched local units and ambiguous candidates are intentionally skipped.
    The public-result gate later decides whether a skipped unit is actually
    needed by a displayed piece of evidence.
    """

    if not local:
        return _Alignment([], 0, 0)
    if not external:
        return _Alignment([], 0, len(local))
    if len(local) * len(external) > _DP_STATE_LIMIT:
        return _linear_align_units(local, external)

    candidate_mapping_count = sum(
        len(_candidate_spans(unit, external, external_index))
        for unit in local
        for external_index in range(len(external))
    )

    candidate_cache: dict[tuple[int, int], list[tuple[int, float]]] = {}

    def candidates(local_index: int, external_index: int) -> list[tuple[int, float]]:
        key = (local_index, external_index)
        if key not in candidate_cache:
            candidate_cache[key] = _candidate_spans(
                local[local_index], external, external_index
            )
        return candidate_cache[key]

    # The old recursive suffix DP overflowed on large tables. An explicit
    # dependency stack keeps the same deterministic objective without a
    # recursion-depth limit.
    values: dict[tuple[int, int], tuple[int, float]] = {}
    stack: list[tuple[int, int, bool]] = [(0, 0, False)]
    while stack:
        local_index, external_index, expanded = stack.pop()
        key = (local_index, external_index)
        if key in values:
            continue
        if local_index == len(local) or external_index == len(external):
            values[key] = (0, 0.0)
            continue
        if not expanded:
            stack.append((local_index, external_index, True))
            dependencies = [
                (local_index + 1, external_index),
                (local_index, external_index + 1),
            ]
            dependencies.extend(
                (local_index + 1, end) for end, _score in candidates(local_index, external_index)
            )
            for dependency in reversed(dependencies):
                if dependency not in values:
                    stack.append((*dependency, False))
            continue
        choices = [
            values[(local_index + 1, external_index)],
            values[(local_index, external_index + 1)],
        ]
        choices.extend(
            (
                values[(local_index + 1, end)][0] + 1,
                values[(local_index + 1, end)][1] + score,
            )
            for end, score in candidates(local_index, external_index)
        )
        values[key] = max(choices)

    def best(local_index: int, external_index: int) -> tuple[int, float]:
        return values[(local_index, external_index)]

    def is_best(value: tuple[int, float], expected: tuple[int, float]) -> bool:
        return value[0] == expected[0] and abs(value[1] - expected[1]) < 1e-9

    aligned: list[tuple[int, int, int]] = []
    local_index = 0
    external_index = 0
    while local_index < len(local):
        if external_index >= len(external):
            local_index += 1
            continue
        current = best(local_index, external_index)
        skip_local = best(local_index + 1, external_index)
        skip_external = best(local_index, external_index + 1)
        matches = [
            (end, score)
            for end, score in _candidate_spans(local[local_index], external, external_index)
            if is_best(
                (best(local_index + 1, end)[0] + 1, best(local_index + 1, end)[1] + score),
                current,
            )
        ]
        if (
            len(matches) == 1
            and not is_best(skip_local, current)
            and not is_best(skip_external, current)
        ):
            aligned.append((local_index, external_index, matches[0][0]))
            local_index += 1
            external_index = matches[0][0]
        elif (
            len(matches) > 1
            or is_best(skip_local, current)
            or (matches and is_best(skip_external, current))
        ):
            # Either this local unit is ambiguous or it is optional in the
            # best monotonic sequence. Leave the external cursor untouched so
            # later units can still use a reliable occurrence.
            local_index += 1
        else:
            external_index += 1
    return _Alignment(
        pairs=aligned,
        candidate_mapping_count=candidate_mapping_count,
        unmapped_local_count=len(local) - len(aligned),
    )


def _map_table_cells(
    local_blocks: list[DocumentBlock],
    external_blocks: list[DocumentBlock],
    table_alignment: list[tuple[int, int, int]],
    mappings: dict[LocationKey, tuple[int, ...]],
    *,
    failure_stage: str,
) -> None:
    for local_index, external_start, external_end in table_alignment:
        local_block = local_blocks[local_index]
        external_span = external_blocks[external_start:external_end]
        if local_block.table is None:
            continue
        external_cells = [
            cell
            for block in external_span
            if block.table is not None
            for row in block.table.rows
            for cell in row.cells
            if _compact(cell.raw_text)
        ]
        local_cells = [
            cell
            for row in local_block.table.rows
            for cell in row.cells
            if _compact(cell.raw_text)
        ]
        local_cell_units = [
            _Unit("CELL", cell.normalized_text, cell.location) for cell in local_cells
        ]
        external_cell_units = [
            _Unit(
                "CELL",
                cell.normalized_text,
                cell.location,
                (cell.location.page,) if cell.location.page is not None else (),
            )
            for cell in external_cells
        ]
        if local_cell_units:
            cell_alignment = _align_units(
                local_cell_units,
                external_cell_units,
                failure_stage=failure_stage,
            )
            for local_cell_index, external_cell_start, external_cell_end in cell_alignment.pairs:
                pages = tuple(
                    sorted(
                        {
                            page
                            for external_cell in external_cell_units[
                                external_cell_start:external_cell_end
                            ]
                            for page in external_cell.pages
                        }
                    )
                )
                if pages:
                    mappings[_location_key(local_cells[local_cell_index].location)] = (*pages,)

        table_pages = tuple(
            sorted(
                {
                    page
                    for block in external_span
                    for page in (block.location.page,)
                    if page is not None
                }
            )
        )
        if table_pages:
            for row in local_block.table.rows:
                for cell in row.cells:
                    if _location_key(cell.location) not in mappings:
                        # A cell without an independently reliable text match
                        # inherits the containing table's physical pages.
                        mappings[_location_key(cell.location)] = table_pages


def _unmapped_structure_descriptors(
    local_blocks: list[DocumentBlock],
    local_units: list[_Unit],
    external_units: list[_Unit],
    mappings: dict[LocationKey, tuple[int, ...]],
) -> tuple[dict[str, Any], ...]:
    local_units_by_id = {_structure_id(unit.location): unit for unit in local_units}
    locations: dict[LocationKey, DocumentLocation] = {}
    blocks_by_key: dict[LocationKey, DocumentBlock] = {}
    for block in local_blocks:
        key = _location_key(block.location)
        locations[key] = block.location
        blocks_by_key[key] = block
        if block.table is not None:
            for row in block.table.rows:
                for cell in row.cells:
                    locations[_location_key(cell.location)] = cell.location

    descriptors: list[dict[str, Any]] = []
    for key, location in locations.items():
        if key in mappings:
            continue
        kind = _structure_kind(location)
        unit = local_units_by_id.get(_structure_id(location))
        candidates = _page_candidates(unit, external_units) if unit is not None else []
        candidate_pages = tuple(
            sorted({page for _start, _end, _score, pages in candidates for page in pages})
        )
        exact_count = (
            sum(
                candidate.kind == unit.kind
                and _compact(candidate.text) == _compact(unit.text)
                for candidate in external_units
            )
            if unit is not None
            else 0
        )
        block = blocks_by_key.get(key)
        block_index = (
            next(
                (
                    index
                    for index, candidate in enumerate(local_blocks)
                    if _location_key(candidate.location) == key
                ),
                None,
            )
            if block is not None
            else None
        )
        previous_pages = (
            mappings.get(_location_key(local_blocks[block_index - 1].location))
            if block_index is not None and block_index > 0
            else None
        )
        next_pages = (
            mappings.get(_location_key(local_blocks[block_index + 1].location))
            if block_index is not None and block_index + 1 < len(local_blocks)
            else None
        )
        is_page_boundary_shape = bool(
            block is not None
            and block.type == "PARAGRAPH"
            and block_index is not None
            and block_index + 1 < len(local_blocks)
            and local_blocks[block_index + 1].type == "TABLE"
        )
        if kind == "TABLE_CELL":
            diagnosis = "TABLE_CELL"
        elif exact_count > 1:
            diagnosis = "REPEATED_TEXT"
        elif is_page_boundary_shape and previous_pages and next_pages and (
            previous_pages != next_pages
        ):
            diagnosis = "PAGE_BOUNDARY"
        elif previous_pages and next_pages and previous_pages == next_pages:
            diagnosis = "MERGED_OR_SPLIT_STRUCTURE"
        elif any(end - start > 1 for start, end, _score, _pages in candidates):
            diagnosis = "MERGED_OR_SPLIT_STRUCTURE"
        elif any(len(pages) > 1 for _start, _end, _score, pages in candidates):
            diagnosis = "PAGE_BOUNDARY"
        else:
            diagnosis = "UNCLASSIFIED"
        descriptors.append(
            _structure_descriptor(
                location,
                candidate_count=len(candidates),
                candidate_pages=candidate_pages,
                diagnosis=diagnosis,
            )
        )
    return tuple(descriptors)


def bind_docx_page_locations(
    document: ParsedDocument,
    sidecar: DocxPageLocationSidecar,
) -> None:
    """Bind exact physical pages to parsed logical structures before comparison."""

    def bind(location: DocumentLocation) -> None:
        if location.structure_id is None:
            location.structure_id = _structure_id(location)
        pages = sidecar.pages_for(location)
        if pages:
            location.physical_pages = pages
            location.page = pages[0]

    for block in document.blocks:
        bind(block.location)
        if block.table is not None:
            for row in block.table.rows:
                for cell in row.cells:
                    bind(cell.location)


def build_docx_page_location_sidecar(
    local_document: ParsedDocument,
    external_document: ParsedDocument,
) -> DocxPageLocationSidecar:
    local_structure_count = page_location_structure_count(local_document)
    external_structure_count = page_location_structure_count(external_document)
    external_detail_page_count = len(
        {
            block.location.page
            for block in external_document.blocks
            if block.location.page is not None
        }
    )
    if local_document.file_id != external_document.file_id:
        _fail(
            "PAGE_ID_VALIDATION",
            "FILE_ID_MISMATCH",
            page_count=external_document.page_count or 0,
            external_detail_page_count=external_detail_page_count,
            local_structure_count=local_structure_count,
            external_structure_count=external_structure_count,
            candidate_mapping_count=0,
            unmapped_location_count=local_structure_count,
        )
    page_count = external_document.page_count
    if page_count is None or page_count < 1:
        _fail(
            "PAGE_ID_VALIDATION",
            "EXTERNAL_PAGE_COUNT_MISSING",
            page_count=0,
            external_detail_page_count=external_detail_page_count,
            local_structure_count=local_structure_count,
            external_structure_count=external_structure_count,
            candidate_mapping_count=0,
            unmapped_location_count=local_structure_count,
        )
    metadata_page_ids = external_document.parser_metadata.get("page_ids")
    external_pages = {
        int(page)
        for page in (metadata_page_ids or [])
        if isinstance(page, (int, float)) and int(page) == page
    }
    if not external_pages:
        external_pages = {
            block.location.page
            for block in external_document.blocks
            if block.location.page is not None
        }
    expected_pages = set(range(1, page_count + 1))
    if external_pages != expected_pages:
        _fail(
            "PAGE_ID_VALIDATION",
            "EXTERNAL_PAGE_ID_INCOMPLETE",
            page_count=page_count,
            external_detail_page_count=external_detail_page_count,
            returned_page_count=len(external_pages),
            local_structure_count=local_structure_count,
            external_structure_count=external_structure_count,
            candidate_mapping_count=0,
            unmapped_location_count=local_structure_count,
        )

    local_units = _local_units(local_document)
    external_units = _external_units(external_document)
    if not local_units:
        return DocxPageLocationSidecar(
            file_id=local_document.file_id,
            page_count=page_count,
            mappings={},
            required_location_count=0,
            candidate_mapping_count=0,
            local_structure_count=local_structure_count,
            external_structure_count=external_structure_count,
            external_detail_page_count=external_detail_page_count,
        )
    if not external_units:
        _fail(
            "PARAGRAPH_MAPPING",
            "EXTERNAL_DETAIL_EMPTY",
            page_count=page_count,
            external_detail_count=len(external_document.blocks),
            local_structure_count=local_structure_count,
            external_structure_count=external_structure_count,
            candidate_mapping_count=0,
            unmapped_location_count=local_structure_count,
        )
    alignment = _align_units(
        local_units,
        external_units,
        failure_stage="PARAGRAPH_MAPPING",
    )
    alignment = _deterministic_fallback(local_units, external_units, alignment)
    mappings: dict[LocationKey, tuple[int, ...]] = {}
    local_blocks = _logical_blocks(local_document)
    external_blocks = _logical_blocks(external_document)
    # Top-level alignment indexes are based on the same filtered block stream.
    for local_index, external_start, external_end in alignment.pairs:
        pages = tuple(
            sorted(
                {
                    page
                    for block in external_blocks[external_start:external_end]
                    if block.location.page is not None
                    for page in (block.location.page,)
                }
            )
        )
        if pages:
            mappings[_location_key(local_blocks[local_index].location)] = pages
    neighbor_mappings = _neighbor_anchor_fallback(local_blocks, mappings)
    mappings.update(neighbor_mappings)

    table_local = [block for block in local_blocks if block.type == "TABLE"]
    table_external = [block for block in external_blocks if block.type == "TABLE"]
    table_local_units = [
        _Unit("TABLE", normalize_text(_table_text(block)), block.location)
        for block in table_local
    ]
    table_external_units = [
        _Unit("TABLE", normalize_text(_table_text(block)), block.location)
        for block in table_external
    ]
    table_alignment = (
        _deterministic_fallback(
            table_local_units,
            table_external_units,
            _align_units(
                table_local_units,
                table_external_units,
                failure_stage="TABLE_MAPPING",
            ),
        )
        if table_local
        else _Alignment([], 0, 0)
    )
    _map_table_cells(
        table_local,
        table_external,
        table_alignment.pairs,
        mappings,
        failure_stage="TABLE_MAPPING",
    )

    required_locations = {
        _location_key(block.location)
        for block in local_blocks
    }
    for block in local_blocks:
        if block.table is not None:
            required_locations.update(
                _location_key(cell.location)
                for row in block.table.rows
                for cell in row.cells
            )
    structure_mappings: dict[str, tuple[int, ...]] = {}
    for key, location in {
        _location_key(block.location): block.location
        for block in local_blocks
    }.items():
        pages = mappings.get(key)
        structure_id = _structure_id(location)
        if pages and structure_id:
            structure_mappings[structure_id] = pages
    for block in local_blocks:
        if block.table is None:
            continue
        for row in block.table.rows:
            for cell in row.cells:
                pages = mappings.get(_location_key(cell.location))
                structure_id = _structure_id(cell.location)
                if pages and structure_id:
                    structure_mappings[structure_id] = pages
    unmapped_structures = _unmapped_structure_descriptors(
        local_blocks,
        local_units,
        external_units,
        mappings,
    )
    return DocxPageLocationSidecar(
        file_id=local_document.file_id,
        page_count=page_count,
        mappings=mappings,
        required_location_count=len(required_locations),
        candidate_mapping_count=(
            alignment.candidate_mapping_count
            + table_alignment.candidate_mapping_count
            + len(neighbor_mappings)
        ),
        local_structure_count=local_structure_count,
        external_structure_count=external_structure_count,
        external_detail_page_count=external_detail_page_count,
        structure_mappings=structure_mappings,
        unmapped_structures=unmapped_structures,
    )


def _enrich_location(
    location: dict[str, Any],
    sidecar: DocxPageLocationSidecar,
) -> list[dict[str, Any]]:
    pages = sidecar.pages_for(location)
    if pages is None:
        _fail(
            "PUBLIC_EVIDENCE_MAPPING",
            "PUBLIC_LOCATION_UNMAPPED",
            **_mapping_failure_details(sidecar),
        )
    return [{**location, "page": page} for page in pages]


def _enrich_side(side: dict[str, Any], sidecar: DocxPageLocationSidecar) -> None:
    source_locations = side.get("locations") or [side.get("location")]
    source_locations = [item for item in source_locations if isinstance(item, dict)]
    if source_locations and all(
        isinstance(location.get("page"), int)
        and 1 <= location["page"] <= sidecar.page_count
        for location in source_locations
    ):
        side["location"] = source_locations[0]
        if len(source_locations) > 1:
            side["locations"] = source_locations
        return
    enriched: list[dict[str, Any]] = []
    for location in source_locations:
        enriched.extend(_enrich_location(location, sidecar))
    if not enriched:
        _fail(
            "PUBLIC_EVIDENCE_MAPPING",
            "PUBLIC_SIDE_LOCATION_MISSING",
            **_mapping_failure_details(sidecar),
        )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for location in enriched:
        key = tuple(sorted((name, repr(value)) for name, value in location.items()))
        if key not in seen:
            seen.add(key)
            deduped.append(location)
    side["location"] = deduped[0]
    side["locations"] = deduped


def _enrich_evidence_item(
    item: dict[str, Any],
    sidecars: dict[str, DocxPageLocationSidecar],
    *,
    required: bool,
) -> None:
    file_id = item.get("file_id") or item.get("source_file_id")
    if not file_id and isinstance(item.get("location"), dict):
        file_id = item["location"].get("file_id")
    if not file_id and isinstance(item.get("locations"), list):
        file_id = next(
            (
                location.get("file_id")
                for location in item["locations"]
                if isinstance(location, dict) and location.get("file_id")
            ),
            None,
        )
    sidecar = sidecars.get(file_id)
    if sidecar is None:
        return
    direct = item.get("location")
    locations = item.get("locations")
    if not required:
        # Non-public diagnostics remain available for JSON traceability. Page
        # enrichment is best effort for them and cannot block the report.
        if isinstance(locations, list):
            for location in locations:
                if isinstance(location, dict):
                    _maybe_enrich_location(location, sidecar)
        elif isinstance(direct, dict):
            _maybe_enrich_location(direct, sidecar)
        return
    source_locations = locations if isinstance(locations, list) else [direct]
    if source_locations and all(
        isinstance(location, dict)
        and isinstance(location.get("page"), int)
        and 1 <= location["page"] <= sidecar.page_count
        for location in source_locations
    ):
        return
    if isinstance(locations, list) and locations:
        enriched: list[dict[str, Any]] = []
        for location in locations:
            if isinstance(location, dict):
                enriched.extend(_enrich_location(location, sidecar))
        item["locations"] = enriched
        if enriched:
            item["location"] = enriched[0]
    elif isinstance(direct, dict):
        values = _enrich_location(direct, sidecar)
        item["location"] = values[0]
        item["locations"] = values
    elif (
        isinstance(item.get("page"), int)
        and 1 <= item["page"] <= sidecar.page_count
    ):
        # Parser warnings can already carry a validated physical page but no
        # logical paragraph/cell location to map.
        return
    elif required:
        _fail(
            "PUBLIC_EVIDENCE_MAPPING",
            "PUBLIC_EVIDENCE_LOCATION_MISSING",
            **_mapping_failure_details(sidecar),
        )


def _maybe_enrich_location(
    location: dict[str, Any], sidecar: DocxPageLocationSidecar
) -> None:
    pages = sidecar.pages_for(location)
    if pages is not None:
        location["page"] = pages[0]


def _set_missing_detail_pages(diff: dict[str, Any]) -> None:
    detail = diff.get("missing_detail")
    target = diff.get("target")
    if not isinstance(detail, dict) or not isinstance(target, dict):
        return
    locations = target.get("locations") or [target.get("location")]
    pages = [
        location.get("page")
        for location in locations
        if isinstance(location, dict) and isinstance(location.get("page"), int)
    ]
    if not pages:
        return
    ordered = sorted(set(pages))
    boundary = detail.get("boundary")
    if boundary == "START" and detail.get("target_anchor_after_page") is None:
        detail["target_anchor_after_page"] = ordered[0]
    elif boundary == "END" and detail.get("target_anchor_before_page") is None:
        detail["target_anchor_before_page"] = ordered[-1]
    elif boundary == "MIDDLE":
        if detail.get("target_anchor_before_page") is None:
            detail["target_anchor_before_page"] = ordered[0]
        if detail.get("target_anchor_after_page") is None and len(ordered) > 1:
            detail["target_anchor_after_page"] = ordered[-1]


def apply_docx_page_location_sidecars(
    result: dict[str, Any],
    sidecars: dict[str, DocxPageLocationSidecar],
) -> dict[str, Any]:
    """Add physical pages to public evidence without changing internal IDs."""

    if not sidecars:
        return result
    for file in result.get("files", []):
        sidecar = sidecars.get(file.get("file_id"))
        if sidecar is None:
            continue
        file["page_count"] = sidecar.page_count
        metadata = file.setdefault("parser_metadata", {})
        metadata["physical_page_numbers"] = True
        metadata["docx_page_location"] = sidecar.summary()

    for diff in result.get("diff_items", []):
        for side_name in ("baseline", "target"):
            side = diff.get(side_name)
            if isinstance(side, dict):
                sidecar = sidecars.get(side.get("file_id"))
                if sidecar is not None:
                    _enrich_side(side, sidecar)
        target_side = diff.get("target")
        if isinstance(target_side, dict):
            target_sidecar = sidecars.get(target_side.get("file_id"))
            if target_sidecar is not None:
                _set_missing_detail_pages(diff)
    for collection_name in ("risk_items", "review_items"):
        for item in result.get(collection_name, []):
            # Draft reports expose only risks tied to a displayed diff. Rule
            # and mapping diagnostics remain traceable in JSON, but are not
            # part of the public evidence cards and must not widen the page
            # mapping gate.
            required = collection_name == "risk_items" and bool(
                item.get("related_diff_ids")
            )
            for evidence in item.get("source_evidence", []):
                if isinstance(evidence, dict):
                    _enrich_evidence_item(evidence, sidecars, required=required)
    for check in result.get("rule_checks", []):
        location = check.get("location")
        if not isinstance(location, dict):
            continue
        sidecar = sidecars.get(location.get("file_id"))
        if sidecar is not None:
            _maybe_enrich_location(location, sidecar)
    for item in result.get("fact_matrix", []):
        candidates = []
        if isinstance(item.get("target_candidate"), dict):
            candidates.append(item["target_candidate"])
        candidates.extend(
            candidate
            for candidate in item.get("candidates", [])
            if isinstance(candidate, dict)
        )
        for candidate in candidates:
            sidecar = sidecars.get(candidate.get("source_file_id"))
            if sidecar is not None and isinstance(candidate.get("location"), dict):
                _maybe_enrich_location(candidate["location"], sidecar)
        for relation in item.get("reference_results", []):
            candidate = relation.get("candidate") if isinstance(relation, dict) else None
            if isinstance(candidate, dict):
                sidecar = sidecars.get(candidate.get("source_file_id"))
                if sidecar is not None and isinstance(candidate.get("location"), dict):
                    _maybe_enrich_location(candidate["location"], sidecar)
    for file in result.get("files", []):
        sidecar = sidecars.get(file.get("file_id"))
        if sidecar is None:
            continue
        profile = file.get("document_profile") or {}
        for location in profile.get("evidence_locations", []):
            if isinstance(location, dict):
                _maybe_enrich_location(location, sidecar)
        structure = file.get("content_structure") or {}
        for location in structure.get("sample_locations", []):
            if isinstance(location, dict):
                _maybe_enrich_location(location, sidecar)
    return result


def page_counts_from_sidecars(
    sidecars: dict[str, DocxPageLocationSidecar],
) -> dict[str, int]:
    return {file_id: sidecar.page_count for file_id, sidecar in sidecars.items()}
