# ConvoReps — AI-Powered Voice Training Platform

ConvoReps is a sophisticated real-time voice simulation system that helps sales representatives, job seekers, and English learners practice conversations via phone. Using cutting-edge AI technologies, it provides realistic, interactive practice sessions with dynamic personalities and natural conversation flow.

## 🌟 Key Features

### Practice Modes
- **🎯 Cold Call Practice**: Train with 13 unique customer personalities, each with distinct attitudes and objection patterns
- **👔 Interview Prep**: Practice with professional interviewers who provide real-time feedback
- **💬 Small Talk**: Improve casual conversation skills with a friendly, sarcastic AI companion

### Advanced Capabilities
- **🎭 Voice Consistency**: Once a mode is selected, the same voice persona is maintained throughout the call
- **⏱️ Smart Time Management**: 
  - 3 free minutes per user with SMS notifications
  - CSV-based usage tracking
  - Whitelist support for unlimited access
- **🔄 Real-Time Processing**: 
  - Streaming transcription and text-to-speech
  - Partial speech recognition for early intent detection
  - Sentence-by-sentence audio generation
- **😤 Dynamic Interactions**: 
  - Bad news detection with escalating responses
  - Natural conversation flow with context awareness
  - Tool calling for time checks
- **📱 SMS Integration**: Automatic SMS notifications with ConvoReps links when time expires
- **🧹 Automatic Cleanup**: Resources are cleaned up after calls to prevent memory leaks

## 🛠️ Technology Stack

- **Backend**: Python Flask
- **Voice Platform**: Twilio Programmable Voice
- **AI Models**: 
  - OpenAI Whisper (transcription)
  - OpenAI GPT-4.1-nano (conversation)
  - ElevenLabs (text-to-speech with multiple voices)
- **Call Bridge**: Amazon Connect (for international testing)
- **Storage**: CSV files for usage tracking
- **Deployment**: Heroku-ready with Gunicorn

## 📋 Prerequisites

1. **Python 3.8+**
2. **Twilio Account** with:
   - Verified phone number
   - Account SID and Auth Token
3. **OpenAI API Key** with access to:
   - Whisper API
   - GPT-4.1-nano or GPT-4.1-mini
4. **ElevenLabs API Key** with:
   - Access to multiple voice IDs
   - Streaming capabilities
5. **Public URL** (use ngrok for local development)

## 🚀 Detailed Setup Instructions

### 1. Clone the Repository
```bash
git clone https://github.com/yourusername/convoreps.git
cd convoreps
```

### 2. Create Virtual Environment
```bash
python -m venv venv

# On Windows
venv\Scripts\activate

# On macOS/Linux
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Environment Configuration
Create a `.env` file in the root directory:

```env
# OpenAI Configuration
OPENAI_API_KEY=your_openai_api_key_here

# ElevenLabs Configuration
ELEVENLABS_API_KEY=your_elevenlabs_api_key_here

# Twilio Configuration
TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_PHONE_NUMBER=+1234567890  # Your Twilio phone number

# Flask Configuration
FLASK_SECRET_KEY=your_secret_key_here

# Streaming Configuration (optional)
USE_STREAMING=true
SENTENCE_STREAMING=true
STREAMING_TIMEOUT=2.0

# Model Configuration (optional - defaults shown)
OPENAI_STREAMING_MODEL=gpt-4.1-nano
OPENAI_STANDARD_MODEL=gpt-4.1-nano
OPENAI_STREAMING_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
OPENAI_STANDARD_TRANSCRIBE_MODEL=whisper-1
ELEVENLABS_VOICE_MODEL=eleven_turbo_v2_5
ELEVENLABS_OUTPUT_FORMAT=mp3_22050_32

# Usage Tracking (optional - defaults shown)
FREE_CALL_MINUTES=3.0
MIN_CALL_DURATION=0.5
USAGE_CSV_PATH=user_usage.csv
USAGE_CSV_BACKUP_PATH=user_usage_backup.csv

# Debug Options (optional)
DEBUG_PARTIAL=false
DEBUG_CONVERSATION=false
```

### 5. Generate Static Files
```bash
# Generate beep sound
python generate_beep.py

# Generate greeting messages
python synthesize_greetings.py
```

### 6. Configure Twilio Phone Number

In your Twilio Console:

1. Navigate to Phone Numbers > Manage > Active Numbers
2. Click on your phone number
3. Configure Voice & Fax settings:
   - **A CALL COMES IN**: 
     - Webhook: `https://your-domain.com/voice`
     - HTTP Method: `POST`
   - **CALL STATUS CHANGES**: 
     - Webhook: `https://your-domain.com/call_status`
     - HTTP Method: `POST`
   - **PRIMARY HANDLER FAILS**: Leave empty

### 7. Start the Application

For development:
```bash
python app.py
```

For production:
```bash
gunicorn app:app
```

### 8. Testing with ngrok (Local Development)
```bash
# In a new terminal
ngrok http 5050

# Update your Twilio webhooks with the ngrok URL
# Example: https://abc123.ngrok.io/voice
```

## 📞 Usage Guide

### Making Your First Call

1. **Call the configured Twilio number**
2. **Listen to the greeting** (different for first-time vs returning users)
3. **After the beep, say one of**:
   - "Cold call" or "Sales practice" → Practice with challenging customers
   - "Interview prep" or "Job interview" → Practice with professional interviewers
   - "Small talk" or "Just chat" → Have casual conversations
4. **Engage in natural conversation** - the AI will respond based on the selected mode

### Special Commands

