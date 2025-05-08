# generate_beep.py
from pydub.generators import Sine
from pydub import AudioSegment
import os

os.makedirs("static", exist_ok=True)

# Generate a 1000Hz sine wave beep, 300ms duration
beep = Sine(1000).to_audio_segment(duration=300).apply_gain(-6)
beep.export("static/beep.mp3", format="mp3")

print("✅ beep.mp3 generated in static/")

