# ConvoReps — Real-Time AI Voice Training Platform (OpenAI Realtime Edition)

ConvoReps is a cutting-edge voice training platform that uses OpenAI's Realtime API to provide ultra-low latency, natural conversation practice for sales professionals, job seekers, and anyone looking to improve their communication skills. This version represents a complete architectural overhaul, delivering sub-second response times and incredibly natural AI voices.

## 🚀 What's New in This Version

### Revolutionary Architecture
- **OpenAI Realtime API**: Direct WebSocket connection for true real-time conversations
- **FastAPI + WebSockets**: Modern async architecture replacing Flask
- **Sub-second Latency**: Responses in ~500ms vs 2-3 seconds in the original
- **Natural Voice Synthesis**: 8 distinct OpenAI voices with human-like speech patterns
- **Smart Intent Detection**: AI assistant helps users choose their practice mode

### Enhanced Features
- **🎭 Expanded Personality Pool**: 6 unique cold call personalities with realistic backgrounds
- **🎯 Intelligent Mode Selection**: Natural conversation flow to determine practice type
- **🗣️ Human Speech Patterns**: Filler words, pauses, emotional variations, and natural interruptions
- **⚡ Real-time Streaming**: Continuous audio flow without file generation
- **📊 Improved Analytics**: Better minute tracking and usage monitoring
- **🔧 Production Ready**: Health checks, metrics, and proper error handling

## 📋 System Requirements

### Required Services
1. **Python 3.8+**
2. **Twilio Account** with:
   - Phone number with Voice capabilities
   - Account SID and Auth Token
   - Webhook configuration access
3. **OpenAI API Access** with:
   - API Key with Realtime API access
   - Access to `gpt-4o-realtime-preview` model
4. **Public URL** for webhooks (use ngrok for development)

### No Longer Required
- ❌ ElevenLabs API (replaced by OpenAI voices)
- ❌ Separate Whisper API calls
- ❌ Audio file storage/management

## 🛠️ Installation Guide

### 1. Clone the Repository
```bash
git clone -b dev https://github.com/yourusername/convoreps.git
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
# OpenAI Configuration (REQUIRED)
OPENAI_API_KEY=your_openai_api_key_here

# Twilio Configuration (REQUIRED)
TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_PHONE_NUMBER=+1234567890  # Your Twilio phone number

# Application Settings (optional - defaults shown)
PORT=5050
FREE_CALL_MINUTES=3.0
MIN_CALL_DURATION=0.5
USAGE_CSV_PATH=user_usage.csv
USAGE_CSV_BACKUP_PATH=user_usage_backup.csv
CONVOREPS_URL=https://convoreps.com
MAX_WEBSOCKET_CONNECTIONS=50

# Note: ElevenLabs is NOT required for this version
```

### 5. Generate Static Files (Optional)
```bash
# Generate beep sound
python generate_beep.py

# Note: synthesize_greetings.py requires ElevenLabs and is optional
# The system will use TTS fallback if greeting files don't exist
```

### 6. Configure Twilio Webhooks

In your Twilio Console:

1. Navigate to **Phone Numbers** → **Manage** → **Active Numbers**
2. Click on your phone number
3. Configure **Voice & Fax**:
   - **A CALL COMES IN**: 
     - Webhook: `https://your-domain.com/voice`
     - HTTP Method: `POST`
   - **PRIMARY HANDLER FAILS**: Leave empty

Note: The `/call_status` webhook is no longer required in this version.

### 7. Start the Application

For development:
```bash
python app.py
```

For production with Uvicorn directly:
```bash
uvicorn app:app --host 0.0.0.0 --port 5050 --log-level info
```

For production with Gunicorn (recommended):
```bash
gunicorn app:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:5050
```

### 8. Testing with ngrok (Development)
```bash
# In a new terminal
ngrok http 5050

# Use the HTTPS URL for your Twilio webhook
# Example: https://abc123.ngrok.io/voice
```

## 🎯 How It Works

### Architecture Overview

```
User Phone Call → Twilio → FastAPI Server → WebSocket → OpenAI Realtime API
                     ↑                           ↓
                     ←──────── Audio Stream ─────←
```

1. **Call Initiation**: User calls Twilio number
2. **Webhook Hit**: Twilio sends request to `/voice` endpoint
3. **WebSocket Setup**: Server establishes WebSocket to OpenAI Realtime API
4. **Media Stream**: Twilio streams audio through `/media-stream` WebSocket
5. **Real-time Processing**: Audio flows directly between user and AI
6. **Natural Conversation**: Sub-second responses with human-like speech

### Call Flow States

1. **Initial Greeting** (MP3 file + beep)
2. **Intent Detection** (AI asks what to practice)
3. **Mode Selection** (User responds with intent)
4. **Personality Assignment** (System selects appropriate AI persona)
5. **Practice Session** (Real-time conversation)
6. **Time Management** (Automatic tracking and limits)
7. **Graceful Ending** (Time limit message + SMS)

