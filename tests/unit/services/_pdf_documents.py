"""
_pdf_documents.py

What:  Builds small, genuine PDFs for the PDF extraction tests with pypdf's
       PdfWriter: pages with given content streams (Flate-compressed or not),
       pages with no content, RC4-encrypted documents, a page whose
       already-encoded stream and filters are supplied directly, so a stream
       that decodes to hundreds of megabytes never has to exist in the test
       (a Flate stream, a Flate-then-ASCII85 stream of mostly zero bytes, a
       JBIG2 stream), two fonts that amplify one shown byte into thousands of
       characters of extracted text (a ToUnicode map, and an /Encoding
       /Differences glyph name), so a few kilobytes of PDF ask pypdf for
       gigabytes, fonts with no /Encoding or with a /BaseEncoding, a page
       (or the catalog of a document with no pages) whose own dictionary
       carries a nested dictionary with a marked value, and pages that draw
       a Form XObject: an ordinary one, one nested inside
       another, one using the glyph-name amplifier, one whose stream is
       JBIG2, and one pypdf cannot decode at all.
Why:   The worker's limits must be proven against real PDF bytes that pypdf
       parses, never mocks, and every test should build them the same way;
       each document here reaches one code path in pypdf that a test needs.
       reference_text is today's extraction formula, the baseline the worker
       must reproduce exactly.
How:   Imported by tests/unit/services/test_pdf_text_worker.py and
       test_pdf_text_extraction.py. Not collected by pytest (no test_ prefix).
       Imports only pypdf and the standard library.
"""

from __future__ import annotations

import base64
import io
import zlib

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

# WinAnsiEncoding is cp1252, so text shown in /F1 may use any cp1252
# character (é, ü, € ...) and pypdf maps it back to the same character.
_FONT_TEXT_ENCODING = "cp1252"
_LINE_SPACING_POINTS = 16
_FIRST_LINE_Y_POINTS = 720
_LEFT_MARGIN_POINTS = 72
_FONT_SIZE_POINTS = 12
_PAGE_WIDTH_POINTS = 612
_PAGE_HEIGHT_POINTS = 792
_ENCRYPTION_ALGORITHM = "RC4-128"
_OWNER_PASSWORD = "owner-password"
_PADDING_CHUNK_BYTES = 1024 * 1024
# The one byte the amplifying fonts expand, and "A" as the UTF-16BE hex a
# ToUnicode map writes it in.
_AMPLIFIED_CODE = b"\x01"
_AMPLIFIED_CODE_NUMBER = 1
_MAPPED_CHARACTER_HEX = b"0041"
# The text the page around an amplifying form shows itself: all pypdf would
# return if it silently dropped the form.
AMPLIFIED_FORM_PAGE_TEXT = "visible page text"
# A filter no PDF reader implements, so pypdf cannot decode a form using it.
_UNKNOWN_FILTER_NAME = "/KernoNoSuchDecode"
_UNDECODABLE_FORM_BYTES = b"bytes no filter can decode"
_FLATE_FILTER_NAME = "/FlateDecode"
_ASCII85_FILTER_NAME = "/ASCII85Decode"
# Filters apply in the order listed: Flate first, then ASCII85.
FLATE_THEN_ASCII85 = (_FLATE_FILTER_NAME, _ASCII85_FILTER_NAME)
JBIG2_FILTER = ("/JBIG2Decode",)
# The JBIG2 file signature and a few bytes: what a JBIG2 stream starts with.
# pypdf never decodes it itself; it hands the bytes to the jbig2dec program.
_JBIG2_SAMPLE_BYTES = b"\x97JB2\r\n\x1a\n" + b"\x00" * 16
# ASCII85 writes four zero bytes as the single character "z", so padding with
# zero bytes makes a stream whose ASCII85 stage multiplies its input by nearly
# four. pypdf skips zero bytes between operators as white space, but not
# straight after an operator's name, so a newline comes first.
_ZERO_BYTE = b"\x00"
_ZERO_PADDING_SEPARATOR = b"\n"
# Where build_pdf_with_nested_page_dictionary puts its nested dictionary, and
# the keys inside it.
_PIECE_INFO_KEY = "/PieceInfo"
_NESTED_DICTIONARY_KEY = "/Kerno"
_MARKED_VALUE_KEY = "/Marker"


