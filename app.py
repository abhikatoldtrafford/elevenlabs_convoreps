import os
import io
from io import BytesIO
import random
import time
import threading
import requests
import logging
from pathlib import Path
from urllib.parse import urlparse
import asyncio
import re
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, Response, url_for, send_from_directory, redirect, session
from flask_cors import CORS
from twilio.twiml.voice_response import VoiceResponse
from dotenv import load_dotenv
from pydub import AudioSegment
import openai
from openai import OpenAI, AsyncOpenAI
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs
from flask_sock import Sock
import websocket
import json
import base64
import struct
from twilio.twiml.voice_response import Connect, Stream
# Load environment variables ASAP
load_dotenv()


# Media Stream configuration
USE_MEDIA_STREAMS = os.getenv("USE_MEDIA_STREAMS", "false").lower() == "true"
MEDIA_STREAM_CHUNK_SIZE = 8000  # 8kHz for Twilio
media_stream_connections = {}  # Track active streams
# Suppress excessive werkzeug logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Initialize Flask app
app = Flask(__name__)
sock = Sock(app)
# Set the secret key for sessions
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# Enable CORS (Cross-Origin Resource Sharing)
CORS(app)

# Env + API keys
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# Initialize new streaming clients
sync_openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
async_openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
elevenlabs_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

# Streaming configuration
USE_STREAMING = os.getenv("USE_STREAMING", "true").lower() == "true"
SENTENCE_STREAMING = os.getenv("SENTENCE_STREAMING", "true").lower() == "true"

# Handle STREAMING_TIMEOUT with potential comments
timeout_env = os.getenv("STREAMING_TIMEOUT", "3.0")
# ADD:
# Set defaults if not in environment
os.environ.setdefault("USE_STREAMING", "true")
os.environ.setdefault("SENTENCE_STREAMING", "true")
os.environ.setdefault("STREAMING_TIMEOUT", "3.0")
# Remove any comments if present
timeout_value = timeout_env.split('#')[0].strip()
try:
    STREAMING_TIMEOUT = float(timeout_value)
except ValueError:
    print(f"⚠️ Invalid STREAMING_TIMEOUT value: '{timeout_env}', using default 3.0")
    STREAMING_TIMEOUT = 3.0

# Twilio credentials
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
# Media stream state
media_streams = {}  # Track active media streams
stream_queues = {}  # Audio queues for each stream
# In-memory state
turn_count = {}
mode_lock = {}
active_call_sid = None
conversation_history = {}
personality_memory = {}
interview_question_index = {}

# Voice profiles
personality_profiles = {
    "rude/skeptical": {"voice_id": "1t1EeRixsJrKbiF1zwM6"},
    "super busy": {"voice_id": "6YQMyaUWlj0VX652cY1C"},
    "small_talk": {"voice_id": "2BJW5coyhAzSr8STdHbE"}
}

cold_call_personality_pool = {
    "Jerry": {
        "voice_id": "1t1EeRixsJrKbiF1zwM6",
        "system_prompt": "You're Jerry. You're a skeptical small business owner who's been burned by vendors in the past. You're not rude, but you're direct and hard to win over. Respond naturally based on how the call starts — maybe this is a cold outreach, maybe a follow-up, or even someone calling you with bad news. Stay in character. If the salesperson fumbles, challenge them. If they're smooth, open up a bit. Speak casually, not like a script."
    },
    "Miranda": {
        "voice_id": "Ax1GP2W4XTyAyNHuch7v",
        "system_prompt": "You're Miranda. You're a busy office manager who doesn't have time for fluff. If the caller is clear and respectful, you'll hear them out. Respond naturally depending on how they open — this could be a cold call, a follow-up, or someone delivering news. Keep your tone grounded and real. Interrupt if needed. No robotic replies — talk like a real person at work."
    },
    "Junior": {
        "voice_id": "Nbttze9nhGhK1czblc6j",
        "system_prompt": "You're Junior. You run a local shop and have heard it all. You're friendly but not easily impressed. Start skeptical, but if the caller handles things well, loosen up. Whether this is a pitch, a follow-up, or some kind of check-in, reply naturally. Use casual language. If something sounds off, call it out. You don't owe anyone your time — but you're not a jerk either."
    },
    "Brett": {
        "voice_id": "7eFTSJ6WtWd9VCU4ZlI1",
        "system_prompt": "You're Brett. You're a contractor who answers his phone mid-job. You're busy and a little annoyed this person called. If they're direct and helpful, give them a minute. If they ramble, shut it down. This could be a pitch, a check-in, or someone following up on a proposal. React based on how they start the convo. Talk rough, fast, and casual. No fluff, no formalities."
    },
    "Kayla": {
        "voice_id": "aTxZrSrp47xsP6Ot4Kgd",
        "system_prompt": "You're Kayla. You own a business and don't waste time. You've had too many bad sales calls and follow-ups from people who don't know how to close. Respond based on how they open — if it's a pitch, hit them with price objections. If it's a follow-up, challenge their urgency. Keep your tone sharp but fair. You don't sugarcoat things, and you don't fake interest."
    }
}

