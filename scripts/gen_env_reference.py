#!/usr/bin/env python3
"""Generate the complete environment-variable reference from the settings models.

    python scripts/gen_env_reference.py            # write docs/env-reference.md
    python scripts/gen_env_reference.py --check    # fail if it would change

Implements `docs/design/deployment-agent-host.md` §1.3. `.env.example`
documents 24 of the router's 104 settings fields while the README calls it
"every configurable environment variable", and the dict-shaped ones
(`file_storage_quota_bytes`, `session_store_quota_bytes`,
`llm_default_presets`) appear nowhere — so an operator tuning a quota has to
read `settings.py` to learn the variable exists.

Generating it is what makes it stay true: `.env.example` remains the curated
quick-start (the handful a deployment actually sets), and this is the
exhaustive reference, pinned by a test so it cannot drift.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

OUT_PATH = Path("docs/env-reference.md")

HEADER = """# Environment-variable reference

<!-- GENERATED FILE — do not edit.
     Regenerate with: python scripts/gen_env_reference.py
     Source of truth: the settings models listed below. -->

Every setting each component reads, with its default. This file is generated
from the Pydantic settings models, so it cannot drift from the code.

For the handful a deployment actually sets, start with
[`.env.example`](../.env.example) (router / SDK / suite quick-start) and
[`deploy/.env.prod.example`](../deploy/.env.prod.example) (production).

Defaults shown as `(required)` have no default — the process will not start
without them. Defaults shown as `(computed)` are derived at load time.
"""


def _models() -> list[tuple[str, str, Any]]:
    """(title, env prefix, model) for each component that reads the env."""
    out: list[tuple[str, str, Any]] = []
    from bp_router.settings import Settings as RouterSettings  # noqa: PLC0415

    out.append(("Router (`bp_router`)", "ROUTER_", RouterSettings))
    try:
        from bp_sdk.settings import AgentConfig  # noqa: PLC0415

        out.append(("Agent SDK (`bp_sdk`)", "AGENT_", AgentConfig))
    except Exception:  # noqa: BLE001 - optional extra
        pass
    try:
        from bp_agents.settings import SuiteSettings  # noqa: PLC0415

        out.append(("Agent suite (`bp_agents`)", "SUITE_", SuiteSettings))
    except Exception:  # noqa: BLE001 - optional extra
        pass
    return out


def _default_repr(field: Any) -> str:
    from pydantic_core import PydanticUndefined  # noqa: PLC0415

    if field.default_factory is not None:  # type: ignore[union-attr]
        try:
            value = field.default_factory()  # type: ignore[misc]
        except Exception:  # noqa: BLE001
            return "(computed)"
        return f"`{value!r}`"
    if field.default is PydanticUndefined:
        return "(required)"
    return f"`{field.default!r}`"


def _describe(field: Any) -> str:
    text = (field.description or "").strip()
    # Collapse to one line: the table is a lookup, not prose. The design docs
    # carry the reasoning.
    return " ".join(text.split()) if text else ""


def render() -> str:
    parts = [HEADER]
    for title, prefix, model in _models():
        parts.append(f"\n## {title}\n")
        parts.append(f"Environment prefix: `{prefix}`\n")
        parts.append("| variable | default | notes |")
        parts.append("| --- | --- | --- |")
        for name, field in sorted(model.model_fields.items()):
            env_name = f"{prefix}{name.upper()}"
            note = _describe(field).replace("|", "\\|")
            parts.append(f"| `{env_name}` | {_default_repr(field)} | {note} |")
        parts.append("")
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed file is stale",
    )
    args = parser.parse_args(argv)

    content = render()
    if args.check:
        current = OUT_PATH.read_text() if OUT_PATH.exists() else ""
        if current != content:
            print(
                f"{OUT_PATH} is stale — run: python scripts/gen_env_reference.py",
                file=sys.stderr,
            )
            return 1
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(content)
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
