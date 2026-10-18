from __future__ import annotations

import logging
import threading
from pathlib import Path

from .commands import run_command
from .events import EventBus
from .fallback import NeedleFallback
from .ha import HomeAssistant, HomeAssistantError
from .skills import Skill, best_match, load_skills, rank

logger = logging.getLogger(__name__)


class Router:
    """Turns transcripts into actions by matching them against the skills in the skills file.

    The file is re-read when it changes, so skills can be added on the Pi without restarting. LLM fallback comes later.
    """

    def __init__(self, bus: EventBus, skills_path: str | Path, cameras: dict[str, str], ha: HomeAssistant | None, threshold: float, fallback: NeedleFallback | None = None) -> None:
        self.bus = bus
        self.skills_path = Path(skills_path)
        self.cameras = cameras
        self.ha = ha
        self.threshold = threshold
        self.fallback = fallback
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

    def _run(self, skill: Skill) -> None:
        action = skill.action
        try:
            if action["type"] == "camera":
                if action["camera"] not in self.cameras:
                    self._notify(f"Unknown camera '{action['camera']}' in skill '{skill.name}'")
                    return
                self.bus.publish("camera", persist=False, action="show", name=action["camera"])
            elif action["type"] == "camera_hide":
                self.bus.publish("camera", persist=False, action="hide")
            elif action["type"] == "command":
                run_command(action["command"])
            elif action["type"] == "ha_service":
                if self.ha is None:
                    raise HomeAssistantError("Home Assistant is not configured")
                data = dict(action.get("data") or {})
                if action.get("entity_id"):
                    data["entity_id"] = action["entity_id"]
                self.ha.call_service(action["service"], data)
        except HomeAssistantError as error:
            logger.error("Skill '%s' failed: %s", skill.name, error)
            self._notify(f"Skill '{skill.name}' failed: {error}")

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
