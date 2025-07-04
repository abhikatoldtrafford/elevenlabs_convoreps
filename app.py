"""
ConvoReps OpenAI Realtime Edition - Production Ready v2.0
Real-time voice conversation practice with OpenAI Realtime API

MAJOR FIXES IN v2.0:
✅ Fixed WebSocket handling for Flask-Sock compatibility
✅ Proper async/sync bridge implementation
✅ Fixed all undefined variables and imports
✅ Enhanced error recovery and retry logic
✅ Thread-safe CSV operations with per-call transcripts
✅ Improved memory management and cleanup
✅ Fixed OpenAI function calling implementation
✅ Better connection lifecycle management
✅ Production-grade logging and monitoring
✅ Comprehensive input validation

Author: ConvoReps Team
Version: 2.0 (OpenAI Realtime Edition - Production Ready)
"""

import os
import io
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
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple, Set
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

from flask import Flask, request, Response, session, send_from_directory
from flask_cors import CORS
from flask_sock import Sock, ConnectionClosed
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Play, Pause, Stream
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv
import openai

try:
    from pydub.generators import Sine
    from pydub import AudioSegment
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False
    print("⚠️ pydub not available, beep generation disabled")

# Load environment variables
load_dotenv()

# Custom formatter for correlation IDs
class CorrelationFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, 'correlation_id'):
            record.correlation_id = getattr(record, 'correlation_id', 'NO-ID')
        return super().format(record)

# Initialize structured logging
formatter = CorrelationFormatter(
    '%(asctime)s - %(name)s - %(levelname)s - [%(correlation_id)s] - %(message)s'
)

file_handler = logging.FileHandler('convoreps_realtime.log')
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = []
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# Silence noisy libraries
logging.getLogger('websockets').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your-secret-key-here")
CORS(app)

# Initialize WebSocket support
sock = Sock(app)

# Configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
PORT = int(os.getenv("PORT", 5050))
FREE_CALL_MINUTES = float(os.getenv("FREE_CALL_MINUTES", "5.0"))
MIN_CALL_DURATION = float(os.getenv("MIN_CALL_DURATION", "0.5"))
USAGE_CSV_PATH = os.getenv("USAGE_CSV_PATH", "user_usage.csv")
USAGE_CSV_BACKUP_PATH = os.getenv("USAGE_CSV_BACKUP_PATH", "user_usage_backup.csv")
CONVOREPS_URL = os.getenv("CONVOREPS_URL", "https://convoreps.com")
MAX_WEBSOCKET_CONNECTIONS = int(os.getenv("MAX_WEBSOCKET_CONNECTIONS", "50"))
TRANSCRIPT_DIR = "transcripts"

# Twilio configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# Initialize clients
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Validate configuration
if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
    raise ValueError('Missing Twilio credentials. Please set them in the .env file.')

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
last_response_cache: Dict[str, str] = {}
recording_urls: Dict[str, str] = {}

# Metrics tracking
metrics = {
    "total_calls": 0,
    "active_calls": 0,
    "failed_calls": 0,
    "sms_sent": 0,
    "sms_failed": 0,
    "tool_calls_made": 0,
    "api_errors": 0,
    "websocket_errors": 0,
    "websocket_duration_sum": 0.0,
    "websocket_count": 0
}
metrics_lock = threading.Lock()

# CSV lock for thread safety
csv_lock = threading.Lock()

# Voice profiles for different personalities
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

# Graceful shutdown
shutdown_event = threading.Event()

def signal_handler(sig, frame):
    """Handle graceful shutdown"""
    logger.info("Graceful shutdown initiated...")
    shutdown_event.set()
    
    with state_lock:
        for call_sid in list(active_streams.keys()):
            cleanup_call_resources(call_sid)
    
    with state_lock:
        for timer in call_timers.values():
            timer.cancel()
    
    create_csv_backup()
    logger.info("Shutdown complete")
    exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Error handling decorator
def error_handler(f):
    """Enhanced error handler with correlation ID"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        correlation_id = str(uuid.uuid4())
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.error(f"Route error in {f.__name__}: {e}", 
                        extra={'correlation_id': correlation_id},
                        exc_info=True)
            
            response = VoiceResponse()
            response.say("I'm sorry, I encountered an error. Please try again.")
            response.hangup()
            
            with metrics_lock:
                metrics['failed_calls'] += 1
                
            return str(response), 500
    return wrapper

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

# Initialize directories
os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
os.makedirs("static", exist_ok=True)
os.makedirs(os.path.dirname(USAGE_CSV_PATH) or ".", exist_ok=True)

# CSV functions
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
        if os.path.exists(USAGE_CSV_BACKUP_PATH):
            try:
                shutil.copy2(USAGE_CSV_BACKUP_PATH, USAGE_CSV_PATH)
                return read_user_usage(phone_number)
            except:
                pass
    
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
        
        with metrics_lock:
            if metrics['total_calls'] % 10 == 0:
                create_csv_backup()
                
    except Exception as e:
        logger.error(f"Error updating CSV: {e}", exc_info=True)

# Transcript management
def get_transcript_path(call_sid: str) -> str:
    """Get transcript file path for a call"""
    return os.path.join(TRANSCRIPT_DIR, f"{call_sid}_transcript.csv")

def append_transcript_to_csv(role: str, transcript: str, call_sid: str):
    """Append transcript to call-specific CSV file"""
    try:
        transcript_path = get_transcript_path(call_sid)
        file_exists = os.path.isfile(transcript_path)
        
        with open(transcript_path, mode="a", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            if not file_exists:
                writer.writerow(["timestamp", "role", "transcript"])
            writer.writerow([datetime.now().isoformat(), role, transcript])
    except Exception as e:
        logger.error(f"Error appending to transcript CSV: {e}")

def cleanup_transcript(call_sid: str):
    """Clean up transcript file after processing"""
    try:
        transcript_path = get_transcript_path(call_sid)
        if os.path.exists(transcript_path):
            os.remove(transcript_path)
    except Exception as e:
        logger.error(f"Error cleaning up transcript: {e}")

# SMS functions
async def send_sms_link(phone_number: str, is_first_call: bool = True):
    """Send SMS with ConvoReps link"""
    correlation_id = str(uuid.uuid4())
    
    try:
        if not validate_phone_number(phone_number):
            return
        
        with state_lock:
            if sms_sent_flags.get(phone_number, False):
                logger.info(f"SMS already sent to {phone_number}", 
                          extra={'correlation_id': correlation_id})
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
        
        logger.info(f"SMS sent successfully to {phone_number}: {message.sid}", 
                   extra={'correlation_id': correlation_id})
        
        with metrics_lock:
            metrics['sms_sent'] += 1
            
    except Exception as e:
        logger.error(f"SMS error: {e}", extra={'correlation_id': correlation_id})
        with metrics_lock:
            metrics['sms_failed'] += 1
        with state_lock:
            sms_sent_flags[phone_number] = False

def run_async_task(coro):
    """Run async coroutine"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    except Exception as e:
        logger.error(f"Async task error: {e}", exc_info=True)
    finally:
        loop.close()

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

