import numpy as np
import logging

logger = logging.getLogger("voice_gateway.biometrics")

# Audio settings
SAMPLE_RATE = 16000

def pcm16_to_float32(pcm_bytes: bytes) -> np.ndarray:
    """Convert raw 16-bit PCM bytes to float32 NumPy array in range [-1.0, 1.0]."""
    if len(pcm_bytes) == 0:
        return np.array([], dtype=np.float32)
    return np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

def get_mel_filterbanks(num_filters: int = 26, nfft: int = 512, sample_rate: int = 16000) -> np.ndarray:
    """Compute triangular Mel-scale filterbank weights."""
    low_mel = 0
    high_mel = 2595 * np.log10(1 + (sample_rate / 2) / 700)
    mel_points = np.linspace(low_mel, high_mel, num_filters + 2)
    hz_points = 700 * (10**(mel_points / 2595) - 1)
    bin_points = np.floor((nfft + 1) * hz_points / sample_rate).astype(int)
    
    filters = np.zeros((num_filters, int(nfft / 2 + 1)))
    for m in range(1, num_filters + 1):
        f_m_minus = bin_points[m - 1]
        f_m = bin_points[m]
        f_m_plus = bin_points[m + 1]
        
        for k in range(f_m_minus, f_m):
            if f_m != f_m_minus:
                filters[m - 1, k] = (k - bin_points[m - 1]) / (f_m - f_m_minus)
        for k in range(f_m, f_m_plus):
            if f_m_plus != f_m:
                filters[m - 1, k] = (bin_points[m + 1] - k) / (f_m_plus - f_m)
    return filters

def estimate_pitch_f0(frame: np.ndarray, sample_rate: int = 16000) -> float:
    """Estimate pitch fundamental frequency (F0) using autocorrelation local peaks."""
    n = len(frame)
    if n == 0 or np.max(np.abs(frame)) < 1e-4:
        return None
    
    # Calculate autocorrelation
    r = np.correlate(frame, frame, mode='full')
    r = r[n-1:]  # Keep non-negative lags
    
    # Human pitch range: 50Hz to 400Hz
    min_lag = int(sample_rate / 400)  # ~40 samples
    max_lag = int(sample_rate / 50)   # ~320 samples
    
    if len(r) <= max_lag:
        return None
        
    # Search for the highest local peak (local maximum)
    best_lag = None
    max_val = -1.0
    
    for lag in range(min_lag, max_lag - 1):
        if r[lag] > r[lag - 1] and r[lag] > r[lag + 1]:
            if r[lag] > max_val:
                max_val = r[lag]
                best_lag = lag
                
    if best_lag is not None and r[0] > 0 and r[best_lag] / r[0] > 0.25:
        return float(sample_rate / best_lag)
    return None

