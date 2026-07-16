from __future__ import annotations

import argparse
import os
import threading
import webbrowser

import uvicorn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="relay",
        description="Start the local Relay task-agent dashboard.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["demo"],
        help="Run the single simulated hackathon demo.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    os.environ["RELAY_MODE"] = "demo" if args.command == "demo" else "standard"

    host = "127.0.0.1"
    port = int(os.environ.get("RELAY_PORT", "8765"))
    url = f"http://{host}:{port}"
    threading.Timer(0.75, lambda: webbrowser.open(url)).start()
    uvicorn.run("relay_agent.app:create_app", factory=True, host=host, port=port)


if __name__ == "__main__":
    main()

