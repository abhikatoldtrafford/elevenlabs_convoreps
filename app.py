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
@error_handler
def voice():
    call_sid = request.values.get("CallSid")
    if turn_count.get(call_sid, 0) == 0:  # First turn of new call
        for f in os.listdir("static"):
            if f.startswith("response_") and "CAb" not in f:  # Don't delete current call
                if time.time() - os.path.getmtime(f"static/{f}") > 600:  # 10+ minutes old
                    try: os.remove(f"static/{f}")
                    except: pass
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

    mp3_path = f"static/response_{call_sid}.mp3"
    flag_path = f"static/response_ready_{call_sid}.txt"
    response = VoiceResponse()

    def is_file_ready(mp3_path, flag_path):
        if not os.path.exists(mp3_path) or not os.path.exists(flag_path):
            return False
        if os.path.getsize(mp3_path) < 1500:
            print("⚠️ MP3 file exists but is too small, not ready yet.")
            return False
        return True

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
        response.play(f"{request.url_root}static/beep.mp3")  # ADD BEEP HERE
        response.pause(length=1)  # Brief pause after beep

    elif is_file_ready(mp3_path, flag_path):
        print(f"🔊 Playing: {mp3_path}")
        public_mp3_url = f"{request.url_root}static/response_{call_sid}.mp3"
        response.play(public_mp3_url)
    else:
        print("⏳ Response not ready — waiting briefly")
        response.play(f"{request.url_root}static/beep.mp3") 
        response.pause(length=2)

    response.gather(
        input='speech',
        action='/process_speech',
        method='POST',
        speechTimeout='2',  # Change from 'auto' to fixed 2 seconds
        speechModel='experimental_conversations',
        enhanced=True,
        actionOnEmptyResult=False,
        timeout=30,  # Increase from 3 to 30 seconds to prevent 499 errors
        profanityFilter=False,
        partialResultCallback='/partial_speech',
        partialResultCallbackMethod='POST',
        language='en-US'
    )
    return str(response)
@app.route("/partial_speech", methods=["POST"])
def partial_speech():
    """Handle partial speech results - simple logging version"""
    
    # Get all the partial result data from Twilio
    call_sid = request.form.get("CallSid")
    sequence_number = request.form.get("SequenceNumber", "0")
    
    # UnstableSpeechResult: Low confidence, still being processed
    unstable_result = request.form.get("UnstableSpeechResult", "")
    
    # Speech activity indicators
    speech_activity = request.form.get("SpeechActivity", "")
    
    # Get caller info for logging
    caller = request.form.get("From", "Unknown")
    
    # Log the partial results with emojis for clarity
    print(f"\n{'='*60}")
    print(f"🎤 PARTIAL SPEECH #{sequence_number} - CallSid: {call_sid}")
    print(f"📞 Caller: {caller}")
    print(f"{'='*60}")

    if unstable_result:
        print(f"⏳ UNSTABLE: '{unstable_result}'")
        
    if speech_activity:
        print(f"🔊 Activity: {speech_activity}")
    
    # Calculate total heard so far
    total_heard = unstable_result
    if total_heard:
        print(f"💭 Total heard so far: '{total_heard}'")
    
    # Detect early intent patterns (just logging, no action)
    detected_intents = []
    lower_text = total_heard.lower()
    
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
    
    # Track conversation flow
    if call_sid not in conversation_history:
        conversation_history[call_sid] = []
    
    # Store partial results in conversation history for debugging
    partial_entry = {
        "type": "partial",
        "sequence": int(sequence_number),  # Convert to int for proper sorting
        "text": unstable_result,  # Rename to just "text" since there's only unstable
        "timestamp": time.time(),
        "word_count": len(unstable_result.split()) if unstable_result else 0
    }
    # Keep only last 10 partial entries to avoid memory issues
    partials = [e for e in conversation_history[call_sid] if e.get("type") == "partial"]
    sorted_partials = sorted(partials, key=lambda x: int(x.get("sequence", 0)))
    if len(partials) >= 10:
        # Remove oldest partial
        conversation_history[call_sid] = [
            e for e in conversation_history[call_sid] 
            if not (e.get("type") == "partial" and e["sequence"] == partials[0]["sequence"])
        ]
    
    conversation_history[call_sid].append(partial_entry)
    
    # Speech length analysis
    
    # Debug all received parameters
    if os.getenv("DEBUG_PARTIAL", "false").lower() == "true":
        print(f"\n🔍 DEBUG - All Parameters:")
        for key, value in request.form.items():
            print(f"   {key}: {value}")
    
    print(f"{'='*60}\n")
    
    # Return 204 No Content - this doesn't affect the call flow
    return "", 204
