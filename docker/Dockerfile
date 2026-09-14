# iPedro V2 container
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ffmpeg powers the /ether radio-transmission audio effect (bandpass +
# static + tremolo filtergraph, and OGG/Opus encode for voice notes).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Bake the tiktoken encoding into the image. Without this it's fetched on
# first use; if that first fetch ever fails at runtime (no network, a
# registry hiccup), count_tokens() silently falls back to a char/4
# estimate — which then quietly weakens the prompt-cache minimum-prefix
# gate in openai_client.py. Baking it removes the runtime fetch entirely.
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

COPY ipedro ./ipedro
COPY scripts ./scripts

# Run as a non-root user for safety.
RUN useradd --create-home --shell /usr/sbin/nologin ipedro \
 && chown -R ipedro:ipedro /app
USER ipedro

ENTRYPOINT ["python", "-m", "ipedro"]
