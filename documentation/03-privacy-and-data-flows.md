# 03 Privacy and data flows

What stays on the Pi, what leaves it, and who can reach what. Written from the code, not from intentions.

## The short version

- **With AI mode off, nothing leaves the Pi** after setup. Wake word, speech to text, skills, text to speech and the camera all run locally.
- **With AI mode on**, up to three kinds of data leave: the audio of each sentence (to OpenAI, only in **AI vocal**), the text of the conversation (to OpenAI or Anthropic, whichever is selected), and a few words typed into a web search (to Wikipedia).
- **The web page has no login.** Anyone on the same network can use it.

## What leaves the device

| Data | Sent when | To | How | Notes |
|---|---|---|---|---|
| **Audio of your sentence** | **AI vocal** selected **and** an OpenAI key (AI text never sends audio) | OpenAI (`/v1/audio/transcriptions`) | **Uploaded as one WAV file** after you finish speaking (16 kHz mono, uncompressed). Not streamed. | Starts after the wake word, which is not included. In a follow-up window it includes the silence before you speak (up to 10 s). Never sent when nothing was said. |
| **Your sentence as text** | AI mode on | The selected provider: OpenAI (Chat Completions) or Anthropic (Messages API) | Streaming HTTPS request, one per sentence plus one per tool round trip (at most 4) | Sent together with the system prompt, the tool definitions and the last 6 exchanges (text only; history is dropped after 2 minutes of silence). |
| **Tool results** | The model calls a tool | The same provider, inside the next request | Text | `list_devices` returns the names, types and states of every Home Assistant entity (lights, sensors, cameras...). Camera, picture and "ok" results are short text. **No video or image is ever sent to a provider.** |
| **A search phrase** | The model calls `show_image` | Wikipedia (fr, en) and Wikimedia Commons | HTTPS GET to fixed API hosts | The phrase is chosen by the model from what you said. |
| **Your browser's request for the picture** | A picture is shown | `*.wikimedia.org` | The browser loads the image itself | Wikimedia sees the browser's address, not the Pi's. |
| Models and packages | Setup / deploy only | PyPI, GitHub releases, Hugging Face | Download | The Needle library's telemetry is switched off in code. |

What the providers do with it is governed by their own terms. The API keys used here are separate from ChatGPT / Claude subscriptions; the usage is billed to the API account (see [04](04-ai-mode.md)).

## What stays on the Pi

| Data | Where | Kept |
|---|---|---|
| Last 5 sentences as audio, with text and levels | `.state/recordings/` | Until replaced by newer ones; `"recordings_keep": 0` disables it |
| API keys saved from the UI | `.state/secrets.json`, mode 600 | Until removed; `.env` holds the defaults (mode 600 on the Pi) |
| AI mode, provider, models; spoken replies on/off | `.state/ai.json`, `.state/tts.json` | Persistent |
| Home Assistant token, RTSP camera credentials | `.env` on the Pi and the Mac, git-ignored | Persistent |
| Camera video | Never stored. HLS is relayed through the app to your browser | Live only |
| Conversation history | Memory only (6 exchanges) | Lost on restart |
| Event history for late page loads | Memory only (100 events) | Lost on restart |

## Who can reach the Pi

| Interface | Bound to | Protection | What an unauthorised person on the LAN can do |
|---|---|---|---|
| Web UI, port 8080 | All interfaces | **None.** POSTs from another website are refused (same-origin check), so a malicious page cannot drive it through your browser, but a device on your network can. | Watch the camera, play the recordings, switch AI mode, **replace** an API key (not read it), restart the app |
| MCP server, port 8765 | `127.0.0.1` only | A secret in the URL path, 120 requests per minute, 64 KB bodies, same narrow tool list | Nothing, unless you publish it through a tunnel (not set up) |
| Home Assistant, port 8123 | All interfaces (default) | HA's own login | Per HA |
| SSH | Standard | Key only (deploy uses `BatchMode`) | Per your SSH setup |

**Never port-forward 8080.** If the Pi is ever on a shared or untrusted network, the missing login on the UI is the first thing to fix.

## Settings that reduce exposure

| To avoid | Do |
|---|---|
| Any audio leaving | Choose **AI text** (header selector, or say "mode IA texte") or AI off. The text of the sentence still goes to the model in AI text |
| Any text leaving | AI mode off |
| Audio being kept on the Pi | `"recordings_keep": 0` |
| Anyone on the LAN playing recordings or using the UI | Put the Pi on a network you trust; an authentication layer is not built yet |

## Decisions behind this

- The original requirement was "speech to text stays local". It is **superseded only in AI mode**, which is opt-in and shown by the AI button (decision D-06).
- Secrets are write-only through the UI: the endpoints say where each secret comes from, never what it is (D-13).
- Pictures come from Wikimedia only and only `https` images from `wikimedia.org` are shown, so neither the model nor a spoken sentence can make the Pi fetch, or the browser load, an arbitrary address (D-19).
