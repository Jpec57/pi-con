# 06 Voice output and conversation flow

How the assistant speaks, how it avoids hearing itself, and how a conversation continues or ends. Code: `tts.py`, `listener.py`, `router.py`.

## Text to speech

- **Engine:** [Piper](https://github.com/OHF-Voice/piper1-gpl), local and free, voice `fr_FR-siwis-medium` (about 63 MB, `models/tts-fr`). Loaded on first use, so the option costs no memory or start-up time while unused.
- **What is read:** `ai_reply` events (language model answers and canned replies) and `say` events (a skill's optional `"say"` sentence). Markdown, links and symbols are removed and long answers are cut at a sentence end (600 characters).
- **When:** an answer is read once it is *complete*. It is not spoken sentence by sentence while streaming, so a long answer adds its whole generation time before the voice starts (a possible improvement).
- **On by default** (`"tts": {"enabled": true}`); the Voix button and the voice commands "coupe la voix" / "active la voix" override it and the choice is saved in `.state/tts.json`.
- **Playback:** `sounddevice` to `tts.output_device`, resampled to the rate the device supports. A queue of 4 sentences; `POST /tts/stop` cuts the current one.

## The audio output on the Pi

This is where reality differs from the plan, and it has not been verified end to end:

- The Pi has **no analog output and the USB device is a microphone only**. The only playback hardware is HDMI.
- `output_device` is `"pulse"`: PipeWire plays to the Pi's default sink, which is HDMI. The ALSA `default` device failed with "Device unavailable".
- The `pulse` device only exists for a process that can reach the user's audio session. A systemd system service has none, so the unit written by `deploy.sh` sets `XDG_RUNTIME_DIR=/run/user/<uid of the service user>` and waits for `user@<uid>.service` (D-15). An earlier version used `%U`, which systemd expands to root's id in a system service: wrong.
- After the fix, playback runs without error in the service. **Whether sound is audible depends on a screen or speaker on the HDMI port, which could not be checked remotely.** For a real assistant, buy a USB speaker or speakerphone (see the buying guide in `PROJECT.md`) and set `output_device` to its name from `pi-con --list-devices`.

## Not hearing itself

The USB microphone has no echo cancellation, so the assistant would hear its own voice and could wake itself. `Speaker.busy()` is true from the moment a sentence is **queued** until 0.4 s after it ends, and the listener drops every microphone frame while it is true (D-16).

Consequences: the assistant cannot be interrupted by voice (no barge-in; that needs echo cancellation, for example a speakerphone) and the wake word is not heard while it speaks.

## A conversation: the follow-up window

After an answer has been read (or immediately, if the voice is off), the listener opens a **follow-up window** of `followup_seconds` (default 10) in which it listens for the next sentence **without the wake word** (D-17). Say nothing and it closes, back to the wake word. Each answer opens a new window, so a conversation can go on.

Details that matter:

- The recogniser's own "nothing heard after 4 s" end is ignored during the window; only the 10 s deadline closes it.
- The recording for that sentence starts when the window opens, so it includes the silence before you speak. This makes uploads bigger and slower (doc 02, doc 10).
- The conversation memory (6 exchanges) is reset after 2 minutes without a sentence.

## Ending a conversation

- **By phrase:** "au revoir", "au revoir Jarvis", "à bientôt", "à plus tard", "bonne nuit", "c'est tout" are canned replies with `"end": true`: the goodbye is spoken and **no window is opened**.
- **By the model:** in AI mode the model can call `end_conversation` for any other way of saying goodbye. The speaker notes it and opens no window after the model's next answer.
- "Stop listening" means ending the conversation, not stopping the wake word listener: that always runs, otherwise nothing could wake the assistant.

## Verified and not verified

| Verified | Not verified |
|---|---|
| Synthesis and playback run without error on the Mac and on the Pi (with `pulse`) | That the sound is audible on the Pi |
| Muting, queueing, follow-up opening and closing, goodbye, in unit tests with fake audio | The follow-up window and goodbye with real French speech |
| Piper 1.8 installs on Python 3.13 aarch64 | First-sentence delay on the Pi (about 5 s including the one-time voice load, one measurement) |
