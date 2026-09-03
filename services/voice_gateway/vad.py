import abc
import os
import logging
import numpy as np

logger = logging.getLogger("voice_gateway.vad")

class VADAdapter(abc.ABC):
    @abc.abstractmethod
    def is_speech(self, pcm_chunk: bytes) -> bool:
        """Determines if the raw PCM16 audio chunk contains speech."""
        pass

class SimpleEnergyVAD(VADAdapter):
    """A lightweight energy-based VAD that requires no dependencies and runs instantly."""
    def __init__(self, threshold: int = 400):
        self.threshold = threshold

    def is_speech(self, pcm_chunk: bytes) -> bool:
        if not pcm_chunk:
            return False
        # Convert PCM16 bytes to numpy array
        audio_data = np.frombuffer(pcm_chunk, dtype=np.int16)
        if len(audio_data) == 0:
            return False
        # Calculate root mean square (RMS) energy
        rms = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))
        return rms > self.threshold

class SileroVAD(VADAdapter):
    """Silero VAD implementation using torch."""
    def __init__(self):
        self.model = None
        self.read_audio = None
        try:
            import torch
            # Local imports to avoid torch dependency overhead on fallback
            torch.set_num_threads(1)
            self.model, utils = torch.hub.load(
                repo_or_dir='snakers4/silero-vad',
                model='silero_vad',
                force_reload=False,
                onnx=False
            )
            (self.get_speech_timestamps, _, self.read_audio, _, _) = utils
            logger.info("Loaded Silero VAD model successfully.")
        except Exception as e:
            logger.error(f"Failed to load Silero VAD: {e}. Falling back to energy-based VAD.")

    def is_speech(self, pcm_chunk: bytes) -> bool:
        if self.model is None or not pcm_chunk:
            # Fallback
            return SimpleEnergyVAD().is_speech(pcm_chunk)
            
        try:
            import torch
            audio_data = np.frombuffer(pcm_chunk, dtype=np.int16).astype(np.float32) / 32768.0
            # Convert to torch tensor
            tensor = torch.from_numpy(audio_data)
            # Evaluate using model
            speech_prob = self.model(tensor, 16000).item()
            return speech_prob > 0.5
        except Exception as e:
            logger.error(f"Error in Silero VAD evaluation: {e}")
            return False

def get_vad_provider() -> VADAdapter:
    provider = os.getenv("VAD_PROVIDER", "simple").lower()
    if provider == "silero":
        return SileroVAD()
    return SimpleEnergyVAD()
