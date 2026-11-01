# 04 AI mode

An optional mode in which every sentence (except a few local controls) goes to a language model that can use a small set of tools, then answers aloud.

## Switching it on

- UI: the **AI** selector in the header with three positions, plus the provider selector when two keys exist. By voice: "mode IA" (on, same input as before), **"mode IA texte"**, **"mode IA vocal"**, "mode OpenAI / Claude". The state is saved in `.state/ai.json`.
- **AI text** sends the model the text the on-device model transcribed; **no audio leaves the Pi**. **AI vocal** also uploads each sentence's audio to OpenAI for a better transcription (needs an OpenAI key; doc 02) and sends the resulting text to the model. Default at first start is vocal unless `"cloud_stt": false` in `settings.json`; the last choice is remembered.
- Each answer has a round **AI icon** at its left in the chat; a canned answer from the Pi uses the same icon.
- AI mode can be switched on without a key; the chat then says that no key is set when you ask something. Keys come from the settings panel (`.state/secrets.json`) or `.env` (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`); the panel wins.

## Providers (`ai/providers.py`, `ai/conversation.py`)

| | OpenAI | Anthropic |
|---|---|---|
| API | Chat Completions, streaming | Messages API, streaming |
| Default model | `gpt-6-luna` (settings.json) | `claude-sonnet-5-5` (settings.json) |
| Speed settings | `reasoning_effort="none"` | `effort: low` |
| Token counts | `stream_options.include_usage` | `usage` of the final message |

Both are called through their official SDKs. Models can be changed in the settings panel without a restart. The OpenAI model name works against the real API (the Pi's requests return 200); the Anthropic ID is from Anthropic's reference and has not been run from this project.

## One sentence, step by step

1. The router (doc 05) sends the sentence to `AiAssistant.ask` unless a *control* skill matched (AI mode, provider, voice, canned replies).
2. `thinking` starts (the UI shows the dots).
3. The request carries: the French system prompt, the last 6 exchanges (reset after 120 s of silence), the sentence, the tool list.
4. The reply streams back; each chunk is a transient `ai_delta` event the UI types out.
5. If the model calls a tool, the tool runs on the Pi and its text result goes back in a new request. At most **4** round trips per sentence.
6. `thinking` ends; one `ai_reply` event carries the final text, the token counts and the estimated price. The speaker reads it (doc 06).

The system prompt asks for one or two short spoken sentences without markdown, tells the model to use tools to show a camera or a picture or act on the house, to say plainly when a tool fails, and to infer the intent of badly transcribed words ("rabit" means "rabbit"). It never edits what the screen shows for what you said.

## Tools: what the model may do

An allowlist (`tools.py`); nothing else is reachable.

| Tool | Does | Notes |
|---|---|---|
| `show_camera`, `hide_camera` | Shows or closes a camera on the Pi's screen | Only when cameras are configured |
| `list_devices` | Lists Home Assistant entities with their state | Read-only. The result goes to the provider |
| `show_image` | Searches Wikipedia / Commons and shows the picture full screen | Doc 07 |
| `hide_image` | Removes the picture on screen | Only when the user asks. A camera-hide never removes a picture |
| `end_conversation` | Ends the conversation: no follow-up window is opened | For goodbyes the phrase list misses |
| One tool per skill marked `"ai": true` | Runs that skill's command or Home Assistant service | Only `command` and `ha_service` skills |

The remote MCP clients get a narrower list (no pictures, no `end_conversation`).

## What stays local in AI mode

Control skills are always matched on the Pi, even in AI mode, so you can leave it by voice and so trivial things cost nothing: `ai_mode`, `ai_provider`, `tts_mode`, `hide_picture` ("enlève l'image") and `reply` (canned answers such as "comment ça va", "merci", the time, goodbye). Everything else, including "montre la caméra", goes to the model.

## Tokens and cost on screen

- The answer's line shows `N in / M out` tokens: the sum over every round trip of that sentence.
- Add `price_per_mtok` (USD per million tokens) per provider and `usd_to_eur` in the `ai` block of `settings.json` and it adds `≈ 0.00013 €`. The shipped values are **estimates**: Claude Sonnet 5.5 $2 / $10; `gpt-6-luna` $0.10 / $0.50 (found on third-party price lists, not on OpenAI's page); 0.87 €/$ (about mid-2026). Check them before relying on the figure.
- **Not counted:** OpenAI voice transcription (billed by audio length) and cached-token discounts.

## Cost model decision

A ChatGPT or Claude subscription is **not** API access, and reusing a subscription login inside an app is not allowed. So there are two separate paths (D-12): API keys for AI mode (a separate bill; set a spend limit in each provider's console), and the MCP server so the Claude / ChatGPT apps can drive the Pi under the subscription. The MCP path is built and tested but not exposed: it needs a tunnel (for example Cloudflare), which needs the owner's account.

## MCP server (`mcp.py`)

Minimal MCP over HTTP POST, stateless, on `127.0.0.1:8765`, path `/mcp/<MCP_TOKEN>`. 120 requests per minute for the whole server, 64 KB bodies, tools limited to skills marked `"mcp": true` plus camera and device tools. The token must be at least 24 characters and can be changed at runtime; with no token the endpoint answers 404 to everything.

## Not verified

The full loop with real keys has been exercised on the Pi (tool calls such as `show_image` and `hide_camera` appear in its logs). The Anthropic path has not been run against the real API from this project: no Anthropic key is configured on the Pi.
