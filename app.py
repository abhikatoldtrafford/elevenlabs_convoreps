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
from twilio.request_validator import RequestValidator
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
def periodic_cleanup():
    """Clean up stale connections periodically"""
    while True:
        time.sleep(60)  # Run every minute
        current_time = time.time()
        stale_calls = []
        
        for call_sid, stream_data in active_streams.items():
            # Remove connections idle for more than 5 minutes
            if current_time - stream_data.get('last_activity', 0) > 300:
                stale_calls.append(call_sid)
        
        for call_sid in stale_calls:
            print(f"🧹 Cleaning up stale connection: {call_sid}")
            cleanup_call_resources(call_sid)

# Start cleanup thread - ADD this at the bottom before if __name__ == "__main__":
cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
cleanup_thread.start()
# Voice Activity Detection for better speech segmentation
class StreamingSpeechProcessor:
    def __init__(self, call_sid):
        self.call_sid = call_sid
        self.audio_buffer = bytearray()  # Use bytearray for efficiency
        self.silence_threshold = 1.0  # Reduced from 1.2
        self.last_speech_time = time.time()
        self.min_speech_length = 0.3
        self.silence_start_time = None
        self.speech_detected = False
        self.max_buffer_size = 24000  # Reduced from 40000 (3 seconds)
        self.energy_threshold = 500  # Configurable threshold
        
    def add_audio(self, audio_chunk):
        """Add audio chunk to buffer with improved memory management"""
        # Immediately process if buffer is getting too large
        if len(self.audio_buffer) + len(audio_chunk) > self.max_buffer_size:
            complete_audio = bytes(self.audio_buffer)
            self.audio_buffer.clear()
            return complete_audio
            
        self.audio_buffer.extend(audio_chunk)
        
        # Need minimum audio for detection
        if len(self.audio_buffer) >= 2400:  # 300ms
            if self.detect_speech_end():
                complete_audio = bytes(self.audio_buffer)
                self.audio_buffer.clear()
                return complete_audio
        return None
    
    def detect_speech_end(self):
        """Improved silence detection with proper cleanup"""
        if len(self.audio_buffer) < 2400:
            return False
            
        # Check last 400ms for silence
        check_size = min(3200, len(self.audio_buffer))
        last_chunk = self.audio_buffer[-check_size:]
        
        try:
            # Calculate RMS more efficiently
            if audioop:
                rms = audioop.rms(bytes(last_chunk), 1)
            else:
                # Faster numpy calculation
                audio_array = np.frombuffer(last_chunk, dtype=np.uint8)
                rms = np.sqrt(np.mean(np.square(audio_array.astype(np.float32))))
        except Exception:
            return True  # Process on error
        
        # Speech detection logic
        if rms > self.energy_threshold:
            self.speech_detected = True
            self.silence_start_time = None
            self.last_speech_time = time.time()
        elif self.speech_detected and rms < (self.energy_threshold * 0.6):
            if self.silence_start_time is None:
                self.silence_start_time = time.time()
            elif time.time() - self.silence_start_time > self.silence_threshold:
                return True
        
        # Timeout protection
        if time.time() - self.last_speech_time > 5.0:  # 5 second timeout
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
    # ADD: Validate Twilio signature
    validator = RequestValidator(os.getenv("TWILIO_AUTH_TOKEN"))
    url = request.url
    params = request.values
    signature = request.headers.get('X-Twilio-Signature', '')
    
    if not validator.validate(url, params, signature):
        response = VoiceResponse()
        response.reject()
        return str(response), 403
    
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
    processing_lock = threading.Lock()
    
    try:
        while True:
            try:
                message = ws.receive(timeout=1.0)
            except Exception:
                # Send keepalive every 15 seconds
                if time.time() - last_keepalive > 15:
                    try:
                        ws.send(json.dumps({"event": "keepalive"}))
                        last_keepalive = time.time()
                    except:
                        break  # Connection lost
                continue
                
            if message is None:
                break
                
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print("Invalid JSON received")
                continue
                
            event_type = data.get('event')
            
            if event_type == 'connected':
                print(f"✅ WebSocket connected: {data.get('protocol')}")
                
            elif event_type == 'start':
                stream_sid = data['streamSid']
                call_sid = data['start']['callSid']
                
                # Initialize with thread-safe lock
                with processing_lock:
                    active_streams[call_sid] = {
                        'ws': ws,
                        'stream_sid': stream_sid,
                        'processing': False,
                        'speech_processor': StreamingSpeechProcessor(call_sid),
                        'last_activity': time.time(),
                        'mark_received': set(),
                        'is_speaking': False,
                        'last_response_time': 0,
                        'lock': processing_lock
                    }
                
                speech_processor = active_streams[call_sid]['speech_processor']
                print(f"🎤 Stream started - CallSid: {call_sid}, StreamSid: {stream_sid}")
                
            elif event_type == 'media' and call_sid and call_sid in active_streams:
                # Update activity timestamp
                active_streams[call_sid]['last_activity'] = time.time()
                
                # Skip if bot is speaking or already processing
                if active_streams[call_sid].get('is_speaking', False):
                    continue
                
                # Decode audio
                try:
                    audio_chunk = base64.b64decode(data['media']['payload'])
                except Exception as e:
                    print(f"Failed to decode audio: {e}")
                    continue
                
                # Add to processor
                complete_audio = speech_processor.add_audio(audio_chunk)
                
                if complete_audio:
                    with processing_lock:
                        if not active_streams[call_sid]['processing']:
                            # Rate limiting
                            current_time = time.time()
                            last_response = active_streams[call_sid].get('last_response_time', 0)
                            if current_time - last_response < 0.5:  # 500ms minimum gap
                                continue
                                
                            active_streams[call_sid]['processing'] = True
                            active_streams[call_sid]['last_response_time'] = current_time
                            
                            # Process in background
                            executor.submit(
                                run_async_task,
                                process_complete_utterance(call_sid, stream_sid, complete_audio)
                            )
                        
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
                    elif mark_name == 'response_end':
                        # Full response completed
                        active_streams[call_sid]['is_speaking'] = False
                        active_streams[call_sid]['processing'] = False
                    
            elif event_type == 'dtmf':
                # Handle DTMF (touch-tone) events
                if call_sid in active_streams:
                    digit = data.get('dtmf', {}).get('digit')
                    print(f"📞 DTMF received: {digit}")
                    # You can add DTMF handling logic here
                    
            elif event_type == 'stop':
                print(f"🛑 Stream stopped - CallSid: {call_sid}")
                break
                
    except Exception as e:
        print(f"💥 WebSocket error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Comprehensive cleanup
        if call_sid:
            cleanup_call_resources(call_sid)
            
def cleanup_call_resources(call_sid):
    """Centralized cleanup function"""
    if call_sid in active_streams:
        # Close WebSocket if still open
        ws = active_streams[call_sid].get('ws')
        if ws:
            try:
                ws.close()
            except:
                pass
                
        # Remove from all dictionaries
        active_streams.pop(call_sid, None)
        turn_count.pop(call_sid, None)
        conversation_history.pop(call_sid, None)
        personality_memory.pop(call_sid, None)
        mode_lock.pop(call_sid, None)
        interview_question_index.pop(call_sid, None)
    
    # Force garbage collection only after cleanup
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
    """Process a complete speech utterance with better error handling"""
    if call_sid not in active_streams:
        return
        
    try:
        # Mark as speaking immediately
        active_streams[call_sid]['is_speaking'] = True
        
        # Convert mulaw to PCM more efficiently
        audio_pcm = convert_mulaw_to_pcm(audio_data)
        
        # Free original audio data immediately
        audio_data = None
        
        # Transcribe with timeout
        transcript = await asyncio.wait_for(
            transcribe_audio(audio_pcm),
            timeout=5.0  # 5 second timeout
        )
        
        # Free PCM data
        audio_pcm = None
        
        if transcript and len(transcript.strip()) > 1:
            print(f"📝 Transcript: {transcript}")
            
            # Spam filter
            spam_patterns = [
                "beadaholique.com", "go to", ".com", "www.", "http",
                "woof woof", "bark bark", "meow", "click here"
            ]
            if any(spam in transcript.lower() for spam in spam_patterns):
                print("🚫 Ignoring spam/noise transcript")
                return
            
            # Check for reset
            if "let's start over" in transcript.lower():
                await handle_reset(call_sid, stream_sid)
                return
                
            # Generate and stream response
            response = await generate_response(call_sid, transcript)
            await stream_tts_response(call_sid, stream_sid, response)
            
    except asyncio.TimeoutError:
        print(f"⏱️ Processing timeout for call {call_sid}")
    except Exception as e:
        print(f"💥 Processing error: {e}")
    finally:
        # Always reset processing flag
        if call_sid in active_streams:
            active_streams[call_sid]['processing'] = False

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
    
    # Limit conversation history (KEEP EXISTING LIMITING LOGIC)
    if len(conversation_history[call_sid]) > 6:
        conversation_history[call_sid] = conversation_history[call_sid][-6:]
    
    # Get personality and prompt
    voice_id, system_prompt, intro_line = get_personality_for_mode(call_sid, mode)
    
    # ADD INTERVIEW QUESTION LOGIC HERE - AFTER getting system_prompt:
    if mode == "interview":
        if call_sid not in interview_question_index:
            interview_question_index[call_sid] = 0
        
        current_question_idx = interview_question_index[call_sid]
        if current_question_idx < len(interview_questions):
            # Include current question in system prompt
            system_prompt += f"\n\nAsk this question next: {interview_questions[current_question_idx]}"
    
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
            max_tokens=40,
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
        
        # INCREMENT INTERVIEW QUESTION INDEX HERE - AFTER response is stored:
        if mode == "interview":
            interview_question_index[call_sid] = (interview_question_index[call_sid] + 1) % len(interview_questions)
        
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
    
    # Limit sentences to prevent over-talking (reduced from 3)
    sentences = sentences[:2]  # Max 2 sentences per response
    
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
    
    # Note: is_speaking flag will be set to False when mark events are received
def convert_mulaw_to_pcm(mulaw_data):
    """Convert 8kHz mulaw to 16kHz PCM for Whisper - optimized"""
    try:
        if not mulaw_data:
            return b''
            
        if audioop:
            # Direct conversion with audioop
            pcm_8khz = audioop.ulaw2lin(mulaw_data, 2)
            # Resample using audioop (more efficient)
            pcm_16khz = audioop.ratecv(
                pcm_8khz, 2, 1, 8000, 16000, None
            )[0]
            return pcm_16khz
        else:
            # Fallback to pydub with proper format specification
            # Create BytesIO object for mulaw data
            mulaw_buffer = io.BytesIO(mulaw_data)
            
            # Load as raw mulaw format
            audio = AudioSegment.from_raw(
                mulaw_buffer,
                sample_width=1,  # mulaw is 8-bit
                frame_rate=8000,
                channels=1,
                frame_width=1
            )
            
            # Convert to 16-bit PCM and resample to 16kHz
            audio = audio.set_sample_width(2).set_frame_rate(16000)
            return audio.raw_data
            
    except Exception as e:
        print(f"💥 Audio conversion error: {e}")
        return b''

async def transcribe_audio(audio_pcm):
    """Transcribe audio using Whisper - memory optimized"""
    if not audio_pcm or len(audio_pcm) < 1600:  # Min 100ms of audio
        return ""
        
    try:
        # Use io.BytesIO more efficiently
        audio_buffer = io.BytesIO()
        
        # Create WAV with minimal overhead
        audio = AudioSegment(
            data=audio_pcm,
            sample_width=2,
            frame_rate=16000,
            channels=1
        )
        
        # Export directly to buffer
        audio.export(audio_buffer, format="wav", parameters=["-ac", "1"])
        audio_buffer.seek(0)
        audio_buffer.name = "audio.wav"
        
        # Free audio segment
        del audio
        
        # Transcribe with smaller model for speed
        result = await async_openai.audio.transcriptions.create(
            model="whisper-1",
            file=audio_buffer,
            language="en",
            response_format="text"  # Simpler format
        )
        
        return result.strip()
        
    except Exception as e:
        print(f"💥 Transcription error: {e}")
        return ""
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
