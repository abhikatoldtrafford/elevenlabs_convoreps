"""
ConvoReps WebSocket Streaming Edition - Production Ready v4.2 FINAL
Real-time voice conversation practice with ultra-low latency streaming

PRODUCTION-READY VERSION WITH ALL FIXES:
✅ Non-streaming tool calling for reliability
✅ SMS deduplication with atomic operations
✅ CSV atomic writes with temp files
✅ Comprehensive input validation
✅ WebSocket security validation
✅ Enhanced error recovery and retries
✅ Backup mechanisms for critical data
✅ Advanced health checks with WebSocket status
✅ Detailed monitoring metrics
✅ Structured logging with correlation IDs (FIXED in v4.1)
✅ Graceful shutdown handling
✅ All original features retained

FINAL FIXES:
v4.1: Fixed logging error where correlation_id was missing from third-party library logs
v4.2: Fixed websockets.connect() parameter from extra_headers to additional_headers

IMPORTANT: Add 'websockets' to your requirements.txt

Author: ConvoReps Team
Version: 4.2 FINAL (Production Ready & Tested)
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
import websockets
import csv
import uuid
import logging
import signal
import tempfile
import shutil
import traceback
from pathlib import Path
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple, Set
import numpy as np
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from flask import Flask, request, Response, session, send_from_directory
from flask_cors import CORS
from flask_sock import Sock
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream, Say, Play, Pause, Gather
from dotenv import load_dotenv
from pydub import AudioSegment

# Try to import audioop, use fallback if not available
try:
    import audioop
    AUDIOOP_AVAILABLE = True
except ImportError:
    print("⚠️ audioop not available, using pydub for audio conversion")
    audioop = None
    AUDIOOP_AVAILABLE = False

# Cross-platform file locking
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False
    print("⚠️ fcntl not available (Windows), using thread lock for CSV access")

from openai import OpenAI, AsyncOpenAI
from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs

# Load environment variables
load_dotenv()

# Custom formatter that handles missing correlation_id gracefully
class CorrelationFormatter(logging.Formatter):
    def format(self, record):
        # Add correlation_id if not present
        if not hasattr(record, 'correlation_id'):
            record.correlation_id = getattr(record, 'correlation_id', 'NO-ID')
        
        # Format the message
        return super().format(record)

# Initialize structured logging with custom formatter
formatter = CorrelationFormatter(
    '%(asctime)s - %(name)s - %(levelname)s - [%(correlation_id)s] - %(message)s'
)

# Set up handlers
file_handler = logging.FileHandler('convoreps.log')
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

# Configure root logger to catch all logs
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = []  # Clear existing handlers
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# Create logger for this module
logger = logging.getLogger(__name__)

# Disable propagation for some noisy libraries
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('openai').setLevel(logging.WARNING)

# Helper function to log with correlation ID
def log_with_correlation(level, msg, correlation_id=None):
    """Log a message with optional correlation ID"""
    extra = {'correlation_id': correlation_id} if correlation_id else {}
    getattr(logger, level)(msg, extra=extra)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your-secret-key-here")
CORS(app)

# Initialize WebSocket support
sock = Sock(app)

# Configuration
ELEVENLABS_WEBSOCKET_ENABLED = os.getenv("ELEVENLABS_WEBSOCKET_ENABLED", "true").lower() == "true"
OPENAI_STREAMING_ENABLED = os.getenv("OPENAI_STREAMING_ENABLED", "false").lower() == "true"  # Disabled for tool calling
MAX_WEBSOCKET_CONNECTIONS = int(os.getenv("MAX_WEBSOCKET_CONNECTIONS", "50"))
FREE_CALL_MINUTES = float(os.getenv("FREE_CALL_MINUTES", "5.0"))
USAGE_CSV_PATH = os.getenv("USAGE_CSV_PATH", "user_usage.csv")
USAGE_CSV_BACKUP_PATH = os.getenv("USAGE_CSV_BACKUP_PATH", "user_usage_backup.csv")
CONVOREPS_URL = os.getenv("CONVOREPS_URL", "https://convoreps.com")
MIN_CALL_DURATION = float(os.getenv("MIN_CALL_DURATION", "0.5"))  # Minimum 30 seconds

# Audio processing constants
AUDIO_CHUNK_SIZE = 160  # bytes for Twilio compatibility
AUDIO_BUFFER_MAX_SIZE = 24000  # 3 seconds at 8kHz
SILENCE_THRESHOLD_MS = 800  # milliseconds
ENERGY_THRESHOLD = 500  # RMS energy threshold
WEBSOCKET_KEEPALIVE_INTERVAL = 15  # seconds
ELEVENLABS_KEEPALIVE_INTERVAL = 15  # seconds

# Initialize API clients
sync_openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
async_openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
elevenlabs_client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

# Initialize Twilio client for SMS
twilio_client = TwilioClient(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN")
)

# Twilio security validator
twilio_validator = RequestValidator(os.getenv("TWILIO_AUTH_TOKEN"))

# Thread pool for async operations - optimized for memory
executor = ThreadPoolExecutor(max_workers=5)

# Global state management with thread safety
state_lock = threading.Lock()
active_streams: Dict[str, Dict[str, Any]] = {}
turn_count: Dict[str, int] = {}
mode_lock: Dict[str, str] = {}
conversation_history: Dict[str, List[Dict[str, str]]] = {}
personality_memory: Dict[str, Any] = {}
interview_question_index: Dict[str, int] = {}
elevenlabs_websockets: Dict[str, Any] = {}
call_start_times: Dict[str, float] = {}
call_timers: Dict[str, threading.Timer] = {}
sms_sent_flags: Dict[str, bool] = {}  # Track SMS sent status
last_response_cache: Dict[str, str] = {}  # Cache for repeat functionality
recording_urls: Dict[str, str] = {}  # Store recording URLs

# Metrics tracking
metrics = {
    "total_calls": 0,
    "active_calls": 0,
    "failed_calls": 0,
    "sms_sent": 0,
    "sms_failed": 0,
    "tool_calls_made": 0,
    "api_errors": {"openai": 0, "elevenlabs": 0, "twilio": 0},
    "websocket_duration_sum": 0.0,
    "websocket_count": 0
}
metrics_lock = threading.Lock()

# CSV lock for all platforms
csv_lock = threading.Lock()

# Voice profiles (preserved from original + new voices)
personality_profiles = {
    "rude/skeptical": {"voice_id": "1t1EeRixsJrKbiF1zwM6"},
    "super busy": {"voice_id": "6YQMyaUWlj0VX652cY1C"},
    "small_talk": {"voice_id": "2BJW5coyhAzSr8STdHbE"}
}

cold_call_personality_pool = {
    "Jerry": {
        "voice_id": "1t1EeRixsJrKbiF1zwM6",
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
        "voice_id": "Ax1GP2W4XTyAyNHuch7v",
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
        "voice_id": "7eFTSJ6WtWd9VCU4ZlI1",
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

# Additional voice profiles
additional_voices = {
    "Burt": {"voice_id": "4YYIPFl9wE5c4L2eu2Gb"},
    "Brad": {"voice_id": "f5HLTX707KIM4SzJYzSz"},
    "Gregory": {"voice_id": "PzuBz8h2SxBvQ7lnUC44"},
    "Belle": {"voice_id": "tIb1FHpzlwSiTGg6JxF0"},
    "Hope": {"voice_id": "zGjIP4SZlMnY9m93k97r"},
    "Jamahal": {"voice_id": "DTKMou8ccj1ZaWGBiotd"}
}

# Special voice for time limit messages
TIME_LIMIT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

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

# Graceful shutdown handler
shutdown_event = threading.Event()

def signal_handler(sig, frame):
    """Handle graceful shutdown"""
    logger.info("Graceful shutdown initiated...")
    shutdown_event.set()
    
    # Close all WebSocket connections
    with state_lock:
        for call_sid in list(active_streams.keys()):
            cleanup_call_resources(call_sid)
    
    # Cancel all timers
    with state_lock:
        for timer in call_timers.values():
            timer.cancel()
    
    # Create final backup
    create_csv_backup()
    
    logger.info("Shutdown complete")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Input validation functions
def validate_phone_number(phone_number: str) -> bool:
    """Validate phone number format"""
    if not phone_number:
        return False
    # E.164 format validation
    pattern = r'^\+[1-9]\d{1,14}$'
    return bool(re.match(pattern, phone_number))

def sanitize_csv_value(value: str) -> str:
    """Sanitize values for CSV to prevent injection"""
    if not value:
        return ""
    # Remove any CSV control characters
    value = str(value).replace('\r', '').replace('\n', ' ')
    # Escape quotes
    value = value.replace('"', '""')
    # Remove any formula injection attempts
    if value.strip().startswith(('=', '+', '-', '@')):
        value = "'" + value
    return value

def validate_call_sid(call_sid: str) -> bool:
    """Validate Twilio Call SID format"""
    if not call_sid:
        return False
    # Twilio Call SIDs start with CA
    pattern = r'^CA[0-9a-fA-F]{32}$'
    return bool(re.match(pattern, call_sid))

def validate_stream_sid(stream_sid: str) -> bool:
    """Validate Twilio Stream SID format"""
    if not stream_sid:
        return False
    # Twilio Stream SIDs start with MZ
    pattern = r'^MZ[0-9a-fA-F]{32}$'
    return bool(re.match(pattern, stream_sid))

# Enhanced CSV functions with atomic writes and backups
def init_csv():
    """Initialize CSV file if it doesn't exist"""
    try:
        if not os.path.exists(USAGE_CSV_PATH):
            csv_dir = os.path.dirname(USAGE_CSV_PATH)
            if csv_dir and not os.path.exists(csv_dir):
                os.makedirs(csv_dir, exist_ok=True)
                
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
    """Read user usage from CSV with validation"""
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
        # Try backup
        if os.path.exists(USAGE_CSV_BACKUP_PATH):
            try:
                shutil.copy2(USAGE_CSV_BACKUP_PATH, USAGE_CSV_PATH)
                return read_user_usage(phone_number)
            except:
                pass
    
    logger.info(f"New user detected: {phone_number}")
    return {
        'phone_number': phone_number,
        'minutes_used': 0.0,
        'minutes_left': FREE_CALL_MINUTES,
        'last_call_date': '',
        'total_calls': 0
    }

