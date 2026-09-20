# Pi-Con — project tracker

Living document. **Read this first, and update the requirements, decisions, status and log as the project moves.**

## Goal

A French-speaking voice home assistant on a Raspberry Pi. Say a wake word, speak an order, and the Pi either performs a local action or forwards the request to an LLM. Develop on a Mac, ship to the Pi as Docker images, and use as much of the Pi's hardware as is practical.

## Requirements (confirmed by the owner, 2026-09-20)

| Topic | Answer |
|---|---|
| Hardware | Raspberry Pi **4**, **64-bit** OS (`linux/arm64`). |
| Network | Pi is on the owner's **home LAN** (not reachable from GitHub-hosted runners). |
| Language | Speech is in **French**. |
| Microphone | None yet, to be bought (see "Microphone buying guide"). |
| Speech to text | **Free / open source**, run locally as a Docker service. The owner suggested sherpa (sherpa-onnx). |
| Orders | Varied: local actions on the Pi (shell, GPIO, ...) and requests to an **LLM through an API**. Exact actions and LLM provider: decide later. |
| Voice replies | **Yes**, the assistant answers aloud (needs French TTS + a speaker). |
| Cost | **No paid services** (no paid VPN/CI/cloud STT). The LLM API is the only expected external cost. |
| Interface | A **screen/web UI showing what was detected** (wake word now, transcripts once STT exists). |
| Dev environment | macOS, native Python for mic tests (Docker Desktop can't pass the Mac mic). |

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
| WAV replay | `src/pi_con/audio.py` | lets Docker on the Mac (no mic) run the full pipeline with `samples/hey_jarvis.wav` from `scripts/make-sample.sh` |
| Deploy | `scripts/deploy.sh`, `.github/workflows/deploy.yml` | build arm64 image, ship over SSH |

### Planned (target)

```
                 ┌──────────── pi-con container (owns the mic) ────────────┐
mic ─> wake word ─> record utterance until silence (VAD) ─> HTTP/WebSocket ─┼─> stt container (sherpa-onnx, French)
                 └─────────────────────────────────────────────────────────┘            │ text
                                                                                        v
                                                  intent router ─┬─> local action registry (shell/GPIO/...)
                                                                 └─> LLM API (tool calling) ─> actions / answer
                                                                                        │
                                                            optional: TTS reply (e.g. Piper, French voice)
```

- **One process owns the mic** (the wake word listener). After a detection it records the utterance and sends the audio to the STT service. STT is then a pure network service: the same container can run on the Pi, or on the Mac for development.
- **Compose stack:** `pi-con` + `stt` (+ later `tts`). The Pi only needs `/dev/snd` for `pi-con`.
- **Intent routing:** deterministic French matching for frequent local commands (fast, offline, no cost); LLM fallback for everything else, using tool calling so the LLM can trigger the same local actions.

## Technical notes and constraints

- **Wake word is English-only.** OpenWakeWord's pretrained models (`hey_jarvis`, `alexa`, `hey_mycroft`, `hey_rhasspy`, `timer`, `weather`) are English. Start with `hey_jarvis` and test with a French accent. If unreliable, train a custom French wake word with OpenWakeWord's training pipeline (synthetic speech from TTS). Picovoice Porcupine supports French but is a proprietary SDK that needs an account key; check its current terms before relying on it.
- **STT candidates for French** (all free; *not benchmarked yet, measure on the real Pi*):

  | Engine | Strength | Caveat |
  |---|---|---|
  | sherpa-onnx | ONNX, ARM-friendly, streaming and offline, several models incl. Whisper-based; owner's preference | verify the exact French models available and their size/latency before committing |
  | faster-whisper / whisper.cpp | best accuracy for French at `small`/`base` | slower; Pi 4 likely too slow for `small` |
  | Vosk | small French model, light, ready-made Docker server | lower accuracy |

  Decision approach: record ~20 real French orders from the chosen microphone, then compare word error rate and latency across 2–3 engines on the Pi.
- **Pi 4 is the target.** No NPU and a Cortex-A72 CPU: STT is the heavy part and this is the tightest constraint in the project. Favour small/streaming engines (sherpa-onnx small French model, Vosk small) over Whisper `small`; Whisper `tiny`/`base` may be usable but must be measured. Use a heatsink/fan (throttling hurts latency), a good SD card or USB SSD, and keep the wake word on 1 thread so STT has the other cores. Ask about RAM (2 GB is tight with Docker + STT; 4 GB+ preferred). A Pi 5 would help, but is not required.
- **Hardware use in practice:** 64-bit OS, TFLite/ONNX Runtime CPU (NEON), tune thread counts (wake word uses 1 thread on purpose so STT keeps the other cores), fast storage (good A2 SD card or NVMe on Pi 5), cooling to avoid throttling.
- **Audio format:** OpenWakeWord needs 16 kHz mono. The mic must support 16 kHz (or go through ALSA `plughw`); check with `arecord -l` and `pi-con --list-devices` on the Pi. Prefer selecting the device by name rather than index in the container, since indexes can change.

## Microphone buying guide

The Pi has no analog input, so use **USB**. Avoid bare I2S MEMS boards and HAT-type mic arrays for a first build: they need kernel drivers/overlays and are awkward with Docker.

| Option | Why | Notes |
|---|---|---|
| **ReSpeaker XVF3800 USB 4-mic array** (Seeed) | Best pick for a far-field assistant: 4 mics, on-chip beamforming, echo cancellation, noise suppression. Plug-and-play USB, also works on the Mac so the same mic is used for dev and prod | Check current availability and price |
| **Jabra Speak 410/510 or Anker PowerConf S330** | USB speakerphone: mic **and** speaker in one, good echo cancellation, works out of the box. Good if the assistant will answer aloud | Larger, less far-field than an array |
| PlayStation Eye (second-hand) | Cheap 4-mic USB array popular in DIY assistants | Old, quality varies |
| Cheap USB conference/omni mic | Lowest cost, fine for near-field (1–2 m) | No echo cancellation |

Recommendation: **ReSpeaker XVF3800** for the assistant itself; if spoken replies are wanted without a separate speaker, a **Jabra/Anker speakerphone** is the simplest single purchase. Prices and availability were not verified.

## Decisions

- **Models are downloaded, not bundled.** The `openwakeword` 0.6 wheel ships no model files. `pi-con --download-models` fetches them; the Dockerfile runs it at build time so the Pi works offline.
- **Load only the configured model** (`Model()` with no args loads all 6) to save CPU on the Pi.
- **Cooldown + reset after a detection**, otherwise one utterance fires the command many times.
- **Deploy without a registry:** `docker save | ssh docker load`. Revisit if images get large or deploys slow.
- **Secrets:** `.env` (git-ignored) locally, GitHub Actions secrets in CI, same variable names.
- **STT as a separate container/service**, not in-process, so engines can be swapped and benchmarked.
- **Target `linux/arm64` only** (64-bit confirmed); the 32-bit option is no longer needed.
- **Deploy = plain SSH from the Mac (`scripts/deploy.sh`), free and works on the LAN.** GitHub-hosted CI runners are on the internet and can't reach a LAN-only Pi, so CI *deploy* would need a tunnel (e.g. Tailscale free personal plan) or a self-hosted runner. Not needed now: CI runs tests on push; deploy is manual-only until that's wanted.
- **UI = web page over Server-Sent Events, served by the app itself.** Works from the Mac, a phone, or a browser in kiosk mode on a Pi screen, with zero extra dependencies. No authentication: LAN only, never port-forward it.
- **`--wav` replay mode** so the whole pipeline (model, events, UI) can be run in Docker on a Mac without a mic; macOS `say` output triggers the model (peak score ~0.99).

## Roadmap / status

Phase 1: wake word
- [x] Fix crash `Could not open .../alexa_v0.1.tflite` (missing model files)
- [x] Load only the configured model, explicit backend selection, detection cooldown
- [x] Unit tests for config and listener
- [x] SSH deploy script + GitHub Actions workflow (untested end-to-end; deploy job is manual-only)
- [x] Web UI (events, SSE, page) verified natively with `--wav` replay and in a browser; 12 unit tests
- [ ] Verify the Docker image on the Mac (`compose.mac.yaml`) and on the Pi
- [ ] Verify wake word on the Mac with a real microphone
- [ ] Verify the arm64 image builds and runs (`docker buildx build --platform linux/arm64`)
- [ ] First deploy to the Pi with `./scripts/deploy.sh`
- [ ] Buy microphone, verify capture and detection on the Pi
- [ ] Tune threshold; decide if `hey_jarvis` is acceptable or a French wake word is needed

Phase 2: capture the order — record until silence (VAD), keep audio format 16 kHz mono
Phase 3: French STT service — benchmark candidates on the Pi, add `stt` to compose
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
