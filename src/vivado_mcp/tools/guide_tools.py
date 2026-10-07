"""A live source-guide entry for cached plugin bootstraps and other scaffolds."""

import json

from mcp.types import ToolAnnotations

from vivado_mcp.guide import GuideSection, read_guide
from vivado_mcp.server import mcp


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def vivado_guide(section: GuideSection = "overview", reference: str = "") -> str:
    """Read the current source Skill, remote guide or an explicitly listed reference.

    overview returns the source root and canonical Skill; remote/remote-tcl describe
    the existing remote CLI. reference accepts only a returned reference filename.
    Every call reads the maintained file again. No EDA/SSH or config files are read.
    """
    try:
        return json.dumps(read_guide(section, reference), ensure_ascii=False)
    except (ValueError, OSError) as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
