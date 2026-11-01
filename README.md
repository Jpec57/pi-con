# Pi-Con

Pi-Con listens to a microphone, detects an [OpenWakeWord](https://github.com/dscripka/openWakeWord) wake word, and launches a configured command. Develop natively on macOS, deploy to a Raspberry Pi 4/5 over SSH. See [PROJECT.md](PROJECT.md) for goals, decisions and status.

> **Documentation:** how it is built and why, what leaves the Pi, and the open transcription problem are in [`documentation/`](documentation/README.md).

## Run on your Mac (test the wake word)



1. **Python 3.11+**: `python3 --version` (install with `brew install python@3.12` if older).
2. **Create a venv and install:**
   ```sh
   cd pi-con
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```
3. **Download the models** (once; the pip package ships none, that was the original crash):
   ```sh
   pi-con --download-models
   ```
4. **Allow microphone access.** The first run triggers a macOS prompt for your terminal app (Terminal, iTerm, VS Code…). If it doesn't, or you clicked "Don't Allow": System Settings → Privacy & Security → Microphone → enable your terminal, then restart it.
5. **Pick the input device:**
   ```sh
   pi-con --list-devices
   ```
   Note the index of your mic (the line marked `>` is the default). Optionally set `"audio_device"` in `config/settings.json` to that index. `null` uses the default.
6. **Make the trigger audible** by setting the command in `config/settings.json`:
   ```json
   "command": ["/usr/bin/say", "Yes?"]
   ```
7. **Run with live scores:**
   ```sh
   pi-con --verbose
   ```
   Say **"Hey Jarvis"** at normal volume, 1–2 m from the mic. You should see `score 0.xxx` lines rise, then `Wake word detected` and hear the reply.
   Open **http://localhost:8080** to see the live UI (mic level, wake score, detected words).
8. **Tune:** if it misses you, lower `"threshold"` (0.5 → 0.3); if it triggers on other speech, raise it (0.5 → 0.7). `"cooldown_seconds"` (default 2) prevents repeat triggers.
9. **Run the tests:** `pytest`.

Troubleshooting:

- `PortAudio`/`Input overflow` warnings: close other audio-heavy apps; usually harmless.
- No scores at all: wrong device (step 5) or mic permission (step 4). Check with `pi-con --list-devices`.
- Silence produces scores near 0.000 and nothing is printed in `--verbose` (only scores ≥ 0.1 are shown).

Config keys in `config/settings.json`: `wakeword` (`hey_jarvis`, `alexa`, `hey_mycroft`, `hey_rhasspy`, `timer`, `weather`, or a path to a custom `.onnx`/`.tflite`), `threshold`, `audio_device`, `command` (executed directly, not through a shell), `cooldown_seconds`.

## The web UI

Once running, open `http://<host>:8080` (`localhost` on the Mac, the Pi's IP on the Pi). It shows the microphone level, the wake word score against the threshold, the last thing heard, and a history. After the wake word (or when you press **Speak**), what you say is transcribed in French and typed out live in the "Last heard" box. Turn it off with `"stt_enabled": false` in `config/settings.json`; the model (~120 MB) is fetched by `pi-con --download-models`. There is no login: keep it on your LAN and never port-forward it. Change the port with `"ui_port"` in `config/settings.json`; `null` disables the UI.

## Cameras and Home Assistant

Put the camera URL in `.env` (`CAMERA_RABBIT_RTSP=rtsp://user:pass@ip:554/stream2`), the admin login (`HA_ADMIN_USER`, `HA_ADMIN_PASSWORD`), then after a deploy run `./scripts/install-ha.sh`: it installs Home Assistant Core natively on the Pi, registers each `CAMERA_<NAME>_RTSP` as `camera.<name>` and stores the access token on the Pi. List the cameras in `config/settings.json` (`cameras`: `name`, `entity`). Open the grid button in the UI to see every Home Assistant device and click a camera. Home Assistant itself is at `http://<pi>:8123`.

## Skills: what you say -> what happens

`config/skills.json` maps spoken phrases to actions. Add a skill, deploy (or edit the file on the Pi: it is reloaded when it changes, no restart), say the phrase:

```json
{ "skills": [
  { "name": "Voir la caméra rabbit",
    "phrases": ["vidéo rabbit", "vidéo rabbite", "vidéo rabit"],
    "action": { "type": "camera", "camera": "rabbit" } },
  { "name": "Salon allumé",
    "phrases": ["allume le salon"],
    "action": { "type": "ha_service", "service": "light.turn_on", "entity_id": "light.salon" } },
  { "name": "Redémarrer le micro", "phrases": ["relance le micro"],
    "action": { "type": "command", "command": ["/usr/bin/logger", "hello"] } }
] }
```

Action types: `camera` (`camera`: a name from `cameras` in settings), `camera_hide`, `command` (a list, run without a shell), `ha_service` (`service` like `light.turn_on`, optional `entity_id` and `data`; needs Home Assistant).

Matching is fuzzy: extra words are ignored and a misheard letter or two still triggers (`skill_threshold` in `config/settings.json`, default 0.8; lower it if the speech-to-text is poor, but not too far or other speech will trigger skills). **Since the speech-to-text is unreliable, add every spelling you see in the UI as another phrase.** Check a sentence without a microphone: `pi-con --test-skill "video rabit"` prints the closest skills and scores. A deploy overwrites `config/skills.json` on the Pi, so keep the master copy in the repo.

### Needle fallback (sentences no skill matched)

When no phrase matches, the sentence goes to [cactus-needle](https://github.com/cactus-compute/needle), a tiny local function-calling model (~100 ms on the Pi, French works, nothing leaves the device; telemetry is switched off in code). So "peux-tu me montrer rabbit s'il te plaît" opens the camera without being listed. It can only use what you expose: show/hide camera, and skills marked `"fallback": true` (with an optional `"description"` and `"fallback_words"`). Its answer is never trusted blindly: a camera name must fuzzy-match a real camera, and the sentence must contain a word related to the tool (the model otherwise says yes to chit-chat). Turn it off with `"needle_enabled": false` in `config/settings.json`. The model (~35 MB) is fetched by `pi-con --download-models` (run by `deploy.sh`).

## AI mode (ChatGPT / Claude) and the MCP server

**Heads-up on cost:** ChatGPT Plus and Claude Pro/Max subscriptions do **not** include API access. AI mode uses API keys, which are billed separately (a voice command costs a fraction of a cent, but set a monthly spend limit / prepaid credit in each console). Without a key nothing is sent anywhere and the AI button stays disabled.

1. Open the UI, click the gear icon, and in **AI & integrations** paste a ChatGPT and/or Claude API key and press *Save key*. It is stored on the Pi in a private file (`.state/secrets.json`, mode 600), takes effect immediately (no restart, no deploy) and is never shown again. *Remove* forgets it. **`.env` is only the default**: if you set `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` there (and deploy, which copies just those lines to the Pi), it is used until you save a different key in the UI. The model names (and the transcription model) can be changed in the same panel without editing files.
2. Try it without a microphone: `pi-con --ask "montre la caméra rabbit" --provider anthropic` prints the answer and the tool calls it *would* make, without running them.
3. In the UI use the **AI** selector (**Off / Text / Vocal**), or say "mode IA", "mode IA texte", "mode IA vocal" / "mode normal". **AI text** sends the model only the text the Pi transcribed (no audio leaves the Pi); **AI vocal** also uploads each sentence to OpenAI for a better transcription. With two keys a selector appears, or say "utilise Claude" / "utilise ChatGPT". In AI mode everything you say goes to the LLM except those switches; it answers in the chat and can use the same tools (show/hide camera, device list, and skills you mark `"ai": true`).
4. With an OpenAI key, **AI vocal** also transcribes your speech with OpenAI (much better French than the local model); the audio leaves the Pi **only in AI mode, after the wake word**. Local speech-to-text still detects when you stop talking and is the fallback if the cloud call fails. Models are in the `ai` block of `config/settings.json` (`gpt-6-luna`, `gpt-4o-transcribe` and `claude-sonnet-5-5` are defaults: change them if your account uses other names; a wrong model name shows up as a notice in the chat).

**MCP (uses the ChatGPT/Claude subscriptions you already pay for, no API bill).** Click *Generate token* in the same settings panel (shown once; `MCP_TOKEN` in `.env` works as a default too, at least 24 characters). Pi-Con serves MCP on `127.0.0.1:8765` (loopback only, a different port from the UI so the UI's restart/debug endpoints are never exposed) at `/mcp/<MCP_TOKEN>`. Tools: `show_camera`, `hide_camera`, `list_devices`, plus skills marked `"mcp": true`; none returns camera images. To reach it from the Claude app you must expose it over HTTPS, e.g. a Cloudflare Tunnel (`cloudflared tunnel --url http://127.0.0.1:8765` for a quick throw-away URL; a named tunnel with your own domain for a stable one), then in Claude: Settings → Connectors → Add custom connector → `https://<your-host>/mcp/<MCP_TOKEN>`. Anyone with that URL can show/hide your camera view: keep it private and generate a new token if it leaks. The settings panel has no login, like the rest of the UI: keep it on your LAN, and note that anyone on it can replace the keys (they can't read them back). Requests coming from another website are refused. ChatGPT Plus may not support custom MCP connectors; check OpenAI's current plan limits.

## Why the text on screen can change while you speak

Three different things can change what you see, and none of them is the language model guessing:

1. **While you speak**, the words typed live come from the on-device speech model (sherpa-onnx). It revises them as it hears more of the sentence.
2. **When you stop**, in **AI vocal** with an OpenAI key, the same audio is sent to OpenAI's transcription, and its text **replaces** the live one if it arrives in time (about 6 s). It is usually more accurate, so the bubble can change at the end. In **AI text**, without a key, or with AI off, the on-device text stays and no audio leaves the Pi.
3. **The language model only sees the final text** and answers it. Its prompt tells it to infer the intent of badly transcribed words ("rabit" means "rabbit"), but it never edits the bubble: the bubble always shows the transcription.

When the two transcriptions differ, a small italic line under your bubble shows **what the on-device model alone heard** (hover for who wrote the bubble). Choose **AI text** in the header to see only the on-device text.

## Thinking animation, pictures, and page reloads

- While the AI works (waiting for the model, running a tool such as the web search) the chat shows three bouncing dots and the orb pulses.
- A picture found on the web appears as a full-screen "hero": blurred backdrop in the picture's colours, the image scaling in with a slow drift, title and source below. It covers the whole screen and **only explicit actions remove it**: the X, a tap, Escape, or saying "enlève l'image" (hard-wired, works in AI mode and without the model); a camera-hide no longer does. `prefers-reduced-motion` turns the animations off.
- **An open page now reloads itself when the app restarts** (a deploy ships new JavaScript; before, a tab opened earlier silently ignored new event types such as `image`).

## Pictures from the web ("montre-moi un pikachu")

In AI mode the assistant has a `show_image` tool: it searches **Wikipedia** (French, then English) and **Wikimedia Commons** (free, no key), and the picture appears on the Pi's screen with its source. Close it with its X, a tap, Escape or say **"enlève l'image"** (also "retire la photo", "ferme l'image"). It is not closed by the camera commands. Only fixed hosts are contacted and only `https` images from `wikimedia.org` are ever shown. It shows the lead image of the best matching article, so it is exact for a place, an animal or a famous object but can be a photo (e.g. a costume for Pikachu) rather than the artwork you had in mind. Needs internet from the Pi and a browser that can reach Wikimedia. Not exposed to the remote MCP clients.

## Is it the microphone or the speech-to-text? (hear yourself back)

Open the UI, gear icon -> **Microphone test**.

- **Record 6 s** records raw microphone audio, keeps it and transcribes it with the local model. Press play and compare what you hear with the "Understood" line.
- Every sentence you say normally is also kept there (at most 5: `"recordings_keep"` in `config/settings.json`, `0` = nothing saved, the app never keeps more than 5), **exactly as the recogniser received it**, including sentences where nothing was understood. In AI mode the text can come from OpenAI; "Local model alone" then shows what the local model understood.
- Each recording shows peak / voice / background levels in dB (0 dB is the maximum). Roughly: voice between -30 and -10 dB is healthy; background more than 20 dB below the voice is clean; the panel warns when it is saturated, very quiet or noisy.
- **Reading the result:** if it *sounds* clear and loud but the text is wrong, the microphone is not the problem, the recogniser is. If it sounds muffled, distant, noisy or quiet, fix the microphone first (closer, other USB port, better mic, see the buying guide in `PROJECT.md`).
- Reproduce without a microphone: `pi-con --transcribe recording.wav` (right-click the player to save a recording; it is a 16 kHz mono WAV) prints what the local model understands. Try it on a file you made with `say` or Piper to see the model's limit with perfectly clean audio.
- Recordings stay in `.state/recordings` on the Pi but the UI has no login: anyone on your network can play them.

## Spoken replies (text-to-speech)

The assistant can answer aloud, in French, with [Piper](https://github.com/OHF-Voice/piper1-gpl) (local, free, no cloud). It is **on by default** (`"tts": {"enabled": true}` in `config/settings.json`); switch it with the **Voix** button in the header (the choice is saved in `.state/tts.json` and then wins over the file) or by voice ("coupe la voix" / "active la voix").

- It reads AI-mode answers and the optional `"say": "..."` sentence of a skill (e.g. `"say": "Voici la caméra rabbit."`).
- While it speaks, and for 0.4 s after, the wake word listener ignores the microphone so the assistant never wakes itself. This also means you cannot interrupt it by voice; `POST /tts/stop` cuts it.
- The voice (`fr_FR-siwis-medium`, ~60 MB) is fetched by `pi-con --download-models` (run by `deploy.sh`) into `models/tts-fr`, and loaded on first use. Other voices: set `"tts": {"voice": "fr_FR-upmc-medium"}`.
- **The Pi has no analog output and a USB mic is not a speaker**: sound goes out through HDMI, or a USB speaker / speakerphone you add. `"output_device": "pulse"` (the default in `settings.json`) plays through PipeWire to whatever the Pi's default sink is; the systemd unit written by `deploy.sh` sets `XDG_RUNTIME_DIR` so the service can reach it (without it the `pulse` device does not exist for a service, only for an SSH login). `null` is the system default.
- Speaker: Find the name or index on the Pi with `pi-con --list-devices` (or `aplay -l`) and set it, e.g. `"output_device": "USB"`.
- Test without the app: `pi-con --say "Bonjour"`.
- **Goodbye:** "au revoir Jarvis" (also "à bientôt", "bonne nuit", "c'est tout"...) is answered, then the app **stops listening for a follow-up** and waits for the wake word. In AI mode the model can do the same for any other way of saying goodbye (`end_conversation` tool). Wanted the whole app to stop listening instead? Say "coupe la voix" for the voice, or stop the service; the wake word listener itself always runs.
- **Follow-up (conversation):** after every answer (once it has been read aloud, or at once if the voice is off) the app listens for `"followup_seconds"` (default 10, `0` = off) for a next sentence **without the wake word**. Say nothing and it goes back to waiting for the wake word. Each answer opens a new window, so a conversation can go on.
- **Tokens per message:** each AI answer in the chat shows the tokens it used, e.g. `ChatGPT · 16:50 · 412 in / 38 out`. The line ends with an **estimated price in euros** (`≈ 0.00013 €`): `price_per_mtok` (USD per million tokens) per provider and `usd_to_eur` (an approximate exchange rate) are in the `ai` block of `config/settings.json`. The shipped values are estimates to check against your provider's pricing page: Claude Sonnet 5.5 $2 / $10; `gpt-6-luna` $0.10 / $0.50 (from third-party price lists, not confirmed on OpenAI's page); 0.87 €/$ (about mid-2026). Remove `price_per_mtok` to show tokens only. Tokens include every tool round trip of the sentence. Voice transcription (OpenAI cloud STT) is billed separately and not counted here.
- macOS with python.org Python: if the voice download fails with `CERTIFICATE_VERIFY_FAILED`, run "Install Certificates.command" once, or `SSL_CERT_FILE=$(python -c "import certifi;print(certifi.where())")`.

## Deploy to the Pi (Pi 4 or Pi 5, 64-bit OS)

One-time setup on the Pi: SSH enabled, your key authorised (`ssh-copy-id pi@raspberrypi.local`), a USB microphone plugged in (`arecord -l` lists it). Your user needs `sudo` (the default Pi user has it).

On your Mac:

```sh
cp .env.example .env      # fill in PI_HOST (and PI_USER, PI_SSH_KEY_PATH if needed)
./scripts/deploy.sh
```

That is all. The script copies the code over SSH, creates a venv on the Pi, installs the package and models, installs a `pi-con` systemd service (starts at boot, restarts on failure), restarts it, and fails with the logs if it isn't running after 8 s. It prints the UI address. Re-run it after every change. `config/settings.json` is overwritten on each deploy.

Logs: `ssh pi@<host> journalctl -fu pi-con`. If the default audio device isn't the mic, run `ssh pi@<host> /home/pi/pi-con/.venv/bin/pi-con --list-devices` and set `"audio_device"` in `config/settings.json` (an index, or a name substring such as `"XVF3800"`).

Testing without a microphone: `./scripts/make-sample.sh` (macOS) then `pi-con --wav samples/hey_jarvis.wav`.

### CI

`.github/workflows/test.yml` runs the tests on every push and pull request. Deploying is done from your Mac only: GitHub-hosted runners cannot reach a Pi on your home network.

## Hardware acceleration

The service uses the CPU backend (TFLite on the Pi), which is plenty for a wake word model. The Pi 4 has no NPU, so STT will run on the CPU; see PROJECT.md for the engine choices and constraints.
