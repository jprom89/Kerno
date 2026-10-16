"""
swallow_sites.py — failures planted inside the try blocks where pypdf 6.14.2 catches and ignores them.

What:  For each place in pypdf 6.14.2 where text extraction catches an
       exception and carries on, plant() replaces one callee that pypdf
       calls inside that try block, so that it raises a MemoryError (or, at
       the site that re-raises a limit error as an ordinary read error,
       pypdf's LimitReachedError) whenever it is called from that site's
       function, and otherwise behaves exactly as before. SITE_LOCATIONS
       names the pypdf file, lines and handler each site targets. The
       failure fires every time, so a run proves the handler swallowed it
       each time; PlantedFailure.raised counts how often it fired.
Why:   pypdf's broad handlers ("except Exception: pass", handlers that log
       and move on, a handler that turns LimitReachedError into PdfReadError
       for an outer handler to swallow) would let a document that ran out of
       memory, or hit a limit, come back as a silently truncated success.
       The worker's observer must refuse such a document at every one of
       these sites, and a planted failure is the only way to put the
       exception exactly inside each try block of the real, unmodified
       pypdf. Shared by the in-process tests and by probe_real_worker.py,
       which plants one site inside a real worker process.
How:   failure = plant(site_name, set_attribute), where set_attribute is
       monkeypatch.setattr with raising=False in a test (so everything is
       put back afterwards) and the builtin setattr in a child process.
       Standard library only at module level; pypdf's submodules are
       imported inside plant(), so a child process imports pypdf only when
       the worker does. Never collected by pytest.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any, Callable

PLANTED_MESSAGE = "failure planted by the Kerno PDF worker tests"
# The value build_pdf_with_nested_page_dictionary marks; a planted dictionary
# failure fires right after pypdf reads it.
DICTIONARY_MARKER = "KernoPlantedFailure"
_DICTIONARY_MARKER_TOKEN = b"(" + DICTIONARY_MARKER.encode("ascii") + b")"
# Frames above fire_if_called_from_site: the planted callee, then the
# function that called it, which must be the site's.
_SITE_FRAME_DEPTH = 2
_CHARMAP_ENCODING = "charmap"
# The base encoding build_pdf_with_base_encoding names for the base-encoding
# site; pypdf looks it up inside a broad handler.
BASE_ENCODING_NAME = "/WinAnsiEncoding"

SITE_LOCATIONS = {
    "line-break-check": "_text_extraction/__init__.py:130-149, crlf_space_check, except Exception: pass",
    "graphics-state-restore": "_text_extraction/_text_extractor.py:240-251, Q handler, except Exception",
    "matrix-concatenation": "_text_extraction/_text_extractor.py:259-262, cm handler, except Exception",
    "font-size": "_text_extraction/_text_extractor.py:303-306, Tf font size, except Exception: pass",
    "encoding-name": "_cmap.py:79-92, /Encoding name lookup, except Exception (logged)",
    "base-encoding": "_cmap.py:94-101, /BaseEncoding lookup, except Exception (logged)",
    "glyph-name": "_cmap.py:111-115, /Differences glyph lookup, except Exception",
    "string-decoding": "_text_extraction/__init__.py:178-187, shown-string decode fallback, except Exception",
    "form-xobject": "_page.py:1904-1945, Form XObject handler, except Exception (logged)",
    "dictionary-value": "generic/_data_structures.py:625-631, dictionary value read, except Exception (logged)",
    "dictionary-value-limit": (
        "generic/_data_structures.py:623-624, LimitReachedError re-raised as PdfReadError, "
        "then swallowed by the enclosing dictionary's except Exception (625-631)"
    ),
}
SITE_NAMES = tuple(SITE_LOCATIONS)

SetAttribute = Callable[[Any, str, Any], None]


class PlantedFailure:
    """One failure planted at one swallow site; raised counts how many times it fired."""

    def __init__(self, failure_type: type[BaseException], site_function: str | None) -> None:
        """Plant failure_type for calls made from site_function, or from anywhere when it is None."""
        self.failure_type = failure_type
        self.site_function = site_function
        self.raised = 0

    def fire_if_called_from_site(self) -> None:
        """Raise the planted failure when the planted callee was called from the site's function."""
        caller = sys._getframe(_SITE_FRAME_DEPTH).f_code.co_name
        if self.site_function is None or caller == self.site_function:
            self.raised += 1
            raise self.failure_type(PLANTED_MESSAGE)