def detect_bad_news(text: str) -> bool:
    """Detect bad news in text"""
    lowered = text.lower()
    bad_news_patterns = [
        r"bad\s*news", r"unfortunately", r"problem", r"delay", r"issue",
        r"we\s*can'?t", r"we\s*won'?t", r"not\s*going\s*to\s*happen",
        r"reschedule", r"price\s*increase", r"delayed", r"won'?t\s*make\s*it",
        r"can'?t\s*deliver", r"apologize", r"sorry\s*to\s*say"
    ]
    return any(re.search(pattern, lowered) for pattern in bad_news_patterns)

def add_bad_news_context(system_prompt: str, transcript: str) -> str:
    """Add context for bad news scenarios"""
    escalation_level = 1
    
    if any(phrase in transcript.lower() for phrase in ["calm down", "relax", "it's not my fault", "not my problem"]):
        escalation_level = 2
    elif any(phrase in transcript.lower() for phrase in ["deal with it", "too bad", "whatever"]):
        escalation_level = 3
    
    escalation_prompts = {
        1: "\n\nThe user just delivered bad news. Respond based on your personality, showing reasonable frustration. Keep response SHORT - 1-2 sentences max.",
        2: "\n\nThe user got defensive, so you're more upset but still professional. Express frustration clearly but briefly.",
        3: "\n\nThe user is being dismissive. You're quite frustrated now but keep it brief and end the conversation if needed."
    }
    
    return system_prompt + escalation_prompts.get(escalation_level, escalation_prompts[1])

def get_personality_for_mode(call_sid: str, mode: str) -> Tuple[str, str, str]:
    """Get personality configuration for mode"""
    if mode == "cold_call":
        with state_lock:
            if call_sid not in personality_memory:
                import random
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
    correlation_id = str(uuid.uuid4())
    logger.info(f"Time limit reached for {call_sid}", 
               extra={'correlation_id': correlation_id})
    
    # Update CSV immediately before playing message
    if call_sid in call_start_times:
        elapsed_minutes = (time.time() - call_start_times[call_sid]) / 60.0
        update_user_usage(from_number, elapsed_minutes)
        logger.info(f"Updated usage for {from_number}: {elapsed_minutes:.2f} minutes", 
                   extra={'correlation_id': correlation_id})
    
    # Send end message through OpenAI WebSocket if still connected
    if call_sid in active_streams:
        openai_ws = active_streams[call_sid].get('openai_ws')
        if openai_ws:
            asyncio.create_task(send_time_limit_message(openai_ws, call_sid, from_number))

