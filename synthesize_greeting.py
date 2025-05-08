import os
from dotenv import load_dotenv
from elevenlabs import generate, set_api_key, Voice, VoiceSettings

load_dotenv()
set_api_key(os.getenv("ELEVENLABS_API_KEY"))

def synthesize_greeting():
    print("🎙️ Synthesizing greeting...")

    greeting_text = (
        "Welcome to Convo Reps. After the beep, say something like 'customer call', 'interview prep', or 'small talk' and we'll get started."
    )

    audio = generate(
        text=greeting_text,
        voice=Voice(
            voice_id="21m00Tcm4TlvDq8ikWAM",
            settings=VoiceSettings(stability=0.4, similarity_boost=0.75)
        ),
        model="eleven_monolingual_v1",
        stream=False,
        output_format="mp3_44100_128"
    )

    output_path = "static/greeting.mp3"
    if os.path.exists(output_path):
        os.remove(output_path)

    with open(output_path, "wb") as f:
        f.write(audio)

    print(f"✅ Greeting synthesized and saved to {output_path}")

synthesize_greeting()
