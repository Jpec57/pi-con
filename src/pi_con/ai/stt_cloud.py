from __future__ import annotations

import io
import logging
import wave

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = "Commandes vocales pour la maison : Jarvis, caméra, vidéo, Rabbit, allume, éteins, ferme, montre."


class OpenAITranscriber:
    """Transcribes one finished utterance with OpenAI (much better French than the small local model).

    Local sherpa still detects the end of speech; this only replaces its text. Failures return '' so the caller keeps the local text.
    """

    def __init__(self, api_key: str, model: str = "gpt-4o-transcribe", prompt: str = DEFAULT_PROMPT) -> None:
        import openai

        self.client = openai.OpenAI(api_key=api_key, timeout=8.0, max_retries=0)
        self.model, self.prompt = model, prompt

    def transcribe(self, pcm: np.ndarray) -> str:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16_000)
            wav.writeframes(pcm.astype(np.int16).tobytes())
        try:
            reply = self.client.audio.transcriptions.create(
                model=self.model, file=("speech.wav", buffer.getvalue(), "audio/wav"), language="fr", prompt=self.prompt,
            )
        except Exception as error:  # network, key, quota: fall back to the local transcript
            logger.warning("Cloud transcription failed (%s); keeping the local text", error)
            return ""
        return " ".join(str(reply.text).split())
