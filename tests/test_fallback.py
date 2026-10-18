import json
import os

from pi_con.events import EventBus
from pi_con.fallback import NeedleFallback
from pi_con.router import Router
from pi_con.skills import parse_skills

SKILLS = parse_skills({"skills": [
    {"name": "Voir rabbit", "phrases": ["vidéo rabbit"], "action": {"type": "camera", "camera": "rabbit"}},
    {"name": "Fermer", "phrases": ["ferme la caméra"], "action": {"type": "camera_hide"}},
    {"name": "Salon allumé", "phrases": ["allume le salon"], "fallback": True, "description": "Turn on the living room light.",
     "action": {"type": "ha_service", "service": "light.turn_on", "entity_id": "light.salon"}},
    {"name": "Redémarrer", "phrases": ["relance"], "action": {"type": "command", "command": ["true"]}},  # not opted in
]})


class FakeNeedle:
    """Stands in for the model: picks a tool from the sentence, like the real one would."""

    def __init__(self, tools):
        self.tools = {tool.__name__: tool for tool in tools}
        self.seen = []

    def run(self, text):
        self.seen.append(text)
        if "bonjour" in text:
            self.tools["hide_camera"]()  # the real model does this: Needle says yes to zero-argument tools
        elif "bébé" in text or "rabbit" in text:
            self.tools["show_camera"](name="rabbite" if "rabbite" in text else "rabbit")
        elif "lumière" in text:
            self.tools["salon_allume"]()
        elif "salon" in text:
            self.tools["show_camera"](name="salon")
        elif "coupe" in text:
            self.tools["hide_camera"]()


def make():
    holder = {}

    def factory(tools):
        holder["agent"] = FakeNeedle(tools)
        return holder["agent"]
    return NeedleFallback({"rabbit": "camera.rabbit"}, agent_factory=factory), holder


def test_only_camera_tools_and_opted_in_skills_are_exposed():
    fallback, holder = make()
    fallback.interpret("coupe", SKILLS)
    assert sorted(holder["agent"].tools) == ["hide_camera", "salon_allume", "show_camera"]  # 'Redémarrer' is not offered


def test_camera_name_is_checked_against_real_cameras():
    fallback, _ = make()
    assert fallback.interpret("montre le bébé rabbit", SKILLS).action == {"type": "camera", "camera": "rabbit"}
    assert fallback.interpret("montre le bébé rabbite", SKILLS).action["camera"] == "rabbit"  # garbled name repaired
    assert fallback.interpret("montre le salon", SKILLS) is None  # invented camera: ignored


def test_hide_and_opted_in_skill():
    fallback, _ = make()
    assert fallback.interpret("coupe la vidéo", SKILLS).action == {"type": "camera_hide"}
    assert fallback.interpret("de la lumière du salon", SKILLS) is SKILLS[2]
    assert fallback.interpret("rien", SKILLS) is None


def test_chit_chat_does_not_trigger_tools_even_when_the_model_says_yes():
    fallback, _ = make()
    assert fallback.interpret("bonjour comment ça va", SKILLS) is None  # model hid the camera; no related word heard
    assert fallback.interpret("coupe la vidéo", SKILLS) is not None  # 'video' is there
    assert fallback.interpret("coupe la vidéo", SKILLS).action == {"type": "camera_hide"}


def test_opted_in_skill_needs_one_of_its_words():
    fallback, _ = make()
    assert fallback.interpret("de la lumière", SKILLS) is None  # no 'salon'/'allume'
    assert fallback.interpret("de la lumière du salon", SKILLS) is SKILLS[2]
    assert fallback.interpret("de la lumière du salllon", SKILLS) is SKILLS[2]  # misspelt


def test_broken_model_never_raises():
    def explode(tools):
        raise RuntimeError("no model")
    fallback = NeedleFallback({"rabbit": "camera.rabbit"}, agent_factory=explode)
    fallback.warm_up(SKILLS)
    assert fallback.interpret("montre rabbit", SKILLS) is None


def write(path, skills):
    path.write_text(json.dumps({"skills": [
        {"name": "Voir rabbit", "phrases": ["vidéo rabbit"], "action": {"type": "camera", "camera": "rabbit"}},
        *skills]}), encoding="utf-8")


def test_router_uses_needle_only_when_no_skill_matches(tmp_path):
    path = tmp_path / "skills.json"
    write(path, [])
    fallback, holder = make()
    bus = EventBus()
    live, _ = bus.subscribe()
    router = Router(bus, path, {"rabbit": "camera.rabbit"}, None, 0.8, fallback)

    assert router.handle("vidéo rabbit")  # a skill matches: the model is not consulted
    assert "agent" not in holder or holder["agent"].seen == []
    while not live.empty():
        live.get_nowait()

    assert router.handle("montre le bébé rabbit")  # no skill: Needle
    events = []
    while not live.empty():
        events.append(live.get_nowait())
    assert [e["type"] for e in events] == ["status", "camera"]
    assert "Needle" in events[0]["message"] and events[1]["name"] == "rabbit"
    assert router.handle("quelle heure est-il") is False
