"""evidence_upload_intake.py — bounded multipart intake for POST /api/v1/evidence (SEC-REMED-003).

What:  a FastAPI dependency that receives one evidence upload under fixed
       limits and hands the endpoint plain values: the filename, at most
       MAX_EVIDENCE_UPLOAD_BYTES of file content, the record_type and the
       title. Every spooled file the parser opened is closed before it returns.
Why:   finding resource-exhaustion.evidence-buffering. With File() and Form()
       parameters, FastAPI parses the entire multipart body before any
       dependency runs, so the body was spooled before the token was checked,
       and the endpoint then read the complete file before checking its size.
       The endpoint declares this dependency after its token and role
       dependencies and before its connection dependency. FastAPI resolves
       them in that order, so intake runs only for an authorised caller and
       finishes before a database connection is leased.
How:   Depends(receive_evidence_upload). Tests:
       pytest tests/unit/api/test_evidence_upload_bounds.py -v
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import HTTPException, Request
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import parse_options_header
from starlette.datastructures import FormData, Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from config.constants import (
    EVIDENCE_UPLOAD_MAX_BODY_BYTES,
    EVIDENCE_UPLOAD_MAX_FIELD_BYTES,
    EVIDENCE_UPLOAD_MAX_FIELDS,
    EVIDENCE_UPLOAD_MAX_FILES,
    MAX_EVIDENCE_UPLOAD_BYTES,
)

_MULTIPART_FORM_DATA = b"multipart/form-data"
_FILE_FIELD = "file"
_RECORD_TYPE_FIELD = "record_type"
_TITLE_FIELD = "title"
_ACCEPTED_FIELDS = frozenset({_FILE_FIELD, _RECORD_TYPE_FIELD, _TITLE_FIELD})

# A refusal sent before the body was read in full leaves the rest unread, so the
# connection cannot carry another request; asking the server to close it stops
# the client from streaming the remainder into it.
_CLOSE_CONNECTION = {"Connection": "close"}

_NOT_MULTIPART = "multipart/form-data is required"
_NO_BOUNDARY = "multipart boundary is missing"
_INVALID_CONTENT_LENGTH = "invalid Content-Length"
_BODY_TOO_LARGE = "upload exceeds the size limit"
_BODY_INCOMPLETE = "upload was not received completely"
_MALFORMED_MULTIPART = "malformed multipart body"
_UNEXPECTED_FIELD = "unexpected form field; only file, record_type and title are accepted"
_REPEATED_FIELD = "each form field may be sent only once"


@dataclass(frozen=True)
class ReceivedEvidenceUpload:
    """One upload as the endpoint receives it: plain values, no open file."""

    filename: str
    content: bytes
    record_type: str
    title: str | None


class _TrackedMultiPartParser(MultiPartParser):
    """Starlette's multipart parser, reporting every spooled file it opened.

    Verified against Starlette 1.3.1: the parser closes its spooled files only
    when it raises its own MultiPartException or an OSError. A file part whose
    closing boundary never arrives is left out of the returned form and stays
    open; any other exception leaves every file open. Intake therefore closes
    all of them itself, whatever the outcome.
    """

    def spooled_files(self) -> list:
        """Return every spooled file this parser created, whether or not it reached the form."""
        return list(self._files_to_close_on_error)


async def receive_evidence_upload(request: Request) -> ReceivedEvidenceUpload:
    """Receive one upload under its limits, or refuse it before reading more than they allow.

    In order: the headers alone can refuse (415 not multipart, 400 no boundary
    or a malformed Content-Length, 413 a declared length over
    EVIDENCE_UPLOAD_MAX_BODY_BYTES); the body is received and counted as it
    arrives, stopping at the first chunk over that limit (413) or on a client
    disconnect (400); only the complete body is parsed, with at most one file,
    two fields and EVIDENCE_UPLOAD_MAX_FIELD_BYTES per field (400); a
    misshapen form is a 422 with a string detail; and at most
    MAX_EVIDENCE_UPLOAD_BYTES + 1 file bytes are read (413 above the limit).
    """
    _refuse_by_headers(request.headers)
    parser = _TrackedMultiPartParser(
        request.headers,
        _single_chunk(await _receive_bounded_body(request)),
        max_files=EVIDENCE_UPLOAD_MAX_FILES,
        max_fields=EVIDENCE_UPLOAD_MAX_FIELDS,
        max_part_size=EVIDENCE_UPLOAD_MAX_FIELD_BYTES,
    )
    try:
        form = await _parse(parser)
        return await _upload_from_form(form)
    finally:
        for spooled_file in parser.spooled_files():
            spooled_file.close()


def _refuse_by_headers(headers: Headers) -> None:
    """Refuse, before reading any of the body, what the headers alone show cannot be a bounded upload.

    Only ASCII digits are a Content-Length (RFC 9110): int() alone would also
    accept a sign, surrounding spaces and underscores. The declared length
    only allows an earlier refusal; the bytes that arrive are always counted.
    Its size is compared without converting the whole string, see
    _declares_more_than.
    """
    media_type, options = parse_options_header(headers.get("content-type"))
    if media_type.lower() != _MULTIPART_FORM_DATA:
        raise HTTPException(status_code=415, detail=_NOT_MULTIPART, headers=_CLOSE_CONNECTION)
    if not options.get(b"boundary"):
        raise HTTPException(status_code=400, detail=_NO_BOUNDARY, headers=_CLOSE_CONNECTION)
    declared = headers.get("content-length")
    if declared is None:
        return
    if not (declared.isascii() and declared.isdigit()):
        raise HTTPException(status_code=400, detail=_INVALID_CONTENT_LENGTH, headers=_CLOSE_CONNECTION)
    if _declares_more_than(declared, EVIDENCE_UPLOAD_MAX_BODY_BYTES):
        raise HTTPException(status_code=413, detail=_BODY_TOO_LARGE, headers=_CLOSE_CONNECTION)


def _declares_more_than(digits: str, limit: int) -> bool:
    """Return whether a string of ASCII digits states a number above limit, without converting all of it.

    int() refuses a decimal string longer than CPython's integer-conversion
    limit (4,300 digits by default) with a ValueError, which here would be a
    500. Leading zeros are allowed, since RFC 9110 defines Content-Length as
    1*DIGIT, and they do not change the value: "0010" declares 10. After
    stripping them, a value with more digits than the limit is above it, one
    with fewer is not, and only a value of the limit's own length is ever
    passed to int().
    """
    significant = digits.lstrip("0")
    limit_digits = len(str(limit))
    if len(significant) != limit_digits:
        return len(significant) > limit_digits
    return int(significant) > limit


async def _receive_bounded_body(request: Request) -> bytes:
    """Return the complete raw body, stopping at the first chunk that crosses EVIDENCE_UPLOAD_MAX_BODY_BYTES.

    The rest of an over-limit body is never consumed. Nothing is parsed or
    spooled here, so a refusal leaves no temporary file behind.
    """
    chunks: list[bytes] = []
    received = 0
    try:
        async with contextlib.aclosing(request.stream()) as stream:
            async for chunk in stream:
                received += len(chunk)
                if received > EVIDENCE_UPLOAD_MAX_BODY_BYTES:
                    raise HTTPException(status_code=413, detail=_BODY_TOO_LARGE, headers=_CLOSE_CONNECTION)
                chunks.append(chunk)
    except ClientDisconnect:
        raise HTTPException(status_code=400, detail=_BODY_INCOMPLETE)
    return b"".join(chunks)


async def _single_chunk(body: bytes) -> AsyncIterator[bytes]:
    """Yield the already-received body as the parser's only chunk."""
    yield body