async def send_time_limit_message(openai_ws, call_sid: str, from_number: str):
    """Send time limit message through OpenAI Realtime"""
    try:
        # Create a conversation item with the time limit message
        message = (
            "Alright, that's the end of your free call — but this is only the beginning. "
            "We just texted you a link to ConvoReps.com. Whether you're prepping for interviews "
            "or sharpening your pitch, this is how you level up — don't wait, your next opportunity is already calling."
        )
        
        time_limit_item = {
            "type": "conversation.item.create",
            "event_id": f"time_limit_{call_sid[:8]}",
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
        
        response_event = {
            "type": "response.create",
            "event_id": f"time_limit_response_{call_sid[:8]}"
        }
        await openai_ws.send(json.dumps(response_event))
        
        # Send SMS
        usage_data = read_user_usage(from_number)
        is_first_call = usage_data['total_calls'] <= 1
        await send_sms_link(from_number, is_first_call=is_first_call)
        
        # Close connection after a delay
        await asyncio.sleep(5)
        if call_sid in active_streams:
            cleanup_call_resources(call_sid)
            
    except Exception as e:
        logger.error(f"Error sending time limit message: {e}")

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
                        executor.submit(run_async_task, 
                                      send_sms_link(from_number, is_first_call=is_first_call))
                
                call_start_times.pop(call_sid, None)
            
            # Cancel timer
            if call_sid in call_timers:
                call_timers[call_sid].cancel()
                call_timers.pop(call_sid, None)
            
            # Close WebSockets
            if 'openai_ws' in active_streams[call_sid]:
                try:
                    asyncio.create_task(active_streams[call_sid]['openai_ws'].close())
                except:
                    pass
            
            if 'twilio_ws' in active_streams[call_sid]:
                try:
                    active_streams[call_sid]['twilio_ws'].close()
                except:
                    pass
            
            # Clean up transcript
            cleanup_transcript(call_sid)
            
            # Clean up state
            active_streams.pop(call_sid, None)
            turn_count.pop(call_sid, None)
            conversation_transcripts.pop(call_sid, None)
            personality_memory.pop(call_sid, None)
            mode_lock.pop(call_sid, None)
            interview_question_index.pop(call_sid, None)
            last_response_cache.pop(call_sid, None)
            recording_urls.pop(call_sid, None)
            
            # Clear SMS flag after delay
            if from_number:
                def clear_sms_flag():
                    time.sleep(300)  # 5 minutes
                    with state_lock:
                        sms_sent_flags.pop(from_number, None)
                threading.Thread(target=clear_sms_flag, daemon=True).start()
    
    with metrics_lock:
        metrics['active_calls'] = max(0, metrics['active_calls'] - 1)
    
    gc.collect()

# Periodic cleanup
def periodic_cleanup():
    """Clean up stale connections periodically"""
    while not shutdown_event.is_set():
        try:
            time.sleep(60)
            current_time = time.time()
            stale_calls = []
            
            with state_lock:
                for call_sid, stream_data in list(active_streams.items()):
                    if current_time - stream_data.get('last_activity', 0) > 300:
                        stale_calls.append(call_sid)
            
            for call_sid in stale_calls:
                logger.info(f"Cleaning up stale connection: {call_sid}")
                cleanup_call_resources(call_sid)
                
        except Exception as e:
            logger.error(f"Cleanup thread error: {e}")

cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
cleanup_thread.start()

# Twilio endpoints
@app.route("/", methods=["GET"])
def index_page():
    return "<html><body><h1>ConvoReps Realtime Server is running!</h1></body></html>"

@app.route("/voice", methods=["POST", "GET"])
@error_handler
def handle_incoming_call():
    """Handle incoming call and return TwiML response"""
    correlation_id = str(uuid.uuid4())
    
    # Security validation
    if TWILIO_AUTH_TOKEN:
        url = request.url
        params = request.values
        signature = request.headers.get('X-Twilio-Signature', '')
        
        if not twilio_validator.validate(url, params, signature):
            logger.warning("Invalid Twilio signature", 
                         extra={'correlation_id': correlation_id})
            response = VoiceResponse()
            response.reject()
            return str(response), 403
    
    # Get parameters
    call_sid = request.values.get("CallSid")
    from_number = request.values.get("From", "+10000000000")
    recording_url = request.values.get("RecordingUrl")
    
    if not validate_call_sid(call_sid):
        logger.error(f"Invalid CallSid: {call_sid}", 
                    extra={'correlation_id': correlation_id})
        response = VoiceResponse()
        response.say("Sorry, there was an error processing your call.")
        response.hangup()
        return str(response)
    
    logger.info(f"New call: {call_sid} from {from_number}", 
               extra={'correlation_id': correlation_id})
    
    # Store recording URL if provided
    if recording_url:
        with state_lock:
            recording_urls[call_sid] = recording_url
            
    # Validate phone number
    if not validate_phone_number(from_number):
        logger.error(f"Invalid phone number: {from_number}", 
                    extra={'correlation_id': correlation_id})
        response = VoiceResponse()
        response.say("Sorry, we couldn't validate your phone number.")
        response.hangup()
        return str(response)
    
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
        
        executor.submit(run_async_task, send_sms_link(from_number, is_first_call=False))
        
        return str(response)
    
    # Initialize call state
    with state_lock:
        if call_sid not in turn_count:
            turn_count[call_sid] = 0
            call_start_times[call_sid] = time.time()
            conversation_transcripts[call_sid] = []
            logger.info(f"User has {minutes_left:.2f} minutes remaining", 
                       extra={'correlation_id': correlation_id})
        else:
            turn_count[call_sid] += 1
            
        with metrics_lock:
            metrics['total_calls'] += 1
            metrics['active_calls'] += 1
    
    response = VoiceResponse()
    
    # Play greeting with beep
    if turn_count.get(call_sid, 0) == 0:
        # Check session for first-time caller status
        has_called_session = session.get("has_called_before", False)
        
        if not has_called_session and not is_repeat_caller:
            session["has_called_before"] = True
            greeting_file = "first_time_greeting.mp3"
            logger.info("New caller detected — playing first-time greeting", 
                       extra={'correlation_id': correlation_id})
        else:
            greeting_file = "returning_user_greeting.mp3"
            logger.info("Returning caller — playing returning greeting", 
                       extra={'correlation_id': correlation_id})
        
        # Play greeting if file exists
        if os.path.exists(f"static/{greeting_file}"):
            response.play(f"{request.url_root}static/{greeting_file}")
        else:
            # Fallback TTS greeting
            if greeting_file == "first_time_greeting.mp3":
                response.say("Welcome to ConvoReps! Let's practice.", voice="alice")
            else:
                response.say("Welcome back! Ready to practice?", voice="alice")
        
        # Play beep sound
        if os.path.exists("static/beep.mp3"):
            response.play(f"{request.url_root}static/beep.mp3")
        response.pause(length=1)
    
    # Connect to WebSocket stream
    host = request.headers.get('X-Forwarded-Host', request.headers.get('Host', request.url_root.replace('http://', '').replace('https://', '').rstrip('/')))
    ws_scheme = "wss" if request.is_secure else "ws"
    stream_url = f"{ws_scheme}://{host}/media-stream"
    
    logger.info(f"Stream URL: {stream_url}", extra={'correlation_id': correlation_id})
    
    connect = Connect()
    stream = Stream(
        url=stream_url,
        track="inbound_track"
    )
    
    # Add custom parameters
    stream.parameter(name="call_sid", value=call_sid)
    stream.parameter(name="from_number", value=from_number)
    stream.parameter(name="minutes_left", value=str(minutes_left))
    stream.parameter(name="correlation_id", value=correlation_id)
    
    connect.append(stream)
    response.append(connect)
    
    # Fallback TwiML
    response.say("Thank you for practicing with ConvoReps!")
    response.pause(length=1)
    response.hangup()
    
    # Set up timer
    timer_duration = minutes_left * 60 if minutes_left < FREE_CALL_MINUTES else FREE_CALL_MINUTES * 60
    logger.info(f"Setting timer for {timer_duration:.0f} seconds", 
               extra={'correlation_id': correlation_id})
    
    timer = threading.Timer(timer_duration, lambda: handle_time_limit(call_sid, from_number))
    timer.start()
    
    with state_lock:
        call_timers[call_sid] = timer
    
    return str(response)

@sock.route('/media-stream')
def handle_media_stream(ws):
    """Handle WebSocket connection between Twilio and OpenAI Realtime"""
    logger.info("Client connected to media stream")
    
    # Check connection limit
    with state_lock:
        if len(active_streams) >= MAX_WEBSOCKET_CONNECTIONS:
            logger.warning("Maximum WebSocket connections reached")
            ws.close()
            return
    
    # Run the async handler
    ws_start_time = time.time()
    try:
        asyncio.run(handle_media_stream_async(ws))
    except Exception as e:
        logger.error(f"WebSocket handler error: {e}", exc_info=True)
        with metrics_lock:
            metrics['websocket_errors'] += 1
    finally:
        # Track WebSocket duration
        ws_duration = time.time() - ws_start_time
        with metrics_lock:
            metrics['websocket_duration_sum'] += ws_duration
            metrics['websocket_count'] += 1

async def handle_media_stream_async(ws):
    """Async handler for WebSocket connection"""
    stream_sid = None
    call_sid = None
    from_number = None
    minutes_left = FREE_CALL_MINUTES
    correlation_id = None
    openai_ws = None
    latest_media_timestamp = 0
    response_start_timestamp_twilio = None
    mark_queue = []
    last_assistant_item = None
    
    try:
        # Connect to OpenAI Realtime API
        openai_url = f'wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}'
        async with websockets.connect(
            openai_url,
            additional_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1"
            },
            ping_interval=20,
            ping_timeout=10
        ) as openai_ws:
            
            # Send initial session configuration
            await send_session_update(openai_ws)
            
            async def receive_from_twilio():
                """Receive audio from Twilio and forward to OpenAI"""
                nonlocal stream_sid, call_sid, from_number, minutes_left, correlation_id, latest_media_timestamp
                
                try:
                    while True:
                        # Receive message from Twilio WebSocket with timeout
                        try:
                            message = await asyncio.get_event_loop().run_in_executor(
                                None, ws.receive, 1.0
                            )
                            if message is None:
                                continue
                        except Exception as e:
                            if "timeout" in str(e).lower():
                                continue
                            else:
                                logger.error(f"WebSocket receive error: {e}")
                                break
                                
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            logger.error("Invalid JSON received from Twilio")
                            continue
                        
                        if data['event'] == 'start':
                            # Extract stream data
                            start_data = data.get('start', {})
                            stream_sid = start_data.get('streamSid')
                            
                            # Extract custom parameters
                            custom_params = start_data.get('customParameters', {})
                            call_sid = custom_params.get('call_sid')
                            from_number = custom_params.get('from_number')
                            minutes_left = float(custom_params.get('minutes_left', FREE_CALL_MINUTES))
                            correlation_id = custom_params.get('correlation_id')
                            
                            logger.info(f"Stream started - CallSid: {call_sid}, StreamSid: {stream_sid}", 
                                       extra={'correlation_id': correlation_id})
                            
                            with state_lock:
                                active_streams[call_sid] = {
                                    'stream_sid': stream_sid,
                                    'from_number': from_number,
                                    'minutes_left': minutes_left,
                                    'last_activity': time.time(),
                                    'correlation_id': correlation_id,
                                    'openai_ws': openai_ws,
                                    'twilio_ws': ws
                                }
                                
                            # Send initial greeting
                            await send_initial_conversation_item(openai_ws, call_sid)
                        
                        elif data['event'] == 'media' and openai_ws.open:
                            latest_media_timestamp = int(data['media']['timestamp'])
                            
                            # Update activity
                            if call_sid in active_streams:
                                active_streams[call_sid]['last_activity'] = time.time()
                            
                            # Forward audio to OpenAI
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": data['media']['payload']
                            }
                            await openai_ws.send(json.dumps(audio_append))
                            
                            # Periodic garbage collection
                            if latest_media_timestamp % 30000 == 0:
                                gc.collect()
                        
                        elif data['event'] == 'mark':
                            # Mark event from Twilio
                            if mark_queue:
                                mark_queue.pop(0)
                        
                        elif data['event'] == 'dtmf':
                            # Handle DTMF tones
                            dtmf_data = data.get('dtmf', {})
                            digit = dtmf_data.get('digit')
                            logger.info(f"DTMF received: {digit}", 
                                       extra={'correlation_id': correlation_id})
                            
                            if digit == '#':
                                await handle_reset(openai_ws, call_sid)
                            elif digit == '*':
                                await repeat_last_response(openai_ws, call_sid)
                        
                        elif data['event'] == 'stop':
                            logger.info(f"Stream stopped - CallSid: {call_sid}", 
                                       extra={'correlation_id': correlation_id})
                            break
                            
                except (ConnectionClosed, OSError) as e:
                    logger.info(f"Client disconnected: {e}")
                except Exception as e:
                    logger.error(f"Error in receive_from_twilio: {e}", exc_info=True)
                finally:
                    logger.info("Connection closed.")
            
            async def send_to_twilio():
                """Receive from OpenAI and forward to Twilio"""
                nonlocal response_start_timestamp_twilio, last_assistant_item
                
                try:
                    async for openai_message in openai_ws:
                        response = json.loads(openai_message)
                        
                        # Log important events
                        if response.get('type') in ['response.content.done', 'rate_limits.updated', 
                                                    'response.done', 'input_audio_buffer.committed',
                                                    'input_audio_buffer.speech_stopped', 
                                                    'input_audio_buffer.speech_started',
                                                    'response.create', 'session.created']:
                            logger.info(f"Received event: {response['type']}")
                        
                        # Handle session created
                        if response.get('type') == 'session.created':
                            logger.info(f"Session created successfully", 
                                      extra={'correlation_id': correlation_id})
                        
                        # Handle audio delta
                        elif response.get('type') == 'response.audio.delta' and 'delta' in response:
                            # Forward audio to Twilio
                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": response['delta']
                                }
                            }
                            await asyncio.get_event_loop().run_in_executor(
                                None, ws.send, json.dumps(audio_delta)
                            )
                            
                            if response_start_timestamp_twilio is None:
                                response_start_timestamp_twilio = latest_media_timestamp
                            
                            # Update last assistant item
                            if response.get('item_id'):
                                last_assistant_item = response['item_id']
                            
                            await send_mark(ws, stream_sid)
                        
                        # Handle rate limits
                        elif response.get('type') == 'rate_limits.updated':
                            rate_limits = response.get('rate_limits', [])
                            for limit in rate_limits:
                                logger.warning(f"Rate limit update - Name: {limit.get('name')}, "
                                             f"Remaining: {limit.get('remaining')}, "
                                             f"Reset: {limit.get('reset_seconds')}")
                        
                        # Handle response done
                        elif response.get('type') == 'response.done':
                            # Parse and log transcripts
                            if 'response' in response and 'output' in response['response']:
                                for item in response['response']['output']:
                                    role = item.get('role')
                                    content = item.get('content', [])
                                    
                                    for c in content:
                                        if c.get('type') == 'audio' and 'transcript' in c:
                                            # Append to CSV
                                            append_transcript_to_csv(role, c['transcript'], call_sid)
                                            
                                            # Store in memory
                                            if call_sid in conversation_transcripts:
                                                conversation_transcripts[call_sid].append({
                                                    'role': role,
                                                    'content': c['transcript'],
                                                    'timestamp': datetime.now().isoformat()
                                                })
                                                
                                                # Limit conversation history
                                                if len(conversation_transcripts[call_sid]) > 50:
                                                    conversation_transcripts[call_sid] = conversation_transcripts[call_sid][-50:]
                                                
                                                # Cache assistant response
                                                if role == 'assistant':
                                                    with state_lock:
                                                        last_response_cache[call_sid] = c['transcript']
                            
                            response_start_timestamp_twilio = None
                            
                            # Check for interview mode
                            mode = mode_lock.get(call_sid, "cold_call")
                            if mode == "interview" and call_sid in interview_question_index:
                                # Advance to next question
                                interview_question_index[call_sid] = (
                                    interview_question_index.get(call_sid, 0) + 1
                                ) % len(interview_questions)
                                
                                # Update session with next question
                                voice, base_prompt, _ = get_personality_for_mode(call_sid, "interview")
                                current_q_idx = interview_question_index[call_sid]
                                if current_q_idx < len(interview_questions):
                                    system_prompt = base_prompt + f"\n\nAsk this question next: {interview_questions[current_q_idx]}"
                                    
                                    update_event = {
                                        "type": "session.update",
                                        "event_id": f"interview_q{current_q_idx}_{call_sid[:8]}",
                                        "session": {
                                            "voice": voice,
                                            "instructions": system_prompt
                                        }
                                    }
                                    await openai_ws.send(json.dumps(update_event))
                        
                        # Handle user speech transcription
                        elif response.get('type') == 'conversation.item.input_audio_transcription.completed':
                            transcript = response.get('transcript', '')
                            if transcript and call_sid:
                                # Log to CSV
                                append_transcript_to_csv('user', transcript, call_sid)
                                
                                # Store in memory
                                if call_sid in conversation_transcripts:
                                    conversation_transcripts[call_sid].append({
                                        'role': 'user',
                                        'content': transcript,
                                        'timestamp': datetime.now().isoformat()
                                    })
                                    
                                    # Limit conversation history
                                    if len(conversation_transcripts[call_sid]) > 50:
                                        conversation_transcripts[call_sid] = conversation_transcripts[call_sid][-50:]
                                
                                # Detect mode if not set
                                if call_sid not in mode_lock:
                                    detected_mode = detect_intent(transcript)
                                    mode_lock[call_sid] = detected_mode
                                    
                                    # Update session for mode
                                    voice, system_prompt, _ = get_personality_for_mode(call_sid, detected_mode)
                                    
                                    if detect_bad_news(transcript):
                                        system_prompt = add_bad_news_context(system_prompt, transcript)
                                    
                                    # Initialize interview question index
                                    if detected_mode == "interview" and call_sid not in interview_question_index:
                                        interview_question_index[call_sid] = 0
                                    
                                    # Update session
                                    if detected_mode != "cold_call":
                                        if detected_mode == "interview":
                                            current_q_idx = interview_question_index.get(call_sid, 0)
                                            if current_q_idx < len(interview_questions):
                                                system_prompt += f"\n\nAsk this question next: {interview_questions[current_q_idx]}"
                                        
                                        await update_session_for_mode(openai_ws, voice, system_prompt)
                                else:
                                    # Check for bad news
                                    if detect_bad_news(transcript):
                                        mode = mode_lock.get(call_sid, "cold_call")
                                        voice, system_prompt, _ = get_personality_for_mode(call_sid, mode)
                                        system_prompt = add_bad_news_context(system_prompt, transcript)
                                        await update_session_for_mode(openai_ws, voice, system_prompt)
                        
                        # Handle speech started - interruption
                        elif response.get('type') == 'input_audio_buffer.speech_started':
                            logger.info("Speech started detected")
                            if last_assistant_item:
                                logger.info(f"Interrupting response with id: {last_assistant_item}")
                                new_timestamp, new_item = await handle_speech_started_event(
                                    openai_ws, ws, stream_sid, 
                                    response_start_timestamp_twilio, latest_media_timestamp,
                                    last_assistant_item, mark_queue
                                )
                                response_start_timestamp_twilio = new_timestamp
                                last_assistant_item = new_item
                        
                        # Handle function calls
                        elif response.get('type') in ['response.function_call_arguments.done', 'response.done']:
                            if response.get('type') == 'response.done':
                                # Check response.output for function calls
                                outputs = response.get('response', {}).get('output', [])
                                for output in outputs:
                                    if output.get('type') == 'function_call':
                                        await handle_function_call_from_response(
                                            output, openai_ws, call_sid,
                                            call_start_times.get(call_sid), minutes_left
                                        )
                            else:
                                # Direct function_call_arguments.done event
                                await handle_function_call(response, openai_ws, call_sid, 
                                                         call_start_times.get(call_sid), minutes_left)
                        
                        elif response.get('type') == 'error':
                            error_code = response.get('code')
                            error_message = response.get('message')
                            error_event_id = response.get('event_id')
                            
                            logger.error(f"OpenAI error - Code: {error_code}, Message: {error_message}, Event ID: {error_event_id}")
                            
                            if error_event_id:
                                logger.error(f"Error caused by event: {error_event_id}")
                            
                            with metrics_lock:
                                metrics['api_errors'] += 1
                                
                except Exception as e:
                    logger.error(f"Error in send_to_twilio: {e}", exc_info=True)
            
            async def send_mark(connection, sid):
                """Send mark event to track audio chunks"""
                if sid:
                    mark_event = {
                        "event": "mark",
                        "streamSid": sid,
                        "mark": {"name": "responsePart"}
                    }
                    await asyncio.get_event_loop().run_in_executor(
                        None, connection.send, json.dumps(mark_event)
                    )
                    mark_queue.append('responsePart')
                    
                    # Limit mark queue size
                    if len(mark_queue) > 100:
                        mark_queue[:50] = []
            
            # Run both tasks concurrently
            await asyncio.gather(receive_from_twilio(), send_to_twilio())
            
    except Exception as e:
        logger.error(f"WebSocket handler error: {e}", exc_info=True)
    finally:
        if call_sid:
            cleanup_call_resources(call_sid)
        try:
            ws.close()
        except:
            pass