class _FailingItemLookups(dict):
    """A copy of one of pypdf's lookup tables whose item lookups (of one key, or of any) fail as planted."""

    def __init__(self, table: dict, failure: PlantedFailure, failing_key: str | None = None) -> None:
        """Copy table; lookups of failing_key, or of any key when it is None, fail as planted."""
        super().__init__(table)
        self._failure = failure
        self._failing_key = failing_key

    def __getitem__(self, key: Any) -> Any:
        """Fail as planted for the failing key when called from the site, else look up as a dict does."""
        if self._failing_key is None or key == self._failing_key:
            self._failure.fire_if_called_from_site()
        return dict.__getitem__(self, key)


class _FailingMembershipTests(dict):
    """A copy of one of pypdf's lookup tables whose "in" tests fail as planted."""

    def __init__(self, table: dict, failure: PlantedFailure) -> None:
        """Copy table and remember the failure to plant."""
        super().__init__(table)
        self._failure = failure

    def __contains__(self, key: Any) -> bool:
        """Fail as planted when called from the site, else test membership as a dict does."""
        self._failure.fire_if_called_from_site()
        return dict.__contains__(self, key)


class _FailingPops(list):
    """pypdf's graphics-state stack, whose pop fails as planted."""

    def __init__(self, items: list, failure: PlantedFailure) -> None:
        """Copy the stack and remember the failure to plant."""
        super().__init__(items)
        self._failure = failure

    def pop(self, *arguments: Any) -> Any:
        """Fail as planted when called from the site, else pop as a list does."""
        self._failure.fire_if_called_from_site()
        return list.pop(self, *arguments)


def _failing_callee(
    original: Callable[..., Any], failure: PlantedFailure, should_fire: Callable[..., bool] | None = None
) -> Callable[..., Any]:
    """Return original wrapped so that, called from the site (and when should_fire agrees), it fails as planted."""

    def planted_callee(*arguments: Any, **keyword_arguments: Any) -> Any:
        """Fail as planted when due, else behave exactly as the original."""
        if should_fire is None or should_fire(*arguments, **keyword_arguments):
            failure.fire_if_called_from_site()
        return original(*arguments, **keyword_arguments)

    return planted_callee


def _decoding_as_charmap(data: bytes, encoding: str = "utf-8", errors: str = "strict") -> bool:
    """Return whether a bytes.decode call asks for "charmap", the font's own encoding at the string-decoding site."""
    return encoding == _CHARMAP_ENCODING


def _plant_line_break_check(set_attribute: SetAttribute) -> PlantedFailure:
    """Make abs() fail inside crlf_space_check's try block (a module global shadowing the builtin)."""
    module = importlib.import_module("pypdf._text_extraction")
    failure = PlantedFailure(MemoryError, "crlf_space_check")
    set_attribute(module, "abs", _failing_callee(abs, failure))
    return failure


def _plant_graphics_state_restore(set_attribute: SetAttribute) -> PlantedFailure:
    """Make the graphics-state stack's pop fail inside the Q handler's try block."""
    extraction_class = importlib.import_module("pypdf._text_extraction._text_extractor").TextExtraction
    failure = PlantedFailure(MemoryError, "_handle_restore_graphics_state")
    original_initialize = extraction_class.initialize_extraction

    def initialize_with_a_failing_stack(extraction: Any, *arguments: Any, **keyword_arguments: Any) -> None:
        """Initialise as pypdf does, then give the extraction a stack whose pop fails as planted."""
        original_initialize(extraction, *arguments, **keyword_arguments)
        extraction.cm_stack = _FailingPops(extraction.cm_stack, failure)

    set_attribute(extraction_class, "initialize_extraction", initialize_with_a_failing_stack)
    return failure


def _plant_matrix_concatenation(set_attribute: SetAttribute) -> PlantedFailure:
    """Make mult() fail inside the cm handler's try block."""
    module = importlib.import_module("pypdf._text_extraction._text_extractor")
    failure = PlantedFailure(MemoryError, "_handle_cm")
    set_attribute(module, "mult", _failing_callee(module.mult, failure))
    return failure


def _plant_font_size(set_attribute: SetAttribute) -> PlantedFailure:
    """Make float() fail inside the Tf handler's font-size try block (a module global shadowing the builtin)."""
    module = importlib.import_module("pypdf._text_extraction._text_extractor")
    failure = PlantedFailure(MemoryError, "_handle_tf")
    set_attribute(module, "float", _failing_callee(float, failure))
    return failure


