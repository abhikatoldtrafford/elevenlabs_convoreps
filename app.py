"""
ConvoReps FastAPI OpenAI Realtime Edition - Production Ready v3.0
Based on working implementation with minimal modifications

This version uses as much of the working code as possible, only adding
ConvoReps-specific features on top of the proven pattern.

Author: ConvoReps Team
Version: 3.0 (Based on Working Implementation)
"""

import os
import json
import base64
import asyncio
import websockets
import csv
import uuid
import logging
import signal
import tempfile
import shutil
import threading
import time
import re
import gc
import random
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple, Set
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Play, Pause
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv
import openai
import uvicorn

try:
    from pydub.generators import Sine
    from pydub import AudioSegment
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False
    print("⚠️ pydub not available, beep generation disabled")

# Load environment variables
load_dotenv()

# Configure logging - matching working implementation
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
PORT = int(os.getenv('PORT', 5050))

# ConvoReps specific configuration
FREE_CALL_MINUTES = float(os.getenv("FREE_CALL_MINUTES", "5.0"))
MIN_CALL_DURATION = float(os.getenv("MIN_CALL_DURATION", "0.5"))
USAGE_CSV_PATH = os.getenv("USAGE_CSV_PATH", "user_usage.csv")
USAGE_CSV_BACKUP_PATH = os.getenv("USAGE_CSV_BACKUP_PATH", "user_usage_backup.csv")
CONVOREPS_URL = os.getenv("CONVOREPS_URL", "https://convoreps.com")
MAX_WEBSOCKET_CONNECTIONS = int(os.getenv("MAX_WEBSOCKET_CONNECTIONS", "50"))

# Twilio configuration
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')

# Voice configuration - ConvoReps voices
VOICE = 'echo'  # Default voice
LOG_EVENT_TYPES = [
    'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'response.create', 'session.created'
]
SHOW_TIMING_MATH = False

# Initialize clients
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)
openaiclient = openai.OpenAI(api_key=OPENAI_API_KEY)

# Validate configuration
if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

# Initialize FastAPI app
app = FastAPI()

# Thread pool for async operations
executor = ThreadPoolExecutor(max_workers=5)

# Global state management
state_lock = threading.Lock()
active_streams: Dict[str, Dict[str, Any]] = {}
call_start_times: Dict[str, float] = {}
call_timers: Dict[str, threading.Timer] = {}
sms_sent_flags: Dict[str, bool] = {}
conversation_transcripts: Dict[str, List[Dict[str, str]]] = {}
personality_memory: Dict[str, Any] = {}
interview_question_index: Dict[str, int] = {}
mode_lock: Dict[str, str] = {}
turn_count: Dict[str, int] = {}
active_sessions: Dict[str, bool] = {}

# Metrics
metrics = {
    "total_calls": 0,
    "active_calls": 0,
    "failed_calls": 0,
    "sms_sent": 0,
    "sms_failed": 0,
    "tool_calls_made": 0,
    "api_errors": 0
}
metrics_lock = threading.Lock()

# CSV lock
csv_lock = threading.Lock()

