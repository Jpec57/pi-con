# Pi-Con — project tracker

Living document. **Read this first, and update the requirements, decisions, status and log as the project moves.**

## Goal

A French-speaking voice home assistant on a Raspberry Pi. Say a wake word, speak an order, and the Pi either performs a local action or forwards the request to an LLM. Develop on a Mac, deploy to the Pi natively over SSH (no Docker), and use as much of the Pi's hardware as is practical.

## Requirements (confirmed by the owner, 2026-09-20)

| Topic | Answer |
|---|---|
| Hardware | Raspberry Pi **4 or 5**, **64-bit** OS (`linux/arm64`). Only these are supported. |
| Network | Pi is on the owner's **home LAN** (not reachable from GitHub-hosted runners). |
| Language | Speech is in **French**. |
| Microphone | None yet, to be bought (see "Microphone buying guide"). |
| Speech to text | **Free / open source**, run locally as a separate service. The owner suggested sherpa (sherpa-onnx). |
| Orders | Varied: local actions on the Pi (shell, GPIO, ...) and requests to an **LLM through an API**. Exact actions and LLM provider: decide later. |
| Voice replies | **Yes**, the assistant answers aloud (needs French TTS + a speaker). |
| Cost | **No paid services** (no paid VPN/CI/cloud STT). The LLM API is the only expected external cost. |
| Interface | A **screen/web UI showing what was detected** (wake word now, transcripts once STT exists). |
| Dev environment | macOS, native Python. |

## Architecture

### Implemented (phase 1: wake word)

```
mic ──sounddevice──> frame queue ──> openwakeword ──score>=threshold──> run_command()
    16 kHz mono int16, 1280-sample frames (80 ms)        + cooldown, model.reset()
```

| Piece | File | Notes |
|---|---|---|
| Config | `src/pi_con/config.py`, `config/settings.json` | wakeword, threshold, audio_device, command, cooldown_seconds |
| Model loading | `src/pi_con/models.py` | downloads model if missing; `tflite` on Linux, `onnx` elsewhere |
| Listener | `src/pi_con/listener.py` | mic capture + detection loop |
| Command | `src/pi_con/commands.py` | runs the configured command, no shell |
| CLI | `src/pi_con/main.py` | `--list-devices`, `--download-models`, `--verbose`, `--config`, `--wav` (replay a WAV instead of the mic) |
| Event bus | `src/pi_con/events.py` | thread-safe pub/sub + short history; the listener publishes `status`, `score` (transient), `wake`; STT will publish `transcript` |
| Web UI | `src/pi_con/ui.py`, `src/pi_con/static/index.html` | stdlib HTTP server + Server-Sent Events, no extra dependencies; port `ui_port` (8080); `/`, `/events`, `/healthz`. Shows mic level, wake score vs threshold, last heard, history |
| WAV replay | `src/pi_con/audio.py` | lets the full pipeline run without a mic with `samples/hey_jarvis.wav` from `scripts/make-sample.sh` |
| Deploy | `scripts/deploy.sh`, `.github/workflows/test.yml` | `deploy.sh`: tar code over SSH, venv + pip install on the Pi, systemd service; CI only runs tests |

| Home Assistant + cameras | `src/pi_con/ha.py`, `skills.py`, `router.py`, `scripts/install-ha.sh`, `scripts/ha_setup.py` | HA Core runs natively on the Pi (venv in `~/homeassistant`, systemd `homeassistant`, port 8123). pi-con asks HA for a camera's HLS stream, proxies it at `/ha/api/hls/...` and the UI plays it with a vendored hls.js. `Router` matches each transcript against `config/skills.json` (see Skills below) and runs the action; the camera one emits a `camera` event. `GET /devices` + header grid button list HA entities (debug view); camera tiles open the stream |

### Skills (2026-10-18)

`config/skills.json`: list of `{name, phrases[], action}`; actions `camera`, `camera_hide`, `command`, `ha_service`. `skills.py` normalises (lower-case, no accents/punctuation) and scores each phrase with a character-level `SequenceMatcher` over windows of the utterance (extra words ignored, a mistranscribed letter lowers the score); the best skill at or above `skill_threshold` (0.8) runs. The router reloads the file when its mtime changes and keeps the previous skills (with a UI notice) if an edit is broken. `pi-con --test-skill "text"` shows scores offline. This is the deterministic half of the Phase 4 router. The sentences no skill matched go to the **Needle fallback** (`fallback.py`, cactus-needle 3.x, local, ~100 ms): tools are generated from the cameras and from skills marked `"fallback": true`; its output is validated (camera name fuzzy-matched to a real camera, plus a related word must appear in the sentence). A cloud LLM stays possible for open questions later.