def text_operations(*lines: str) -> bytes:
    """Return a content stream that shows each line, top to bottom, in font /F1."""
    shown = [f"BT /F1 {_FONT_SIZE_POINTS} Tf {_LEFT_MARGIN_POINTS} {_FIRST_LINE_Y_POINTS} Td"]
    for index, line in enumerate(lines):
        if index:
            shown.append(f"0 -{_LINE_SPACING_POINTS} Td")
        shown.append(f"({_escaped(line)}) Tj")
    shown.append("ET")
    return " ".join(shown).encode(_FONT_TEXT_ENCODING)


def padded(content: bytes, decoded_size: int) -> bytes:
    """Return content followed by spaces so it is exactly decoded_size bytes long."""
    return content + b" " * (decoded_size - len(content))


def zero_padded(content: bytes, decoded_size: int) -> bytes:
    """Return content, a newline, then zero bytes, so it is exactly decoded_size bytes long."""
    separated = content + _ZERO_PADDING_SEPARATOR
    return separated + _ZERO_BYTE * (decoded_size - len(separated))


def build_pdf(
    page_contents: list[bytes | None],
    compress: bool = True,
    user_password: str | None = None,
) -> bytes:
    """Return a PDF with one page per entry: its content stream, or no /Contents for None.

    Streams are Flate-compressed unless compress is False. A user_password
    (the empty string included) encrypts the document with RC4-128, which
    pypdf can read without an optional cryptography package.
    """
    writer = PdfWriter()
    font = writer._add_object(_helvetica())
    for content in page_contents:
        page = writer.add_blank_page(_PAGE_WIDTH_POINTS, _PAGE_HEIGHT_POINTS)
        page[NameObject("/Resources")] = _resources(font)
        if content is not None:
            page[NameObject("/Contents")] = writer._add_object(_stream(content, compress))
    if user_password is not None:
        writer.encrypt(user_password, _OWNER_PASSWORD, algorithm=_ENCRYPTION_ALGORITHM)
    return _written(writer)


def build_pdf_with_deflated_page(deflated_content: bytes) -> bytes:
    """Return a one-page PDF whose content stream is the given FlateDecode bytes, stored as-is."""
    return build_pdf_with_encoded_page(deflated_content, (_FLATE_FILTER_NAME,))


def build_pdf_with_encoded_page(stored_data: bytes, filter_names: tuple[str, ...]) -> bytes:
    """Return a one-page PDF whose content stream is stored_data as-is, decoded by filter_names in order."""
    writer = PdfWriter()
    font = writer._add_object(_helvetica())
    page = writer.add_blank_page(_PAGE_WIDTH_POINTS, _PAGE_HEIGHT_POINTS)
    page[NameObject("/Resources")] = _resources(font)
    page[NameObject("/Contents")] = writer._add_object(_encoded_stream(stored_data, filter_names))
    return _written(writer)


def build_pdf_with_jbig2_page() -> bytes:
    """Return a one-page PDF whose content stream is declared /JBIG2Decode, so only jbig2dec could decode it."""
    return build_pdf_with_encoded_page(_JBIG2_SAMPLE_BYTES, JBIG2_FILTER)


def flate_then_ascii85(content: bytes) -> bytes:
    """Return content encoded for FLATE_THEN_ASCII85: ASCII85 with Adobe framing, then Flate."""
    return zlib.compress(base64.a85encode(content, adobe=True))


def build_pdf_with_form(page_content: bytes, form_content: bytes) -> bytes:
    """Return a one-page PDF whose page shows its own content, then draws a Form XObject (/X1).

    pypdf extracts a form's text inside a broad except that logs and moves on,
    so a form that fails to decode silently drops its text from the page.
    """
    writer = PdfWriter()
    font = writer._add_object(_helvetica())
    form = DecodedStreamObject()
    form.set_data(form_content)
    _mark_as_form(form, font)
    return _page_drawing_form(writer, page_content, writer._add_object(form.flate_encode()), font)