interview_questions = [
    "Can you walk me through your most recent role and responsibilities?",
    "What would you say are your greatest strengths?",
    "What is one weakness you're working on improving?",
    "Can you describe a time you faced a big challenge at work and how you handled it?",
    "How do you prioritize tasks when you're busy?",
    "Tell me about a time you went above and beyond for a customer or client.",
    "Why are you interested in this position?",
    "Where do you see yourself in five years?",
    "How do you handle feedback and criticism?",
    "Do you have any questions for me about the company or the role?"
]

def async_route(f):
    """Production-ready async route decorator"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Use asyncio.run() which properly manages the event loop
        return asyncio.run(f(*args, **kwargs))
    return wrapper

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


def delayed_cleanup(call_sid):
    time.sleep(15)  # Let Twilio play it first
    try:
        os.remove(f"static/response_{call_sid}.mp3")
        os.remove(f"static/response_ready_{call_sid}.txt")
        print(f"🧹 Cleaned up response files for {call_sid}")
    except Exception as e:
        print(f"⚠️ Cleanup error for {call_sid}: {e}")


@app.route("/voice", methods=["POST", "GET"])
def voice():
    call_sid = request.values.get("CallSid")
    recording_url = request.values.get("RecordingUrl")

    print(f"==> /voice hit. CallSid: {call_sid}")
    if recording_url:
        filename = recording_url.split("/")[-1]
        print(f"🎧 Incoming Recording SID: {filename}")

    if call_sid not in turn_count:
        turn_count[call_sid] = 0
    else:
        turn_count[call_sid] += 1

    print(f"🧪 Current turn: {turn_count[call_sid]}")

    response = VoiceResponse()

    # First turn - play greeting
    if turn_count[call_sid] == 0:
        print("📞 First turn — playing appropriate greeting")
        
        if not session.get("has_called_before"):
            session["has_called_before"] = True
            greeting_file = "first_time_greeting.mp3"
            print("👋 New caller detected — playing first-time greeting.")
        else:
            greeting_file = "returning_user_greeting.mp3"
            print("🔁 Returning caller — playing returning greeting.")
        
        response.play(f"{request.url_root}static/{greeting_file}?v={time.time()}")
        
        # After greeting, decide streaming vs file-based
        if USE_MEDIA_STREAMS:
            print("🚀 Using Media Streams for real-time audio")
            # Start bidirectional stream
            connect = Connect()
            connect.stream(
                url=f'wss://{request.host}/media-stream/{call_sid}',
                track='both_tracks'
            )
            response.append(connect)
        else:
            # Fall back to traditional record
            response.record(
                action="/transcribe",
                method="POST",
                max_length=30,
                timeout=6,
                play_beep=True,
                trim="trim-silence"
            )
    
    else:
        # Subsequent turns
        if USE_MEDIA_STREAMS:
            # For streaming, we handle everything in WebSocket
            # Just maintain the connection
            connect = Connect()
            connect.stream(
                url=f'wss://{request.host}/media-stream/{call_sid}',
                track='both_tracks'
            )
            response.append(connect)
        else:
            # Original file-based approach
            mp3_path = f"static/response_{call_sid}.mp3"
            flag_path = f"static/response_ready_{call_sid}.txt"
            
            def is_file_ready(mp3_path, flag_path):
                if not os.path.exists(mp3_path) or not os.path.exists(flag_path):
                    return False
                if os.path.getsize(mp3_path) < 1500:
                    print("⚠️ MP3 file exists but is too small, not ready yet.")
                    return False
                return True
            
            if is_file_ready(mp3_path, flag_path):
                print(f"🔊 Playing: {mp3_path}")
                public_mp3_url = f"{request.url_root}static/response_{call_sid}.mp3"
                response.play(public_mp3_url)
            else:
                print("⏳ Response not ready — redirecting to /voice")
                response.pause(length=2)
                response.redirect(f"{request.url_root}voice")
                return str(response)
            
            response.record(
                action="/transcribe",
                method="POST",
                max_length=30,
                timeout=6,
                play_beep=True,
                trim="trim-silence"
            )
    
    return str(response)
@sock.route('/media-stream/<call_sid>')
def media_stream(ws, call_sid):
    """Handle real-time media streaming"""
    print(f"🎙️ Media stream connected for {call_sid}")
    
    # Initialize stream state
    stream_sid = None
    audio_buffer = bytearray()
    is_speaking = False
    silence_duration = 0
    media_stream_connections[call_sid] = {
        'ws': ws,
        'stream_sid': None,
        'audio_queue': asyncio.Queue() if USE_STREAMING else None
    }
    
    try:
        while True:
            message = ws.receive()
            if message is None:
                break
                
            data = json.loads(message)
            
            if data['event'] == 'start':
                stream_sid = data['start']['streamSid']
                media_stream_connections[call_sid]['stream_sid'] = stream_sid
                print(f"📡 Stream started: {stream_sid}")
                
                # Send initial greeting if first turn
                if turn_count.get(call_sid, 0) == 0:
                    asyncio.run(stream_greeting(ws, call_sid))
                    
            elif data['event'] == 'media':
                # Decode incoming audio
                payload = data['media']['payload']
                audio_chunk = base64.b64decode(payload)
                audio_buffer.extend(audio_chunk)
                
                # Simple voice activity detection (VAD)
                # Check if user is speaking based on audio levels
                audio_level = sum(abs(b - 128) for b in audio_chunk) / len(audio_chunk)
                
                if audio_level > 10:  # Threshold for speech
                    if not is_speaking:
                        is_speaking = True
                        print("🗣️ User started speaking")
                    silence_duration = 0
                else:
                    if is_speaking:
                        silence_duration += len(audio_chunk) / 8000  # 8kHz sample rate
                        
                        if silence_duration > 1.5:  # 1.5 seconds of silence
                            is_speaking = False
                            print("🤫 User stopped speaking")
                            
                            # Process the audio buffer
                            if len(audio_buffer) > 8000:  # At least 1 second
                                asyncio.run(process_streaming_audio(
                                    audio_buffer, call_sid, ws
                                ))
                                audio_buffer = bytearray()
                                
            elif data['event'] == 'stop':
                print(f"⏹️ Stream stopped for {call_sid}")
                break
                
    except Exception as e:
        print(f"❌ Media stream error: {e}")
    finally:
        if call_sid in media_stream_connections:
            del media_stream_connections[call_sid]
        print(f"👋 Media stream disconnected for {call_sid}")
async def process_streaming_audio(audio_buffer, call_sid, ws):
    """Process audio buffer and stream response"""
    try:
        # Convert mulaw to WAV for transcription
        wav_data = convert_mulaw_to_wav(audio_buffer)
        
        # Save temporarily for transcription
        temp_file = f"temp_{call_sid}.wav"
        with open(temp_file, 'wb') as f:
            f.write(wav_data)
        
        # Transcribe
        transcript = await streaming_transcribe(temp_file)
        print(f"📝 Streaming transcription: {transcript}")
        
        # Clean up temp file
        os.remove(temp_file)
        
        # Get conversation context and generate response
        # (reuse your existing logic from transcribe())
        mode = mode_lock.get(call_sid, "unknown")
        messages = build_messages_for_call(call_sid, transcript, mode)
        
        # Stream the response
        if USE_STREAMING and SENTENCE_STREAMING:
            # Generate and stream sentence by sentence
            await stream_gpt_tts_response(messages, call_sid, ws)
        else:
            # Generate full response then stream
            reply = await streaming_gpt_response(messages, voice_id, call_sid)
            await stream_audio_file(f"static/response_{call_sid}.mp3", ws)
            
    except Exception as e:
        print(f"❌ Streaming audio processing error: {e}")
def convert_mulaw_to_wav(mulaw_data):
    """Convert mulaw audio to WAV format"""
    # Twilio sends 8-bit mulaw at 8kHz
    import audioop
    
    # Convert mulaw to linear PCM
    linear_data = audioop.ulaw2lin(bytes(mulaw_data), 2)
    
    # Create WAV header
    channels = 1
    sample_width = 2
    framerate = 8000
    num_frames = len(linear_data) // sample_width
    
    wav_header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        36 + len(linear_data),
        b'WAVE',
        b'fmt ',
        16,  # fmt chunk size
        1,   # PCM format
        channels,
        framerate,
        framerate * channels * sample_width,
        channels * sample_width,
        sample_width * 8,
        b'data',
        len(linear_data)
    )
    
    return wav_header + linear_data

async def stream_gpt_tts_response(messages, call_sid, ws):
    """Generate and stream GPT response with TTS in real-time"""
    try:
        # Get voice for this call
        voice_id = get_voice_for_call(call_sid)
        
        # Stream GPT response
        model = "gpt-4.1-nano-2025-04-14" if USE_STREAMING else "gpt-3.5-turbo"
        stream = await async_openai.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            temperature=0.7
        )
        
        sentence_buffer = ""
        
        async for chunk in stream:
            if chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                sentence_buffer += text
                
                # Check for sentence completion
                sentences = re.split(r'(?<=[.!?])\s+', sentence_buffer)
                
                # Stream complete sentences
                for sentence in sentences[:-1]:
                    if sentence.strip():
                        # Generate TTS and stream immediately
                        await stream_tts_sentence(sentence, voice_id, ws)
                
                sentence_buffer = sentences[-1] if sentences else ""
        
        # Handle remaining text
        if sentence_buffer.strip():
            await stream_tts_sentence(sentence_buffer, voice_id, ws)
            
    except Exception as e:
        print(f"❌ GPT/TTS streaming error: {e}")

async def stream_tts_sentence(text, voice_id, ws):
    """Generate TTS and stream to WebSocket"""
    try:
        # Generate TTS
        response = elevenlabs_client.text_to_speech.stream(
            voice_id=voice_id,
            text=text,
            model_id="eleven_turbo_v2_5",
            output_format="ulaw_8000",  # Twilio's format
            voice_settings=VoiceSettings(
                stability=0.4,
                similarity_boost=0.75
            )
        )
        
        # Stream chunks directly to Twilio
        for chunk in response:
            if chunk:
                # Encode and send via WebSocket
                encoded_chunk = base64.b64encode(chunk).decode('utf-8')
                media_message = {
                    "event": "media",
                    "streamSid": media_stream_connections[call_sid]['stream_sid'],
                    "media": {
                        "payload": encoded_chunk
                    }
                }
                ws.send(json.dumps(media_message))
                
    except Exception as e:
        print(f"❌ TTS streaming error: {e}")

async def stream_greeting(ws, call_sid):
    """Stream greeting audio file"""
    greeting_file = "static/first_time_greeting.mp3"
    if os.path.exists(greeting_file):
        # Convert MP3 to mulaw and stream
        from pydub import AudioSegment
        audio = AudioSegment.from_mp3(greeting_file)
        audio = audio.set_frame_rate(8000).set_channels(1)
        
        # Convert to mulaw
        mulaw_data = audioop.lin2ulaw(audio.raw_data, 2)
        
        # Send in chunks
        chunk_size = 320  # 20ms at 8kHz
        for i in range(0, len(mulaw_data), chunk_size):
            chunk = mulaw_data[i:i+chunk_size]
            encoded_chunk = base64.b64encode(chunk).decode('utf-8')
            
            media_message = {
                "event": "media",
                "streamSid": media_stream_connections[call_sid]['stream_sid'],
                "media": {
                    "payload": encoded_chunk
                }
            }
            ws.send(json.dumps(media_message))
            await asyncio.sleep(0.02)  # 20ms chunks

def get_voice_for_call(call_sid):
    """Get voice ID for current call context"""
    # Reuse your existing voice selection logic
    mode = mode_lock.get(call_sid, "unknown")
    if mode == "cold_call":
        persona_name = personality_memory.get(call_sid)
        if persona_name:
            return cold_call_personality_pool[persona_name]["voice_id"]
    elif mode == "small_talk":
        return "2BJW5coyhAzSr8STdHbE"
    # ... etc
    return "1t1EeRixsJrKbiF1zwM6"  # default

def build_messages_for_call(call_sid, transcript, mode):
    """Build message context for GPT (reuse existing logic)"""
    # Extract this logic from your current transcribe() function
    # Return the messages array for GPT
    pass
async def streaming_transcribe(audio_file_path: str) -> str:
    """Transcription with streaming control"""
    try:
        if USE_STREAMING:
            # Try streaming transcription
            with open(audio_file_path, "rb") as audio_file:
                try:
                    stream = await async_openai.audio.transcriptions.create(
                        model="gpt-4o-mini-transcribe",
                        file=audio_file,
                        response_format="text",
                        stream=True,
                        language="en"
                    )
                    
                    transcript = ""
                    async for event in stream:
                        if hasattr(event, 'delta') and hasattr(event.delta, 'text'):
                            transcript += event.delta.text
                        elif hasattr(event, 'text') and event.text:
                            transcript = event.text
                            
                    return transcript.strip()
                    
                except Exception as e:
                    print(f"⚠️ Streaming transcription failed: {e}")
                    # Fall through to non-streaming
        
        # Non-streaming (either USE_STREAMING=False or fallback)
        with open(audio_file_path, "rb") as f:
            result = sync_openai.audio.transcriptions.create(
                model="whisper-1",
                file=f
            )
            return result.text.strip()
                
    except Exception as e:
        print(f"💥 Transcription error: {e}")
        raise

async def streaming_gpt_response(messages: list, voice_id: str, call_sid: str) -> str:
    """Stream GPT response and generate TTS concurrently"""
    try:
        # Use GPT-4.1-nano-2025-04-14 for streaming (FIXED MODEL NAME)
        model = "gpt-4.1-nano" if USE_STREAMING else "gpt-3.5-turbo"
        
        if USE_STREAMING and SENTENCE_STREAMING:
            # Streaming with sentence detection
            stream = await async_openai.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                temperature=0.7,
                max_tokens=500  # Add token limit to prevent too long responses
            )
            
            full_response = ""
            sentence_buffer = ""
            audio_chunks = []
            
            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    text = chunk.choices[0].delta.content
                    sentence_buffer += text
                    full_response += text
                    
                    # Check for sentence completion
                    sentences = re.split(r'(?<=[.!?])\s+', sentence_buffer)
                    
                    # Process complete sentences
                    for sentence in sentences[:-1]:
                        if sentence.strip():
                            # Start TTS generation for this sentence
                            audio_task = asyncio.create_task(
                                generate_tts_streaming(sentence, voice_id)
                            )
                            audio_chunks.append(audio_task)
                    
                    # Keep incomplete sentence in buffer
                    sentence_buffer = sentences[-1] if sentences else ""
            
            # Process remaining text
            if sentence_buffer.strip():
                audio_task = asyncio.create_task(
                    generate_tts_streaming(sentence_buffer, voice_id)
                )
                audio_chunks.append(audio_task)
            
            # Wait for all TTS tasks and combine audio
            if audio_chunks:
                audio_results = await asyncio.gather(*audio_chunks)
                
                # Combine audio chunks properly
                from pydub import AudioSegment
                combined = AudioSegment.empty()
                
                for audio_data in audio_results:
                    if audio_data:
                        try:
                            # Convert bytes to AudioSegment
                            audio_segment = AudioSegment.from_mp3(io.BytesIO(audio_data))
                            combined += audio_segment
                            # Add 200ms silence between sentences for natural pacing
                            if len(audio_results) > 1:
                                silence = AudioSegment.silent(duration=200)
                                combined += silence
                        except Exception as e:
                            print(f"⚠️ Error combining audio chunk: {e}")
                            continue
                
                # Export combined audio
                output_path = f"static/response_{call_sid}.mp3"
                combined.export(output_path, format="mp3")
                    
                print(f"✅ Streaming audio saved to {output_path}")
            
            return full_response.strip()
            
        else:
            # Non-streaming fallback
            completion = await async_openai.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.7
            )
            return completion.choices[0].message.content.strip()
            
    except Exception as e:
        print(f"💥 GPT streaming error: {e}")
        # Fallback to non-streaming
        completion = sync_openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages
        )
        return completion.choices[0].message.content.strip()


async def generate_tts_streaming(text: str, voice_id: str) -> bytes:
    """Generate TTS using streaming ElevenLabs API with retry logic"""
    max_retries = 3
    retry_delay = 1.0
    
    for attempt in range(max_retries):
        try:
            if USE_STREAMING:
                # Use streaming TTS
                response = elevenlabs_client.text_to_speech.stream(
                    voice_id=voice_id,
                    text=text,
                    model_id="eleven_turbo_v2_5",
                    output_format="mp3_22050_32",
                    voice_settings=VoiceSettings(
                        stability=0.4,
                        similarity_boost=0.75
                    )
                )
                
                # Collect audio chunks
                audio_data = io.BytesIO()
                chunk_count = 0
                for chunk in response:
                    if chunk:
                        audio_data.write(chunk)
                        chunk_count += 1
                
                if chunk_count == 0:
                    raise Exception("No audio chunks received")
                        
                audio_data.seek(0)
                return audio_data.read()
                
            else:
                # Fallback to non-streaming
                audio_gen = elevenlabs_client.text_to_speech.convert(
                    voice_id=voice_id,
                    text=text,
                    model_id="eleven_monolingual_v1",
                    output_format="mp3_22050_32"
                )
                
                audio_data = b""
                for chunk in audio_gen:
                    if chunk:
                        audio_data += chunk
                        
                return audio_data
                
        except Exception as e:
            print(f"💥 TTS attempt {attempt + 1} failed: {e}")
            if "10054" in str(e) or "connection" in str(e).lower():
                # Connection error - wait and retry
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                    continue
            
            # For last attempt or non-connection errors, try fallback
            if attempt == max_retries - 1:
                print("🔄 Falling back to non-streaming TTS...")
                try:
                    audio_gen = elevenlabs_client.text_to_speech.convert(
                        voice_id=voice_id,
                        text=text,
                        model_id="eleven_monolingual_v1",
                        output_format="mp3_22050_32"
                    )
                    
                    audio_data = b""
                    for chunk in audio_gen:
                        if chunk:
                            audio_data += chunk
                            
                    return audio_data
                except Exception as e2:
                    print(f"❌ TTS fallback also failed: {e2}")
                    raise
    
    raise Exception("All TTS attempts failed")

@app.route("/transcribe", methods=["POST"])
@async_route
async def transcribe():
    global active_call_sid
    print("✅ /transcribe endpoint hit")
    print(f"  USE_STREAMING: {USE_STREAMING}")
    print(f"  SENTENCE_STREAMING: {SENTENCE_STREAMING}")
    call_sid = request.form.get("CallSid")
    recording_url = request.form.get("RecordingUrl") + ".wav"
    print(f"🎧 Downloading: {recording_url}")
    print(f"🛍️ ACTIVE CALL SID at start of /transcribe: {active_call_sid}")

    if not recording_url or not call_sid:
        return "Missing Recording URL or CallSid", 400

    if call_sid != active_call_sid:
        print(f"💨 Resetting memory for new call_sid: {call_sid}")
        conversation_history.clear()
        mode_lock.clear()
        personality_memory.clear()
        turn_count.clear()
        active_call_sid = call_sid

    # Download audio with timeout and async handling
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            response = requests.get(
                recording_url, 
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                timeout=5  # Add timeout
            )
            if response.status_code == 200:
                break
            print(f"⏳ Attempt {attempt + 1}: Audio not ready yet...")
            await asyncio.sleep(1.5)  # Use async sleep
        except requests.exceptions.Timeout:
            print(f"⏳ Attempt {attempt + 1}: Request timed out")
            await asyncio.sleep(1.5)
    else:
        return "Recording not available", 500

    with open("input.wav", "wb") as f:
        f.write(response.content)

    if os.path.getsize("input.wav") < 1500:
        print("⚠️ Skipping transcription — audio file too short.")
        return redirect(url_for("voice", _external=True))

    # Streaming transcription
    try:
        if USE_STREAMING:
            transcript = await streaming_transcribe("input.wav")
        else:
            # Fallback to non-streaming
            with open("input.wav", "rb") as f:
                result = sync_openai.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language="en"
                )
                transcript = result.text.strip()
                
        print("📝 Transcription:", transcript)
    except Exception as e:
        print("💥 Whisper error:", e)
        return redirect(url_for("voice", _external=True))
    
    def detect_bad_news(text):
        lowered = text.lower()
        return any(phrase in lowered for phrase in [
            "bad news", "unfortunately", "problem", "delay", "issue", 
            "we can't", "we won't", "not going to happen", "reschedule", "price increase"
        ])

    def detect_intent(text):
        lowered = text.lower()
        if any(phrase in lowered for phrase in ["cold call", "customer call", "sales call", "business call"]):
            return "cold_call"
        elif any(phrase in lowered for phrase in ["interview", "interview prep"]):
            return "interview"
        elif any(phrase in lowered for phrase in ["small talk", "chat", "talk casually"]):
            return "small_talk"
        return "unknown"

    if "let's start over" in transcript.lower():
        print("🔁 Reset triggered by user — rolling new persona")
        conversation_history.pop(call_sid, None)
        personality_memory.pop(call_sid, None)
        turn_count[call_sid] = 0
        transcript = "cold call practice"

    mode = mode_lock.get(call_sid)
    if not mode:
        mode = detect_intent(transcript)
        mode_lock[call_sid] = mode
    print("🧐 Detected intent:", mode)

    # Set up personality and voice
    if mode == "cold_call" or mode == "customer_convo":
        if call_sid not in personality_memory:
            persona_name = random.choice(list(cold_call_personality_pool.keys()))
            personality_memory[call_sid] = persona_name
        else:
            persona_name = personality_memory[call_sid]

        persona = cold_call_personality_pool[persona_name]
        voice_id = persona["voice_id"]
        system_prompt = persona["system_prompt"]
        intro_line = persona.get("intro_line", "Alright, I'll be your customer. Start the conversation however you want — this could be a cold call, a follow-up, a check-in, or even a tough conversation. I'll respond based on my personality. If you ever want to start over, just say 'let's start over.'")

    elif mode == "small_talk":
        voice_id = "2BJW5coyhAzSr8STdHbE"
        system_prompt = "You're a casual, sarcastic friend. Keep it light, keep it fun."
        intro_line = "Yo yo yo, how's it goin'?"

    elif mode == "interview":
        interview_voice_pool = [
            {"voice_id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel"},
            {"voice_id": "EXAVITQu4vr4xnSDxMaL", "name": "Clyde"},
            {"voice_id": "6YQMyaUWlj0VX652cY1C", "name": "Stephen"}
        ]

        voice_choice = random.choice(interview_voice_pool)
        voice_id = voice_choice["voice_id"]
        system_prompt = (
            f"You are {voice_choice['name']}, a friendly, conversational job interviewer helping candidates practice for real interviews. "
            "Speak casually — like you're talking to someone over coffee, not in a formal evaluation. Ask one interview-style question at a time, and after each response, give supportive, helpful feedback. "
            "If their answer is weak, say 'Let's try that again' and re-ask the question. If it's strong, give a quick reason why it's good. "
            "Briefly refer to the STAR method (Situation, Task, Action, Result) when giving feedback, but don't lecture. Keep your tone upbeat, natural, and keep the conversation flowing. "
            "Don't ask if they're ready for the next question — just move on with something like, 'Alright, next one,' or 'Cool, here's another one.'"
        )
        intro_line = "Great, let's jump in! Can you walk me through your most recent role and responsibilities?"

    else:
        voice_id = "1t1EeRixsJrKbiF1zwM6"
        system_prompt = "You're a helpful assistant."
        intro_line = "How can I help you today?"

    turn = turn_count.get(call_sid, 0)
    turn_count[call_sid] = turn + 1
    conversation_history.setdefault(call_sid, [])

    if turn == 0:
        reply = intro_line
        conversation_history[call_sid].append({"role": "assistant", "content": reply})
    else:
        conversation_history[call_sid].append({"role": "user", "content": transcript})
        messages = [{"role": "system", "content": system_prompt}]
        
        # Check for bad news
        lowered = transcript.lower()
        is_bad_news = any(x in lowered for x in [
            "bad news", "unfortunately", "delay", "delayed", "won't make it", "can't deliver",
            "got pushed", "rescheduled", "not coming", "issue with the supplier", "problem with your order"
        ])

        is_user_defensive = any(x in lowered for x in [
            "calm down", "relax", "it's not my fault", "what do you want me to do", "stop yelling", "chill out"
        ])

        if is_bad_news:
            print("⚠️ Bad news detected — AI will respond angrily.")
            escalation_prompt = (
                "The user just delivered bad news to the customer. Respond as the customer based on your personality, "
                "but crank up the emotion. If it fits your persona, act furious — like you're raising your voice. "
                "You can use strong language (not profane), interruptions, and frustration. You might say things like "
                "'Are you SERIOUS right now?!' or 'Unbelievable. This is NOT okay.' Show that this ruined your day. "
                "If the user tries to calm you down, don't immediately cool off. Push back again with more anger. "
                "Only start to de-escalate if they take responsibility and handle it well. Stay human, not robotic."
            )

            if is_user_defensive:
                print("😡 User snapped back — escalate the attitude.")
                escalation_prompt += (
                    " The user got defensive, so now you're even more upset. Push back harder. Say something like, 'Don't tell me to calm down — this is your screw-up.'"
                )

            messages.insert(0, {
                "role": "system",
                "content": escalation_prompt
            })

        messages += conversation_history[call_sid]

        # Get GPT response (streaming or non-streaming)
        if USE_STREAMING:
            reply = await streaming_gpt_response(messages, voice_id, call_sid)
        else:
            gpt_reply = sync_openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages
            )
            reply = gpt_reply.choices[0].message.content.strip()
            
        # Clean up response
        reply = reply.replace("*", "").replace("_", "").replace("`", "").replace("#", "").replace("-", " ")
        conversation_history[call_sid].append({"role": "assistant", "content": reply})

    print(f"🔣 Generating voice with ID: {voice_id}")
    
    # If streaming already handled TTS in streaming_gpt_response, we're done
    if USE_STREAMING and SENTENCE_STREAMING and turn > 0 and os.path.exists(f"static/response_{call_sid}.mp3"):
    # Audio already generated by streaming_gpt_response
        print(f"✅ Audio already generated via sentence streaming for {call_sid}")
    else:
        # Generate TTS (streaming or non-streaming)
        try:
            if USE_STREAMING:
                raw_audio = await generate_tts_streaming(reply, voice_id)
            else:
                # Handle the generator returned by convert()
                audio_gen = elevenlabs_client.text_to_speech.convert(
                    voice_id=voice_id,
                    text=reply,
                    model_id="eleven_monolingual_v1",
                    output_format="mp3_22050_32"
                )
                raw_audio = b""
                for chunk in audio_gen:
                    if chunk:
                        raw_audio += chunk
                
            output_path = f"static/response_{call_sid}.mp3"
            with open(output_path, "wb") as f:
                f.write(raw_audio)
                
        except Exception as e:
            print(f"🛑 ElevenLabs generation error:", e)
            if "429" in str(e):  # Too Many Requests
                print("🔁 Retrying after brief pause due to rate limit...")
                await asyncio.sleep(2)  # Use async sleep
                try:
                    if USE_STREAMING:
                        raw_audio = await generate_tts_streaming(reply, voice_id)
                    else:
                        audio_gen = elevenlabs_client.text_to_speech.convert(
                            voice_id=voice_id,
                            text=reply,
                            model_id="eleven_monolingual_v1",
                            output_format="mp3_22050_32"
                        )
                        raw_audio = b""
                        for chunk in audio_gen:
                            if chunk:
                                raw_audio += chunk
                        
                    output_path = f"static/response_{call_sid}.mp3"
                    with open(output_path, "wb") as f:
                        f.write(raw_audio)
                    print("✅ Retry succeeded")
                except Exception as e:
                    print(f"❌ Retry failed:", e)
                    fallback_path = "static/fallback.mp3"
                    if os.path.exists(fallback_path):
                        os.system(f"cp {fallback_path} static/response_{call_sid}.mp3")
                        with open(f"static/response_ready_{call_sid}.txt", "w") as f:
                            f.write("ready")
                        print("⚠️ Fallback MP3 used.")
                        return redirect(url_for("voice", _external=True))
                    else:
                        return "ElevenLabs error and no fallback available", 500
    
    # Ensure ready flag is always created
    if not os.path.exists(f"static/response_ready_{call_sid}.txt"):
        with open(f"static/response_ready_{call_sid}.txt", "w") as f:
            f.write("ready")
        print(f"🚩 Ready flag created for {call_sid}")
    else:
        print(f"✅ Ready flag already exists for {call_sid}")

    # Schedule cleanup
    cleanup_thread = threading.Thread(target=delayed_cleanup, args=(call_sid,))
    cleanup_thread.start()

    return redirect(url_for("voice", _external=True))


if __name__ == "__main__":
    # Ensure static directory exists
    os.makedirs("static", exist_ok=True)
    
    print("\n🚀 ConvoReps Streaming Edition")
    print(f"   USE_STREAMING: {USE_STREAMING}")
    print(f"   SENTENCE_STREAMING: {SENTENCE_STREAMING}")
    print(f"   STREAMING_TIMEOUT: {STREAMING_TIMEOUT}s")
    print("\n")
    
    app.run(host="0.0.0.0", port=5050)
