import sys
import os
import logging
import asyncio
import base64
import json
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Add workspace root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from shared.tracing import TracingMiddleware, get_trace_id, get_tracing_headers
from shared.auth.jwt_utils import create_access_token
from services.voice_gateway.vad import get_vad_provider
from services.voice_gateway.asr import get_asr_provider
from services.voice_gateway.tts import get_tts_provider
from services.voice_gateway import biometrics

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

class TraceLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[TraceID: {get_trace_id()}] {msg}", kwargs

logger = TraceLoggerAdapter(logging.getLogger("voice_gateway"), {})

app = FastAPI(title="Voice Gateway (ASR/TTS/VAD)")

# Enable CORS for local testing dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TracingMiddleware)

# Services configuration
AGENT_CORE_URL = os.getenv("AGENT_CORE_URL", "http://agent_core:8001")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# Initialize modules
vad = get_vad_provider()
asr = get_asr_provider()
tts = get_tts_provider()

@app.get("/health")
def health():
    return {"status": "ok", "service": "voice_gateway"}

async def handle_pubsub_notifications(session_id: str, websocket: WebSocket):
    """Subscribe to Redis pub/sub channel for session and push payment outcomes to client."""
    redis_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/1"
    logger.info(f"Starting pub/sub subscriber for channel session_channel_{session_id}")
    
    try:
        r_client = aioredis.from_url(redis_url, decode_responses=True)
        pubsub = r_client.pubsub()
        channel_name = f"session_channel_{session_id}"
        await pubsub.subscribe(channel_name)
        
        while True:
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message:
                    logger.info(f"Pub/sub message received on {channel_name}: {message['data']}")
                    data = json.loads(message["data"])
                    
                    # Synthesize text based on status
                    payload = data.get("payload", {})
                    status = data.get("status", "failed")
                    order_id = data.get("order_id")
                    
                    # Extract amount or fallback
                    amount = payload.get("amount") or 500.0
                    utr = payload.get("utr_number")
                    
                    if status == "captured":
                        speech_text = f"Payment of {amount} rupees was successful. Reference number is {utr}."
                    else:
                        reasons = ", ".join(payload.get("reasons", [])) or "Bank declined the transaction"
                        speech_text = f"Payment of {amount} rupees failed. Reason: {reasons}."
                        
                    # Synthesize TTS
                    try:
                        audio_b64, audio_mime = await tts.synthesize(speech_text)
                    except Exception as tts_err:
                        logger.error(f"TTS synthesis failed: {tts_err}")
                        audio_b64, audio_mime = None, None
                    
                    # Push to client
                    await websocket.send_json({
                        "type": "webhook_event",
                        "status": status,
                        "spoken_text": speech_text,
                        "audio": audio_b64,
                        "audio_mime": audio_mime,
                        "payload": payload
                    })
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in pub/sub message loop: {e}")
                await asyncio.sleep(1.0)
                
    except Exception as e:
        logger.error(f"Redis Pub/Sub client connection failed: {e}. Gateway will not stream live notifications.")