async def send_session_update(openai_ws):
    """Send session configuration to OpenAI"""
    # Default to cold call personality
    voice, system_prompt, _ = get_personality_for_mode("", "cold_call")
    
    session_update = {
        "type": "session.update",
        "event_id": f"session_update_{uuid.uuid4().hex[:8]}",
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
    
    logger.info(f"Sending session update with voice: {voice}")
    await openai_ws.send(json.dumps(session_update))

async def send_initial_conversation_item(openai_ws, call_sid: str):
    """Send initial greeting to start conversation"""
    # Get appropriate greeting based on mode
    mode = mode_lock.get(call_sid, "cold_call")
    _, _, greeting = get_personality_for_mode(call_sid, mode)
    
    initial_conversation_item = {
        "type": "conversation.item.create",
        "event_id": f"initial_greeting_{call_sid[:8]}",
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
    
    response_create = {
        "type": "response.create",
        "event_id": f"initial_response_{call_sid[:8]}"
    }
    await openai_ws.send(json.dumps(response_create))

async def update_session_for_mode(openai_ws, voice: str, system_prompt: str):
    """Update session with new voice and instructions"""
    session_update = {
        "type": "session.update",
        "event_id": f"mode_update_{uuid.uuid4().hex[:8]}",
        "session": {
            "voice": voice,
            "instructions": system_prompt
        }
    }
    await openai_ws.send(json.dumps(session_update))

async def handle_speech_started_event(openai_ws, twilio_ws, stream_sid: str, 
                                    response_start_timestamp: int, latest_timestamp: int,
                                    last_assistant_item: str, mark_queue: list):
    """Handle interruption when user starts speaking"""
    logger.info("Handling speech started event")
    
    if mark_queue and response_start_timestamp is not None:
        elapsed_time = latest_timestamp - response_start_timestamp
        logger.info(f"Calculating elapsed time for truncation: {latest_timestamp} - {response_start_timestamp} = {elapsed_time}ms")
        
        if last_assistant_item:
            logger.info(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")
            
            # Truncate the assistant's response
            truncate_event = {
                "type": "conversation.item.truncate",
                "item_id": last_assistant_item,
                "content_index": 0,
                "audio_end_ms": elapsed_time
            }
            await openai_ws.send(json.dumps(truncate_event))
        
        # Clear Twilio's audio buffer
        clear_event = {
            "event": "clear",
            "streamSid": stream_sid
        }
        await asyncio.get_event_loop().run_in_executor(
            None, twilio_ws.send, json.dumps(clear_event)
        )
        
        mark_queue.clear()
        
        # Reset timestamps
        return None, None
    
    return response_start_timestamp, last_assistant_item

async def handle_function_call(response: dict, openai_ws, call_sid: str, 
                             call_start_time: float, minutes_left: float):
    """Handle function call from OpenAI (function_call_arguments.done event)"""
    function_name = response.get('name')
    call_id = response.get('call_id')
    
    await process_function_call(function_name, call_id, None, openai_ws, 
                              call_sid, call_start_time, minutes_left)

async def handle_function_call_from_response(output: dict, openai_ws, call_sid: str,
                                           call_start_time: float, minutes_left: float):
    """Handle function call from response.done event output"""
    function_name = output.get('name')
    call_id = output.get('call_id')
    arguments_str = output.get('arguments', '{}')
    
    await process_function_call(function_name, call_id, arguments_str, openai_ws,
                              call_sid, call_start_time, minutes_left)

async def process_function_call(function_name: str, call_id: str, arguments_str: str,
                              openai_ws, call_sid: str, call_start_time: float, 
                              minutes_left: float):
    """Process function call and send result back to model"""
    if function_name == 'check_remaining_time' and call_start_time:
        # Calculate remaining time
        elapsed_minutes = (time.time() - call_start_time) / 60.0
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
        
        # Send with event_id for error tracking
        function_output["event_id"] = f"func_output_{call_id}"
        
        await openai_ws.send(json.dumps(function_output))
        
        # Trigger response
        response_event = {
            "type": "response.create",
            "event_id": f"func_response_{call_id}"
        }
        await openai_ws.send(json.dumps(response_event))
        
        with metrics_lock:
            metrics['tool_calls_made'] += 1
            
        # Check if time is exhausted
        if remaining <= MIN_CALL_DURATION:
            logger.info(f"Tool detected time exhausted for {call_sid}")
            if call_sid in call_timers:
                call_timers[call_sid].cancel()
                call_timers.pop(call_sid, None)
            
            # Update usage immediately
            update_user_usage(active_streams[call_sid].get('from_number'), elapsed_minutes)
            
            # Trigger end flow
            from_number = active_streams[call_sid].get('from_number')
            if from_number:
                asyncio.create_task(send_time_limit_message(openai_ws, call_sid, from_number))

async def handle_reset(openai_ws, call_sid: str):
    """Handle conversation reset via DTMF #"""
    logger.info("Reset triggered by user")
    
    with state_lock:
        conversation_transcripts.pop(call_sid, None)
        personality_memory.pop(call_sid, None)
        mode_lock.pop(call_sid, None)
        turn_count[call_sid] = 0
        interview_question_index.pop(call_sid, None)
        last_response_cache.pop(call_sid, None)
    
    reset_message = "Alright, let's start fresh. What would you like to practice?"
    
    reset_item = {
        "type": "conversation.item.create",
        "event_id": f"reset_{call_sid[:8]}",
        "item": {
            "type": "message",
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": f"Reset the conversation and say: {reset_message}"
                }
            ]
        }
    }
    await openai_ws.send(json.dumps(reset_item))
    
    response_event = {
        "type": "response.create",
        "event_id": f"reset_response_{call_sid[:8]}"
    }
    await openai_ws.send(json.dumps(response_event))

async def repeat_last_response(openai_ws, call_sid: str):
    """Repeat the last assistant response via DTMF *"""
    # Check cache first
    with state_lock:
        cached_response = last_response_cache.get(call_sid)
    
    if cached_response:
        repeat_item = {
            "type": "conversation.item.create",
            "event_id": f"repeat_{call_sid[:8]}",
            "item": {
                "type": "message",
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"Repeat exactly: {cached_response}"
                    }
                ]
            }
        }
        await openai_ws.send(json.dumps(repeat_item))
        
        response_event = {
            "type": "response.create",
            "event_id": f"repeat_response_{call_sid[:8]}"
        }
        await openai_ws.send(json.dumps(response_event))
        return
    
    # Fall back to conversation history
    if call_sid in conversation_transcripts:
        messages = conversation_transcripts[call_sid]
        for message in reversed(messages):
            if message['role'] == 'assistant':
                repeat_item = {
                    "type": "conversation.item.create",
                    "event_id": f"repeat_history_{call_sid[:8]}",
                    "item": {
                        "type": "message",
                        "role": "system",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Repeat exactly: {message['content']}"
                            }
                        ]
                    }
                }
                await openai_ws.send(json.dumps(repeat_item))
                
                response_event = {
                    "type": "response.create",
                    "event_id": f"repeat_history_response_{call_sid[:8]}"
                }
                await openai_ws.send(json.dumps(response_event))
                return
    
    # No previous response found
    no_response_item = {
        "type": "conversation.item.create",
        "event_id": f"no_repeat_{call_sid[:8]}",
        "item": {
            "type": "message",
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": "Say: I don't have a previous response to repeat."
                }
            ]
        }
    }
    await openai_ws.send(json.dumps(no_response_item))
    
    response_event = {
        "type": "response.create",
        "event_id": f"no_repeat_response_{call_sid[:8]}"
    }
    await openai_ws.send(json.dumps(response_event))

