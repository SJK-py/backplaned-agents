"""Isolated MarkItDown conversion worker.

md_converter's `convert` handler runs each file→Markdown conversion in a
SEPARATE process (this module) so a conversion that crashes the interpreter
(a segfault in a native parser) or gets OOM-killed on a huge/complex file
takes down only this short-lived child — not the shared md_converter agent
that serves every user. In-process, such a death produced NO error log and
NO result frame: the task simply vanished from the admin panel. As a child,
the same death is just a non-zero exit / fatal signal the parent translates
into a clean failed result.

Usage (invoked by `agent._convert_isolated`, not by hand):

    python -m bp_agents.agents.md_converter._worker <in_path> <out_path>

Reads <in_path>, writes the Markdown (UTF-8) to <out_path>, exits 0. Any
conversion error prints a one-line reason to stderr and exits 3.
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: _worker <in_path> <out_path>", file=sys.stderr)
        return 2
    in_path, out_path = argv[1], argv[2]
    # Import here (not at module top) so a missing optional dependency or a
    # MarkItDown import failure surfaces as a worker error the parent can
    # report — not an import-time crash before argv is even parsed.
    from bp_agents.agents.md_converter.agent import _markitdown_file  # noqa: PLC0415

    text = _markitdown_file(in_path)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — report ANY failure, then exit
        # A one-line, bounded reason for the parent's logs. The full traceback
        # isn't needed on the wire; the parent logs returncode + this line.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(3) from None