- **"Let's start over"**: Reset the conversation and get a new personality
- **"How much time do I have?"**: Check remaining free minutes
- **Whitelist number**: +13368236243 has unlimited minutes for testing

### Cold Call Personalities

The system randomly assigns one of these personalities:
- **Jerry**: Skeptical, been burned before
- **Miranda**: Busy office manager
- **Brett**: Annoyed contractor
- **Kayla**: Sharp business owner
- **Jamahal**: Street-smart, no-nonsense
- **Hope**: Polite but cautious
- **Brad**: Casual and distracted
- **Stephen**: Formal, wants data
- **Edward**: Sarcastic, tests you
- **Kendall**: Wants you to get to the point
- **RJ**: Easily distracted
- **Dakota**: Reserved, hard to read
- **Mark**: Decisive, yes or no quickly

## 🔧 API Endpoints

### `/voice` (POST/GET)
- Initial webhook for incoming calls
- Handles greeting playback and mode selection
- Manages turn counting and session initialization

### `/process_speech` (POST)
- Processes final speech recognition results
- Generates AI responses with GPT
- Handles text-to-speech conversion
- Manages conversation flow and tool calls

### `/partial_speech` (POST)
- Receives real-time partial transcription results
- Logs speech progress for debugging
- Early intent detection (no action taken)

### `/call_status` (POST)
- Receives Twilio status callbacks
- Handles call completion and cleanup
- Updates usage tracking

### `/static/<filename>` (GET)
- Serves generated audio files
- Provides access to greetings and responses

## 🏗️ Architecture Overview

### Call Flow
1. **Incoming Call** → Twilio webhook hits `/voice`
2. **Greeting** → Play appropriate greeting based on user history
3. **Mode Selection** → User speaks intent, processed by `/process_speech`
4. **Conversation Loop**:
   - User speaks → Twilio gathers speech
   - Speech processed → GPT generates response
   - TTS creates audio → Played back to user
   - Loop continues until hangup or time limit

### State Management
- **In-Memory Storage**:
  - `conversation_history`: Full conversation context
  - `mode_lock`: Locked practice mode per call
  - `voice_lock`: Locked voice ID for consistency
  - `personality_memory`: Assigned personality per call
  - `active_streams`: Active call tracking
  - `call_timers`: Time limit enforcement

### Minute Tracking System
- CSV-based persistent storage
- Tracks per-phone number:
  - Total minutes used
  - Minutes remaining
  - Last call date
  - Total number of calls
- Automatic SMS notifications when minutes expire

## 🔍 Advanced Features

### Streaming Configuration

The application supports multiple streaming modes:

```python
# Full streaming (lowest latency)
USE_STREAMING=true
SENTENCE_STREAMING=true

# Partial streaming (balanced)
USE_STREAMING=true
SENTENCE_STREAMING=false

# No streaming (highest reliability)
USE_STREAMING=false
```

### Tool Calling

The AI can check remaining time when asked:
```python
# User: "How much time do I have left?"
# AI uses check_remaining_time tool
# Response: "You have about 2 minutes remaining in your free call."
```

### Bad News Detection

Special handling for difficult conversations:
- Detects phrases like "bad news", "delay", "problem"
- AI responds with appropriate emotion
- Escalates if user becomes defensive

## 🐛 Troubleshooting

### Common Issues

1. **"No audio heard"**
   - Check ElevenLabs API key and quota
   - Verify voice IDs are correct
   - Check static file generation

2. **"Call drops immediately"**
   - Verify Twilio webhook URLs
   - Check for Python errors in logs
   - Ensure ngrok is running (for local dev)

3. **"AI doesn't respond"**
   - Verify OpenAI API key
   - Check model names in .env
   - Monitor API rate limits

4. **"Minutes not tracking"**
   - Ensure CSV file has write permissions
   - Check file lock issues
   - Verify phone number format

### Debug Modes

Enable debug output in `.env`:
```env
DEBUG_PARTIAL=true      # Log all partial speech
DEBUG_CONVERSATION=true # Log conversation state
```

## 🚦 Production Deployment

### Heroku Deployment

1. **Create Heroku app**:
```bash
heroku create your-app-name
```

2. **Set environment variables**:
```bash
heroku config:set OPENAI_API_KEY=your_key
heroku config:set ELEVENLABS_API_KEY=your_key
# ... set all required env vars
```

3. **Deploy**:
```bash
git push heroku main
```

4. **Update Twilio webhooks** with Heroku URL

### Performance Optimization

- **Use Redis** for state management (instead of in-memory)
- **Implement caching** for frequently used audio
- **Use CDN** for static file delivery
- **Monitor API usage** to optimize costs

## 📊 Usage Analytics

The CSV tracking provides insights into:
- User engagement patterns
- Average call duration
- Repeat usage rates
- Popular practice modes

Access via `user_usage.csv` with columns:
- `phone_number`
- `minutes_used`
- `minutes_left`
- `last_call_date`
- `total_calls`

## 🔐 Security Considerations

1. **Always use HTTPS** in production
2. **Rotate API keys** regularly
3. **Implement rate limiting** for public deployments
4. **Sanitize phone numbers** in logs
5. **Use environment variables** for all secrets

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## 📄 License

This project is proprietary. All rights reserved.

## 🆘 Support

For issues or questions:
- Check existing GitHub issues
- Review Twilio/OpenAI/ElevenLabs documentation
- Contact support at support@convoreps.com

---

**Live Test Number**: +1 (336) 823-6243 (via Amazon Connect bridge)

Built with ❤️ for better communication skills
