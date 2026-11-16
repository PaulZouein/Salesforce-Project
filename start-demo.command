#!/bin/sh
# Salesforce Ticket Investigator on the simulated demo orgs: no Salesforce org needed. macOS: double-click.
cd "${0%/*}" 2>/dev/null || cd "$(dirname "$0")" || exit 1
exec sh ./start.command --demo "$@"
