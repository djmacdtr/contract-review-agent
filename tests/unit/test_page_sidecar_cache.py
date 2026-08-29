from app.documents.page_locations import (
    PAGE_LOCATION_CACHE_VERSION,
    DocxPageLocationSidecar,
    deserialize_docx_page_location_sidecar,
    page_location_cache_identity,
    page_location_cache_key,
    serialize_docx_page_location_sidecar,
    validate_docx_page_location_sidecar,
)


def make_sidecar() -> DocxPageLocationSidecar:
    return DocxPageLocationSidecar(
        file_id="fil_current",
        page_count=3,
        mappings={(1, None, None, None): (1,), (None, 0, 0, 0): (2, 3)},
        required_location_count=2,
        candidate_mapping_count=4,
        local_structure_count=2,
        external_structure_count=2,
        external_detail_page_count=3,
        structure_mappings={"paragraph:1": (1,), "table_cell:0:0:0": (2, 3)},
        unmapped_structures=(),
    )


def test_page_sidecar_round_trip_rebinds_file_id_without_text() -> None:
    value = serialize_docx_page_location_sidecar(make_sidecar())

    assert value["cache_version"] == PAGE_LOCATION_CACHE_VERSION
    assert "text" not in value
    rebound = deserialize_docx_page_location_sidecar(value, file_id="fil_new")
    validate_docx_page_location_sidecar(rebound, file_id="fil_new")
    assert rebound.file_id == "fil_new"
    assert rebound.mappings[(None, 0, 0, 0)] == (2, 3)


def test_page_sidecar_cache_identity_excludes_task_file_id() -> None:
    key = page_location_cache_key("sha256")
    batch_id, payload_digest = page_location_cache_identity("sha256")

    assert "file_id" not in key
    assert batch_id.startswith("page_")
    assert len(payload_digest) == 64


def test_page_sidecar_rejects_out_of_range_page() -> None:
    value = serialize_docx_page_location_sidecar(make_sidecar())
    value["mappings"][0]["pages"] = [4]

    try:
        deserialize_docx_page_location_sidecar(value, file_id="fil_new")
    except ValueError as exc:
        assert str(exc) == "page out of range"
    else:
        raise AssertionError("invalid page sidecar was accepted")