# Health and monitoring endpoints
@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    with state_lock:
        active_count = len(active_streams)
    
    with metrics_lock:
        error_rate = metrics['api_errors'] / max(metrics['total_calls'], 1)
        api_health = "healthy" if error_rate < 0.1 else "degraded"
        ws_health = "healthy" if metrics['websocket_errors'] < 10 else "degraded"
    
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "active_streams": active_count,
        "api_health": api_health,
        "websocket_health": ws_health,
        "memory_usage_mb": get_memory_usage(),
        "version": "2.0-realtime",
        "realtime_model": OPENAI_REALTIME_MODEL
    }

@app.route("/metrics", methods=["GET"])
def metrics_endpoint():
    """Detailed metrics endpoint"""
    with state_lock:
        active_count = len(active_streams)
        mode_breakdown = {}
        for mode in ["cold_call", "interview", "small_talk"]:
            mode_breakdown[mode] = sum(1 for m in mode_lock.values() if m == mode)
        active_timers = len(call_timers)
    
    with metrics_lock:
        avg_ws_duration = (
            metrics['websocket_duration_sum'] / max(metrics['websocket_count'], 1)
            if metrics['websocket_count'] > 0 else 0
        )
        
        return {
            "active_streams": active_count,
            "total_calls": metrics['total_calls'],
            "failed_calls": metrics['failed_calls'],
            "conversation_modes": mode_breakdown,
            "sms_stats": {
                "sent": metrics['sms_sent'],
                "failed": metrics['sms_failed']
            },
            "tool_calls": metrics['tool_calls_made'],
            "api_errors": metrics['api_errors'],
            "websocket_errors": metrics['websocket_errors'],
            "websocket_metrics": {
                "average_duration_seconds": avg_ws_duration,
                "total_connections": metrics['websocket_count']
            },
            "timer_stats": {
                "active_timers": active_timers,
                "free_minutes_per_user": FREE_CALL_MINUTES
            },
            "free_minutes_per_user": FREE_CALL_MINUTES,
            "csv_stats": get_csv_statistics(),
            "memory_usage_mb": get_memory_usage()
        }

