import os
import io
import json
import base64
import random
import time
import threading
import asyncio
import re
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import numpy as np

from flask import Flask, request, Response, session
from flask_cors import CORS
from flask_sock import Sock
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from dotenv import load_dotenv
from pydub import AudioSegment
import audioop

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

# Configure WebSocket settings
app.config['SOCK_SERVER_OPTIONS'] = {
    'ping_interval': 25,
    'ping_timeout': 120,
    'max_size': 2**20  # 1MB max message size
}

# Initialize API clients
sync_openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
async_openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
elevenlabs_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

# Thread pool for async operations
executor = ThreadPoolExecutor(max_workers=10)

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

# Voice Activity Detection for better speech segmentation
class StreamingSpeechProcessor:
    def __init__(self, call_sid):
        self.call_sid = call_sid
        self.audio_buffer = b''
        self.silence_threshold = 0.8  # seconds
        self.last_speech_time = time.time()
        self.min_speech_length = 0.5  # minimum speech length in seconds
        
    def add_audio(self, audio_chunk):
        """Add audio chunk to buffer"""
        self.audio_buffer += audio_chunk
        
        # Simple VAD based on audio energy
        if self.detect_speech_end():
            complete_audio = self.audio_buffer
            self.audio_buffer = b''
            return complete_audio
        return None
        
    def detect_speech_end(self):
        """Simple silence detection"""
        if len(self.audio_buffer) < 8000:  # Less than 1 second
            return False
            
        # Check last 400ms for silence
        last_chunk = self.audio_buffer[-3200:]
        rms = audioop.rms(last_chunk, 1)
        
        # If silence detected and enough audio accumulated
        if rms < 500 and len(self.audio_buffer) > 4000:
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
    
    response = VoiceResponse()
    
    # Check if first time caller
    if turn_count[call_sid] == 0 and not session.get("has_called_before"):
        session["has_called_before"] = True
        greeting_file = "first_time_greeting.mp3"
        print("👋 New caller detected — playing first-time greeting.")
    elif turn_count[call_sid] == 0:
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
    stream = Stream(
        url=f"wss://{request.host}/media-stream",
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
    processing_task = None
    
    try:
        while True:
            message = ws.receive()
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
                    'audio_queue': asyncio.Queue(),
                    'mark_received': set()
                }
                
                speech_processor = active_streams[call_sid]['speech_processor']
                
                print(f"🎤 Stream started - CallSid: {call_sid}, StreamSid: {stream_sid}")
                
                # Start async processing task
                processing_task = asyncio.create_task(
                    process_stream_continuously(call_sid, stream_sid)
                )
                
                # Send initial response after beep
                asyncio.create_task(send_initial_response(call_sid, stream_sid))
                
            elif event_type == 'media':
                if call_sid and call_sid in active_streams:
                    # Decode the audio chunk
                    audio_chunk = base64.b64decode(data['media']['payload'])
                    
                    # Add to speech processor
                    complete_audio = speech_processor.add_audio(audio_chunk)
                    
                    if complete_audio:
                        # Process complete utterance
                        asyncio.create_task(
                            process_complete_utterance(call_sid, stream_sid, complete_audio)
                        )
                        
            elif event_type == 'mark':
                # Track mark events for audio playback completion
                if call_sid in active_streams:
                    mark_name = data['mark'].get('name')
                    active_streams[call_sid]['mark_received'].add(mark_name)
                    print(f"✓ Mark received: {mark_name}")
                    
            elif event_type == 'stop':
                print(f"🛑 Stream stopped - CallSid: {call_sid}")
                break
                
    except Exception as e:
        print(f"💥 WebSocket error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Cleanup
        if processing_task:
            processing_task.cancel()
        if call_sid in active_streams:
            del active_streams[call_sid]
        if call_sid in turn_count:
            turn_count[call_sid] = 0

async def send_initial_response(call_sid, stream_sid):
    """Send initial response based on mode"""
    await asyncio.sleep(0.5)  # Brief pause after beep
    
    # Determine initial response
    turn = turn_count.get(call_sid, 0)
    if turn == 0:
        # First turn - send appropriate greeting
        initial_text = get_initial_greeting(call_sid)
        if initial_text:
            await stream_tts_response(call_sid, stream_sid, initial_text)
            # Update conversation history
            if call_sid not in conversation_history:
                conversation_history[call_sid] = []
            conversation_history[call_sid].append({
                "role": "assistant",
                "content": initial_text
            })

async def process_complete_utterance(call_sid, stream_sid, audio_data):
    """Process a complete speech utterance"""
    if call_sid not in active_streams:
        return
        
    # Mark as processing to prevent overlaps
    active_streams[call_sid]['processing'] = True
    
    try:
        # Convert mulaw to PCM for transcription
        audio_pcm = convert_mulaw_to_pcm(audio_data)
        
        # Transcribe
        transcript = await transcribe_audio(audio_pcm)
        
        if transcript and len(transcript.strip()) > 0:
            print(f"📝 Transcript: {transcript}")
            
            # Check for reset command
            if "let's start over" in transcript.lower():
                await handle_reset(call_sid, stream_sid)
                return
                
            # Process and generate response
            response = await generate_response(call_sid, transcript)
            
            # Stream response back
            await stream_tts_response(call_sid, stream_sid, response)
            
    except Exception as e:
        print(f"💥 Processing error: {e}")
    finally:
        if call_sid in active_streams:
            active_streams[call_sid]['processing'] = False

async def process_stream_continuously(call_sid, stream_sid):
    """Continuous processing task for the stream"""
    try:
        while call_sid in active_streams:
            # Check for timeouts or other conditions
            if time.time() - active_streams[call_sid]['last_activity'] > 300:
                print(f"⏱️ Stream timeout for {call_sid}")
                break
                
            await asyncio.sleep(0.1)
            
    except asyncio.CancelledError:
        print(f"🛑 Processing task cancelled for {call_sid}")

def get_initial_greeting(call_sid):
    """Get initial greeting based on mode detection"""
    # This would be called on first interaction
    # For now, return None to let the user speak first
    return None

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
            max_tokens=150,
            stream=True
        )
        
        # Collect streamed response
        full_response = ""
        async for chunk in completion:
            if chunk.choices[0].delta.content:
                full_response += chunk.choices[0].delta.content
                
        # Clean response
        reply = clean_response_text(full_response)
        
        # Store in history
        conversation_history[call_sid].append({
            "role": "assistant",
            "content": reply
        })
        
        return reply
        
    except Exception as e:
        print(f"💥 GPT error: {e}")
        return "I'm having trouble understanding. Could you say that again?"

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
        "but crank up the emotion. If it fits your persona, act furious — like you're raising your voice. "
        "You can use strong language (not profane), interruptions, and frustration. Show that this ruined your day."
    )
    
    if any(x in transcript.lower() for x in ["calm down", "relax", "it's not my fault"]):
        escalation_prompt += " The user got defensive, so now you're even more upset."
        
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
            "Alright, I'll be your customer. Start the conversation however you want."
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
            "Keep your tone upbeat and natural."
        )
        
        return (
            voice_choice["voice_id"],
            system_prompt,
            "Great, let's jump in! Can you walk me through your most recent role?"
        )
        
    else:  # small_talk
        return (
            "2BJW5coyhAzSr8STdHbE",
            "You're a casual, sarcastic friend. Keep it light, keep it fun.",
            "Yo yo yo, how's it goin'?"
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
    
    for i, sentence in enumerate(sentences):
        if sentence.strip():
            try:
                # Generate TTS for sentence
                audio_stream = elevenlabs_client.text_to_speech.convert_as_stream(
                    voice_id=voice_id,
                    text=sentence,
                    model_id="eleven_turbo_v2_5",
                    voice_settings=VoiceSettings(
                        stability=0.4,
                        similarity_boost=0.75
                    )
                )
                
                # Stream audio chunks
                chunk_count = 0
                audio_buffer = b''
                
                for chunk in audio_stream:
                    if chunk:
                        audio_buffer += chunk
                        
                        # Send chunks of ~20ms (160 bytes at 8kHz)
                        if len(audio_buffer) >= 640:
                            # Convert to mulaw
                            mulaw_chunk = convert_to_mulaw(audio_buffer[:640])
                            payload = base64.b64encode(mulaw_chunk).decode('utf-8')
                            
                            # Send media message
                            media_message = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": payload
                                }
                            }
                            ws.send(json.dumps(media_message))
                            
                            audio_buffer = audio_buffer[640:]
                            chunk_count += 1
                            
                            # Small delay to prevent overwhelming
                            if chunk_count % 10 == 0:
                                await asyncio.sleep(0.01)
                
                # Send remaining audio
                if audio_buffer:
                    mulaw_chunk = convert_to_mulaw(audio_buffer)
                    payload = base64.b64encode(mulaw_chunk).decode('utf-8')
                    
                    media_message = {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {
                            "payload": payload
                        }
                    }
                    ws.send(json.dumps(media_message))
                
                # Send mark message
                mark_message = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {
                        "name": f"sentence_{i}"
                    }
                }
                ws.send(json.dumps(mark_message))
                
            except Exception as e:
                print(f"💥 TTS streaming error: {e}")

