from __future__ import annotations

import pytest

from app.adapters.llm.base import LlmResult
from app.results.advice_batches import generate_advice_in_batches


def advice_result(count: int) -> dict:
    files = [
        {"file_id": "fil_base", "file_name": "baseline.docx", "role": "BASELINE"},
        {"file_id": "fil_target", "file_name": "target.docx", "role": "TARGET"},
    ]
    risks = []
    diffs = []
    for index in range(count):
        risk_id = f"risk_{index:06d}"
        diff_id = f"diff_{index:06d}"
        risks.append(
            {
                "risk_id": risk_id,
                "risk_type": "ADDITION_OR_CHANGE",
                "title": "文字内容发生变化",
                "related_diff_ids": [diff_id],
                "source_evidence": [],
            }
        )
        diffs.append(
            {
                "diff_id": diff_id,
                "diff_type": "TEXT_CHANGED",
                "baseline": {
                    "file_id": "fil_base",
                    "text": f"承租人名称为甲方{index}",
                    "location": {"page": 1},
                },
                "target": {
                    "file_id": "fil_target",
                    "text": f"承租人名称为乙方{index}",
                    "location": {"page": 1},
                },
                "segments": [],
            }
        )
    return {
        "files": files,
        "risk_items": risks,
        "diff_items": diffs,
        "advice": {},
        "warnings": [],
        "metadata": {"model_runs": []},
    }


class AdviceFixture:
    def __init__(self, *, omit_first_once: bool = False, fail_first: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.omit_first_once = omit_first_once
        self.fail_first = fail_first

    async def generate_advice(self, payload: dict) -> LlmResult:
        risk_items = payload["risk_items"]
        risk_ids = [item["risk_id"] for item in risk_items]
        self.calls.append(risk_ids)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("fixture upstream failure")
        if self.omit_first_once and len(self.calls) == 1:
            risk_ids = risk_ids[1:]
        diff_by_id = {
            item["diff_id"]: item for item in payload.get("diff_items", [])
        }
        return LlmResult(
            value={
                "overall_advice": "请按具体业务依据处理差异。",
                "priority_actions": [],
                "manual_review_focus": [],
                "limitations": [],
                "evidence_refs": [],
                "risk_advices": [
                    {
                        "risk_id": risk_id,
                        "analysis_advice": (
                            "请核对"
                            + diff_by_id[
                                next(
                                    item["related_diff_ids"][0]
                                    for item in payload["risk_items"]
                                    if item["risk_id"] == risk_id
                                )
                            ]["target"]["text"]
                            + "的业务依据。"
                        ),
                    }
                    for risk_id in risk_ids
                ],
            },
            configured_model="fixture-advice",
            actual_model="fixture-advice-v1",
            mock=True,
        )


@pytest.mark.asyncio
async def test_advice_batches_split_189_risks_into_eight_item_batches() -> None:
    result = advice_result(189)
    llm = AdviceFixture()

    stats = await generate_advice_in_batches(result, llm)

    assert len(llm.calls) == 24
    assert all(1 <= len(batch) <= 8 for batch in llm.calls)
    assert stats.initial_batch_count == 24
    assert stats.recovery_batch_count == 0
    assert stats.accepted_count == 189
    assert stats.fallback_count == 0
    assert all(item["analysis_advice"] for item in result["risk_items"])


@pytest.mark.asyncio
async def test_one_missing_advice_item_gets_one_four_item_recovery_batch() -> None:
    result = advice_result(8)
    llm = AdviceFixture(omit_first_once=True)

    stats = await generate_advice_in_batches(result, llm)

    assert [len(batch) for batch in llm.calls] == [8, 1]
    assert stats.recovery_batch_count == 1
    assert stats.accepted_count == 8
    assert stats.fallback_count == 0


@pytest.mark.asyncio
async def test_failed_batch_does_not_discard_other_batches() -> None:
    result = advice_result(9)
    llm = AdviceFixture(fail_first=True)

    stats = await generate_advice_in_batches(result, llm)

    assert len(llm.calls) == 4
    assert stats.accepted_count == 9
    assert stats.fallback_count == 0
    assert stats.failure_codes["RuntimeError"] == 1
    assert all(item["analysis_advice"] for item in result["risk_items"])
    assert not result["warnings"]


@pytest.mark.asyncio
async def test_zero_risk_advice_does_not_call_model() -> None:
    result = advice_result(0)
    llm = AdviceFixture()

    stats = await generate_advice_in_batches(result, llm)

    assert llm.calls == []
    assert stats.logical_call_count == 0
    assert stats.accepted_count == 0
    assert stats.fallback_count == 0
