# 02 Audio and speech to text

How the microphone becomes text. This is the weakest part of the system today (see [10](10-transcription-options.md)).

## The path of one spoken sentence

```
USB mic (44.1 or 48 kHz) ──resample──> 16 kHz mono int16, 80 ms frames (1280 samples)
        │
        ├─ always: wake word model (openWakeWord "hey_jarvis", English) ── score >= 0.5 ──> wake
        │
        └─ after a wake (or the UI orb, or a follow-up window): an *utterance*
              each frame ──> on-device recogniser (sherpa-onnx, French) ──> live words ("partial" events)
              the same frames are also kept in memory
              the recogniser decides when you stopped (about 1 s of silence) ──> end of speech
                    │
                    ├─ AI off, or AI text, or no OpenAI key:
                    │       final text = the on-device text (no audio leaves the Pi)
                    └─ AI vocal + OpenAI key:
                            the kept audio is UPLOADED as one WAV file to OpenAI;
                            final text = OpenAI's text if it arrives within 6 s, else the on-device text
```

## Is the voice streamed to OpenAI, or uploaded?

**Uploaded as one file, after you finish speaking. Nothing is streamed.**

- **Only in AI vocal.** In **AI text** the audio never leaves the Pi: the text the on-device model wrote is what the language model receives. The choice is the selector in the header, by voice "mode IA texte" / "mode IA vocal", or `"cloud_stt": false` in `settings.json` for the first-start default (D-23).
- When the on-device recogniser decides the sentence is over, the app packs *all* the frames of that utterance into a WAV file (16 kHz, mono, 16-bit, uncompressed, about 32 KB per second of audio) and sends it in a single HTTP request to OpenAI's transcription endpoint (`POST /v1/audio/transcriptions`, model `gpt-4o-transcribe` by default, `language=fr`, plus a short vocabulary hint, see below). The code is `ai/stt_cloud.py` (the request) and `CloudUtterance` in `stt.py` (the wait).
- The on-device recogniser keeps running during the upload and keeps showing its own words. The final text swaps in when OpenAI answers.
- **The wait is capped at 6 seconds** (`CloudUtterance`, and the HTTP client itself times out at 8 s with no retry). If OpenAI has not answered by then, the on-device text is used and the late answer is thrown away. Measured from the Pi, a 13 s clip took 5 to 12 s, so this timeout is often hit. This is the single most important fact about the current accuracy; see [10](10-transcription-options.md).
- What is in the file: from the first frame of the utterance (after the wake word, which itself is not included) to the end of speech. In a **follow-up window** that includes up to 10 s of leading silence, because the recording starts when the window opens.
- Silence is not uploaded, but the test used is *the on-device model heard something*, not *there was speech*: if the on-device text is empty the utterance ends without a cloud call, even when you did speak. The better engine therefore cannot rescue a sentence the on-device model misses completely (finding F3 in [10](10-transcription-options.md)).
- OpenAI's streaming "Realtime" transcription was considered and **not built** (decision D-14). It would remove the upload wait, at the cost of a WebSocket client and different billing.

What is sent to the *language model* (as opposed to the transcription) is text only; see [03](03-privacy-and-data-flows.md).

## The two engines

| | On-device | OpenAI |
|---|---|---|
| Engine | sherpa-onnx, streaming zipformer, French (Common Voice, 2023-04-14), int8, about 123 MB | `gpt-4o-transcribe` (configurable in the settings panel) |
| When used | Always, for the live words and to detect the end of speech; final text when the cloud one is not used | **AI vocal** selected (header: AI Off / Text / Vocal) and an OpenAI key present |
| Latency | Streaming: words appear as you talk | One round trip after you stop; measured 5 to 12 s for a 13 s clip from the Pi |
| Cost | None | Billed by audio length on the OpenAI account (not counted in the euro figure shown in the chat) |
| Strength / weakness | Fast and private, but a small model: garbles names and loanwords even on clean audio | Much better French when it answers in time |

Why the text on the screen can change: the live words are the on-device model revising itself; at the end, OpenAI's text can replace them. The language model never edits the bubble. A small italic line under the bubble shows what the on-device model alone heard when it differs.

## Wake word

- openWakeWord, ONNX backend, only the configured model loaded (`hey_jarvis`, threshold 0.5, 2 s cooldown). The pretrained models are English only; a French wake word would need a custom trained model (open question).
- The model is reset after each detection, otherwise one utterance fires the command repeatedly.

## Microphone handling

- The USB mic (C-Media) only records at 44.1 or 48 kHz. `listener.py` falls back to that rate and resamples each block to 16 kHz in the processing loop, not in the audio callback.
- **Watchdog:** if no audio block arrives for 3 s the stream is closed and reopened; after 4 failed reopens, or if closing hangs, the process exits and systemd restarts it. This was added after the service froze with a dead USB stream. The underlying cause (about one input overflow per second on the Pi) is not found.
- The listener ignores the microphone while the assistant is speaking (doc 06).

## Utterance rules

| Rule | Value | Where |
|---|---|---|
| Silence at the start before giving up | 4 s (on-device endpointer) | `stt.py` `NO_SPEECH_TIMEOUT` |
| Silence that ends a sentence | 1 s | `stt.py` `END_SILENCE` |
| Longest utterance | 12 s | `max_listen_seconds` |
| Wait for a follow-up sentence | 10 s, ignoring the 4 s rule | `followup_seconds` |
| Wait for OpenAI's text | 6 s | `CloudUtterance` |

A pause of more than a second in the middle of a sentence cuts the sentence in two. The on-device endpointer is simple and has not been tuned.

## Hearing yourself back

`recordings.py` keeps the last **5** utterances (never more, whatever the setting) as 16 kHz WAV with the text, the on-device text and levels, shown in the settings dialog. This is the tool to tell a microphone problem from a transcription problem. Levels are peak, voice (the loudest 5 % of 80 ms frames) and background (the quietest 10 %), in dBFS. Healthy: voice between -30 and -10 dB and 20 dB or more above the background. `pi-con --transcribe file.wav` runs the on-device model on any recording.

## Vocabulary hint

The OpenAI request carries a short prompt ("Commandes vocales pour la maison : Jarvis, caméra, vidéo, Rabbit, allume, éteins, ferme, montre."). It helps the model spell "Rabbit" and "Jarvis", but a prompt made of commands can also be echoed back when the audio is mostly silence: one test returned "Jarvis, allume." for a clip in which the speaker said something else. See [10](10-transcription-options.md).