def _plant_encoding_name(set_attribute: SetAttribute) -> PlantedFailure:
    """Make the "in charset_encoding" test fail inside _parse_encoding's /Encoding-name try block."""
    module = importlib.import_module("pypdf._cmap")
    failure = PlantedFailure(MemoryError, "_parse_encoding")
    set_attribute(module, "charset_encoding", _FailingMembershipTests(module.charset_encoding, failure))
    return failure


def _plant_base_encoding(set_attribute: SetAttribute) -> PlantedFailure:
    """Make the /BaseEncoding table lookup fail inside its try block, leaving the handler's own lookup working."""
    module = importlib.import_module("pypdf._cmap")
    failure = PlantedFailure(MemoryError, "_parse_encoding")
    lookups = _FailingItemLookups(module.charset_encoding, failure, BASE_ENCODING_NAME)
    set_attribute(module, "charset_encoding", lookups)
    return failure


def _plant_glyph_name(set_attribute: SetAttribute) -> PlantedFailure:
    """Make every glyph-name lookup fail inside the /Differences loop's try block."""
    module = importlib.import_module("pypdf._cmap")
    failure = PlantedFailure(MemoryError, "_parse_encoding")
    set_attribute(module, "adobe_glyphs", _FailingItemLookups(module.adobe_glyphs, failure))
    return failure


def _plant_string_decoding(set_attribute: SetAttribute) -> PlantedFailure:
    """Make a shown string's decode with the font's "charmap" encoding fail, leaving the fallback decode working."""
    byte_string_class = importlib.import_module("pypdf.generic").ByteStringObject
    failure = PlantedFailure(MemoryError, "get_text_operands")
    planted = _failing_callee(byte_string_class.decode, failure, _decoding_as_charmap)
    set_attribute(byte_string_class, "decode", planted)
    return failure


def _plant_form_xobject(set_attribute: SetAttribute) -> PlantedFailure:
    """Make PageObject.extract_xform_text fail; pypdf calls it only inside the Form XObject handler's try block.

    No caller check: the worker wraps the method too, so its caller may be
    the worker's wrapper rather than pypdf's _extract_text.
    """
    page_class = importlib.import_module("pypdf").PageObject
    failure = PlantedFailure(MemoryError, None)
    set_attribute(page_class, "extract_xform_text", _failing_callee(page_class.extract_xform_text, failure))
    return failure


def _plant_dictionary_value(set_attribute: SetAttribute, failure_type: type[BaseException]) -> PlantedFailure:
    """Make a dictionary read fail, as failure_type, right after it reads the marked value."""
    module = importlib.import_module("pypdf.generic._data_structures")
    failure = PlantedFailure(failure_type, "read_from_stream")
    original_read = module.read_object

    def read_object_failing_after_the_marker(stream: Any, *arguments: Any, **keyword_arguments: Any) -> Any:
        """Read one object as pypdf does; if it was the marked value, then fail as planted."""
        position = stream.tell()
        upcoming = stream.read(len(_DICTIONARY_MARKER_TOKEN))
        stream.seek(position)
        value = original_read(stream, *arguments, **keyword_arguments)
        if upcoming == _DICTIONARY_MARKER_TOKEN:
            failure.fire_if_called_from_site()
        return value

    set_attribute(module, "read_object", read_object_failing_after_the_marker)
    return failure


def _plant_dictionary_memory_error(set_attribute: SetAttribute) -> PlantedFailure:
    """Plant a MemoryError at the marked dictionary value."""
    return _plant_dictionary_value(set_attribute, MemoryError)


def _plant_dictionary_limit(set_attribute: SetAttribute) -> PlantedFailure:
    """Plant pypdf's LimitReachedError at the marked dictionary value."""
    limit_error = importlib.import_module("pypdf.errors").LimitReachedError
    return _plant_dictionary_value(set_attribute, limit_error)


_PLANTERS: dict[str, Callable[[SetAttribute], PlantedFailure]] = {
    "line-break-check": _plant_line_break_check,
    "graphics-state-restore": _plant_graphics_state_restore,
    "matrix-concatenation": _plant_matrix_concatenation,
    "font-size": _plant_font_size,
    "encoding-name": _plant_encoding_name,
    "base-encoding": _plant_base_encoding,
    "glyph-name": _plant_glyph_name,
    "string-decoding": _plant_string_decoding,
    "form-xobject": _plant_form_xobject,
    "dictionary-value": _plant_dictionary_memory_error,
    "dictionary-value-limit": _plant_dictionary_limit,
}


def plant(site_name: str, set_attribute: SetAttribute) -> PlantedFailure:
    """Plant the named site's failure in the imported pypdf, changing attributes only through set_attribute."""
    return _PLANTERS[site_name](set_attribute)
