"""PyInstaller entry point for kdcut-mcp.exe: channel stats for the Claude desktop app (stdio)."""
import sys

from kdcut.mcp_server import main

sys.exit(main())
