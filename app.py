import os
import io
import random
import time
import threading
import requests
from flask import Flask, request, Response, url_for
from flask_cors import CORS
from twilio.twiml.voice_response import VoiceResponse
from elevenlabs import generate, save, set_api_key
import openai
from dotenv import load_dotenv
from pathlib import Path
from flask import Flask, request, Response, url_for, send_from_directory
from flask import redirect, url_for, Response
from pydub import AudioSegment
from flask import send_from_directory
from urllib.parse import urlparse
from flask import request, redirect, url_for
import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

cold_call_personality_pool = {
    "Jerry": {
        "voice_id": "1t1EeRixsJrKbiF1zwM6",
        "system_prompt": "You're Jerry. You're a skeptical small business owner who’s been burned by vendors in the past. You're not rude, but you're direct and hard to win over. Respond naturally based on how the call starts — maybe this is a cold outreach, maybe a follow-up, or even someone calling you with bad news. Stay in character. If the salesperson fumbles, challenge them. If they’re smooth, open up a bit. Speak casually, not like a script."
    },
    "Miranda": {
        "voice_id": "Ax1GP2W4XTyAyNHuch7v",
        "system_prompt": "You're Miranda. You're a busy office manager who doesn’t have time for fluff. If the caller is clear and respectful, you'll hear them out. Respond naturally depending on how they open — this could be a cold call, a follow-up, or someone delivering news. Keep your tone grounded and real. Interrupt if needed. No robotic replies — talk like a real person at work."
    },
    "Junior": {
        "voice_id": "Nbttze9nhGhK1czblc6j",
        "system_prompt": "You're Junior. You run a local shop and have heard it all. You're friendly but not easily impressed. Start skeptical, but if the caller handles things well, loosen up. Whether this is a pitch, a follow-up, or some kind of check-in, reply naturally. Use casual language. If something sounds off, call it out. You don’t owe anyone your time — but you’re not a jerk either."
    },
    "Brett": {
        "voice_id": "7eFTSJ6WtWd9VCU4ZlI1",
        "system_prompt": "You're Brett. You're a contractor who answers his phone mid-job. You’re busy and a little annoyed this person called. If they’re direct and helpful, give them a minute. If they ramble, shut it down. This could be a pitch, a check-in, or someone following up on a proposal. React based on how they start the convo. Talk rough, fast, and casual. No fluff, no formalities."
    },
    "Kayla": {
        "voice_id": "aTxZrSrp47xsP6Ot4Kgd",
        "system_prompt": "You're Kayla. You own a business and don’t waste time. You’ve had too many bad sales calls and follow-ups from people who don’t know how to close. Respond based on how they open — if it’s a pitch, hit them with price objections. If it’s a follow-up, challenge their urgency. Keep your tone sharp but fair. You don’t sugarcoat things, and you don’t fake interest."
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


# Flask setup
app = Flask(__name__, static_url_path="/static", static_folder="static")
CORS(app)

# Env + API keys
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
set_api_key(os.getenv("ELEVENLABS_API_KEY"))
openai.api_key = os.getenv("OPENAI_API_KEY")
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



@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


def delayed_cleanup(call_sid):
    time.sleep(15)  # Let Twilio play it first
    try:
        os.remove(f"static/response_{call_sid}.mp3")
        os.remove(f"static/response_ready_{call_sid}.txt")
        print(f"🧹 Cleaned up response files for {call_sid}")
    except Exception as e:
        print(f"⚠️ Cleanup error for {call_sid}: {e}")
 


@app.route("/voice", methods=["POST", "GET"])
def voice():
    call_sid = request.values.get("CallSid")
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
        print("📞 First turn — playing greeting")
        response.play(f"{request.url_root}static/greeting.mp3?v={time.time()}")
    elif is_file_ready(mp3_path, flag_path):
        print(f"🔊 Playing: {mp3_path}")
        ngrok_domain = os.getenv("NGROK_DOMAIN")
        public_mp3_url = f"{request.url_root}static/response_{call_sid}.mp3"
        response.play(public_mp3_url)
    else:
        print("⏳ Response not ready — redirecting to /voice")
        response.pause(length=2)
        ngrok_domain = os.getenv("NGROK_DOMAIN")
        response.redirect(f"{request.url_root}voice")
        return str(response)

    response.record(
        action="/transcribe",
        method="POST",
        max_length=30,
        timeout=6,
        play_beep=True,
        trim="trim-silence"
    )
    return str(response)


@app.route("/transcribe", methods=["POST"])
def transcribe():
    global active_call_sid
    print("✅ /transcribe endpoint hit")

    call_sid = request.form.get("CallSid")
    recording_url = request.form.get("RecordingUrl") + ".wav"
    print(f"🎧 Downloading: {recording_url}")
    print(f"🛍️ ACTIVE CALL SID at start of /transcribe: {active_call_sid}")

    if not recording_url or not call_sid:
        return "Missing Recording URL or CallSid", 400

    if call_sid != active_call_sid:
        print(f"💨 Resetting memory for new call_sid: {call_sid}")
        conversation_history.clear()
        mode_lock.clear()
        personality_memory.clear()
        turn_count.clear()
        active_call_sid = call_sid

    for attempt in range(3):
        response = requests.get(recording_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        if response.status_code == 200:
            break
        print(f"⏳ Attempt {attempt + 1}: Audio not ready yet...")
        time.sleep(1.5)
    else:
        return "Recording not available", 500

    with open("input.wav", "wb") as f:
        f.write(response.content)

    if os.path.getsize("input.wav") < 1500:
        print("⚠️ Skipping transcription — audio file too short.")
        return redirect(url_for("voice", _external=True))

    try:
        with open("input.wav", "rb") as f:
            result = openai.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="en"
            )
        transcript = result.text.strip()
        print("📝 Transcription:", transcript)
    except Exception as e:
        print("💥 Whisper error:", e)
        return redirect(url_for("voice", _external=True))
    
    def detect_bad_news(text):
        lowered = text.lower()
        return any(phrase in lowered for phrase in [
            "bad news", 
            "unfortunately", 
            "problem", 
            "delay", 
            "issue", 
            "we can't", 
            "we won't", 
            "not going to happen", 
            "reschedule", 
            "price increase"
        ])

    bad_news_flag = detect_bad_news(transcript)


    def detect_intent(text):
        lowered = text.lower()
        if any(phrase in lowered for phrase in ["cold call", "customer call", "sales call", "business call"]):
            return "cold_call"
        elif any(phrase in lowered for phrase in ["interview", "interview prep"]):
            return "interview"
        elif any(phrase in lowered for phrase in ["small talk", "chat", "talk casually"]):
            return "small_talk"
        return "unknown"



    if "let's start over" in transcript.lower():
        print("🔁 Reset triggered by user — rolling new persona")
        conversation_history.pop(call_sid, None)
        personality_memory.pop(call_sid, None)
        turn_count[call_sid] = 0
        transcript = "cold call practice"

    mode = mode_lock.get(call_sid)
    if not mode:
        mode = detect_intent(transcript)
        mode_lock[call_sid] = mode
    print("🧐 Detected intent:", mode)

    if mode == "cold_call" or mode == "customer_convo":
        if call_sid not in personality_memory:
            persona_name = random.choice(list(cold_call_personality_pool.keys()))
            personality_memory[call_sid] = persona_name
        else:
            persona_name = personality_memory[call_sid]

        persona = cold_call_personality_pool[persona_name]
        voice_id = persona["voice_id"]
        system_prompt = persona["system_prompt"]
        intro_line = persona.get("intro_line", "Alright, I’ll be your customer. Start the conversation however you want — this could be a cold call, a follow-up, a check-in, or even a tough conversation. I’ll respond based on my personality. If you ever want to start over, just say 'let’s start over.'")


    elif mode == "small_talk":
        voice_id = "2BJW5coyhAzSr8STdHbE"
        system_prompt = "You're a casual, sarcastic friend. Keep it light, keep it fun."
        intro_line = "Yo yo yo, how’s it goin’?"

    elif mode == "interview":
        interview_voice_pool = [
            {"voice_id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel"},  # natural, calm
            {"voice_id": "EXAVITQu4vr4xnSDxMaL", "name": "Clyde"},   # decent, a bit flat
            {"voice_id": "6YQMyaUWlj0VX652cY1C", "name": "Stephen"}  # real, human, professional
        ]

        voice_choice = random.choice(interview_voice_pool)
        voice_id = voice_choice["voice_id"]
        system_prompt = (
            f"You are {voice_choice['name']}, a friendly, conversational job interviewer helping candidates practice for real interviews. "
            "Speak casually — like you're talking to someone over coffee, not in a formal evaluation. Ask one interview-style question at a time, and after each response, give supportive, helpful feedback. "
            "If their answer is weak, say 'Let’s try that again' and re-ask the question. If it's strong, give a quick reason why it's good. "
            "Briefly refer to the STAR method (Situation, Task, Action, Result) when giving feedback, but don’t lecture. Keep your tone upbeat, natural, and keep the conversation flowing. "
            "Don’t ask if they’re ready for the next question — just move on with something like, 'Alright, next one,' or 'Cool, here’s another one.'"
        )

        intro_line = "Great, let's jump in! Can you walk me through your most recent role and responsibilities?"

    else:
        voice_id = "1t1EeRixsJrKbiF1zwM6"
        system_prompt = "You're a helpful assistant."
        intro_line = "How can I help you today?"

    turn = turn_count.get(call_sid, 0)
    turn_count[call_sid] = turn + 1
    conversation_history.setdefault(call_sid, [])

    if turn == 0:
        reply = intro_line
        conversation_history[call_sid].append({"role": "assistant", "content": reply})
    else:
        conversation_history[call_sid].append({"role": "user", "content": transcript})
        messages = [{"role": "system", "content": system_prompt}]
        lowered = transcript.lower()
        is_bad_news = any(x in lowered for x in [
            "bad news", "unfortunately", "delay", "delayed", "won’t make it", "can't deliver",
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

        messages += conversation_history[call_sid]

        gpt_reply = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages
        )
        reply = gpt_reply.choices[0].message.content.strip()
        reply = reply.replace("*", "")
        reply = reply.replace("_", "")
        reply = reply.replace("`", "")
        reply = reply.replace("#", "")
        reply = reply.replace("-", " ")
        conversation_history[call_sid].append({"role": "assistant", "content": reply})

    print(f"🔣 Generating voice with ID: {voice_id}")
    try:
        raw_audio = generate(text=reply, voice=voice_id, model="eleven_monolingual_v1")
    except Exception as e:
        print(f"🛑 ElevenLabs generation error:", e)
        if "429" in str(e):  # Too Many Requests
            print("🔁 Retrying after brief pause due to rate limit...")
            time.sleep(2)  # Wait before retry
            try:
                raw_audio = generate(text=reply, voice=voice_id, model="eleven_monolingual_v1")
                print("✅ Retry succeeded")
            except Exception as e:
                print(f"❌ Retry failed:", e)
                fallback_path = "static/fallback.mp3"
                if os.path.exists(fallback_path):
                    os.system(f"cp {fallback_path} static/response_{call_sid}.mp3")
                    with open(f"static/response_ready_{call_sid}.txt", "w") as f:
                        f.write("ready")
                    print("⚠️ Fallback MP3 used.")
                    return redirect(url_for("voice", _external=True))
                else:
                    return "ElevenLabs error and no fallback available", 500


    output_path = f"static/response_{call_sid}.mp3"
    with open(output_path, "wb") as f:
        f.write(raw_audio)


    print(f"✅ Clean MP3 saved via ElevenLabs to {output_path}")

    with open(f"static/response_ready_{call_sid}.txt", "w") as f:
        f.write("ready")
    print(f"🚩 Ready flag created for {call_sid}")

    return redirect(url_for("voice", _external=True))





if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050)

