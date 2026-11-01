# 09 Decision log

Every significant decision, newest status last in each entry. **Accepted** is in force. **Superseded** was replaced (the entry says by what). **Open** is undecided. Dates are when the decision was made or recorded in `PROJECT.md`.

---

### D-01 Deploy natively over SSH, no Docker (2026-10-04)
- **Context:** the first version shipped in Docker; the owner wanted it simpler. Docker also complicates USB audio and Home Assistant on the Pi.
- **Decision:** `scripts/deploy.sh` copies the code over SSH, builds a venv on the Pi and runs a systemd service. All Docker files were removed, and Docker itself was removed from the Pi.
- **Consequences:** one code path, easy to debug, no image builds. The environment is only as reproducible as `requirements-pi.txt`. Status: **Accepted**.

### D-02 Raspberry Pi 4 and 5, 64-bit OS only (2026-09-20)
- **Decision:** one architecture (`aarch64`), no 32-bit support. In practice the target is a **Pi 5 with 16 GB** (the docs long said Pi 4).
- **Consequences:** no CPU or RAM pressure so far; the Pi 4 constraint in `PROJECT.md` is more conservative than needed. Status: **Accepted**.

### D-03 One process owns the microphone; parts talk through an event bus (2026-09-20)
- **Decision:** the listener alone reads the microphone; everything else reacts to published events (doc 07).
- **Consequences:** parts were added without touching the listener's core loop. A crash takes everything down; systemd restarts it. Status: **Accepted**.

### D-04 Load only the configured wake word model; cooldown and reset after a detection (2026-09-20)
- **Context:** `Model()` loads all six pretrained models; one utterance fired the command many times.
- **Decision:** load one model, 2 s cooldown, reset the model after each detection.
- **Consequences:** less CPU; the wake word is English (`hey_jarvis`). Status: **Accepted**.

### D-05 Speech to text in-process, not a separate service (2026-10-04)
- **Supersedes:** the plan for `stt` as its own systemd unit.
- **Decision:** the on-device recogniser runs inside the app.
- **Consequences:** simplest to run and debug; swapping the engine means editing `stt.py`. Splitting it out later is still possible. Status: **Accepted** (revisit if the engine changes, see D-22).

### D-06 On-device speech model by default; OpenAI transcription only in AI mode (2026-10-04, 2026-10-18)
- **Context:** the owner required free, local STT (sherpa-onnx suggested). The small French model proved weak with a cheap microphone.
- **Decision:** the on-device model always runs (live words, end of speech, fallback text). In **AI vocal** (see D-23) with an OpenAI key, the audio of the sentence is uploaded and OpenAI's text replaces the local one. **This supersedes "audio never leaves the Pi" only for AI mode**, which is opt-in.
- **Consequences:** better French when it works; privacy cost described in doc 03; **in practice the cloud text is often discarded by a 6 s timeout** (doc 10). Status: **Accepted, under review**.

### D-07 Deploy from the Mac; no CI deploy (2026-09-20)
- **Context:** GitHub-hosted runners cannot reach a Pi on a home LAN; a tunnel or self-hosted runner costs effort and money.
- **Decision:** plain SSH from the Mac. Status: **Accepted**. (No CI workflow exists in the repository today.)

### D-08 The UI is a web page over Server-Sent Events, served by the app, no login (2026-09-20)
- **Decision:** stdlib HTTP server, no dependencies; works from the Mac, a phone or a kiosk browser on the Pi. LAN only, never port-forwarded. POSTs from another origin are refused.
- **Consequences:** anyone on the LAN can use it (doc 03). Status: **Accepted**; authentication is **Open** if the network is ever shared.

### D-09 Skills with fuzzy matching, tolerant of mis-transcription (2026-10-18)
- **Context:** the hard-coded camera matcher broke on every misheard word.
- **Decision:** `config/skills.json` with fuzzy phrase matching (doc 05), hot-reloaded.
- **Consequences:** poor transcription becomes workable by listing the spellings seen; phrases of different skills must not overlap. Status: **Accepted**.

### D-10 Needle as a validated fallback only (2026-10-18)
- **Context:** Needle understands French paraphrases locally but says yes to zero-argument tools for chit-chat and its confidence is useless.
- **Decision:** use it only for unmatched sentences, and validate every result (real camera name, a related word in the sentence).
- **Status:** **Accepted**.

### D-11 Home Assistant Core natively for cameras, HLS in the browser (2026-10-18)
- **Decision:** HA in a venv with systemd; the camera is a generic RTSP camera; the page plays HLS through hls.js (vendored, no CDN); HA's snapshot and MJPEG endpoints do not work for a stream-only camera.
- **Status:** **Accepted**.

### D-12 AI mode with API keys, opt-in; MCP for subscription apps (2026-10-18)
- **Context:** the owner has ChatGPT Plus and Claude subscriptions and wants no extra bill. A subscription is not API access and its login may not be reused in an app.
- **Decision:** two paths: API keys for AI mode (separate bill, spend limits in each console) and an MCP server so the subscription apps can drive the Pi.
- **Consequences:** the MCP path needs a tunnel and the owner's account, so it is built but not exposed. Status: **Accepted**.