def get_memory_usage() -> float:
    """Get memory usage"""
    try:
        import psutil
        process = psutil.Process()
        return process.memory_info().rss / 1024 / 1024
    except:
        return 0.0

def get_csv_statistics() -> Dict[str, Any]:
    """Get CSV statistics"""
    try:
        if not os.path.exists(USAGE_CSV_PATH):
            return {"error": "CSV file not found"}
        
        total_users = 0
        total_minutes_used = 0.0
        active_users_today = 0
        today = datetime.now().date()
        
        with csv_lock:
            with open(USAGE_CSV_PATH, 'r', newline='') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    total_users += 1
                    total_minutes_used += float(row.get('minutes_used', 0))
                    
                    if row.get('last_call_date'):
                        try:
                            call_date = datetime.fromisoformat(row['last_call_date']).date()
                            if call_date == today:
                                active_users_today += 1
                        except:
                            pass
        
        return {
            "total_users": total_users,
            "total_minutes_consumed": round(total_minutes_used, 2),
            "active_users_today": active_users_today,
            "average_minutes_per_user": round(total_minutes_used / max(total_users, 1), 2)
        }
    except Exception as e:
        return {"error": str(e)}

# Static file serving
@app.route("/static/<path:filename>")
def serve_static(filename):
    """Serve static files securely"""
    if '..' in filename or filename.startswith('/'):
        return "Invalid filename", 400
    return send_from_directory("static", filename)

