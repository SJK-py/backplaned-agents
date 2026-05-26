"""bp_router.filename_utils — Safe encoding for ``Content-Disposition``
filenames.

Content-Disposition is one of the easier places to introduce header
injection: a filename containing CR/LF or a literal double-quote
breaks out of the quoted-string and lets a downloader inject extra
header attributes (or fully split the response on proxies that don't
strip control chars).

Two helpers:

  - ``_FILENAME_REJECT``: regex for control chars + ``"``. Used at
    upload time to reject obviously-malformed filenames so the DB
    only ever stores the well-behaved set.
  - ``safe_filename_for_header(name)``: emits an RFC 6266 / RFC 5987
    pair: an ASCII-safe ``filename="..."`` (defanged where needed)
    plus ``filename*=UTF-8''<percent-encoded>`` for clients that
    speak the modern form.

Lives outside `bp_router.api` so tests can exercise it without
pulling in fastapi.
"""

from __future__ import annotations

import re
from urllib.parse import quote

# Control characters and double-quote are rejected at upload time so
# we don't have to defang on every download. CR/LF in particular
# enable response splitting through `Content-Disposition`.
_FILENAME_REJECT = re.compile(r"[\x00-\x1f\x7f\"]")


def safe_filename_for_header(name: str) -> str:
    """Build an RFC 6266 / RFC 5987 ``Content-Disposition`` value.

    Emits BOTH a sanitised ASCII ``filename=`` (for legacy clients)
    and ``filename*=UTF-8''<percent-encoded>`` (for everything modern).
    The ASCII fallback strips non-ASCII and the chars that would break
    the quoted-string format.
    """
    base = name.replace("\\", "/").split("/")[-1] or "download"
    ascii_safe = base.encode("ascii", "ignore").decode("ascii")
    # Collapse runs of unsafe chars to underscore so filename="..." is
    # always a well-formed quoted string.
    ascii_safe = re.sub(r"[\\\"\x00-\x1f\x7f]", "_", ascii_safe) or "download"
    star = quote(base, safe="")
    return f'attachment; filename="{ascii_safe}"; filename*=UTF-8\'\'{star}'
