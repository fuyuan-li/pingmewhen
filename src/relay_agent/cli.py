from __future__ import annotations

import argparse
from copy import deepcopy
import os
import sys
import threading
import webbrowser

import uvicorn
from dotenv import load_dotenv

from relay_agent.call_capabilities import CapabilityAccessLogFilter
from relay_agent.local_tts import LocalTTSUnavailable, MacOSLocalTTS


def relay_log_config() -> dict:
    config = deepcopy(uvicorn.config.LOGGING_CONFIG)
    config.setdefault("filters", {})["relay_capabilities"] = {"()": CapabilityAccessLogFilter}
    access_handler = config["handlers"]["access"]
    access_handler["filters"] = [*access_handler.get("filters", []), "relay_capabilities"]
    return config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pingmewhen",
        description="Start the local PingMeWhen task-agent dashboard.",
    )
    parser.add_argument(
        "--check-install",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.check_install:
        try:
            chunks = MacOSLocalTTS().render_text("PingMeWhen installation check")
        except (LocalTTSUnavailable, ValueError) as error:
            print(f"PingMeWhen installation check failed: {error}", file=sys.stderr)
            raise SystemExit(1) from error
        if not chunks:
            print("PingMeWhen installation check failed: local speech returned no audio.", file=sys.stderr)
            raise SystemExit(1)
        print("PingMeWhen is installed and macOS on-device speech is ready.")
        return

    load_dotenv()
    host = "127.0.0.1"
    port = int(os.environ.get("RELAY_PORT", "8765"))
    url = f"http://{host}:{port}"
    threading.Timer(0.75, lambda: webbrowser.open(url)).start()
    uvicorn.run("relay_agent.app:create_app", factory=True, host=host, port=port, log_config=relay_log_config())


if __name__ == "__main__":
    main()
