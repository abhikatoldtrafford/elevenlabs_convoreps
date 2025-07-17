"""
ConvoReps - AI Voice Practice Platform
Features:
- Cold call, interview, and small talk practice
- Voice consistency throughout calls (locked after mode selection)
- Minute tracking with CSV storage
- SMS notifications when time expires
- ElevenLabs natural voice synthesis
- Tool calling for time checking
"""

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
import csv
import shutil
import tempfile
from datetime import datetime
from typing import Dict, Any, Set
from twilio.rest import Client as TwilioClient

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
SENTENCE_STREAMING = True

# Model Configuration - Centralized for easy updates
MODELS = {
    "openai": {
        "streaming_gpt": os.getenv("OPENAI_STREAMING_MODEL", "gpt-4.1-nano"),
        "standard_gpt": os.getenv("OPENAI_STANDARD_MODEL", "gpt-4.1-mini"),
        "streaming_transcribe": os.getenv("OPENAI_STREAMING_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
        "standard_transcribe": os.getenv("OPENAI_STANDARD_TRANSCRIBE_MODEL", "whisper-1")
    },
    "elevenlabs": {
        "voice_model": os.getenv("ELEVENLABS_VOICE_MODEL", "eleven_turbo_v2_5"),
        "output_format": os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_22050_32")
    }
}

# Handle STREAMING_TIMEOUT with potential comments
timeout_env = os.getenv("STREAMING_TIMEOUT", "2.0")
# ADD:
# Set defaults if not in environment
os.environ.setdefault("USE_STREAMING", "true")
os.environ.setdefault("SENTENCE_STREAMING", "true")
os.environ.setdefault("STREAMING_TIMEOUT", "2.0")
# Remove any comments if present
timeout_value = timeout_env.split('#')[0].strip()
try:
    STREAMING_TIMEOUT = float(timeout_value)
except ValueError:
    print(f"⚠️ Invalid STREAMING_TIMEOUT value: '{timeout_env}', using default 3.0")
    STREAMING_TIMEOUT = 3.0

# ConvoReps minute tracking configuration
FREE_CALL_MINUTES = float(os.getenv("FREE_CALL_MINUTES", "3.0"))
MIN_CALL_DURATION = float(os.getenv("MIN_CALL_DURATION", "0.5"))
USAGE_CSV_PATH = os.getenv("USAGE_CSV_PATH", "user_usage.csv")
USAGE_CSV_BACKUP_PATH = os.getenv("USAGE_CSV_BACKUP_PATH", "user_usage_backup.csv")

# Twilio credentials
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# Initialize Twilio client for SMS
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# In-memory state
turn_count = {}
mode_lock = {}
voice_lock = {}  # ADD THIS to lock voice selection
active_call_sid = None
conversation_history = {}
personality_memory = {}
interview_question_index = {}
# State management lock (IMPORTANT: Define this BEFORE other new variables)
state_lock = threading.Lock()
# Active streams tracking (IMPORTANT: This tracks call data)
active_streams: Dict[str, Dict[str, Any]] = {}
# Call start times (IMPORTANT: Used for minute calculation)
call_start_times: Dict[str, float] = {}
# New state variables for time tracking
call_timers: Dict[str, threading.Timer] = {}
sms_sent_flags: Dict[str, bool] = {}
processed_calls: Set[str] = set()
active_sessions: Dict[str, bool] = {}
csv_lock = threading.Lock()

# Voice profiles
personality_profiles = {
    "rude/skeptical": {"voice_id": "1t1EeRixsJrKbiF1zwM6"},
    "super busy": {"voice_id": "6YQMyaUWlj0VX652cY1C"},
    "small_talk": {"voice_id": "2BJW5coyhAzSr8STdHbE"}
}

cold_call_personality_pool = {
    "Jerry": {
        "voice_id": "1t1EeRixsJrKbiF1zwM6",
        "system_prompt": "You're Jerry. You're a skeptical small business owner who's been burned by vendors in the past. You're not rude, but you're direct and hard to win over. Respond naturally based on how the call starts — maybe this is a cold outreach, maybe a follow-up, or even someone calling you with bad news. Stay in character. If the salesperson fumbles, challenge them. If they're smooth, open up a bit. Speak casually, not like a script.\n\nYou have access to 'check_remaining_time' tool. Use it when:\n- The caller asks 'how much time do I have left?' or similar\n- After about 2 minutes of conversation (to proactively inform them)\n- When wrapping up the call\n\nExample tool usage scenarios:\n- If user says 'How much practice time do I have?' → Use the tool\n- If conversation is going well after 2 minutes → Say something like 'By the way, let me check how much time you have left for practice' then use the tool\n- When ending: 'Before we wrap up, let me see your remaining time' then use the tool\n\nWhen sharing time info, be helpful: 'You've got about X minutes left - let's make them count!' or 'Just X minutes remaining, so let's focus on the key points.'"
    },
    "Miranda": {
        "voice_id": "Ax1GP2W4XTyAyNHuch7v",
        "system_prompt": "You're Miranda. You're a busy office manager who doesn't have time for fluff. If the caller is clear and respectful, you'll hear them out. Respond naturally depending on how they open — this could be a cold call, a follow-up, or someone delivering news. Keep your tone grounded and real. Interrupt if needed. No robotic replies — talk like a real person at work.\n\nYou have access to 'check_remaining_time' tool. Use it when:\n- The caller asks about their time limit or remaining minutes\n- After roughly 60% of the conversation (be proactive)\n- When you sense the call should wrap up\n\nExample tool usage scenarios:\n- User: 'Do I have much time left?' → Use the tool immediately\n- After good progress: 'Let me quickly check your time - I want to make sure we cover everything' then use the tool\n- Near the end: 'We should probably check how much time you have left' then use the tool\n\nBe direct with time info: 'You have X minutes. What else did you need to cover?' or 'Only X minutes left - let's get to the point.'"
    },
    "Junior": {
        "voice_id": "Nbttze9nhGhK1czblc6j",
        "system_prompt": "You're Junior. You run a local shop and have heard it all. You're friendly but not easily impressed. Start skeptical, but if the caller handles things well, loosen up. Whether this is a pitch, a follow-up, or some kind of check-in, reply naturally. Use casual language. If something sounds off, call it out. You don't owe anyone your time — but you're not a jerk either.\n\nYou have access to 'check_remaining_time' tool. Use it when:\n- They ask anything about time limits or how long they can practice\n- After a good chunk of conversation (around 2 minutes)\n- When you're ready to end the call\n\nExample tool usage scenarios:\n- User: 'How long can we talk?' → 'Let me check that for you' then use the tool\n- Mid-conversation: 'Hey, let me see how much time you got left' then use the tool\n- Wrapping up: 'Before you go, let's check your time' then use the tool\n\nKeep it casual: 'Looks like you got X minutes left, buddy' or 'Only X minutes to go - better make it good!'"
    },
    "Brett": {
        "voice_id": "7eFTSJ6WtWd9VCU4ZlI1",
        "system_prompt": "You're Brett. You're a contractor who answers his phone mid-job. You're busy and a little annoyed this person called. If they're direct and helpful, give them a minute. If they ramble, shut it down. This could be a pitch, a check-in, or someone following up on a proposal. React based on how they start the convo. Talk rough, fast, and casual. No fluff, no formalities.\n\nYou have access to 'check_remaining_time' tool. Use it when:\n- They ask about time (even indirectly like 'can we keep going?')\n- You've been on for a while and want to wrap up\n- You're getting impatient\n\nExample tool usage scenarios:\n- User: 'Is there a time limit?' → 'Yeah, let me check' then use the tool\n- Getting antsy: 'Look, how much time we got here?' then use the tool\n- Ready to go: 'I gotta get back to work - lemme see your time' then use the tool\n\nBe blunt: 'You got X minutes. Make it quick.' or 'X minutes left. What's your point?'"
    },
    "Kayla": {
        "voice_id": "aTxZrSrp47xsP6Ot4Kgd",
        "system_prompt": "You're Kayla. You own a business and don't waste time. You've had too many bad sales calls and follow-ups from people who don't know how to close. Respond based on how they open — if it's a pitch, hit them with price objections. If it's a follow-up, challenge their urgency. Keep your tone sharp but fair. You don't sugarcoat things, and you don't fake interest.\n\nYou have access to 'check_remaining_time' tool. Use it when:\n- They inquire about practice time or limits\n- You've heard enough and want to see how much longer this will go\n- After significant conversation progress\n\nExample tool usage scenarios:\n- User: 'How much time do I get?' → 'Let's see what you're working with' then use the tool\n- Fed up: 'I need to know how much longer this is going to take' then use the tool\n- Business-like: 'What's our time situation here?' then use the tool\n\nBe businesslike: 'You have X minutes remaining. Use them wisely.' or 'X minutes left. Better close strong.'"
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

# Message scripts - EXACT WORDING from dev branch
MESSAGE_SCRIPTS = {
    "sms_first_call": (
        "Nice work on your first ConvoReps call! Ready for more? "
        "Get 30 extra minutes for $6.99 — no strings: convoreps.com"
    ),
    "sms_repeat_caller": (
        "Hey! You've already used your free call. Here's link to grab "
        "a Starter Pass for $6.99 and unlock more time: convoreps.com"
    ),
    "voice_time_limit": (
        "Alright, that's the end of your free call — but this is only the beginning. "
        "We just texted you a link to ConvoReps.com. Whether you're prepping for interviews "
        "or sharpening your pitch, this is how you level up — don't wait, your next opportunity is already calling."
    ),
    "voice_repeat_caller": (
        "Hey! You've already used your free call. But no worries, we just texted you a link to "
        "Convoreps.com. Grab a Starter Pass for $6.99 and turn practice into real-world results."
    )
}

# CSV Functions for minute tracking
def init_csv():
    """Initialize CSV file if it doesn't exist"""
    try:
        if not os.path.exists(USAGE_CSV_PATH):
            with open(USAGE_CSV_PATH, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['phone_number', 'minutes_used', 'minutes_left', 'last_call_date', 'total_calls'])
            print(f"Created usage tracking CSV: {USAGE_CSV_PATH}")
    except Exception as e:
        print(f"Error creating CSV file: {e}")

def read_user_usage(phone_number: str) -> Dict[str, Any]:
    """Read user usage from CSV"""
    init_csv()
    
    try:
        with csv_lock:
            with open(USAGE_CSV_PATH, 'r', newline='') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    if row['phone_number'] == phone_number:
                        return {
                            'phone_number': row['phone_number'],
                            'minutes_used': float(row.get('minutes_used', 0)),
                            'minutes_left': float(row.get('minutes_left', FREE_CALL_MINUTES)),
                            'last_call_date': row.get('last_call_date', ''),
                            'total_calls': int(row.get('total_calls', 0))
                        }
    except Exception as e:
        print(f"Error reading CSV: {e}")
    
    return {
        'phone_number': phone_number,
        'minutes_used': 0.0,
        'minutes_left': FREE_CALL_MINUTES,
        'last_call_date': '',
        'total_calls': 0
    }

def update_user_usage(phone_number: str, minutes_used: float):
    """Update user usage in CSV"""
    if minutes_used <= 0:
        print(f"⚠️ Skipping update for {phone_number}: minutes_used={minutes_used}")
        return
    
    init_csv()
    
    try:
        with csv_lock:
            rows = []
            user_found = False
            
            try:
                with open(USAGE_CSV_PATH, 'r', newline='') as csvfile:
                    reader = csv.DictReader(csvfile)
                    fieldnames = reader.fieldnames
                    
                    for row in reader:
                        if row['phone_number'] == phone_number:
                            user_found = True
                            current_used = float(row.get('minutes_used', 0))
                            current_left = float(row.get('minutes_left', FREE_CALL_MINUTES))
                            new_used = current_used + minutes_used
                            new_left = max(0, current_left - minutes_used)
                            
                            row['minutes_used'] = str(round(new_used, 2))
                            row['minutes_left'] = str(round(new_left, 2))
                            row['last_call_date'] = datetime.now().isoformat()
                            row['total_calls'] = str(int(row.get('total_calls', 0)) + 1)
                            
                            print(f"📊 Updated {phone_number}: Used {new_used:.2f} total, {new_left:.2f} left")
                        
                        rows.append(row)
            except FileNotFoundError:
                fieldnames = ['phone_number', 'minutes_used', 'minutes_left', 'last_call_date', 'total_calls']
            
            if not user_found:
                new_used = minutes_used
                new_left = max(0, FREE_CALL_MINUTES - minutes_used)
                new_user = {
                    'phone_number': phone_number,
                    'minutes_used': str(round(new_used, 2)),
                    'minutes_left': str(round(new_left, 2)),
                    'last_call_date': datetime.now().isoformat(),
                    'total_calls': '1'
                }
                rows.append(new_user)
                print(f"📊 New user {phone_number}: Used {new_used:.2f}, {new_left:.2f} left")
            
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', delete=False, newline='') as tmp_file:
                writer = csv.DictWriter(tmp_file, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
                temp_name = tmp_file.name
            
            shutil.move(temp_name, USAGE_CSV_PATH)
            
        print(f"✅ Successfully updated usage for {phone_number}: {minutes_used:.2f} minutes used this call")
                
    except Exception as e:
        print(f"❌ Error updating CSV for {phone_number}: {e}")

def send_convoreps_sms_link(phone_number: str, is_first_call: bool = True, retry_count: int = 0):
    """Send ConvoReps SMS with exact message scripts and retry logic"""
    max_retries = 3
    
    try:
        with state_lock:
            if sms_sent_flags.get(phone_number, False):
                print(f"📱 SMS already sent to {phone_number}, skipping duplicate")
                return
            sms_sent_flags[phone_number] = True
        
        if is_first_call:
            message_body = MESSAGE_SCRIPTS["sms_first_call"]
        else:
            message_body = MESSAGE_SCRIPTS["sms_repeat_caller"]
        
        print(f"📤 Sending SMS to {phone_number} (first_call={is_first_call}, retry={retry_count})")
        
        message = twilio_client.messages.create(
            body=message_body,
            from_=TWILIO_PHONE_NUMBER,
            to=phone_number
        )
        
        print(f"✅ ConvoReps SMS sent successfully to {phone_number}: {message.sid}")
            
    except Exception as e:
        print(f"❌ SMS error for {phone_number} (retry {retry_count}): {e}")
        with state_lock:
            sms_sent_flags[phone_number] = False
            
        # Retry logic
        if retry_count < max_retries - 1:
            print(f"🔄 Retrying SMS in 5 seconds...")
            threading.Timer(5.0, lambda: send_convoreps_sms_link(phone_number, is_first_call, retry_count + 1)).start()
        else:
            print(f"❌ Failed to send SMS after {max_retries} attempts to {phone_number}")

def handle_time_limit(call_sid: str, from_number: str):
    """Handle when free time limit is reached - SMS sent here"""
    print(f"⏰ TIME LIMIT REACHED for {call_sid} from {from_number}")
    
    with state_lock:
        if call_sid not in active_streams:
            print(f"📞 Call {call_sid} already ended, skipping time limit handling")
            return
        
        if call_sid in processed_calls:
            print(f"✅ Call {call_sid} already processed, skipping duplicate")
            return
    
    # Update usage immediately when time limit hit
    if call_sid in call_start_times:
        elapsed_minutes = (time.time() - call_start_times[call_sid]) / 60.0
        print(f"📊 Updating usage: {elapsed_minutes:.2f} minutes used")
        update_user_usage(from_number, elapsed_minutes)
        
        with state_lock:
            processed_calls.add(call_sid)
    
    # Mark session as time-limited (triggers voice message on next speech)
    with state_lock:
        if call_sid in active_sessions:
            active_sessions[call_sid] = False
            print(f"🚫 Marked session {call_sid} as time-limited")
            
    # SMS will be sent when time limit voice message is delivered
    print(f"💬 SMS will be sent after time limit message is played")

def get_time_check_tool():
    """Get the time check tool definition for OpenAI"""
    return {
        "type": "function",
        "function": {
            "name": "check_remaining_time",
            "description": "Check how many minutes the user has left in their free call",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }

def handle_tool_call(call_sid: str) -> str:
    """Handle check_remaining_time tool call
    
    Returns time remaining. If time is exhausted, marks session for immediate termination.
    """
    print(f"🔧 Tool call: check_remaining_time for {call_sid}")
    
    if call_sid and call_sid in call_start_times:
        elapsed_minutes = (time.time() - call_start_times[call_sid]) / 60.0
        from_number = active_streams.get(call_sid, {}).get('from_number')
        if from_number:
            usage_data = read_user_usage(from_number)
            remaining = max(0, usage_data['minutes_left'] - elapsed_minutes)
            
            print(f"⏱️ Time check: {elapsed_minutes:.2f} minutes elapsed, {remaining:.2f} remaining")
            
            if remaining <= MIN_CALL_DURATION:
                print(f"🚨 Tool detected time exhausted for {call_sid}")
                # Mark for immediate termination
                with state_lock:
                    active_sessions[call_sid] = False
                # Return special message that AI will use to say goodbye
                return "TIME_EXPIRED: The user has exhausted their free minutes. Say a brief, friendly goodbye and mention they'll receive a text with more information."
            
            # Format remaining time nicely
            if remaining < 1:
                return f"You have less than a minute remaining in your free call."
            elif remaining < 2:
                return f"You have about {round(remaining, 1)} minute remaining in your free call."
            else:
                return f"You have about {round(remaining, 1)} minutes remaining in your free call."
    
    print(f"⚠️ Unable to check time for {call_sid}")
    return "Unable to check remaining time."

def ensure_static_files():
    """Ensure required static files exist"""
    os.makedirs("static", exist_ok=True)
    
    # Create a simple beep sound if it doesn't exist
    beep_path = "static/beep.mp3"
    if not os.path.exists(beep_path):
        try:
            # Generate a simple beep using ElevenLabs with minimal text
            print("🔊 Generating beep.mp3...")
            audio_gen = elevenlabs_client.text_to_speech.convert(
                voice_id="21m00Tcm4TlvDq8ikWAM",  # Rachel voice
                text="beep",
                model_id=MODELS["elevenlabs"]["voice_model"],
                output_format=MODELS["elevenlabs"]["output_format"]
            )
            raw_audio = b""
            for chunk in audio_gen:
                if chunk:
                    raw_audio += chunk
            
            with open(beep_path, "wb") as f:
                f.write(raw_audio)
            print("✅ beep.mp3 created")
        except Exception as e:
            print(f"⚠️ Could not create beep.mp3: {e}")
            # Create silent file as fallback
            with open(beep_path, "wb") as f:
                f.write(b"")  # Empty MP3
    
    # Create greeting files if they don't exist
    greetings = {
        "first_time_greeting.mp3": "Welcome to ConvoReps! Tell me what you'd like to practice: cold calls, interviews, or just some small talk.",
        "returning_user_greeting.mp3": "Welcome back to ConvoReps! What would you like to practice today?",
        "fallback.mp3": "Just a moment please."
    }
    
    for filename, text in greetings.items():
        filepath = f"static/{filename}"
        if not os.path.exists(filepath):
            try:
                print(f"🔊 Generating {filename}...")
                audio_gen = elevenlabs_client.text_to_speech.convert(
                    voice_id="21m00Tcm4TlvDq8ikWAM",  # Rachel voice
                    text=text,
                    model_id=MODELS["elevenlabs"]["voice_model"],
                    output_format=MODELS["elevenlabs"]["output_format"]
                )
                raw_audio = b""
                for chunk in audio_gen:
                    if chunk:
                        raw_audio += chunk
                
                with open(filepath, "wb") as f:
                    f.write(raw_audio)
                print(f"✅ {filename} created")
            except Exception as e:
                print(f"⚠️ Could not create {filename}: {e}")

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

@app.route("/call_status", methods=["POST"])
@error_handler
def call_status():
    """Handle Twilio status callbacks - especially hangups"""
    call_sid = request.values.get("CallSid")
    call_status = request.values.get("CallStatus")
    call_duration = request.values.get("CallDuration", "0")
    
    print(f"📞 Call status update: {call_sid} is now {call_status} (duration: {call_duration}s)")
    
    # Handle call completion/hangup
    if call_status in ["completed", "busy", "failed", "no-answer"]:
        print(f"☎️ Call ended with status: {call_status}")
        if call_sid:
            cleanup_call_resources(call_sid)
    
    return "", 204


def delayed_cleanup(call_sid):
    time.sleep(120)  # Let Twilio play it first
    try:
        os.remove(f"static/response_{call_sid}.mp3")
        os.remove(f"static/response_ready_{call_sid}.txt")
        print(f"🧹 Cleaned up response files for {call_sid}")
    except Exception as e:
        print(f"⚠️ Cleanup error for {call_sid}: {e}")

def cleanup_call_resources(call_sid: str):
    """Clean up call resources with proper minute tracking and SMS handling"""
    print(f"🧹 Cleaning up resources for call {call_sid}")
    
    with state_lock:
        if call_sid in active_streams:
            from_number = active_streams[call_sid].get('from_number', '')
            
            # Cancel timer
            if call_sid in call_timers:
                call_timers[call_sid].cancel()
                call_timers.pop(call_sid, None)
                print(f"⏲️ Cancelled timer for {call_sid}")
            
            # Update usage based on actual duration
            if call_sid in call_start_times and from_number:
                if call_sid not in processed_calls:
                    call_duration = time.time() - call_start_times[call_sid]
                    minutes_used = call_duration / 60.0
                    
                    if minutes_used > 0.01:
                        print(f"📊 Call {call_sid} duration: {minutes_used:.2f} minutes")
                        update_user_usage(from_number, minutes_used)
                        
                        processed_calls.add(call_sid)
                        
                        # Check if SMS needed after call ends
                        usage_after = read_user_usage(from_number)
                        print(f"📱 Post-call check for {from_number}: {usage_after['minutes_left']:.2f} minutes left")
                        
                        if usage_after['minutes_left'] <= 0.5:
                            is_first_call = usage_after['total_calls'] <= 1
                            print(f"📤 User exhausted minutes, sending SMS (first_call={is_first_call})")
                            send_convoreps_sms_link(from_number, is_first_call=is_first_call)
                
                call_start_times.pop(call_sid, None)
            
            # Clean up state
            active_streams.pop(call_sid, None)
            active_sessions.pop(call_sid, None)
            personality_memory.pop(call_sid, None)
            mode_lock.pop(call_sid, None)
            voice_lock.pop(call_sid, None)  # Clean up voice lock
            interview_question_index.pop(call_sid, None)
            
            print(f"✅ Cleaned up all state for {call_sid}")
            
            # Clear SMS flag after delay
            if from_number:
                def clear_sms_flag():
                    time.sleep(300)  # 5 minutes
                    with state_lock:
                        sms_sent_flags.pop(from_number, None)
                        print(f"🔄 Cleared SMS flag for {from_number}")
                threading.Thread(target=clear_sms_flag, daemon=True).start()
            
            # Prevent memory leak
            if len(processed_calls) > 100:
                processed_calls.clear()
                print("🧹 Cleared processed_calls set (>100 entries)")


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
    from_number = request.values.get("From", "")
    
    # Check user's free minutes
    if from_number:
        usage_data = read_user_usage(from_number)
        is_repeat_caller = usage_data['total_calls'] > 0
        minutes_left = usage_data['minutes_left']
        
        print(f"📊 User {from_number}: {minutes_left:.2f} minutes left, total calls: {usage_data['total_calls']}")
        
        if minutes_left < MIN_CALL_DURATION:
            print(f"🚫 User {from_number} has insufficient minutes ({minutes_left:.2f} < {MIN_CALL_DURATION})")
            
            # Generate ElevenLabs audio for the message
            if is_repeat_caller:
                message_text = MESSAGE_SCRIPTS["voice_repeat_caller"]
            else:
                message_text = "Your free minutes have been used. We just texted you a link to continue."
            
            # Use a friendly voice for the message
            no_time_voice_id = "21m00Tcm4TlvDq8ikWAM"  # Rachel voice
            
            # Generate audio file
            no_time_audio_path = f"static/no_time_{call_sid}.mp3"
            try:
                audio_gen = elevenlabs_client.text_to_speech.convert(
                    voice_id=no_time_voice_id,
                    text=message_text,
                    model_id=MODELS["elevenlabs"]["voice_model"],
                    output_format=MODELS["elevenlabs"]["output_format"]
                )
                raw_audio = b""
                for chunk in audio_gen:
                    if chunk:
                        raw_audio += chunk
                
                with open(no_time_audio_path, "wb") as f:
                    f.write(raw_audio)
                print(f"✅ No-time message audio generated: {len(raw_audio)} bytes")
            except Exception as e:
                print(f"❌ Failed to generate no-time audio: {e}")
                # Fallback to Twilio TTS if ElevenLabs fails
                response = VoiceResponse()
                response.say(message_text)
                response.hangup()
                send_convoreps_sms_link(from_number, is_first_call=not is_repeat_caller)
                return str(response)
            
            # Play the ElevenLabs audio
            response = VoiceResponse()
            response.play(f"{request.url_root}static/no_time_{call_sid}.mp3")
            response.pause(length=1)
            response.hangup()
            
            # Send SMS when user has no time left
            send_convoreps_sms_link(from_number, is_first_call=not is_repeat_caller)
            
            # Clean up audio file after delay
            def cleanup_no_time_audio():
                time.sleep(30)
                try:
                    os.remove(no_time_audio_path)
                    print(f"🧹 Cleaned up no-time audio for {call_sid}")
                except:
                    pass
            threading.Thread(target=cleanup_no_time_audio, daemon=True).start()
            
            return str(response)
    else:
        minutes_left = FREE_CALL_MINUTES
        is_repeat_caller = False
        print(f"⚠️ No phone number provided, using default {FREE_CALL_MINUTES} minutes")

    print(f"==> /voice hit. CallSid: {call_sid}")
    if recording_url:
        filename = recording_url.split("/")[-1]
        print(f"🎧 Incoming Recording SID: {filename}")

    if call_sid not in turn_count:
        turn_count[call_sid] = 0
        call_start_times[call_sid] = time.time()  # IMPORTANT: Track call start time
        
        # Store call data
        active_streams[call_sid] = {
            'from_number': from_number,
            'minutes_left': minutes_left,
            'last_activity': time.time()
        }
        active_sessions[call_sid] = True
        
        # Set up timer for time limit
        timer_duration = minutes_left * 60 if minutes_left < FREE_CALL_MINUTES else FREE_CALL_MINUTES * 60
        print(f"⏲️ Setting timer for {timer_duration:.0f} seconds ({timer_duration/60:.1f} minutes)")
        
        timer = threading.Timer(timer_duration, lambda: handle_time_limit(call_sid, from_number))
        timer.start()
        
        with state_lock:
            call_timers[call_sid] = timer
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
        #response.play(f"{request.url_root}static/beep.mp3") 
        response.pause(length=2)

    response.gather(
        input='speech',
        action='/process_speech',
        method='POST',
        speechTimeout='2',  # Change from 'auto' to fixed 2 seconds
        speechModel='experimental_conversations',
        enhanced=True,
        actionOnEmptyResult=False,
        timeout=10,  # Increase from 3 to 30 seconds to prevent 499 errors
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
        #response.play(f"{request.url_root}static/beep.mp3")
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
        voice_lock.clear()  # Clear voice locks
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
        voice_lock.pop(call_sid, None)  # Clear voice lock
        mode_lock.pop(call_sid, None)   # Clear mode lock
        turn_count[call_sid] = 0
        transcript = "cold call practice"

    # Check if time limit reached (user was speaking when timer expired)
    if call_sid and call_sid in active_sessions and not active_sessions.get(call_sid, True):
        print(f"⏰ User was speaking when time limit hit - delivering message now")
        
        # Get the locked voice for consistency
        if call_sid in voice_lock:
            voice_id = voice_lock[call_sid]["voice_id"]
            print(f"🎤 Using locked voice {voice_id} for time limit message")
        else:
            # Fallback if somehow voice wasn't locked
            voice_id = "21m00Tcm4TlvDq8ikWAM"  # Rachel as default
            print(f"⚠️ No locked voice found, using default Rachel")
        
        reply = MESSAGE_SCRIPTS["voice_time_limit"]
        
        # Send SMS
        if call_sid in active_streams:
            from_number = active_streams[call_sid].get('from_number')
            if from_number:
                usage_data = read_user_usage(from_number)
                is_first_call = usage_data['total_calls'] <= 1
                send_convoreps_sms_link(from_number, is_first_call=is_first_call)
        
        print(f"🔔 Delivering time limit message for {call_sid}")
        print(f"📢 Message: {reply[:50]}...")
        
        # Generate TTS for time limit message
        output_path = f"static/response_{call_sid}.mp3"
        try:
            audio_gen = elevenlabs_client.text_to_speech.convert(
                voice_id=voice_id,
                text=reply,
                model_id=MODELS["elevenlabs"]["voice_model"],
                output_format=MODELS["elevenlabs"]["output_format"]
            )
            raw_audio = b""
            for chunk in audio_gen:
                if chunk:
                    raw_audio += chunk
            
            with open(output_path, "wb") as f:
                f.write(raw_audio)
            print(f"✅ Time limit audio generated: {len(raw_audio)} bytes")
        except Exception as e:
            print(f"🛑 ElevenLabs error for time limit message: {e}")
        
        # Create ready flag
        with open(f"static/response_ready_{call_sid}.txt", "w") as f:
            f.write("ready")
        
        # Mark that time limit message was delivered
        active_sessions[call_sid] = None
        
        # Return response that will play message and then hangup
        response = VoiceResponse()
        response.play(f"{request.url_root}static/response_{call_sid}.mp3")
        response.pause(length=1)  # Brief pause before hanging up
        response.say("Goodbye!")  # Fallback in case audio fails
        response.hangup()
        
        # Cleanup will happen via status callback
        print(f"🔚 Ending call {call_sid} after time limit message")
        
        return str(response)

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
        system_prompt = "You're a casual, sarcastic friend. Keep it light, keep it fun.\n\nYou have access to 'check_remaining_time' tool. Use it when:\n- They ask 'how long can we chat?' or any time-related question\n- After about 2 minutes of good conversation\n- When the conversation is naturally winding down\n\nExample tool usage:\n- User: 'Do we have a time limit?' → 'Oh yeah, let me check that' then use the tool\n- Proactively: 'Actually, let me see how much time we have left to hang out' then use the tool\n- Casual: 'Before we wrap this up, lemme check your time real quick' then use the tool\n\nKeep it casual: 'Looks like we got X minutes left to chill' or 'Only X minutes? Time flies when you're having fun!'"
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
            voice_id = voice_choice["voice_id"]
            print(f"🎭 Interview: Assigned new voice {voice_choice['name']} ({voice_id}) to {call_sid}")
        else:
            # IMPORTANT: Reuse the same voice throughout the interview
            voice_choice = personality_memory[call_sid]
            voice_id = voice_choice["voice_id"]
        
        system_prompt = (
            f"You are {voice_choice['name']}, a friendly, conversational job interviewer helping candidates practice for real interviews. "
            "Speak casually — like you're talking to someone over coffee, not in a formal evaluation. Ask one interview-style question at a time, and after each response, give supportive, helpful feedback. "
            "If their answer is weak, say 'Let's try that again' and re-ask the question. If it's strong, give a quick reason why it's good. "
            "Briefly refer to the STAR method (Situation, Task, Action, Result) when giving feedback, but don't lecture. Keep your tone upbeat, natural, and keep the conversation flowing. "
            "Don't ask if they're ready for the next question — just move on with something like, 'Alright, next one,' or 'Cool, here's another one.'"
            "\n\nYou have access to 'check_remaining_time' tool. Use it when:\n"
            "- The candidate asks about time ('How long do I have?', 'What's my time limit?')\n"
            "- After covering 3-4 questions (to give them a time update)\n"
            "- When you sense they might be rushing\n\n"
            "Example tool usage:\n"
            "- User: 'How many more questions can we do?' → 'Good question, let me check your time' then use the tool\n"
            "- After question 3: 'Let's see how we're doing on time' then use the tool\n"
            "- If they seem rushed: 'No need to rush - let me check how much time you have' then use the tool\n\n"
            "Frame time updates professionally: 'You have X minutes left, which is perfect for Y more questions' or 'With X minutes remaining, let's focus on your strongest examples.'"
        )
        intro_line = "Great, let's jump in! Can you walk me through your most recent role and responsibilities?"

    else:
        voice_id = "1t1EeRixsJrKbiF1zwM6"
        system_prompt = "You're a helpful assistant.\n\nYou have access to 'check_remaining_time' tool. Use it when:\n- The user asks about their remaining time or practice limit\n- After substantial conversation\n- Before ending the interaction\n\nExample tool usage:\n- User: 'How much time do I have?' → Use the tool immediately\n- Proactively: 'Let me check how much practice time you have left' then use the tool\n- Helpful: 'I'll check your remaining time to make sure we cover everything' then use the tool\n\nBe supportive: 'You have X minutes left - how can I best help you in that time?' or 'With X minutes remaining, let's focus on what matters most to you.'"
        intro_line = "How can I help you today?"

    # Manage turn count and conversation history
    turn = turn_count.get(call_sid, 0)
    turn_count[call_sid] = turn + 1
    conversation_history.setdefault(call_sid, [])

    # Generate response
    if turn == 0:
        # Get intro line from voice_lock if available, otherwise use defaults
        if call_sid in voice_lock and "intro_line" in voice_lock[call_sid]:
            reply = voice_lock[call_sid]["intro_line"]
        else:
            # Fallback intro lines based on mode
            if mode == "cold_call" or mode == "customer_convo":
                if call_sid in personality_memory:
                    persona_name = personality_memory[call_sid]
                    persona = cold_call_personality_pool.get(persona_name, {})
                    reply = persona.get("intro_line", "Alright, I'll be your customer. Start the conversation however you want — this could be a cold call, a follow-up, a check-in, or even a tough conversation. I'll respond based on my personality. If you ever want to start over, just say 'let's start over.'")
                else:
                    reply = "Alright, I'll be your customer. Start the conversation however you want."
            elif mode == "small_talk":
                reply = "Yo yo yo, how's it goin'?"
            elif mode == "interview":
                reply = "Great, let's jump in! Can you walk me through your most recent role and responsibilities?"
            else:
                reply = "How can I help you today?"
        
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
                    # Add tool support to existing call
                    gpt_reply = sync_openai.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=messages,
                        temperature=0.7,
                        max_tokens=150,
                        tools=[get_time_check_tool()],
                        tool_choice="auto"
                    )
                    
                    response_message = gpt_reply.choices[0].message
                    
                    # Check for tool calls
                    if response_message.tool_calls:
                        tool_call = response_message.tool_calls[0]
                        if tool_call.function.name == "check_remaining_time":
                            tool_result = handle_tool_call(call_sid)
                            
                            messages.append(response_message)
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": tool_result
                            })
                            
                            final_completion = sync_openai.chat.completions.create(
                                model="gpt-3.5-turbo",
                                messages=messages,
                                temperature=0.7,
                                max_tokens=150
                            )
                            reply = final_completion.choices[0].message.content.strip()
                    else:
                        reply = response_message.content.strip()
            except Exception as e:
                print(f"💥 GPT error: {e}")
                # Fallback response
                reply = "I'm having a bit of trouble understanding. Could you say that again?"
                
        # Clean up response
        reply = reply.replace("*", "").replace("_", "").replace("`", "").replace("#", "").replace("-", " ")
        conversation_history[call_sid].append({"role": "assistant", "content": reply})

    # Use mode from voice_lock if available (for consistency)
    if call_sid in voice_lock and "mode" in voice_lock[call_sid]:
        mode = voice_lock[call_sid]["mode"]
    
    print(f"🔣 Generating voice with ID: {voice_id}")
    
    # Log which persona/character is speaking
    if mode == "cold_call" or mode == "customer_convo":
        persona_name = personality_memory.get(call_sid, "Unknown")
        print(f"🎭 Speaking as: {persona_name}")
    elif mode == "interview":
        interviewer = personality_memory.get(call_sid, {})
        if isinstance(interviewer, dict):
            print(f"🎭 Speaking as: {interviewer.get('name', 'Unknown')} (Interviewer)")
    elif mode == "small_talk":
        print(f"🎭 Speaking as: Casual Friend")
    
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
                    model_id=MODELS["elevenlabs"]["voice_model"],
                    output_format=MODELS["elevenlabs"]["output_format"]
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
                                model_id=MODELS["elevenlabs"]["voice_model"],
                                output_format=MODELS["elevenlabs"]["output_format"]
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

    # Check if this is the final message before ending
    is_ending = False
    with state_lock:
        if active_sessions.get(call_sid) == "ENDING":
            is_ending = True
            print(f"🔚 This is the final message before ending call {call_sid}")

    # Build response
    response = VoiceResponse()
    
    if is_ending:
        # Play the goodbye message and then hangup
        response.play(f"{request.url_root}static/response_{call_sid}.mp3")
        response.pause(length=1)
        response.hangup()
        
        # Send SMS after goodbye
        if call_sid in active_streams:
            from_number = active_streams[call_sid].get('from_number')
            if from_number:
                usage_data = read_user_usage(from_number)
                is_first_call = usage_data['total_calls'] <= 1
                print(f"📤 Sending SMS after AI goodbye")
                send_convoreps_sms_link(from_number, is_first_call=is_first_call)
    else:
        # Normal flow - continue conversation
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
                        model=MODELS["openai"]["streaming_transcribe"],
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
                model=MODELS["openai"]["standard_transcribe"],
                file=f
            )
            return result.text.strip()
                
    except Exception as e:
        print(f"💥 Transcription error: {e}")
        raise

async def streaming_gpt_response(messages: list, voice_id: str, call_sid: str) -> str:
    """Stream GPT response and generate TTS concurrently with tool support"""
    try:
        model = MODELS["openai"]["streaming_gpt"] if USE_STREAMING else MODELS["openai"]["standard_gpt"]
        
        if USE_STREAMING and SENTENCE_STREAMING:
            # Create output file immediately
            output_path = f"static/response_{call_sid}.mp3"
            temp_path = f"static/response_{call_sid}_temp.mp3"
            
            # Streaming with sentence detection
            stream = await async_openai.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                temperature=0.7,
                tools=[get_time_check_tool()],
                tool_choice="auto"
            )
            
            full_response = ""
            sentence_buffer = ""
            sentence_count = 0
            first_audio_saved = False
            tool_calls = []
            current_tool_call = None
            
            async for chunk in stream:
                # Handle tool calls
                if chunk.choices[0].delta.tool_calls:
                    for tc_delta in chunk.choices[0].delta.tool_calls:
                        if tc_delta.index == 0:
                            if tc_delta.id:
                                current_tool_call = {
                                    "id": tc_delta.id,
                                    "name": tc_delta.function.name if tc_delta.function.name else "",
                                    "arguments": ""
                                }
                            if tc_delta.function and tc_delta.function.arguments:
                                current_tool_call["arguments"] += tc_delta.function.arguments
                
                # Process content
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
            
            # Handle tool call if present
            if current_tool_call and current_tool_call.get("name") == "check_remaining_time":
                tool_result = handle_tool_call(call_sid)
                
                # Check if time expired
                if "TIME_EXPIRED" in tool_result:
                    print(f"⏰ Tool detected time expired - AI will say goodbye")
                    with state_lock:
                        active_sessions[call_sid] = "ENDING"
                
                messages.append({
                    "role": "assistant",
                    "content": full_response if full_response else None,
                    "tool_calls": [{
                        "id": current_tool_call["id"],
                        "type": "function",
                        "function": {
                            "name": "check_remaining_time",
                            "arguments": "{}"
                        }
                    }]
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": current_tool_call["id"],
                    "content": tool_result
                })
                
                # Get final response
                final_stream = await async_openai.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True,
                    temperature=0.7
                )
                
                final_response = ""
                async for chunk in final_stream:
                    if chunk.choices[0].delta.content:
                        final_response += chunk.choices[0].delta.content
                
                return final_response.strip() if final_response else full_response.strip()
            
            return full_response.strip()
            
        else:
            # Non-streaming with tool support
            completion = await async_openai.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.7,
                tools=[get_time_check_tool()],
                tool_choice="auto"
            )
            
            response_message = completion.choices[0].message
            
            if response_message.tool_calls:
                tool_call = response_message.tool_calls[0]
                if tool_call.function.name == "check_remaining_time":
                    tool_result = handle_tool_call(call_sid)
                    
                    # Check if time expired
                    if "TIME_EXPIRED" in tool_result:
                        print(f"⏰ Tool detected time expired - AI will say goodbye")
                        with state_lock:
                            active_sessions[call_sid] = "ENDING"
                    
                    messages.append(response_message)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result
                    })
                    
                    final_completion = await async_openai.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=0.7
                    )
                    return final_completion.choices[0].message.content.strip()
            
            return response_message.content.strip()
            
    except Exception as e:
        print(f"💥 GPT streaming error: {e}")
        # Fallback to non-streaming
        completion = sync_openai.chat.completions.create(
            model=MODELS["openai"]["standard_gpt"],
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
                    model_id=MODELS["elevenlabs"]["voice_model"],
                    output_format=MODELS["elevenlabs"]["output_format"],
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
                    model_id=MODELS["elevenlabs"]["voice_model"],
                    output_format=MODELS["elevenlabs"]["output_format"]
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
                        model_id=MODELS["elevenlabs"]["voice_model"],
                        output_format=MODELS["elevenlabs"]["output_format"]
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
    # Ensure static directory and files exist
    ensure_static_files()
    
    # Initialize CSV for minute tracking
    init_csv()
    print(f"📊 CSV tracking enabled at: {USAGE_CSV_PATH}")
    
    print("\n🚀 ConvoReps Streaming Edition")
    print(f"   USE_STREAMING: {USE_STREAMING}")
    print(f"   SENTENCE_STREAMING: {SENTENCE_STREAMING}")
    print(f"   STREAMING_TIMEOUT: {STREAMING_TIMEOUT}s")
    print(f"\n📦 Model Configuration:")
    print(f"   OpenAI Streaming: {MODELS['openai']['streaming_gpt']}")
    print(f"   OpenAI Standard: {MODELS['openai']['standard_gpt']}")
    print(f"   Transcription: {MODELS['openai']['streaming_transcribe']} / {MODELS['openai']['standard_transcribe']}")
    print(f"   ElevenLabs: {MODELS['elevenlabs']['voice_model']}")
    print(f"\n🎭 Voice Consistency:")
    print(f"   Once a mode is selected, the same voice is used throughout the call")
    print(f"   Cold Call: One of 5 personas (Jerry, Miranda, Junior, Brett, Kayla)")
    print(f"   Interview: One of 3 interviewers (Rachel, Clyde, Stephen)")
    print(f"   Small Talk: Casual friend voice")
    print("\n")
    
    print(f"🏁 Application started on port 5050")
    print(f"📊 Free minutes per user: {FREE_CALL_MINUTES}")
    print(f"⏱️ Minimum call duration: {MIN_CALL_DURATION} minutes")
    print(f"\n📞 IMPORTANT: Configure your Twilio phone number with:")
    print(f"   Voice Configuration:")
    print(f"   - A CALL COMES IN → Webhook: https://your-domain.com/voice (HTTP POST)")
    print(f"   - CALL STATUS CHANGES → https://your-domain.com/call_status (HTTP POST)")
    print(f"   - PRIMARY HANDLER FAILS → Leave empty")
    print(f"\n   This ensures proper call tracking and cleanup!")
    
    app.run(host="0.0.0.0", port=5050)
