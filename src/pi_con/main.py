from __future__ import annotations

import argparse
import logging
import os
import sys
import threading

from .commands import run_command
from .config import load_settings
from .debug import DebugState, RingLogHandler
from .events import EventBus
from .listener import WakeWordListener
from .ha import HomeAssistant
from .router import Router

logger = logging.getLogger(__name__)


def restart_process() -> bool:
    """Exit shortly after answering the HTTP request; systemd (Restart=always) starts a fresh process."""
    logger.warning("Restart requested from the UI")
    threading.Timer(0.5, os._exit, args=(0,)).start()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Listen for an OpenWakeWord model and run a command.")
    parser.add_argument("--config", help="Path to a JSON settings file")
    parser.add_argument("--list-devices", action="store_true", help="Print audio inputs and exit")
    parser.add_argument("--verbose", action="store_true", help="Log live wake word scores to help tune the threshold")
    parser.add_argument("--test-skill", metavar="TEXT", help="Show which skill a spoken sentence would trigger, then exit (no microphone needed)")
    parser.add_argument("--download-models", action="store_true", help="Download the configured model and exit")
    parser.add_argument(
        "--wav",
        help="Replay a 16 kHz mono 16-bit WAV file in a loop instead of using the microphone (for testing without a mic)",
    )
    args = parser.parse_args()
    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_ring = RingLogHandler()
    logging.getLogger().addHandler(log_ring)
    settings = load_settings(args.config)
    if args.test_skill is not None:
        from .skills import load_skills, rank
        ranked = rank(args.test_skill, load_skills(settings.skills_file))[:3]
        for match in ranked:
            verdict = "TRIGGERS" if match.score >= settings.skill_threshold else "no"
            print(f"{match.score:.2f}  {verdict:8}  {match.skill.name}  (closest phrase: {match.phrase!r})")
        print(f"threshold {settings.skill_threshold:.2f}; heard as {args.test_skill!r}")
        return
    if args.download_models:
        from .models import ensure_models
        ensure_models(settings.wakeword)
        if settings.stt_enabled:
            from .stt import ensure_stt_model
            ensure_stt_model(settings.stt_model_dir)
        if settings.needle_enabled:
            from .fallback import NeedleFallback
            from .skills import load_skills
            NeedleFallback({camera.name: camera.entity for camera in settings.cameras}).warm_up(load_skills(settings.skills_file))
        return

    wav_frames = None
    if args.wav:
        from .audio import read_wav_frames
        try:
            wav_frames = read_wav_frames(args.wav, settings.sample_rate, settings.frame_samples)
        except (OSError, ValueError) as error:
            parser.error(str(error))

    bus = EventBus()
    debug = DebugState()
    transcriber = None
    if settings.stt_enabled:
        from .stt import Transcriber
        transcriber = Transcriber(settings.stt_model_dir, settings.stt_threads)
    ha = HomeAssistant(settings.ha_url, settings.ha_token)
    cameras = {camera.name: camera.entity for camera in settings.cameras}
    fallback = None
    if settings.needle_enabled:
        from .fallback import NeedleFallback
        fallback = NeedleFallback(cameras)
    Router(bus, settings.skills_file, cameras, ha, settings.skill_threshold, fallback).start()
    listener = WakeWordListener(
        settings, lambda: run_command(settings.command), bus=bus, wav_frames=wav_frames, transcriber=transcriber, debug=debug,
    )
    if settings.ui_port is not None:
        from .ui import start_ui
        start_ui(
            bus, settings.ui_port, on_listen=listener.request_listen,
            actions={"restart-audio": listener.request_audio_restart, "restart-app": restart_process},
            wake_phrase=settings.wake_phrase,
            ha=ha,
            cameras=cameras,
            debug_info=lambda: {**debug.snapshot(), "log": list(log_ring.lines)},
        )
    try:
        listener.run_forever()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        import sounddevice as sd
        if not isinstance(error, sd.PortAudioError):
            raise
        logger.error(
            "Cannot open the audio input (%s). Check `pi-con --list-devices` and 'audio_device'.", error,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
