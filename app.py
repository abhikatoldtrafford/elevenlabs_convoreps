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

# Load environment variables ASAP
load_dotenv()

# Suppress excessive werkzeug logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Initialize Flask app
app = Flask(__name__)

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
SENTENCE_STREAMING = False

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
    time.sleep(120)  # Let Twilio play it first
    try:
        os.remove(f"static/response_{call_sid}.mp3")
        os.remove(f"static/response_ready_{call_sid}.txt")
        print(f"🧹 Cleaned up response files for {call_sid}")
    except Exception as e:
        print(f"⚠️ Cleanup error for {call_sid}: {e}")

@app.route("/voice", methods=["POST", "GET"])
def voice():
    """Handle incoming voice calls with optimized gather settings"""
    call_sid = request.values.get("CallSid")
    
    # Cleanup old files on first turn
    if turn_count.get(call_sid, 0) == 0:
        for f in os.listdir("static"):
            if f.startswith("response_") and "CAb" not in f and call_sid not in f:
                try:
                    file_path = f"static/{f}"
                    if time.time() - os.path.getmtime(file_path) > 600:  # 10+ minutes old
                        os.remove(file_path)
                except:
                    pass
    
    recording_url = request.values.get("RecordingUrl")
    
    print(f"==> /voice hit. CallSid: {call_sid}")
    if recording_url:
        filename = recording_url.split("/")[-1]
        print(f"🎧 Incoming Recording SID: {filename}")
    
    # Initialize or increment turn count
    if call_sid not in turn_count:
        turn_count[call_sid] = 0
    else:
        turn_count[call_sid] += 1
    
    print(f"🧪 Current turn: {turn_count[call_sid]}")
    
    mp3_path = f"static/response_{call_sid}.mp3"
    flag_path = f"static/response_ready_{call_sid}.txt"
    response = VoiceResponse()
    
    def is_file_ready(mp3_path, flag_path):
        """Check if audio file is ready to play"""
        if not os.path.exists(mp3_path) or not os.path.exists(flag_path):
            return False
        if os.path.getsize(mp3_path) < 1500:
            print("⚠️ MP3 file exists but is too small, not ready yet.")
            return False
        return True
    
    # Handle first turn with greeting + beep
    if turn_count[call_sid] == 0:
        print("📞 First turn — playing appropriate greeting")
        
        if not session.get("has_called_before"):
            session["has_called_before"] = True
            greeting_file = "first_time_greeting.mp3"
            print("👋 New caller detected — playing first-time greeting.")
        else:
            greeting_file = "returning_user_greeting.mp3"
            print("🔁 Returning caller — playing returning greeting.")
        
        # Play greeting
        response.play(f"{request.url_root}static/{greeting_file}?v={time.time()}")
        # Play beep after greeting
        response.play(f"{request.url_root}static/beep.mp3")
        # Brief pause after beep
        response.pause(length=1)
        
    elif is_file_ready(mp3_path, flag_path):
        print(f"🔊 Playing: {mp3_path}")
        public_mp3_url = f"{request.url_root}static/response_{call_sid}.mp3?v={time.time()}"
        response.play(public_mp3_url)
        
        # Clean up the ready flag after playing
        try:
            os.remove(flag_path)
        except:
            pass
            
    else:
        print("⏳ Response not ready — waiting briefly")
        # Just pause, don't redirect to avoid loops
        response.pause(length=1)
    
    # Determine optimal speech settings based on context
    is_first_turn = turn_count.get(call_sid, 0) == 0
    mode = mode_lock.get(call_sid, "unknown")
    current_turn = turn_count.get(call_sid, 0)
    
    # Configure gather parameters based on mode and turn
    if is_first_turn:
        # First turn: expect short commands
        speech_model = "experimental_utterances"  # Better for short utterances
        speech_timeout = "3"  # Give user time to start speaking
        gather_timeout = 30
        hints = "cold call, sales call, customer call, interview, interview prep, small talk, chat, conversation"
        
    elif mode == "interview":
        # Interview mode: expect longer responses
        speech_model = "experimental_conversations"  # Better for natural conversation
        
        # Dynamic timeout based on expected answer length
        if current_turn < 3:  # Early questions tend to have longer answers
            speech_timeout = "5"
            gather_timeout = 60
        else:  # Later questions might be shorter
            speech_timeout = "4"
            gather_timeout = 45
            
        # Interview-specific hints
        hints = "$OOV_CLASS_DIGIT_SEQUENCE, STAR method, situation, task, action, result, experience, skills"
        
    elif mode == "cold_call":
        # Cold call mode: natural conversation flow
        speech_model = "experimental_conversations"
        speech_timeout = "3"
        gather_timeout = 30
        
        # Sales conversation hints
        hints = "yes, no, interested, not interested, maybe, tell me more, pricing, features, demo"
        
    elif mode == "small_talk":
        # Small talk: casual conversation
        speech_model = "experimental_conversations"
        speech_timeout = "2"
        gather_timeout = 30
        hints = None  # No specific hints for casual conversation
        
    else:
        # Default/unknown mode
        speech_model = "experimental_conversations"
        speech_timeout = "2"
        gather_timeout = 30
        hints = None
    
    # Build gather parameters
    gather_params = {
        "input": "speech",
        "action": "/process_speech",
        "method": "POST",
        "speechTimeout": speech_timeout,
        "speechModel": speech_model,
        "enhanced": True,  # Use enhanced model for better accuracy
        "actionOnEmptyResult": False,
        "timeout": gather_timeout,
        "profanityFilter": False,
        "partialResultCallback": "/partial_speech",
        "partialResultCallbackMethod": "POST",
        "language": "en-US"
    }
    
    # Only add hints if they exist
    if hints:
        gather_params["hints"] = hints
    
    # Special handling for interviews - add bargeIn
    if mode == "interview" and current_turn > 0:
        gather_params["bargeIn"] = True  # Allow interruption during prompts
    
    # Log gather configuration
    print(f"📊 Gather config: model={speech_model}, speechTimeout={speech_timeout}, timeout={gather_timeout}")
    if hints:
        print(f"💡 Hints: {hints[:50]}...")
    
    # Execute gather
    try:
        response.gather(**gather_params)
    except Exception as e:
        print(f"💥 Gather error: {e}")
        # Fallback gather with minimal settings
        response.gather(
            input="speech",
            action="/process_speech",
            method="POST",
            timeout=30
        )
    
    # Add a fallback say in case gather times out completely
    response.say("I didn't catch that. Please try again.")
    response.redirect(f"{request.url_root}voice")
    
    return str(response)
