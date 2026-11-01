from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

from .skills import Skill, normalize, phrase_score, word_similarity

logger = logging.getLogger(__name__)

NAME_THRESHOLD = 0.8  # how close a camera name picked by Needle must be to a configured camera
# Needle's native engine reports usage by default; this is a private home assistant.
os.environ.setdefault("NEEDLE_TELEMETRY", "0")
os.environ.setdefault("DO_NOT_TRACK", "1")


SCREEN_WORDS = {"camera", "cameras", "cam", "video", "videos", "ecran", "image", "flux", "stream", "surveillance"}
ANCHOR_SIMILARITY = 0.8


def _slug(text: str) -> str:
    return "_".join(normalize(text)) or "skill"


class NeedleFallback:
    """Understands sentences that match no skill, with the cactus-needle function-calling model (local, French works).

    Only tools the owner exposes are offered: camera show/hide, plus any skill marked `"fallback": true`.
    The model's output is untrusted: it is checked against the real cameras/skills and otherwise ignored.
    """

    def __init__(self, cameras: dict[str, str], agent_factory: Callable[[list[Callable]], object] | None = None) -> None:
        self.cameras = cameras
        self._agent_factory = agent_factory
        self._lock = threading.Lock()
        self._key: tuple[Skill, ...] | None = None
        self._agent: object | None = None
        self._calls: list[tuple[str, str]] = []  # (tool name, argument) recorded by the tools below
        self._by_tool: dict[str, Skill] = {}
        self._broken = False

    # -- tools -------------------------------------------------------------------------------------------------
    def _build_tools(self, skills: tuple[Skill, ...]) -> list[Callable]:
        record = self._calls.append
        tools: list[Callable] = []
        self._by_tool = {}
        if self.cameras:
            def show_camera(name: str):
                record(("show_camera", name))
                return name
            show_camera.__doc__ = f"Display the live video of a home camera on the screen. Cameras: {', '.join(self.cameras)}."
            tools.append(show_camera)
        if any(skill.action["type"] == "camera_hide" for skill in skills):
            def hide_camera():
                record(("hide_camera", ""))
                return True
            hide_camera.__doc__ = "Close the camera video and go back to the main screen."
            tools.append(hide_camera)
        for skill in skills:
            if skill.fallback and skill.action["type"] in ("command", "ha_service", "hide_picture"):
                tool_name = _slug(skill.name)
                tools.append(self._skill_tool(tool_name, skill.description or f"{skill.name}. For example: {skill.phrases[0]}."))
                self._by_tool[tool_name] = skill
        return tools

    def _skill_tool(self, tool_name: str, doc: str) -> Callable:
        record = self._calls.append

        def run():  # no parameters: the schema Needle sees must not contain anything but the description
            record((tool_name, ""))
            return True
        run.__name__, run.__doc__ = tool_name, doc
        return run

    def _load(self, skills: tuple[Skill, ...]):
        """(Re)build the agent when the skills changed. The first run downloads the ~15 MB model."""
        if self._agent is not None and self._key == skills:
            return self._agent
        tools = self._build_tools(skills)
        if not tools:
            return None
        if self._agent_factory is not None:
            agent = self._agent_factory(tools)
        else:
            import needle
            agent = needle.Needle(tools=[needle.tool(tool) for tool in tools], stateless=True)
        self._agent, self._key = agent, skills
        return agent

    def warm_up(self, skills: tuple[Skill, ...]) -> None:
        """Load the model in the background at start so the first unmatched sentence is not slow."""
        try:
            with self._lock:
                self._load(skills)
        except Exception:
            self._broken = True
            logger.exception("Needle fallback unavailable (is cactus-needle installed?)")

    def _anchored(self, tool: str, text: str) -> bool:
        """Needle says yes too easily to tools without arguments (it hid the camera for 'bonjour comment ça va'), so
        also require a related word in the sentence, spelled roughly right: the model proposes, the words confirm."""
        if tool in ("show_camera", "hide_camera"):
            anchors = set(SCREEN_WORDS)
            if tool == "show_camera":
                anchors |= {word for name in self.cameras for word in normalize(name)}
        else:
            skill = self._by_tool[tool]
            anchors = {word for phrase in (*skill.phrases, skill.fallback_words) for word in normalize(phrase) if len(word) > 3}
        heard = normalize(text)
        # short words must match exactly: 'ça' is 0.8 similar to 'cam'
        return any(h == a or (len(h) > 3 and word_similarity(h, a) >= ANCHOR_SIMILARITY) for h in heard for a in anchors)

    # -- interpretation ----------------------------------------------------------------------------------------
    def _resolve_camera(self, argument: str) -> str | None:
        words = normalize(argument)
        if not words:
            return next(iter(self.cameras)) if len(self.cameras) == 1 else None
        scored = [(phrase_score(words, name), name) for name in self.cameras]
        score, name = max(scored)
        return name if score >= NAME_THRESHOLD else None

    def interpret(self, text: str, skills: tuple[Skill, ...]) -> Skill | None:
        """The skill Needle picks for a sentence, or None if it picks nothing or something we do not accept."""
        if self._broken or not normalize(text):
            return None
        with self._lock:
            try:
                agent = self._load(skills)
                if agent is None:
                    return None
                self._calls.clear()
                agent.run(text)
                calls = list(self._calls)
            except Exception:
                logger.exception("Needle failed on %r", text)
                return None
        if not calls:
            return None
        tool, argument = calls[0]  # one action per sentence
        if not self._anchored(tool, text):
            logger.info("Needle chose %s for %r but the sentence has no related word: ignored", tool, text)
            return None
        if tool == "show_camera":
            camera = self._resolve_camera(argument)
            if camera is None:
                logger.info("Needle chose camera %r which is not one of %s: ignored", argument, list(self.cameras))
                return None
            return Skill(f"Caméra {camera}", (), {"type": "camera", "camera": camera})
        if tool == "hide_camera":
            return Skill("Fermer la caméra", (), {"type": "camera_hide"})
        return self._by_tool.get(tool)