def update_user_usage(phone_number: str, minutes_used: float):
    """Update user usage in CSV with atomic writes"""
    if not validate_phone_number(phone_number):
        logger.warning(f"Invalid phone number format: {phone_number}")
        return
    
    # Prevent negative or zero updates
    if minutes_used <= 0:
        logger.warning(f"Invalid minutes_used: {minutes_used}")
        return
    
    init_csv()
    
    try:
        with csv_lock:
            # Read all data
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
            
            # Atomic write with temp file
            with tempfile.NamedTemporaryFile(mode='w', delete=False, newline='') as tmp_file:
                writer = csv.DictWriter(tmp_file, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
                temp_name = tmp_file.name
            
            # Atomic replace
            shutil.move(temp_name, USAGE_CSV_PATH)
            
        logger.info(f"Updated usage for {phone_number}: {minutes_used:.2f} minutes used")
        
        # Periodic backup every 10 updates
        with metrics_lock:
            if metrics['total_calls'] % 10 == 0:
                create_csv_backup()
                
    except Exception as e:
        logger.error(f"Error updating CSV: {e}", exc_info=True)

def check_time_remaining(phone_number: str) -> float:
    """Check remaining free minutes for a phone number"""
    if not validate_phone_number(phone_number):
        logger.warning(f"Invalid phone number format: {phone_number}")
        return FREE_CALL_MINUTES
    
    usage = read_user_usage(phone_number)
    return max(0.0, usage['minutes_left'])

async def send_sms_link(phone_number: str, is_first_call: bool = True, retry_count: int = 0):
    """Send SMS with ConvoReps link with retry mechanism"""
    correlation_id = str(uuid.uuid4())
    
    try:
        if not validate_phone_number(phone_number):
            logger.warning(f"Invalid phone number for SMS: {phone_number}", 
                         extra={'correlation_id': correlation_id})
            return
        
        # Check if SMS already sent for this call
        with state_lock:
            if sms_sent_flags.get(phone_number, False):
                logger.info(f"SMS already sent to {phone_number}", 
                          extra={'correlation_id': correlation_id})
                return
            sms_sent_flags[phone_number] = True
        
        from_number = os.getenv("TWILIO_PHONE_NUMBER")
        if not from_number:
            logger.error("TWILIO_PHONE_NUMBER not configured", 
                        extra={'correlation_id': correlation_id})
            return
        
        # Choose message based on context
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
        
        # Send SMS
        message = twilio_client.messages.create(
            body=message_body,
            from_=from_number,
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
        
        # Retry logic
        if retry_count < 3:
            await asyncio.sleep(2 ** retry_count)  # Exponential backoff
            await send_sms_link(phone_number, is_first_call, retry_count + 1)
        else:
            # Clear flag if all retries failed
            with state_lock:
                sms_sent_flags[phone_number] = False

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
                
            # Clean up old SMS flags
            with state_lock:
                cutoff_time = current_time - 3600  # 1 hour
                old_numbers = [num for num, _ in sms_sent_flags.items() 
                             if num not in [s.get('from_number') for s in active_streams.values()]]
                for num in old_numbers:
                    sms_sent_flags.pop(num, None)
                    
        except Exception as e:
            logger.error(f"Cleanup thread error: {e}")

# Start cleanup thread
cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
cleanup_thread.start()

# Enhanced Voice Activity Detection
class StreamingSpeechProcessor:
    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self.audio_buffer = bytearray()
        self.silence_threshold = SILENCE_THRESHOLD_MS / 1000.0  # Convert to seconds
        self.last_speech_time = time.time()
        self.min_speech_length = 0.3
        self.silence_start_time: Optional[float] = None
        self.speech_detected = False
        self.max_buffer_size = AUDIO_BUFFER_MAX_SIZE
        self.energy_threshold = ENERGY_THRESHOLD
        self.processing_audio = False
        
    def add_audio(self, audio_chunk: bytes) -> Optional[bytes]:
        """Add audio chunk to buffer with improved memory management"""
        try:
            if self.processing_audio:
                return None
                
            if not audio_chunk or not isinstance(audio_chunk, bytes):
                return None
                
            if len(self.audio_buffer) + len(audio_chunk) > self.max_buffer_size:
                complete_audio = bytes(self.audio_buffer)
                self.audio_buffer.clear()
                self.processing_audio = True
                return complete_audio
                
            self.audio_buffer.extend(audio_chunk)
            
            if len(self.audio_buffer) >= 2400:  # 300ms
                if self.detect_speech_end():
                    complete_audio = bytes(self.audio_buffer)
                    self.audio_buffer.clear()
                    self.processing_audio = True
                    return complete_audio
            return None
        except Exception as e:
            logger.error(f"Error in add_audio: {e}")
            self.audio_buffer.clear()
            self.processing_audio = False
            return None
    
    def detect_speech_end(self) -> bool:
        """Enhanced silence detection with better accuracy"""
        if len(self.audio_buffer) < 2400:
            return False
            
        check_size = min(3200, len(self.audio_buffer))
        last_chunk = self.audio_buffer[-check_size:]
        
        try:
            if AUDIOOP_AVAILABLE and audioop:
                rms = audioop.rms(bytes(last_chunk), 1)
            else:
                audio_array = np.frombuffer(last_chunk, dtype=np.uint8)
                if len(audio_array) == 0:
                    return True
                rms = np.sqrt(np.mean(np.square(audio_array.astype(np.float32))))
        except Exception as e:
            logger.error(f"RMS calculation error: {e}")
            return True
        
        if rms > self.energy_threshold:
            self.speech_detected = True
            self.silence_start_time = None
            self.last_speech_time = time.time()
        elif self.speech_detected and rms < (self.energy_threshold * 0.5):
            if self.silence_start_time is None:
                self.silence_start_time = time.time()
            elif time.time() - self.silence_start_time > self.silence_threshold:
                return True
        
        if time.time() - self.last_speech_time > 5.0:
            return True
            
        return False
    
    def reset(self):
        """Reset processor state"""
        self.processing_audio = False
        self.speech_detected = False
        self.silence_start_time = None

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

@app.route("/voice", methods=["POST", "GET"])
@error_handler
def voice():
    """Initial voice webhook with security validation"""
    correlation_id = str(uuid.uuid4())
    
    # Security validation
    if os.getenv("TWILIO_AUTH_TOKEN"):
        url = request.url
        params = request.values
        signature = request.headers.get('X-Twilio-Signature', '')
        
        if not twilio_validator.validate(url, params, signature):
            logger.warning("Invalid Twilio signature", 
                         extra={'correlation_id': correlation_id})
            response = VoiceResponse()
            response.reject()
            return str(response), 403
    
    # Get and validate parameters
    call_sid = request.values.get("CallSid")
    from_number = request.values.get("From")
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
        logger.info(f"Recording URL stored: {recording_url}", 
                   extra={'correlation_id': correlation_id})
    
    # Default phone number for testing
    if not from_number:
        from_number = "+10000000000"
        logger.warning("Missing From number, using test number", 
                      extra={'correlation_id': correlation_id})
    
    # Validate phone number
    if not validate_phone_number(from_number):
        logger.error(f"Invalid phone number: {from_number}", 
                    extra={'correlation_id': correlation_id})
        response = VoiceResponse()
        response.say("Sorry, we couldn't validate your phone number.")
        response.hangup()
        return str(response)
    
    # Check free minutes
    minutes_left = check_time_remaining(from_number)
    usage_data = read_user_usage(from_number)
    is_repeat_caller = usage_data['total_calls'] > 0
    
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
            logger.info(f"User has {minutes_left:.2f} minutes remaining", 
                       extra={'correlation_id': correlation_id})
        else:
            # Increment turn count for subsequent turns
            turn_count[call_sid] += 1
            logger.info(f"Turn {turn_count[call_sid]} for call {call_sid}", 
                       extra={'correlation_id': correlation_id})
            
        with metrics_lock:
            metrics['total_calls'] += 1
            metrics['active_calls'] += 1
    
    response = VoiceResponse()
    
    # Play greeting with beep
    if turn_count.get(call_sid, 0) == 0:
        # Check session and CSV for first-time caller status
        has_called_session = session.get("has_called_before", False)
        has_called_csv = is_repeat_caller
        
        if not has_called_session and not has_called_csv:
            session["has_called_before"] = True
            greeting_file = "first_time_greeting.mp3"
            logger.info("New caller detected — playing first-time greeting", 
                       extra={'correlation_id': correlation_id})
        else:
            greeting_file = "returning_user_greeting.mp3"
            logger.info("Returning caller — playing returning greeting", 
                       extra={'correlation_id': correlation_id})
        
        # Play greeting and beep if files exist
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
    
    # Start bidirectional stream
    connect = Connect()
    
    ws_scheme = "wss" if request.is_secure else "ws"
    host = request.headers.get('X-Forwarded-Host', request.host)
    stream_url = f"{ws_scheme}://{host}/media-stream"
    logger.info(f"Stream URL: {stream_url}", extra={'correlation_id': correlation_id})
    
    stream = Stream(
        url=stream_url,
        name="convoreps_stream",
        track="inbound_track",  # Explicitly set for bidirectional streams
        statusCallback=f"{request.url_root}stream-status",
        statusCallbackMethod="POST"
    )
    
    # Add custom parameters
    stream.parameter(name="call_sid", value=call_sid)
    stream.parameter(name="from_number", value=from_number)
    stream.parameter(name="timestamp", value=str(int(time.time())))
    stream.parameter(name="minutes_left", value=str(minutes_left))
    stream.parameter(name="correlation_id", value=correlation_id)
    
    connect.append(stream)
    response.append(connect)
    
    # Fallback TwiML
    response.say("Thank you for practicing with ConvoReps!")
    response.pause(length=1)
    response.hangup()
    
    # Set up timer for time limit
    timer_duration = minutes_left * 60 if minutes_left < FREE_CALL_MINUTES else FREE_CALL_MINUTES * 60
    logger.info(f"Setting timer for {timer_duration:.0f} seconds", 
               extra={'correlation_id': correlation_id})
    
    timer = threading.Timer(timer_duration, lambda: handle_time_limit(call_sid, from_number))
    timer.start()
    
    with state_lock:
        call_timers[call_sid] = timer
    
    return str(response)

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
    
    if call_sid in active_streams:
        executor.submit(
            run_async_task,
            play_time_limit_message(call_sid, from_number)
        )

async def play_time_limit_message(call_sid: str, from_number: str):
    """Play the time limit message and end call"""
    if call_sid not in active_streams:
        return
    
    stream_sid = active_streams[call_sid].get('stream_sid')
    if not stream_sid:
        return
    
    ws = active_streams[call_sid]['ws']
    
    # Clear any ongoing audio
    try:
        ws.send(json.dumps({
            "event": "clear",
            "streamSid": stream_sid
        }))
    except:
        pass
    
    message = (
        "Alright, that's the end of your free call — but this is only the beginning. "
        "We just texted you a link to ConvoReps.com. Whether you're prepping for interviews "
        "or sharpening your pitch, this is how you level up — don't wait, your next opportunity is already calling."
    )
    
    await stream_tts_with_voice_id(call_sid, stream_sid, message, TIME_LIMIT_VOICE_ID)
    
    usage_data = read_user_usage(from_number)
    is_first_call = usage_data['total_calls'] <= 1
    
    await send_sms_link(from_number, is_first_call=is_first_call)
    
    await asyncio.sleep(2)
    
    if call_sid in active_streams and active_streams[call_sid].get('ws'):
        try:
            active_streams[call_sid]['ws'].close()
        except:
            pass

async def stream_tts_with_voice_id(call_sid: str, stream_sid: str, text: str, voice_id: str):
    """Stream TTS with a specific voice ID"""
    if call_sid not in active_streams:
        return
        
    ws = active_streams[call_sid]['ws']
    
    try:
        logger.info(f"Generating TTS with voice {voice_id}: {text[:50]}...")
        
        audio_stream = elevenlabs_client.text_to_speech.stream(
            text=text,
            voice_id=voice_id,
            model_id="eleven_turbo_v2_5",
            voice_settings=VoiceSettings(
                stability=0.5,
                similarity_boost=0.75,
                style=0.0,
                use_speaker_boost=True
            ),
            output_format="ulaw_8000",
            optimize_streaming_latency=4
        )
        
        chunk_count = 0
        audio_buffer = bytearray()
        
        for audio_chunk in audio_stream:
            if isinstance(audio_chunk, bytes) and audio_chunk:
                audio_buffer.extend(audio_chunk)
                
                while len(audio_buffer) >= 160:
                    chunk_to_send = bytes(audio_buffer[:160])
                    audio_buffer = audio_buffer[160:]
                    
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
                    
                    if chunk_count % 10 == 0:
                        await asyncio.sleep(0.001)
        
        if audio_buffer:
            payload = base64.b64encode(bytes(audio_buffer)).decode('utf-8')
            media_message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": payload
                }
            }
            ws.send(json.dumps(media_message))
        
        logger.info(f"Sent {chunk_count} audio chunks")
        
    except Exception as e:
        logger.error(f"TTS streaming error: {e}")
        with metrics_lock:
            metrics['api_errors']['elevenlabs'] += 1