### AI mode and MCP (2026-10-18)

- `ai/` (`state.py`, `providers.py`, `conversation.py`, `stt_cloud.py`): optional **AI mode**, off until an API key exists. Toggle from the UI (`GET /ai`, `POST /ai/mode|provider`) or by voice (control skills `ai_mode`, `ai_provider` in `config/skills.json`, always matched locally even in AI mode). In AI mode every other sentence goes to the active provider (Anthropic Messages API or OpenAI Chat Completions, streaming, tool loop, short history), which gets the allowlisted tools from `tools.py` (camera show/hide, `list_devices`, skills with `"ai": true`). `CloudUtterance` (`stt.py`) swaps the local transcript for an OpenAI transcription of the recorded utterance while AI mode is on; local sherpa still ends the utterance and is the fallback.
- `mcp.py`: minimal MCP server (JSON-RPC over HTTP POST, stateless) on **127.0.0.1:`mcp_port`**, path `/mcp/<MCP_TOKEN>`, 120 req/min, 64 KB bodies, same allowlist with `"mcp": true`. Meant to be reached only through a tunnel so the Claude app (subscription) can drive the Pi.

### Planned (target)

```
                 ┌──────────── pi-con process (owns the mic) ────────────┐
mic ─> wake word ─> record utterance until silence (VAD) ─> HTTP/WebSocket ─┼─> stt service (sherpa-onnx, French)
                 └─────────────────────────────────────────────────────────┘            │ text
                                                                                        v
                                                  intent router ─┬─> local action registry (shell/GPIO/...)
                                                                 └─> LLM API (tool calling) ─> actions / answer
                                                                                        │
                                                            optional: TTS reply (e.g. Piper, French voice)
```

- **One process owns the mic** (the wake word listener). After a detection it records the utterance and sends the audio to the STT service. STT is then a pure network service: the same service can run on the Pi, or on the Mac for development.
- **Services:** `pi-con` + `stt` (+ later `tts`) as systemd units.
- **Intent routing:** deterministic French matching for frequent local commands (fast, offline, no cost); LLM fallback for everything else, using tool calling so the LLM can trigger the same local actions.

## Technical notes and constraints

- **Wake word is English-only.** OpenWakeWord's pretrained models (`hey_jarvis`, `alexa`, `hey_mycroft`, `hey_rhasspy`, `timer`, `weather`) are English. Start with `hey_jarvis` and test with a French accent. If unreliable, train a custom French wake word with OpenWakeWord's training pipeline (synthetic speech from TTS). Picovoice Porcupine supports French but is a proprietary SDK that needs an account key; check its current terms before relying on it.
- **STT candidates for French** (all free; *not benchmarked yet, measure on the real Pi*):

  | Engine | Strength | Caveat |
  |---|---|---|
  | sherpa-onnx | ONNX, ARM-friendly, streaming and offline, several models incl. Whisper-based; owner's preference | verify the exact French models available and their size/latency before committing |
  | faster-whisper / whisper.cpp | best accuracy for French at `small`/`base` | slower; Pi 4 likely too slow for `small` |
  | Vosk | small French model, light, ready-made server | lower accuracy |

  Decision approach: record ~20 real French orders from the chosen microphone, then compare word error rate and latency across 2–3 engines on the Pi.
- **Pi 4 is the target.** No NPU and a Cortex-A72 CPU: STT is the heavy part and this is the tightest constraint in the project. Favour small/streaming engines (sherpa-onnx small French model, Vosk small) over Whisper `small`; Whisper `tiny`/`base` may be usable but must be measured. Use a heatsink/fan (throttling hurts latency), a good SD card or USB SSD, and keep the wake word on 1 thread so STT has the other cores. Ask about RAM (2 GB is tight with STT; 4 GB+ preferred). A Pi 5 would help, but is not required.
- **Hardware use in practice:** 64-bit OS, TFLite/ONNX Runtime CPU (NEON), tune thread counts (wake word uses 1 thread on purpose so STT keeps the other cores), fast storage (good A2 SD card or NVMe on Pi 5), cooling to avoid throttling.
- **Audio format:** OpenWakeWord needs 16 kHz mono. The mic must support 16 kHz (or go through ALSA `plughw`); check with `arecord -l` and `pi-con --list-devices` on the Pi. Prefer selecting the device by name rather than index on the Pi, since indexes can change.

## Microphone buying guide

The Pi has no analog input, so use **USB**. Avoid bare I2S MEMS boards and HAT-type mic arrays for a first build: they need kernel drivers/overlays and are awkward to set up.

