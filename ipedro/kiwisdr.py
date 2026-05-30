"""KiwiSDR WebSocket client — fetches a chunk of live SSB audio.

Public KiwiSDR receivers expose a WebSocket interface for live audio.
This module opens an anonymous connection, tunes to a frequency/mode,
reads ~N seconds of 4-bit IMA-ADPCM samples (12 kHz mono), decodes
them, resamples to the radio_fx working rate (8 kHz), and returns
float32 PCM. Used by ``radio_fx._fetch_live_from_url`` when the
configured URL starts with the ``kiwi://`` scheme.

URL form (for ``RADIO_FX_LIVE_URLS``):

    kiwi://host:port?freq=14040&mode=lsb[&password=...]

Protocol notes (extracted from the public ``kiwiclient`` source):

  * Connect WebSocket to ``ws://HOST:PORT/<12-hex-random>/SND`` — the
    hex token is just a per-session id; public Kiwis don't validate it.
  * Server greets with text ``MSG ... audio_init=0 audio_rate=12000 ...``.
  * Authenticate: ``SET auth t=kiwi p=#`` (the ``#`` is the conventional
    empty/anonymous password; private Kiwis would supply a real one).
  * Send identification: ``SET ident_user=ipedro-radio``.
  * Tune: ``SET mod=<lsb|usb|am|cw> low_cut=300 high_cut=2700 freq=<kHz>``.
  * Server begins streaming binary frames. Each frame:
      bytes [0:3]  = ASCII "SND"
      byte  [3]    = flags
      bytes [4:8]  = LE uint32 sequence number
      bytes [8:10] = LE int16 RSSI (S-meter, ignored)
      bytes [10:]  = 4-bit IMA-ADPCM payload, 2 samples per byte at 12 kHz
  * Keep alive with ``SET keepalive`` every few seconds.

Different Kiwi firmware revisions vary the binary header by a few bytes
(some omit RSSI, some include extra status). We parse defensively: any
frame whose payload starts on a wrong boundary will produce noise; we
discard it and continue. As long as ≥1s of valid audio decodes, the
call succeeds.

If anything goes wrong — bad URL, host unreachable, handshake rejected,
no audio — ``fetch_pcm`` returns an empty array; the caller falls
through to bundled/synthetic. No exceptions escape.
"""

from __future__ import annotations

import asyncio
import logging
import random
import struct
import time
import urllib.parse
from typing import Optional

import numpy as np

try:
    import websockets
    import websockets.exceptions
except ImportError:  # pragma: no cover — declared in requirements.txt
    websockets = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

KIWI_SAMPLE_RATE = 12_000   # KiwiSDR audio output rate
DEFAULT_TIMEOUT = 18.0      # seconds for the whole fetch (connect + audio)
HANDSHAKE_TIMEOUT = 6.0     # seconds for connect + auth + initial config
KEEPALIVE_INTERVAL = 5.0    # send "SET keepalive" every N seconds


