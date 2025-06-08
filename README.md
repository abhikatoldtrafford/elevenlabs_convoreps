# ConvoReps — Real-Time AI Phone Training

ConvoReps is a real-time voice simulation system for training sales reps, job seekers, and English learners via phone. It uses Twilio, OpenAI (Whisper + GPT), and ElevenLabs to carry out full conversations over the phone with natural pacing and personality switching.

---

## 📞 Live Test Number

**Amazon Connect → Twilio Bridge**  
Call this number to test system behavior and latency:

**+1 (336) 823-6243**

---

## 🧠 Tech Stack

- Python (Flask)
- Twilio Programmable Voice
- OpenAI Whisper (transcription)
- OpenAI GPT-4o (conversation logic)
- ElevenLabs TTS (voice synthesis)
- Amazon Connect (international call testing)

---

## 🔑 API Keys (Provided Privately)

You’ll be sent these via a private GitHub Gist:

- `OPENAI_API_KEY`
- `ELEVENLABS_API_KEY`

Twilio Account SID and token can be shared securely if needed — or you can test using the live bridge setup.

---

## 🎯 Current Behavior

- `/voice` – Handles initial Twilio voice webhook, plays greeting
- `/transcribe` – File-based voice → transcript → GPT → ElevenLabs → MP3 playback
- Cold call, interview, and small talk modes supported
- Personalized AI voices with memory per session
- Voice responses currently served as MP3 files

---

## 🚧 Milestone: Real-Time Audio Pipeline

**Goal:** Replace `/transcribe` with a real-time, low-latency pipeline under 1.5s round-trip.

Must preserve:
- Mode-specific logic and voice personalities
- Session memory and switching
- Fallback to file-based audio if streaming fails

---