async def transcribe_audio(audio_pcm):
    """Transcribe audio using Whisper"""
    try:
        # Create temporary file for transcription
        with io.BytesIO() as audio_file:
            # Convert PCM to WAV format
            audio = AudioSegment(
                data=audio_pcm,
                sample_width=2,
                frame_rate=16000,
                channels=1
            )
            audio.export(audio_file, format="wav")
            audio_file.seek(0)
            audio_file.name = "temp.wav"  # OpenAI needs a filename
            
            # Transcribe
            result = await async_openai.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="en"
            )
            
            return result.text.strip()
            
    except Exception as e:
        print(f"💥 Transcription error: {e}")
        return ""

def convert_mulaw_to_pcm(mulaw_data):
    """Convert 8kHz mulaw to 16kHz PCM for Whisper"""
    try:
        # Decode mulaw to linear PCM
        pcm_data = audioop.ulaw2lin(mulaw_data, 2)
        
        # Create audio segment
        audio = AudioSegment(
            data=pcm_data,
            sample_width=2,
            frame_rate=8000,
            channels=1
        )
        
        # Resample to 16kHz
        audio = audio.set_frame_rate(16000)
        
        return audio.raw_data
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
        
        # Convert to mulaw
        pcm_data = audio.raw_data
        mulaw_data = audioop.lin2ulaw(pcm_data, 2)
        
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

if __name__ == "__main__":
    # Ensure static directory exists
    os.makedirs("static", exist_ok=True)
    
    print("\n🚀 ConvoReps WebSocket Streaming Edition")
    print("   ✓ Bidirectional Media Streams enabled")
    print("   ✓ Real-time speech processing")
    print("   ✓ Ultra-low latency streaming")
    print("   ✓ GPT-4.1-nano for conversations")
    print("\n")
    
    # Run with production WSGI server for better WebSocket support
    try:
        from gevent import pywsgi
        from geventwebsocket.handler import WebSocketHandler
        server = pywsgi.WSGIServer(('0.0.0.0', 5050), app, handler_class=WebSocketHandler)
        print("🟢 Starting server on port 5050 with gevent...")
        server.serve_forever()
    except ImportError:
        # Fallback to Flask development server
        print("⚠️  Running in development mode. Install gevent for production.")
        app.run(host="0.0.0.0", port=5050, debug=True)
