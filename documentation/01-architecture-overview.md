# 01 Architecture overview

## One process, a few threads

Pi-Con is a single Python process (`pi-con`, entry point `main.py`) running as a systemd service on the Pi. It was developed on a Mac and the same code runs on both. There is no Docker and no separate speech service: everything is in-process, which keeps deployment to "copy the files, restart the service".

```
                         ┌────────────────────────── pi-con process ───────────────────────────┐
 USB mic ──sounddevice──>│ listener thread                                                      │
 (44.1/48 kHz, resampled │   wake word (openWakeWord, ONNX)                                     │
  to 16 kHz mono)        │   └─ after "Hey Jarvis": on-device STT (sherpa-onnx), live words     │
                         │        └─ end of speech ─> [AI mode + cloud STT: upload to OpenAI]   │
                         │                         └─> "transcript" event                       │
                         │                                                                      │
                         │ router thread  <── transcript ──                                     │
                         │   skills (fuzzy) ─> actions / canned replies                         │
                         │   AI mode ───────> language model (OpenAI or Anthropic) + tools      │
                         │   Needle fallback (local function-calling model)                     │
                         │                                                                      │
                         │ speaker threads <── ai_reply / say ──  Piper TTS ──> speaker         │
                         │                                                                      │
                         │ UI server thread  (HTTP + Server-Sent Events, port 8080)             │
                         │ MCP server thread (127.0.0.1:8765, optional)                         │
                         └──────────────┬───────────────────────────────────────────────────────┘
                                        │ all of the above talk through one in-memory EventBus
              browser (Mac / phone / Pi screen) <── SSE ──┘
              Home Assistant (same Pi, port 8123): camera streams, device list, service calls
```

## The pieces

| Piece | Files | Role |
|---|---|---|
| Entry point and wiring | `main.py` | Builds everything, owns the CLI (`--list-devices`, `--download-models`, `--test-skill`, `--ask`, `--say`, `--transcribe`, `--wav`) |
| Listener | `listener.py`, `audio.py` | Owns the microphone. Runs the wake word, then the utterance, follow-up window, microphone test; watchdog restarts a stuck USB stream |
| Wake word | `models.py` | openWakeWord, English pretrained `hey_jarvis` (the only wake word available without training one) |
| Speech to text | `stt.py`, `ai/stt_cloud.py` | On-device streaming recogniser (sherpa-onnx) and the optional OpenAI transcription |
| Recordings | `recordings.py` | The last 5 utterances as WAV, for the "hear yourself back" panel |
| Router | `router.py`, `skills.py`, `fallback.py` | Decides what a sentence means (see doc 05) |
| AI mode | `ai/` (`state.py`, `providers.py`, `conversation.py`) | Language model, tools, history, usage |
| Tools | `tools.py`, `images.py` | The allowlist of what the model may do (camera, devices, pictures, end the conversation) |
| Voice output | `tts.py` | Piper text-to-speech, playback, microphone muting, follow-up hook |
| Event bus | `events.py` | Thread-safe publish/subscribe with a short history |
| Web UI | `ui.py`, `static/index.html` | Stdlib HTTP server, one page, live over Server-Sent Events |
| Home Assistant client | `ha.py` | Camera HLS streams, entity list, service calls |
| MCP server | `mcp.py` | Lets the Claude / ChatGPT apps drive the Pi through a tunnel (optional) |
| Secrets | `secrets.py` | API keys saved from the UI (`.state/secrets.json`, mode 600) over `.env` defaults |
| Config | `config.py`, `config/settings.json`, `config/skills.json` | Settings and skills; skills hot-reload |

## Why in-process and event-driven

- **One owner for the microphone.** Only one thing can read the USB mic; the listener does, and everything else reacts to events.
- **Events decouple the parts.** The listener never imports the router or the UI. It publishes `wake`, `partial`, `transcript`, `recording`; the router publishes `ai_reply`, `camera`, `image`, `say`; the speaker and the UI subscribe. Adding the speaker, the recordings panel and the picture viewer did not change the listener's core loop.
- **Transient vs persistent events.** Live meters, partial words, thinking markers and pictures are *transient* (only delivered to connected pages). Transcripts and status lines are kept in a short history so a page opened later can catch up.
- **Trade-off:** one crash takes everything down. systemd restarts the service in 3 seconds, and the listener watchdog deliberately exits the process if the USB audio stays stuck.

## Where each part runs

| Part | On the Pi | On the Mac (development) |
|---|---|---|
| Whole app | systemd service `pi-con`, venv in `~/pi-con/.venv`, Python 3.13, aarch64 | `pi-con` from `.venv` (Python 3.14) |
| Home Assistant Core | Native venv in `~/homeassistant`, systemd, port 8123 | Not run; the app talks to the Pi's |
| Microphone | USB mic (C-Media) | Built-in microphone |
| Speaker | HDMI only today, through PipeWire (`pulse` device) | Default output |
| Models | `models/` (wake word, French STT ~123 MB, Piper voice ~63 MB, Needle) | Same, downloaded once |

The hardware is documented in `PROJECT.md` as "Pi 4 or 5". The Pi that is actually deployed to is a **Pi 5 with 16 GB**.

## What is deliberately not in the design

- No authentication on the web UI (LAN only; see doc 03 and 07).
- No database. State is a few small JSON files in `.state/`.
- No separate STT or TTS service. The earlier plan for `stt` and `tts` as separate systemd units was dropped for simplicity (decision D-05); the code can still be split later.
