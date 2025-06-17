"""
ConvoReps WebSocket Edition
Real-time voice conversation practice using Twilio Media Streams

Features:
- Bidirectional WebSocket streaming for ultra-low latency
- Multiple conversation modes (cold calling, interviews, small talk)
- Dynamic AI personalities with different voices
- Real-time speech processing and interruption handling
- GPT-4.1-nano for natural conversations
- ElevenLabs streaming TTS

Author: ConvoReps Team
Version: 2.0 (WebSocket)
"""

import sys
if sys.version_info < (3, 8):
    print("❌ Python 3.8+ required")
    sys.exit(1)

import os
import io
import json
import base64
import random
import time
import threading
import asyncio
import re
import gc
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import numpy as np

from flask import Flask, request, Response, session, send_from_directory
from flask_cors import CORS
from flask_sock import Sock
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from dotenv import load_dotenv
from pydub import AudioSegment

# Try to import audioop, use fallback if not available
try:
    import audioop
except ImportError:
    print("⚠️ audioop not available, using pydub for audio conversion")
    audioop = None

from openai import OpenAI, AsyncOpenAI
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your-secret-key-here")
CORS(app)

# Initialize WebSocket support
sock = Sock(app)

# Configure WebSocket settings (removed incompatible options for flask-sock 0.7.0)

# Initialize API clients
sync_openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
async_openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
elevenlabs_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

# Thread pool for async operations - reduced for memory
executor = ThreadPoolExecutor(max_workers=3)

# Global state management
active_streams = {}  # Active WebSocket connections
turn_count = {}
mode_lock = {}
conversation_history = {}
personality_memory = {}
interview_question_index = {}

# Voice profiles (preserved from original)
personality_profiles = {
    "rude/skeptical": {"voice_id": "1t1EeRixsJrKbiF1zwM6"},
    "super busy": {"voice_id": "6YQMyaUWlj0VX652cY1C"},
    "small_talk": {"voice_id": "2BJW5coyhAzSr8STdHbE"}
}

