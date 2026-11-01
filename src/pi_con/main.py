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
    parser.add_argument("--ask", metavar="TEXT", help="Ask the AI (needs an API key) as if it was said: prints the answer and the tool calls WITHOUT running them")
    parser.add_argument("--provider", choices=("openai", "anthropic"), help="Provider for --ask (default: the saved one)")
    parser.add_argument("--say", metavar="TEXT", help="Speak TEXT with the configured voice and speaker, then exit (tests the spoken replies)")
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
    if args.ask is not None:
        from .ai.conversation import AiAssistant
        from .ai.state import AiState
        from .tools import Tool, build_tools
        from .skills import load_skills
        ask_bus = EventBus()
        from .secrets import SecretStore
        state = AiState(ask_bus, settings.ai or {}, path=os.devnull, store=SecretStore(".state/secrets.json"))
        provider = args.provider or state.provider
        if provider not in state.configured():
            parser.error(f"no API key for {provider}: save one in the settings panel of the UI, or set {'OPENAI_API_KEY' if provider == 'openai' else 'ANTHROPIC_API_KEY'} in .env")
        cams = {camera.name: camera.entity for camera in settings.cameras}
        skills = load_skills(settings.skills_file)

        def dry_run() -> list[Tool]:
            real = build_tools(type("NoRouter", (), {"run_skill": staticmethod(lambda skill: "ok")})(), cams, None, lambda: skills, "ai")
            return [Tool(t.name, t.description, t.schema, lambda a, n=t.name: print(f"  [tool] {n} {a}") or "ok (dry run: nothing was executed)", t.read_only) for t in real]

        print(f"{provider} ({state.model(provider)}):")
        print(AiAssistant(ask_bus, state, dry_run).ask(args.ask, provider) or "(no answer, see the error above)")
        for event in ask_bus.subscribe()[1]:
            if event["type"] == "status":
                print(event["message"])
        return
    tts_settings = settings.tts or {}
    voice = str(tts_settings.get("voice", "fr_FR-siwis-medium"))
    voice_dir = str(tts_settings.get("voice_dir", "models/tts-fr"))
    if args.say is not None:
        from .tts import PiperSynthesizer, play_sound, clean_for_speech
        audio, rate = PiperSynthesizer(voice_dir, voice)(clean_for_speech(args.say))
        play_sound(audio, rate, tts_settings.get("output_device"))
        return
    if args.download_models:
        from .models import ensure_models
        ensure_models(settings.wakeword)
        if settings.stt_enabled:
            from .stt import ensure_stt_model
            ensure_stt_model(settings.stt_model_dir)
        if tts_settings.get("download", True):
            from .tts import ensure_voice
            ensure_voice(voice_dir, voice)
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
    from .ai.state import AiState
    from .secrets import SecretStore
    store = SecretStore(".state/secrets.json")  # keys saved from the UI; .env values are the defaults
    ai_state = AiState(bus, settings.ai or {}, store=store)
    cloud_stt: dict = {}  # (key, model) -> transcriber, rebuilt when either is changed in the UI

    def cloud_transcribe():
        """The OpenAI transcriber while AI mode is on and an OpenAI key exists (the audio leaves the Pi only then)."""
        key = store.get("OPENAI_API_KEY")
        if not (ai_state.mode and ai_state.cloud_stt() and key):
            return None
        fingerprint = (key, ai_state.stt_model())
        if fingerprint not in cloud_stt:
            from .ai.stt_cloud import OpenAITranscriber
            cloud_stt.clear()
            cloud_stt[fingerprint] = OpenAITranscriber(key, ai_state.stt_model())
        return cloud_stt[fingerprint].transcribe

    transcriber = None
    if settings.stt_enabled:
        from .stt import Transcriber
        transcriber = Transcriber(settings.stt_model_dir, settings.stt_threads, cloud=cloud_transcribe)
    ha = HomeAssistant(settings.ha_url, settings.ha_token)
    cameras = {camera.name: camera.entity for camera in settings.cameras}
    fallback = None
    if settings.needle_enabled:
        from .fallback import NeedleFallback
        fallback = NeedleFallback(cameras)
    from .tts import PiperSynthesizer, Speaker
    speaker = Speaker(
        bus, PiperSynthesizer(voice_dir, voice), enabled=bool(tts_settings.get("enabled", False)),
        output_device=tts_settings.get("output_device"), voice=voice,
    )
    router = Router(bus, settings.skills_file, cameras, ha, settings.skill_threshold, fallback, speaker=speaker)
    from .ai.conversation import AiAssistant
    from .tools import build_tools as build_ai_tools
    router.ai = AiAssistant(
        bus, ai_state, lambda: build_ai_tools(router, cameras, ha, router.skills, "ai"),
        history_turns=int((settings.ai or {}).get("history_turns", 6)),
    )
    router.start()
    speaker.start()
    ai_state.announce()
    speaker.announce()
    mcp_server = None
    if settings.mcp_port is not None:
        from .mcp import build_tools, start_mcp
        mcp_server = start_mcp(settings.mcp_port, store.get("MCP_TOKEN"), lambda: build_tools(router, cameras, ha, router.skills, "mcp"))
    listener = WakeWordListener(
        settings, lambda: run_command(settings.command), bus=bus, wav_frames=wav_frames, transcriber=transcriber, debug=debug, muted=speaker.busy,
    )
    if settings.followup_seconds > 0:
        speaker.after_reply = lambda: listener.request_followup(settings.followup_seconds)
    if settings.ui_port is not None:
        from .ui import start_ui
        start_ui(
            bus, settings.ui_port, on_listen=listener.request_listen,
            actions={"restart-audio": listener.request_audio_restart, "restart-app": restart_process},
            wake_phrase=settings.wake_phrase,
            ha=ha,
            ai_state=ai_state, mcp=mcp_server, speaker=speaker,
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
