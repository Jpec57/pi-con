# 10 Transcription: evidence, options, and how to decide

Transcription is the bottleneck of the whole assistant: when the text is wrong, nothing downstream can be right. This document records what has been **measured**, what is **inferred**, the options, and a way to choose. **No decision has been taken** (D-22).

## What was measured (2026-11-01, on the Pi)

Five real recordings from the owner's testing, from the "hear yourself back" panel. Levels use the corrected metric (voice = loudest 5 % of 80 ms frames).

| Recording | Length | Peak | Voice | Background | Voice above background | Text the app used |
|---|---|---|---|---|---|---|
| 1 | 13.0 s | -10.2 dB | -20.6 dB | -45.5 dB | 24.9 dB | "elle est pas affichée" |
| 2 | 10.7 s | -9.7 dB | -21.2 dB | -45.9 dB | 24.7 dB | *(nothing)* |
| 3 | 11.8 s | -22.7 dB | -34.5 dB | -45.7 dB | 11.2 dB | "si l'eau meilleur résultat s'est me cmus est à idées" |
| 4 | 14.6 s | -10.9 dB | -34.3 dB | -46.5 dB | 12.2 dB | "deux" |
| 5 | 10.7 s | -14.8 dB | -27.7 dB | -47.2 dB | 19.5 dB | *(nothing)* |

Other measurements:

| # | Measurement | Result |
|---|---|---|
| M1 | The on-device model on clean, synthetic French audio ("Peux-tu me parler de Dragon Ball Z, s'il te plaît ?") | "peux tu me parler de dragons bolzide s'il te plaît": names and loanwords are garbled even with perfect audio |
| M2 | The same five recordings run offline through the on-device model (`pi-con --transcribe`) | **Identical to the live text** in all five, including the two empty ones: the live path loses nothing; it is the model |
| M3 | Time for OpenAI to transcribe recording 1 (13 s, 405 KB) from the Pi | **12.0 s**; two further runs of a near-identical file took 8.1 s and 5.1 s |
| M4 | The Pi's upload speed (400 KB POST to a speed-test endpoint, 3 runs) | 3.2 s, 3.7 s, 5.0 s: **about 80 to 130 KB/s** (roughly 1 Mbit/s up). Latency to `api.openai.com` is fine (about 25 ms) |
| M5 | The text the app used vs the on-device text, in the three recordings that have text | Identical in all three: the OpenAI text was not what ended up on screen |
| M6 | Pi log: in 2 of 5 cases the chat request is logged before the transcription response | Consistent with the app giving up after 6 s, then the late answer arriving about 0.03 to 0.3 s later (inference from log order, not a direct measurement) |
| M7 | The OpenAI request on recording 1 with the vocabulary prompt | Returned **"Jarvis, allume."**: words from the prompt, not from the speech. A second attempt on a (barely) trimmed version with the prompt returned the correct sentence; without the prompt, "Keno ele pafche." |
| M8 | Length of the recordings | 10.7 to 14.6 s for sentences of 1 to 3 s: they include the silence of the 10 s follow-up window |

The sample is **five clips from one speaker**; treat the percentages as impressions, not statistics.

## What the evidence says

**F1. The cloud transcription is usually thrown away.** The app waits 6 s for OpenAI. From this Pi a 13 s uncompressed clip needs 3 to 5 s just to upload (M4) plus OpenAI's own time, 5 to 12 s in total (M3). The on-device text is then used (M5, M6). So AI mode with "cloud transcription" on mostly behaves like on-device only, while still sending the audio.

**F2. The upload is much bigger than it needs to be.** WAV is uncompressed (32 KB per second), and the clip includes the leading silence of the follow-up window (M8). A 2 s sentence travels as 10 to 14 s of audio.

**F3. The cloud engine cannot rescue a total on-device miss.** A clip is only uploaded if the on-device model heard something (to avoid uploading silence). Recordings 2 and 5 contain speech at healthy levels but the on-device text was empty, so nothing was sent and the sentence was lost (M2).

**F4. The vocabulary prompt can be echoed.** On a mostly-silent clip the model answered with the prompt's words (M7). It also helps on clean speech, so it should be handled, not just removed.

**F5. The on-device model is weak, especially on names and quiet speech.** Clean audio does not fix names (M1). The worst sentence (recording 3) is also the quietest (voice 34 dB below full scale, 11 dB above the background).

**F6. The microphone is not clearly the problem.** Three of five clips are at healthy levels (voice about -21 to -28 dB, 20 to 25 dB above a steady background of about -46 dB), and two of those three still failed on-device or were lost (F3). Quiet speech (recordings 3 and 4) is worse: speaking closer or louder, or more gain, is the cheapest test.

## Candidate causes, strongest evidence first

