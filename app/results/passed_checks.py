from __future__ import annotations

import re
from typing import Any

from app.comparison.models import ComparisonDiagnostics, DiffItem
from app.documents.models import ParsedDocument

CONTENT_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("date", "日期", re.compile(r"\d{4}[年./-]\d{1,2}(?:[月./-]\d{1,2}日?)?")),
    (
        "duration",
        "期限",
        re.compile(
            r"(?:期限|期间|租期|有效期|宽限期|届满|到期)|"
            r"(?:\d+|[一二三四五六七八九十百千万]+)"
            r"(?:年(?!\d{1,2}月)|个月|期)|"
            r"(?:\d+|[一二三四五六七八九十百千万]+)"
            r"(?:月|日)(?:内|以上|以下|期限|期间)"
        ),
    ),
    (
        "percentage",
        "比例或利率",
        re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|基点)|(?:比例|利率|税率)"),
    ),
    (
        "money",
        "金额",
        re.compile(
            r"(?:人民币|美元|欧元|港币|¥|￥|\$)|"
            r"\d[\d,]*(?:\.\d+)?\s*(?:元|万元|亿元)"
        ),
    ),
)
TABLE_DIFF_TYPES = {
    "TABLE_ROW_ADDED",
    "TABLE_ROW_DELETED",
    "TABLE_CELL_CHANGED",
    "TABLE_STRUCTURE_EXPANDED",
}


def _document_text(document: ParsedDocument) -> str:
    return "\n".join(block.raw_text for block in document.blocks)


def _diff_text(diff: DiffItem) -> str:
    return "\n".join(
        side.text for side in (diff.baseline, diff.target) if side is not None
    )


def build_comparison_passed_checks(
    documents: list[ParsedDocument],
    differences: list[DiffItem],
    diagnostics: ComparisonDiagnostics,
    *,
    check_prefix: str,
    module_code: str,
    content_title: str,
    numeric_sensitive: bool,
) -> list[dict[str, Any]]:
    if not diagnostics.reliable:
        return []
    checks: list[dict[str, Any]] = []
    if not differences:
        checks.append(
            {
                "check_id": f"{check_prefix}_content",
                "module_code": module_code,
                "title": content_title,
                "description": "已完成本次文档内容对齐和全文比较，未发现对应内容差异。",
            }
        )
    if numeric_sensitive and len(documents) >= 2:
        document_texts = [_document_text(document) for document in documents]
        for code, label, pattern in CONTENT_PATTERNS:
            if not all(pattern.search(text) for text in document_texts):
                continue
            if any(pattern.search(_diff_text(diff)) for diff in differences):
                continue
            checks.append(
                {
                    "check_id": f"{check_prefix}_{code}",
                    "module_code": module_code,
                    "title": f"{label}未发生变化",
                    "description": f"本次文档实际包含{label}内容，已完成对应比较且未发现差异。",
                }
            )
    if diagnostics.compatible_table_count and not any(
        diff.diff_type in TABLE_DIFF_TYPES for diff in differences
    ):
        count = diagnostics.compatible_table_count
        checks.append(
            {
                "check_id": f"{check_prefix}_tables",
                "module_code": module_code,
                "title": "表格内容未发生变化",
                "description": f"已完成 {count} 个可可靠对齐表格的内容比较，未发现表格差异。",
            }
        )
    return checks
