#!/bin/sh
# Launcher for the MCP server.
#
# Claude starts MCP servers from an arbitrary working directory, while both
# .env discovery and the default relative WHATSAPP_DB_PATH resolve against
# the current directory. Anchoring to this script's own location makes the
# server work no matter where it is invoked from.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python -m whatsapp.mcp_server
