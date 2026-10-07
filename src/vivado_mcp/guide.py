"""Read maintained product guidance on demand, with explicit document routes."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Literal

SOURCE_ROOT = Path(__file__).resolve().parents[2]
GuideSection = Literal["overview", "remote", "remote-tcl", "reference"]
SECTIONS = {
    "overview": "skills/otter-vivado/SKILL.md",
    "remote": "docs/REMOTE_BUILD.md",
    "remote-tcl": "docs/REMOTE_TCL.md",
}
REFERENCES = (
    "analysis.md",
    "controls.md",
    "debug-delivery.md",
    "evidence-and-docs.md",
    "experiment.md",
    "hardware-debug.md",
    "ross.md",
    "visualization.md",
    "waveform.md",
    "workflows.md",
)


def read_guide(section: GuideSection = "overview", reference: str = "") -> dict:
    """Only maintained guides are readable; no config, arbitrary path or credential access."""
    if section == "reference":
        if reference not in REFERENCES:
            raise ValueError(f"Choose a reference from: {', '.join(REFERENCES)}")
        relative = f"skills/otter-vivado/references/{reference}"
    else:
        if section not in SECTIONS:
            raise ValueError("Choose overview, remote, remote-tcl or reference")
        if reference:
            raise ValueError("reference is only accepted with section=reference")
        relative = SECTIONS[section]

    root = SOURCE_ROOT.resolve()
    expected = root / relative
    document = expected.resolve(strict=True)
    if document != expected or not document.is_relative_to(root):
        raise ValueError("Guide must be the maintained source file, without redirected paths")
    content = document.read_text(encoding="utf-8")
    return {
        "section": section,
        "source_root": str(root),
        "path": relative,
        "content": content,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "cli": [sys.executable, "-m", "vivado_mcp"],
        "sections": [*SECTIONS, "reference"],
        "references": list(REFERENCES),
    }