def build_pdf_with_amplifying_form(name_characters: int, times_shown: int) -> bytes:
    """Return a one-page PDF showing AMPLIFIED_FORM_PAGE_TEXT, then drawing a form that uses the glyph-name amplifier.

    The form's single Tj shows byte 0x01 times_shown times in a font whose
    /Differences glyph name for it is name_characters "A"s, so extracting
    the form asks for name_characters * times_shown characters at once.
    pypdf extracts a form inside a broad except that logs and moves on, so a
    failure there would otherwise leave only the page's own text.
    """
    writer = PdfWriter()
    amplifying_font = writer._add_object(_amplifying_encoding_font(writer, name_characters))
    form = DecodedStreamObject()
    form.set_data(_showing_the_amplified_code(times_shown))
    _mark_as_form(form, amplifying_font)
    page_font = writer._add_object(_helvetica())
    page_content = text_operations(AMPLIFIED_FORM_PAGE_TEXT)
    return _page_drawing_form(writer, page_content, writer._add_object(form.flate_encode()), page_font)


def build_pdf_with_undecodable_form(page_content: bytes) -> bytes:
    """Return a one-page PDF whose page shows its own content, then draws a form whose stream uses an unknown filter.

    A merely malformed document: pypdf cannot decode the form, logs that it
    is skipping it, and returns the page's own text. That tolerance is what
    the worker must leave unchanged.
    """
    writer = PdfWriter()
    font = writer._add_object(_helvetica())
    form = EncodedStreamObject()
    form._data = _UNDECODABLE_FORM_BYTES
    form[NameObject("/Filter")] = NameObject(_UNKNOWN_FILTER_NAME)
    _mark_as_form(form, font)
    return _page_drawing_form(writer, page_content, writer._add_object(form), font)


def build_pdf_with_jbig2_form(page_content: bytes) -> bytes:
    """Return a one-page PDF whose page shows its own content, then draws a form whose stream is /JBIG2Decode."""
    writer = PdfWriter()
    font = writer._add_object(_helvetica())
    form = _encoded_stream(_JBIG2_SAMPLE_BYTES, JBIG2_FILTER)
    _mark_as_form(form, font)
    return _page_drawing_form(writer, page_content, writer._add_object(form), font)


def build_pdf_with_nested_forms(page_content: bytes, outer_form_content: bytes, inner_form_content: bytes) -> bytes:
    """Return a one-page PDF whose page draws form /X1, which shows its own content and then draws form /X2.

    pypdf reports each form's text to visitor_text in fragments and then
    again whole, so the inner form's text reaches the visitor at both depths.
    """
    writer = PdfWriter()
    font = writer._add_object(_helvetica())
    inner = DecodedStreamObject()
    inner.set_data(inner_form_content)
    _mark_as_form(inner, font)
    outer = DecodedStreamObject()
    outer.set_data(outer_form_content + b" /X2 Do")
    _mark_as_form(outer, font)
    _add_form_resource(outer[NameObject("/Resources")], "/X2", writer._add_object(inner.flate_encode()))
    return _page_drawing_form(writer, page_content, writer._add_object(outer.flate_encode()), font)


def build_pdf_with_unencoded_font(page_content: bytes) -> bytes:
    """Return a one-page PDF showing page_content in a font with no /Encoding, whose strings pypdf decodes as "charmap"."""
    return _one_page_in_font(_font_dictionary(), page_content)


def build_pdf_with_base_encoding(page_content: bytes, base_encoding: str) -> bytes:
    """Return a one-page PDF showing page_content in a font whose /Encoding is a dictionary naming base_encoding."""
    encoding = DictionaryObject()
    encoding[NameObject("/BaseEncoding")] = NameObject(base_encoding)
    font = _font_dictionary()
    font[NameObject("/Encoding")] = encoding
    return _one_page_in_font(font, page_content)


def build_pdf_with_nested_page_dictionary(page_content: bytes, marked_value: str) -> bytes:
    """Return a one-page PDF whose page dictionary ends with /PieceInfo << /Kerno << /Marker (marked_value) >> >>.

    Readers ignore /PieceInfo; pypdf still reads it with the page, two
    dictionaries deep and after /Contents, so a failure planted at the marked
    value lands inside pypdf's dictionary reading.
    """
    writer = PdfWriter()
    font = writer._add_object(_helvetica())
    page = writer.add_blank_page(_PAGE_WIDTH_POINTS, _PAGE_HEIGHT_POINTS)
    page[NameObject("/Resources")] = _resources(font)
    page[NameObject("/Contents")] = writer._add_object(_stream(page_content, True))
    page[NameObject(_PIECE_INFO_KEY)] = _marked_piece_info(marked_value)
    return _written(writer)