| | Cause | Evidence | Fix direction |
|---|---|---|---|
| C1 | Cloud answer arrives after the 6 s wait | F1, M3 to M6 | Make the upload small and fast; wait adaptively |
| C2 | Clip is large: uncompressed, with leading silence | F2 | Trim, compress |
| C3 | Cloud is skipped when the on-device model hears nothing | F3 | Gate on audio energy, not on local text |
| C4 | Vocabulary prompt echoed on near-silence | F4 | Shorter prompt, or none for sentences with little speech |
| C5 | Small on-device model, names and loanwords | M1, F5 | A better local model (not benchmarked) or rely on the cloud (C1 to C3 first) |
| C6 | Quiet speech, distance, microphone gain | F6 | Placement, gain, a better microphone |
| C7 | Sentences cut by a pause longer than 1 s | By design, not measured | Tune the end-of-speech rule |

## Options

| Option | What it changes | Effort | Cost / risk | Fixes |
|---|---|---|---|---|
| **A. Trim the leading silence before upload**, and start the recording at the first speech with a short pre-roll (about 0.4 s) | Typical clip 2 to 4 s instead of 10 to 14 s: about 100 KB, about 1 s to upload | Small, no new dependency | Cutting a soft first word if the pre-roll is too short | C1, C2, C4 (partly) |
| **B. Compress the upload** (FLAC, or Ogg/MP3) | 2 to 10 times smaller | Small to medium: needs an encoder (`soundfile` / libsndfile, or `ffmpeg`) | A new dependency on the Pi; check the formats the endpoint accepts | C1, C2 |
| **C. Upload when there is speech by energy, even if the on-device text is empty** | Recordings 2 and 5 would have gone to OpenAI | Small | A little more audio sent; a threshold to tune | C3 |
| **D. Wait longer, or adaptively** (for example up to 12 s) | The cloud text is used more often | Trivial | Slower answers; worth doing only together with A | C1 |
| **E. Shorter vocabulary hint**, or none unless the sentence is short | Fewer echoes of commands | Trivial | Slightly worse spelling of "Rabbit" and "Jarvis" | C4 |
| **F. Stream the audio** (OpenAI Realtime transcription over a WebSocket) while you speak | No upload wait after you stop; text ready almost at once | Large: a streaming client, resampling, new failure modes, different billing | Most complex; the best latency | C1 fundamentally |
| **G. A better on-device model** (larger French zipformer, or a Whisper-based model through sherpa-onnx) | Better text without the cloud | Medium, **needs a benchmark first** (accuracy and speed on this Pi) | Pi CPU load; larger download; accuracy not known | C5 |
| **H. Microphone**: closer, more gain, another USB port, a better microphone (the ReSpeaker XVF3800 or a speakerphone in the buying guide) | Cleaner, louder signal; echo cancellation would also allow barge-in | Hardware | Cost | C6 |
| **I. Tune the end-of-speech rule** (1 s of silence; mid-sentence pauses) | Fewer cut sentences | Small | Slower end detection | C7 |

## A suggested order (mine, not decided)

1. **A + C + E together**, because they are small, need no new dependency and attack F1 to F4 directly: send less audio (about 100 KB), send it when there is speech, and echo less. Re-measure with the same five-clip protocol: the expected effect is a cloud answer well inside 6 s, so the better text is finally used.
2. **D** if answers still arrive late; **B** if the upload is still the slow part.
3. **H (placement and gain) in parallel**: it costs nothing to try speaking closer, and recording 3 and 4 suggest it matters.
4. **F or G only if the above is not enough**, and G only after a benchmark.

Reasoning: the cloud model is the better engine and the assistant already pays to send it the audio, so making its answer *arrive in time* is the cheapest large win. Replacing the on-device model (G) or streaming (F) are bigger bets that should wait for the numbers.

## How to measure, so the decision rests on data

Use the tools that exist:

1. Write a fixed list of **20 sentences** the assistant should understand (commands, questions, a few names such as "Pikachu", "Rabbit"). Say each one, in the same spot, at normal volume, some from across the room.
2. For each sentence note: what was said, what the screen shows, the line "on-device model heard", whether the final text came from OpenAI (the bubble's tooltip), and the time between the end of the sentence and the final text.
3. Count word errors by hand (or sentences understood well enough to act on). Do this **per configuration**: today's, then after each option.
4. Keep the clips: the microphone test panel keeps the last 5 only, so save the interesting ones (right-click the player) and replay them with `pi-con --transcribe file.wav` to compare engines or settings on identical audio.
5. To time OpenAI separately from the upload, send the same clip from the Pi and from the Mac: the difference is the uplink.

What this document cannot say yet: whether OpenAI's transcription is accurate enough on this microphone once it answers in time. The 13 s test (M7) returned a correct sentence once and the prompt's words once, from near-identical audio.
