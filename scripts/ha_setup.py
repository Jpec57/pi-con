"""Set up a fresh Home Assistant for pi-con: admin user, long-lived token, RTSP camera(s).

Run with Home Assistant's own Python (it ships aiohttp):
    <ha-venv>/bin/python scripts/ha_setup.py --env .env

Reads HA_ADMIN_USER, HA_ADMIN_PASSWORD and CAMERA_<NAME>_RTSP (one per camera) from the env file,
and writes HA_TOKEN back to it. Safe to re-run: existing users, tokens and cameras are kept.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

import aiohttp

CLIENT_ID = "http://pi-con.local/"


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if (match := re.match(r"\s*([A-Z0-9_]+)=(.*)", line)):
            values[match[1]] = match[2].strip()
    return values


def write_env_value(path: Path, key: str, value: str) -> None:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith(f"{key}=")]
    path.write_text("\n".join([*lines, f"{key}={value}"]) + "\n", encoding="utf-8")


async def exchange(http: aiohttp.ClientSession, base: str, code: str) -> str:
    async with http.post(f"{base}/auth/token", data={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID}) as reply:
        reply.raise_for_status()
        return (await reply.json())["access_token"]


async def onboard_or_login(http: aiohttp.ClientSession, base: str, user: str, password: str) -> str:
    """Return a short-lived access token for the admin, creating the admin on a brand new install."""
    async with http.get(f"{base}/api/onboarding") as reply:
        steps = {step["step"]: step["done"] for step in await reply.json()} if reply.status == 200 else {}
    if steps.get("user") is False:
        body = {"client_id": CLIENT_ID, "name": "Jarvis", "username": user, "password": password, "language": "fr"}
        async with http.post(f"{base}/api/onboarding/users", json=body) as reply:
            reply.raise_for_status()
            token = await exchange(http, base, (await reply.json())["auth_code"])
        headers = {"Authorization": f"Bearer {token}"}
        for step, extra in (("core_config", {}), ("analytics", {}), ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID + "?auth_callback=1"})):
            async with http.post(f"{base}/api/onboarding/{step}", json=extra, headers=headers) as reply:
                if reply.status not in (200, 403):  # 403: step already done
                    reply.raise_for_status()
        return token
    flow = {"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID}
    async with http.post(f"{base}/auth/login_flow", json=flow) as reply:
        flow_id = (await reply.json())["flow_id"]
    async with http.post(f"{base}/auth/login_flow/{flow_id}", json={"client_id": CLIENT_ID, "username": user, "password": password}) as reply:
        result = await reply.json()
    if result.get("type") != "create_entry":
        sys.exit("Login to Home Assistant failed: wrong HA_ADMIN_USER / HA_ADMIN_PASSWORD?")
    return await exchange(http, base, result["result"])


async def long_lived_token(http: aiohttp.ClientSession, base: str, access_token: str) -> str:
    async with http.ws_connect(f"{base}/api/websocket") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": access_token})
        if (await ws.receive_json())["type"] != "auth_ok":
            sys.exit("Home Assistant rejected the access token")
        await ws.send_json({"id": 1, "type": "auth/long_lived_access_token", "client_name": "pi-con", "lifespan": 3650})
        reply = await ws.receive_json()
        if not reply.get("success"):
            sys.exit(f"Cannot create the long-lived token: {reply}")
        return reply["result"]


async def ws_call(http: aiohttp.ClientSession, base: str, headers: dict, command: dict):
    async with http.ws_connect(f"{base}/api/websocket") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": headers["Authorization"].split()[1]})
        await ws.receive_json()
        await ws.send_json({"id": 1, **command})
        reply = await ws.receive_json()
        if not reply.get("success"):
            sys.exit(f"Home Assistant command {command['type']} failed: {reply}")
        return reply["result"]


async def add_camera(http: aiohttp.ClientSession, base: str, headers: dict, name: str, rtsp_url: str) -> None:
    entity_id = "camera." + name.replace(" ", "_")
    async with http.get(f"{base}/api/states/{entity_id}", headers=headers) as reply:
        if reply.status == 200:
            print(f"Camera '{name}' already configured ({entity_id})")
            return
    async with http.post(f"{base}/api/config/config_entries/flow", json={"handler": "generic", "show_advanced_options": False}, headers=headers) as reply:
        step = await reply.json()
    form = {
        "stream_source": rtsp_url,
        "advanced": {"framerate": 5, "verify_ssl": False, "rtsp_transport": "tcp", "authentication": "basic"},
    }
    while step.get("type") == "form":
        print(f"  step {step['step_id']}")
        if step["step_id"] == "user":
            payload = form
        elif step["step_id"] == "user_confirm":
            payload = {"confirmed_ok": True}
        elif step["step_id"] == "confirm_still":
            payload = {"confirmed_ok": True}
        else:
            sys.exit(f"Unexpected config step: {step}")
        async with http.post(f"{base}/api/config/config_entries/flow/{step['flow_id']}", json=payload, headers=headers) as reply:
            step = await reply.json()
        if step.get("errors"):
            sys.exit(f"Home Assistant refused the camera: {step['errors']}")
    if step.get("type") != "create_entry":
        sys.exit(f"Camera setup did not finish: {step}")
    # The flow titles the entry after the host; give the entity the camera's name so it is camera.<name>.
    entries = await ws_call(http, base, headers, {"type": "config/entity_registry/list"})
    created = next(e for e in entries if e["config_entry_id"] == step["result"]["entry_id"] and e["entity_id"].startswith("camera."))
    await ws_call(http, base, headers, {"type": "config/entity_registry/update", "entity_id": created["entity_id"], "new_entity_id": entity_id, "name": name.title()})
    print(f"Camera '{name}' added as {entity_id}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env")
    parser.add_argument("--url", default="http://127.0.0.1:8123")
    args = parser.parse_args()
    env_path = Path(args.env)
    env = read_env(env_path)
    user, password = env.get("HA_ADMIN_USER", ""), env.get("HA_ADMIN_PASSWORD", "")
    if not user or not password:
        sys.exit("Set HA_ADMIN_USER and HA_ADMIN_PASSWORD in the env file")
    async with aiohttp.ClientSession() as http:
        access = await onboard_or_login(http, args.url, user, password)
        if not env.get("HA_TOKEN"):
            write_env_value(env_path, "HA_TOKEN", await long_lived_token(http, args.url, access))
            print("Long-lived token written to", env_path)
        headers = {"Authorization": f"Bearer {access}"}
        for key, value in env.items():
            if (match := re.fullmatch(r"CAMERA_([A-Z0-9_]+)_RTSP", key)):
                await add_camera(http, args.url, headers, match[1].lower().replace("_", " "), value)


if __name__ == "__main__":
    asyncio.run(main())