@app.route("/stream-status", methods=["POST"])
def stream_status():
    """Handle stream status callbacks"""
    call_sid = request.values.get("CallSid")
    stream_sid = request.values.get("StreamSid")
    status = request.values.get("StreamStatus")
    
    logger.info(f"Stream status - CallSid: {call_sid}, StreamSid: {stream_sid}, Status: {status}")
    
    if status == "stopped":
        cleanup_call_resources(call_sid)
    
    return "", 204

@sock.route('/media-stream')
def media_stream(ws):
    """Enhanced WebSocket handler with security and monitoring"""
    stream_sid = None
    call_sid = None
    speech_processor = None
    last_keepalive = time.time()
    processing_lock = threading.Lock()
    audio_queue = asyncio.Queue()
    ws_start_time = time.time()
    correlation_id = None
    
    # Check connection limit
    with state_lock:
        if len(active_streams) >= MAX_WEBSOCKET_CONNECTIONS:
            logger.warning("Maximum WebSocket connections reached")
            ws.close()
            return
    
    try:
        while not shutdown_event.is_set():
            try:
                message = ws.receive(timeout=1.0)
            except Exception:
                # Keepalive
                if time.time() - last_keepalive > WEBSOCKET_KEEPALIVE_INTERVAL:
                    try:
                        ws.send(json.dumps({"event": "keepalive", "timestamp": time.time()}))
                        last_keepalive = time.time()
                    except:
                        break
                continue
                
            if message is None:
                break
                
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                logger.error("Invalid JSON received")
                continue
                
            event_type = data.get('event')
            
            if event_type == 'connected':
                logger.info(f"WebSocket connected: {data.get('protocol', 'unknown')}")
                ws.send(json.dumps({
                    "event": "connected_ack",
                    "timestamp": time.time()
                }))
                
            elif event_type == 'start':
                stream_sid = data['streamSid']
                
                # Validate stream SID
                if not validate_stream_sid(stream_sid):
                    logger.error(f"Invalid StreamSid: {stream_sid}")
                    ws.close()
                    return
                
                call_sid = data['start']['callSid']
                
                # Validate call SID
                if not validate_call_sid(call_sid):
                    logger.error(f"Invalid CallSid: {call_sid}")
                    ws.close()
                    return
                
                custom_params = data['start'].get('customParameters', {})
                from_number = custom_params.get('from_number', '')
                minutes_left = float(custom_params.get('minutes_left', FREE_CALL_MINUTES))
                correlation_id = custom_params.get('correlation_id', str(uuid.uuid4()))
                
                logger.info(f"Stream started - CallSid: {call_sid}, StreamSid: {stream_sid}", 
                           extra={'correlation_id': correlation_id})
                
                # Initialize stream data
                with processing_lock:
                    speech_processor = StreamingSpeechProcessor(call_sid)
                    
                    with state_lock:
                        active_streams[call_sid] = {
                            'ws': ws,
                            'stream_sid': stream_sid,
                            'from_number': from_number,
                            'minutes_left': minutes_left,
                            'processing': False,
                            'speech_processor': speech_processor,
                            'last_activity': time.time(),
                            'mark_received': set(),
                            'is_speaking': False,
                            'last_response_time': 0,
                            'lock': processing_lock,
                            'audio_queue': audio_queue,
                            'custom_params': custom_params,
                            'interruption_requested': False,
                            'correlation_id': correlation_id
                        }
                
            elif event_type == 'media' and call_sid and call_sid in active_streams:
                # Update activity
                with state_lock:
                    active_streams[call_sid]['last_activity'] = time.time()
                
                # Handle interruption
                if active_streams[call_sid].get('is_speaking', False):
                    try:
                        audio_chunk = base64.b64decode(data['media']['payload'])
                        if AUDIOOP_AVAILABLE:
                            rms = audioop.rms(audio_chunk, 1)
                        else:
                            audio_array = np.frombuffer(audio_chunk, dtype=np.uint8)
                            rms = np.sqrt(np.mean(np.square(audio_array.astype(np.float32))))
                        
                        if rms > 700:
                            active_streams[call_sid]['interruption_requested'] = True
                            ws.send(json.dumps({
                                "event": "clear",
                                "streamSid": stream_sid
                            }))
                    except:
                        pass
                    continue
                
                # Process audio
                try:
                    audio_chunk = base64.b64decode(data['media']['payload'])
                except Exception as e:
                    logger.error(f"Failed to decode audio: {e}")
                    continue
                
                complete_audio = speech_processor.add_audio(audio_chunk)
                
                if complete_audio:
                    with processing_lock:
                        if not active_streams[call_sid]['processing']:
                            current_time = time.time()
                            last_response = active_streams[call_sid].get('last_response_time', 0)
                            
                            if current_time - last_response < 0.3:
                                speech_processor.reset()
                                continue
                                
                            active_streams[call_sid]['processing'] = True
                            active_streams[call_sid]['last_response_time'] = current_time
                            
                            executor.submit(
                                run_async_task,
                                process_complete_utterance(call_sid, stream_sid, complete_audio)
                            )
                        
            elif event_type == 'mark':
                if call_sid in active_streams:
                    mark_data = data.get('mark', {})
                    mark_name = mark_data.get('name')
                    
                    with state_lock:
                        active_streams[call_sid]['mark_received'].add(mark_name)
                    
                    if mark_name == 'response_end':
                        active_streams[call_sid]['is_speaking'] = False
                        active_streams[call_sid]['processing'] = False
                        active_streams[call_sid]['interruption_requested'] = False
                        if speech_processor:
                            speech_processor.reset()
                    
            elif event_type == 'dtmf':
                if call_sid in active_streams:
                    dtmf_data = data.get('dtmf', {})
                    digit = dtmf_data.get('digit')
                    logger.info(f"DTMF received: {digit}", 
                               extra={'correlation_id': correlation_id})
                    
                    if digit == '#':
                        executor.submit(
                            run_async_task,
                            handle_reset(call_sid, stream_sid)
                        )
                    elif digit == '*':
                        executor.submit(
                            run_async_task,
                            repeat_last_response(call_sid, stream_sid)
                        )
                    
            elif event_type == 'stop':
                logger.info(f"Stream stopped - CallSid: {call_sid}", 
                           extra={'correlation_id': correlation_id})
                break
                
            elif event_type == 'error':
                error_code = data.get('error', {}).get('code')
                error_message = data.get('error', {}).get('message')
                logger.error(f"Stream error - Code: {error_code}, Message: {error_message}", 
                            extra={'correlation_id': correlation_id})
                
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        # Track WebSocket duration
        ws_duration = time.time() - ws_start_time
        with metrics_lock:
            metrics['websocket_duration_sum'] += ws_duration
            metrics['websocket_count'] += 1
        
        if call_sid:
            cleanup_call_resources(call_sid)

