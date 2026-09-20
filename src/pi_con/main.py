from __future__ import annotations

import argparse
import logging

from .commands import run_command
from .config import load_settings
from .listener import WakeWordListener


def main() -> None:
    parser = argparse.ArgumentParser(description="Listen for an OpenWakeWord model and run a command.")
    parser.add_argument("--config", help="Path to a JSON settings file")
    parser.add_argument("--list-devices", action="store_true", help="Print audio inputs and exit")
    args = parser.parse_args()
    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)
    WakeWordListener(settings, lambda: run_command(settings.command)).run_forever()


if __name__ == "__main__":
    main()
