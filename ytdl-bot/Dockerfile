FROM python:3.12-slim

# ffmpeg converts/merges audio and video for yt-dlp
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime for full YouTube support; Deno is the
# one it uses by default. The deno:bin image contains just the binary.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY yt-dlp-bot.py .

CMD ["python", "yt-dlp-bot.py"]