## 🎭 Available Personalities

### Cold Call Practice (6 Diverse Personas)

#### Sarah - Marketing Agency Owner
- **Voice**: Warm and conversational (ash)
- **Style**: Professional but approachable
- **Challenges**: Budget concerns, current solutions, implementation time
- **Best For**: B2B service/software sales practice

#### David - IT Director
- **Voice**: Thoughtful and measured (sage)
- **Style**: Technical, detail-oriented
- **Challenges**: Security, integrations, technical specifications
- **Best For**: Technical product sales, enterprise software

#### Maria - E-commerce Operations Manager
- **Voice**: Energetic and enthusiastic (shimmer)
- **Style**: Efficiency-focused, ROI-driven
- **Challenges**: Needs CFO approval, scaling concerns
- **Best For**: Operational tools, productivity solutions

#### James - Healthcare CFO
- **Voice**: Smooth and analytical (ballad)
- **Style**: Numbers-focused, skeptical
- **Challenges**: Prove ROI, justify costs, budget constraints
- **Best For**: Financial objection handling

#### Lisa - Customer Success Director
- **Voice**: Warm and empathetic (ash)
- **Style**: Team-focused, user experience priority
- **Challenges**: Change management, team adoption
- **Best For**: User-centric product pitches

#### Robert - Startup Founder
- **Voice**: Dynamic and fast-paced (verse)
- **Style**: Direct, time-conscious, no fluff
- **Challenges**: Limited time, needs quick value
- **Best For**: Elevator pitch practice

### Interview Practice

#### Rachel Chen - Senior HR Manager
- **Voice**: Clear and professional (coral)
- **Background**: 12 years in talent acquisition
- **Style**: Warm but evaluative, uses STAR method conversationally
- **Features**: 
  - Provides real-time feedback
  - Helps candidates recover from mistakes
  - Adapts to different candidate types
  - Comprehensive question bank

### Small Talk Practice

#### Alex - Friendly Colleague
- **Voice**: Energetic and friendly (shimmer)
- **Scenarios**: Elevator chats, coffee breaks, networking events
- **Topics**: Weekend plans, local events, hobbies, work projects
- **Goal**: Build confidence in casual professional conversations

## 🎙️ OpenAI Voice Options

The system uses OpenAI's Realtime API voices with these characteristics:

- **alloy**: Neutral and balanced
- **ash**: Warm and conversational ✨
- **ballad**: Smooth and expressive ✨
- **coral**: Professional and clear ✨
- **echo**: Confident and engaging
- **sage**: Wise and thoughtful ✨
- **shimmer**: Energetic and friendly
- **verse**: Versatile and dynamic ✨

✨ = New voices in this version

## 📊 Advanced Features

### Natural Speech Enhancement

Every AI persona includes:
- **Voice Dynamics**: Varying pace, pitch, and volume
- **Authentic Fillers**: "Umm", "Well", "Actually" used naturally
- **Emotional Coloring**: Enthusiasm, concern, curiosity reflected in voice
- **Thinking Patterns**: Pauses, restarts, and self-corrections
- **Engagement Signals**: "Mhm", "Right", "I see" for active listening
- **Human Sounds**: Breathing, sighs, and natural reactions

### Smart Intent Detection

The AI assistant:
1. Greets users warmly
2. Asks what they'd like to practice
3. Listens for keywords and context
4. Confirms understanding
5. Smoothly transitions to the appropriate persona

### Real-time Features

- **Interruption Handling**: Users can interrupt AI mid-sentence
- **Speech Detection**: Advanced VAD (Voice Activity Detection)
- **Continuous Streaming**: No audio file generation or storage
- **Tool Calling**: AI can check remaining time when asked
- **Session Persistence**: Maintains context throughout the call

## 🔧 API Endpoints

### HTTP Endpoints

#### `GET /`
- Health check page
- Returns simple HTML confirmation

#### `POST /voice`
- Twilio webhook endpoint
- Handles incoming calls
- Sets up WebSocket connection
- Returns TwiML response

#### `GET /health`
- Detailed health check
- Returns system status, active streams, metrics
- Useful for monitoring

#### `GET /usage-data`
- Returns CSV usage data
- Plain text CSV format
- For analytics and reporting

### WebSocket Endpoint

#### `WS /media-stream`
- Handles real-time audio streaming
- Connects Twilio to OpenAI Realtime API
- Manages bidirectional audio flow
- Processes transcripts and tool calls

## 📈 Monitoring & Analytics

### Built-in Metrics

The system tracks:
- Total calls
- Active calls
- Failed calls
- SMS sent/failed
- Tool calls made
- API errors

Access metrics via the `/health` endpoint.

### Usage Tracking

CSV-based tracking includes:
- Phone number (sanitized)
- Minutes used
- Minutes remaining
- Last call date
- Total calls made

### Conversation Logging

