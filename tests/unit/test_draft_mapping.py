from app.adapters.llm.schemas import FactCandidate
from app.documents.models import DocumentLocation
from app.draft_review.mapping import (
    MAX_REFERENCE_FACTS_PER_TARGET,
    build_mapping_batches,
    expected_mapping_pairs,
    reference_fact_id,
    retrieve_reference_facts,
    subset_mapping_payload,
)


def fact(
    *,
    file_id: str,
    field_key: str,
    display_name: str,
    value_type: str,
    raw_value: str,
    paragraph: int,
) -> FactCandidate:
    return FactCandidate(
        field_key=field_key,
        display_name=display_name,
        value_type=value_type,
        raw_value=raw_value,
        normalized_hint=raw_value,
        source_file_id=file_id,
        evidence_text=f"{display_name}为{raw_value}",
        location=DocumentLocation(paragraph_index=paragraph),
        confidence=0.95,
    )


def test_local_retrieval_prefers_same_dynamic_concept_and_limits_context() -> None:
    target = fact(
        file_id="target",
        field_key="financing_amount",
        display_name="融资金额",
        value_type="MONEY",
        raw_value="100万元",
        paragraph=1,
    )
    references = [
        fact(
            file_id="reference",
            field_key=("financing_amount" if index == 0 else f"other_{index}"),
            display_name=("融资金额" if index == 0 else f"无关字段{index}"),
            value_type=("MONEY" if index < 3 else "TEXT"),
            raw_value=("100万元" if index == 0 else str(index)),
            paragraph=index,
        )
        for index in range(10)
    ]

    selected = retrieve_reference_facts(target, references)

    assert selected[0].field_key == "financing_amount"
    assert len(selected) <= MAX_REFERENCE_FACTS_PER_TARGET


def test_mapping_batches_are_target_bounded_and_can_retry_only_missing_pairs() -> None:
    references = [
        fact(
            file_id="reference",
            field_key="amount",
            display_name="金额",
            value_type="MONEY",
            raw_value=f"{index + 1}万元",
            paragraph=index,
        )
        for index in range(5)
    ]
    targets = [
        (
            f"target_fact_{index + 1:06d}",
            fact(
                file_id="target",
                field_key="amount",
                display_name="金额",
                value_type="MONEY",
                raw_value=f"{index + 1}万元",
                paragraph=index,
            ),
        )
        for index in range(10)
    ]

    batches = build_mapping_batches(
        targets,
        references,
        reference_file_id="reference",
        max_payload_chars=100_000,
    )

    assert [len(batch["groups"]) for batch in batches] == [8, 2]
    all_pairs = expected_mapping_pairs(batches[0])
    missing_pair = {next(iter(all_pairs))}
    retry = subset_mapping_payload(batches[0], missing_pair)
    assert expected_mapping_pairs(retry) == missing_pair
    assert retry["groups"][0]["references"][0]["reference_fact_id"].startswith(
        "fact_"
    )
    assert reference_fact_id(references[0]).startswith("fact_")