@app.websocket("/ws/audio/{session_id}")
async def websocket_audio_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    logger.info(f"WebSocket connection established for session {session_id}")
    
    # Redis client for biometrics storage
    redis_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/1"
    r_client = aioredis.from_url(redis_url, decode_responses=True)
    enrollment_mode = False
    
    # 1. Issue JWT for MCP authentication during this session
    session_token = create_access_token({"session_id": session_id})
    
    # Send configuration packet to client
    await websocket.send_json({
        "type": "config",
        "session_token": session_token,
        "msg": "Connected to Voice Gateway. Streaming enabled."
    })
    
    # 2. Spawn Redis Pub/Sub task for live webhook notifications
    pubsub_task = asyncio.create_task(handle_pubsub_notifications(session_id, websocket))
    
    # Slicing state parameters
    speech_buffer = bytearray()
    silence_frames = 0
    in_speech = False
    
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                logger.info(f"Received websocket.disconnect message for session {session_id}")
                break
            
            # WebSocket client can send binary PCM audio, or control JSON
            if "bytes" in message:
                pcm_chunk = message["bytes"]
                
                is_active = vad.is_speech(pcm_chunk)
                is_flush = len(pcm_chunk) < 1000
                
                import time
                if not is_flush:
                    # Accumulate all incoming audio frames
                    speech_buffer.extend(pcm_chunk)
                    
                    if is_active:
                        if not in_speech:
                            logger.info(f"VAD transition: [speech_start] at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                            in_speech = True
                        silence_frames = 0
                    else:
                        if in_speech:
                            silence_frames += 1
                            
                    # Auto-fallback: if we hit a long silence threshold (7 seconds of silence), auto-trigger speech end
                    if in_speech and silence_frames >= 27:
                        logger.info(f"VAD transition: [speech_end] via silence threshold (7s) at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                        is_flush = True
                
                if is_flush and len(speech_buffer) > 0:
                    if in_speech:
                        logger.info(f"VAD transition: [speech_end] via flush/stop signal at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                    else:
                        logger.info(f"Manual flush signal received: Slicing buffer even though VAD remained inactive.")
                    
                    # 16kHz 16-bit mono PCM is 32,000 bytes per second (32 bytes per ms)
                    buffer_ms = len(speech_buffer) / 32.0
                    logger.info(f"ASR: Processing audio buffer of size {len(speech_buffer)} bytes (~{buffer_ms:.1f} ms)")
                    
                    pcm_bytes = bytes(speech_buffer)
                    bio_template = biometrics.extract_voice_template(pcm_bytes)
                    
                    if enrollment_mode:
                        enrollment_mode = False
                        speech_buffer = bytearray()
                        in_speech = False
                        silence_frames = 0
                        
                        if bio_template:
                            await r_client.set(f"voice_profile:{session_id}", json.dumps(bio_template["fingerprint"]))
                            logger.info(f"Biometrics: Enrolled voice signature successfully for session {session_id}")
                            await websocket.send_json({
                                "type": "biometric_status",
                                "enrolled": True,
                                "msg": "Voice signature successfully enrolled!"
                            })
                            reply_msg = "Your voice biometric signature has been successfully registered."
                            audio_b64, audio_mime = await tts.synthesize(reply_msg)
                            await websocket.send_json({
                                "type": "agent_reply",
                                "text": reply_msg,
                                "action": "done",
                                "order_id": None,
                                "payment_id": None,
                                "audio": audio_b64,
                                "audio_mime": audio_mime
                            })
                        else:
                            await websocket.send_json({
                                "type": "biometric_status",
                                "enrolled": False,
                                "msg": "Failed to extract voice features. Please try again."
                            })
                        continue
                    
                    # Transcribe buffered audio
                    transcript = await asr.transcribe(pcm_bytes)
                    logger.info(f"ASR raw transcript result: '{transcript}'")
                    
                    # Reset state variables
                    speech_buffer = bytearray()
                    in_speech = False
                    silence_frames = 0
                    
                    if transcript:
                        # Send transcription back to browser
                        await websocket.send_json({
                            "type": "transcript",
                            "text": transcript,
                            "is_final": True
                        })
                        
                        # Evaluate voice biometrics matching & liveness
                        biometrics_passed = True
                        liveness_passed = True
                        biometric_score = 100.0
                        liveness_score = 100.0
                        distance = 0.0
                        pitch_std = 0.0
                        is_synthetic = False
                        is_replay = False
                        
                        enrolled_profile_json = await r_client.get(f"voice_profile:{session_id}")
                        if enrolled_profile_json:
                            enrolled_fingerprint = json.loads(enrolled_profile_json)
                            if bio_template:
                                matched, distance = biometrics.verify_speaker(enrolled_fingerprint, bio_template)
                                biometrics_passed = matched
                                liveness_passed = bio_template["is_live"]
                                liveness_score = bio_template["liveness_score"]
                                is_synthetic = bio_template["is_synthetic"]
                                is_replay = bio_template["is_replay"]
                                pitch_std = bio_template["pitch_std"]
                                biometric_score = max(0.0, min(100.0, 100.0 - (distance * 1.67)))
                            else:
                                biometrics_passed = False
                                biometric_score = 0.0
                                liveness_passed = False
                                liveness_score = 0.0
                        else:
                            # Not enrolled yet: skip speaker match, check liveness only
                            if bio_template:
                                liveness_passed = bio_template["is_live"]
                                liveness_score = bio_template["liveness_score"]
                                is_synthetic = bio_template["is_synthetic"]
                                is_replay = bio_template["is_replay"]
                                pitch_std = bio_template["pitch_std"]
                                
                        # Send biometric verification result back to browser logs
                        await websocket.send_json({
                            "type": "biometric_verification",
                            "passed": biometrics_passed,
                            "liveness_passed": liveness_passed,
                            "biometric_score": biometric_score,
                            "liveness_score": liveness_score,
                            "distance": distance,
                            "pitch_jitter": pitch_std,
                            "low_ratio": bio_template.get("low_ratio", 0.0) if bio_template else 0.0,
                            "high_ratio": bio_template.get("high_ratio", 0.0) if bio_template else 0.0,
                            "cadence_tempo": bio_template.get("cadence_tempo", 0.0) if bio_template else 0.0,
                            "cadence_rhythm": bio_template.get("cadence_rhythm", 0.0) if bio_template else 0.0,
                            "enrolled": enrolled_profile_json is not None
                        })
                        
                        # Send text to Agent Core orchestrator
                        headers = get_tracing_headers()
                        async with httpx.AsyncClient(timeout=60.0) as client:
                            try:
                                resp = await client.post(
                                    f"{AGENT_CORE_URL}/agent/chat",
                                    json={
                                        "session_id": session_id,
                                        "message": transcript,
                                        "token": session_token,
                                        "metadata": {
                                            "biometrics": {
                                                "passed": biometrics_passed,
                                                "liveness_passed": liveness_passed,
                                                "liveness_score": liveness_score,
                                                "biometric_score": biometric_score,
                                                "distance": distance,
                                                "pitch_std": pitch_std,
                                                "is_synthetic": is_synthetic,
                                                "is_replay": is_replay,
                                                "enrolled": enrolled_profile_json is not None
                                            }
                                        }
                                    },
                                    headers=headers
                                )
                                
                                if resp.status_code == 200:
                                    reply_data = resp.json()
                                    logger.info(f"Agent reply processed by Agent Core: {reply_data}")
                                    
                                    # Synthesize TTS
                                    spoken_text = reply_data.get("spoken_text", "")
                                    
                                    # Strip out any [Live API Warning: ...] prefix for TTS synthesis so it doesn't speak out JSON/quota warnings
                                    tts_text = spoken_text
                                    if tts_text.startswith("[Live API Warning:"):
                                        import re
                                        tts_text = re.sub(r"^\[Live API Warning:[^\]]+\]\s*", "", tts_text)
                                        
                                    try:
                                        audio_b64, audio_mime = await tts.synthesize(tts_text)
                                    except Exception as tts_err:
                                        logger.error(f"TTS synthesis failed: {tts_err}")
                                        audio_b64, audio_mime = None, None
                                    
                                    # Send back speech audio + reply packet
                                    await websocket.send_json({
                                        "type": "agent_reply",
                                        "text": spoken_text,
                                        "action": reply_data.get("action"),
                                        "order_id": reply_data.get("order_id"),
                                        "payment_id": reply_data.get("payment_id"),
                                        "audio": audio_b64,
                                        "audio_mime": audio_mime
                                    })
                                else:
                                    logger.error(f"Agent Core returned status code {resp.status_code}")
                            except Exception as ae:
                                logger.error(f"Failed to communicate with Agent Core: {ae}")
                                await websocket.send_json({
                                    "type": "error",
                                    "msg": "Agent Core communication failure"
                                })
            
            elif "text" in message:
                # Handle text-based messages
                data = json.loads(message["text"])
                msg_type = data.get("type")
                
                if msg_type == "start_enrollment":
                    enrollment_mode = True
                    logger.info("Voice enrollment mode active.")
                    await websocket.send_json({
                        "type": "biometric_status",
                        "enrolled": False,
                        "msg": "Enrollment mode started. Speak for 3 seconds to register."
                    })
                    continue
                elif msg_type == "clear_biometrics":
                    await r_client.delete(f"voice_profile:{session_id}")
                    logger.info("Voice signature cleared.")
                    await websocket.send_json({
                        "type": "biometric_status",
                        "enrolled": False,
                        "msg": "Biometric signature cleared."
                    })
                    continue
                elif msg_type == "check_status":
                    is_enrolled = await r_client.exists(f"voice_profile:{session_id}")
                    await websocket.send_json({
                        "type": "biometric_status",
                        "enrolled": bool(is_enrolled),
                        "msg": "Profile registered" if is_enrolled else "No profile registered"
                    })
                    continue
                
                if msg_type == "text_input":
                    text_msg = data.get("text")
                    logger.info(f"Received text input from client: '{text_msg}'")
                    
                    # Forward directly to Agent Core
                    headers = get_tracing_headers()
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        try:
                            resp = await client.post(
                                f"{AGENT_CORE_URL}/agent/chat",
                                json={
                                    "session_id": session_id,
                                    "message": text_msg,
                                    "token": session_token
                                },
                                headers=headers
                            )
                            if resp.status_code == 200:
                                reply_data = resp.json()
                                # Synthesize TTS
                                spoken_text = reply_data.get("spoken_text", "")
                                try:
                                    audio_b64, audio_mime = await tts.synthesize(spoken_text)
                                except Exception as tts_err:
                                    logger.error(f"TTS synthesis failed: {tts_err}")
                                    audio_b64, audio_mime = None, None
                                
                                await websocket.send_json({
                                    "type": "agent_reply",
                                    "text": spoken_text,
                                    "action": reply_data.get("action"),
                                    "order_id": reply_data.get("order_id"),
                                    "payment_id": reply_data.get("payment_id"),
                                    "audio": audio_b64,
                                    "audio_mime": audio_mime
                                })
                        except Exception as ae:
                            logger.error(f"Failed to process text input: {ae}")
                            
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for session {session_id}")
    finally:
        pubsub_task.cancel()
