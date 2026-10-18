from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from .ha import device_list
from .skills import Skill, normalize


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict], str]
    read_only: bool = False


def _slug(text: str) -> str:
    return "_".join(normalize(text)) or "skill"


def build_tools(router, cameras: dict[str, str], ha, skills: Callable[[], tuple[Skill, ...]], flag: str) -> list[Tool]:
    """What a remote assistant may do. An allowlist: camera show/hide, the device list, and the skills that set `flag` (\"mcp\" or \"ai\") to true."""
    tools: list[Tool] = []

    def show_camera(args: dict) -> str:
        name = args.get("camera")
        if name not in cameras:
            raise ValueError(f"unknown camera {name!r}; choose one of {', '.join(cameras)}")
        return router.run_skill(Skill(f"Camera {name}", (), {"type": "camera", "camera": name}))

    if cameras:
        tools.append(Tool(
            "show_camera", "Show a home camera's live video on the Raspberry Pi's screen.",
            {"type": "object", "properties": {"camera": {"type": "string", "enum": list(cameras)}}, "required": ["camera"], "additionalProperties": False},
            show_camera,
        ))
        tools.append(Tool(
            "hide_camera", "Close the camera video on the Pi's screen and return to the main screen.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda _args: router.run_skill(Skill("Fermer la caméra", (), {"type": "camera_hide"})),
        ))

    def list_devices(_args: dict) -> str:
        if ha is None:
            return "Home Assistant is not configured"
        devices = device_list(ha.states(), cameras)
        return json.dumps([{"name": d["name"], "type": d["domain"], "state": d["state"]} for d in devices], ensure_ascii=False)

    tools.append(Tool(
        "list_devices", "List the devices Home Assistant knows about (cameras, lights, sensors) with their state.",
        {"type": "object", "properties": {}, "additionalProperties": False}, list_devices, read_only=True,
    ))

    taken = {tool.name for tool in tools}
    for skill in skills():
        name = _slug(skill.name)
        if getattr(skill, flag) and skill.action["type"] in ("command", "ha_service") and name not in taken:
            taken.add(name)
            tools.append(Tool(
                name, skill.description or f"{skill.name}. Same as saying: {skill.phrases[0]}.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda _args, skill=skill: router.run_skill(skill),
            ))
    return tools
