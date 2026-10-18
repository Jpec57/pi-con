from __future__ import annotations

import difflib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

PROVIDERS = ("openai", "anthropic")
CONTROL_TYPES = ("ai_mode", "ai_provider")  # always handled locally, even in AI mode, so the owner can leave it by voice
ACTION_FIELDS = {  # action type -> required fields
    "camera": ("camera",),
    "camera_hide": (),
    "command": ("command",),
    "ha_service": ("service",),
    "ai_mode": ("mode",),
    "ai_provider": ("provider",),
}


@dataclass(frozen=True)
class Skill:
    name: str
    phrases: tuple[str, ...]  # what the speech recogniser may produce; list every spelling you see in the UI
    action: dict
    fallback: bool = False  # may the Needle model trigger it for sentences no phrase matched? Camera skills need no flag
    description: str = ""  # tells Needle what the skill does (used only when `fallback` is true)
    fallback_words: str = ""  # extra words (besides the phrases') one of which must be heard for Needle to trigger it
    mcp: bool = False  # exposed as a tool to remote assistants (Claude / ChatGPT apps) through the MCP server
    ai: bool = False  # exposed as a tool to the LLM in AI mode


@dataclass(frozen=True)
class Match:
    skill: Skill
    phrase: str
    score: float


def normalize(text: str) -> list[str]:
    """Lower-case words without accents or punctuation: 'Vidéo Rabbit !' -> ['video', 'rabbit']."""
    plain = unicodedata.normalize("NFKD", text.casefold()).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", plain)


def parse_skills(data: object) -> tuple[Skill, ...]:
    if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
        raise ValueError('skills file must look like {"skills": [...]}')
    skills = []
    for index, raw in enumerate(data["skills"], 1):
        label = f"skill #{index} ({raw.get('name', '?') if isinstance(raw, dict) else '?'})"
        if not isinstance(raw, dict) or not raw.get("name"):
            raise ValueError(f"{label}: needs a name")
        phrases = raw.get("phrases")
        if not isinstance(phrases, list) or not phrases or not all(isinstance(p, str) and normalize(p) for p in phrases):
            raise ValueError(f"{label}: 'phrases' must be a non-empty list of words to say")
        action = raw.get("action")
        if not isinstance(action, dict) or action.get("type") not in ACTION_FIELDS:
            raise ValueError(f"{label}: action.type must be one of {', '.join(ACTION_FIELDS)}")
        missing = [field for field in ACTION_FIELDS[action["type"]] if not action.get(field)]
        if missing:
            raise ValueError(f"{label}: action '{action['type']}' needs {', '.join(missing)}")
        if action["type"] == "command" and not (isinstance(action["command"], list) and all(isinstance(a, str) for a in action["command"])):
            raise ValueError(f"{label}: 'command' must be a list of strings, e.g. [\"/usr/bin/logger\", \"hello\"]")
        if action["type"] == "ai_mode" and action["mode"] not in ("on", "off", "toggle"):
            raise ValueError(f"{label}: 'mode' must be on, off or toggle")
        if action["type"] == "ai_provider" and action["provider"] not in PROVIDERS:
            raise ValueError(f"{label}: 'provider' must be one of {', '.join(PROVIDERS)}")
        if action["type"] == "ha_service" and action["service"].count(".") != 1:
            raise ValueError(f"{label}: 'service' must look like light.turn_on")
        skills.append(Skill(str(raw["name"]), tuple(phrases), action, bool(raw.get("fallback", False)), str(raw.get("description", "")), str(raw.get("fallback_words", "")), bool(raw.get("mcp", False)), bool(raw.get("ai", False))))
    return tuple(skills)


def load_skills(path: str | Path) -> tuple[Skill, ...]:
    return parse_skills(json.loads(Path(path).read_text(encoding="utf-8")))


def word_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def phrase_score(words: list[str], phrase: str) -> float:
    """Best similarity (0..1) between the phrase and any run of words in the utterance, so extra words are fine
    ('euh montre la vidéo rabbit s'il te plaît') and a misheard letter only lowers the score ('vidéo rabit').

    Every meaningful word of the phrase must also be heard: 'mets la vidéo' shares 'la vidéo' with 'ferme la vidéo'
    but not its verb, so the score is capped by the worst-matching word (words of 1-2 letters are ignored)."""
    target = normalize(phrase)
    best = 0.0
    for size in {max(1, len(target) - 1), len(target), len(target) + 1}:
        for start in range(max(1, len(words) - size + 1)):
            window = words[start:start + size]
            ratio = difflib.SequenceMatcher(None, " ".join(window), " ".join(target)).ratio()
            floor = min((max((word_similarity(w, h) for h in window), default=0.0) for w in target if len(w) > 2), default=1.0)
            best = max(best, min(ratio, floor + 0.3))
    return best


def rank(text: str, skills: tuple[Skill, ...]) -> list[Match]:
    """All skills with their best phrase score, best first."""
    words = normalize(text)
    if not words:
        return []
    matches = [max((Match(skill, phrase, phrase_score(words, phrase)) for phrase in skill.phrases), key=lambda m: m.score) for skill in skills]
    return sorted(matches, key=lambda match: -match.score)


def best_match(text: str, skills: tuple[Skill, ...], threshold: float) -> Match | None:
    ranked = rank(text, skills)
    return ranked[0] if ranked and ranked[0].score >= threshold else None