### D-13 Secrets configured in the UI, `.env` as the default, write-only (2026-10-18)
- **Decision:** a key saved from the settings panel (`.state/secrets.json`, mode 600) wins over `.env`; endpoints never return a secret, only where it comes from.
- **Consequences:** anyone on the LAN can replace but not read a key. Status: **Accepted**.

### D-14 OpenAI transcription: upload the finished sentence, not a stream (2026-10-18)
- **Context:** the on-device recogniser already finds the end of speech; the simplest cloud integration is to send the audio then.
- **Decision:** one WAV upload per sentence with a vocabulary hint, 6 s wait, fall back to the local text. OpenAI's Realtime streaming transcription was not built.
- **Consequences:** simple, but the answer arrives after the upload and processing, which exceeds 6 s on this Pi's uplink for long clips (doc 10). Status: **Accepted, under review**.

### D-15 Spoken replies with Piper, played through PipeWire (`pulse`) (2026-11-01)
- **Decision:** local Piper voice `fr_FR-siwis-medium`, on by default, loaded lazily; playback to the `pulse` device; the service unit sets `XDG_RUNTIME_DIR` for its user.
- **Why:** free and offline fits "no paid services"; the Pi has only HDMI audio, and `pulse` is the device that reaches it (the ALSA default failed).
- **Consequences:** no audible output until a speaker or screen is on HDMI (not verified). Status: **Accepted**.

### D-16 The microphone is muted while the assistant speaks (2026-11-01)
- **Decision:** the listener drops frames from the moment a sentence is queued until 0.4 s after it ends.
- **Consequences:** no self-triggering; no barge-in. A speakerphone with echo cancellation would allow it. Status: **Accepted**.

### D-17 A follow-up window instead of re-saying the wake word; goodbye ends it (2026-11-01)
- **Decision:** after an answer, listen for 10 s without the wake word; goodbye (phrase or model tool) opens none.
- **Consequences:** natural conversation; the window records leading silence that is then uploaded (doc 10). Status: **Accepted**.

### D-18 Keep the last 5 sentences as audio (2026-11-01)
- **Context:** the owner needed to tell a microphone problem from a transcription problem.
- **Decision:** `.state/recordings`, at most 5 whatever the setting, with levels and both texts.
- **Consequences:** a privacy cost (doc 03). The voice-level metric was wrong for mostly-silent clips and was fixed on 2026-11-01. Status: **Accepted**.

### D-19 Pictures only from Wikimedia, through fixed hosts (2026-11-01)
- **Decision:** `show_image` queries Wikipedia (fr, en) and Commons; the result is used only if it is `https` on `wikimedia.org`; the page enforces the same rule.
- **Consequences:** safe and free; shows the article's lead image, so it can be a photo rather than the artwork wanted (a costume for Pikachu). Status: **Accepted**.

### D-20 Prices and exchange rate are configuration, shown as estimates in euros (2026-11-01)
- **Decision:** token counts are exact; the euro figure comes from `price_per_mtok` and `usd_to_eur` in `settings.json`, shipped as labelled estimates.
- **Consequences:** wrong if the provider changes prices; transcription audio is not counted. Status: **Accepted**.

### D-21 An open page reloads itself when the app restarts (2026-11-01)
- **Context:** a tab older than a deploy silently ignored new event types.
- **Decision:** the page embeds a start id and compares it with `/boot` on each reconnect.
- **Status:** **Accepted**.

### D-22 Improving transcription (2026-11-01)
- **Context:** transcription is the bottleneck; evidence and options in doc 10.
- **Decision:** none yet.
- **Status:** **Open**.

---

### D-23 AI text and AI vocal instead of one AI button (2026-11-01)
- **Context:** the only way to stop audio leaving the Pi in AI mode was a checkbox buried in the settings panel; the owner wanted to choose per use, and the upload is also the slow part of the pipeline (doc 10).
- **Decision:** the header has an AI selector Off / Text / Vocal. **Text:** the on-device transcription is sent to the model as text, no audio is uploaded. **Vocal:** the audio is also uploaded to OpenAI for transcription. The choice is saved and settable by voice ("mode IA texte" / "mode IA vocal"). The `cloud_stt` setting is only the first-start default; the settings checkbox and the `cloud_stt` option were removed.
- **Consequences:** a clear, visible privacy choice; AI text gets the weaker on-device transcription. The first-start default stays vocal. Status: **Accepted**.

### D-24 A picture is independent of the camera and can only be removed deliberately (2026-11-01)
- **Context:** a stray word in the follow-up window made the model call `hide_camera`, which also closed the picture, so pictures seemed to vanish. The picture sat under the chat with only a small close button.
- **Decision:** the picture is a full-screen overlay above everything. Camera-hide events no longer touch it. It is removed by the X, a tap, Escape, a hard-wired local voice command ("enlève l'image", a `hide_picture` control skill that works in AI mode and that Needle can also trigger), the model's own `hide_image` tool, or by showing another picture or a camera.
- **Consequences:** "ferme la vidéo" now closes only the camera. Status: **Accepted**.
