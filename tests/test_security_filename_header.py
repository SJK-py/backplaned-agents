"""Tests for the safe `Content-Disposition` filename encoding.

Original code interpolated `original_filename` directly into the
header f-string, allowing CR/LF response splitting and double-quote
attribute injection on download.

Fix: emit RFC 6266 form `attachment; filename="<ascii>"; filename*=UTF-8''<percent>`,
and reject control chars / quotes at upload time so the DB stays clean.
"""

from __future__ import annotations

import re

import pytest

from bp_router.filename_utils import (
    _FILENAME_REJECT,
)
from bp_router.filename_utils import (
    safe_filename_for_header as _safe_filename_for_header,
)

# ---------------------------------------------------------------------------
# Encoder output
# ---------------------------------------------------------------------------


def test_simple_ascii_filename_unchanged_in_filename_attr() -> None:
    out = _safe_filename_for_header("report.pdf")
    assert 'filename="report.pdf"' in out
    assert "filename*=UTF-8''report.pdf" in out


def test_unicode_filename_uses_rfc5987_star_form() -> None:
    """`filename=` must stay ASCII-safe; `filename*=` carries the
    UTF-8 percent-encoded original."""
    out = _safe_filename_for_header("réport-日本語.pdf")
    # filename= got the non-ASCII chars stripped.
    m = re.search(r'filename="([^"]+)"', out)
    assert m is not None
    assert m.group(1).isascii()
    # filename*= preserves the original via percent-encoding.
    assert "filename*=UTF-8''" in out
    assert "%C3%A9" in out  # é
    assert "%E6%97%A5" in out  # 日


def test_quote_in_filename_does_not_break_quoted_string() -> None:
    """A literal `"` in the filename used to break out of the quoted
    string. Now it's collapsed to underscore in the ASCII fallback,
    while filename*= preserves the original."""
    out = _safe_filename_for_header('weird".name.txt')
    # Count double-quotes — should be exactly 2 (the wrappers around
    # the ASCII fallback). If a third snuck in, the header is broken.
    assert out.count('"') == 2
    m = re.search(r'filename="([^"]+)"', out)
    assert m is not None
    assert '"' not in m.group(1)
    assert "%22" in out  # the encoded "


def test_crlf_in_filename_gets_collapsed() -> None:
    """CR/LF would enable response splitting (header injection).
    Collapsed to underscore in the ASCII attr; percent-encoded in
    filename*= so it can never appear raw in the header."""
    out = _safe_filename_for_header("a\r\nX-Pwn: 1\r\n\r\nb.txt")
    # No raw CR or LF anywhere in the header value.
    assert "\r" not in out
    assert "\n" not in out
    # The encoded form has %0D%0A.
    assert "%0D%0A" in out


def test_directory_traversal_segments_stripped_from_ascii() -> None:
    """`../etc/passwd` shouldn't end up as a literal in the ASCII
    filename (although it's harmless in a Content-Disposition header,
    a downloader saving as it lands in `passwd` not `../etc/passwd`)."""
    out = _safe_filename_for_header("../etc/passwd")
    m = re.search(r'filename="([^"]+)"', out)
    assert m is not None
    # Only the basename survives.
    assert m.group(1) == "passwd"


def test_empty_filename_falls_back_to_download() -> None:
    out = _safe_filename_for_header("")
    assert 'filename="download"' in out


def test_all_unsafe_chars_substituted_with_underscores() -> None:
    """Each unsafe char is collapsed to underscore in the ASCII attr,
    preserving length but never breaking the quoted string."""
    out = _safe_filename_for_header('"""')
    assert 'filename="___"' in out
    assert "filename*=UTF-8''%22%22%22" in out


def test_filename_that_collapses_to_empty_falls_back_to_download() -> None:
    """A filename whose ASCII form is entirely non-printable (only
    non-ASCII source chars) collapses to empty, which triggers the
    "download" placeholder."""
    out = _safe_filename_for_header("日本語")
    assert 'filename="download"' in out
    # filename*= still preserves the original.
    assert "filename*=UTF-8''%E6%97%A5%E6%9C%AC%E8%AA%9E" in out


# ---------------------------------------------------------------------------
# Upload-time rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    'report".txt',
    "line1\r\nline2.txt",
    "embed\x00null.txt",
    "\x1bescape.txt",
    'mixed"quote\nand-newline.txt',
])
def test_upload_filename_regex_matches_dangerous_chars(bad: str) -> None:
    assert _FILENAME_REJECT.search(bad) is not None


@pytest.mark.parametrize("ok", [
    "report.pdf",
    "réport-日本語.pdf",
    "snake_case.txt",
    "spaces are fine.txt",
    "punctuation;,.()[]{}.txt",
    "../parent.txt",        # traversal — handled at download, OK to store
    "héllo wörld.zip",
])
def test_upload_filename_regex_accepts_normal_filenames(ok: str) -> None:
    assert _FILENAME_REJECT.search(ok) is None
