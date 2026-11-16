"""Local web interface for the Salesforce Ticket Investigator.

``python -m webapp`` serves a page on 127.0.0.1 where you check the setup, connect an org, paste
a ticket and read the result: root cause, evidence, fix and a draft comment.

The interface is a front end for reading and for starting investigations. It has no way to
change a Salesforce org: it calls read-only functions of ``core`` only, and the investigation
it starts runs unattended, where every write tool is refused because nobody is there to approve
it. Applying a fix stays in Claude Code, where each write asks.
"""

__version__ = "0.1.0"
