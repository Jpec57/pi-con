from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from . import images
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
            "hide_camera", "Close the camera video on the Pi's screen. It does not remove a picture shown with show_image (use hide_image).",
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

    bus = getattr(router, "bus", None)
    if flag == "ai" and bus is not None:  # for the assistant in a conversation only: the remote MCP clients keep their narrow list
        def show_image(args: dict) -> str:
            query = str(args.get("query", "")).strip()
            if not query:
                raise ValueError("query is required")
            found = images.find_image(query)
            if found is None:
                return f"No picture found for {query!r}. Say so; do not describe one as if it was on the screen."
            bus.publish("image", persist=False, **found)
            return f"A picture of '{found['title']}' ({found['source']}) is now shown on the Pi's screen."

        tools.append(Tool(
            "show_image",
            "Search the web (Wikipedia / Wikimedia Commons) for a picture and show it on the Pi's screen. Use it when asked to show, "
            "see or display a person, animal, place, object or character. The query is a short subject, e.g. 'Pikachu', 'tour Eiffel'.",
            {"type": "object", "properties": {"query": {"type": "string", "description": "what to show, 1 to 4 words"}}, "required": ["query"], "additionalProperties": False},
            show_image, read_only=True,
        ))
        tools.append(Tool(
            "hide_image", "Remove the picture currently shown on the Pi's screen. Use it only when the user asks to remove or close the picture.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda _args: bus.publish("image_hide", persist=False) and "The picture was removed from the screen.",
        ))
        tools.append(Tool(
            "end_conversation",
            "Call this when the user says goodbye or wants to stop (au revoir, à plus tard, c'est tout, merci c'est bon): the assistant "
            "stops listening for a follow-up and waits for the wake word again. Then say a short goodbye.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda _args: bus.publish("conversation_end", persist=False) and "ok: no follow-up will be listened for",
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