| Option | Why | Notes |
|---|---|---|
| **ReSpeaker XVF3800 USB 4-mic array** (Seeed) | Best pick for a far-field assistant: 4 mics, on-chip beamforming, echo cancellation, noise suppression. Plug-and-play USB, also works on the Mac so the same mic is used for dev and prod | Check current availability and price |
| **Jabra Speak 410/510 or Anker PowerConf S330** | USB speakerphone: mic **and** speaker in one, good echo cancellation, works out of the box. Good if the assistant will answer aloud | Larger, less far-field than an array |
| PlayStation Eye (second-hand) | Cheap 4-mic USB array popular in DIY assistants | Old, quality varies |
| Cheap USB conference/omni mic | Lowest cost, fine for near-field (1–2 m) | No echo cancellation |

Recommendation: **ReSpeaker XVF3800** for the assistant itself; if spoken replies are wanted without a separate speaker, a **Jabra/Anker speakerphone** is the simplest single purchase. Prices and availability were not verified.

## Decisions

- **Models are downloaded, not bundled.** The `openwakeword` 0.6 wheel ships no model files. `pi-con --download-models` fetches them; `deploy.sh` runs it on the Pi.
- **Load only the configured model** (`Model()` with no args loads all 6) to save CPU on the Pi.
- **Cooldown + reset after a detection**, otherwise one utterance fires the command many times.
- **No Docker (2026-10-04).** Dropped to keep things simple: `scripts/deploy.sh` ships the code over SSH, builds a venv on the Pi and runs a systemd service. Pi 4 and Pi 5 only (both arm64, so one code path). Future STT/TTS services will be plain systemd services too.
- **Secrets:** `.env` (git-ignored) locally; never committed.
- **STT as a separate service**, not in-process, so engines can be swapped and benchmarked.
- **Target 64-bit Pi OS (arm64) only**; Pi 4 and 5.
- **Deploy = plain SSH from the Mac (`scripts/deploy.sh`), free and works on the LAN.** GitHub-hosted CI runners are on the internet and can't reach a LAN-only Pi, so CI deploy would need a tunnel or a self-hosted runner. Not needed: CI only runs tests.
- **UI = web page over Server-Sent Events, served by the app itself.** Works from the Mac, a phone, or a browser in kiosk mode on a Pi screen, with zero extra dependencies. No authentication: LAN only, never port-forward it.
- **`--wav` replay mode** so the whole pipeline (model, events, UI) can be run without a mic; macOS `say` output triggers the model (peak score ~0.99).

- **Secrets are configured in the UI, `.env` is the default (2026-10-18).** `secrets.py` (`SecretStore`): a value saved from the settings panel (`.state/secrets.json`, mode 600, write-only: endpoints return only where each secret comes from) wins over the `.env` value; removing it falls back to `.env`. Same for the AI models / cloud-STT toggle (`.state/ai.json`, over `config/settings.json`) and the MCP token (changeable at runtime, endpoint idle when empty). POSTs from another origin get 403 so a website cannot change settings through the owner's browser; the UI is otherwise unauthenticated (LAN only), so anyone on the LAN can replace but not read keys.
- **Cost model for LLMs (2026-10-18).** A ChatGPT/Claude subscription is not API access and reusing its login in an app is not allowed, so there are two paths: MCP (the subscription apps call the Pi, no API bill) and optional API keys for AI mode (separate bill, owner sets spend limits). **The "STT stays local" decision is superseded only in AI mode** (opt-in, after the wake word, with the AI button showing it is on); with AI mode off nothing leaves the Pi. This answers the open question about off-device audio.
- **Home Assistant for cameras (2026-10-18).** HA Core native in a venv, no Docker. Camera = HA "generic" integration with the RTSP URL. HA's MJPEG/snapshot endpoints do not work for a stream-only camera (cached still), HLS does, so the browser plays HLS via hls.js (vendored in `static/`, v1.5.20, no CDN). The HA long-lived token and the RTSP credentials live only in `~/pi-con/.env` on the Pi (mode 600) and `.env` on the Mac, never in git or the browser.

## Roadmap / status

Phase 1: wake word
- [x] Fix crash `Could not open .../alexa_v0.1.tflite` (missing model files)
- [x] Load only the configured model, explicit backend selection, detection cooldown
- [x] Unit tests for config and listener
- [x] SSH deploy script (native + systemd) and a test-only GitHub Actions workflow
- [x] Web UI (events, SSE, page) verified natively with `--wav` replay and in a browser; 12 unit tests
- [ ] Verify wake word on the Mac with a real microphone
- [ ] First deploy to the Pi with `./scripts/deploy.sh`
- [ ] Buy microphone, verify capture and detection on the Pi
- [ ] Tune threshold; decide if `hey_jarvis` is acceptable or a French wake word is needed