@app.route("/partial_speech", methods=["POST"])
def partial_speech():
    """Handle partial speech results with enhanced processing"""
    
    try:
        # Get all the partial result data from Twilio
        call_sid = request.form.get("CallSid")
        sequence_number = request.form.get("SequenceNumber", "0")
        unstable_result = request.form.get("UnstableSpeechResult", "")
        speech_activity = request.form.get("SpeechActivity", "")
        caller = request.form.get("From", "Unknown")
        
        # Performance optimization: Skip very similar partials
        last_partial_key = f"{call_sid}_last_partial"
        last_partial_text = session.get(last_partial_key, "")
        
        if unstable_result and last_partial_text:
            # Calculate word-level similarity
            current_words = set(unstable_result.lower().split())
            last_words = set(last_partial_text.lower().split())
            
            if current_words and last_words:
                similarity = len(current_words & last_words) / len(current_words | last_words)
                if similarity > 0.85 and len(unstable_result) - len(last_partial_text) < 5:
                    return "", 204  # Skip logging similar partial
        
        session[last_partial_key] = unstable_result
        
        # Initialize conversation history if needed
        if call_sid not in conversation_history:
            conversation_history[call_sid] = []
        
        # Initialize partial storage structures - CHECK SPECIFICALLY FOR THESE KEYS
        if f"{call_sid}_partials" not in conversation_history:
            conversation_history[f"{call_sid}_partials"] = {}
            
        if f"{call_sid}_partial_stats" not in conversation_history:
            conversation_history[f"{call_sid}_partial_stats"] = {
                "start_time": time.time(),
                "word_count": 0,
                "last_activity": time.time()
            }
        
        # Store partial by sequence number for ordering
        seq_num = int(sequence_number) if sequence_number else 0
        conversation_history[f"{call_sid}_partials"][seq_num] = {
            "text": unstable_result,
            "timestamp": time.time(),
            "activity": speech_activity
        }
        
        # Get ordered text from all partials
        ordered_partials = sorted(conversation_history[f"{call_sid}_partials"].items())
        if ordered_partials:
            latest_text = ordered_partials[-1][1]["text"]
        else:
            latest_text = unstable_result
        
        # Log with clear formatting
        print(f"\n{'='*60}")
        print(f"🎤 PARTIAL SPEECH #{sequence_number} - CallSid: {call_sid}")
        print(f"📞 Caller: {caller}")
        print(f"{'='*60}")
        
        if unstable_result:
            print(f"⏳ UNSTABLE: '{unstable_result}'")
            
        if speech_activity:
            print(f"🔊 Activity: {speech_activity}")
        
        # Track conversation statistics
        stats = conversation_history[f"{call_sid}_partial_stats"]
        current_word_count = len(unstable_result.split()) if unstable_result else 0
        stats["word_count"] = max(stats["word_count"], current_word_count)
        stats["last_activity"] = time.time()
        
        # Detect completion patterns
        completion_indicators = []
        lower_text = unstable_result.lower() if unstable_result else ""
        
        # Check for incomplete endings
        incomplete_endings = ["like", "so", "and", "but", "or", "with", "for", "to", "the", "a"]
        words = unstable_result.strip().split() if unstable_result else []
        
        if words and words[-1].lower().rstrip('.,!?') in incomplete_endings:
            completion_indicators.append("⚠️ INCOMPLETE ENDING")
        
        # Check for complete sentences
        if unstable_result and any(unstable_result.rstrip().endswith(p) for p in ['.', '!', '?']):
            completion_indicators.append("✅ COMPLETE SENTENCE")
        
        # Detect early intent patterns (only on early turns)
        detected_intents = []
        
        # Only detect intent if we don't already have a mode locked
        if call_sid not in mode_lock or mode_lock.get(call_sid) == "unknown":
            if any(phrase in lower_text for phrase in ["cold call", "sales call", "customer call"]):
                detected_intents.append("🎯 COLD CALL PRACTICE")
            
            if any(phrase in lower_text for phrase in ["interview", "interview prep"]):
                detected_intents.append("👔 INTERVIEW PRACTICE")
                
            if any(phrase in lower_text for phrase in ["small talk", "chat", "conversation"]):
                detected_intents.append("💬 SMALL TALK")
                
            if any(phrase in lower_text for phrase in ["bad news", "delay", "problem", "issue"]):
                detected_intents.append("😠 BAD NEWS DETECTED")
                
            if any(phrase in lower_text for phrase in ["let's start over", "start over", "reset"]):
                detected_intents.append("🔄 RESET REQUEST")
        
        if detected_intents:
            print(f"\n🎯 Early Intent Detection:")
            for intent in detected_intents:
                print(f"   {intent}")
        
        if completion_indicators:
            print(f"\n📊 Speech Completion Status:")
            for indicator in completion_indicators:
                print(f"   {indicator}")
        
        # Long speech detection
        elapsed_time = time.time() - stats["start_time"]
        if elapsed_time > 30 and seq_num > 50:
            print(f"\n⏰ LONG SPEECH DETECTED: {elapsed_time:.1f}s, {stats['word_count']} words")
        
        # Clean up old partials to prevent memory issues
        if len(conversation_history[f"{call_sid}_partials"]) > 100:
            # Keep only the last 50 partials
            sorted_keys = sorted(conversation_history[f"{call_sid}_partials"].keys())
            for key in sorted_keys[:-50]:
                del conversation_history[f"{call_sid}_partials"][key]
        
        print(f"💭 Latest ordered text: '{latest_text}'")
        print(f"📈 Stats: {current_word_count} words, {elapsed_time:.1f}s elapsed")
        print(f"{'='*60}\n")
        
        # Return 204 No Content
        return "", 204
        
    except Exception as e:
        print(f"💥 Error in partial_speech: {e}")
        import traceback
        traceback.print_exc()
        return "", 204  # Return 204 even on error to prevent Twilio retries
