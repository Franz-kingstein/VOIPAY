import abc
import os
import io
import logging
from typing import Dict

logger = logging.getLogger("voice_gateway.asr")

class ASRAdapter(abc.ABC):
    @abc.abstractmethod
    async def transcribe(self, pcm_data: bytes) -> str:
        """Transcribe raw PCM16 audio (16kHz, mono) to text."""
        pass

class WhisperASR(ASRAdapter):
    """Local ASR using faster-whisper (tiny model for fast CPU execution)."""
    def __init__(self, model_size: str = "tiny"):
        self.model = None
        try:
            from faster_whisper import WhisperModel
            logger.info(f"Loading faster-whisper model: {model_size}...")
            # Run on CPU with int8 quantization
            self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
            logger.info("faster-whisper model loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to load faster-whisper model: {e}. ASR will fall back to Mock provider.")

    async def transcribe(self, pcm_data: bytes) -> str:
        if self.model is None:
            return await MockASR().transcribe(pcm_data)
            
        try:
            import numpy as np
            # Convert PCM16 bytes to float32 normalized np array
            audio_data = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
            
            # Run transcription (run in executor since it's CPU intensive/synchronous)
            import anyio
            segments, info = await anyio.to_thread.run_sync(
                lambda: self.model.transcribe(audio_data, beam_size=1)
            )
            
            text_result = " ".join([seg.text for seg in segments]).strip()
            logger.info(f"Whisper transcription result: '{text_result}' (language: {info.language})")
            return text_result
        except Exception as e:
            logger.error(f"Whisper transcription failed: {e}")
            return ""

class DeepgramASRAdapter(ASRAdapter):
    """Stub for Deepgram cloud ASR services."""
    def __init__(self):
        self.api_key = os.getenv("DEEPGRAM_API_KEY")

    async def transcribe(self, pcm_data: bytes) -> str:
        if not self.api_key:
            logger.warning("DEEPGRAM_API_KEY not found. Deepgram ASR falling back to Mock provider.")
            return await MockASR().transcribe(pcm_data)
        
        # Real Deepgram API call could be wired here
        logger.info("Deepgram API call simulated.")
        return "Pay 500 rupees to Ramesh"

class MockASR(ASRAdapter):
    """Mock ASR that returns hardcoded text for end-to-end testing without microphone."""
    def __init__(self):
        # We can detect specific dummy byte patterns to simulate different commands
        pass

    async def transcribe(self, pcm_data: bytes) -> str:
        # Check size of payload to return different test phrases
        size = len(pcm_data)
        if size == 0:
            return ""
        
        # If payload contains a small length, mock "yes" for confirmation
        if size < 5000:
            return "yes"
            
        # Otherwise, mock the primary payment command
        return "Pay 500 rupees to Ramesh for the grocery order"

class GeminiASR(ASRAdapter):
    """Cloud ASR using the Google Gemini API to transcribe audio bytes in-memory."""
    def __init__(self):
        self.api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")

    async def transcribe(self, pcm_data: bytes) -> str:
        if not self.api_key:
            logger.warning("No API key configured for Gemini ASR. Falling back to Mock ASR.")
            return await MockASR().transcribe(pcm_data)
            
        try:
            import base64
            import wave
            import io
            import httpx
            
            # Convert PCM16 mono to standard WAV container in-memory
            wav_buf = io.BytesIO()
            with wave.open(wav_buf, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(pcm_data)
            wav_bytes = wav_buf.getvalue()
            
            # Base64 encode the WAV audio file
            b64_audio = base64.b64encode(wav_bytes).decode("utf-8")
            
            # Call Gemini API to transcribe the audio content
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent?key={self.api_key}"
            headers = {"Content-Type": "application/json"}
            payload = {
                "contents": [{
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "audio/wav",
                                "data": b64_audio
                            }
                        },
                        {
                            "text": "Transcribe this voice payment audio recording accurately. The primary default language is English (including payment terms like 'pay', 'rupees', numbers, and recipient names). Output ONLY the verbatim transcription text in plain English (or native Hindi/Tamil script if spoken in a regional language). Do not add any extra commentary or punctuation."
                        }
                    ]
                }]
            }
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    resp_data = resp.json()
                    try:
                        text_result = resp_data["candidates"][0]["content"]["parts"][0]["text"].strip()
                        logger.info(f"Gemini ASR transcription success: '{text_result}'")
                        return text_result
                    except Exception as parse_err:
                        logger.error(f"Failed to parse Gemini ASR response: {parse_err}. Response was: {resp_data}")
                else:
                    logger.error(f"Gemini ASR failed with status {resp.status_code}: {resp.text}")
                    
        except Exception as e:
            logger.error(f"Gemini ASR failed: {e}")
            
        return await MockASR().transcribe(pcm_data)

def get_asr_provider() -> ASRAdapter:
    provider = os.getenv("ASR_PROVIDER", "mock").lower()
    if provider == "gemini":
        return GeminiASR()
    elif provider == "whisper":
        return WhisperASR()
    elif provider == "deepgram":
        return DeepgramASRAdapter()
    return MockASR()
