# 05 Skills and routing

What happens to a sentence once it has been transcribed. Code: `router.py`, `skills.py`, `fallback.py`, `config/skills.json`.

## The order of decisions (`Router.handle`)

```
transcript ──> best skill by fuzzy match (score >= skill_threshold, default 0.8)
                │
                ├─ AI mode on, and the match is not a control skill  ──> language model (doc 04)
                │
                ├─ a skill matched                                    ──> run its action
                │
                └─ nothing matched ──> Needle fallback (local) ──> run what it chose, or ignore the sentence
```

- In AI mode **only control skills** stay local (`ai_mode`, `ai_provider`, `tts_mode`, `hide_picture`, `reply`). Everything else goes to the model, which can call the same actions as tools.
- With AI mode off, an unmatched sentence is passed to Needle; if Needle chooses nothing, the sentence is simply ignored (it still appears in the chat).

## Skills

`config/skills.json` is a list of `{name, phrases[], action, ...}`. The file is re-read when it changes; if an edit is broken the previous skills stay active and the UI shows the error.

| Action type | Does | Required fields |
|---|---|---|
| `camera` / `camera_hide` | Shows or closes a camera, or the picture on screen | `camera` |
| `command` | Runs a program on the Pi, no shell | `command` (list of strings) |
| `ha_service` | Calls a Home Assistant service | `service` (`light.turn_on`), optional `entity_id`, `data` |
| `ai_mode`, `ai_provider` | Switches AI mode (`on`, `off`, `toggle`, `text`, `vocal`) or the provider | `mode`, `provider` |
| `hide_picture` | Removes the picture on screen ("enlève l'image", "retire la photo", "ferme l'image"...). Hard-wired and local, also in AI mode; Needle can trigger it too (`"fallback": true`) | none |
| `tts_mode` | Voice on or off | `mode` |
| `reply` | A canned answer | `text` (a sentence or a list of variants), optional `end` |

Optional fields: `fallback` (Needle may trigger it), `description`, `fallback_words`, `mcp` (remote assistants may use it), `ai` (the model may use it), `say` (a sentence read aloud after the action).

### Canned replies

`reply` skills answer basic questions instantly and for free, also in AI mode: bonjour, comment ça va (and the answer "ça va bien"), merci, who are you, the time (`{heure}`), the date (`{date}`), goodbye. A list of variants gives one at random. `"end": true` (goodbye) means no follow-up window is opened (doc 06).

## How matching tolerates bad transcription

Because the speech recogniser is imperfect, matching is fuzzy on purpose (decision D-09):

1. Text is lower-cased, stripped of accents and punctuation.
2. Each phrase is compared with every window of the utterance (phrase length plus or minus one word) using a character-level similarity; extra words in the utterance are ignored.
3. Every meaningful word of the phrase (3 letters or more) must be heard too: the score is capped by the worst-matching word, so "mets la vidéo" does not trigger "ferme la vidéo".
4. The best skill at or above the threshold runs. On equal scores the **longer matched phrase wins** ("mode ia texte" beats "mode ia"); after that, the first in the file.

To tune: list every spelling you see in the chat as an extra phrase, and check offline with `pi-con --test-skill "what was heard"`, which prints the scores. Known looseness: "comment je fais" scores 0.76 against "comment ça va", close to the 0.8 threshold.

## Needle fallback

[cactus-needle](https://github.com/cactus-compute/needle) is a tiny local function-calling model (about 35 MB, about 100 ms on the Pi). It is the fallback for sentences no phrase matched, so "peux-tu me montrer rabbit s'il te plaît" opens the camera without being listed.

Its answers are never trusted blindly (D-10), because on chit-chat it tends to say yes to zero-argument tools ("il fait beau" once looped "hide camera" eight times) and its confidence does not separate right from wrong:

- a camera name must fuzzy-match a configured camera;
- the sentence must contain a word related to the tool (exact match for words under 4 letters);
- it can only use the camera tools and skills marked `"fallback": true`.

Known remaining miss: "mets la vidéo du bébé" is read as hide (harmless, wrong). Needle is a fallback only; open questions go to the language model in AI mode.