cold_call_personality_pool = {
    "Jerry": {
        "voice_id": "1t1EeRixsJrKbiF1zwM6",
        "system_prompt": "You're Jerry, a skeptical small business owner. Be direct but not rude. Stay in character. Keep responses SHORT - 1-2 sentences max."
    },
    "Miranda": {
        "voice_id": "Ax1GP2W4XTyAyNHuch7v",
        "system_prompt": "You're Miranda, a busy office manager. No time for fluff. Be grounded and real. Keep responses SHORT - 1-2 sentences max."
    },
    "Brett": {
        "voice_id": "7eFTSJ6WtWd9VCU4ZlI1",
        "system_prompt": "You're Brett, a contractor answering mid-job. Busy and a bit annoyed. Talk rough, fast, casual. Keep responses SHORT."
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

# Voice Activity Detection for better speech segmentation
class StreamingSpeechProcessor:
    def __init__(self, call_sid):
        self.call_sid = call_sid
        self.audio_buffer = b''
        self.silence_threshold = 1.2  # Reduced from 1.5 seconds
        self.last_speech_time = time.time()
        self.min_speech_length = 0.3  # minimum speech length
        self.silence_start_time = None
        self.speech_detected = False
        self.max_buffer_size = 40000  # 5 seconds max buffer
        
    def add_audio(self, audio_chunk):
        """Add audio chunk to buffer"""
        # Limit buffer size to prevent memory issues
        if len(self.audio_buffer) > self.max_buffer_size:
            # Force process if buffer is too large
            complete_audio = self.audio_buffer
            self.audio_buffer = b''
            self.clear_state()
            return complete_audio
            
        self.audio_buffer += audio_chunk
        
        # Check if we have enough audio for processing
        if len(self.audio_buffer) >= 160:  # At least 20ms of audio
            # Simple VAD based on audio energy
            if self.detect_speech_end():
                complete_audio = self.audio_buffer
                self.audio_buffer = b''
                self.clear_state()
                return complete_audio
        return None
    
    def clear_state(self):
        """Clear internal state to free memory"""
        self.silence_start_time = None
        self.speech_detected = False
        gc.collect()  # Force garbage collection
        
    def detect_speech_end(self):
        """Improved silence detection"""
        if len(self.audio_buffer) < 2400:  # Less than 300ms (min speech length)
            return False
            
        # Check last 400ms for silence (reduced from 800ms)
        check_size = min(3200, len(self.audio_buffer))
        last_chunk = self.audio_buffer[-check_size:]
        
        # Calculate RMS (root mean square) for silence detection
        try:
            if audioop:
                rms = audioop.rms(last_chunk, 1)
            else:
                # Manual RMS calculation
                audio_array = np.frombuffer(last_chunk, dtype=np.uint8)
                rms = np.sqrt(np.mean(audio_array**2))
        except:
            # If error, process what we have
            return True
        
        # Speech detection
        if rms > 800:  # Speech detected
            self.speech_detected = True
            self.silence_start_time = None
            self.last_speech_time = time.time()
        elif self.speech_detected and rms < 500:  # Silence after speech
            if self.silence_start_time is None:
                self.silence_start_time = time.time()
            elif time.time() - self.silence_start_time > self.silence_threshold:
                # Enough silence detected after speech
                return True
        
        # Timeout protection - if buffer is getting large, process it
        if len(self.audio_buffer) > 24000:  # 3 seconds of audio
            return True
            
        return False

def error_handler(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            print(f"💥 Route error in {f.__name__}: {e}")
            response = VoiceResponse()
            response.say("I'm sorry, I encountered an error. Please try again.")
            response.hangup()
            return str(response)
    return wrapper

@app.route("/voice", methods=["POST", "GET"])
@error_handler
def voice():
    """Initial voice webhook - start bidirectional stream"""
    call_sid = request.values.get("CallSid")
    
    print(f"📞 New call: {call_sid}")
    
    # Initialize turn count
    if call_sid not in turn_count:
        turn_count[call_sid] = 0
        print(f"🔢 Initialized turn count for {call_sid}")
    
    response = VoiceResponse()
    
    # Check if first time caller (only play greeting on very first turn)
    if turn_count[call_sid] == 0 and not session.get("has_called_before"):
        session["has_called_before"] = True
        greeting_file = "first_time_greeting.mp3"
        print("👋 New caller detected — playing first-time greeting.")
    elif turn_count[call_sid] == 0 and session.get("has_called_before"):
        greeting_file = "returning_user_greeting.mp3"
        print("🔁 Returning caller — playing returning greeting.")
    else:
        greeting_file = None
    
    # Play greeting if applicable
    if greeting_file and os.path.exists(f"static/{greeting_file}"):
        response.play(f"{request.url_root}static/{greeting_file}")
        response.play(f"{request.url_root}static/beep.mp3")
        response.pause(length=1)
    
    # Start bidirectional stream
    connect = Connect()
    
    # Build WebSocket URL properly
    if request.is_secure:
        ws_scheme = "wss"
    else:
        ws_scheme = "ws"
    
    # Handle potential proxy headers
    host = request.headers.get('X-Forwarded-Host', request.host)
    
    stream_url = f"{ws_scheme}://{host}/media-stream"
    print(f"🔗 Stream URL: {stream_url}")
    
    stream = Stream(
        url=stream_url,
        name="convoreps_stream"
    )
    connect.append(stream)
    response.append(connect)
    
    # Add fallback pause
    response.pause(length=300)  # 5 minute max call
    
    return str(response)

@sock.route('/media-stream')
def media_stream(ws):
    """Handle bidirectional media stream WebSocket connection"""
    stream_sid = None
    call_sid = None
    speech_processor = None
    last_keepalive = time.time()
    
    try:
        while True:
            # Set timeout for receive to handle keepalive
            try:
                message = ws.receive(timeout=1.0)
            except:
                # Check if we need to send keepalive
                if time.time() - last_keepalive > 15:
                    ws.send(json.dumps({"event": "keepalive"}))
                    last_keepalive = time.time()
                continue
                
            if message is None:
                break
                
            data = json.loads(message)
            event_type = data.get('event')
            
            if event_type == 'connected':
                print(f"✅ WebSocket connected: {data.get('protocol')}")
                
            elif event_type == 'start':
                stream_sid = data['streamSid']
                call_sid = data['start']['callSid']
                
                # Initialize stream state
                active_streams[call_sid] = {
                    'ws': ws,
                    'stream_sid': stream_sid,
                    'processing': False,
                    'speech_processor': StreamingSpeechProcessor(call_sid),
                    'last_activity': time.time(),
                    'mark_received': set(),
                    'is_speaking': False,
                    'last_response_time': 0
                }
                
                speech_processor = active_streams[call_sid]['speech_processor']
                
                print(f"🎤 Stream started - CallSid: {call_sid}, StreamSid: {stream_sid}")
                
                # Don't send initial response - wait for user to speak first
                
            elif event_type == 'media':
                if call_sid and call_sid in active_streams:
                    # Update last activity
                    active_streams[call_sid]['last_activity'] = time.time()
                    
                    # Skip processing if bot is speaking
                    if active_streams[call_sid].get('is_speaking', False):
                        continue
                    
                    # Decode the audio chunk
                    audio_chunk = base64.b64decode(data['media']['payload'])
                    
                    # Add to speech processor
                    complete_audio = speech_processor.add_audio(audio_chunk)
                    
                    if complete_audio and not active_streams[call_sid]['processing']:
                        # Prevent rapid-fire responses
                        current_time = time.time()
                        if current_time - active_streams[call_sid].get('last_response_time', 0) < 1.0:
                            continue
                            
                        # Process complete utterance in background
                        active_streams[call_sid]['last_response_time'] = current_time
                        executor.submit(run_async_task, 
                            process_complete_utterance(call_sid, stream_sid, complete_audio))
                        
            elif event_type == 'mark':
                # Track mark events for audio playback completion
                if call_sid in active_streams:
                    mark_name = data['mark'].get('name')
                    active_streams[call_sid]['mark_received'].add(mark_name)
                    print(f"✓ Mark received: {mark_name}")
                    
                    # Check if all marks received for current response
                    if mark_name and mark_name.startswith('sentence_'):
                        # Bot finished speaking this sentence
                        active_streams[call_sid]['is_speaking'] = False
                    
            elif event_type == 'stop':
                print(f"🛑 Stream stopped - CallSid: {call_sid}")
                break
                
    except Exception as e:
        print(f"💥 WebSocket error: {e}")
        print(f"   Error type: {type(e).__name__}")
        print(f"   Call SID: {call_sid}")
        print(f"   Stream SID: {stream_sid}")
        import traceback
        traceback.print_exc()
    finally:
        # Cleanup with aggressive memory freeing
        if call_sid:
            if call_sid in active_streams:
                del active_streams[call_sid]
            if call_sid in turn_count:
                del turn_count[call_sid]
            if call_sid in conversation_history:
                del conversation_history[call_sid]
            if call_sid in personality_memory:
                del personality_memory[call_sid]
            if call_sid in mode_lock:
                del mode_lock[call_sid]
        
        # Force garbage collection
        gc.collect()

# Helper function to run async tasks
def run_async_task(coro):
    """Run async coroutine in new event loop"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

async def send_initial_response(call_sid, stream_sid):
    """Send initial response based on mode - REMOVED to prevent auto-talking"""
    pass  # Don't send anything initially

async def process_complete_utterance(call_sid, stream_sid, audio_data):
    """Process a complete speech utterance"""
    if call_sid not in active_streams:
        return
        
    # Mark as processing to prevent overlaps
    active_streams[call_sid]['processing'] = True
    
    try:
        # Convert mulaw to PCM for transcription
        audio_pcm = convert_mulaw_to_pcm(audio_data)
        
        # Clear original audio data
        del audio_data
        gc.collect()
        
        # Transcribe
        transcript = await transcribe_audio(audio_pcm)
        
        # Clear PCM data
        del audio_pcm
        gc.collect()
        
        if transcript and len(transcript.strip()) > 1:  # Ignore single character transcripts
            print(f"📝 Transcript: {transcript}")
            
            # Filter out noise/spam transcripts
            if any(spam in transcript.lower() for spam in [
                "beadaholique.com", "go to", ".com", "www.", "http",
                "woof woof", "bark bark", "meow", "click here"
            ]):
                print("🚫 Ignoring spam/noise transcript")
                return
            
            # Check for reset command
            if "let's start over" in transcript.lower():
                await handle_reset(call_sid, stream_sid)
                return
                
            # Mark bot as speaking before generating response
            active_streams[call_sid]['is_speaking'] = True
                
            # Process and generate response
            response = await generate_response(call_sid, transcript)
            
            # Stream response back
            await stream_tts_response(call_sid, stream_sid, response)
            
    except Exception as e:
        print(f"💥 Processing error: {e}")
    finally:
        if call_sid in active_streams:
            active_streams[call_sid]['processing'] = False
            # Will be set to False when marks are received
        gc.collect()  # Clean up after processing

async def handle_reset(call_sid, stream_sid):
    """Handle reset command"""
    print("🔁 Reset triggered by user")
    
    # Clear conversation state
    conversation_history.pop(call_sid, None)
    personality_memory.pop(call_sid, None)
    mode_lock.pop(call_sid, None)
    turn_count[call_sid] = 0
    
    # Send reset confirmation
    reset_message = "Alright, let's start fresh. What would you like to practice?"
    await stream_tts_response(call_sid, stream_sid, reset_message)

async def generate_response(call_sid, transcript):
    """Generate AI response based on transcript"""
    # Update turn count
    turn = turn_count.get(call_sid, 0)
    turn_count[call_sid] = turn + 1
    
    # Detect intent if not locked
    mode = mode_lock.get(call_sid)
    if not mode:
        mode = detect_intent(transcript)
        mode_lock[call_sid] = mode
    
    print(f"🧐 Mode: {mode}, Turn: {turn}")
    
    # Initialize conversation history
    if call_sid not in conversation_history:
        conversation_history[call_sid] = []
        
    # Add user message
    conversation_history[call_sid].append({
        "role": "user",
        "content": transcript
    })
    
    # Limit conversation history to last 5 messages to prevent memory overflow
    if len(conversation_history[call_sid]) > 5:
        conversation_history[call_sid] = conversation_history[call_sid][-5:]
    
    # Get personality and prompt
    voice_id, system_prompt, intro_line = get_personality_for_mode(call_sid, mode)
    
    # Build messages for GPT
    messages = [{"role": "system", "content": system_prompt}]
    
    # Check for bad news
    if detect_bad_news(transcript):
        messages = add_bad_news_context(messages, transcript)
    
    # Add conversation history
    messages.extend(conversation_history[call_sid])
    
    # Generate response
    try:
        # Use GPT-4.1-nano for conversation
        completion = await async_openai.chat.completions.create(
            model="gpt-4.1-nano",
            messages=messages,
            temperature=0.7,
            max_tokens=40,  # Reduced from 60 to keep responses shorter and save memory
            stream=True
        )
        
        # Collect streamed response
        full_response = ""
        async for chunk in completion:
            if chunk.choices[0].delta.content:
                full_response += chunk.choices[0].delta.content
                
        # Clean response
        reply = clean_response_text(full_response)
        
        # Further truncate if too long
        sentences = re.split(r'(?<=[.!?])\s+', reply)
        if len(sentences) > 2:
            reply = ' '.join(sentences[:2])
        
        # Store in history
        conversation_history[call_sid].append({
            "role": "assistant",
            "content": reply
        })
        
        return reply
        
    except Exception as e:
        print(f"💥 GPT error: {e}")
        return "I didn't catch that. Could you repeat?"

def detect_intent(text):
    """Detect conversation intent"""
    lowered = text.lower()
    if any(phrase in lowered for phrase in ["cold call", "customer call", "sales call", "business call"]):
        return "cold_call"
    elif any(phrase in lowered for phrase in ["interview", "interview prep"]):
        return "interview"
    elif any(phrase in lowered for phrase in ["small talk", "chat", "talk casually"]):
        return "small_talk"
    return "cold_call"  # Default

def detect_bad_news(text):
    """Detect if message contains bad news"""
    lowered = text.lower()
    return any(phrase in lowered for phrase in [
        "bad news", "unfortunately", "problem", "delay", "issue",
        "we can't", "we won't", "not going to happen", "reschedule", 
        "price increase", "delayed", "won't make it", "can't deliver"
    ])

def add_bad_news_context(messages, transcript):
    """Add context for bad news response"""
    escalation_prompt = (
        "The user just delivered bad news to the customer. Respond as the customer based on your personality, "
        "but show reasonable frustration. Keep response SHORT - 1-2 sentences max. Don't overreact."
    )
    
    if any(x in transcript.lower() for x in ["calm down", "relax", "it's not my fault"]):
        escalation_prompt += " The user got defensive, so you're more upset but still brief."
        
    messages.insert(0, {"role": "system", "content": escalation_prompt})
    return messages

def get_personality_for_mode(call_sid, mode):
    """Get personality configuration for mode"""
    if mode == "cold_call":
        if call_sid not in personality_memory:
            persona_name = random.choice(list(cold_call_personality_pool.keys()))
            personality_memory[call_sid] = persona_name
        else:
            persona_name = personality_memory[call_sid]
            
        persona = cold_call_personality_pool[persona_name]
        return (
            persona["voice_id"],
            persona["system_prompt"],
            "Hello?"  # Simple greeting instead of long intro
        )
        
    elif mode == "interview":
        voice_pool = [
            {"voice_id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel"},
            {"voice_id": "EXAVITQu4vr4xnSDxMaL", "name": "Clyde"},
            {"voice_id": "6YQMyaUWlj0VX652cY1C", "name": "Stephen"}
        ]
        
        if call_sid not in personality_memory:
            voice_choice = random.choice(voice_pool)
            personality_memory[call_sid] = voice_choice
        else:
            voice_choice = personality_memory[call_sid]
            
        system_prompt = (
            f"You are {voice_choice['name']}, a friendly, conversational job interviewer. "
            "Ask one interview question at a time, give supportive feedback. "
            "Keep your tone upbeat and natural. Keep responses SHORT - 1-2 sentences."
        )
        
        return (
            voice_choice["voice_id"],
            system_prompt,
            "Great, let's start. Tell me about yourself."
        )
        
    else:  # small_talk
        return (
            "2BJW5coyhAzSr8STdHbE",
            "You're a casual, sarcastic friend. Keep it light, keep it fun. SHORT responses only - 1-2 sentences max.",
            "Hey, what's up?"
        )

def clean_response_text(text):
    """Clean response text"""
    return text.replace("*", "").replace("_", "").replace("`", "").replace("#", "").replace("-", " ")

async def stream_tts_response(call_sid, stream_sid, text):
    """Stream TTS audio back through WebSocket"""
    if call_sid not in active_streams:
        return
        
    ws = active_streams[call_sid]['ws']
    
    # Get voice ID
    mode = mode_lock.get(call_sid, "cold_call")
    voice_id, _, _ = get_personality_for_mode(call_sid, mode)
    
    # Split text into sentences for faster streaming
    sentences = re.split(r'(?<=[.!?])\s+', text)
    
    # Limit sentences to prevent over-talking
    sentences = sentences[:2]  # Max 2 sentences per response (reduced from 3)
    
    total_sentences = len([s for s in sentences if s.strip()])
    
    for i, sentence in enumerate(sentences):
        if sentence.strip():
            try:
                print(f"🔊 Generating TTS for: {sentence[:50]}...")
                
                # Use the correct ElevenLabs streaming method
                audio_stream = elevenlabs_client.text_to_speech.stream(
                    text=sentence,
                    voice_id=voice_id,
                    model_id="eleven_turbo_v2_5",
                    voice_settings=VoiceSettings(
                        stability=0.4,
                        similarity_boost=0.75,
                        style=0.0,
                        use_speaker_boost=True
                    ),
                    output_format="ulaw_8000",  # Direct mulaw format for Twilio
                    optimize_streaming_latency=3  # Max optimization for low latency
                )
                
                # Process the audio stream without accumulating large buffers
                chunk_count = 0
                
                # Iterate through the stream
                for audio_chunk in audio_stream:
                    if isinstance(audio_chunk, bytes) and audio_chunk:
                        # Send immediately without buffering
                        if len(audio_chunk) >= 160:
                            # Send in 160-byte chunks
                            for j in range(0, len(audio_chunk), 160):
                                chunk_to_send = audio_chunk[j:j+160]
                                if chunk_to_send:
                                    payload = base64.b64encode(chunk_to_send).decode('utf-8')
                                    media_message = {
                                        "event": "media",
                                        "streamSid": stream_sid,
                                        "media": {
                                            "payload": payload
                                        }
                                    }
                                    ws.send(json.dumps(media_message))
                                    chunk_count += 1
                                    
                                    # Small delay every 5 chunks to prevent overwhelming
                                    if chunk_count % 5 == 0:
                                        await asyncio.sleep(0.001)
                        else:
                            # Send small chunks directly
                            payload = base64.b64encode(audio_chunk).decode('utf-8')
                            media_message = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": payload
                                }
                            }
                            ws.send(json.dumps(media_message))
                
                # Send mark message to track completion
                mark_message = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {
                        "name": f"sentence_{i}"
                    }
                }
                ws.send(json.dumps(mark_message))
                
                # If this is the last sentence, add end marker
                if i == total_sentences - 1:
                    await asyncio.sleep(0.1)
                    end_mark = {
                        "event": "mark",
                        "streamSid": stream_sid,
                        "mark": {
                            "name": "response_end"
                        }
                    }
                    ws.send(json.dumps(end_mark))
                
                print(f"✅ Sent {chunk_count} audio chunks for sentence {i}")
                
                # Clean up after each sentence
                del audio_stream
                gc.collect()
                
            except Exception as e:
                print(f"💥 TTS streaming error: {e}")
                print(f"   Error type: {type(e).__name__}")
                import traceback
                traceback.print_exc()

async def transcribe_audio(audio_pcm):
    """Transcribe audio using Whisper"""
    try:
        # Create temporary file for transcription
        with io.BytesIO() as audio_file:
            # Convert PCM to WAV format with minimal memory usage
            try:
                audio = AudioSegment(
                    data=audio_pcm,
                    sample_width=2,
                    frame_rate=16000,
                    channels=1
                )
                audio.export(audio_file, format="wav")
                audio_file.seek(0)
                audio_file.name = "temp.wav"  # OpenAI needs a filename
                
                # Clear the original audio data
                del audio
                del audio_pcm
                gc.collect()
                
                # Transcribe
                result = await async_openai.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="en"
                )
                
                return result.text.strip()
                
            except Exception as e:
                print(f"💥 Audio processing error: {e}")
                return ""
            
    except Exception as e:
        print(f"💥 Transcription error: {e}")
        return ""

def convert_mulaw_to_pcm(mulaw_data):
    """Convert 8kHz mulaw to 16kHz PCM for Whisper"""
    try:
        if audioop:
            # Use audioop if available
            pcm_data = audioop.ulaw2lin(mulaw_data, 2)
            
            # Create audio segment
            audio = AudioSegment(
                data=pcm_data,
                sample_width=2,
                frame_rate=8000,
                channels=1
            )
            
            # Clear intermediate data
            del pcm_data
        else:
            # Use pydub's built-in conversion
            audio = AudioSegment.from_file(
                io.BytesIO(mulaw_data),
                format="mulaw",
                frame_rate=8000,
                channels=1,
                sample_width=1
            )
            # Convert to PCM
            audio = audio.set_sample_width(2)
        
        # Resample to 16kHz
        audio = audio.set_frame_rate(16000)
        result = audio.raw_data
        
        # Clean up
        del audio
        gc.collect()
        
        return result
    except Exception as e:
        print(f"💥 Audio conversion error: {e}")
        return b''

def convert_to_mulaw(audio_data):
    """Convert audio to 8kHz mulaw for Twilio"""
    try:
        # Assume input is 22050Hz from ElevenLabs
        audio = AudioSegment(
            data=audio_data,
            sample_width=2,
            frame_rate=22050,
            channels=1
        )
        
        # Resample to 8kHz
        audio = audio.set_frame_rate(8000)
        
        if audioop:
            # Convert to mulaw using audioop
            pcm_data = audio.raw_data
            mulaw_data = audioop.lin2ulaw(pcm_data, 2)
        else:
            # Export as mulaw using pydub
            buffer = io.BytesIO()
            audio.export(buffer, format="mulaw")
            mulaw_data = buffer.getvalue()
        
        return mulaw_data
    except Exception as e:
        print(f"💥 Mulaw conversion error: {e}")
        return b''

# Partial speech endpoint (kept for compatibility but not used with bidirectional)
@app.route("/partial_speech", methods=["POST"])
def partial_speech():
    """Legacy endpoint - not used with bidirectional streams"""
    return "", 204

# Process speech endpoint (kept for compatibility)
@app.route("/process_speech", methods=["POST"])
def process_speech():
    """Legacy endpoint - redirect to voice"""
    response = VoiceResponse()
    response.redirect("/voice")
    return str(response)

# Static file serving for greetings
@app.route("/static/<path:filename>")
def static_files(filename):
    """Serve static files"""
    return send_from_directory("static", filename)

# Health check endpoint
@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

# WebSocket test endpoint
@app.route("/ws-test", methods=["GET"])
def ws_test():
    """Test WebSocket support"""
    return {
        "websocket_support": True,
        "active_streams": len(active_streams),
        "flask_sock_version": "0.7.0",
        "ready": True
    }

if __name__ == "__main__":
    # Ensure static directory exists
    os.makedirs("static", exist_ok=True)
    
    # Force initial garbage collection
    gc.collect()
    
    print("\n🚀 ConvoReps WebSocket Streaming Edition (Memory Optimized)")
    print("   ✓ Bidirectional Media Streams enabled")
    print("   ✓ Real-time speech processing")
    print("   ✓ Ultra-low latency streaming")
    print("   ✓ GPT-4.1-nano for conversations")
    print("   ✓ Memory optimized for 512MB limit")
    print("\n")
    
    # Run with Flask's built-in server (flask-sock handles WebSocket upgrade)
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