# Voice profiles for ConvoReps
cold_call_personality_pool = {
    "Jerry": {
        "voice": "ash",
        "system_prompt": """You're Jerry, a skeptical small business owner. Be direct but not rude. Stay in character. Keep responses SHORT - 1-2 sentences max.

You have access to a tool called 'check_remaining_time' that tells you how many minutes are left in the user's free call.

WHEN TO USE THE TOOL:
- If user asks "how much time do I have?" or similar questions about time
- If the conversation has gone on for more than 3 minutes
- If user mentions wrapping up, ending the call, or asks if there's a time limit

HOW TO RESPOND WITH TIME INFO:
- If > 2 minutes left: "You've got about X minutes left to practice."
- If < 2 minutes: "Just so you know, you have about X minutes remaining."
- If < 30 seconds: "Looks like your time is almost up - maybe X seconds left."

NEVER mention the tool by name, just naturally work the time info into your response. Stay in character as Jerry - be direct about it."""
    },
    "Miranda": {
        "voice": "echo",
        "system_prompt": """You're Miranda, a busy office manager. No time for fluff. Be grounded and real. Keep responses SHORT - 1-2 sentences max.

You have access to a tool called 'check_remaining_time' that tells you how many minutes are left in the user's free call.

WHEN TO USE THE TOOL:
- If user asks about time or mentions time limits
- If conversation has been going on for a while (3+ minutes)
- If user seems to be wrapping up

HOW TO RESPOND:
- Be direct: "You've got X minutes left."
- If low on time: "Just X minutes remaining, FYI."
- Stay in character - you're busy, so be matter-of-fact about it."""
    },
    "Brett": {
        "voice": "sage",
        "system_prompt": """You're Brett, a contractor answering mid-job. Busy and a bit annoyed. Talk rough, fast, casual. Keep responses SHORT.

You have access to a tool called 'check_remaining_time' for checking remaining call time.

WHEN TO USE IT:
- If asked about time
- If call's been going on too long (you're busy!)
- If they're rambling

HOW TO RESPOND:
- Gruff: "Look, you got X minutes left."
- If low: "Time's almost up - X minutes."
- Stay annoyed but informative."""
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

# CSV filename for continuous logging - matching working implementation
CSV_FILE_NAME = "conversation_transcript.csv"

# Initialize directories
os.makedirs("static", exist_ok=True)
os.makedirs(os.path.dirname(USAGE_CSV_PATH) or ".", exist_ok=True)

# Validation functions
def validate_phone_number(phone_number: str) -> bool:
    """Validate phone number format"""
    if not phone_number:
        return False
    pattern = r'^\+[1-9]\d{1,14}$'
    return bool(re.match(pattern, phone_number))

def validate_call_sid(call_sid: str) -> bool:
    """Validate Twilio Call SID format"""
    if not call_sid:
        return False
    pattern = r'^CA[0-9a-fA-F]{32}$'
    return bool(re.match(pattern, call_sid))

def sanitize_csv_value(value: str) -> str:
    """Sanitize values for CSV to prevent injection"""
    if not value:
        return ""
    value = str(value).replace('\r', '').replace('\n', ' ')
    value = value.replace('"', '""')
    if value.strip().startswith(('=', '+', '-', '@')):
        value = "'" + value
    return value

# CSV functions for ConvoReps
def init_csv():
    """Initialize CSV file if it doesn't exist"""
    try:
        if not os.path.exists(USAGE_CSV_PATH):
            with open(USAGE_CSV_PATH, 'w', newline='') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['phone_number', 'minutes_used', 'minutes_left', 'last_call_date', 'total_calls'])
            logger.info(f"Created usage tracking CSV: {USAGE_CSV_PATH}")
    except Exception as e:
        logger.error(f"Error creating CSV file: {e}")

def create_csv_backup():
    """Create backup of CSV file"""
    try:
        if os.path.exists(USAGE_CSV_PATH):
            shutil.copy2(USAGE_CSV_PATH, USAGE_CSV_BACKUP_PATH)
            logger.info("CSV backup created")
    except Exception as e:
        logger.error(f"Error creating CSV backup: {e}")

def read_user_usage(phone_number: str) -> Dict[str, Any]:
    """Read user usage from CSV"""
    if not validate_phone_number(phone_number):
        logger.warning(f"Invalid phone number format: {phone_number}")
        return {
            'phone_number': phone_number,
            'minutes_used': 0.0,
            'minutes_left': FREE_CALL_MINUTES,
            'last_call_date': '',
            'total_calls': 0
        }
    
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
        logger.error(f"Error reading CSV: {e}")
    
    return {
        'phone_number': phone_number,
        'minutes_used': 0.0,
        'minutes_left': FREE_CALL_MINUTES,
        'last_call_date': '',
        'total_calls': 0
    }

def update_user_usage(phone_number: str, minutes_used: float):
    """Update user usage in CSV with atomic writes"""
    if not validate_phone_number(phone_number) or minutes_used <= 0:
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
                        
                        rows.append(row)
            except FileNotFoundError:
                fieldnames = ['phone_number', 'minutes_used', 'minutes_left', 'last_call_date', 'total_calls']
            
            if not user_found:
                new_user = {
                    'phone_number': sanitize_csv_value(phone_number),
                    'minutes_used': str(round(minutes_used, 2)),
                    'minutes_left': str(round(max(0, FREE_CALL_MINUTES - minutes_used), 2)),
                    'last_call_date': datetime.now().isoformat(),
                    'total_calls': '1'
                }
                rows.append(new_user)
            
            with tempfile.NamedTemporaryFile(mode='w', delete=False, newline='') as tmp_file:
                writer = csv.DictWriter(tmp_file, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
                temp_name = tmp_file.name
            
            shutil.move(temp_name, USAGE_CSV_PATH)
            
        logger.info(f"Updated usage for {phone_number}: {minutes_used:.2f} minutes used")
                
    except Exception as e:
        logger.error(f"Error updating CSV: {e}")

# Helper function to append transcript - from working implementation
def append_transcript_to_csv(role: str, transcript: str, stream_sid: str):
    """
    Appends a single row to a CSV file. Each row will contain:
    - streamSid (which call)
    - role (assistant/user)
    - transcript text
    """
    file_exists = os.path.isfile(CSV_FILE_NAME)

    with open(CSV_FILE_NAME, mode="a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        if not file_exists:
            writer.writerow(["streamSid", "role", "transcript"])

        writer.writerow([stream_sid, role, transcript])

# SMS functions
def parse_sms_instructions_from_transcript():
    """Parse SMS instructions from transcript"""
    if not os.path.exists(CSV_FILE_NAME):
        return None, None
        
    df = pd.read_csv(CSV_FILE_NAME, header=None, names=["streamSid", "role", "transcript"])
    
    try:
        sms_instruction_rows = df[df['transcript'].str.contains(r'SMS sent to', case=False, na=False)]
        if sms_instruction_rows.empty:
            print("No lines found with 'SMS sent to'")
            return None, None

        relevant_lines = "\n".join(sms_instruction_rows['transcript'].tolist())

        system_prompt = """
        You are a parser agent. You will receive a transcript of a conversation that may contain:
        "SMS sent to <phone_number> : <sms_body>".

        Your job is to extract two fields:
        1) phone_number (valid phone number)
        2) sms_body (the actual message text)

        ***OUTPUT JSON***
        {'phone_number': 'extracted_phone_number',
        'sms_body': 'extracted_sms_body'}

        PS: If multiple lines are provided, parse the last one in the text.
        PS: If no valid pattern is found, output an empty JSON.
        """

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Parse this: {relevant_lines}"}
        ]
        
        response = openaiclient.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=150,
            temperature=0.7,
            response_format={"type": "json_object"}
        )

        parsed_json = json.loads(response.choices[0].message.content.strip())
        phone_number = parsed_json.get("phone_number")
        sms_body = parsed_json.get("sms_body")

        if not phone_number or not sms_body:
            return None, None

        return phone_number, sms_body

    except Exception as e:
        print(f"Error parsing SMS: {e}")
        return None, None

def send_sms_via_twilio():
    """Send SMS using Twilio"""
    phone_number, sms_body = parse_sms_instructions_from_transcript()
    if not phone_number or not sms_body:
        print("No valid SMS instructions found. Not sending SMS.")
        return
        
    print(f"Attempting to send SMS to: {phone_number}")
    print(f"SMS Body: {sms_body}")
    
    try:
        message = twilio_client.messages.create(
            body=sms_body,
            from_=TWILIO_PHONE_NUMBER,
            to=phone_number,
        )
        print("Message sent successfully:", message.sid)
        
        with metrics_lock:
            metrics['sms_sent'] += 1
            
    except Exception as e:
        print(f"Error sending SMS via Twilio: {e}")
        with metrics_lock:
            metrics['sms_failed'] += 1

async def send_convoreps_sms_link(phone_number: str, is_first_call: bool = True):
    """Send ConvoReps specific SMS"""
    try:
        if not validate_phone_number(phone_number):
            return
        
        with state_lock:
            if sms_sent_flags.get(phone_number, False):
                logger.info(f"SMS already sent to {phone_number}")
                return
            sms_sent_flags[phone_number] = True
        
        if is_first_call:
            message_body = (
                "Nice work on your first ConvoReps call! Ready for more? "
                "Get 30 extra minutes for $6.99 — no strings: convoreps.com"
            )
        else:
            message_body = (
                "Hey! You've already used your free call. Here's link to grab "
                "a Starter Pass for $6.99 and unlock more time: convoreps.com"
            )
        
        message = twilio_client.messages.create(
            body=message_body,
            from_=TWILIO_PHONE_NUMBER,
            to=phone_number
        )
        
        logger.info(f"ConvoReps SMS sent to {phone_number}: {message.sid}")
        
        with metrics_lock:
            metrics['sms_sent'] += 1
            
    except Exception as e:
        logger.error(f"SMS error: {e}")
        with metrics_lock:
            metrics['sms_failed'] += 1
        with state_lock:
            sms_sent_flags[phone_number] = False

# Mode detection
def detect_intent(text: str) -> str:
    """Detect conversation mode from text"""
    lowered = text.lower()
    
    cold_call_patterns = [
        r"cold\s*call", r"customer\s*call", r"sales\s*call", 
        r"business\s*call", r"practice\s*calling"
    ]
    interview_patterns = [
        r"interview", r"job\s*interview", r"interview\s*prep",
        r"practice\s*interview", r"mock\s*interview"
    ]
    small_talk_patterns = [
        r"small\s*talk", r"chat", r"casual\s*talk",
        r"conversation", r"just\s*talk"
    ]
    
    if any(re.search(pattern, lowered) for pattern in cold_call_patterns):
        return "cold_call"
    elif any(re.search(pattern, lowered) for pattern in interview_patterns):
        return "interview"
    elif any(re.search(pattern, lowered) for pattern in small_talk_patterns):
        return "small_talk"
    
    return "cold_call"

def get_personality_for_mode(call_sid: str, mode: str) -> Tuple[str, str, str]:
    """Get personality configuration for mode"""
    if mode == "cold_call":
        with state_lock:
            if call_sid not in personality_memory:
                persona_name = random.choice(list(cold_call_personality_pool.keys()))
                personality_memory[call_sid] = persona_name
            else:
                persona_name = personality_memory[call_sid]
            
        persona = cold_call_personality_pool[persona_name]
        return (
            persona["voice"],
            persona["system_prompt"],
            "Hello?"
        )
        
    elif mode == "interview":
        voice = "alloy"
        system_prompt = (
            f"You are Rachel, a friendly, conversational job interviewer. "
            "Ask one interview question at a time, give supportive feedback. "
            "Keep your tone upbeat and natural. Keep responses SHORT - 1-2 sentences. "
            "Be encouraging and professional.\n\n"
            "You have access to 'check_remaining_time' tool. If the interview has been going on "
            "for more than 4 minutes, casually check and mention time remaining: "
            "'We're making good progress - just to let you know, we have about X minutes left.'"
        )
        
        return (
            voice,
            system_prompt,
            "Great, let's start. Tell me about yourself."
        )
        
    else:  # small_talk
        return (
            "shimmer",
            "You're a casual, sarcastic friend. Keep it light and fun. Use humor and be relatable. SHORT responses only - 1-2 sentences max.",
            "Hey, what's up?"
        )

# Timer handling
def handle_time_limit(call_sid: str, from_number: str):
    """Handle when free time limit is reached"""
    logger.info(f"Time limit reached for {call_sid}")
    
    # Update CSV immediately
    if call_sid in call_start_times:
        elapsed_minutes = (time.time() - call_start_times[call_sid]) / 60.0
        update_user_usage(from_number, elapsed_minutes)
        logger.info(f"Updated usage for {from_number}: {elapsed_minutes:.2f} minutes")
    
    # Mark session for time limit message
    with state_lock:
        if call_sid in active_sessions:
            active_sessions[call_sid] = False

def cleanup_call_resources(call_sid: str):
    """Clean up call resources"""
    with state_lock:
        if call_sid in active_streams:
            from_number = active_streams[call_sid].get('from_number', '')
            
            # Update usage if not already done
            if call_sid in call_start_times and call_sid not in call_timers:
                call_duration = time.time() - call_start_times[call_sid]
                minutes_used = call_duration / 60.0
                
                if from_number and validate_phone_number(from_number) and minutes_used > 0.01:
                    update_user_usage(from_number, minutes_used)
                    logger.info(f"Call {call_sid} lasted {minutes_used:.2f} minutes")
                    
                    # Check if SMS needed
                    usage_after = read_user_usage(from_number)
                    if usage_after['minutes_left'] <= 0.5:
                        is_first_call = usage_after['total_calls'] <= 1
                        asyncio.create_task(send_convoreps_sms_link(from_number, is_first_call=is_first_call))
                
                call_start_times.pop(call_sid, None)
            
            # Cancel timer
            if call_sid in call_timers:
                call_timers[call_sid].cancel()
                call_timers.pop(call_sid, None)
            
            # Clean up state
            active_streams.pop(call_sid, None)
            active_sessions.pop(call_sid, None)
            turn_count.pop(call_sid, None)
            conversation_transcripts.pop(call_sid, None)
            personality_memory.pop(call_sid, None)
            mode_lock.pop(call_sid, None)
            interview_question_index.pop(call_sid, None)
            
            # Clear SMS flag after delay
            if from_number:
                def clear_sms_flag():
                    time.sleep(300)  # 5 minutes
                    with state_lock:
                        sms_sent_flags.pop(from_number, None)
                threading.Thread(target=clear_sms_flag, daemon=True).start()
    
    with metrics_lock:
        metrics['active_calls'] = max(0, metrics['active_calls'] - 1)

# HTTP endpoints - matching working implementation structure
@app.get("/", response_class=HTMLResponse)
async def index_page():
    return "<html><body><h1>ConvoReps Realtime Server is running!</h1></body></html>"

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    logger.info("Received incoming call request from: %s", request.client.host)
    
    # Get form data
    form_data = await request.form()
    
    # Get parameters
    call_sid = form_data.get("CallSid")
    from_number = form_data.get("From", "+10000000000")
    
    if not validate_call_sid(call_sid):
        logger.error(f"Invalid CallSid: {call_sid}")
        response = VoiceResponse()
        response.say("Sorry, there was an error processing your call.")
        response.hangup()
        return HTMLResponse(content=str(response), media_type="application/xml")
    
    logger.info(f"New call: {call_sid} from {from_number}")
    
    # Validate phone number
    if not validate_phone_number(from_number):
        logger.error(f"Invalid phone number: {from_number}")
        response = VoiceResponse()
        response.say("Sorry, we couldn't validate your phone number.")
        response.hangup()
        return HTMLResponse(content=str(response), media_type="application/xml")
    
    # Check free minutes
    usage_data = read_user_usage(from_number)
    is_repeat_caller = usage_data['total_calls'] > 0
    minutes_left = usage_data['minutes_left']
    
    # Require minimum call duration
    if minutes_left < MIN_CALL_DURATION and is_repeat_caller:
        response = VoiceResponse()
        response.say(
            "Hey! You've already used your free call. But no worries, we just texted you a link to Convoreps.com. "
            "Grab a Starter Pass for $6.99 and turn practice into real-world results.",
            voice="alice",
            language="en-US"
        )
        response.hangup()
        
        asyncio.create_task(send_convoreps_sms_link(from_number, is_first_call=False))
        
        return HTMLResponse(content=str(response), media_type="application/xml")
    
    # Initialize call state
    with state_lock:
        if call_sid not in turn_count:
            turn_count[call_sid] = 0
            call_start_times[call_sid] = time.time()
            conversation_transcripts[call_sid] = []
            active_sessions[call_sid] = True
            logger.info(f"User has {minutes_left:.2f} minutes remaining")
            
            # Store call data
            active_streams[call_sid] = {
                'from_number': from_number,
                'minutes_left': minutes_left,
                'last_activity': time.time()
            }
        else:
            turn_count[call_sid] += 1
            
        with metrics_lock:
            metrics['total_calls'] += 1
            metrics['active_calls'] += 1
    
    response = VoiceResponse()
    
    # Play greeting with beep
    if turn_count.get(call_sid, 0) == 0:
        if not is_repeat_caller:
            greeting_file = "first_time_greeting.mp3"
            logger.info("New caller detected — playing first-time greeting")
        else:
            greeting_file = "returning_user_greeting.mp3"
            logger.info("Returning caller — playing returning greeting")
        
        # Play greeting if file exists
        if os.path.exists(f"static/{greeting_file}"):
            response.play(f"/static/{greeting_file}")
        else:
            # Fallback TTS greeting
            if greeting_file == "first_time_greeting.mp3":
                response.say("Welcome to ConvoReps! Let's practice.", voice="alice")
            else:
                response.say("Welcome back! Ready to practice?", voice="alice")
        
        # Play beep sound
        if os.path.exists("static/beep.mp3"):
            response.play("/static/beep.mp3")
        response.pause(length=1)
    
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream')
    response.append(connect)
    
    # Set up timer
    timer_duration = minutes_left * 60 if minutes_left < FREE_CALL_MINUTES else FREE_CALL_MINUTES * 60
    logger.info(f"Setting timer for {timer_duration:.0f} seconds")
    
    timer = threading.Timer(timer_duration, lambda: handle_time_limit(call_sid, from_number))
    timer.start()
    
    with state_lock:
        call_timers[call_sid] = timer
    
    logger.info("Successfully created the TwiML response")
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("Client connected")
    await websocket.accept()

    openai_ws = None
    stream_sid = None
    call_sid = None
    
    try:
        # Connect to OpenAI using websockets library - matching working implementation
        openai_ws = await websockets.connect(
            f'wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}',
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1"
            }
        )
        
        # Connection-specific state
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None

        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, latest_media_timestamp, call_sid
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    
                    if data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        # Get call_sid from Twilio params
                        call_sid = data['start'].get('callSid')
                        if not call_sid:
                            # Try custom parameters
                            call_sid = data['start'].get('customParameters', {}).get('call_sid')
                        
                        print(f"Incoming stream has started {stream_sid} for call {call_sid}")
                        latest_media_timestamp = 0
                        
                        # Send session update
                        await send_session_update(openai_ws, call_sid)
                        
                        # Update stream data
                        if call_sid and call_sid in active_streams:
                            active_streams[call_sid]['stream_sid'] = stream_sid
                            active_streams[call_sid]['last_activity'] = time.time()

                    elif data['event'] == 'media':
                        latest_media_timestamp = int(data['media']['timestamp'])
                        
                        # Update activity
                        if call_sid and call_sid in active_streams:
                            active_streams[call_sid]['last_activity'] = time.time()
                        
                        # Check if time limit reached
                        if call_sid and call_sid in active_sessions and not active_sessions[call_sid]:
                            # Time limit reached, send final message
                            await send_time_limit_message(openai_ws, call_sid)
                            active_sessions[call_sid] = None  # Mark as handled
                            return
                        
                        # Forward audio to OpenAI
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))

                    elif data['event'] == 'mark':
                        # Mark event from Twilio
                        if mark_queue:
                            mark_queue.pop(0)

            except WebSocketDisconnect:
                print("Client disconnected.")
                send_sms_via_twilio()
                if os.path.exists(CSV_FILE_NAME):
                    os.remove(CSV_FILE_NAME)
            except Exception as e:
                logger.error(f"Error in WebSocket handling: {e}")
            finally:
                logger.info("Connection closed. Triggering SMS.")
                send_sms_via_twilio()
                if os.path.exists(CSV_FILE_NAME):
                    os.remove(CSV_FILE_NAME)

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio, call_sid
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)

                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)

                    # When we get audio from the assistant
                    if response.get('type') == 'response.audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                        await websocket.send_json(audio_delta)

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                            if SHOW_TIMING_MATH:
                                print(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                        # Update last_assistant_item
                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    # ADDED: Parse transcripts and append to CSV
                    if response.get('type') == 'response.done':
                        if 'response' in response and 'output' in response['response']:
                            for item in response['response']['output']:
                                role = item.get('role')
                                content = item.get('content', [])
                                for c in content:
                                    if c.get('type') == 'audio' and 'transcript' in c:
                                        append_transcript_to_csv(role, c['transcript'], stream_sid or "unknownSID")
                                        
                                        # Store in memory for ConvoReps
                                        if call_sid and call_sid in conversation_transcripts:
                                            conversation_transcripts[call_sid].append({
                                                'role': role,
                                                'content': c['transcript'],
                                                'timestamp': datetime.now().isoformat()
                                            })
                                            
                                            # Limit history
                                            if len(conversation_transcripts[call_sid]) > 50:
                                                conversation_transcripts[call_sid] = conversation_transcripts[call_sid][-50:]
                        
                        # Check for interview mode
                        if call_sid:
                            mode = mode_lock.get(call_sid, "cold_call")
                            if mode == "interview" and call_sid in interview_question_index:
                                # Advance to next question
                                interview_question_index[call_sid] = (
                                    interview_question_index.get(call_sid, 0) + 1
                                ) % len(interview_questions)

                    # Handle user speech transcription
                    elif response.get('type') == 'conversation.item.input_audio_transcription.completed':
                        transcript = response.get('transcript', '')
                        if transcript and call_sid:
                            # Log to CSV
                            append_transcript_to_csv('user', transcript, stream_sid or "unknownSID")
                            
                            # Store in memory
                            if call_sid in conversation_transcripts:
                                conversation_transcripts[call_sid].append({
                                    'role': 'user',
                                    'content': transcript,
                                    'timestamp': datetime.now().isoformat()
                                })
                                
                                # Limit history
                                if len(conversation_transcripts[call_sid]) > 50:
                                    conversation_transcripts[call_sid] = conversation_transcripts[call_sid][-50:]
                            
                            # Detect mode if not set
                            if call_sid not in mode_lock:
                                detected_mode = detect_intent(transcript)
                                mode_lock[call_sid] = detected_mode
                                
                                # Update session for mode
                                voice, system_prompt, _ = get_personality_for_mode(call_sid, detected_mode)
                                
                                # Initialize interview question index
                                if detected_mode == "interview" and call_sid not in interview_question_index:
                                    interview_question_index[call_sid] = 0
                                
                                # Update session
                                if detected_mode != "cold_call":
                                    if detected_mode == "interview":
                                        current_q_idx = interview_question_index.get(call_sid, 0)
                                        if current_q_idx < len(interview_questions):
                                            system_prompt += f"\n\nAsk this question next: {interview_questions[current_q_idx]}"
                                    
                                    session_update = {
                                        "type": "session.update",
                                        "session": {
                                            "voice": voice,
                                            "instructions": system_prompt
                                        }
                                    }
                                    await openai_ws.send(json.dumps(session_update))

                    # If user speech starts, we can interrupt the assistant
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id:{last_assistant_item}")
                            await handle_speech_started_event()

                    # Handle function calls
                    elif response.get('type') in ['response.function_call_arguments.done', 'response.done']:
                        if response.get('type') == 'response.done':
                            # Check response.output for function calls
                            outputs = response.get('response', {}).get('output', [])
                            for output in outputs:
                                if output.get('type') == 'function_call':
                                    await handle_function_call_from_response(output, openai_ws, call_sid)
                        else:
                            # Direct function_call_arguments.done event
                            await handle_function_call(response, openai_ws, call_sid)

            except WebSocketDisconnect:
                print("WebSocket to Twilio closed (send_to_twilio).")
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, sid):
            if sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        async def handle_function_call(response: dict, openai_ws, call_sid: str):
            """Handle function call from OpenAI"""
            function_name = response.get('name')
            call_id = response.get('call_id')
            
            if function_name == 'check_remaining_time' and call_sid:
                # Calculate remaining time
                if call_sid in call_start_times:
                    elapsed_minutes = (time.time() - call_start_times[call_sid]) / 60.0
                    minutes_left = active_streams[call_sid].get('minutes_left', FREE_CALL_MINUTES)
                    remaining = max(0, minutes_left - elapsed_minutes)
                    
                    # Create function output
                    function_output = {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps({
                                "minutes_remaining": round(remaining, 1)
                            })
                        }
                    }
                    
                    await openai_ws.send(json.dumps(function_output))
                    await openai_ws.send(json.dumps({"type": "response.create"}))
                    
                    with metrics_lock:
                        metrics['tool_calls_made'] += 1
                        
                    # Check if time is exhausted
                    if remaining <= MIN_CALL_DURATION:
                        logger.info(f"Tool detected time exhausted for {call_sid}")
                        handle_time_limit(call_sid, active_streams[call_sid].get('from_number'))

        async def handle_function_call_from_response(output: dict, openai_ws, call_sid: str):
            """Handle function call from response.done event output"""
            function_name = output.get('name')
            call_id = output.get('call_id')
            
            if function_name == 'check_remaining_time' and call_sid:
                await handle_function_call({'name': function_name, 'call_id': call_id}, openai_ws, call_sid)

        async def send_time_limit_message(openai_ws, call_sid: str):
            """Send time limit message through OpenAI"""
            message = (
                "Alright, that's the end of your free call — but this is only the beginning. "
                "We just texted you a link to ConvoReps.com. Whether you're prepping for interviews "
                "or sharpening your pitch, this is how you level up — don't wait, your next opportunity is already calling."
            )
            
            time_limit_item = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"Say exactly this to end the call: {message}"
                        }
                    ]
                }
            }
            
            await openai_ws.send(json.dumps(time_limit_item))
            await openai_ws.send(json.dumps({"type": "response.create"}))
            
            # Send SMS
            if call_sid in active_streams:
                from_number = active_streams[call_sid].get('from_number')
                if from_number:
                    usage_data = read_user_usage(from_number)
                    is_first_call = usage_data['total_calls'] <= 1
                    await send_convoreps_sms_link(from_number, is_first_call=is_first_call)
            
            # Schedule cleanup
            await asyncio.sleep(5)
            cleanup_call_resources(call_sid)

        # Use gather so both tasks run concurrently
        await asyncio.gather(receive_from_twilio(), send_to_twilio())

    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        with metrics_lock:
            metrics['api_errors'] += 1
    finally:
        if openai_ws:
            await openai_ws.close()
        if call_sid:
            cleanup_call_resources(call_sid)
        logger.info("WebSocket connection closed")