# ---------------------------------------------------------------- URL parsing
def parse_kiwi_url(url: str) -> Optional[dict]:
    """Parse a ``kiwi://host:port?freq=...&mode=...&password=...`` URL.

    Returns a dict with ``host``, ``port``, ``freq_khz``, ``mode``,
    ``password`` on success; ``None`` if the URL is malformed or the
    scheme isn't ``kiwi``. ``freq`` and ``mode`` are required; password
    defaults to the conventional anonymous ``#``.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != "kiwi" or not parsed.hostname:
        return None
    qs = urllib.parse.parse_qs(parsed.query)
    if "freq" not in qs or "mode" not in qs:
        return None
    try:
        freq_khz = float(qs["freq"][0])
    except (ValueError, IndexError):
        return None
    mode = qs["mode"][0].lower().strip()
    if mode not in ("lsb", "usb", "am", "cw", "sam"):
        return None
    return {
        "host": parsed.hostname,
        "port": parsed.port or 8073,
        "freq_khz": freq_khz,
        "mode": mode,
        "password": qs.get("password", ["#"])[0],
    }


# ---------------------------------------------------------------- IMA-ADPCM
# Standard IMA-ADPCM lookup tables — public-domain. KiwiSDR uses the
# canonical algorithm: 4 bits per sample, step-index adaptive.
_IMA_INDEX_TABLE = np.array(
    [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8],
    dtype=np.int8,
)
_IMA_STEP_TABLE = np.array(
    [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37,
     41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173,
     190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
     724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
     2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
     6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289,
     16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767],
    dtype=np.int32,
)


class ADPCMState:
    """Per-stream IMA-ADPCM decoder state. KiwiSDR resets the predictor
    on each frame in some firmware revisions but most stream
    continuously; we keep state across frames and clip on overflow."""

    __slots__ = ("predictor", "index")

    def __init__(self, predictor: int = 0, index: int = 0):
        self.predictor = predictor
        self.index = index


def decode_ima_adpcm(payload: bytes, state: ADPCMState) -> np.ndarray:
    """Decode a 4-bit IMA-ADPCM payload into int16 PCM samples.

    Two samples per input byte (low nibble first, then high). The state
    is mutated in place so successive payloads decode contiguously.
    Returns a 1-D int16 numpy array of length ``2 * len(payload)``.
    """
    if not payload:
        return np.zeros(0, dtype=np.int16)
    out = np.empty(2 * len(payload), dtype=np.int16)
    predictor = int(state.predictor)
    index = int(state.index)
    for i, byte in enumerate(payload):
        for shift in (0, 4):
            nibble = (byte >> shift) & 0x0F
            step = int(_IMA_STEP_TABLE[index])
            delta = step >> 3
            if nibble & 1:
                delta += step >> 2
            if nibble & 2:
                delta += step >> 1
            if nibble & 4:
                delta += step
            if nibble & 8:
                predictor -= delta
            else:
                predictor += delta
            if predictor > 32767:
                predictor = 32767
            elif predictor < -32768:
                predictor = -32768
            index += int(_IMA_INDEX_TABLE[nibble])
            if index < 0:
                index = 0
            elif index > 88:
                index = 88
            out[2 * i + (shift >> 2)] = predictor
    state.predictor = predictor
    state.index = index
    return out


# ---------------------------------------------------------------- frame parse
def _parse_snd_frame(msg: bytes) -> Optional[bytes]:
    """Extract the ADPCM payload from a Kiwi binary audio frame.

    Returns the ADPCM bytes, or ``None`` if the frame isn't a valid
    audio frame (other tags include ``WF`` for waterfall, ``EXT``,
    keepalive replies, etc.).

    The wire format is mostly stable across Kiwi firmware:
        msg[0:3]  = b"SND"
        msg[3]    = flags
        msg[4:8]  = LE uint32 sequence number
        msg[8:10] = LE int16 RSSI (S-meter)
        msg[10:]  = ADPCM payload
    Older firmware sometimes omits RSSI; we accept a payload starting
    at offset 8 or 10 (whichever leaves a sensible payload size).
    """
    if len(msg) < 12 or msg[:3] != b"SND":
        return None
    # Try the common (≥ v1.461) layout first.
    payload_10 = msg[10:]
    if 256 <= len(payload_10) <= 4096:
        return payload_10
    payload_8 = msg[8:]
    if 256 <= len(payload_8) <= 4096:
        return payload_8
    return None


# ---------------------------------------------------------------- session
def _gen_session_token() -> str:
    return "".join(random.choices("0123456789abcdef", k=12))


async def _fetch_pcm_async(
    *, host: str, port: int, freq_khz: float, mode: str, duration_s: float,
    password: str = "#", timeout: float = DEFAULT_TIMEOUT,
) -> np.ndarray:
    """Connect to a KiwiSDR and return ``duration_s`` of int16 mono PCM
    at 12 kHz. Empty array on any failure (no exceptions escape)."""
    if websockets is None:
        log.warning("kiwisdr: websockets library not installed")
        return np.zeros(0, dtype=np.int16)
    target_samples = int(duration_s * KIWI_SAMPLE_RATE)
    if target_samples <= 0:
        return np.zeros(0, dtype=np.int16)
    url = f"ws://{host}:{port}/{_gen_session_token()}/SND"
    state = ADPCMState()
    collected: list[np.ndarray] = []
    collected_n = 0
    start_t = time.monotonic()
    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(
                url, open_timeout=HANDSHAKE_TIMEOUT,
                close_timeout=2.0, max_size=2**20,
                user_agent_header="ipedro-radio/1.0",
            ) as ws:
                # Handshake: auth → identify → tune.
                await ws.send(f"SET auth t=kiwi p={password}")
                await ws.send("SET ident_user=ipedro-radio")
                await ws.send(
                    f"SET mod={mode} low_cut=300 high_cut=2700 "
                    f"freq={freq_khz:.2f}"
                )
                await ws.send("SET AR OK in=12000 out=12000")
                await ws.send("SET squelch=0 squelch_param=0")
                await ws.send("SET lms_autonotch=0")
                await ws.send(
                    "SET agc=1 hang=0 thresh=-100 slope=6 "
                    "decay=1000 manGain=50"
                )
                last_keepalive = time.monotonic()
                while collected_n < target_samples:
                    if time.monotonic() - last_keepalive > KEEPALIVE_INTERVAL:
                        await ws.send("SET keepalive")
                        last_keepalive = time.monotonic()
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=4.0)
                    except asyncio.TimeoutError:
                        log.info("kiwisdr: idle 4s waiting for audio")
                        break
                    if isinstance(msg, str):
                        # Text MSG/STATUS frames — ignore (logs/handshake).
                        continue
                    payload = _parse_snd_frame(msg)
                    if payload is None:
                        continue
                    pcm = decode_ima_adpcm(payload, state)
                    if pcm.size == 0:
                        continue
                    collected.append(pcm)
                    collected_n += pcm.size
    except asyncio.TimeoutError:
        log.info("kiwisdr: %s:%s timed out after %.1fs (got %.1fs audio)",
                 host, port, time.monotonic() - start_t,
                 collected_n / KIWI_SAMPLE_RATE)
    except (OSError, websockets.exceptions.WebSocketException) as exc:
        log.info("kiwisdr: %s:%s failed: %s", host, port, exc)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("kiwisdr: %s:%s unexpected error: %s", host, port, exc)
    if not collected:
        return np.zeros(0, dtype=np.int16)
    pcm = np.concatenate(collected)
    if pcm.size > target_samples:
        pcm = pcm[:target_samples]
    return pcm


def fetch_pcm(
    *, host: str, port: int, freq_khz: float, mode: str,
    duration_s: float = 30.0, password: str = "#",
    timeout: float = DEFAULT_TIMEOUT,
) -> np.ndarray:
    """Sync wrapper. Runs the async fetch in a fresh event loop.

    Safe to call from inside ``asyncio.to_thread`` (a worker thread has
    no asyncio context, so ``asyncio.run`` creates a fresh loop here)."""
    try:
        return asyncio.run(_fetch_pcm_async(
            host=host, port=port, freq_khz=freq_khz, mode=mode,
            duration_s=duration_s, password=password, timeout=timeout,
        ))
    except RuntimeError as exc:  # pragma: no cover - asyncio.run misuse
        log.warning("kiwisdr: asyncio.run refused: %s", exc)
        return np.zeros(0, dtype=np.int16)


def fetch_pcm_from_url(url: str, *, duration_s: float = 30.0,
                       timeout: float = DEFAULT_TIMEOUT) -> np.ndarray:
    """Convenience: parse a ``kiwi://...`` URL and fetch. Returns an
    empty array on bad URL or fetch failure."""
    params = parse_kiwi_url(url)
    if params is None:
        log.info("kiwisdr: malformed URL %s", url)
        return np.zeros(0, dtype=np.int16)
    return fetch_pcm(
        host=params["host"], port=params["port"],
        freq_khz=params["freq_khz"], mode=params["mode"],
        password=params["password"],
        duration_s=duration_s, timeout=timeout,
    )