def extract_voice_template(pcm_bytes: bytes, sample_rate: int = 16000) -> dict:
    """
    Extract MFCC features, pitch jitter, and spectral ranges using pure NumPy.
    Returns a dict containing the speaker fingerprint and anti-spoofing flags.
    """
    signal = pcm16_to_float32(pcm_bytes)
    if len(signal) < 3200:  # Less than 200ms of audio
        logger.warning("Audio input too short for feature extraction.")
        return None
        
    # Keep the raw signal for pitch estimation
    raw_signal = np.copy(signal)
    
    # Pre-emphasis filter (for MFCCs spectral balance only)
    emphasized = np.append(signal[0], signal[1:] - 0.97 * signal[:-1])
    
    # Framing parameterization
    frame_length = int(0.025 * sample_rate)  # 25ms -> 400 samples
    frame_step = int(0.010 * sample_rate)    # 10ms -> 160 samples
    signal_len = len(emphasized)
    
    num_frames = int(np.floor((signal_len - frame_length) / frame_step)) + 1
    
    # Pre-compute Hamming window and Mel filters
    window = np.hamming(frame_length)
    nfft = 512
    filters = get_mel_filterbanks(num_filters=26, nfft=nfft, sample_rate=sample_rate)
    
    mfcc_list = []
    pitch_list = []
    rms_list = []
    
    # Spectral energy variables
    total_power_spectrum = np.zeros(int(nfft / 2 + 1))
    
    for t in range(num_frames):
        start = t * frame_step
        frame = emphasized[start:start + frame_length]
        raw_frame = raw_signal[start:start + frame_length]
        
        # Apply Hamming window
        windowed_frame = frame * window
        
        # Power Spectrum FFT
        fft_complex = np.fft.rfft(windowed_frame, nfft)
        power_spec = (np.abs(fft_complex) ** 2) / nfft
        total_power_spectrum += power_spec
        
        # Mel filter energies
        mel_energies = np.dot(power_spec, filters.T)
        mel_energies = np.where(mel_energies == 0, np.finfo(float).eps, mel_energies)
        log_mel_energies = np.log(mel_energies)
        
        # Discrete Cosine Transform (DCT Type-II) to extract 13 MFCCs
        dct_matrix = np.cos(np.pi * np.arange(13)[:, None] * (2 * np.arange(26) + 1) / 52)
        mfccs = np.dot(dct_matrix, log_mel_energies)
        mfcc_list.append(mfccs)
        
        # RMS energy for speech cadence rhythm analysis
        rms = float(np.sqrt(np.mean(raw_frame ** 2)))
        rms_list.append(rms)
        
        # Pitch estimation on raw_frame (no pre-emphasis)
        f0 = estimate_pitch_f0(raw_frame, sample_rate)
        if f0 is not None:
            pitch_list.append(f0)
            
    if len(mfcc_list) == 0:
        return None
        
    # 1. Biometric vocal tract vector (Average MFCCs across voiced speech frames only)
    rms_arr = np.array(rms_list)
    max_rms = float(np.max(rms_arr)) if len(rms_arr) > 0 else 1.0
    voiced_indices = [i for i, r in enumerate(rms_list) if r >= 0.05 * max_rms]
    
    if len(voiced_indices) > 0:
        voiced_mfccs = [mfcc_list[i] for i in voiced_indices]
        mean_mfccs = np.mean(voiced_mfccs, axis=0)
    else:
        mean_mfccs = np.mean(mfcc_list, axis=0)
    
    # 1.5 Speech Cadence (Prosody / Rhythm) Analysis
    duration = len(signal) / sample_rate
    rms_arr = np.array(rms_list)
    
    # Smooth energy envelope using 3-frame convolution filter to remove noise spikes
    if len(rms_arr) > 3:
        smoothed_rms = np.convolve(rms_arr, np.ones(3)/3, mode='same')
    else:
        smoothed_rms = rms_arr
        
    # Detect local peaks (maxima) representing word syllables above a dynamic threshold (6% peak energy)
    max_energy = np.max(smoothed_rms) if len(smoothed_rms) > 0 else 1.0
    energy_threshold = 0.06 * max_energy
    
    peaks = []
    for i in range(1, len(smoothed_rms) - 1):
        if smoothed_rms[i] > smoothed_rms[i-1] and smoothed_rms[i] > smoothed_rms[i+1] and smoothed_rms[i] > energy_threshold:
            peaks.append(i)
            
    # Compute tempo (syllables per second)
    cadence_tempo = float(len(peaks) / duration) if duration > 0 else 0.0
    
    # Compute rhythm (interval variance between peaks)
    intervals = np.diff(peaks)
    cadence_rhythm = float(np.std(intervals)) if len(intervals) >= 2 else 0.0
    
    # 2. Anti-Spoofing 1: Pitch Jitter Check (Cloned/Synthetic Voice Detection)
    is_synthetic = False
    pitch_std = 0.0
    if len(pitch_list) >= 4:
        pitch_std = float(np.std(pitch_list))
        # Synthetic / Text-to-speech engines produce very flat or mechanical F0 tracks
        if pitch_std < 4.0:
            is_synthetic = True
            logger.warning(f"Liveness Check: Synthetic voice signature flagged. Pitch Jitter = {pitch_std:.2f} Hz.")
    else:
        # Not enough voiced frames: typically implies artificial or whispery playback
        is_synthetic = True
        logger.warning("Liveness Check: Insufficient pitch fluctuations. Flags synthetic voice.")
        
    # 2.2 Robotic Cadence Check:
    # If there are multiple syllables but they have a perfectly flat spacing interval (variance < 0.5 frames),
    # it indicates artificial clock-timed speech generation (robotic text-to-speech clone).
    if len(intervals) >= 3 and cadence_rhythm < 0.5:
        is_synthetic = True
        logger.warning(f"Liveness Check: Robotic constant cadence detected (StDev = {cadence_rhythm:.2f} frames). Flagging synthetic clone.")
        
    # 3. Anti-Spoofing 2: Frequency Band Analysis (Replay Defense)
    is_replay = False
    total_energy = np.sum(total_power_spectrum)
    low_ratio = 0.0
    high_ratio = 0.0
    if total_energy > 0:
        # Standard phone/laptop speakers attenuate low-frequency bass (<100Hz) and high-frequency treble (>7.5kHz)
        low_energy = np.sum(total_power_spectrum[:3])
        high_energy = np.sum(total_power_spectrum[240:])
        
        low_ratio = low_energy / total_energy
        high_ratio = high_energy / total_energy
        
        # Replay attacks have highly constricted/depleted spectral boundaries
        if low_ratio < 0.0005 and high_ratio < 0.0005:
            is_replay = True
            logger.warning(f"Liveness Check: Speaker replay attack flagged. Spectrum ratios: low={low_ratio:.5f}, high={high_ratio:.5f}")
            
    # Calculate confidence score
    liveness_score = 100.0
    if is_synthetic:
        liveness_score -= 50.0
    if is_replay:
        liveness_score -= 40.0
        
    is_live = liveness_score >= 70.0
    
    return {
        "fingerprint": mean_mfccs.tolist(),
        "is_synthetic": is_synthetic,
        "is_replay": is_replay,
        "is_live": is_live,
        "liveness_score": liveness_score,
        "pitch_std": pitch_std,
        "low_ratio": float(low_ratio),
        "high_ratio": float(high_ratio),
        "cadence_tempo": cadence_tempo,
        "cadence_rhythm": cadence_rhythm
    }
 
