#!/usr/bin/env bash
# macOS only: synthesise a "Hey Jarvis" sample (16 kHz mono 16-bit WAV) for testing without a microphone.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p samples
say -o samples/hey_jarvis.aiff "[[slnc 1500]] Hey Jarvis [[slnc 1200]]"
afconvert -f WAVE -d LEI16@16000 -c 1 samples/hey_jarvis.aiff samples/hey_jarvis.wav
rm samples/hey_jarvis.aiff
echo "Created samples/hey_jarvis.wav"
