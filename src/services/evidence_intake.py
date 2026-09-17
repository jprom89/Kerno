"""Evidence intake — turns an uploaded file into the text Kerno can reason over.

Plain-English summary
---------------------
A compliance team's evidence arrives as documents: a policy PDF, a signed
runbook, an exported CSV of vendors. This module is the one place that turns
those bytes into the plain text stored in ``context_records.body``, which is
what the scorer, the retrieval layer, and the LLM rationale all read.

What it deliberately does NOT do (KER-406 §16 decisions):
  * It does not keep the original file. No blob storage exists; we extract text
    and discard the bytes. An auditor asking for the original signed PDF gets
    the text — a known, accepted gap tracked with KER-405 finding #4.
  * It does not parse a CSV row-wise. One upload is one evidence document, so a
    vendor-list export becomes a single record, not forty. Row-wise import
    needs column mapping and is a separate future story.
  * It does not generate embeddings. ``context_records.embedding`` stays NULL;
    a future backfill can walk ``WHERE embedding IS NULL`` at no migration cost.

Rejecting is preferred to storing something unusable: an unsupported extension
raises rather than saving a blob the scorer could never read, and an extraction
that yields nothing but whitespace is an error rather than an empty record.

How to run or test
------------------
Unit tests (no database, no network):

    pytest tests/unit/services/test_evidence_intake.py -v
"""

from __future__ import annotations

import hashlib
import io
import logging
from pathlib import PurePosixPath

from pypdf import PdfReader

from config.constants import (
    MAX_EVIDENCE_UPLOAD_BYTES,
    SUPPORTED_EVIDENCE_EXTENSIONS,
    RbacRole,
)
from src.exceptions import EvidenceExtractionError, UnsupportedFileTypeError

logger = logging.getLogger(__name__)

# RBAC roles permitted to upload evidence and to link or unlink it (KER-406
# AC-7). platform_engineer is deliberately absent: connector and webhook
# permissions are a separate concern and must not be conflated with curating
# the evidence base. auditor is absent because it is read-only — auditors can
# still LIST evidence, which is why the read endpoint is not gated by this.
EVIDENCE_CAPABLE_ROLES: tuple[RbacRole, ...] = (
    RbacRole.COMPLIANCE_LEAD,
    RbacRole.VCISO,
    RbacRole.SECURITY_ENGINEER,
)

# Extensions read as plain text; everything else supported needs a parser.
_PLAIN_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".csv"})

# context_records.external_id is VARCHAR(255) — filenames are truncated to fit
# rather than overflowing the column.
_EXTERNAL_ID_MAX_LENGTH = 255


def extract_text(filename: str, content: bytes) -> str:
    """Return the plain text of an uploaded evidence document.

    Dispatches on the filename's extension: PDFs go through pypdf, plain-text
    formats are decoded as UTF-8 with a latin-1 fallback. Raises
    ``UnsupportedFileTypeError`` for an extension we cannot read,
    ``EvidenceExtractionError`` when a supported type yields nothing usable,
    and ``ValueError`` when the upload exceeds MAX_EVIDENCE_UPLOAD_BYTES.
    """
    if len(content) > MAX_EVIDENCE_UPLOAD_BYTES:
        raise ValueError(
            f"file exceeds the {MAX_EVIDENCE_UPLOAD_BYTES}-byte upload limit"
        )
    extension = _file_extension(filename)
    if extension not in SUPPORTED_EVIDENCE_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"unsupported file type {extension or '(none)'!r}; "
            f"supported: {sorted(SUPPORTED_EVIDENCE_EXTENSIONS)}"
        )
    text = (
        _extract_pdf_text(content)
        if extension == ".pdf"
        else _decode_plain_text(content)
    )
    if not text.strip():
        raise EvidenceExtractionError(
            f"no readable text could be extracted from {filename!r}"
        )
    return text.strip()


def content_fingerprint(text: str) -> str:
    """Return the SHA-256 hex digest of the extracted text.

    Stored in ``context_records.content_hash`` (VARCHAR(64) — a hex digest fits
    exactly) and used to recognise a re-uploaded document instead of creating a
    duplicate record.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def external_id_for(filename: str) -> str:
    """Return the filename trimmed to fit context_records.external_id.

    Strips any directory component a browser may include, then truncates to the
    column width so a long name cannot overflow the insert.
    """
    return PurePosixPath(filename).name[:_EXTERNAL_ID_MAX_LENGTH]


def _file_extension(filename: str) -> str:
    """Return the lowercased extension of a filename, or an empty string."""
    return PurePosixPath(filename).suffix.lower()


def _extract_pdf_text(content: bytes) -> str:
    """Return the concatenated text of every page in a PDF.

    Pages that yield no text (scanned images, for example) contribute nothing
    rather than failing the whole document — a mixed PDF is still worth
    ingesting. A structurally unreadable file raises EvidenceExtractionError.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise EvidenceExtractionError(f"could not read PDF: {exc}") from exc
    return "\n".join(pages)


def _decode_plain_text(content: bytes) -> str:
    """Decode plain-text bytes as UTF-8, falling back to latin-1.

    The fallback exists because exported CSVs from legacy systems are routinely
    latin-1; latin-1 decodes any byte sequence, so this never raises. Text that
    truly is not text is caught by the empty-extraction check in extract_text.
    """
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        logger.info("Evidence upload was not valid UTF-8; decoded as latin-1.")
        return content.decode("latin-1")
