#!/bin/sh
# Salesforce Ticket Investigator: set up (first time) and start. macOS: double-click this file.
# The same with the simulated demo orgs: start-demo.command.
# The folder this file is in, without needing any other program to work it out.
cd "${0%/*}" 2>/dev/null || cd "$(dirname "$0")" || exit 1

# Double-clicked scripts do not always get the folders where Python, Node and Homebrew put their commands.
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:$HOME/.local/bin"
export PATH

PY=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done

if [ -z "$PY" ]; then
  echo
  echo "Python 3.11 or newer was not found on this Mac."
  echo
  echo "  1. Install it from https://www.python.org/downloads/"
  echo "     (or, with Homebrew:  brew install python@3.13)"
  echo "  2. Double-click this file again."
  echo
  printf "Press Return to close. "
  read -r _
  exit 1
fi

"$PY" scripts/bootstrap.py "$@"
status=$?
if [ "$status" -ne 0 ]; then
  echo
  printf "Press Return to close. "
  read -r _
fi
exit "$status"
