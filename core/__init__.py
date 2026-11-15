"""Salesforce investigation core.

All Salesforce logic and every safety check live here. The package depends only on the
Python standard library and knows nothing about Claude Code or MCP, so other front ends
(a web app, another agent project) can import it directly.

Start with :class:`core.app.Investigator`.
"""

__version__ = "0.1.0"
