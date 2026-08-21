DRAFT_PAYLOAD = {
    "client_reference_id": "integration-draft-001",
    "target_file": {
        "url": "https://files.example.com/draft.docx?token=target-secret",
        "file_name": "draft.docx",
    },
    "template_file": {
        "url": "https://files.example.com/template.docx?token=template-secret",
        "file_name": "template.docx",
    },
    "reference_files": [
        {
            "url": "https://files.example.com/review.pdf?token=review-secret",
            "file_name": "review.pdf",
        }
    ],
}

FINAL_PAYLOAD = {
    "client_reference_id": "integration-final-001",
    "baseline_file": {
        "url": "https://files.example.com/baseline.docx?token=base-secret",
        "file_name": "baseline.docx",
    },
    "target_file": {
        "url": "https://files.example.com/signed.pdf?token=signed-secret",
        "file_name": "signed.pdf",
    },
}
