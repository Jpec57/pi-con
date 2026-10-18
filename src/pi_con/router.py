from __future__ import annotations

import logging
import threading
from pathlib import Path

from .commands import run_command
from .events import EventBus
from .fallback import NeedleFallback
from .ha import HomeAssistant, HomeAssistantError
from .skills import CONTROL_TYPES, Skill, best_match, load_skills, rank

logger = logging.getLogger(__name__)


class Router:
    """Turns transcripts into actions by matching them against the skills in the skills file.

    The file is re-read when it changes, so skills can be added on the Pi without restarting. LLM fallback comes later.
    """

    def __init__(self, bus: EventBus, skills_path: str | Path, cameras: dict[str, str], ha: HomeAssistant | None, threshold: float, fallback: NeedleFallback | None = None, ai=None) -> None:
        self.bus = bus
        self.skills_path = Path(skills_path)
        self.cameras = cameras
        self.ha = ha
        self.threshold = threshold
        self.fallback = fallback
        self.ai = ai  # AiAssistant or None
        self._skills: tuple[Skill, ...] = ()
        self._loaded_mtime = -1.0

    def skills(self) -> tuple[Skill, ...]:
        """Current skills; a broken edit keeps the previous ones and is reported once."""
        try:
            mtime = self.skills_path.stat().st_mtime
        except OSError:
            return self._skills
        if mtime != self._loaded_mtime:
            self._loaded_mtime = mtime
            try:
                self._skills = load_skills(self.skills_path)
                logger.info("Loaded %d skills from %s", len(self._skills), self.skills_path)
            except (ValueError, OSError) as error:  # json.JSONDecodeError is a ValueError
                logger.error("Cannot load %s: %s", self.skills_path, error)
                self._notify(f"Skills file ignored: {error}")
        return self._skills

    def _notify(self, message: str) -> None:
        self.bus.publish("status", state="info", message=message)

    def handle(self, text: str) -> bool:
        skills = self.skills()
        match = best_match(text, skills, self.threshold)
        if self.ai is not None and self.ai.state.mode and (match is None or match.skill.action["type"] not in CONTROL_TYPES):
            self.ai.ask(text)  # AI mode: everything but the mode/provider switches goes to the LLM
            return True
        if match is not None:
            logger.info("Skill '%s' (heard %r, matched %r, score %.2f)", match.skill.name, text, match.phrase, match.score)
            self._notify(f"✓ {match.skill.name}")
            self._run(match.skill)
            return True
        ranked = rank(text, skills)
        logger.info("No skill matched %r (closest: %s)", text, f"{ranked[0].skill.name} {ranked[0].score:.2f}" if ranked else "none")
        chosen = self.fallback.interpret(text, skills) if self.fallback else None
        if chosen is None:
            return False
        logger.info("Needle understood %r as '%s'", text, chosen.name)
        self._notify(f"✓ {chosen.name} (Needle)")
        self._run(chosen)
        return True

    def run_skill(self, skill: Skill) -> str:
        """Run a skill's action and say what happened (also used by the MCP server, which hands the text to the assistant)."""
        action = skill.action
        try:
            if action["type"] == "camera":
                if action["camera"] not in self.cameras:
                    message = f"Unknown camera '{action['camera']}' in skill '{skill.name}'"
                    self._notify(message)
                    return message
                self.bus.publish("camera", persist=False, action="show", name=action["camera"])
                return f"Camera {action['camera']} is now shown on the Pi screen"
            if action["type"] == "camera_hide":
                self.bus.publish("camera", persist=False, action="hide")
                return "Camera closed"
            if action["type"] in CONTROL_TYPES:
                return self._control(action)
            if action["type"] == "command":
                run_command(action["command"])
                return f"Started: {skill.name}"
            if action["type"] == "ha_service":
                if self.ha is None:
                    raise HomeAssistantError("Home Assistant is not configured")
                data = dict(action.get("data") or {})
                if action.get("entity_id"):
                    data["entity_id"] = action["entity_id"]
                self.ha.call_service(action["service"], data)
                return f"Done: {skill.name}"
        except HomeAssistantError as error:
            logger.error("Skill '%s' failed: %s", skill.name, error)
            self._notify(f"Skill '{skill.name}' failed: {error}")
            return f"Failed: {error}"
        return f"Nothing to do for {skill.name}"

    def _control(self, action: dict) -> str:
        if self.ai is None:
            message = "AI mode is not available"
        elif action["type"] == "ai_mode":
            state = self.ai.state
            on = (not state.mode) if action["mode"] == "toggle" else action["mode"] == "on"
            if state.set_mode(on):
                message = f"Mode IA {'activé' if on else 'désactivé'} ({state.provider})" if on else "Mode IA désactivé"
            else:
                message = "Mode IA impossible: aucune clé API (OPENAI_API_KEY ou ANTHROPIC_API_KEY dans .env)"
        else:
            message = f"IA: {action['provider']}" if self.ai.state.set_provider(action["provider"]) else f"{action['provider']} n'est pas configuré (clé API manquante)"
        self._notify(message)
        return message

    _run = run_skill  # the voice path ignores the text

    def start(self) -> None:
        if self.fallback is not None:
            threading.Thread(target=lambda: self.fallback.warm_up(self.skills()), name="needle-warmup", daemon=True).start()
        subscriber, _ = self.bus.subscribe(after_id=1 << 62)  # live events only: never replay old orders

        def loop() -> None:
            while True:
                event = subscriber.get()
                if event["type"] == "transcript":
                    try:
                        self.handle(event["text"])
                    except Exception:
                        logger.exception("Cannot handle %r", event["text"])

        threading.Thread(target=loop, name="router", daemon=True).start()