def build_pdf_without_pages_marking_its_catalog(marked_value: str) -> bytes:
    """Return a PDF with no pages whose catalog ends with /PieceInfo << /Kerno << /Marker (marked_value) >> >>.

    pypdf reads the catalog only when it counts the pages, so a failure
    planted at the marked value lands there, and no page follows.
    """
    writer = PdfWriter()
    writer._root_object[NameObject(_PIECE_INFO_KEY)] = _marked_piece_info(marked_value)
    return _written(writer)


def _marked_piece_info(marked_value: str) -> DictionaryObject:
    """Return << /Kerno << /Marker (marked_value) >> >>: a dictionary two deep holding the marked value."""
    nested = DictionaryObject()
    nested[NameObject(_MARKED_VALUE_KEY)] = TextStringObject(marked_value)
    piece_info = DictionaryObject()
    piece_info[NameObject(_NESTED_DICTIONARY_KEY)] = nested
    return piece_info


def build_pdf_with_amplifying_to_unicode(mapped_characters: int, times_shown: int) -> bytes:
    """Return a one-page PDF whose font's ToUnicode map turns byte 0x01 into mapped_characters "A"s.

    The page's single Tj shows that byte times_shown times, so one text
    fragment expands to mapped_characters * times_shown characters while
    every decoded stream stays small. pypdf builds that fragment by
    appending one mapped string per shown byte.
    """
    cmap = (
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"/CMapName /Amplifier def /CMapType 2 def\n"
        b"1 begincodespacerange <00> <FF> endcodespacerange\n"
        b"1 beginbfchar <01> <" + _MAPPED_CHARACTER_HEX * mapped_characters + b"> endbfchar\n"
        b"endcmap CMapName currentdict /CMap defineresource pop end end\n"
    )
    writer = PdfWriter()
    font = _font_dictionary()
    font[NameObject("/ToUnicode")] = writer._add_object(_stream(cmap, True))
    return _one_page_showing_the_amplified_code(writer, writer._add_object(font), times_shown)


def build_pdf_with_amplifying_encoding(name_characters: int, times_shown: int) -> bytes:
    """Return a one-page PDF whose font's /Differences gives byte 0x01 a glyph name of name_characters "A"s.

    pypdf keeps a glyph name it does not know as that code's text, and joins
    a shown string's characters in one allocation, so the single Tj showing
    the byte times_shown times asks for name_characters * times_shown
    characters at once.
    """
    writer = PdfWriter()
    font = _amplifying_encoding_font(writer, name_characters)
    return _one_page_showing_the_amplified_code(writer, writer._add_object(font), times_shown)


def deflated_text_with_padding(content: bytes, decoded_size: int) -> bytes:
    """Return content padded with spaces to decoded_size bytes, Flate-compressed a chunk at a time."""
    compressor = zlib.compressobj()
    chunks = [compressor.compress(content)]
    remaining = decoded_size - len(content)
    while remaining > 0:
        chunk_size = min(remaining, _PADDING_CHUNK_BYTES)
        chunks.append(compressor.compress(b" " * chunk_size))
        remaining -= chunk_size
    chunks.append(compressor.flush())
    return b"".join(chunks)


def reference_text(pdf: bytes) -> str:
    """Return the text today's in-process extraction produced, the baseline the worker must match."""
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)


def _escaped(line: str) -> str:
    """Return a line with the characters a PDF literal string reserves escaped."""
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _helvetica() -> DictionaryObject:
    """Return a standard Type1 Helvetica font with WinAnsiEncoding; it embeds no streams."""
    font = _font_dictionary()
    font[NameObject("/Encoding")] = NameObject("/WinAnsiEncoding")
    return font


def _font_dictionary() -> DictionaryObject:
    """Return a standard Type1 Helvetica font dictionary with no encoding of its own."""
    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    return font


def _amplifying_encoding_font(writer: PdfWriter, name_characters: int) -> DictionaryObject:
    """Return a font whose /Differences gives byte 0x01 a glyph name of name_characters "A"s."""
    differences = ArrayObject(
        [NumberObject(_AMPLIFIED_CODE_NUMBER), NameObject("/" + "A" * name_characters)]
    )
    encoding = DictionaryObject()
    encoding[NameObject("/Differences")] = differences
    font = _font_dictionary()
    font[NameObject("/Encoding")] = writer._add_object(encoding)
    return font