Phase 2: capture the order — record until silence (VAD), keep audio format 16 kHz mono
Phase 3 (first version done, in-process): French STT service — benchmark candidates on the Pi, add `stt` as a service
Phase 4: intent router — local action registry + LLM fallback with tool calling
Phase 5: spoken replies (TTS confirmed as required): French voice (e.g. Piper) + speaker; must not feed back into the mic (mute/pause listening while speaking, or use a mic with echo cancellation)
Phase 6: hardening — healthchecks, structured logs, restart policy, update flow

## Open questions

- How much RAM does the Pi 4 have (2/4/8 GB)? Cooling in place?
- LLM provider and first local actions: deferred by the owner, to be decided later.
- Speaker choice for the spoken replies (see mic guide: a USB speakerphone covers mic + speaker).
- Will the UI run on a screen attached to the Pi (kiosk browser) or only be opened from other devices?
- Wake word: keep `hey_jarvis` or a custom French one (e.g. "Dis Jarvis", "OK maison")?
- Should audio or transcripts ever be stored or sent off-device? (STT stays local; the LLM API would receive the transcribed text.)

## Log

- **2026-09-20** — Container crashed at startup with `ValueError: Could not open .../alexa_v0.1.tflite`. Root cause: the `openwakeword` wheel contains no model files and `Model()` tried to load all pretrained models. Fixed with `models.py` (download-if-missing, single model, explicit tflite/onnx backend) and a build-time download in the Dockerfile. Added detection cooldown, `--verbose` score logging, deploy script and CI workflow.
- **2026-09-20** — Owner confirmed: Pi 4 or 5 (64-bit), home LAN, French, no mic yet, free local STT (sherpa suggested), orders both local and via an LLM API. Recorded requirements, target architecture, STT options and mic guide above.
- **2026-09-20** — Owner answers: CI should not need paid services, plain SSH preferred; hardware is a **Pi 4**; spoken replies wanted; actions/LLM later. Asked for Docker run steps and a screen showing detected words. Added event bus, web UI (SSE), `--wav` replay, compose override for Mac, port mapping in Docker/deploy. Found and fixed a real bug during the end-to-end run: numpy `float32` scores were not JSON-serialisable and broke the UI stream (regression test added). Deploy job in CI made manual-only.
- **2026-10-04** — Removed Docker entirely (Dockerfile, compose files, build script). Supported targets are Pi 4 and Pi 5 only. `./scripts/deploy.sh` now installs natively on the Pi (venv + systemd). CI deploy job removed; workflow renamed `test.yml`. Untested on a real Pi yet.
- **2026-10-04** — First deploy failed: the Pi runs Python 3.13 (Trixie) and `openwakeword` requires `tflite-runtime` on Linux, which has no wheel for it. `deploy.sh` now installs `requirements-pi.txt` plus `openwakeword --no-deps` and runs the ONNX backend (`models.py` already falls back to it). Also silenced tar xattr and debconf noise.
- **2026-10-04** — Mic (SunFounder/C-Media USB, 44.1/48 kHz only) crash-looped the service: now falls back to the native rate and resamples in the processing loop. Added French speech-to-text **in-process** with sherpa-onnx (streaming zipformer fr, int8, ~123 MB, downloaded by `--download-models`): after the wake word, or the UI **Speak** button (`POST /listen`), the utterance is transcribed live until ~1 s of silence (max `max_listen_seconds`). Events `capture`, `partial` (transient), `transcript`. UI shows words with a typewriter animation. Verified on the Pi: model loads, button triggers a capture. Not yet verified with real speech; the Pi logs ~30 `input overflow` warnings/min (cause unknown). This supersedes the "STT as a separate service" decision for now: in-process is simpler; split it out later only if needed.
- **2026-10-04** — Service froze with "no audio": the process was alive but the USB mic stream had stopped delivering blocks (kernel logged `retire_capture_urb` errors). Added a watchdog in `listener.py` (no block for 3 s → close and reopen the stream; after 4 failed reopens, or if closing hangs, `os._exit(3)` so systemd restarts the process), a `debug.py` state/log ring, `GET /debug`, `POST /restart-audio` and `POST /restart-app`, and a "Debug & controls" panel in the UI (stream state, last block age, overflows, stalls, temperature, recent logs, restart buttons). Verified on the Pi: both restart actions work. The stall itself has not been reproduced since, so the watchdog is untested against a real freeze. Underlying cause (USB capture glitches, ~1 overflow/s) still open: try another USB port/powered hub, or a different mic.
- **2026-10-04** — UI redesigned (chat-style, dark glass/aurora look): orb = Speak button reacting to mic level, conversation with fading older messages that expands when scrolling up and collapses 2.5 s after returning to the bottom, typewriter bubbles, meters/controls/debug moved into a dialog (gear icon). Checked in Chrome against a fake event feed; not yet on a real phone or the Pi's own screen.
- **2026-10-04** — UI v2: black/red palette, chat centred with centred bubbles, orb moved to a bottom dock (shrinks when the chat expands). The wake phrase shown in the UI comes from `wake_phrase` in `config/settings.json` (default: prettified `wakeword`) and is injected into the page as the JS global `WAKE_PHRASE`; note this is display text only, the detected model is still `wakeword` (hey_jarvis, English pre-trained).
- **2026-10-18** — Removed the unrelated `workout-vizualizer` Docker project and Docker itself from the Pi at the owner's request (it held 15 GB of 29 GB; the Postgres volume was deleted with it). Installed Home Assistant Core natively (`./scripts/install-ha.sh`, ~730 MB), created the admin and a long-lived token (`scripts/ha_setup.py`), added the Tapo camera as `camera.rabbit` (H.264 640x360 `stream2`). Voice order "Montre la caméra rabbit", UI camera card (close button / "ferme la caméra") and a Devices dialog verified in Chrome against the Pi. The spoken order itself was simulated with a transcript: not yet tried with the real mic and French STT (the STT may spell "rabbit" differently: add spellings to `aliases` in `config/settings.json`). Gotcha: HA gzips HLS playlists, the proxy must forward `Content-Encoding`. Open: Chrome on the Mac could not open `raspberrypi.local` (use the IP).
- **2026-10-18** — Owner reports the speech-to-text is poor (suspected cheap mic). Replaced the hard-coded camera matcher with configurable **skills** (`config/skills.json`, fuzzy phrase matching tolerant of mis-transcriptions, hot-reload, `--test-skill`). "vidéo rabbit" / "vidéo rabbite" open the camera. This makes poor STT workable (list the spellings you see) but does not fix it: next step is to measure the mic (levels in the details dialog, closer placement, a better USB mic from the buying guide) and compare STT models.
- **2026-10-18** — Added the **Needle fallback** (owner asked for it as a fallback only). Measured on the Mac with the real model: French paraphrases work ("peux-tu me montrer rabbit s'il te plaît"), but Needle says yes to zero-argument tools for chit-chat ("bonjour comment ça va" -> hide camera, "il fait beau" looped hide 8 times) and its confidence does not separate right from wrong (0.01-0.8), so every result is checked: the camera name must resolve to a configured camera and the sentence must contain a related word (exact match for words under 4 letters, since "ça" is 0.8 similar to "cam"). Also tightened the fuzzy skill matcher: a mismatched verb no longer gets outvoted by shared words ("mets la vidéo" no longer triggers "ferme la vidéo"). Known remaining miss: "mets la vidéo du bébé" is read by Needle as hide (harmless, wrong).
- **2026-10-18** — Owner has ChatGPT Plus + Claude subscriptions and wants no extra bill: researched that subscriptions do not include API access and that Anthropic reserves subscription logins for its own apps, so built (1) an **MCP server** (loopback, token path, narrow tools) for the Claude/ChatGPT apps, and (2) at the owner's request the **API-key AI mode** as a quick-test fallback for ChatGPT Plus (MCP support on Plus is unclear). Verified with fake SDK clients (tool loop, streaming, error mapping), routing, UI in Chrome; **not yet run against the real APIs** (no keys in the session): the OpenAI model names (`gpt-6-luna`, `gpt-4o-transcribe`) come from OpenAI's docs page and may need adjusting, and `claude-sonnet-5-5` is configurable. Not done: streaming transcription over the OpenAI Realtime API (buffered after end-of-speech instead), spoken replies (TTS), and exposing MCP over a tunnel (needs the owner's Cloudflare account). Note: the full `pytest` run prints a native `recursive_mutex lock failed` message at interpreter exit (also on the earlier baseline; all tests pass), not investigated.
- **2026-10-18** — Moved the API keys, models, cloud-STT toggle and MCP token from `.env`/`settings.json`-only to a **settings panel in the UI** (gear -> AI & integrations), keeping `.env`/`settings.json` as defaults. Checked in Chrome: saving a key flips the provider to configured and AI mode becomes usable with no restart, the key is not in any response, the file is mode 600, a cross-origin POST is refused (403), a generated MCP token is shown once and works immediately. 103 tests.