async def _parse(parser: _TrackedMultiPartParser) -> FormData:
    """Parse the received body; a count, size or syntax violation is a 400."""
    try:
        return await parser.parse()
    except MultiPartException as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    except FormParserError:
        raise HTTPException(status_code=400, detail=_MALFORMED_MULTIPART)


async def _upload_from_form(form: FormData) -> ReceivedEvidenceUpload:
    """Check the form's shape (422 with a string detail) and read the file within its limit.

    The detail is a string, as every other refusal from this router is: the
    dashboard shows it to the user as-is.
    """
    _require_accepted_unique_fields(form)
    upload = form.get(_FILE_FIELD)
    if upload is None:
        raise HTTPException(status_code=422, detail="file is required")
    if not isinstance(upload, UploadFile):
        raise HTTPException(status_code=422, detail="file must be an uploaded file")
    record_type = form.get(_RECORD_TYPE_FIELD)
    if not isinstance(record_type, str) or not record_type:
        raise HTTPException(status_code=422, detail="record_type is required")
    title = form.get(_TITLE_FIELD)
    return ReceivedEvidenceUpload(
        filename=upload.filename or "",
        content=await _read_within_limit(upload),
        record_type=record_type,
        title=title if isinstance(title, str) and title else None,
    )


def _require_accepted_unique_fields(form: FormData) -> None:
    """Refuse an unknown or repeated field name (422) without echoing the client's name back."""
    names = [name for name, _value in form.multi_items()]
    for name in names:
        if name not in _ACCEPTED_FIELDS:
            raise HTTPException(status_code=422, detail=_UNEXPECTED_FIELD)
        if names.count(name) > 1:
            raise HTTPException(status_code=422, detail=_REPEATED_FIELD)


async def _read_within_limit(upload: UploadFile) -> bytes:
    """Read at most MAX_EVIDENCE_UPLOAD_BYTES + 1 bytes: one byte more than allowed proves the file too large."""
    content = await upload.read(MAX_EVIDENCE_UPLOAD_BYTES + 1)
    if len(content) > MAX_EVIDENCE_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds the {MAX_EVIDENCE_UPLOAD_BYTES}-byte upload limit",
        )
    return content
