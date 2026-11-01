# 07 UI and events

The web page is the only screen. It shows what was heard, what the assistant answered, the camera, pictures, and the debug tools. Code: `events.py`, `ui.py`, `static/index.html`.

## The event bus

Everything the app does is announced as an event: a dict `{id, type, time, ...}` published on one in-memory `EventBus`. The UI, the speaker and the router subscribe. Each subscriber has a bounded queue, so a stalled browser can never block the audio loop.

| Event | Published by | Persistent? | Meaning |
|---|---|---|---|
| `status` | many | yes | A notice or the listening state |
| `score` | listener | no | Live wake word score and microphone level (4 per second) |
| `wake` | listener | yes | Wake word heard |
| `capture` | listener | no | Listening for a sentence on / off |
| `partial` | listener | no | Live words from the on-device recogniser |
| `transcript` | listener | yes | The final text, plus `local` (what the on-device model heard) and `engine` (who wrote `text`) |
| `recording` | listener | no | A saved recording, with text and levels |
| `ai` | AI state | yes | AI mode, provider, which keys exist |
| `thinking` | AI assistant | no | The model is working (`active` true / false) |
| `ai_delta` | AI assistant | no | A streamed chunk of the answer |
| `ai_reply` | AI assistant, router | yes | The final answer (with `usage`, or `end` for a goodbye) |
| `tts`, `speaking` | speaker | yes / no | Voice on or off; currently speaking |
| `say` | router | no | A skill's sentence to read aloud |
| `camera` | router | no | Show or hide a camera (hide also closes a picture) |
| `image` | the `show_image` tool | no | A picture to show |
| `image_hide` | the "enlève l'image" skill, the `hide_image` tool | no | Remove the picture. A `camera` hide does **not** |
| `conversation_end` | the `end_conversation` tool | no | The next answer opens no follow-up window |

Persistent events are replayed to a page opened later (last 100). Transient ones only reach pages connected at that moment. The key `id` belongs to the bus; payloads must not use it (a recording's id is `audio` for that reason).

## Transport

`GET /events` is a Server-Sent Events stream; the browser reconnects by itself and sends `Last-Event-ID`. A keep-alive comment goes out every 15 s.

## Endpoints

| Route | Purpose |
|---|---|
| `GET /`, `/healthz`, `/boot`, `/debug`, `/devices` | Page, health, start id, debug snapshot, Home Assistant entities |
| `GET /events` | The event stream |
| `GET /ai`, `/tts`, `/settings`, `/recordings`, `/recordings/<id>.wav` | State and settings views (never a key or token), the audio recordings |
| `GET /camera/<name>/stream`, `/ha/api/hls/...` | Camera HLS, relayed from Home Assistant (it gzips playlists; the relay forwards `Content-Encoding`) |
| `POST /listen`, `/ai/mode`, `/ai/provider`, `/tts/mode`, `/tts/test`, `/tts/stop`, `/settings/*`, `/recordings/test`, `/restart-audio`, `/restart-app` | Actions |

Every POST from a browser page on another origin is refused with 403.

## What the page does

- **Orb** (bottom): press to listen without the wake word; it swells with the microphone level and pulses while the AI is thinking.
- **Chat:** your sentences, the answers (with time and token / euro figures), system notices. Older messages fade and the thread expands when scrolled.
- **Typewriter text:** your live words type in as they arrive. In AI mode the final text can differ (OpenAI's transcription); a small italic line then shows what the on-device model heard.
- **Thinking:** three bouncing dots in the chat while the model works or a tool runs, until the first words or an error.
- **AI selector** (header): Off / Text / Vocal, replacing the old single AI button. Each AI answer has a round **AI icon** at its left.
- **Picture hero:** a picture found on the web covers the **whole screen** (above everything) over a solid base and a blurred backdrop in its own colours, scales in with a slow drift and shows its title, a source link, the latest answer as text, and a "Listening…" or "Thinking…" pill (the chat is hidden behind it). **Only explicit actions remove it:** the X in the corner, a tap, Escape, the voice command "enlève l'image" (hard-wired, works in AI mode and through Needle), the model's `hide_image` tool, or showing another picture or a camera. A camera-hide, which the model can call on a stray word, no longer closes it. `prefers-reduced-motion` turns the animation off. Only `https` images from `wikimedia.org` are accepted by the page; the page preloads the image before revealing it and reports "Cannot load the picture" in the chat if it fails.
- **Camera:** HLS played with a vendored hls.js (Safari plays HLS natively). Home Assistant's snapshot and MJPEG endpoints do not work for a stream-only camera.
- **Devices dialog:** everything Home Assistant knows; camera tiles open the stream.
- **Details dialog (gear):** microphone level, wake score, restart buttons, **microphone test** (record 6 s, hear the last 5 sentences with what was understood and the levels), API keys and models (the cloud-transcription checkbox is gone: use the AI selector), recent logs.

## Staying current after a deploy

The page embeds the app's start id (`BOOT_ID`) and, on each (re)connection of the event stream, compares it with `GET /boot`. If the app restarted, the page reloads itself (D-21). Before this existed, a tab opened before a deploy kept running old JavaScript that silently ignored new event types, which looked exactly like "the AI understood but nothing was shown".

## Testing without a microphone

- `pi-con --wav samples/hey_jarvis.wav` replays a recording instead of the microphone (`scripts/make-sample.sh` makes one on macOS).
- UI work can be checked against a small harness that runs the real UI server and publishes events; the picture, thinking and debug-line behaviour were verified that way in Chrome.
