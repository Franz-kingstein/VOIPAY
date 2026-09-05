import abc
import os
import math
import struct
import logging
import base64
import io
import wave
import numpy as np

logger = logging.getLogger("voice_gateway.tts")

def pcm_to_wav_b64(pcm_bytes: bytes, sample_rate: int = 16000) -> str:
    """Wrap raw 16-bit PCM bytes into a valid WAV container and base64 encode."""
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as wav_file:
        wav_file.setnchannels(1)     # mono
        wav_file.setsampwidth(2)     # 16-bit (2 bytes)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    
    # Call seek(0) after closing the wave file and before reading bytes
    wav_buf.seek(0)
    wav_bytes = wav_buf.read()
    return base64.b64encode(wav_bytes).decode("utf-8")

def float32_to_pcm16_bytes(waveform, sample_rate: int = 16000) -> bytes:
    """Convert float32 numpy waveform in range [-1.0, 1.0] to 16-bit PCM bytes."""
    if isinstance(waveform, bytes):
        waveform = np.frombuffer(waveform, dtype=np.float32)
    elif not isinstance(waveform, np.ndarray):
        waveform = np.array(waveform, dtype=np.float32)
    
    # Clip to [-1.0, 1.0], scale to int16 range, and cast
    clipped = np.clip(waveform, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype(np.int16)
    return pcm16.tobytes()

class TTSAdapter(abc.ABC):
    @abc.abstractmethod
    async def synthesize_waveform(self, text: str) -> np.ndarray:
        """Synthesize text reply into float32 numpy waveform array in range [-1.0, 1.0]."""
        pass

    async def synthesize(self, text: str) -> tuple[str, str]:
        """Convert synthesized float32 waveform to PCM16, write WAV container, log details, and encode."""
        # 1. Synthesize the raw float32 waveform
        waveform = await self.synthesize_waveform(text)
        
        # 2. Query actual output sample_rate from configuration
        sample_rate = getattr(self, "sample_rate", 16000)
        
        # 3. Cast float32 waveform to int16 PCM bytes
        pcm_bytes = float32_to_pcm16_bytes(waveform, sample_rate)
        
        # Log dtype, shape, sample_rate, and pcm_bytes length right after synthesis
        logger.info(
            f"TTS synthesis complete: dtype={waveform.dtype}, shape={waveform.shape}, "
            f"sample_rate={sample_rate}, pcm_bytes_len={len(pcm_bytes)}"
        )
        
        # 4. Debug dump mode: when TTS_DEBUG_DUMP=true, write synthesized waveform to disk as debug_output.wav
        if os.getenv("TTS_DEBUG_DUMP", "false").lower() == "true":
            debug_path = "debug_output.wav"
            logger.info(f"TTS_DEBUG_DUMP=true: Writing synthesized wav file to disk at '{debug_path}'")
            try:
                with wave.open(debug_path, "wb") as debug_file:
                    debug_file.setnchannels(1)
                    debug_file.setsampwidth(2) # 2 bytes for int16
                    debug_file.setframerate(sample_rate)
                    debug_file.writeframes(pcm_bytes)
            except Exception as de:
                logger.error(f"Failed to write debug WAV file: {de}")
                
        # 5. Pack int16 PCM bytes into proper WAV container written to BytesIO buffer
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wav_file:
            wav_file.setnchannels(1)     # mono
            wav_file.setsampwidth(2)     # 2 bytes for 16-bit PCM
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_bytes)
            
        # Call buffer.seek(0) after wave.open(...).close() and before reading bytes
        wav_buf.seek(0)
        wav_bytes = wav_buf.read()
        
        b64_audio = base64.b64encode(wav_bytes).decode("utf-8")
        return b64_audio, "audio/wav"

class MockTTS(TTSAdapter):
    """Zero-dependency offline synthesizer generating a mock sine wave in a WAV container."""
    def __init__(self):
        self.sample_rate = 16000

    async def synthesize_waveform(self, text: str) -> np.ndarray:
        logger.info(f"Mock TTS synthesizing response: '{text}'")
        duration = 0.8
        frequency = 440
        t = np.linspace(0, duration, int(duration * self.sample_rate), endpoint=False)
        waveform = 0.5 * np.sin(2 * np.pi * frequency * t)
        return waveform.astype(np.float32)

def detect_language(text: str) -> tuple[str, str]:
    """Detect language for demo: Tamil if Tamil unicode script is present, otherwise default to English."""
    # 1. Tamil script check
    if any('\u0B80' <= char <= '\u0BFF' for char in text):
        return 'ta', 'co.in'
        
    # Default to Indian English accent for all demo interactions
    return 'en', 'co.in'

class GTTSAdapter(TTSAdapter):
    """gTTS wrapper returning valid MP3 bytes directly, bypassing float32 WAV conversion."""
    def __init__(self):
        self.sample_rate = 16000

    async def synthesize_waveform(self, text: str) -> np.ndarray:
        mock_tts = MockTTS()
        return await mock_tts.synthesize_waveform(text)

    async def synthesize(self, text: str) -> tuple[str, str]:
        try:
            from gtts import gTTS
            import io
            
            lang, tld = detect_language(text)
            logger.info(f"gTTS API synthesizing response in lang='{lang}' (TLD={tld}): '{text}'")
            tts = gTTS(text=text, lang=lang, tld=tld)
            fp = io.BytesIO()
            tts.write_to_fp(fp)
            mp3_bytes = fp.getvalue()
            
            # Save debug MP3 file if debug mode is active
            if os.getenv("TTS_DEBUG_DUMP", "false").lower() == "true":
                try:
                    with open("debug_output.mp3", "wb") as f:
                        f.write(mp3_bytes)
                except Exception as de:
                    logger.error(f"Failed to write debug MP3 file: {de}")
            
            b64_audio = base64.b64encode(mp3_bytes).decode("utf-8")
            return b64_audio, "audio/mp3"
        except Exception as e:
            logger.error(f"gTTS synthesis failed: {e}. Falling back to MockTTS WAV.")
            mock_tts = MockTTS()
            return await mock_tts.synthesize(text)

class CoquiTTS(TTSAdapter):
    """Coqui TTS local server interface returning WAV audio generated from float32 waveforms."""
    def __init__(self):
        # Read from config: actual Coqui output sample rate is 22050
        self.sample_rate = 22050

    async def synthesize_waveform(self, text: str) -> np.ndarray:
        # Simulate Coqui TTS returning a float32 waveform
        duration = 0.8
        t = np.linspace(0, duration, int(duration * self.sample_rate), endpoint=False)
        waveform = 0.5 * np.sin(2 * np.pi * 440 * t)
        return waveform.astype(np.float32)

class ElevenLabsTTS(TTSAdapter):
    """ElevenLabs wrapper falling back to MockTTS WAV."""
    def __init__(self):
        self.sample_rate = 16000

    async def synthesize_waveform(self, text: str) -> np.ndarray:
        logger.warning("ElevenLabs TTS credentials not found. Simulating waveform.")
        mock_tts = MockTTS()
        return await mock_tts.synthesize_waveform(text)

def get_tts_provider() -> TTSAdapter:
    provider = os.getenv("TTS_PROVIDER", "mock").lower()
    if provider == "gtts":
        return GTTSAdapter()
    elif provider == "coqui":
        return CoquiTTS()
    elif provider == "elevenlabs":
        return ElevenLabsTTS()
    return MockTTS()