def cleanup_call_resources(call_sid: str):
    """Enhanced cleanup with metrics and logging"""
    correlation_id = str(uuid.uuid4())
    
    with state_lock:
        if call_sid in active_streams:
            from_number = active_streams[call_sid].get('from_number', '')
            
            # Calculate and update usage only if timer didn't already do it
            if call_sid in call_start_times and call_sid not in call_timers:
                # Timer was cancelled or didn't fire, so we need to update usage
                call_duration = time.time() - call_start_times[call_sid]
                minutes_used = call_duration / 60.0
                
                if from_number and validate_phone_number(from_number):
                    # Check if we need to update (timer might have already done it)
                    usage_before = read_user_usage(from_number)
                    
                    # Only update if there's actual time to update
                    if minutes_used > 0.01:  # More than ~1 second
                        update_user_usage(from_number, minutes_used)
                        logger.info(f"Call {call_sid} lasted {minutes_used:.2f} minutes", 
                                   extra={'correlation_id': correlation_id})
                        
                        # Check if SMS needed (only if time just ran out)
                        usage_after = read_user_usage(from_number)
                        if usage_before['minutes_left'] > 0.5 and usage_after['minutes_left'] <= 0.5:
                            is_first_call = usage_after['total_calls'] <= 1
                            executor.submit(run_async_task, 
                                          send_sms_link(from_number, is_first_call=is_first_call))
                
                call_start_times.pop(call_sid, None)
            
            # Cancel timer
            if call_sid in call_timers:
                call_timers[call_sid].cancel()
                call_timers.pop(call_sid, None)
            
            # Close WebSocket
            ws = active_streams[call_sid].get('ws')
            if ws:
                try:
                    ws.close()
                except:
                    pass
            
            # Close ElevenLabs WebSocket
            if call_sid in elevenlabs_websockets:
                try:
                    asyncio.run(elevenlabs_websockets[call_sid].close())
                except:
                    pass
                elevenlabs_websockets.pop(call_sid, None)
            
            # Clean up state
            active_streams.pop(call_sid, None)
            turn_count.pop(call_sid, None)
            conversation_history.pop(call_sid, None)
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
    
    # Update metrics
    with metrics_lock:
        metrics['active_calls'] = max(0, metrics['active_calls'] - 1)
    
    gc.collect()

