FROM python:3.12-slim

# ffmpeg pulls in libopus (libopus.so.0), which discord.py needs for voice; yt-dlp
# (installed below from the lock) shells out to it for /youtube extraction.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
# Dependencies come from the lockfile — pyproject.toml/uv.lock are the single source
# of truth; never hand-list packages here (that drifted both directions once).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH"

COPY bot.py db.py engine.py library.py seed_plex.py ./

CMD ["python", "bot.py"]
