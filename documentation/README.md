# Pi-Con documentation

Pi-Con is a French-speaking voice assistant for a Raspberry Pi. You say a wake word ("Hey Jarvis"), speak an order, and the Pi either does something locally (show a camera, run a command) or hands the sentence to a language model that can use the same tools, then answers aloud.

These documents describe **how it is built and why**. For how to run it, see [`../start.md`](../start.md) and [`../README.md`](../README.md). For status, roadmap and the dated log, see [`../PROJECT.md`](../PROJECT.md).

| Document | Read it to know |
|---|---|
| [01 Architecture overview](01-architecture-overview.md) | The pieces, how they talk to each other, what runs where |
| [02 Audio and speech-to-text](02-audio-and-speech-to-text.md) | Wake word, the two transcription engines, **streamed or uploaded?**, why transcription is the weak point |
| [03 Privacy and data flows](03-privacy-and-data-flows.md) | Exactly what leaves the Pi, when, and where it goes |
| [04 AI mode](04-ai-mode.md) | The language model path: providers, tools, history, token and cost display |
| [05 Skills and routing](05-skills-and-routing.md) | What happens to a sentence: skills, canned replies, the Needle fallback |
| [06 Voice output](06-voice-output.md) | Text-to-speech, not hearing itself, the follow-up window, goodbye |
| [07 UI and events](07-ui-and-events.md) | The event bus, the web page, pictures, debug tools |
| [08 Deployment and operations](08-deployment-and-operations.md) | Deploy, the systemd service, state files, known operational traps |
| [09 Decision log](09-decision-log.md) | Every significant decision: context, choice, consequences, status |
| [10 Transcription: options and how to decide](10-transcription-options.md) | The current bottleneck: hypotheses, candidate fixes, how to measure |

## Conventions used in these documents

- **Verified** means checked by running it. **Not verified** means designed and unit-tested but not yet seen working on the real Pi (or not heard with real speech). The documents say which, because several important things (audio output on the Pi, real French speech) have not been verified end to end.
- File names refer to `src/pi_con/` unless a path says otherwise.
- Keep these documents and `PROJECT.md` in step: when a decision changes, add an entry to the decision log and update the document it affects.
