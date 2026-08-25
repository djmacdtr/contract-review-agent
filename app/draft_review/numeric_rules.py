"""Safe execution for model-discovered numeric validation plans.

The model may describe a plan, but it never supplies executable code.  This
module accepts a deliberately small JSON AST and evaluates it with Decimal.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.adapters.llm.schemas import FactCandidate, ValidationSpec
from app.draft_review.facts import normalized_fact_components


class NumericAstError(ValueError):
    pass


ARITHMETIC_OPS = {"add", "subtract", "multiply", "divide", "sum"}
COMPARE_OPS = {
    "equals",
    "not_equals",
    "less_than",
    "less_or_equal",
    "greater_than",
    "greater_or_equal",
}
_ALL_OPS = ARITHMETIC_OPS | COMPARE_OPS
MAX_AST_NODES = 24
MAX_AST_DEPTH = 6


def _require_mapping(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise NumericAstError("numeric AST nodes must be objects")
    return node


def validate_ast(
    node: Any,
    *,
    _root: bool = True,
    _depth: int = 0,
    _budget: list[int] | None = None,
) -> None:
    if _budget is None:
        _budget = [0]
    if _depth > MAX_AST_DEPTH:
        raise NumericAstError("numeric AST exceeds maximum depth")
    _budget[0] += 1
    if _budget[0] > MAX_AST_NODES:
        raise NumericAstError("numeric AST exceeds maximum node count")
    value = _require_mapping(node)
    if set(value) - {
        "op",
        "value",
        "concept_id",
        "fact_id",
        "source_file_id",
        "args",
        "left",
        "right",
        "tolerance",
    }:
        raise NumericAstError("numeric AST contains unsupported fields")
    op = value.get("op")
    if op == "literal":
        literal = value.get("value")
        if not isinstance(literal, str):
            raise NumericAstError("numeric literals must be decimal strings")
        try:
            Decimal(literal)
        except (InvalidOperation, ValueError) as exc:
            raise NumericAstError("numeric literal is invalid") from exc
        if set(value) != {"op", "value"}:
            raise NumericAstError("literal node has unsupported fields")
        return
    if op == "fact":
        if "fact_id" in value or "source_file_id" in value:
            fact_id = value.get("fact_id")
            source_file_id = value.get("source_file_id")
            if not isinstance(fact_id, str) or not fact_id:
                raise NumericAstError("fact nodes require fact_id")
            if not isinstance(source_file_id, str) or not source_file_id:
                raise NumericAstError("fact nodes require source_file_id")
            if set(value) != {"op", "fact_id", "source_file_id"}:
                raise NumericAstError("qualified fact node has unsupported fields")
        else:
            concept_id = value.get("concept_id")
            if not isinstance(concept_id, str) or not concept_id:
                raise NumericAstError("fact nodes require concept_id")
            if set(value) != {"op", "concept_id"}:
                raise NumericAstError("fact node has unsupported fields")
        return
    if op not in _ALL_OPS:
        raise NumericAstError("numeric AST operation is not allowlisted")
    if op in ARITHMETIC_OPS:
        args = value.get("args")
        if not isinstance(args, list) or len(args) < (1 if op == "sum" else 2):
            raise NumericAstError("arithmetic operation has an invalid argument count")
        if set(value) != {"op", "args"}:
            raise NumericAstError("arithmetic node has unsupported fields")
        for child in args:
            validate_ast(child, _root=False, _depth=_depth + 1, _budget=_budget)
        return
    if (
        set(value) - {"op", "left", "right", "tolerance"}
        or "left" not in value
        or "right" not in value
    ):
        raise NumericAstError("comparison node requires left and right")
    if op != "equals" and "tolerance" in value:
        raise NumericAstError("tolerance is only valid for equals")
    if "tolerance" in value:
        tolerance = value["tolerance"]
        if not isinstance(tolerance, str):
            raise NumericAstError("tolerance must be a decimal string")
        try:
            if Decimal(tolerance) < 0:
                raise NumericAstError("tolerance must not be negative")
        except InvalidOperation as exc:
            raise NumericAstError("tolerance is invalid") from exc
    validate_ast(value["left"], _root=False, _depth=_depth + 1, _budget=_budget)
    validate_ast(value["right"], _root=False, _depth=_depth + 1, _budget=_budget)


def _decimal_for_fact(fact: FactCandidate) -> Decimal:
    components = normalized_fact_components(fact)
    if components and isinstance(components.get("value"), Decimal):
        return components["value"]
    raw = fact.raw_value.replace(",", "").replace("，", "")
    import re

    match = re.search(r"[-+]?\d+(?:\.\d+)?", raw)
    if not match:
        raise NumericAstError(f"fact {fact.field_key} is not numeric")
    value = Decimal(match.group())
    if "亿" in raw:
        value *= Decimal("100000000")
    elif "万" in raw:
        value *= Decimal("10000")
    return value


def referenced_fact_ids(node: Any) -> set[str]:
    value = _require_mapping(node)
    if value.get("op") == "fact":
        return {str(value["concept_id"])}
    references: set[str] = set()
    for key in ("args", "left", "right"):
        child = value.get(key)
        if isinstance(child, list):
            for item in child:
                references.update(referenced_fact_ids(item))
        elif isinstance(child, dict):
            references.update(referenced_fact_ids(child))
    return references


def referenced_fact_refs(node: Any) -> set[tuple[str, str]]:
    """Return file-qualified references for the new semantic-plan AST."""

    value = _require_mapping(node)
    if value.get("op") == "fact":
        fact_id = value.get("fact_id")
        source_file_id = value.get("source_file_id")
        if not isinstance(fact_id, str) or not isinstance(source_file_id, str):
            raise NumericAstError("semantic fact nodes require qualified references")
        return {(fact_id, source_file_id)}
    references: set[tuple[str, str]] = set()
    for key in ("args", "left", "right"):
        child = value.get(key)
        if isinstance(child, list):
            for item in child:
                references.update(referenced_fact_refs(item))
        elif isinstance(child, dict):
            references.update(referenced_fact_refs(child))
    return references


def evaluate_ast(node: Any, values: dict[str, Decimal]) -> Decimal | bool:
    validate_ast(node)
    op = node["op"]
    if op == "literal":
        return Decimal(node["value"])
    if op == "fact":
        try:
            key: Any = (
                node["fact_id"],
                node["source_file_id"],
            ) if "fact_id" in node else node["concept_id"]
            value = values[key]
            return value if isinstance(value, Decimal) else Decimal(str(value))
        except KeyError as exc:
            raise NumericAstError("referenced fact is missing") from exc
    if op in ARITHMETIC_OPS:
        args = [evaluate_ast(child, values) for child in node["args"]]
        if not all(isinstance(item, Decimal) for item in args):
            raise NumericAstError("arithmetic operands must be numeric")
        if op == "sum":
            return sum(args, Decimal(0))
        if op == "add":
            return args[0] + args[1]
        if op == "subtract":
            return args[0] - args[1]
        if op == "multiply":
            return args[0] * args[1]
        if args[1] == 0:
            raise NumericAstError("division by zero")
        return args[0] / args[1]
    left = evaluate_ast(node["left"], values)
    right = evaluate_ast(node["right"], values)
    if not isinstance(left, Decimal) or not isinstance(right, Decimal):
        raise NumericAstError("comparison operands must be numeric")
    if op == "equals":
        tolerance = Decimal(node.get("tolerance", "0"))
        return abs(left - right) <= tolerance
    if op == "not_equals":
        return left != right
    if op == "less_than":
        return left < right
    if op == "less_or_equal":
        return left <= right
    if op == "greater_than":
        return left > right
    return left >= right


def evaluate_validation_spec(
    spec: ValidationSpec,
    facts: list[FactCandidate] | dict[tuple[str, str], FactCandidate],
) -> dict[str, Any]:
    if isinstance(facts, dict):
        return _evaluate_qualified_spec(spec, facts)

    value_candidates: dict[str, set[Decimal]] = {}
    evidence_by_key: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        try:
            value = _decimal_for_fact(fact)
        except NumericAstError:
            continue
        keys = {fact.field_key}
        if fact.concept_id:
            keys.add(fact.concept_id)
        for key in keys:
            value_candidates.setdefault(key, set()).add(value)
            evidence_by_key.setdefault(key, []).append(
                {
                    "file_id": fact.source_file_id,
                    "text": fact.evidence_text,
                    "location": fact.location.model_dump(mode="json", exclude_none=True),
                }
            )
    referenced = referenced_fact_ids(spec.expression)
    ambiguous = sorted(key for key in referenced if len(value_candidates.get(key, set())) > 1)
    values = {
        key: next(iter(candidates))
        for key, candidates in value_candidates.items()
        if len(candidates) == 1
    }
    evidence = [item for key in sorted(referenced) for item in evidence_by_key.get(key, [])]
    if ambiguous:
        return {
            "validation_id": spec.validation_id,
            "rule_name": spec.display_name,
            "status": "REVIEW_REQUIRED",
            "message": "数值规则引用了多个不一致的事实值，需要人工复核。",
            "reason_code": "NUMERIC_RULE_AMBIGUOUS_INPUT",
            "error": f"ambiguous facts: {', '.join(ambiguous)}",
            "source_evidence": evidence,
        }
    try:
        result = evaluate_ast(spec.expression, values)
    except NumericAstError as exc:
        return {
            "validation_id": spec.validation_id,
            "rule_name": spec.display_name,
            "status": "REVIEW_REQUIRED",
            "message": "数值规则无法在现有事实证据上安全执行，需要人工复核。",
            "reason_code": "NUMERIC_RULE_UNCERTAIN",
            "error": str(exc),
            "source_evidence": evidence,
        }
    if not isinstance(result, bool):
        return {
            "validation_id": spec.validation_id,
            "rule_name": spec.display_name,
            "status": "REVIEW_REQUIRED",
            "message": "数值规则未返回布尔比较结果，需要人工复核。",
            "reason_code": "NUMERIC_RULE_UNCERTAIN",
            "source_evidence": evidence,
        }
    return {
        "validation_id": spec.validation_id,
        "rule_name": spec.display_name,
        "status": "PASSED" if result else "FAILED",
        "message": "声明式数值规则通过。" if result else "声明式数值规则未通过。",
        "reason_code": "NUMERIC_RULE_FAILED" if not result else None,
        "source_evidence": evidence,
    }


def _evaluate_qualified_spec(
    spec: ValidationSpec,
    fact_index: dict[tuple[str, str], FactCandidate],
) -> dict[str, Any]:
    try:
        referenced = referenced_fact_refs(spec.expression)
    except NumericAstError as exc:
        return {
            "validation_id": spec.validation_id,
            "rule_name": spec.display_name,
            "status": "REVIEW_REQUIRED",
            "message": "数值规则引用格式不安全，需要人工复核。",
            "reason_code": "NUMERIC_RULE_UNCERTAIN",
            "error": str(exc),
            "source_evidence": [],
        }
    values: dict[tuple[str, str], Decimal] = {}
    evidence: list[dict[str, Any]] = []
    missing: list[str] = []
    for ref in sorted(referenced):
        fact = fact_index.get(ref)
        if fact is None:
            missing.append(f"{ref[0]}@{ref[1]}")
            continue
        try:
            values[ref] = _decimal_for_fact(fact)
        except NumericAstError:
            missing.append(f"{ref[0]}@{ref[1]}")
            continue
        evidence.append(
            {
                "file_id": fact.source_file_id,
                "text": fact.evidence_text,
                "location": fact.location.model_dump(mode="json", exclude_none=True),
            }
        )
    if missing:
        return {
            "validation_id": spec.validation_id,
            "rule_name": spec.display_name,
            "status": "REVIEW_REQUIRED",
            "message": "数值规则引用的已评审事实不存在或不可规范化。",
            "reason_code": "NUMERIC_RULE_UNCERTAIN",
            "error": f"missing qualified facts: {', '.join(missing)}",
            "source_evidence": evidence,
        }
    try:
        result = evaluate_ast(spec.expression, values)
    except NumericAstError as exc:
        return {
            "validation_id": spec.validation_id,
            "rule_name": spec.display_name,
            "status": "REVIEW_REQUIRED",
            "message": "数值规则无法在现有事实证据上安全执行，需要人工复核。",
            "reason_code": "NUMERIC_RULE_UNCERTAIN",
            "error": str(exc),
            "source_evidence": evidence,
        }
    if not isinstance(result, bool):
        return {
            "validation_id": spec.validation_id,
            "rule_name": spec.display_name,
            "status": "REVIEW_REQUIRED",
            "message": "数值规则未返回布尔比较结果，需要人工复核。",
            "reason_code": "NUMERIC_RULE_UNCERTAIN",
            "source_evidence": evidence,
        }
    return {
        "validation_id": spec.validation_id,
        "rule_name": spec.display_name,
        "status": "PASSED" if result else "FAILED",
        "message": "声明式数值规则通过。" if result else "声明式数值规则未通过。",
        "reason_code": "NUMERIC_RULE_FAILED" if not result else None,
        "source_evidence": evidence,
    }
