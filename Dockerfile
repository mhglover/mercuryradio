FROM python:3.12-slim

# ffmpeg pulls in libopus (libopus.so.0), which discord.py needs for voice.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --no-cache-dir "discord.py[voice]>=2.4" "python-dotenv>=1.0" "mutagen>=1.47"
COPY bot.py db.py library.py seed_plex.py ./

CMD ["python", "bot.py"]