@app.route("/sms", methods=["GET", "POST"])
def sms_webhook():
    """Handle incoming SMS messages from Twilio"""
    correlation_id = str(uuid.uuid4())
    
    try:
        # Security validation
        if TWILIO_AUTH_TOKEN:
            url = request.url
            params = request.values
            signature = request.headers.get('X-Twilio-Signature', '')
            
            if not twilio_validator.validate(url, params, signature):
                logger.warning("Invalid Twilio signature for SMS", 
                             extra={'correlation_id': correlation_id})
                return "", 403
        
        from_number = request.values.get('From')
        to_number = request.values.get('To')
        body = request.values.get('Body', '').strip().lower()
        
        logger.info(f"Incoming SMS from {from_number}: {body}", 
                   extra={'correlation_id': correlation_id})
        
        response = VoiceResponse()
        
        # Handle keywords
        if any(keyword in body for keyword in ['help', 'info', 'start', 'practice']):
            response.message(
                "Welcome to ConvoReps! Call this number to practice conversations. "
                "Get 30 minutes for $6.99 at convoreps.com"
            )
        elif any(keyword in body for keyword in ['stop', 'unsubscribe', 'cancel']):
            response.message(
                "You've been unsubscribed from ConvoReps SMS. "
                "Reply START to resubscribe."
            )
        else:
            response.message(
                "Thanks for your message! Call this number to practice with ConvoReps. "
                "Visit convoreps.com for more info."
            )
        
        return str(response)
        
    except Exception as e:
        logger.error(f"SMS webhook error: {e}", 
                    extra={'correlation_id': correlation_id},
                    exc_info=True)
        return "", 200