def _one_page_showing_the_amplified_code(writer: PdfWriter, font: object, times_shown: int) -> bytes:
    """Add one page whose single compressed Tj shows byte 0x01 times_shown times in font /F1; return the PDF."""
    page = writer.add_blank_page(_PAGE_WIDTH_POINTS, _PAGE_HEIGHT_POINTS)
    page[NameObject("/Resources")] = _resources(font)
    page[NameObject("/Contents")] = writer._add_object(_stream(_showing_the_amplified_code(times_shown), True))
    return _written(writer)


def _showing_the_amplified_code(times_shown: int) -> bytes:
    """Return a content stream whose single Tj shows byte 0x01 times_shown times in font /F1."""
    shown = f"BT /F1 {_FONT_SIZE_POINTS} Tf {_LEFT_MARGIN_POINTS} {_FIRST_LINE_Y_POINTS} Td (".encode()
    return shown + _AMPLIFIED_CODE * times_shown + b") Tj ET"


def _mark_as_form(stream: DecodedStreamObject | EncodedStreamObject, font: object) -> None:
    """Make a stream a Form XObject covering the page, whose content shows text in font /F1."""
    stream[NameObject("/Type")] = NameObject("/XObject")
    stream[NameObject("/Subtype")] = NameObject("/Form")
    corners = (0, 0, _PAGE_WIDTH_POINTS, _PAGE_HEIGHT_POINTS)
    stream[NameObject("/BBox")] = ArrayObject([NumberObject(corner) for corner in corners])
    stream[NameObject("/Resources")] = _resources(font)


def _page_drawing_form(writer: PdfWriter, page_content: bytes, form: object, page_font: object) -> bytes:
    """Add one page showing page_content in page_font as /F1, then drawing form as /X1; return the PDF."""
    resources = _resources(page_font)
    _add_form_resource(resources, "/X1", form)
    page = writer.add_blank_page(_PAGE_WIDTH_POINTS, _PAGE_HEIGHT_POINTS)
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = writer._add_object(_stream(page_content + b" /X1 Do", True))
    return _written(writer)


def _add_form_resource(resources: DictionaryObject, name: str, form: object) -> None:
    """Make form drawable under name (such as "/X1") by content that uses these resources."""
    forms = DictionaryObject()
    forms[NameObject(name)] = form
    resources[NameObject("/XObject")] = forms


def _one_page_in_font(font: DictionaryObject, page_content: bytes) -> bytes:
    """Return a one-page PDF showing page_content, compressed, with font as /F1."""
    writer = PdfWriter()
    page = writer.add_blank_page(_PAGE_WIDTH_POINTS, _PAGE_HEIGHT_POINTS)
    page[NameObject("/Resources")] = _resources(writer._add_object(font))
    page[NameObject("/Contents")] = writer._add_object(_stream(page_content, True))
    return _written(writer)


def _encoded_stream(stored_data: bytes, filter_names: tuple[str, ...]) -> EncodedStreamObject:
    """Return a stream holding stored_data as-is, decoded by filter_names in order."""
    stream = EncodedStreamObject()
    names = [NameObject(name) for name in filter_names]
    if len(names) == 1:
        stream[NameObject("/Filter")] = names[0]
    else:
        stream[NameObject("/Filter")] = ArrayObject(names)
    stream._data = stored_data
    return stream


def _resources(font: object) -> DictionaryObject:
    """Return a page resource dictionary naming the font /F1."""
    fonts = DictionaryObject()
    fonts[NameObject("/F1")] = font
    resources = DictionaryObject()
    resources[NameObject("/Font")] = fonts
    return resources


def _stream(content: bytes, compress: bool) -> DecodedStreamObject | EncodedStreamObject:
    """Return a stream object holding content, Flate-compressed when compress is True."""
    stream = DecodedStreamObject()
    stream.set_data(content)
    if compress:
        return stream.flate_encode()
    return stream


def _written(writer: PdfWriter) -> bytes:
    """Return the bytes of the PDF the writer holds."""
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
