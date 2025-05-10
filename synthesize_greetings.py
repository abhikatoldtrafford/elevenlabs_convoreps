import os
from dotenv import load_dotenv
from elevenlabs import generate, set_api_key, Voice, VoiceSettings
from pydub import AudioSegment

load_dotenv()
set_api_key(os.getenv("ELEVENLABS_API_KEY"))

def synthesize_and_save(text, output_filename):
    print(f"🎙️ Synthesizing: {output_filename}")
    audio = generate(
        text=text,
        voice=Voice(
            voice_id="21m00Tcm4TlvDq8ikWAM",
            settings=VoiceSettings(stability=0.4, similarity_boost=0.75)
        ),
        model="eleven_monolingual_v1",
        stream=False,
        output_format="mp3_44100_128"
    )

    output_path = f"static/{output_filename}"
    if os.path.exists(output_path):
        os.remove(output_path)

    with open(output_path, "wb") as f:
        f.write(audio)

    # Trim leading noise
    print(f"🔊 Trimming noise for: {output_filename}")
    sound = AudioSegment.from_mp3(output_path)
    trimmed = sound[50:]  # remove first 50 milliseconds
    trimmed.export(output_path, format="mp3")
    print(f"✅ Saved {output_filename}")

def synthesize_greetings():
    first_time_text = (
        "Welcome to ConvoReps! Your no-pressure conversation practice partner. "
        "After the beep, say something like 'customer call', 'interview prep', or 'small talk' and we'll get started. "
        "Your first call is free, so relax and give it a shot."
    )

    returning_text = (
        "Welcome back to ConvoReps. After the beep, just say 'customer call', 'interview prep', or 'small talk' to get started."
    )

    synthesize_and_save(first_time_text, "first_time_greeting.mp3")
    synthesize_and_save(returning_text, "returning_user_greeting.mp3")

if __name__ == "__main__":
    synthesize_greetings()
