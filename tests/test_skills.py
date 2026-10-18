import json
import os

import pytest

from pi_con.events import EventBus
from pi_con.router import Router
from pi_con.skills import best_match, normalize, parse_skills, rank

SKILLS = parse_skills({"skills": [
    {"name": "Rabbit", "phrases": ["vidéo rabbit", "vidéo rabbite"], "action": {"type": "camera", "camera": "rabbit"}},
    {"name": "Jardin", "phrases": ["vidéo jardin"], "action": {"type": "camera", "camera": "jardin"}},
    {"name": "Fermer", "phrases": ["ferme la vidéo"], "action": {"type": "camera_hide"}},
]})


def test_normalize_strips_accents_and_punctuation():
    assert normalize("Vidéo Rabbit !") == ["video", "rabbit"]


@pytest.mark.parametrize("heard", [
    "vidéo rabbit", "Vidéo Rabbite", "video rabit", "vidéo rabbi", "euh vidéo rabbit s'il te plaît", "la vidéo rabbit",
])
def test_misheard_or_padded_sentences_still_trigger(heard):
    match = best_match(heard, SKILLS, 0.8)
    assert match is not None and match.skill.name == "Rabbit"


def test_each_phrase_picks_its_own_skill():
    assert best_match("vidéo jardin", SKILLS, 0.8).skill.name == "Jardin"
    assert best_match("ferme la vidéo", SKILLS, 0.8).skill.name == "Fermer"


def test_a_different_verb_is_not_outvoted_by_shared_words():
    assert best_match("mets la vidéo du bébé", SKILLS, 0.8) is None  # shares 'la vidéo' with 'ferme la vidéo'
    assert best_match("ferme la vidéo", SKILLS, 0.8).skill.name == "Fermer"
    assert best_match("ferne la vidéo", SKILLS, 0.8).skill.name == "Fermer"  # a garbled verb still works


@pytest.mark.parametrize("heard", ["quelle heure est-il", "allume la lumière", "bonjour", ""])
def test_unrelated_speech_triggers_nothing(heard):
    assert best_match(heard, SKILLS, 0.8) is None


def test_rank_reports_scores_best_first():
    ranked = rank("vidéo rabit", SKILLS)
    assert ranked[0].skill.name == "Rabbit" and ranked[0].score > ranked[1].score


@pytest.mark.parametrize("bad, message", [
    ({}, "skills"),
    ({"skills": [{"phrases": ["a"], "action": {"type": "camera_hide"}}]}, "name"),
    ({"skills": [{"name": "x", "phrases": [], "action": {"type": "camera_hide"}}]}, "phrases"),
    ({"skills": [{"name": "x", "phrases": ["a"], "action": {"type": "fly"}}]}, "action.type"),
    ({"skills": [{"name": "x", "phrases": ["a"], "action": {"type": "camera"}}]}, "needs camera"),
    ({"skills": [{"name": "x", "phrases": ["a"], "action": {"type": "command", "command": "ls"}}]}, "list of strings"),
    ({"skills": [{"name": "x", "phrases": ["a"], "action": {"type": "ha_service", "service": "turn_on"}}]}, "light.turn_on"),
])
def test_invalid_skills_are_rejected_with_a_clear_message(bad, message):
    with pytest.raises(ValueError, match=message):
        parse_skills(bad)


def write(path, skills):
    path.write_text(json.dumps({"skills": skills}), encoding="utf-8")
    stamp = path.stat().st_mtime + 5  # distinct mtime so the reload is detected even within one second
    os.utime(path, (stamp, stamp))


def drain(subscriber):
    events = []
    while not subscriber.empty():
        events.append(subscriber.get_nowait())
    return events


def test_router_runs_camera_skills_and_reloads_edits(tmp_path):
    path = tmp_path / "skills.json"
    write(path, [{"name": "Rabbit", "phrases": ["vidéo rabbit"], "action": {"type": "camera", "camera": "rabbit"}}])
    bus = EventBus()
    live, _ = bus.subscribe()
    router = Router(bus, path, {"rabbit": "camera.rabbit"}, None, 0.8)
    assert router.handle("vidéo rabite") is True
    events = drain(live)
    assert [e["type"] for e in events] == ["status", "camera"] and events[1]["name"] == "rabbit"
    assert router.handle("allume la lumière") is False and drain(live) == []

    write(path, [{"name": "Fermer", "phrases": ["ferme tout"], "action": {"type": "camera_hide"}}])  # edited on the Pi
    assert router.handle("ferme tout") is True
    assert drain(live)[-1]["action"] == "hide"


def test_router_runs_commands_and_ha_services(tmp_path):
    path = tmp_path / "skills.json"
    write(path, [
        {"name": "Lumière", "phrases": ["allume le salon"], "action": {"type": "ha_service", "service": "light.turn_on", "entity_id": "light.salon", "data": {"brightness": 100}}},
        {"name": "Hello", "phrases": ["dis bonjour"], "action": {"type": "command", "command": ["true"]}},
    ])
    calls = []

    class HA:
        def call_service(self, service, data):
            calls.append((service, data))

    router = Router(EventBus(), path, {}, HA(), 0.8)
    assert router.handle("allume le salon")
    assert calls == [("light.turn_on", {"brightness": 100, "entity_id": "light.salon"})]
    assert router.handle("dis bonjour")


def test_unknown_camera_and_broken_file_are_reported_not_fatal(tmp_path):
    path = tmp_path / "skills.json"
    write(path, [{"name": "Ghost", "phrases": ["vidéo fantôme"], "action": {"type": "camera", "camera": "ghost"}}])
    bus = EventBus()
    live, _ = bus.subscribe()
    router = Router(bus, path, {"rabbit": "camera.rabbit"}, None, 0.8)
    router.handle("vidéo fantôme")
    assert any("Unknown camera" in e.get("message", "") for e in drain(live))

    path.write_text("{ not json", encoding="utf-8")
    os.utime(path, (path.stat().st_mtime + 10, path.stat().st_mtime + 10))
    assert router.handle("vidéo fantôme") is True  # the previous skills are kept
    assert any("Skills file ignored" in e.get("message", "") for e in drain(live))
