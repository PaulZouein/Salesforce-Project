"""``python -m webapp`` — open the local interface in your browser.

    python -m webapp              # your real Salesforce CLI and orgs
    python -m webapp --demo       # the simulated demo orgs, no Salesforce account needed
    python -m webapp --port 9000 --no-browser

Without ``--port`` it uses 8765, or the next free port after it. If a copy is already running
there, that copy is opened instead of starting a second one on the same tickets.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import urllib.request
import webbrowser
from typing import Any

from .server import DEFAULT_PORT, App, create_server

PORTS_TO_TRY = 10


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Ask the question the server's own bind will ask. On Windows that means insisting on being
        # alone on the port; elsewhere it means not being put off by a port that was only just closed.
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def running_copy(port: int) -> dict[str, Any] | None:
    """The status of an investigator page already answering on this port, or ``None`` if it is something else."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as response:
            data = json.loads(response.read())
    except Exception:  # noqa: BLE001 - anything that is not our page counts as "something else"
        return None
    return data if isinstance(data, dict) and data.get("ok") is True and "sample_ticket_key" in data else None


def open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - a missing browser must not stop the server
        pass


def self_test(demo: bool) -> int:
    """Start the page on a free port, ask it what a browser would ask, and stop. Used by the set-up check."""
    problems: list[str] = []
    app = App(demo=demo)
    server = create_server(app, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def ask(path: str) -> Any:
        with urllib.request.urlopen(base + path, timeout=120) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if path.startswith("/api/") else raw

    try:
        if app.token not in ask("/"):
            problems.append("the page was not served")
        status = ask("/api/status")
        if not status.get("ok"):
            problems.append("the status could not be read")
        if demo and len(ask("/api/orgs").get("orgs") or []) != 3:
            problems.append("the simulated orgs did not answer")
    except Exception as exc:  # noqa: BLE001 - whatever went wrong is the result of the test
        problems.append(f"{type(exc).__name__}: {exc}")
    finally:
        server.shutdown()
        server.server_close()
    if problems:
        print("The page did not pass its own check: " + "; ".join(problems), file=sys.stderr)
        return 1
    print("The page starts and answers" + (", and the simulated orgs answer." if demo else "."))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m webapp", description="Salesforce Ticket Investigator interface")
    parser.add_argument("--port", type=int, default=None,
                        help=f"port on 127.0.0.1 (default {DEFAULT_PORT}, or the next free one)")
    parser.add_argument("--demo", action="store_true", help="use the simulated demo orgs")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--self-test", action="store_true", help="start, check that the page answers, and stop")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test(args.demo)

    candidates = [args.port] if args.port else list(range(DEFAULT_PORT, DEFAULT_PORT + PORTS_TO_TRY))
    port = None
    for candidate in candidates:
        if port_is_free(candidate):
            port = candidate
            break
        copy = running_copy(candidate)
        if copy is not None:
            url = f"http://127.0.0.1:{candidate}/"
            print(f"The investigator is already running at {url} "
                  f"({'on the demo orgs' if copy.get('demo') else 'on your own orgs'}). Opening it.")
            print("To start it differently, close the window of that copy first.")
            if not args.no_browser:
                open_browser(url)
            return 0
    if port is None:
        wanted = f"port {args.port}" if args.port else f"ports {candidates[0]} to {candidates[-1]}"
        print(f"Could not listen on 127.0.0.1: {wanted} already in use by other programs. "
              "Choose another with --port.", file=sys.stderr)
        return 1

    app = App(demo=args.demo)
    try:
        server = create_server(app, port)
    except OSError as exc:
        print(f"Could not listen on 127.0.0.1:{port}: {exc}. Try another port with --port.", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Salesforce Ticket Investigator is running at {url}")
    print("It only answers on this computer. Press Ctrl+C to stop.")
    if app.demo:
        print("Demo mode: simulated orgs demo-prod, demo-sandbox and demo-dev. No real org is involved.")
    if not args.no_browser:
        open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
