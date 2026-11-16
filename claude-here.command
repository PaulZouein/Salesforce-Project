#!/bin/sh
# Open Claude Code in this folder with the investigator's tools (for working in the terminal, and for apply mode).
cd "${0%/*}" 2>/dev/null || cd "$(dirname "$0")" || exit 1
exec sh ./start.command --claude "$@"