async def send_initial_conversation_item(openai_ws, greeting: str = None):
    """Send initial conversation item if AI talks first."""
    if not greeting:
        greeting = "Hello?"
        
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": f"Greet the user with: '{greeting}'"
                }
            ]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))

async def send_session_update(openai_ws, call_sid: str = None):
    """Send session update to OpenAI WebSocket."""
    # Get personality for mode
    mode = mode_lock.get(call_sid, "cold_call") if call_sid else "cold_call"
    voice, system_prompt, greeting = get_personality_for_mode(call_sid or "", mode)
    
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": voice,
            "instructions": system_prompt,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
            "tools": [
                {
                    "type": "function",
                    "name": "check_remaining_time",
                    "description": "Check how many minutes the user has left in their free call",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                }
            ],
            "tool_choice": "auto"
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

    await send_initial_conversation_item(openai_ws, greeting)

# Health and monitoring endpoints
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    with state_lock:
        active_count = len(active_streams)
    
    with metrics_lock:
        error_rate = metrics['api_errors'] / max(metrics['total_calls'], 1)
        api_health = "healthy" if error_rate < 0.1 else "degraded"
    
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "active_streams": active_count,
        "api_health": api_health,
        "version": "3.0-working-pattern",
        "realtime_model": OPENAI_REALTIME_MODEL
    }

# Serve static files
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    # Generate beep sound if missing
    if not os.path.exists("static/beep.mp3") and PYDUB_AVAILABLE:
        logger.warning("beep.mp3 not found - generating")
        try:
            beep = Sine(1000).to_audio_segment(duration=300).apply_gain(-6)
            beep.export("static/beep.mp3", format="mp3")
            logger.info("Generated beep.mp3")
        except Exception as e:
            logger.error(f"Could not generate beep.mp3: {e}")
    
    # Create placeholder greetings if missing
    for greeting_file in ["first_time_greeting.mp3", "returning_user_greeting.mp3"]:
        if not os.path.exists(f"static/{greeting_file}"):
            logger.warning(f"{greeting_file} not found - will use TTS fallback")
    
    # Initialize CSV
    init_csv()
    
    logger.info("\n🚀 ConvoReps FastAPI OpenAI Realtime Edition v3.0")
    logger.info("   Based on working implementation pattern")
    logger.info(f"   Model: {OPENAI_REALTIME_MODEL}")
    logger.info(f"   Free Minutes: {FREE_CALL_MINUTES}")
    logger.info(f"   Port: {PORT}")
    
    uvicorn.run(app, host='0.0.0.0', port=PORT)
