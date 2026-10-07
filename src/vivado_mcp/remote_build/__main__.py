"""Run the remote build CLI without starting a local MCP/GUI session."""

from .core import main

if __name__ == "__main__":
    raise SystemExit(main())