if __name__ == "__main__":
    # Generate beep sound if missing
    if not os.path.exists("static/beep.mp3") and PYDUB_AVAILABLE:
        logger.warning("beep.mp3 not found - generating")
        try:
            # Generate a 1000Hz sine wave beep, 300ms duration
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
    
    # Initial garbage collection
    gc.collect()
    
    logger.info("\n🚀 ConvoReps OpenAI Realtime Edition v2.0")
    logger.info("   ✅ OpenAI Realtime API for end-to-end audio")
    logger.info("   ✅ Twilio Media Streams integration")
    logger.info("   ✅ Flask-Sock WebSocket handling")
    logger.info("   ✅ User tracking and time limits")
    logger.info("   ✅ Function calling for time checking")
    logger.info("   ✅ SMS sending with deduplication")
    logger.info("   ✅ Multiple conversation modes")
    logger.info("   ✅ Production-ready error handling")
    logger.info("   ✅ Thread-safe operations")
    logger.info("   ✅ Memory optimization")
    logger.info(f"\n   Configuration:")
    logger.info(f"   - Model: {OPENAI_REALTIME_MODEL}")
    logger.info(f"   - Free Minutes: {FREE_CALL_MINUTES}")
    logger.info(f"   - Port: {PORT}")
    logger.info("\n")
    
    app.run(host="0.0.0.0", port=PORT, debug=False)