def verify_speaker(enrolled_profile: any, test_profile: dict, threshold: float = 0.52) -> tuple[bool, float, float]:
    """Compare voice profiles using L2 unit-vector cosine distance and similarity, plus prosody cadence matching."""
    if isinstance(enrolled_profile, list):
        enrolled_fingerprint = enrolled_profile
        enrolled_tempo = None
        enrolled_rhythm = None
    elif isinstance(enrolled_profile, dict):
        enrolled_fingerprint = enrolled_profile.get("fingerprint", [])
        enrolled_tempo = enrolled_profile.get("cadence_tempo")
        enrolled_rhythm = enrolled_profile.get("cadence_rhythm")
    else:
        return False, 1.0, 0.0

    test_fingerprint = test_profile.get("fingerprint", [])
    if not enrolled_fingerprint or not test_fingerprint or len(enrolled_fingerprint) < 13 or len(test_fingerprint) < 13:
        return False, 1.0, 0.0
    
    # Slice off the 0th coefficient to achieve volume-independent matching
    vec1 = np.array(enrolled_fingerprint[1:], dtype=np.float64)
    vec2 = np.array(test_fingerprint[1:], dtype=np.float64)
    
    norm1 = np.linalg.norm(vec1)
    norm2 = np.linalg.norm(vec2)
    
    if norm1 < 1e-6 or norm2 < 1e-6:
        return False, 1.0, 0.0

    u1 = vec1 / norm1
    u2 = vec2 / norm2
    
    cos_sim = float(np.dot(u1, u2))
    unit_distance = float(np.linalg.norm(u1 - u2))
    
    # Check behavioral cadence matching
    cadence_penalty = 0.0
    if enrolled_tempo is not None and enrolled_rhythm is not None:
        test_tempo = test_profile.get("cadence_tempo", 0.0)
        test_rhythm = test_profile.get("cadence_rhythm", 0.0)
        
        tempo_diff = abs(test_tempo - enrolled_tempo)
        rhythm_diff = abs(test_rhythm - enrolled_rhythm)
        
        # Apply a matching penalty if speaking rate or rhythm intervals differ significantly
        if tempo_diff > 3.5 or rhythm_diff > 12.0:
            cadence_penalty = 0.12  # Degrades matching score significantly
            logger.warning(f"Behavioral verification alert: Cadence mismatch. tempo_diff={tempo_diff:.2f}, rhythm_diff={rhythm_diff:.2f}")
            
    effective_distance = unit_distance + cadence_penalty
    matched = (effective_distance < threshold) and (cos_sim >= 0.85)
    return matched, effective_distance, cos_sim