@app.route("/process_speech", methods=["POST"])
@async_route
async def process_speech():
    """Handle final speech recognition results from Gather"""
    global active_call_sid
    
    # Set a maximum processing time to avoid 499 errors
    start_time = time.time()
    MAX_PROCESSING_TIME = 12  # seconds
    
    print("✅ /process_speech endpoint hit")
    print(f"  USE_STREAMING: {USE_STREAMING}")
    print(f"  SENTENCE_STREAMING: {SENTENCE_STREAMING}")
    
    # Get the speech recognition results
    call_sid = request.form.get("CallSid")
    speech_result = request.form.get("SpeechResult", "")
    confidence = request.form.get("Confidence", "0.0")
    
    print(f"📝 Final Speech Result: '{speech_result}'")
    print(f"🎯 Confidence: {confidence}")
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
    
    # If we're taking too long already, send an early response to prevent timeout
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
        active_call_sid = call_sid
    
    # Helper function to get clean conversation history
    def get_clean_conversation_history(call_sid):
        """Get only valid message entries from conversation history"""
        if call_sid not in conversation_history:
            return []
        
        clean_history = []
        for entry in conversation_history[call_sid]:
            # Only include entries with role and content (skip partial entries)
            if (isinstance(entry, dict) and 
                "role" in entry and 
                "content" in entry and
                entry.get("type") != "partial"):
                clean_history.append({
                    "role": entry["role"],
                    "content": entry["content"]
                })
        
        return clean_history
    
    # Define helper functions
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

    # Check for reset command
    if "let's start over" in transcript.lower():
        print("🔁 Reset triggered by user — rolling new persona")
        conversation_history.pop(call_sid, None)
        personality_memory.pop(call_sid, None)
        turn_count[call_sid] = 0
        transcript = "cold call practice"

    # Determine mode
    mode = mode_lock.get(call_sid)
    if not mode:
        mode = detect_intent(transcript)
        mode_lock[call_sid] = mode
    print("🧐 Detected intent:", mode)

    # Clean conversation history for this call (remove partial entries)
    if call_sid in conversation_history:
        conversation_history[call_sid] = [
            entry for entry in conversation_history[call_sid]
            if isinstance(entry, dict) and "role" in entry and "content" in entry and entry.get("type") != "partial"
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

        # Check if we already have a voice assigned for consistency
        if call_sid not in personality_memory:
            voice_choice = random.choice(interview_voice_pool)
            personality_memory[call_sid] = voice_choice
        else:
            voice_choice = personality_memory[call_sid]
        
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

    # Manage turn count and conversation history
    turn = turn_count.get(call_sid, 0)
    turn_count[call_sid] = turn + 1
    conversation_history.setdefault(call_sid, [])

    # Generate response
    if turn == 0:
        reply = intro_line
        conversation_history[call_sid].append({"role": "assistant", "content": reply})
    else:
        # Add user message to history
        conversation_history[call_sid].append({"role": "user", "content": transcript})
        
        # Build messages array with validation
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

        # Add clean conversation history
        messages += get_clean_conversation_history(call_sid)
        
        # Debug: Print messages structure
        print(f"📋 Messages array length: {len(messages)}")
        for i, msg in enumerate(messages):
            if isinstance(msg, dict) and "role" in msg:
                content_preview = msg.get('content', '')[:50] + '...' if msg.get('content', '') else 'No content'
                print(f"   [{i}] {msg['role']}: {content_preview}")
            else:
                print(f"   [{i}] INVALID: {msg}")

        # Check if we're close to timeout before GPT call
        if time.time() - start_time > MAX_PROCESSING_TIME - 3:
            print("⚠️ Near timeout, using quick response")
            reply = "Let me think about that for a moment."
        else:
            # Get GPT response
            try:
                if USE_STREAMING:
                    reply = await streaming_gpt_response(messages, voice_id, call_sid)
                else:
                    gpt_reply = sync_openai.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=messages,
                        temperature=0.7,
                        max_tokens=150  # Limit response length for speed
                    )
                    reply = gpt_reply.choices[0].message.content.strip()
            except Exception as e:
                print(f"💥 GPT error: {e}")
                # Fallback response
                reply = "I'm having a bit of trouble understanding. Could you say that again?"
                
        # Clean up response
        reply = reply.replace("*", "").replace("_", "").replace("`", "").replace("#", "").replace("-", " ")
        conversation_history[call_sid].append({"role": "assistant", "content": reply})

    print(f"🔣 Generating voice with ID: {voice_id}")
    print(f"🗣️ Reply: {reply[:100]}...")
    
    # Generate TTS with timeout protection
    output_path = f"static/response_{call_sid}.mp3"
    
    # Check if we're close to timeout
    if time.time() - start_time > MAX_PROCESSING_TIME - 2:
        print("⚠️ Near timeout, using fallback audio")
        fallback_path = "static/fallback.mp3"
        if os.path.exists(fallback_path):
            os.system(f"cp {fallback_path} {output_path}")
        else:
            # Create a very quick TTS response
            try:
                quick_gen = elevenlabs_client.text_to_speech.convert(
                    voice_id=voice_id,
                    text="Just a moment.",
                    model_id="eleven_turbo_v2_5",
                    output_format="mp3_22050_32"
                )
                raw_audio = b""
                for chunk in quick_gen:
                    if chunk:
                        raw_audio += chunk
                with open(output_path, "wb") as f:
                    f.write(raw_audio)
            except:
                pass
    else:
        # Normal TTS generation
        if USE_STREAMING and SENTENCE_STREAMING and turn > 0 and os.path.exists(output_path):
            # Audio already generated by streaming_gpt_response
            print(f"✅ Audio already generated via sentence streaming for {call_sid}")
        else:
            # Generate TTS (streaming or non-streaming)
            try:
                audio_gen = elevenlabs_client.text_to_speech.convert(
                    voice_id=voice_id,
                    text=reply,
                    model_id="eleven_turbo_v2_5" if USE_STREAMING else "eleven_monolingual_v1",
                    output_format="mp3_22050_32"
                )
                raw_audio = b""
                for chunk in audio_gen:
                    if chunk:
                        raw_audio += chunk
                
                with open(output_path, "wb") as f:
                    f.write(raw_audio)
                    f.flush()
                print(f"✅ Audio saved to {output_path} ({len(raw_audio)} bytes)")
                    
            except Exception as e:
                print(f"🛑 ElevenLabs generation error: {e}")
                if "429" in str(e):  # Too Many Requests
                    print("🔁 Retrying after brief pause due to rate limit...")
                    await asyncio.sleep(2)
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
                            
                        with open(output_path, "wb") as f:
                            f.write(raw_audio)
                        print("✅ Retry succeeded")
                    except Exception as e2:
                        print(f"❌ Retry failed: {e2}")
                        fallback_path = "static/fallback.mp3"
                        if os.path.exists(fallback_path):
                            os.system(f"cp {fallback_path} {output_path}")
    
    # Always create ready flag after audio is saved
    with open(f"static/response_ready_{call_sid}.txt", "w") as f:
        f.write("ready")
    print(f"🚩 Ready flag created for {call_sid}")

    # Schedule cleanup
    cleanup_thread = threading.Thread(target=delayed_cleanup, args=(call_sid,))
    cleanup_thread.start()

    # Play a beep before the AI response
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