@app.route("/process_speech", methods=["POST"])
@async_route
async def process_speech():
    """Handle final speech recognition results with full optimizations"""
    global active_call_sid
    
    # Performance tracking
    start_time = time.time()
    MAX_PROCESSING_TIME = 12  # seconds
    timing = {"start": start_time}
    
    print("✅ /process_speech endpoint hit")
    print(f"  USE_STREAMING: {USE_STREAMING}")
    print(f"  SENTENCE_STREAMING: {SENTENCE_STREAMING}")
    
    # Get the speech recognition results
    call_sid = request.form.get("CallSid")
    speech_result = request.form.get("SpeechResult", "")
    confidence = request.form.get("Confidence", "0.0")
    
    # Also capture additional Twilio speech data
    stability = request.form.get("Stability", "")
    speech_model = request.form.get("SpeechModel", "")
    
    print(f"📝 Final Speech Result: '{speech_result}'")
    print(f"🎯 Confidence: {confidence}")
    print(f"🎙️ Model: {speech_model}")
    print(f"🛍️ ACTIVE CALL SID at start of /process_speech: {active_call_sid}")
    
    # Check if we got any speech
    if not speech_result:
        print("⚠️ No speech detected, redirecting back to voice")
        response = VoiceResponse()
        response.play(f"{request.url_root}static/beep.mp3")
        response.redirect(url_for("voice", _external=True))
        return str(response)
    
    # Use the speech result as the transcript
    transcript = speech_result.strip()
    
    # Fix incomplete sentences
    incomplete_endings = ["like", "so", "and", "but", "or", "with", "for", "to", "the", "a", "um", "uh"]
    words = transcript.split()
    
    if words and words[-1].lower().rstrip('.,!?') in incomplete_endings:
        print("⚠️ Detected incomplete sentence, adding ellipsis")
        transcript = transcript.rstrip('.,!?') + "..."
    
    # Early timeout check
    if time.time() - start_time > 10:
        print("⚠️ Processing taking too long, sending early response")
        response = VoiceResponse()
        response.say("Just a moment please...")
        response.redirect(url_for("voice", _external=True))
        return str(response)
    
    # Reset memory for new calls
    if call_sid != active_call_sid:
        print(f"💨 Resetting memory for new call_sid: {call_sid}")
        conversation_history.clear()
        mode_lock.clear()
        personality_memory.clear()
        turn_count.clear()
        interview_question_index.clear()
        active_call_sid = call_sid
    
    # Helper functions
    def get_clean_conversation_history(call_sid):
        """Get only valid message entries from conversation history"""
        if call_sid not in conversation_history:
            return []
        
        clean_history = []
        for entry in conversation_history[call_sid]:
            if (isinstance(entry, dict) and 
                "role" in entry and 
                "content" in entry and
                entry.get("type") != "partial"):
                clean_history.append({
                    "role": entry["role"],
                    "content": entry["content"]
                })
        
        return clean_history
    
    def detect_bad_news(text):
        lowered = text.lower()
        return any(phrase in lowered for phrase in [
            "bad news", "unfortunately", "problem", "delay", "issue", 
            "we can't", "we won't", "not going to happen", "reschedule", 
            "price increase", "pushed back", "cancelled", "postponed"
        ])

    def detect_intent(text):
        lowered = text.lower()
        if any(phrase in lowered for phrase in ["cold call", "customer call", "sales call", "business call"]):
            return "cold_call"
        elif any(phrase in lowered for phrase in ["interview", "interview prep", "job interview"]):
            return "interview"
        elif any(phrase in lowered for phrase in ["small talk", "chat", "casual conversation"]):
            return "small_talk"
        return "unknown"

    # Check for reset command
    if any(phrase in transcript.lower() for phrase in ["let's start over", "start over", "reset", "begin again"]):
        print("🔁 Reset triggered by user — rolling new persona")
        conversation_history.pop(call_sid, None)
        personality_memory.pop(call_sid, None)
        interview_question_index.pop(call_sid, None)
        turn_count[call_sid] = 0
        transcript = "cold call practice"  # Default reset mode

    # Determine mode with safety check
    turn = turn_count.get(call_sid, 0)
    mode = mode_lock.get(call_sid)
    
    if not mode:
        if turn == 0:
            mode = detect_intent(transcript)
            mode_lock[call_sid] = mode
            print("🧐 Intent detected and locked:", mode)
        else:
            # Safety: Don't allow mode detection after turn 0
            print("⚠️ Late mode detection prevented, defaulting to unknown")
            mode = "unknown"
            mode_lock[call_sid] = mode
    else:
        print("📌 Using existing mode:", mode)

    # Clean conversation history
    if call_sid in conversation_history:
        conversation_history[call_sid] = [
            entry for entry in conversation_history[call_sid]
            if isinstance(entry, dict) and "role" in entry and 
            "content" in entry and entry.get("type") != "partial"
        ]

    # Set up personality and voice based on mode
    if mode == "cold_call" or mode == "customer_convo":
        if call_sid not in personality_memory:
            persona_name = random.choice(list(cold_call_personality_pool.keys()))
            personality_memory[call_sid] = persona_name
        else:
            persona_name = personality_memory[call_sid]

        persona = cold_call_personality_pool[persona_name]
        voice_id = persona["voice_id"]
        system_prompt = persona["system_prompt"]
        intro_line = persona.get("intro_line", "Alright, I'll be your customer. Start the conversation.")

    elif mode == "small_talk":
        voice_id = personality_profiles["small_talk"]["voice_id"]
        system_prompt = "You're a casual, friendly conversationalist. Keep it light and natural."
        intro_line = "Hey there! What's on your mind?"

    elif mode == "interview":
        # Consistent voice throughout interview
        if call_sid not in personality_memory:
            voice_choice = random.choice([
                {"voice_id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel"},
                {"voice_id": "EXAVITQu4vr4xnSDxMaL", "name": "Clyde"},
                {"voice_id": "6YQMyaUWlj0VX652cY1C", "name": "Stephen"}
            ])
            personality_memory[call_sid] = voice_choice
        else:
            voice_choice = personality_memory[call_sid]
        
        voice_id = voice_choice["voice_id"]
        
        # Track interview question progression
        if call_sid not in interview_question_index:
            interview_question_index[call_sid] = 0
        
        system_prompt = (
            f"You are {voice_choice['name']}, a friendly job interviewer. "
            "Give brief, encouraging feedback after each answer. "
            "Keep responses under 50 words. "
            "Move to the next question smoothly."
        )
        intro_line = "Great! Let's start. Can you walk me through your most recent role?"

    else:
        voice_id = "1t1EeRixsJrKbiF1zwM6"
        system_prompt = "You're a helpful assistant. Be concise and friendly."
        intro_line = "How can I help you today?"

    # Update turn count
    turn_count[call_sid] = turn + 1
    conversation_history.setdefault(call_sid, [])

    timing["mode_setup"] = time.time()
    print(f"⏱️ Mode setup took: {timing['mode_setup'] - timing['start']:.2f}s")

    # Generate response
    if turn == 0:
        reply = intro_line
        conversation_history[call_sid].append({"role": "assistant", "content": reply})
    else:
        # Add user message to history
        conversation_history[call_sid].append({"role": "user", "content": transcript})
        
        # Build messages array
        messages = [{"role": "system", "content": system_prompt}]
        
        # Check for bad news and add appropriate prompt
        if detect_bad_news(transcript):
            print("⚠️ Bad news detected — AI will respond accordingly")
            escalation_prompt = (
                "The user just delivered bad news. Respond emotionally based on your personality. "
                "Express frustration appropriately. Stay in character."
            )
            messages.append({"role": "system", "content": escalation_prompt})

        # Add conversation history (limit to last 10 exchanges for performance)
        history = get_clean_conversation_history(call_sid)
        messages.extend(history[-20:])  # Last 10 back-and-forth exchanges
        
        # Check timeout before GPT call
        if time.time() - start_time > MAX_PROCESSING_TIME - 3:
            print("⚠️ Near timeout, using quick response")
            reply = "I understand. Let me process that."
        else:
            # Get GPT response with optimized settings
            try:
                if USE_STREAMING:
                    reply = await streaming_gpt_response(messages, voice_id, call_sid)
                else:
                    model = "gpt-3.5-turbo" if mode != "interview" else "gpt-4"
                    gpt_reply = sync_openai.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=0.7,
                        max_tokens=100,  # Keep responses concise
                        presence_penalty=0.3,  # Reduce repetition
                        frequency_penalty=0.3
                    )
                    reply = gpt_reply.choices[0].message.content.strip()
                    
                timing["gpt_done"] = time.time()
                print(f"⏱️ GPT took: {timing['gpt_done'] - timing.get('mode_setup', start_time):.2f}s")
                    
            except Exception as e:
                print(f"💥 GPT error: {e}")
                reply = "I understand. Could you repeat that?"
        
        # Clean up response
        reply = reply.replace("*", "").replace("_", "").replace("`", "").replace("#", "")
        conversation_history[call_sid].append({"role": "assistant", "content": reply})

    print(f"🔣 Generating voice with ID: {voice_id}")
    print(f"🗣️ Reply: {reply[:100]}...")
    
    # TTS Generation with caching and optimization
    output_path = f"static/response_{call_sid}.mp3"
    
    # Check cache for common short responses
    cache_key = f"{voice_id}_{reply[:50].lower().replace(' ', '_')}"
    cache_path = f"static/cache/{cache_key}.mp3"
    
    if len(reply) < 50 and os.path.exists(cache_path):
        print(f"📦 Using cached audio for common response")
        os.system(f"cp {cache_path} {output_path}")
    else:
        # Check timeout before TTS
        if time.time() - start_time > MAX_PROCESSING_TIME - 2:
            print("⚠️ Near timeout, using ultra-fast TTS")
            # Use fastest possible TTS
            try:
                audio_gen = elevenlabs_client.text_to_speech.convert(
                    voice_id=voice_id,
                    text="Just a moment.",
                    model_id="eleven_turbo_v2_5",
                    output_format="mp3_22050_32"
                )
                raw_audio = b"".join(chunk for chunk in audio_gen if chunk)
                with open(output_path, "wb") as f:
                    f.write(raw_audio)
            except:
                # Ultimate fallback
                fallback_path = "static/fallback.mp3"
                if os.path.exists(fallback_path):
                    os.system(f"cp {fallback_path} {output_path}")
        else:
            # Normal TTS generation
            try:
                # Choose model based on mode and length
                if len(reply) < 50:
                    model_id = "eleven_turbo_v2_5"  # Fastest for short
                else:
                    model_id = "eleven_monolingual_v1"  # Better quality for longer
                
                audio_gen = elevenlabs_client.text_to_speech.convert(
                    voice_id=voice_id,
                    text=reply,
                    model_id=model_id,
                    output_format="mp3_22050_32",
                    voice_settings=VoiceSettings(
                        stability=0.5,
                        similarity_boost=0.75,
                        style=0.0,
                        use_speaker_boost=True
                    )
                )
                
                raw_audio = b""
                for chunk in audio_gen:
                    if chunk:
                        raw_audio += chunk
                
                with open(output_path, "wb") as f:
                    f.write(raw_audio)
                    f.flush()
                    
                print(f"✅ Audio saved to {output_path} ({len(raw_audio)} bytes)")
                
                timing["tts_done"] = time.time()
                print(f"⏱️ TTS took: {timing['tts_done'] - timing.get('gpt_done', start_time):.2f}s")
                
                # Cache short common responses
                if len(reply) < 50 and not os.path.exists(cache_path):
                    os.makedirs("static/cache", exist_ok=True)
                    os.system(f"cp {output_path} {cache_path}")
                    
            except Exception as e:
                print(f"🛑 ElevenLabs error: {e}")
                # Retry with different model
                try:
                    audio_gen = elevenlabs_client.text_to_speech.convert(
                        voice_id=voice_id,
                        text=reply,
                        model_id="eleven_multilingual_v2",
                        output_format="mp3_22050_32"
                    )
                    raw_audio = b"".join(chunk for chunk in audio_gen if chunk)
                    with open(output_path, "wb") as f:
                        f.write(raw_audio)
                    print("✅ Retry with multilingual model succeeded")
                except:
                    # Final fallback
                    fallback_path = "static/fallback.mp3"
                    if os.path.exists(fallback_path):
                        os.system(f"cp {fallback_path} {output_path}")
    
    # Always create ready flag
    with open(f"static/response_ready_{call_sid}.txt", "w") as f:
        f.write("ready")
    print(f"🚩 Ready flag created for {call_sid}")
    
    # Clear partial data (don't delete the keys entirely)
    if f"{call_sid}_partials" in conversation_history:
        conversation_history[f"{call_sid}_partials"].clear()
    if f"{call_sid}_partial_stats" in conversation_history:
        conversation_history[f"{call_sid}_partial_stats"]["last_activity"] = time.time()
    
    # Schedule cleanup
    cleanup_thread = threading.Thread(target=delayed_cleanup, args=(call_sid,))
    cleanup_thread.daemon = True
    cleanup_thread.start()
    
    # Log total time
    total_time = time.time() - start_time
    print(f"⏱️ Total processing time: {total_time:.2f}s")
    
    # Performance warning
    if total_time > 8:
        print(f"⚠️ SLOW RESPONSE: {total_time:.2f}s - investigate!")
    
    # Play beep and redirect
    response = VoiceResponse()
    response.play(f"{request.url_root}static/beep.mp3")
    response.redirect(url_for("voice", _external=True))
    return str(response)
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
                        stream=True, language='en'
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
            # Create output file immediately
            output_path = f"static/response_{call_sid}.mp3"
            temp_path = f"static/response_{call_sid}_temp.mp3"
            
            # Streaming with sentence detection
            stream = await async_openai.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                temperature=0.7
            )
            
            full_response = ""
            sentence_buffer = ""
            sentence_count = 0
            first_audio_saved = False
            
            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    text = chunk.choices[0].delta.content
                    sentence_buffer += text
                    full_response += text
                    
                    # Check for sentence completion
                    sentences = re.split(r'(?<=[.!?])\s+', sentence_buffer)
                    
                    # Process complete sentences immediately
                    for sentence in sentences[:-1]:
                        if sentence.strip():
                            sentence_count += 1
                            print(f"🎯 Processing sentence {sentence_count}: {sentence[:30]}...")
                            
                            # Generate TTS for this sentence
                            try:
                                audio_data = await generate_tts_streaming(sentence, voice_id)
                                
                                # Save first sentence immediately
                                if not first_audio_saved:
                                    with open(output_path, "wb") as f:
                                        f.write(audio_data)
                                    first_audio_saved = True
                                    print(f"✅ First audio chunk saved - ready to play!")
                                else:
                                    # Append subsequent sentences
                                    # This is a simplified approach - you might need proper MP3 concatenation
                                    with open(output_path, "ab") as f:
                                        f.write(audio_data)
                                        
                            except Exception as e:
                                print(f"⚠️ TTS error for sentence {sentence_count}: {e}")
                    
                    sentence_buffer = sentences[-1] if sentences else ""
            
            # Process final sentence
            if sentence_buffer.strip():
                try:
                    audio_data = await generate_tts_streaming(sentence_buffer, voice_id)
                    if not first_audio_saved:
                        with open(output_path, "wb") as f:
                            f.write(audio_data)
                    else:
                        with open(output_path, "ab") as f:
                            f.write(audio_data)
                except Exception as e:
                    print(f"⚠️ TTS error for final sentence: {e}")
            
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



if __name__ == "__main__":
    # Ensure static directory exists
    os.makedirs("static", exist_ok=True)
    
    print("\n🚀 ConvoReps Streaming Edition")
    print(f"   USE_STREAMING: {USE_STREAMING}")
    print(f"   SENTENCE_STREAMING: {SENTENCE_STREAMING}")
    print(f"   STREAMING_TIMEOUT: {STREAMING_TIMEOUT}s")
    print("\n")
    
    app.run(host="0.0.0.0", port=5050)