def run_async_task(coro):
    """Run async coroutine with proper error handling"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    except Exception as e:
        logger.error(f"Async task error: {e}", exc_info=True)
    finally:
        loop.close()

async def process_complete_utterance(call_sid: str, stream_sid: str, audio_data: bytes):
    """Process utterance with enhanced error handling"""
    if call_sid not in active_streams:
        return
    
    correlation_id = active_streams[call_sid].get('correlation_id', str(uuid.uuid4()))
    
    try:
        with state_lock:
            active_streams[call_sid]['is_speaking'] = True
        
        # Convert audio
        audio_pcm = convert_mulaw_to_pcm(audio_data)
        audio_data = None
        
        # Transcribe
        transcript = await asyncio.wait_for(
            transcribe_audio(audio_pcm),
            timeout=5.0
        )
        audio_pcm = None
        
        if transcript and len(transcript.strip()) > 1:
            logger.info(f"Transcript: {transcript}", 
                       extra={'correlation_id': correlation_id})
            
            # Spam filter
            spam_patterns = [
                r"beadaholique\.com", r"go\s+to", r"\.com", r"www\.", r"http",
                r"woof\s+woof", r"bark\s+bark", r"meow", r"click\s+here"
            ]
            if any(re.search(pattern, transcript.lower()) for pattern in spam_patterns):
                logger.info("Ignoring spam/noise transcript", 
                           extra={'correlation_id': correlation_id})
                return
            
            # Check for reset
            if "let's start over" in transcript.lower() or "reset" in transcript.lower():
                await handle_reset(call_sid, stream_sid)
                return
            
            # Generate response
            response = await generate_response(call_sid, transcript)
            
            # Cache response for repeat function
            with state_lock:
                last_response_cache[call_sid] = response
            
            # Stream response
            if ELEVENLABS_WEBSOCKET_ENABLED:
                await stream_tts_websocket(call_sid, stream_sid, response)
            else:
                await stream_tts_response(call_sid, stream_sid, response)
            
    except asyncio.TimeoutError:
        logger.warning(f"Processing timeout for call {call_sid}", 
                      extra={'correlation_id': correlation_id})
    except Exception as e:
        logger.error(f"Processing error: {e}", 
                    extra={'correlation_id': correlation_id},
                    exc_info=True)
    finally:
        with state_lock:
            if call_sid in active_streams:
                active_streams[call_sid]['processing'] = False

async def handle_reset(call_sid: str, stream_sid: str):
    """Handle conversation reset"""
    logger.info("Reset triggered by user")
    
    with state_lock:
        conversation_history.pop(call_sid, None)
        personality_memory.pop(call_sid, None)
        mode_lock.pop(call_sid, None)
        turn_count[call_sid] = 0
        interview_question_index.pop(call_sid, None)
        last_response_cache.pop(call_sid, None)
    
    reset_message = "Alright, let's start fresh. What would you like to practice?"
    
    if ELEVENLABS_WEBSOCKET_ENABLED:
        await stream_tts_websocket(call_sid, stream_sid, reset_message)
    else:
        await stream_tts_response(call_sid, stream_sid, reset_message)

async def repeat_last_response(call_sid: str, stream_sid: str):
    """Repeat the last assistant response"""
    # First check cache
    with state_lock:
        cached_response = last_response_cache.get(call_sid)
    
    if cached_response:
        if ELEVENLABS_WEBSOCKET_ENABLED:
            await stream_tts_websocket(call_sid, stream_sid, cached_response)
        else:
            await stream_tts_response(call_sid, stream_sid, cached_response)
        return
    
    # Fall back to conversation history
    if call_sid in conversation_history:
        messages = conversation_history[call_sid]
        for message in reversed(messages):
            if message['role'] == 'assistant':
                if ELEVENLABS_WEBSOCKET_ENABLED:
                    await stream_tts_websocket(call_sid, stream_sid, message['content'])
                else:
                    await stream_tts_response(call_sid, stream_sid, message['content'])
                return
    
    no_response_message = "I don't have a previous response to repeat."
    if ELEVENLABS_WEBSOCKET_ENABLED:
        await stream_tts_websocket(call_sid, stream_sid, no_response_message)
    else:
        await stream_tts_response(call_sid, stream_sid, no_response_message)

async def generate_response(call_sid: str, transcript: str) -> str:
    """Generate response with tool calling (non-streaming for reliability)"""
    correlation_id = active_streams[call_sid].get('correlation_id', str(uuid.uuid4()))
    
    # Update turn count
    with state_lock:
        turn = turn_count.get(call_sid, 0)
        turn_count[call_sid] = turn + 1
    
    # Detect intent
    mode = mode_lock.get(call_sid)
    if not mode:
        mode = detect_intent(transcript)
        with state_lock:
            mode_lock[call_sid] = mode
    
    logger.info(f"Mode: {mode}, Turn: {turn}", 
               extra={'correlation_id': correlation_id})
    
    # Initialize conversation history
    with state_lock:
        if call_sid not in conversation_history:
            conversation_history[call_sid] = []
        
        conversation_history[call_sid].append({
            "role": "user",
            "content": transcript
        })
        
        # Limit history
        if len(conversation_history[call_sid]) > 10:
            conversation_history[call_sid] = conversation_history[call_sid][-10:]
    
    # Get personality
    voice_id, system_prompt, intro_line = get_personality_for_mode(call_sid, mode)
    
    # Handle interview questions
    if mode == "interview":
        with state_lock:
            if call_sid not in interview_question_index:
                interview_question_index[call_sid] = 0
            
            current_question_idx = interview_question_index[call_sid]
            if current_question_idx < len(interview_questions):
                system_prompt += f"\n\nAsk this question next: {interview_questions[current_question_idx]}"
    
    # Build messages
    messages = [{"role": "system", "content": system_prompt}]
    
    # Add time awareness if low
    if call_sid in call_start_times:
        elapsed_minutes = (time.time() - call_start_times[call_sid]) / 60.0
        minutes_left = active_streams[call_sid].get('minutes_left', FREE_CALL_MINUTES)
        remaining = max(0, minutes_left - elapsed_minutes)
        
        if remaining < 2.0:  # Less than 2 minutes
            messages[0]["content"] += (
                f"\n\nIMPORTANT: The user has less than {remaining:.1f} minutes left. "
                "You should proactively use the check_remaining_time tool to inform them about their remaining time. "
                "Don't wait for them to ask - be helpful and let them know time is running low."
            )
    
    # Check for bad news
    if detect_bad_news(transcript):
        messages = add_bad_news_context(messages, transcript)
    
    # Add conversation history
    messages.extend(conversation_history[call_sid])
    
    # Define tool with clear description
    tools = [
        {
            "type": "function",
            "function": {
                "name": "check_remaining_time",
                "description": "Check how many minutes the user has left in their free call. Use this when: 1) User asks about time, 2) You notice time might be running low, 3) The conversation seems to be wrapping up.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        }
    ]
    
    # Generate response (non-streaming for tool calling reliability)
    try:
        completion = await async_openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.7,
            max_tokens=60,
            tools=tools,
            tool_choice="auto"
        )
        
        message = completion.choices[0].message
        
        # Check for tool calls
        if hasattr(message, 'tool_calls') and message.tool_calls:
            logger.info(f"Tool calls detected: {len(message.tool_calls)}", 
                       extra={'correlation_id': correlation_id})
            
            with metrics_lock:
                metrics['tool_calls_made'] += 1
            
            # Process tool calls
            for tool_call in message.tool_calls:
                if tool_call.function.name == "check_remaining_time":
                    # Calculate remaining time
                    from_number = active_streams[call_sid].get('from_number', '')
                    if from_number and call_sid in call_start_times:
                        elapsed_minutes = (time.time() - call_start_times[call_sid]) / 60.0
                        minutes_left = active_streams[call_sid].get('minutes_left', FREE_CALL_MINUTES)
                        remaining = max(0, minutes_left - elapsed_minutes)
                        
                        # Check if time is exhausted
                        if remaining <= MIN_CALL_DURATION:
                            logger.info(f"Tool detected time exhausted for {call_sid}", 
                                      extra={'correlation_id': correlation_id})
                            
                            # Update CSV immediately before time runs out
                            elapsed_minutes = (time.time() - call_start_times[call_sid]) / 60.0
                            update_user_usage(from_number, elapsed_minutes)
                            
                            # Cancel the timer since we're handling it now
                            if call_sid in call_timers:
                                call_timers[call_sid].cancel()
                                call_timers.pop(call_sid, None)
                            
                            # Trigger end flow
                            executor.submit(
                                run_async_task,
                                play_time_limit_message(call_sid, from_number)
                            )
                            
                            return "Your free time is up! I'm sending you a link to continue practicing."
                        
                        # Add assistant message with tool call
                        messages.append(message)
                        
                        # Add tool response
                        tool_response = f"User has {remaining:.1f} minutes remaining in their free call."
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_response
                        })
                        
                        # Get final response
                        final_completion = await async_openai.chat.completions.create(
                            model="gpt-3.5-turbo",
                            messages=messages,
                            temperature=0.7,
                            max_tokens=60
                        )
                        full_response = final_completion.choices[0].message.content
                    else:
                        full_response = message.content or "I didn't catch that. Could you repeat?"
                else:
                    full_response = message.content or "I didn't catch that. Could you repeat?"
        else:
            full_response = message.content or "I didn't catch that. Could you repeat?"
        
        # Clean response
        reply = clean_response_text(full_response)
        
        # Truncate if needed
        sentences = re.split(r'(?<=[.!?])\s+', reply)
        if len(sentences) > 2:
            reply = ' '.join(sentences[:2])
        
        # Store in history
        with state_lock:
            conversation_history[call_sid].append({
                "role": "assistant",
                "content": reply
            })
            
            # Update interview index
            if mode == "interview":
                interview_question_index[call_sid] = (interview_question_index[call_sid] + 1) % len(interview_questions)
        
        return reply
        
    except Exception as e:
        logger.error(f"GPT error: {e}", 
                    extra={'correlation_id': correlation_id},
                    exc_info=True)
        with metrics_lock:
            metrics['api_errors']['openai'] += 1
        return "I didn't catch that. Could you repeat?"

def detect_intent(text: str) -> str:
    """Enhanced intent detection"""
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

def add_bad_news_context(messages: List[Dict[str, str]], transcript: str) -> List[Dict[str, str]]:
    """Add context for bad news scenarios"""
    escalation_level = 1
    
    if any(phrase in transcript.lower() for phrase in ["calm down", "relax", "it's not my fault", "not my problem"]):
        escalation_level = 2
    elif any(phrase in transcript.lower() for phrase in ["deal with it", "too bad", "whatever"]):
        escalation_level = 3
    
    escalation_prompts = {
        1: "The user just delivered bad news. Respond based on your personality, showing reasonable frustration. Keep response SHORT - 1-2 sentences max.",
        2: "The user got defensive, so you're more upset but still professional. Express frustration clearly but briefly.",
        3: "The user is being dismissive. You're quite frustrated now but keep it brief and end the conversation if needed."
    }
    
    messages.insert(0, {
        "role": "system", 
        "content": escalation_prompts.get(escalation_level, escalation_prompts[1])
    })
    return messages

def get_personality_for_mode(call_sid: str, mode: str) -> Tuple[str, str, str]:
    """Get personality configuration"""
    if mode == "cold_call":
        with state_lock:
            if call_sid not in personality_memory:
                persona_name = random.choice(list(cold_call_personality_pool.keys()))
                personality_memory[call_sid] = persona_name
            else:
                persona_name = personality_memory[call_sid]
            
        persona = cold_call_personality_pool[persona_name]
        return (
            persona["voice_id"],
            persona["system_prompt"],
            "Hello?"
        )
        
    elif mode == "interview":
        voice_pool = [
            {"voice_id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel"},
            {"voice_id": "EXAVITQu4vr4xnSDxMaL", "name": "Clyde"},
            {"voice_id": "6YQMyaUWlj0VX652cY1C", "name": "Stephen"}
        ]
        
        with state_lock:
            if call_sid not in personality_memory:
                voice_choice = random.choice(voice_pool)
                personality_memory[call_sid] = voice_choice
            else:
                voice_choice = personality_memory[call_sid]
            
        system_prompt = (
            f"You are {voice_choice['name']}, a friendly, conversational job interviewer. "
            "Ask one interview question at a time, give supportive feedback. "
            "Keep your tone upbeat and natural. Keep responses SHORT - 1-2 sentences. "
            "Be encouraging and professional.\n\n"
            "You have access to 'check_remaining_time' tool. If the interview has been going on "
            "for more than 4 minutes, casually check and mention time remaining: "
            "'We're making good progress - just to let you know, we have about X minutes left.'"
        )
        
        return (
            voice_choice["voice_id"],
            system_prompt,
            "Great, let's start. Tell me about yourself."
        )
        
    else:  # small_talk
        return (
            "2BJW5coyhAzSr8STdHbE",
            "You're a casual, sarcastic friend. Keep it light and fun. Use humor and be relatable. SHORT responses only - 1-2 sentences max.",
            "Hey, what's up?"
        )

def clean_response_text(text: str) -> str:
    """Clean response text"""
    text = re.sub(r'[*_`#]', '', text)
    text = re.sub(r'\s+([,.!?])', r'\1', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

async def stream_tts_websocket(call_sid: str, stream_sid: str, text: str):
    """Stream TTS using ElevenLabs WebSocket"""
    if call_sid not in active_streams:
        return
    
    ws = active_streams[call_sid]['ws']
    correlation_id = active_streams[call_sid].get('correlation_id', str(uuid.uuid4()))
    
    mode = mode_lock.get(call_sid, "cold_call")
    voice_id, _, _ = get_personality_for_mode(call_sid, mode)
    
    elevenlabs_ws = None
    keepalive_task = None
    try:
        # Build WebSocket URL with all parameters
        model_id = "eleven_turbo_v2_5"
        elevenlabs_url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
            f"?model_id={model_id}"
            f"&optimize_streaming_latency=4"
            f"&output_format=ulaw_8000"
            f"&inactivity_timeout=180"
        )
        
        headers = {
            "xi-api-key": os.getenv("ELEVENLABS_API_KEY")
        }
        
        async with websockets.connect(elevenlabs_url, additional_headers=headers) as elevenlabs_ws:
            elevenlabs_websockets[call_sid] = elevenlabs_ws
            
            # Start keepalive task
            async def keepalive():
                try:
                    while elevenlabs_ws.open:
                        await asyncio.sleep(ELEVENLABS_KEEPALIVE_INTERVAL)
                        if elevenlabs_ws.open:
                            await elevenlabs_ws.send(json.dumps({"text": " "}))
                            logger.debug(f"Sent ElevenLabs keepalive for {call_sid}")
                except:
                    pass
            
            keepalive_task = asyncio.create_task(keepalive())
            
            # Send initial configuration
            init_message = {
                "text": " ",
                "voice_settings": {
                    "stability": 0.4,
                    "similarity_boost": 0.75,
                    "style": 0.0,
                    "use_speaker_boost": True
                },
                "generation_config": {
                    "chunk_length_schedule": [50, 90, 120, 150]
                }
            }
            
            await elevenlabs_ws.send(json.dumps(init_message))
            
            # Send text in sentences
            sentences = re.split(r'(?<=[.!?])\s+', text)
            sentences = sentences[:2]
            
            for i, sentence in enumerate(sentences):
                if sentence.strip():
                    if active_streams[call_sid].get('interruption_requested', False):
                        break
                    
                    text_message = {
                        "text": sentence + " ",
                        "flush": i == len(sentences) - 1
                    }
                    await elevenlabs_ws.send(json.dumps(text_message))
            
            # Send end of sequence
            await elevenlabs_ws.send(json.dumps({"text": ""}))
            
            # Receive and forward audio
            chunk_count = 0
            while True:
                try:
                    response = await asyncio.wait_for(elevenlabs_ws.recv(), timeout=5.0)
                    data = json.loads(response)
                    
                    if data.get("audio"):
                        if active_streams[call_sid].get('interruption_requested', False):
                            break
                        
                        media_message = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": data["audio"]
                            }
                        }
                        ws.send(json.dumps(media_message))
                        chunk_count += 1
                        
                        if chunk_count % 10 == 0:
                            mark_message = {
                                "event": "mark",
                                "streamSid": stream_sid,
                                "mark": {
                                    "name": f"audio_chunk_{chunk_count}"
                                }
                            }
                            ws.send(json.dumps(mark_message))
                    
                    if data.get("isFinal"):
                        break
                        
                except asyncio.TimeoutError:
                    logger.warning("ElevenLabs WebSocket timeout", 
                                 extra={'correlation_id': correlation_id})
                    break
                except Exception as e:
                    logger.error(f"ElevenLabs WebSocket receive error: {e}", 
                               extra={'correlation_id': correlation_id})
                    break
            
            # Send end marker
            end_mark = {
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {
                    "name": "response_end"
                }
            }
            ws.send(json.dumps(end_mark))
            
            logger.info(f"Sent {chunk_count} audio chunks via WebSocket", 
                       extra={'correlation_id': correlation_id})
            
    except websockets.exceptions.WebSocketException as e:
        logger.error(f"ElevenLabs WebSocket connection error: {e}", 
                    extra={'correlation_id': correlation_id})
        with metrics_lock:
            metrics['api_errors']['elevenlabs'] += 1
        # Fallback to HTTP
        await stream_tts_response(call_sid, stream_sid, text)
    except Exception as e:
        logger.error(f"TTS WebSocket error: {e}", 
                    extra={'correlation_id': correlation_id},
                    exc_info=True)
        await stream_tts_response(call_sid, stream_sid, text)
    finally:
        # Cancel keepalive task
        if keepalive_task:
            keepalive_task.cancel()
        # Clean up WebSocket reference
        if call_sid in elevenlabs_websockets:
            elevenlabs_websockets.pop(call_sid, None)

async def stream_tts_response(call_sid: str, stream_sid: str, text: str):
    """Fallback TTS streaming via HTTP"""
    if call_sid not in active_streams:
        return
    
    ws = active_streams[call_sid]['ws']
    correlation_id = active_streams[call_sid].get('correlation_id', str(uuid.uuid4()))
    
    mode = mode_lock.get(call_sid, "cold_call")
    voice_id, _, _ = get_personality_for_mode(call_sid, mode)
    
    sentences = re.split(r'(?<=[.!?])\s+', text)
    sentences = sentences[:2]
    
    for i, sentence in enumerate(sentences):
        if sentence.strip():
            if active_streams[call_sid].get('interruption_requested', False):
                break
                
            try:
                logger.info(f"Generating TTS for: {sentence[:50]}...", 
                           extra={'correlation_id': correlation_id})
                
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
                    output_format="ulaw_8000",
                    optimize_streaming_latency=4
                )
                
                chunk_count = 0
                audio_buffer = bytearray()
                
                for audio_chunk in audio_stream:
                    if isinstance(audio_chunk, bytes) and audio_chunk:
                        audio_buffer.extend(audio_chunk)
                        
                        while len(audio_buffer) >= 160:
                            chunk_to_send = bytes(audio_buffer[:160])
                            audio_buffer = audio_buffer[160:]
                            
                            if active_streams[call_sid].get('interruption_requested', False):
                                break
                            
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
                            
                            if chunk_count % 10 == 0:
                                await asyncio.sleep(0.001)
                
                if audio_buffer and not active_streams[call_sid].get('interruption_requested', False):
                    payload = base64.b64encode(bytes(audio_buffer)).decode('utf-8')
                    media_message = {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {
                            "payload": payload
                        }
                    }
                    ws.send(json.dumps(media_message))
                
                mark_message = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {
                        "name": f"sentence_{i}"
                    }
                }
                ws.send(json.dumps(mark_message))
                
                logger.info(f"Sent {chunk_count} audio chunks for sentence {i}", 
                           extra={'correlation_id': correlation_id})
                
                del audio_stream
                gc.collect()
                
            except Exception as e:
                logger.error(f"TTS streaming error: {e}", 
                            extra={'correlation_id': correlation_id},
                            exc_info=True)
                with metrics_lock:
                    metrics['api_errors']['elevenlabs'] += 1
    
    await asyncio.sleep(0.1)
    end_mark = {
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {
            "name": "response_end"
        }
    }
    ws.send(json.dumps(end_mark))

def convert_mulaw_to_pcm(mulaw_data: bytes) -> bytes:
    """Convert mulaw to PCM with validation"""
    try:
        if not mulaw_data or not isinstance(mulaw_data, bytes):
            return b''
        
        if len(mulaw_data) > 100000:
            logger.warning(f"Audio chunk too large: {len(mulaw_data)} bytes, truncating")
            mulaw_data = mulaw_data[:100000]
            
        if AUDIOOP_AVAILABLE and audioop:
            try:
                pcm_8khz = audioop.ulaw2lin(mulaw_data, 2)
                pcm_16khz = audioop.ratecv(pcm_8khz, 2, 1, 8000, 16000, None)[0]
                return pcm_16khz
            except audioop.error as e:
                logger.warning(f"audioop conversion error: {e}, falling back to pydub")
        
        mulaw_buffer = io.BytesIO(mulaw_data)
        audio = AudioSegment.from_raw(
            mulaw_buffer,
            sample_width=1,
            frame_rate=8000,
            channels=1,
            frame_width=1
        )
        audio = audio.set_sample_width(2).set_frame_rate(16000)
        return audio.raw_data
            
    except Exception as e:
        logger.error(f"Audio conversion error: {e}")
        return b''

async def transcribe_audio(audio_pcm: bytes) -> str:
    """Transcribe audio with retry logic"""
    if not audio_pcm or len(audio_pcm) < 1600:
        return ""
    
    retry_count = 0
    max_retries = 3
    
    while retry_count < max_retries:
        try:
            audio_buffer = io.BytesIO()
            
            audio = AudioSegment(
                data=audio_pcm,
                sample_width=2,
                frame_rate=16000,
                channels=1
            )
            
            audio.export(
                audio_buffer, 
                format="wav",
                parameters=["-ac", "1", "-ar", "16000"]
            )
            audio_buffer.seek(0)
            audio_buffer.name = "audio.wav"
            
            del audio
            
            result = await async_openai.audio.transcriptions.create(
                model="whisper-1",
                file=audio_buffer,
                language="en",
                response_format="text",
                temperature=0.0
            )
            
            return result.strip()
            
        except Exception as e:
            retry_count += 1
            if retry_count >= max_retries:
                logger.error(f"Transcription error after {max_retries} retries: {e}")
                with metrics_lock:
                    metrics['api_errors']['openai'] += 1
                return ""
            await asyncio.sleep(2 ** retry_count)

@app.route("/sms", methods=["POST"])
def sms_webhook():
    """Handle incoming SMS with validation"""
    correlation_id = str(uuid.uuid4())
    
    try:
        # Security validation
        if os.getenv("TWILIO_AUTH_TOKEN"):
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
                    extra={'correlation_id': correlation_id})
        return "", 200

# Legacy endpoints for compatibility
@app.route("/partial_speech", methods=["POST"])
def partial_speech():
    """Legacy endpoint - not used with bidirectional streams"""
    return "", 204

@app.route("/process_speech", methods=["POST"])
def process_speech():
    """Legacy endpoint - redirect to voice"""
    response = VoiceResponse()
    response.redirect("/voice")
    return str(response)

# Static file serving
@app.route("/static/<path:filename>")
def static_files(filename):
    """Serve static files securely"""
    # Validate filename
    if '..' in filename or filename.startswith('/'):
        return "Invalid filename", 400
    return send_from_directory("static", filename)

# Enhanced health and monitoring endpoints
@app.route("/health", methods=["GET"])
def health_check():
    """Enhanced health check"""
    with state_lock:
        active_count = len(active_streams)
        ws_health = "healthy" if active_count < MAX_WEBSOCKET_CONNECTIONS * 0.9 else "warning"
    
    with metrics_lock:
        error_rate = sum(metrics['api_errors'].values()) / max(metrics['total_calls'], 1)
        api_health = "healthy" if error_rate < 0.1 else "degraded"
    
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "active_streams": active_count,
        "websocket_health": ws_health,
        "api_health": api_health,
        "memory_usage_mb": get_memory_usage(),
        "websocket_enabled": {
            "elevenlabs": ELEVENLABS_WEBSOCKET_ENABLED,
            "openai_streaming": OPENAI_STREAMING_ENABLED
        },
        "version": "4.2"
    }

@app.route("/metrics", methods=["GET"])
def metrics_endpoint():
    """Detailed metrics"""
    with state_lock:
        active_count = len(active_streams)
        mode_breakdown = {
            "cold_call": sum(1 for m in mode_lock.values() if m == "cold_call"),
            "interview": sum(1 for m in mode_lock.values() if m == "interview"),
            "small_talk": sum(1 for m in mode_lock.values() if m == "small_talk")
        }
        active_timers = len(call_timers)
    
    with metrics_lock:
        avg_ws_duration = (
            metrics['websocket_duration_sum'] / max(metrics['websocket_count'], 1)
            if metrics['websocket_count'] > 0 else 0
        )
        
        metrics_data = {
            "active_streams": active_count,
            "total_calls": metrics['total_calls'],
            "failed_calls": metrics['failed_calls'],
            "conversation_modes": mode_breakdown,
            "memory_usage_mb": get_memory_usage(),
            "websocket_connections": {
                "twilio": active_count,
                "elevenlabs": len(elevenlabs_websockets)
            },
            "websocket_metrics": {
                "average_duration_seconds": avg_ws_duration,
                "total_connections": metrics['websocket_count']
            },
            "timer_stats": {
                "active_timers": active_timers,
                "free_minutes_per_user": FREE_CALL_MINUTES
            },
            "sms_stats": {
                "sent": metrics['sms_sent'],
                "failed": metrics['sms_failed'],
                "success_rate": metrics['sms_sent'] / max(metrics['sms_sent'] + metrics['sms_failed'], 1)
            },
            "tool_calls": metrics['tool_calls_made'],
            "api_errors": metrics['api_errors'],
            "csv_stats": get_csv_statistics()
        }
    
    return metrics_data

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

def get_memory_usage() -> float:
    """Get memory usage"""
    try:
        import psutil
        process = psutil.Process()
        return process.memory_info().rss / 1024 / 1024
    except:
        return 0.0

@app.route("/ws-test", methods=["GET"])
def ws_test():
    """WebSocket test endpoint"""
    return {
        "websocket_support": True,
        "active_streams": len(active_streams),
        "flask_sock_version": "0.7.0",
        "streaming_enabled": {
            "elevenlabs_websocket": ELEVENLABS_WEBSOCKET_ENABLED,
            "openai_streaming": OPENAI_STREAMING_ENABLED
        },
        "max_connections": MAX_WEBSOCKET_CONNECTIONS,
        "ready": True
    }

if __name__ == "__main__":
    # Ensure directories exist
    os.makedirs("static", exist_ok=True)
    os.makedirs(os.path.dirname(USAGE_CSV_PATH) or ".", exist_ok=True)
    
    # Generate required static files if missing
    if not os.path.exists("static/beep.mp3"):
        logger.warning("beep.mp3 not found - generating")
        try:
            from pydub.generators import Sine
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
    
    logger.info("\n🚀 ConvoReps WebSocket Streaming Edition v4.2 FINAL - PRODUCTION READY")
    logger.info("   ✅ Non-streaming tool calling for reliability")
    logger.info("   ✅ SMS deduplication with atomic operations")
    logger.info("   ✅ CSV atomic writes with temp files")
    logger.info("   ✅ Comprehensive input validation")
    logger.info("   ✅ WebSocket security validation")
    logger.info("   ✅ Enhanced error recovery and retries")
    logger.info("   ✅ Backup mechanisms for critical data")
    logger.info("   ✅ Advanced health checks")
    logger.info("   ✅ Detailed monitoring metrics")
    logger.info("   ✅ Structured logging with correlation IDs")
    logger.info("   ✅ Graceful shutdown handling")
    logger.info("   ✅ All original features retained")
    logger.info(f"\n   Configuration:")
    logger.info(f"   - ElevenLabs WebSocket: {'✅ Enabled' if ELEVENLABS_WEBSOCKET_ENABLED else '❌ Disabled'}")
    logger.info(f"   - OpenAI Streaming: {'✅ Enabled' if OPENAI_STREAMING_ENABLED else '❌ Disabled'}")
    logger.info(f"   - Max Connections: {MAX_WEBSOCKET_CONNECTIONS}")
    logger.info(f"   - Free Call Minutes: {FREE_CALL_MINUTES}")
    logger.info(f"   - Min Call Duration: {MIN_CALL_DURATION}")
    logger.info(f"   - Usage CSV: {USAGE_CSV_PATH}")
    logger.info(f"   - ConvoReps URL: {CONVOREPS_URL}")
    logger.info("\n")
    
    # Run with production settings
    app.run(
        host="0.0.0.0",
        port=5050,
        debug=False,
        threaded=True,
        use_reloader=False
    )

# FINAL VERSION 4.2 - Production Ready with All Fixes
# v4.1: Fixed logging error where correlation_id was missing
# v4.2: Fixed websockets parameter from extra_headers to additional_headers
