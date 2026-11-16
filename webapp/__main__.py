"""``python -m webapp`` — open the local interface in your browser.

    python -m webapp              # your real Salesforce CLI and orgs
    python -m webapp --demo       # the simulated demo orgs, no Salesforce account needed
    python -m webapp --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import sys
import webbrowser

from .server import DEFAULT_PORT, App, create_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m webapp", description="Salesforce Ticket Investigator interface")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port on 127.0.0.1 (default {DEFAULT_PORT})")
    parser.add_argument("--demo", action="store_true", help="use the simulated demo orgs")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)

    app = App(demo=args.demo)
    try:
        server = create_server(app, args.port)
    except OSError as exc:
        print(f"Could not listen on 127.0.0.1:{args.port}: {exc}. Try another port with --port.", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Salesforce Ticket Investigator is running at {url}")
    print("It only answers on this computer. Press Ctrl+C to stop.")
    if app.demo:
        print("Demo mode: simulated orgs demo-prod, demo-sandbox and demo-dev. No real org is involved.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - a missing browser must not stop the server
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