- Real-time transcripts saved to `conversation_transcript.csv`
- In-memory conversation history (last 50 exchanges)
- Automatic cleanup after calls

## 🚨 Troubleshooting

### Common Issues

1. **"WebSocket connection failed"**
   - Verify OpenAI API key has Realtime access
   - Check firewall/proxy settings
   - Ensure proper WSS support

2. **"No audio heard"**
   - Verify Twilio audio format (μ-law)
   - Check WebSocket connection stability
   - Monitor `/health` endpoint

3. **"High latency"**
   - Check server location vs Twilio region
   - Monitor WebSocket connection quality
   - Verify no audio buffering issues

4. **"Call drops immediately"**
   - Validate webhook URL is HTTPS
   - Check Twilio credentials
   - Verify phone number format

### Debug Mode

Enable detailed logging:
```python
# In app.py, change logging level
logging.basicConfig(level=logging.DEBUG)
```

Monitor WebSocket events:
```python
# Set LOG_EVENT_TYPES to include more events
LOG_EVENT_TYPES = ['all']  # Shows all OpenAI events
```

## 🚀 Production Deployment

### Heroku Deployment

1. **Update Procfile** (already configured):
```
web: uvicorn app:app --host 0.0.0.0 --port $PORT --log-level info
```

2. **Create app and set config**:
```bash
heroku create your-app-name
heroku config:set OPENAI_API_KEY=your_key
heroku config:set TWILIO_ACCOUNT_SID=your_sid
heroku config:set TWILIO_AUTH_TOKEN=your_token
heroku config:set TWILIO_PHONE_NUMBER=+1234567890
```

3. **Deploy**:
```bash
git push heroku dev:main
```

### Performance Optimization

1. **Use Redis for state** (future enhancement):
```python
# Replace in-memory dicts with Redis
import redis
r = redis.Redis(host='localhost', port=6379, db=0)
```

2. **Implement connection pooling**:
```python
# Reuse WebSocket connections where possible
connection_pool = {}
```

3. **Add caching layer**:
```python
# Cache user data, personality assignments
from functools import lru_cache
```

### Scaling Considerations

- **Horizontal Scaling**: Run multiple instances with load balancer
- **WebSocket Limits**: Monitor `MAX_WEBSOCKET_CONNECTIONS`
- **Rate Limiting**: Implement per-user rate limits
- **Cost Management**: Monitor OpenAI Realtime API usage

## 🔐 Security Best Practices

1. **Environment Variables**: Never commit `.env` file
2. **Input Validation**: All phone numbers and CallSids validated
3. **HTTPS Only**: Enforce SSL for all webhooks
4. **Rate Limiting**: Implement to prevent abuse
5. **Access Control**: Consider IP whitelisting for webhooks
6. **Audit Logging**: Track all calls and usage

## 🧪 Testing Guide

### Manual Testing
1. Call your Twilio number
2. Say "cold call" after the beep
3. Practice your pitch with the AI persona
4. Ask "how much time do I have left?"
5. Let the call reach time limit

### Automated Testing
```python
# Example test for intent detection
def test_intent_detection():
    assert detect_intent("I want to practice cold calling") == "cold_call"
    assert detect_intent("interview prep please") == "interview"
    assert detect_intent("just want to chat") == "small_talk"
```

### Load Testing
```bash
# Use locust for WebSocket load testing
locust -f load_test.py --host=https://your-domain.com
```

## 📚 Architecture Decisions

### Why OpenAI Realtime API?
- **Latency**: 10x faster than Whisper→GPT→ElevenLabs pipeline
- **Quality**: Natural voices with consistent quality
- **Simplicity**: Single API vs three separate services
- **Cost**: More efficient at scale

### Why FastAPI + WebSockets?
- **Performance**: Async handling for concurrent connections
- **Modern**: Better WebSocket support than Flask
- **Type Safety**: Built-in request/response validation
- **Documentation**: Auto-generated API docs

### Why CSV for Usage Tracking?
- **Simplicity**: No database required
- **Portability**: Easy to analyze and migrate
- **Reliability**: Atomic writes prevent corruption
- **Compatibility**: Works everywhere

## 🤝 Contributing

### Development Setup
1. Fork the repository
2. Create feature branch: `git checkout -b feature/amazing-feature`
3. Make changes with tests
4. Run linting: `black app.py && flake8`
5. Commit: `git commit -m 'Add amazing feature'`
6. Push: `git push origin feature/amazing-feature`
7. Create Pull Request

### Code Style
- Follow PEP 8
- Use type hints where possible
- Document all functions
- Keep functions under 50 lines
- Write tests for new features

## 📄 License

Proprietary - All rights reserved by ConvoReps.

## 🆘 Support

- **Documentation**: This README
- **Issues**: GitHub Issues
- **Email**: support@convoreps.com
- **Live Test**: +1 (336) 823-6243

---

**Built with ❤️ for the future of communication training**

*Version 3.3 - Smart Intent Detection with OpenAI Realtime API*
