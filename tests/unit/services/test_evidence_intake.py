"""Unit tests for src/services/evidence_intake.py (KER-406, PILOT-READY-004).

Covers text extraction for every supported format — including a REAL PDF built
byte-by-byte in the test rather than a mock, because "pypdf can extract text"
is exactly the kind of assumption that should be proven, not asserted — plus
the rejection paths (unsupported extension, oversize, unreadable content) and
the two helpers that keep values inside their column widths. PDFs go through
the real bounded worker process (src/services/pdf_text_worker.py), so these
tests also show its refusals arriving at extract_text unchanged, without
parser text, and that plain text and oversize files never start a worker.

How to run
----------
    pytest tests/unit/services/test_evidence_intake.py -v
"""

from __future__ import annotations

import hashlib
import io
import zlib

import pytest

from config.constants import MAX_EVIDENCE_UPLOAD_BYTES
from src.exceptions import (
    EvidenceExtractionError,
    EvidenceProcessingUnavailableError,
    UnsupportedFileTypeError,
)
from src.services import pdf_text_extraction
from src.services.evidence_intake import (
    content_fingerprint,
    external_id_for,
    extract_text,
)
from tests.unit.services._pdf_documents import build_pdf, text_operations


def _minimal_pdf(text: str) -> bytes:
    """Build a genuine single-page PDF containing `text`, with no extra deps.

    Hand-assembled so the test proves real extraction against real PDF bytes
    rather than trusting a mocked reader.
    """
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    compressed = zlib.compress(stream)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(compressed)
        + compressed
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref_at = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objects) + 1))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, xref_at)
    )
    return out.getvalue()


# ── Extraction: the supported formats ─────────────────────────────────────────


def test_extracts_plain_text():
    assert extract_text("policy.txt", b"Access review completed 2026-Q1.") == (
        "Access review completed 2026-Q1."
    )


def test_extracts_markdown_and_csv():
    assert "Incident Runbook" in extract_text("runbook.md", b"# Incident Runbook\nv4")
    assert "CloudCo" in extract_text("vendors.csv", b"vendor,tier\nCloudCo,1\n")


def test_extracts_real_pdf_text():
    # A genuine PDF, not a mock — proves the pypdf dependency actually works.
    pdf = _minimal_pdf("Board approved ISMS policy")
    assert "Board approved ISMS policy" in extract_text("policy.pdf", pdf)


def test_csv_is_one_document_not_rows():
    # §16 decision 3: file-as-document. Every row lands in one record's text.
    text = extract_text("vendors.csv", b"vendor,tier\nCloudCo,1\nDataCo,2\n")
    assert "CloudCo" in text and "DataCo" in text


def test_extension_matching_is_case_insensitive():
    assert extract_text("POLICY.TXT", b"content") == "content"


def test_latin1_fallback_for_non_utf8_bytes():
    # Legacy CSV exports are routinely latin-1; this must not raise.
    assert "café" in extract_text("legacy.csv", "café".encode("latin-1"))


# ── Rejection paths ───────────────────────────────────────────────────────────


def test_unsupported_extension_is_rejected():
    with pytest.raises(UnsupportedFileTypeError):
        extract_text("scan.png", b"\x89PNG\r\n")


def test_missing_extension_is_rejected():
    with pytest.raises(UnsupportedFileTypeError):
        extract_text("README", b"content")


def test_oversize_upload_is_rejected_before_parsing():
    oversize = b"x" * (MAX_EVIDENCE_UPLOAD_BYTES + 1)
    with pytest.raises(ValueError, match="upload limit"):
        extract_text("big.txt", oversize)


def test_whitespace_only_extraction_is_an_error_not_an_empty_record():
    with pytest.raises(EvidenceExtractionError):
        extract_text("blank.txt", b"   \n\t  ")


def test_corrupt_pdf_raises_extraction_error():
    # PILOT-READY-004: the detail is fixed wording, never pypdf's own message.
    with pytest.raises(EvidenceExtractionError) as refusal:
        extract_text("broken.pdf", b"%PDF-1.4 this is not a real pdf")
    assert str(refusal.value) == "the PDF could not be read"


def test_encrypted_pdf_is_refused_without_its_content():
    pdf = build_pdf([text_operations("confidential board minutes")], user_password="pw")
    with pytest.raises(EvidenceExtractionError) as refusal:
        extract_text("minutes.pdf", pdf)
    assert str(refusal.value) == "encrypted PDFs are not supported"


def test_pdf_text_from_the_worker_is_stripped_as_before():
    pdf = build_pdf([None, text_operations("Access review completed"), None])
    assert extract_text("review.pdf", pdf) == "Access review completed"


def test_pdf_with_no_text_is_an_error_not_an_empty_record():
    with pytest.raises(EvidenceExtractionError, match="no readable text"):
        extract_text("scan.pdf", build_pdf([None, None]))


def test_unavailable_pdf_processing_propagates_unchanged(monkeypatch):
    def worker_failed(content):
        """Fail as extract_pdf_text does when the worker cannot run."""
        raise EvidenceProcessingUnavailableError("evidence processing is unavailable; try again later")

    monkeypatch.setattr(pdf_text_extraction, "extract_pdf_text", worker_failed)
    with pytest.raises(EvidenceProcessingUnavailableError):
        extract_text("policy.pdf", _minimal_pdf("Board approved ISMS policy"))


def _refuse_to_start_a_worker(monkeypatch) -> None:
    """Fail the test if anything reaches the PDF worker."""

    def must_not_run(content):
        """Fail: this input must never reach the worker."""
        raise AssertionError("a PDF worker was started")

    monkeypatch.setattr(pdf_text_extraction, "extract_pdf_text", must_not_run)


def test_plain_text_never_starts_a_pdf_worker(monkeypatch):
    _refuse_to_start_a_worker(monkeypatch)
    assert extract_text("policy.txt", b"Access review completed.") == "Access review completed."


def test_oversize_pdf_is_refused_before_any_worker_starts(monkeypatch):
    _refuse_to_start_a_worker(monkeypatch)
    with pytest.raises(ValueError, match="upload limit"):
        extract_text("big.pdf", b"%PDF-1.4" + b" " * MAX_EVIDENCE_UPLOAD_BYTES)


# ── Helpers that keep values inside their column widths ───────────────────────


def test_content_fingerprint_is_sha256_of_the_text():
    text = "Access review completed."
    assert content_fingerprint(text) == hashlib.sha256(text.encode()).hexdigest()
    assert len(content_fingerprint(text)) == 64  # fits VARCHAR(64) exactly


def test_identical_text_fingerprints_identically():
    # This is what makes AC-2 dedupe work on re-upload.
    assert content_fingerprint("same") == content_fingerprint("same")
    assert content_fingerprint("same") != content_fingerprint("different")


def test_external_id_strips_path_and_truncates_to_column_width():
    assert external_id_for("C:/Users/me/policy.pdf".replace("C:", "")) == "policy.pdf"
    assert len(external_id_for("a" * 400 + ".txt")) == 255
