import base64
import io
import wave
import numpy as np
import pytest
from services.voice_gateway.tts import MockTTS, CoquiTTS

@pytest.mark.anyio
async def test_tts_mock_wav_encoding():
    tts = MockTTS()
    b64_audio, mime_type = await tts.synthesize("Hello world")
    
    assert mime_type == "audio/wav"
    assert b64_audio is not None
    
    audio_bytes = base64.b64decode(b64_audio)
    
    # Assert standard WAV RIFF/WAVE headers
    assert audio_bytes[0:4] == b"RIFF"
    assert audio_bytes[8:12] == b"WAVE"
    
    # Open WAV using wave module
    with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
        n_channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        framerate = wav_file.getframerate()
        n_frames = wav_file.getnframes()
        
        assert n_channels == 1
        assert sample_width == 2  # 16-bit
        assert framerate == tts.sample_rate
        assert n_frames > 0
        
        # Read frames and verify signal variance is non-trivial (not flat DC offset)
        frames = wav_file.readframes(n_frames)
        samples = np.frombuffer(frames, dtype=np.int16)
        
        assert len(samples) == n_frames
        variance = np.var(samples)
        assert variance > 1.0

@pytest.mark.anyio
async def test_tts_coqui_wav_encoding():
    tts = CoquiTTS()
    b64_audio, mime_type = await tts.synthesize("Hello world")
    
    assert mime_type == "audio/wav"
    assert b64_audio is not None
    
    audio_bytes = base64.b64decode(b64_audio)
    
    assert audio_bytes[0:4] == b"RIFF"
    assert audio_bytes[8:12] == b"WAVE"
    
    # Open WAV using wave module
    with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
        n_channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        framerate = wav_file.getframerate()
        n_frames = wav_file.getnframes()
        
        assert n_channels == 1
        assert sample_width == 2
        assert framerate == tts.sample_rate
        assert n_frames > 0
        
        frames = wav_file.readframes(n_frames)
        samples = np.frombuffer(frames, dtype=np.int16)
        
        assert len(samples) == n_frames
        variance = np.var(samples)
        assert variance > 1.0
