import array
import asyncio
import base64
from collections import deque
import ctypes.util
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlparse
import urllib.request
import uuid

import discord
from discord.ext import commands
from discord.gateway import DiscordVoiceWebSocket, SpeakingState
from discord.utils import MISSING
from discord.voice_state import ConnectionFlowState, VoiceConnectionState
from dotenv import load_dotenv
import yt_dlp

# ==============================================================================
# CONFIGURACIÓN DE LOGGING Y VARIABLES DE ENTORNO
# ==============================================================================

RECENT_LOGS: Deque[str] = deque(maxlen=400)


class MemoryRingLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            RECENT_LOGS.append(self.format(record))
        except Exception:
            pass


log_fmt = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
ring_handler = MemoryRingLogHandler()
ring_handler.setFormatter(log_fmt)
logging.getLogger().addHandler(ring_handler)
for _vlogger in ("discord.voice_state", "discord.voice_client", "discord.player"):
    logging.getLogger(_vlogger).setLevel(logging.DEBUG)

logger = logging.getLogger("ElCompitaMusica")

load_dotenv()
DISCORD_TOKEN: Optional[str] = os.getenv("DISCORD_TOKEN")
YOUTUBE_COOKIES: Optional[str] = os.getenv("YOUTUBE_COOKIES")
COOKIES_MASTER_FILE: str = os.path.join(tempfile.gettempdir(), "yt_cookies_master.txt")
COOKIES_WORKING_FILE: str = os.path.join(tempfile.gettempdir(), "yt_cookies_working.txt")
AUDIO_CACHE_DIR: str = os.path.join(tempfile.gettempdir(), "elcompita_audio")
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)

BGUTIL_PROCESS: Optional[subprocess.Popen] = None


def start_bgutil_pot_server() -> None:
    """
    Inicia el servidor local bgutil-ytdlp-pot-provider en 127.0.0.1:4416 para generar
    Proof-of-Origin Tokens (po_token) de YouTube automáticamente en Render/Docker.
    """
    global BGUTIL_PROCESS
    server_script = "/opt/bgutil/server/build/main.js"
    if not os.path.isfile(server_script):
        logger.info("ℹ️ Servidor bgutil local no encontrado en /opt/bgutil (modo fuera de contenedor).")
        return
    try:
        BGUTIL_PROCESS = subprocess.Popen(
            ["node", server_script, "--port", "4416"],
            cwd="/opt/bgutil/server",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        for _ in range(35):
            if BGUTIL_PROCESS.poll() is not None:
                out = ""
                if BGUTIL_PROCESS.stdout:
                    out = BGUTIL_PROCESS.stdout.read().decode("utf-8", errors="ignore").strip()
                logger.warning(f"⚠️ Servidor POT bgutil terminó prematuramente: {out[:300]}")
                return
            time.sleep(0.1)
            try:
                with socket.create_connection(("127.0.0.1", 4416), timeout=0.2):
                    logger.info("🛡️ Servidor POT (bgutil-ytdlp-pot-provider) activo en 127.0.0.1:4416")
                    return
            except OSError:
                continue
        logger.info("🛡️ Proceso POT iniciado en segundo plano (127.0.0.1:4416).")
    except Exception as exc:
        logger.warning(f"No se pudo iniciar servidor POT bgutil: {exc}")


def ensure_opus_loaded() -> bool:
    """Garantiza que libopus esté cargado en memoria antes de reproducir audio en Discord."""
    if discord.opus.is_loaded():
        return True

    try:
        discord.opus._load_default()
        if discord.opus.is_loaded():
            logger.info("✅ libopus cargado mediante _load_default()")
            return True
    except Exception:
        pass

    candidates = [
        ctypes.util.find_library("opus"),
        "libopus.so.0",
        "libopus.so",
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/aarch64-linux-gnu/libopus.so.0",
        "/lib/x86_64-linux-gnu/libopus.so.0",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            discord.opus.load_opus(candidate)
            if discord.opus.is_loaded():
                logger.info(f"✅ libopus cargado exitosamente desde: {candidate}")
                return True
        except Exception:
            continue

    logger.error("❌ No se pudo cargar libopus. El audio de voz podría fallar.")
    return False


def _decrypt_cookies_enc(enc_path: str, secret: str) -> Optional[str]:
    """Desencripta cookies.enc usando el DISCORD_TOKEN como llave simétrica."""
    try:
        with open(enc_path, "r", encoding="utf-8") as f:
            raw = base64.b64decode(f.read().strip())
        salt, mac, ciphertext = raw[:16], raw[16:48], raw[48:]
        key = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 100_000, dklen=32)
        expected_mac = hmac.new(key, salt + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected_mac):
            return None
        out = bytearray()
        for idx in range(0, len(ciphertext), 32):
            block = hashlib.sha256(key + idx.to_bytes(8, "big")).digest()
            chunk = ciphertext[idx : idx + 32]
            out.extend(a ^ b for a, b in zip(chunk, block))
        return bytes(out).decode("utf-8")
    except Exception as exc:
        logger.warning(f"No se pudo desencriptar cookies.enc: {exc}")
        return None


def prepare_cookies_file() -> Optional[str]:
    """Prepara un archivo maestro de cookies para YouTube sin permitir que yt-dlp lo sobreescriba."""
    if os.path.isfile("cookies.txt") and os.path.getsize("cookies.txt") > 0:
        try:
            shutil.copyfile("cookies.txt", COOKIES_MASTER_FILE)
            shutil.copyfile(COOKIES_MASTER_FILE, COOKIES_WORKING_FILE)
            logger.info("✅ Cookies de YouTube cargadas desde ./cookies.txt")
            return COOKIES_WORKING_FILE
        except Exception as exc:
            logger.warning(f"Error copiando ./cookies.txt: {exc}")

    if YOUTUBE_COOKIES and YOUTUBE_COOKIES.strip():
        try:
            content = YOUTUBE_COOKIES.strip()
            if not content.startswith("#") and ".youtube.com" not in content:
                content = base64.b64decode(content).decode("utf-8")
            with open(COOKIES_MASTER_FILE, "w", encoding="utf-8") as f:
                f.write(content.strip() + "\n")
            shutil.copyfile(COOKIES_MASTER_FILE, COOKIES_WORKING_FILE)
            logger.info("✅ Cookies de YouTube cargadas desde YOUTUBE_COOKIES")
            return COOKIES_WORKING_FILE
        except Exception as exc:
            logger.warning(f"No se pudo procesar YOUTUBE_COOKIES: {exc}")

    if os.path.isfile("cookies.enc") and DISCORD_TOKEN:
        decrypted = _decrypt_cookies_enc("cookies.enc", DISCORD_TOKEN)
        if decrypted and ".youtube.com" in decrypted:
            try:
                with open(COOKIES_MASTER_FILE, "w", encoding="utf-8") as f:
                    f.write(decrypted.strip() + "\n")
                shutil.copyfile(COOKIES_MASTER_FILE, COOKIES_WORKING_FILE)
                logger.info("✅ Cookies de YouTube desencriptadas y activadas desde cookies.enc")
                return COOKIES_WORKING_FILE
            except Exception as exc:
                logger.warning(f"No se pudo escribir cookies desencriptadas: {exc}")

    logger.info("ℹ️ No se detectaron cookies; se usará modo POT + híbrido.")
    return None


ACTIVE_COOKIES_FILE = prepare_cookies_file()


def get_fresh_cookie_file() -> Optional[str]:
    """Devuelve una copia fresca del archivo de cookies para que yt-dlp no lo corrompa."""
    if os.path.isfile(COOKIES_MASTER_FILE) and os.path.getsize(COOKIES_MASTER_FILE) > 0:
        try:
            shutil.copyfile(COOKIES_MASTER_FILE, COOKIES_WORKING_FILE)
            return COOKIES_WORKING_FILE
        except Exception:
            return COOKIES_MASTER_FILE
    return ACTIVE_COOKIES_FILE


def build_ytdl_options(
    *,
    allow_playlist: bool = False,
    flat_playlist: bool = False,
    playlist_end: int = 100,
    default_search: str = "ytsearch",
    use_cookies: bool = False,
    for_soundcloud: bool = False,
    player_clients: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Construye opciones optimizadas de yt-dlp:
    - En YouTube utiliza el proveedor POT local (http://127.0.0.1:4416) + Node.js para yt-dlp-ejs.
    - En SoundCloud fuerza MP3 progresivo/HLS (http_mp3_1_0 / hls_mp3_1_0).
    """
    if for_soundcloud:
        fmt = "http_mp3_1_0/hls_mp3_1_0/bestaudio[ext=mp3]/bestaudio[ext=opus]/bestaudio/best"
    else:
        fmt = (
            "bestaudio[ext=webm][protocol=https]/"
            "bestaudio[ext=m4a][protocol=https]/"
            "bestaudio[ext=mp3][protocol=https]/"
            "bestaudio[protocol=https]/"
            "bestaudio/best"
        )

    opts: Dict[str, Any] = {
        "format": fmt,
        "default_search": default_search,
        "noplaylist": not allow_playlist,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "extract_flat": "in_playlist" if flat_playlist else False,
        "playlistend": playlist_end,
        "source_address": "0.0.0.0",
        "geo_bypass": True,
        "nocheckcertificate": True,
        "buffersize": 65536,
        "retries": 3,
        "fragment_retries": 3,
        "js_runtimes": {"node": {}},
    }

    if not for_soundcloud:
        ext_args: Dict[str, Any] = {
            "youtubepot-bgutilhttp": {
                "base_url": ["http://127.0.0.1:4416"],
            }
        }
        if player_clients:
            ext_args["youtube"] = {"player_client": player_clients}
        opts["extractor_args"] = ext_args

    if use_cookies and not for_soundcloud:
        cookie_path = get_fresh_cookie_file()
        if cookie_path and os.path.isfile(cookie_path) and os.path.getsize(cookie_path) > 0:
            opts["cookiefile"] = cookie_path

    return opts


# Opciones de FFmpeg para archivos locales en /tmp
FFMPEG_LOCAL_OPTIONS = {
    "before_options": "-nostdin",
    "options": "-vn",
}

# Opciones de FFmpeg para streaming directo con reconexión
FFMPEG_STREAM_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
        "-reconnect_on_http_error 4xx,5xx -reconnect_delay_max 5 -nostdin"
    ),
    "options": "-vn -bufsize 4M",
}


# ==============================================================================
# CLIENTE DE VOZ RESILIENTE (CORRIGE BUGS DE RECONEXIÓN EN DISCORD.PY 2.7.1)
# ==============================================================================

class ResilientVoiceConnectionState(VoiceConnectionState):
    def __init__(self, voice_client: discord.VoiceClient, *, hook: Optional[Any] = None) -> None:
        self._last_external_sender: Optional[bytes] = None
        super().__init__(voice_client, hook=hook)

    async def reinit_dave_session(self) -> None:
        await super().reinit_dave_session()
        if self.dave_session is not None and self._last_external_sender is not None:
            try:
                self.dave_session.set_external_sender(self._last_external_sender)
            except Exception:
                pass
        logger.info(
            f"🔐 DAVE reinit: ver={self.dave_protocol_version} "
            f"session={self.dave_session is not None} "
            f"ready={getattr(self.dave_session, 'ready', None)}"
        )

    async def _connect_websocket(self, resume: bool) -> DiscordVoiceWebSocket:
        ws = await super()._connect_websocket(resume)
        orig_bin = ws.received_binary_message

        async def _wrapped_binary(msg: bytes) -> None:
            op = msg[2] if len(msg) > 2 else -1
            if op == DiscordVoiceWebSocket.MLS_EXTERNAL_SENDER and len(msg) > 3:
                self._last_external_sender = msg[3:]
            try:
                await orig_bin(msg)
                rdy = getattr(self.dave_session, "ready", None) if self.dave_session else None
                logger.info(
                    f"🔑 Voice WS Binary op={op} len={len(msg)}B | dave_ready={rdy} can_encrypt={self.can_encrypt}"
                )
            except Exception as exc:
                logger.exception(f"❌ Error procesando frame binario DAVE op={op}: {exc}")

        ws.received_binary_message = _wrapped_binary  # type: ignore
        return ws

    async def voice_server_update(self, data: Any) -> None:
        was_connected = self.state is ConnectionFlowState.connected
        if was_connected:
            # Reiniciar IP para que _handshake_websocket espere el nuevo descubrimiento UDP tras READY
            self.ip = MISSING
        await super().voice_server_update(data)
        if was_connected and self.endpoint:
            try:
                self._create_socket()
            except Exception as exc:
                logger.debug(f"Aviso recreando socket UDP tras cambio de servidor de voz: {exc}")


class ResilientVoiceClient(discord.VoiceClient):
    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        self._pkt_count: int = 0
        self._drop_count: int = 0
        super().__init__(client, channel)

    def create_connection_state(self) -> VoiceConnectionState:
        async def _on_voice_ws_msg(ws: DiscordVoiceWebSocket, msg: Dict[str, Any]) -> None:
            op = msg.get("op")
            if op in (
                DiscordVoiceWebSocket.READY,
                DiscordVoiceWebSocket.SESSION_DESCRIPTION,
                DiscordVoiceWebSocket.SPEAKING,
                DiscordVoiceWebSocket.DAVE_PREPARE_TRANSITION,
                DiscordVoiceWebSocket.DAVE_EXECUTE_TRANSITION,
                DiscordVoiceWebSocket.DAVE_PREPARE_EPOCH,
                11,
                12,
                13,
            ):
                rdy = getattr(self._connection.dave_session, "ready", None) if self._connection.dave_session else None
                logger.info(f"📨 Voice WS JSON op={op} | dave_v={self._connection.dave_protocol_version} ready={rdy}")

            if op == DiscordVoiceWebSocket.SESSION_DESCRIPTION:
                if self.is_playing() and not self.is_paused():
                    try:
                        await ws.speak(SpeakingState.voice)
                        logger.info("🔊 Estado speaking(voice) restaurado tras sesión/reconexión de voz.")
                    except Exception as exc:
                        logger.debug(f"No se pudo restaurar speaking(voice): {exc}")

        return ResilientVoiceConnectionState(self, hook=_on_voice_ws_msg)

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        self._pkt_count += 1
        self.checked_add("sequence", 1, 65535)
        if encode:
            encoded_data = self.encoder.encode(data, self.encoder.SAMPLES_PER_FRAME)
        else:
            encoded_data = data
        packet = self._get_voice_packet(encoded_data)
        try:
            self._connection.send_packet(packet)
            if self._pkt_count in (1, 50, 150, 400) or self._pkt_count % 1000 == 0:
                conn = self._connection
                dave_rdy = getattr(conn.dave_session, "ready", None) if conn.dave_session else None
                logger.info(
                    f"📡 RTP #{self._pkt_count}: seq={self.sequence} pcm={len(data)}B opus={len(encoded_data)}B "
                    f"pkt={len(packet)}B mode={self.mode} dave_v={conn.dave_protocol_version} "
                    f"dave_ready={dave_rdy} can_enc={conn.can_encrypt}"
                )
        except OSError as exc:
            self._drop_count += 1
            if self._drop_count <= 5 or self._drop_count % 100 == 0:
                logger.warning(f"⚠️ UDP packet dropped (#{self._drop_count}, seq={self.sequence}): {exc}")
        self.checked_add("timestamp", discord.opus.Encoder.SAMPLES_PER_FRAME, 4294967295)


# ==============================================================================
# FILTRO ANTI-COVERS / ANTI-REMIXES Y VERIFICACIÓN DE AUDIO PCM
# ==============================================================================

BANNED_VARIANT_KEYWORDS = (
    "slowed",
    "reverb",
    "sped up",
    "speed up",
    "nightcore",
    "daycore",
    "hardcore",
    "uptempo",
    "frenchcore",
    "hardstyle",
    "remix",
    "bootleg",
    "mashup",
    "megamix",
    "cover",
    "karaoke",
    "instrumental",
    "acapella",
    "a cappella",
    "8d audio",
    "8d ",
    "teaser",
    "preview",
    "snippet",
    "ringtone",
    "parody",
    "parodia",
    "tribute",
    "rework",
    "flip",
    "bass boosted",
    "earrape",
)


def is_unwanted_audio_variant(
    candidate_title: str,
    user_query: str = "",
    expected_duration: Optional[int] = None,
    candidate_duration: Optional[int] = None,
    strict_duration: bool = True,
) -> bool:
    """
    Devuelve True si el candidato es un cover, remix raro, slowed+reverb, hardcore,
    preview de 30s o versión alterada que el usuario NO pidió.
    """
    cand_lower = (candidate_title or "").lower()
    query_lower = (user_query or "").lower()

    for kw in BANNED_VARIANT_KEYWORDS:
        if kw in cand_lower and kw not in query_lower:
            return True

    if candidate_duration is not None:
        dur = int(candidate_duration)
        # Rechazar previews de 30 segundos (SoundCloud Go+) o clips cortos
        if dur < 45:
            return True
        # Rechazar sets largos de DJ (>15 min) salvo que la pista original dure eso
        if dur > 900 and (not expected_duration or int(expected_duration) < 600):
            return True
        if strict_duration and expected_duration and int(expected_duration) >= 45:
            if abs(dur - int(expected_duration)) > 28:
                return True

    return False


def verify_audio_file_pcm(filepath: str) -> bool:
    """
    Verifica con FFmpeg que el archivo descargado en /tmp contenga muestras de audio reales (no silencio ni DRM).
    """
    try:
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            filepath,
            "-t",
            "3",
            "-f",
            "s16le",
            "-ac",
            "2",
            "-ar",
            "48000",
            "pipe:1",
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
        pcm_bytes = proc.stdout
        stderr_txt = proc.stderr.decode("utf-8", errors="ignore").strip()
        if len(pcm_bytes) < 16000:
            logger.warning(f"⚠️ Archivo {filepath} produjo muy pocos datos PCM ({len(pcm_bytes)}B). Err: {stderr_txt}")
            return False

        samples = array.array("h")
        samples.frombytes(pcm_bytes[: (len(pcm_bytes) // 2) * 2])
        peak = max((abs(s) for s in samples), default=0)
        logger.info(
            f"🔊 Audio verificado con FFmpeg ({os.path.basename(filepath)}): {len(pcm_bytes)} bytes PCM en 3s | pico={peak}/32767"
        )
        return peak > 10
    except Exception as exc:
        logger.warning(f"Error verificando PCM de {filepath}: {exc}")
        return True


def is_playlist_query(query: str) -> bool:
    """Detecta si el usuario envió una URL de playlist real (ignorando mixes automáticos RD... si tienen v=)."""
    if not query.startswith(("http://", "https://")):
        return False
    parsed = urlparse(query)
    qs = parse_qs(parsed.query)
    if "/playlist" in parsed.path or "/sets/" in parsed.path or "/album/" in parsed.path:
        return True
    if "list" in qs:
        list_id = qs["list"][0]
        if "v" in qs and (list_id.startswith("RD") or list_id.startswith("OLAK")):
            return False
        return True
    return False


def extract_youtube_video_id(url_or_query: str) -> Optional[str]:
    """Extrae el video_id de 11 caracteres de cualquier URL de YouTube / YouTube Music / youtu.be."""
    if not url_or_query.startswith(("http://", "https://")):
        return None
    try:
        parsed = urlparse(url_or_query)
        host = parsed.netloc.lower()
        if "youtu.be" in host:
            vid = parsed.path.lstrip("/").split("/")[0]
            return vid if len(vid) == 11 else None
        if "youtube.com" in host:
            qs = parse_qs(parsed.query)
            if "v" in qs and qs["v"] and len(qs["v"][0]) == 11:
                return qs["v"][0]
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v") and len(parts[1]) == 11:
                return parts[1]
    except Exception:
        pass
    return None


def normalize_youtube_url(url: str, *, playlist_mode: bool = False) -> str:
    """Convierte enlaces de music.youtube.com o youtu.be a formato estándar www.youtube.com."""
    if not url.startswith(("http://", "https://")):
        return url
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if "youtube.com" not in host and "youtu.be" not in host:
            return url

        if "youtu.be" in host:
            vid_id = parsed.path.lstrip("/").split("/")[0]
            if vid_id:
                return f"https://www.youtube.com/watch?v={vid_id}"

        qs = parse_qs(parsed.query)
        if playlist_mode and "list" in qs and qs["list"]:
            list_id = qs["list"][0]
            if "v" in qs and qs["v"] and not list_id.startswith("PL"):
                return f"https://www.youtube.com/watch?v={qs['v'][0]}&list={list_id}"
            return f"https://www.youtube.com/playlist?list={list_id}"

        if "v" in qs and qs["v"]:
            return f"https://www.youtube.com/watch?v={qs['v'][0]}"
    except Exception:
        pass
    return url


def fetch_youtube_oembed_metadata(url: str) -> Optional[Dict[str, Any]]:
    """Obtiene el título, artista y miniatura de un video de YouTube/YouTube Music vía oEmbed público."""
    try:
        clean_url = normalize_youtube_url(url, playlist_mode=False)
        oembed_url = f"https://www.youtube.com/oembed?url={quote(clean_url, safe='')}&format=json"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        title = data.get("title", "").strip()
        author = data.get("author_name", "").strip()
        for suffix in (" - Topic", "VEVO", "Official"):
            if author.endswith(suffix):
                author = author[: -len(suffix)].strip()

        vid_id = extract_youtube_video_id(clean_url) or ""
        full_title = title
        if author and author.lower() not in title.lower() and " - " not in title and " – " not in title:
            full_title = f"{author} - {title}"

        return {
            "title": full_title or title,
            "raw_title": title,
            "author": author,
            "thumbnail": data.get("thumbnail_url"),
            "video_id": vid_id,
            "webpage_url": clean_url,
            "duration": None,
        }
    except Exception as exc:
        logger.debug(f"oEmbed no disponible para {url}: {exc}")
        return None


def fetch_youtube_flat_search(query: str) -> Optional[Dict[str, Any]]:
    """
    Busca en YouTube con extract_flat=True (que NO reproduce el video ni sufre bloqueo de bot)
    para obtener el video_id, título, duración y carátula oficial de YouTube en <0.8s.
    """
    try:
        opts = build_ytdl_options(
            allow_playlist=False,
            flat_playlist=True,
            playlist_end=5,
            default_search="ytsearch",
            use_cookies=False,
        )
        opts["extract_flat"] = True
        search_target = query if query.startswith(("http://", "https://")) else f"ytsearch5:{query}"
        with yt_dlp.YoutubeDL(opts) as ytdl:
            info = ytdl.extract_info(search_target, download=False)
        entries = (info or {}).get("entries") or ([info] if info and info.get("id") else [])
        for entry in entries:
            if not entry:
                continue
            vid_id = entry.get("id")
            title = entry.get("title")
            duration = entry.get("duration")
            if not vid_id or not title:
                continue
            if not query.startswith(("http://", "https://")) and is_unwanted_audio_variant(
                title, user_query=query, candidate_duration=duration, strict_duration=False
            ):
                continue
            channel = (entry.get("channel") or entry.get("uploader") or "").replace(" - Topic", "").strip()
            full_title = title
            if channel and channel.lower() not in title.lower() and " - " not in title and " – " not in title:
                full_title = f"{channel} - {title}"
            thumbnails = entry.get("thumbnails") or []
            thumb = entry.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else None)
            if not thumb and len(str(vid_id)) == 11:
                thumb = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
            return {
                "title": full_title,
                "raw_title": title,
                "author": channel,
                "thumbnail": thumb,
                "video_id": str(vid_id),
                "webpage_url": f"https://www.youtube.com/watch?v={vid_id}",
                "duration": duration,
            }
    except Exception as exc:
        logger.debug(f"Flat search de YouTube falló para '{query}': {exc}")
    return None


def fetch_youtube_piped_stream(video_id: str) -> Optional[Dict[str, Any]]:
    """
    Si yt-dlp llega a fallar en un video_id de YouTube, consulta instancias públicas de Piped/Invidious
    para obtener el stream de audio original exacto de ese mismo video_id de YouTube.
    """
    if not video_id or len(video_id) != 11:
        return None

    piped_hosts = [
        "https://pipedapi.kavin.rocks",
        "https://api.piped.private.coffee",
        "https://pipedapi.tokhmi.xyz",
        "https://pipedapi.moomoo.me",
    ]
    for host in piped_hosts:
        try:
            url = f"{host}/streams/{video_id}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            audio_streams = data.get("audioStreams") or []
            best_stream = None
            best_bitrate = -1
            for st in audio_streams:
                st_url = st.get("url")
                bitrate = int(st.get("bitrate") or 0)
                if st_url and bitrate > best_bitrate:
                    best_stream = st_url
                    best_bitrate = bitrate
            if best_stream:
                logger.info(f"✅ Stream exacto de YouTube obtenido vía Piped ({host}) para {video_id}")
                return {
                    "url": best_stream,
                    "title": data.get("title"),
                    "duration": data.get("duration"),
                    "thumbnail": data.get("thumbnailUrl"),
                }
        except Exception:
            continue

    invidious_hosts = [
        "https://inv.nadeko.net",
        "https://invidious.nerdvpn.de",
        "https://yewtu.be",
    ]
    for host in invidious_hosts:
        try:
            url = f"{host}/api/v1/videos/{video_id}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            adaptive = data.get("adaptiveFormats") or []
            for fmt in adaptive:
                mime = str(fmt.get("type") or "")
                fmt_url = fmt.get("url")
                if fmt_url and mime.startswith("audio/"):
                    logger.info(f"✅ Stream exacto de YouTube obtenido vía Invidious ({host}) para {video_id}")
                    return {
                        "url": fmt_url,
                        "title": data.get("title"),
                        "duration": data.get("lengthSeconds"),
                        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    }
        except Exception:
            continue

    return None


@dataclass
class Track:
    """Representa una pista de audio en la cola de reproducción."""

    title: str
    webpage_url: str
    requester: discord.abc.User
    stream_url: Optional[str] = None
    local_file: Optional[str] = None
    soundcloud_url: Optional[str] = None
    video_id: Optional[str] = None
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    is_autoplay: bool = False
    based_on_title: Optional[str] = None

    @property
    def formatted_duration(self) -> str:
        if not self.duration:
            return "En vivo / Desconocida"
        minutes, seconds = divmod(int(self.duration), 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


def cleanup_track_file(track: Optional[Track]) -> None:
    """Elimina el archivo temporal de /tmp cuando la canción termina para no llenar la RAM de Render."""
    if not track or not track.local_file:
        return
    try:
        if os.path.isfile(track.local_file):
            os.remove(track.local_file)
    except Exception:
        pass
    track.local_file = None


class GuildMusicState:
    """Gestiona el estado musical aislado para cada servidor (Guild) de Discord."""

    def __init__(self) -> None:
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None
        self.last_track: Optional[Track] = None
        self.autoplay: bool = True
        self.history_ids: Deque[str] = deque(maxlen=40)
        self.text_channel: Optional[discord.abc.Messageable] = None
        self.inactivity_task: Optional[asyncio.Task] = None
        self.prefetch_task: Optional[asyncio.Task] = None
        self.play_lock: asyncio.Lock = asyncio.Lock()

    def register_played(self, track: Track) -> None:
        self.current = track
        self.last_track = track
        if track.video_id and track.video_id not in self.history_ids:
            self.history_ids.append(track.video_id)
        if track.webpage_url and track.webpage_url not in self.history_ids:
            self.history_ids.append(track.webpage_url)
        if track.title:
            self.history_ids.append(track.title.lower())

    def clear(self) -> None:
        while self.queue:
            t = self.queue.popleft()
            cleanup_track_file(t)
        cleanup_track_file(self.current)
        self.current = None
        self.last_track = None
        self.cancel_inactivity_task()
        self.cancel_prefetch_task()

    def cancel_inactivity_task(self) -> None:
        if self.inactivity_task and not self.inactivity_task.done():
            self.inactivity_task.cancel()
            self.inactivity_task = None

    def cancel_prefetch_task(self) -> None:
        if self.prefetch_task and not self.prefetch_task.done():
            self.prefetch_task.cancel()
            self.prefetch_task = None


def _try_direct_youtube_info(target_query: str) -> Optional[Dict[str, Any]]:
    """
    Intenta extraer el stream directo oficial de YouTube probando:
    1) Con proveedor POT (bgutil) sin cookies (evita rechazos por cookies rotadas de otra IP).
    2) Con proveedor POT (bgutil) + cookies.
    """
    profiles = [
        {"use_cookies": False, "player_clients": ["default", "mweb", "tv"]},
        {"use_cookies": True, "player_clients": ["default", "tv", "web"]},
    ]
    for prof in profiles:
        opts = build_ytdl_options(
            allow_playlist=False,
            flat_playlist=False,
            use_cookies=prof["use_cookies"],
            for_soundcloud=False,
            player_clients=prof["player_clients"],
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ytdl:
                info = ytdl.extract_info(target_query, download=False)
            if info and "entries" in info:
                entries = [e for e in (info.get("entries") or []) if e]
                info = entries[0] if entries else None
            if info and info.get("url"):
                logger.info(
                    f"✅ Stream directo de YouTube obtenido (cookies={prof['use_cookies']}): {info.get('title')}"
                )
                return info
        except Exception as exc:
            logger.debug(
                f"Intento YouTube (cookies={prof['use_cookies']}) falló para '{target_query}': {exc}"
            )
    return None


def _resolve_soundcloud_stream(
    search_text: str,
    requester: discord.abc.User,
    *,
    yt_meta: Optional[Dict[str, Any]] = None,
    user_query: str = "",
) -> Track:
    """
    Resuelve el stream de audio completo en formato MP3 (http_mp3_1_0 / hls_mp3_1_0),
    filtrando estrictamente covers, remixes raros, slowed+reverb, hardcore y previews de 30s,
    y verificando que la duración coincida con la canción original de YouTube.
    """
    clean_query = search_text.strip()
    for noise in (
        "(Official Video)",
        "(Video Oficial)",
        "[Official Video]",
        "(Official Music Video)",
        "[Official Music Video]",
        "(Official Audio)",
        "(Audio Oficial)",
        "(Official Lyric Video)",
        "(Lyrics)",
        "(Letra)",
        "(Visualizer)",
        " - Topic",
    ):
        clean_query = re.sub(re.escape(noise), "", clean_query, flags=re.IGNORECASE)
    clean_query = clean_query.strip()

    if clean_query.startswith(("http://", "https://")) and "soundcloud.com" in clean_query:
        sc_single_opts = build_ytdl_options(
            allow_playlist=False,
            flat_playlist=False,
            use_cookies=False,
            for_soundcloud=True,
        )
        with yt_dlp.YoutubeDL(sc_single_opts) as ytdl:
            info = ytdl.extract_info(clean_query, download=False)
        if info and info.get("url"):
            return Track(
                title=info.get("title", clean_query),
                webpage_url=info.get("webpage_url") or clean_query,
                soundcloud_url=info.get("webpage_url") or clean_query,
                stream_url=info["url"],
                video_id=str(info.get("id", "")),
                duration=info.get("duration"),
                thumbnail=info.get("thumbnail"),
                requester=requester,
            )

    flat_opts = build_ytdl_options(
        allow_playlist=False,
        flat_playlist=True,
        playlist_end=12,
        default_search="scsearch",
        use_cookies=False,
        for_soundcloud=True,
    )
    flat_opts["ignoreerrors"] = True

    with yt_dlp.YoutubeDL(flat_opts) as ytdl:
        search_info = ytdl.extract_info(f"scsearch12:{clean_query}", download=False)

    entries = [e for e in (search_info or {}).get("entries", []) if e]
    if not entries:
        raise ValueError(f"No se encontraron resultados disponibles para '{search_text}'.")

    stream_opts = build_ytdl_options(
        allow_playlist=False,
        flat_playlist=False,
        use_cookies=False,
        for_soundcloud=True,
    )
    expected_dur = yt_meta.get("duration") if yt_meta else None

    # Dos pasadas: 1) Coincidencia estricta de duración (±28s) y sin palabras prohibidas.
    #              2) Sin palabras prohibidas ni previews (<45s).
    for strict_pass in (True, False):
        for entry in entries:
            cand_url = entry.get("url") or entry.get("webpage_url")
            cand_title = entry.get("title") or ""
            cand_dur = entry.get("duration")
            if not cand_url:
                continue

            if is_unwanted_audio_variant(
                cand_title,
                user_query=user_query or clean_query,
                expected_duration=expected_dur,
                candidate_duration=cand_dur,
                strict_duration=strict_pass,
            ):
                continue

            try:
                with yt_dlp.YoutubeDL(stream_opts) as ytdl:
                    track_info = ytdl.extract_info(cand_url, download=False)
                if not track_info or not track_info.get("url"):
                    continue

                full_title = track_info.get("title") or cand_title
                duration = track_info.get("duration") or cand_dur
                if is_unwanted_audio_variant(
                    full_title,
                    user_query=user_query or clean_query,
                    expected_duration=expected_dur,
                    candidate_duration=duration,
                    strict_duration=strict_pass,
                ):
                    continue

                best_url = track_info["url"]
                for fmt in track_info.get("formats") or []:
                    fmt_id = str(fmt.get("format_id") or "")
                    fmt_url = fmt.get("url")
                    if fmt_url and "http_mp3" in fmt_id:
                        best_url = fmt_url
                        break

                sc_page = track_info.get("webpage_url") or cand_url
                display_title = (
                    (yt_meta.get("title") if yt_meta else None)
                    or full_title
                    or clean_query
                )
                display_webpage = (yt_meta.get("webpage_url") if yt_meta else None) or sc_page
                display_vid_id = (yt_meta.get("video_id") if yt_meta else None) or str(track_info.get("id", ""))
                display_thumb = (yt_meta.get("thumbnail") if yt_meta else None) or track_info.get("thumbnail")

                candidate_track = Track(
                    title=display_title,
                    webpage_url=display_webpage,
                    soundcloud_url=sc_page,
                    stream_url=best_url,
                    video_id=display_vid_id,
                    duration=duration or expected_dur,
                    thumbnail=display_thumb,
                    requester=requester,
                )
                logger.info(
                    f"✅ Pista verificada seleccionada: {candidate_track.title} (fuente='{full_title}', {duration}s)"
                )
                return candidate_track
            except Exception as exc:
                logger.debug(f"Saltando candidato ({cand_url}) por error: {exc}")
                continue

    raise ValueError(f"No se encontró una versión original completa para '{clean_query}'.")


def _find_downloaded_file(prefix: str) -> Optional[str]:
    """Busca en AUDIO_CACHE_DIR el archivo descargado con el prefijo dado."""
    try:
        for fname in os.listdir(AUDIO_CACHE_DIR):
            if fname.startswith(prefix) and not fname.endswith((".part", ".ytdl", ".temp")):
                fpath = os.path.join(AUDIO_CACHE_DIR, fname)
                if os.path.isfile(fpath) and os.path.getsize(fpath) > 4096:
                    return fpath
    except Exception:
        pass
    return None


def download_track_to_tmp(track: Track) -> Track:
    """
    Descarga la pista en /tmp/elcompita_audio priorizando SIEMPRE el audio original de YouTube:
    1) Descarga directa de YouTube con yt-dlp + POT provider (sin cookies y con cookies).
    2) Si yt-dlp falla y hay video_id de YouTube, extrae el stream original de YouTube vía Piped/Invidious y lo descarga con FFmpeg.
    3) Solo como último recurso, busca la versión oficial equivalente (filtrando covers/remixes/previews).
    """
    if track.local_file and os.path.isfile(track.local_file) and os.path.getsize(track.local_file) > 4096:
        return track

    if track.duration and int(track.duration) > 1500:
        return resolve_track_stream(track)

    uid = f"trk_{uuid.uuid4().hex[:10]}"
    outtmpl = os.path.join(AUDIO_CACHE_DIR, f"{uid}.%(ext)s")

    is_explicit_soundcloud = bool(track.webpage_url and "soundcloud.com" in track.webpage_url)

    # 1. Intentar SIEMPRE descargar primero desde YouTube (salvo que el usuario haya pasado un link de SoundCloud)
    if not is_explicit_soundcloud:
        yt_target = track.webpage_url
        if not yt_target or not yt_target.startswith(("http://", "https://")) or "soundcloud.com" in yt_target:
            if track.video_id and len(str(track.video_id)) == 11:
                yt_target = f"https://www.youtube.com/watch?v={track.video_id}"
            else:
                yt_target = f"ytsearch1:{track.title}"
        else:
            yt_target = normalize_youtube_url(yt_target, playlist_mode=False)

        for use_cookies, clients in (
            (False, ["default", "mweb", "tv"]),
            (True, ["default", "tv", "web"]),
        ):
            yt_opts = build_ytdl_options(
                allow_playlist=False,
                flat_playlist=False,
                use_cookies=use_cookies,
                for_soundcloud=False,
                player_clients=clients,
            )
            yt_opts["outtmpl"] = outtmpl
            try:
                with yt_dlp.YoutubeDL(yt_opts) as ytdl:
                    info = ytdl.extract_info(yt_target, download=True)
                    if info and "entries" in info:
                        entries = [e for e in (info.get("entries") or []) if e]
                        info = entries[0] if entries else None
                    found = _find_downloaded_file(uid)
                    if found and info and verify_audio_file_pcm(found):
                        track.local_file = found
                        track.stream_url = info.get("url") or track.stream_url
                        track.title = info.get("title") or track.title
                        track.webpage_url = info.get("webpage_url") or track.webpage_url
                        track.video_id = str(info.get("id") or track.video_id or "")
                        track.duration = info.get("duration") or track.duration
                        track.thumbnail = info.get("thumbnail") or track.thumbnail
                        logger.info(
                            f"⚡ Audio original de YouTube descargado en /tmp ({os.path.getsize(found) // 1024} KB): {track.title}"
                        )
                        return track
                    elif found:
                        cleanup_track_file(Track(title="", webpage_url="", requester=track.requester, local_file=found))
            except Exception as exc:
                logger.debug(f"Intento descarga YouTube (cookies={use_cookies}) falló para '{yt_target}': {exc}")

        # 2. Si yt-dlp falló y tenemos el video_id de YouTube, intentar extraer el audio exacto del video vía Piped/Invidious
        vid_id = track.video_id if (track.video_id and len(str(track.video_id)) == 11) else extract_youtube_video_id(yt_target)
        if vid_id:
            piped_info = fetch_youtube_piped_stream(vid_id)
            if piped_info and piped_info.get("url"):
                piped_file = os.path.join(AUDIO_CACHE_DIR, f"{uid}.m4a")
                try:
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-v",
                        "error",
                        "-nostdin",
                        "-reconnect",
                        "1",
                        "-reconnect_streamed",
                        "1",
                        "-i",
                        piped_info["url"],
                        "-vn",
                        "-c:a",
                        "copy",
                        piped_file,
                    ]
                    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
                    if os.path.isfile(piped_file) and verify_audio_file_pcm(piped_file):
                        track.local_file = piped_file
                        track.stream_url = piped_info["url"]
                        track.duration = track.duration or piped_info.get("duration")
                        track.thumbnail = track.thumbnail or piped_info.get("thumbnail")
                        logger.info(
                            f"⚡ Audio exacto de YouTube (Piped/Invidious) descargado en /tmp ({os.path.getsize(piped_file) // 1024} KB): {track.title}"
                        )
                        return track
                except Exception as exc:
                    logger.debug(f"Descarga Piped/Invidious falló para {vid_id}: {exc}")

    # 3. Respaldo verificado (sin covers ni remixes) y descarga a /tmp
    if not track.soundcloud_url and not is_explicit_soundcloud:
        track.stream_url = None
        track = resolve_track_stream(track)

    sc_target = track.soundcloud_url or (
        track.webpage_url if (track.webpage_url and "soundcloud.com" in track.webpage_url) else None
    )
    if sc_target:
        try:
            sc_opts = build_ytdl_options(
                allow_playlist=False,
                flat_playlist=False,
                use_cookies=False,
                for_soundcloud=True,
            )
            sc_opts["outtmpl"] = outtmpl
            with yt_dlp.YoutubeDL(sc_opts) as ytdl:
                ytdl.extract_info(sc_target, download=True)
            found = _find_downloaded_file(uid)
            if found and verify_audio_file_pcm(found):
                track.local_file = found
                logger.info(
                    f"⚡ Audio MP3 descargado en /tmp ({os.path.getsize(found) // 1024} KB): {track.title}"
                )
            elif found:
                cleanup_track_file(Track(title="", webpage_url="", requester=track.requester, local_file=found))
        except Exception as exc:
            logger.warning(f"Descarga MP3 a /tmp falló, se usará stream directo: {exc}")

    return track


def extract_tracks(query: str, requester: discord.abc.User) -> List[Track]:
    """
    Extrae una canción individual o una playlist completa de YouTube, YouTube Music o SoundCloud.
    Conserva SIEMPRE el enlace y video_id oficial de YouTube para reproducir el video exacto que pidió el usuario.
    """
    playlist_mode = is_playlist_query(query)
    normalized_query = normalize_youtube_url(query, playlist_mode=playlist_mode)

    if not playlist_mode:
        if normalized_query.startswith(("http://", "https://")) and "soundcloud.com" in normalized_query:
            return [_resolve_soundcloud_stream(normalized_query, requester, user_query=query)]

        # 1. Intentar extracción directa de YouTube (con POT provider)
        target_for_yt = (
            normalized_query
            if normalized_query.startswith(("http://", "https://"))
            else f"ytsearch1:{normalized_query}"
        )
        info = _try_direct_youtube_info(target_for_yt)
        if info:
            vid_id = str(info.get("id") or extract_youtube_video_id(normalized_query) or "")
            webpage = (
                info.get("webpage_url")
                or (f"https://www.youtube.com/watch?v={vid_id}" if len(vid_id) == 11 else normalized_query)
            )
            return [
                Track(
                    title=info.get("title", "Título desconocido"),
                    webpage_url=webpage,
                    stream_url=info.get("url"),
                    video_id=vid_id,
                    duration=info.get("duration"),
                    thumbnail=info.get("thumbnail"),
                    requester=requester,
                )
            ]

        # 2. Obtener metadatos oficiales exactos de YouTube (video_id, título, duración, miniatura)
        yt_meta: Optional[Dict[str, Any]] = None
        if normalized_query.startswith(("http://", "https://")) and (
            "youtube.com" in normalized_query or "youtu.be" in normalized_query
        ):
            yt_meta = fetch_youtube_flat_search(normalized_query) or fetch_youtube_oembed_metadata(normalized_query)
            if yt_meta and not yt_meta.get("title"):
                oembed = fetch_youtube_oembed_metadata(normalized_query)
                if oembed:
                    yt_meta.update({k: v for k, v in oembed.items() if v})
        elif not normalized_query.startswith(("http://", "https://")):
            yt_meta = fetch_youtube_flat_search(normalized_query)

        if yt_meta and yt_meta.get("video_id"):
            return [
                Track(
                    title=yt_meta.get("title") or query,
                    webpage_url=yt_meta.get("webpage_url") or normalized_query,
                    video_id=str(yt_meta["video_id"]),
                    duration=yt_meta.get("duration"),
                    thumbnail=yt_meta.get("thumbnail"),
                    requester=requester,
                )
            ]

        search_term = (yt_meta.get("title") if yt_meta else None) or query
        track = _resolve_soundcloud_stream(search_term, requester, yt_meta=yt_meta, user_query=query)
        return [track]

    # Modo Playlist
    opts = build_ytdl_options(
        allow_playlist=True,
        flat_playlist=True,
        playlist_end=100,
        use_cookies=False,
    )
    opts["ignoreerrors"] = True

    info = None
    yt_error = None
    try:
        with yt_dlp.YoutubeDL(opts) as ytdl:
            info = ytdl.extract_info(normalized_query, download=False)
    except Exception as exc:
        yt_error = exc
        logger.warning(f"Aviso en extracción de playlist ({normalized_query}): {exc}")

    if not info:
        raise ValueError(f"No se pudo obtener la playlist solicitada ({yt_error}).")

    tracks: List[Track] = []
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        raise ValueError(f"No se encontraron canciones en la playlist: `{query}`")

    for entry in entries:
        vid_id = entry.get("id")
        raw_url = entry.get("webpage_url") or entry.get("url")
        if not raw_url and vid_id:
            raw_url = f"https://www.youtube.com/watch?v={vid_id}"
        elif raw_url and not raw_url.startswith("http") and vid_id:
            raw_url = f"https://www.youtube.com/watch?v={vid_id}"

        if not raw_url:
            continue

        thumbnails = entry.get("thumbnails") or []
        thumb_url = entry.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else None)

        tracks.append(
            Track(
                title=entry.get("title") or "Canción de la playlist",
                webpage_url=normalize_youtube_url(raw_url, playlist_mode=False),
                stream_url=None,
                video_id=str(vid_id) if vid_id else None,
                duration=entry.get("duration"),
                thumbnail=thumb_url,
                requester=requester,
            )
        )

    if not tracks:
        raise ValueError("No se encontraron pistas reproducibles en la playlist.")

    return tracks


def resolve_track_stream(track: Track) -> Track:
    """Resuelve el stream de audio real de un Track priorizando YouTube."""
    if track.stream_url:
        return track

    target = track.webpage_url or (
        f"https://www.youtube.com/watch?v={track.video_id}" if track.video_id else track.title
    )
    target = normalize_youtube_url(target, playlist_mode=False)

    if "soundcloud.com" not in target:
        info = _try_direct_youtube_info(target)
        if info and info.get("url"):
            track.stream_url = info.get("url")
            track.title = info.get("title") or track.title
            track.webpage_url = info.get("webpage_url") or track.webpage_url
            track.video_id = str(info.get("id") or track.video_id or "")
            track.duration = info.get("duration") or track.duration
            track.thumbnail = info.get("thumbnail") or track.thumbnail
            return track

        vid_id = track.video_id if (track.video_id and len(str(track.video_id)) == 11) else extract_youtube_video_id(target)
        if vid_id:
            piped_info = fetch_youtube_piped_stream(vid_id)
            if piped_info and piped_info.get("url"):
                track.stream_url = piped_info["url"]
                track.duration = track.duration or piped_info.get("duration")
                track.thumbnail = track.thumbnail or piped_info.get("thumbnail")
                return track

    yt_meta: Optional[Dict[str, Any]] = None
    search_title = track.title
    if track.webpage_url and ("youtube.com" in track.webpage_url or "youtu.be" in track.webpage_url):
        yt_meta = fetch_youtube_flat_search(track.webpage_url) or fetch_youtube_oembed_metadata(track.webpage_url)
        if yt_meta and (not search_title or search_title == "Canción de la playlist"):
            search_title = yt_meta.get("title") or search_title
        if yt_meta and track.duration and not yt_meta.get("duration"):
            yt_meta["duration"] = track.duration
    elif track.duration:
        yt_meta = {
            "title": track.title,
            "webpage_url": track.webpage_url,
            "video_id": track.video_id,
            "duration": track.duration,
            "thumbnail": track.thumbnail,
        }

    sc_track = _resolve_soundcloud_stream(search_title, track.requester, yt_meta=yt_meta, user_query=track.title)
    track.stream_url = sc_track.stream_url
    track.soundcloud_url = sc_track.soundcloud_url
    track.title = track.title if track.title != "Canción de la playlist" else sc_track.title
    track.duration = track.duration or sc_track.duration
    track.thumbnail = track.thumbnail or sc_track.thumbnail
    return track


def fetch_recommended_track(
    last_track: Track,
    history_ids: List[str],
    bot_user: discord.abc.User,
) -> Optional[Track]:
    """
    Obtiene una canción recomendada oficial usando el Mix/Radio de YouTube (RD<video_id>),
    filtrando estrictamente remixes raros, covers, slowed+reverb o teasers.
    """
    history_set = set(history_ids)
    if last_track.video_id:
        history_set.add(last_track.video_id)
    if last_track.webpage_url:
        history_set.add(last_track.webpage_url)
    if last_track.title:
        history_set.add(last_track.title.lower())

    # 1. Consultar Mix oficial de YouTube (RD<video_id>) con extract_flat=True
    if last_track.video_id and len(str(last_track.video_id)) == 11:
        mix_url = f"https://www.youtube.com/watch?v={last_track.video_id}&list=RD{last_track.video_id}"
        mix_opts = build_ytdl_options(
            allow_playlist=True,
            flat_playlist=True,
            playlist_end=25,
            use_cookies=False,
        )
        mix_opts["ignoreerrors"] = True
        try:
            with yt_dlp.YoutubeDL(mix_opts) as ytdl:
                mix_info = ytdl.extract_info(mix_url, download=False)
            if mix_info and "entries" in mix_info:
                for entry in mix_info.get("entries") or []:
                    if not entry:
                        continue
                    cand_id = entry.get("id")
                    cand_title = entry.get("title") or "Recomendación"
                    cand_dur = entry.get("duration")
                    if (
                        not cand_id
                        or cand_id in history_set
                        or cand_title.lower() in history_set
                        or is_unwanted_audio_variant(cand_title, user_query="", candidate_duration=cand_dur, strict_duration=False)
                    ):
                        continue
                    webpage = f"https://www.youtube.com/watch?v={cand_id}"
                    candidate = Track(
                        title=cand_title,
                        webpage_url=webpage,
                        video_id=str(cand_id),
                        duration=cand_dur,
                        thumbnail=f"https://i.ytimg.com/vi/{cand_id}/hqdefault.jpg",
                        requester=bot_user,
                        is_autoplay=True,
                        based_on_title=last_track.title,
                    )
                    return download_track_to_tmp(candidate)
        except Exception as exc:
            logger.debug(f"Aviso en Mix RD para {last_track.video_id}: {exc}")

    # 2. Búsqueda de canciones oficiales relacionadas en YouTube (extract_flat=True)
    try:
        yt_search_opts = build_ytdl_options(
            allow_playlist=False,
            flat_playlist=True,
            playlist_end=10,
            default_search="ytsearch",
            use_cookies=False,
        )
        yt_search_opts["extract_flat"] = True
        with yt_dlp.YoutubeDL(yt_search_opts) as ytdl:
            s_info = ytdl.extract_info(f"ytsearch10:{last_track.title}", download=False)
        for entry in (s_info or {}).get("entries") or []:
            if not entry:
                continue
            cand_id = entry.get("id")
            cand_title = entry.get("title") or ""
            cand_dur = entry.get("duration")
            if (
                not cand_id
                or cand_id in history_set
                or cand_title.lower() in history_set
                or cand_title.lower() == last_track.title.lower()
                or is_unwanted_audio_variant(cand_title, user_query="", candidate_duration=cand_dur, strict_duration=False)
            ):
                continue
            candidate = Track(
                title=cand_title,
                webpage_url=f"https://www.youtube.com/watch?v={cand_id}",
                video_id=str(cand_id),
                duration=cand_dur,
                thumbnail=f"https://i.ytimg.com/vi/{cand_id}/hqdefault.jpg",
                requester=bot_user,
                is_autoplay=True,
                based_on_title=last_track.title,
            )
            return download_track_to_tmp(candidate)
    except Exception as exc:
        logger.debug(f"Fallo en búsqueda YouTube para recomendados: {exc}")

    return None


class MusicCog(commands.Cog, name="Música"):
    """Comandos y lógica principal de reproducción musical para ElCompitaMusica."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.states: Dict[int, GuildMusicState] = {}

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState()
        return self.states[guild_id]

    async def _update_now_playing_status(
        self,
        guild: discord.Guild,
        track: Optional[Track],
    ) -> None:
        """
        Actualiza el estado del bot y el estado del canal de voz ('Establece un estado de canal')
        para que todos en el servidor vean qué canción está sonando y quién la pidió.
        """
        try:
            if track:
                requester_name = getattr(track.requester, "display_name", track.requester.name)
                activity_text = f"🎶 {track.title[:80]} (por {requester_name})"
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.listening,
                        name=activity_text[:128],
                    )
                )
            else:
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.listening,
                        name="!play | Música + Autoplay",
                    )
                )
        except Exception as exc:
            logger.debug(f"No se pudo actualizar presence: {exc}")

        voice_client: Optional[discord.VoiceClient] = guild.voice_client
        if voice_client and voice_client.channel:
            try:
                if track:
                    requester_name = getattr(track.requester, "display_name", track.requester.name)
                    status_str = f"🎶 {track.title[:70]} — pedido por {requester_name}"
                else:
                    status_str = ""
                route = discord.http.Route(
                    "PUT",
                    "/channels/{channel_id}/voice-status",
                    channel_id=voice_client.channel.id,
                )
                await self.bot.http.request(route, json={"status": status_str[:480]})
            except Exception as exc:
                logger.debug(f"No se pudo actualizar el voice-status del canal: {exc}")

    async def _announce_track(
        self,
        guild: discord.Guild,
        state: GuildMusicState,
        track: Track,
        status_msg: Optional[discord.Message] = None,
    ) -> None:
        """Envía el cartel de 'Reproduciendo ahora' al canal de comandos y al chat del canal de voz."""
        if track.is_autoplay:
            embed = discord.Embed(
                title="✨ Recomendado automático (Autoplay)",
                description=f"**[{track.title}]({track.webpage_url})**",
                color=discord.Color.purple(),
            )
            embed.add_field(name="⏱️ Duración", value=track.formatted_duration, inline=True)
            if track.based_on_title:
                embed.add_field(
                    name="📻 Basado en",
                    value=track.based_on_title[:60],
                    inline=True,
                )
            embed.set_footer(text="Usa !autoplay para activar/desactivar las recomendaciones automáticas.")
        else:
            embed = discord.Embed(
                title="🎶 Reproduciendo ahora",
                description=f"**[{track.title}]({track.webpage_url})**",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="⏱️ Duración", value=track.formatted_duration, inline=True)
            embed.add_field(name="🙋 Pedido por", value=track.requester.mention, inline=True)
            embed.set_footer(text="Comandos: !skip (!s) | !queue (!q) | !pause | !stop")

        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)

        sent_channel_id: Optional[int] = None

        if status_msg is not None:
            try:
                await status_msg.edit(content=None, embed=embed)
                sent_channel_id = status_msg.channel.id
            except Exception:
                status_msg = None

        if status_msg is None and state.text_channel:
            try:
                msg = await state.text_channel.send(embed=embed)
                sent_channel_id = msg.channel.id
            except Exception as exc:
                logger.warning(f"No se pudo enviar embed al canal de texto: {exc}")

        voice_client: Optional[discord.VoiceClient] = guild.voice_client
        if (
            voice_client
            and voice_client.channel
            and isinstance(voice_client.channel, discord.abc.Messageable)
            and voice_client.channel.id != sent_channel_id
        ):
            try:
                await voice_client.channel.send(embed=embed)
            except Exception:
                pass

    async def _prefetch_next_in_queue(self, guild: discord.Guild) -> None:
        """Pre-descarga en segundo plano la siguiente canción de la cola mientras suena la actual."""
        await asyncio.sleep(2)
        state = self.get_state(guild.id)
        if not state.queue:
            return
        next_up = state.queue[0]
        if next_up.local_file and os.path.isfile(next_up.local_file):
            return
        try:
            await self.bot.loop.run_in_executor(None, download_track_to_tmp, next_up)
        except Exception as exc:
            logger.debug(f"No se pudo pre-cargar '{next_up.title}': {exc}")

    async def _on_track_finished(
        self,
        guild: discord.Guild,
        finished_track: Track,
        play_error: Optional[Exception],
    ) -> None:
        """
        Espera 250ms a que el hilo AudioPlayer anterior cierre su bloque finally (speak(False))
        antes de iniciar la siguiente canción, evitando que silencie a la nueva pista.
        """
        await asyncio.sleep(0.25)
        await self.play_next(guild, finished_track=finished_track, error=play_error)

    async def play_next(
        self,
        guild: discord.Guild,
        finished_track: Optional[Track] = None,
        error: Optional[Exception] = None,
        target_voice_channel: Optional[discord.abc.Connectable] = None,
        status_msg: Optional[discord.Message] = None,
    ) -> None:
        """Descarga la pista, asegura la conexión de voz e inicia la reproducción inmediatamente."""
        if finished_track:
            cleanup_track_file(finished_track)

        if error:
            logger.error(f"Error en el reproductor de {guild.name} ({guild.id}): {error}")

        state = self.get_state(guild.id)

        async with state.play_lock:
            voice_client: Optional[discord.VoiceClient] = guild.voice_client
            if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
                return

            next_track: Optional[Track] = None

            if state.queue:
                next_track = state.queue.popleft()
            elif state.autoplay and state.last_track and self.bot.user:
                seed_track = state.last_track
                history_snapshot = list(state.history_ids)
                try:
                    next_track = await self.bot.loop.run_in_executor(
                        None,
                        fetch_recommended_track,
                        seed_track,
                        history_snapshot,
                        self.bot.user,
                    )
                except Exception as exc:
                    logger.error(f"Error buscando recomendación automática: {exc}")
                    next_track = None

            if not next_track:
                state.current = None
                await self._update_now_playing_status(guild, None)
                return

            # 1. Descargar la pista a /tmp ANTES de abrir o usar la sesión de voz
            try:
                next_track = await self.bot.loop.run_in_executor(
                    None, download_track_to_tmp, next_track
                )
            except Exception as exc:
                logger.warning(f"No se pudo cargar '{next_track.title}': {exc}")
                if state.text_channel:
                    try:
                        await state.text_channel.send(
                            embed=discord.Embed(
                                description=f"⚠️ No se pudo reproducir **{next_track.title}**. Saltando a la siguiente...",
                                color=discord.Color.red(),
                            )
                        )
                    except Exception:
                        pass
                self.bot.loop.create_task(self.play_next(guild, target_voice_channel=target_voice_channel))
                return

            # 2. Conectar al canal de voz usando ResilientVoiceClient
            voice_client = guild.voice_client
            if voice_client is None or not voice_client.is_connected():
                if target_voice_channel is not None:
                    try:
                        if voice_client is not None:
                            await voice_client.disconnect(force=True)
                        voice_client = await target_voice_channel.connect(
                            timeout=15.0,
                            reconnect=True,
                            cls=ResilientVoiceClient,
                            self_deaf=False,
                            self_mute=False,
                        )
                        # Dar hasta 2.0s al event loop para completar el handshake MLS DAVE E2EE antes de transmitir RTP
                        for _ in range(20):
                            conn = getattr(voice_client, "_connection", None)
                            if conn and (conn.dave_protocol_version == 0 or conn.can_encrypt):
                                break
                            await asyncio.sleep(0.1)
                    except Exception as exc:
                        logger.error(f"No se pudo conectar al canal de voz: {exc}")
                        cleanup_track_file(next_track)
                        return
                else:
                    state.clear()
                    return
            elif target_voice_channel is not None and voice_client.channel != target_voice_channel:
                await voice_client.move_to(target_voice_channel)
                for _ in range(15):
                    conn = getattr(voice_client, "_connection", None)
                    if conn and (conn.dave_protocol_version == 0 or conn.can_encrypt):
                        break
                    await asyncio.sleep(0.1)

            state.register_played(next_track)
            ensure_opus_loaded()

            # 3. Inicializar FFmpegPCMAudio + PCMVolumeTransformer
            try:
                if next_track.local_file and os.path.isfile(next_track.local_file):
                    logger.info(f"▶️ Reproduciendo archivo local: {next_track.local_file} ({next_track.title})")
                    raw_source = discord.FFmpegPCMAudio(
                        next_track.local_file,
                        **FFMPEG_LOCAL_OPTIONS,
                    )
                elif next_track.stream_url:
                    logger.info(f"▶️ Reproduciendo stream directo: {next_track.title}")
                    raw_source = discord.FFmpegPCMAudio(
                        next_track.stream_url,
                        **FFMPEG_STREAM_OPTIONS,
                    )
                else:
                    raise ValueError("No se encontró fuente de audio reproducible.")
                audio_source = discord.PCMVolumeTransformer(raw_source, volume=1.0)
            except Exception as exc:
                logger.exception("Fallo al inicializar FFmpegPCMAudio:")
                cleanup_track_file(next_track)
                self.bot.loop.create_task(self.play_next(guild, target_voice_channel=target_voice_channel))
                return

            # 4. Callback no bloqueante
            def after_playback(play_error: Optional[Exception]) -> None:
                self.bot.loop.call_soon_threadsafe(
                    lambda: self.bot.loop.create_task(
                        self._on_track_finished(guild, next_track, play_error)
                    )
                )

            voice_client.play(audio_source, after=after_playback)

            # Garantizar que el estado speaking(voice) esté activo en el WebSocket de voz
            try:
                await asyncio.sleep(0.15)
                if voice_client.ws is not MISSING and voice_client.is_playing():
                    await voice_client.ws.speak(SpeakingState.voice)
            except Exception:
                pass

            # 5. Pre-descargar la siguiente canción y anunciar en Discord
            state.cancel_prefetch_task()
            if state.queue:
                state.prefetch_task = self.bot.loop.create_task(self._prefetch_next_in_queue(guild))

            await self._announce_track(guild, state, next_track, status_msg=status_msg)
            await self._update_now_playing_status(guild, next_track)

    @commands.hybrid_command(
        name="play",
        aliases=["p"],
        description="Reproduce una canción o playlist completa de YouTube, YouTube Music o SoundCloud.",
    )
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send(
                embed=discord.Embed(
                    description="❌ Debes estar conectado a un canal de voz para usar este comando.",
                    color=discord.Color.red(),
                )
            )
            return

        target_channel = ctx.author.voice.channel
        state = self.get_state(ctx.guild.id)
        state.text_channel = ctx.channel

        loading_embed = discord.Embed(
            description=f"🔎 Buscando y preparando **{query[:100]}** — pedido por {ctx.author.mention}...",
            color=discord.Color.blurple(),
        )
        status_msg = await ctx.send(embed=loading_embed)

        try:
            tracks = await self.bot.loop.run_in_executor(
                None, extract_tracks, query, ctx.author
            )
        except Exception as exc:
            err_embed = discord.Embed(
                title="❌ Error al buscar el audio",
                description=f"No se pudo procesar `{query}`.\nDetalle: `{exc}`",
                color=discord.Color.red(),
            )
            await status_msg.edit(embed=err_embed)
            return

        voice_client: Optional[discord.VoiceClient] = ctx.guild.voice_client
        was_playing = bool(
            voice_client
            and voice_client.is_connected()
            and (voice_client.is_playing() or voice_client.is_paused())
        ) or (state.play_lock.locked())

        for t in tracks:
            state.queue.append(t)

        if len(tracks) > 1:
            embed = discord.Embed(
                title="📚 Playlist agregada a la cola",
                description=f"Se agregaron **{len(tracks)} canciones** a la cola de reproducción.",
                color=discord.Color.green(),
            )
            embed.add_field(name="🎵 Primera pista", value=tracks[0].title, inline=False)
            embed.add_field(name="🙋 Pedido por", value=ctx.author.mention, inline=True)
            embed.add_field(name="📦 Total en cola", value=f"{len(state.queue)} canciones", inline=True)
            if tracks[0].thumbnail:
                embed.set_thumbnail(url=tracks[0].thumbnail)
            await status_msg.edit(embed=embed)

            if not was_playing:
                await self.play_next(ctx.guild, target_voice_channel=target_channel)
            elif len(state.queue) == len(tracks):
                state.cancel_prefetch_task()
                state.prefetch_task = self.bot.loop.create_task(self._prefetch_next_in_queue(ctx.guild))
        else:
            track = tracks[0]
            if was_playing:
                embed = discord.Embed(
                    title="📥 Agregada a la cola",
                    description=f"**[{track.title}]({track.webpage_url})**",
                    color=discord.Color.green(),
                )
                embed.add_field(name="⏱️ Duración", value=track.formatted_duration, inline=True)
                embed.add_field(name="📍 Posición en cola", value=f"#{len(state.queue)}", inline=True)
                embed.add_field(name="🙋 Pedido por", value=track.requester.mention, inline=True)
                if track.thumbnail:
                    embed.set_thumbnail(url=track.thumbnail)
                await status_msg.edit(embed=embed)

                if len(state.queue) == 1:
                    state.cancel_prefetch_task()
                    state.prefetch_task = self.bot.loop.create_task(self._prefetch_next_in_queue(ctx.guild))
            else:
                await self.play_next(
                    ctx.guild,
                    target_voice_channel=target_channel,
                    status_msg=status_msg,
                )

    @commands.hybrid_command(
        name="autoplay",
        aliases=["ap"],
        description="Activa o desactiva la reproducción automática de recomendados al terminar la cola.",
    )
    async def autoplay(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        state = self.get_state(ctx.guild.id)
        state.autoplay = not state.autoplay
        status = "✅ **Activado**" if state.autoplay else "⛔ **Desactivado**"
        color = discord.Color.green() if state.autoplay else discord.Color.orange()
        await ctx.send(
            embed=discord.Embed(
                description=f"📻 El modo **Autoplay (Recomendados automáticos)** ahora está: {status}",
                color=color,
            )
        )

    @commands.hybrid_command(
        name="skip",
        aliases=["s"],
        description="Salta la canción que está sonando actualmente.",
    )
    async def skip(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        voice_client: Optional[discord.VoiceClient] = ctx.guild.voice_client
        if not voice_client or (not voice_client.is_playing() and not voice_client.is_paused()):
            await ctx.send(
                embed=discord.Embed(
                    description="❌ No hay ninguna canción reproduciéndose en este momento.",
                    color=discord.Color.red(),
                )
            )
            return

        state = self.get_state(ctx.guild.id)
        skipped_title = state.current.title if state.current else "Canción actual"
        voice_client.stop()
        await ctx.send(
            embed=discord.Embed(
                description=f"⏭️ Se saltó **{skipped_title}**.",
                color=discord.Color.orange(),
            )
        )

    @commands.hybrid_command(
        name="pause",
        description="Pausa la reproducción actual.",
    )
    async def pause(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        voice_client: Optional[discord.VoiceClient] = ctx.guild.voice_client
        if not voice_client or not voice_client.is_playing():
            await ctx.send(
                embed=discord.Embed(
                    description="❌ No hay música reproduciéndose para pausar.",
                    color=discord.Color.red(),
                )
            )
            return

        voice_client.pause()
        await ctx.send(
            embed=discord.Embed(
                description="⏸️ Reproducción pausada. Usa `!resume` para continuar.",
                color=discord.Color.gold(),
            )
        )

    @commands.hybrid_command(
        name="resume",
        description="Reanuda la reproducción pausada.",
    )
    async def resume(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        voice_client: Optional[discord.VoiceClient] = ctx.guild.voice_client
        if not voice_client or not voice_client.is_paused():
            await ctx.send(
                embed=discord.Embed(
                    description="❌ La reproducción no está pausada.",
                    color=discord.Color.red(),
                )
            )
            return

        voice_client.resume()
        await ctx.send(
            embed=discord.Embed(
                description="▶️ Reproducción reanudada.",
                color=discord.Color.green(),
            )
        )

    @commands.hybrid_command(
        name="queue",
        aliases=["q"],
        description="Muestra las próximas 10 canciones en la cola de reproducción.",
    )
    async def queue(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        state = self.get_state(ctx.guild.id)

        if not state.current and not state.queue:
            await ctx.send(
                embed=discord.Embed(
                    description="📭 La cola de reproducción está vacía.",
                    color=discord.Color.light_grey(),
                )
            )
            return

        embed = discord.Embed(
            title=f"📜 Cola de reproducción — {ctx.guild.name}",
            color=discord.Color.blurple(),
        )

        if state.current:
            embed.add_field(
                name="🔊 Sonando ahora",
                value=(
                    f"**[{state.current.title}]({state.current.webpage_url})** "
                    f"(`{state.current.formatted_duration}`) — {state.current.requester.mention}"
                ),
                inline=False,
            )

        if state.queue:
            upcoming = list(state.queue)[:10]
            lines = [
                f"`{idx}.` **[{t.title}]({t.webpage_url})** (`{t.formatted_duration}`) — {t.requester.mention}"
                for idx, t in enumerate(upcoming, start=1)
            ]
            remaining = len(state.queue) - len(upcoming)
            if remaining > 0:
                lines.append(f"\n*...y **{remaining}** canciones más en la cola.*")

            embed.add_field(
                name=f"⏳ Próximas canciones ({len(state.queue)} en total)",
                value="\n".join(lines),
                inline=False,
            )
        else:
            autoplay_label = "Activado ✅" if state.autoplay else "Desactivado ⛔"
            embed.add_field(
                name="⏳ Próximas canciones",
                value=f"*No hay más canciones en cola. Autoplay: **{autoplay_label}**.*",
                inline=False,
            )

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="stop",
        description="Detiene la reproducción y limpia toda la cola.",
    )
    async def stop(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        voice_client: Optional[discord.VoiceClient] = ctx.guild.voice_client
        state = self.get_state(ctx.guild.id)

        state.clear()
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()
        await self._update_now_playing_status(ctx.guild, None)

        await ctx.send(
            embed=discord.Embed(
                description="⏹️ Reproducción detenida y cola de canciones vaciada.",
                color=discord.Color.red(),
            )
        )

    @commands.hybrid_command(
        name="leave",
        aliases=["dc"],
        description="Desconecta al bot del canal de voz.",
    )
    async def leave(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        voice_client: Optional[discord.VoiceClient] = ctx.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            await ctx.send(
                embed=discord.Embed(
                    description="❌ No estoy conectado a ningún canal de voz.",
                    color=discord.Color.red(),
                )
            )
            return

        state = self.get_state(ctx.guild.id)
        state.clear()
        await self._update_now_playing_status(ctx.guild, None)
        await voice_client.disconnect()

        await ctx.send(
            embed=discord.Embed(
                description="👋 Desconectado del canal de voz. ¡Hasta la próxima, compita!",
                color=discord.Color.blurple(),
            )
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        guild = member.guild
        voice_client: Optional[discord.VoiceClient] = guild.voice_client
        if not voice_client or not voice_client.channel:
            return

        state = self.get_state(guild.id)

        if self.bot.user and member.id == self.bot.user.id and after.channel is None:
            state.clear()
            return

        non_bot_members = [m for m in voice_client.channel.members if not m.bot]

        if len(non_bot_members) == 0:
            if state.inactivity_task is None or state.inactivity_task.done():
                state.inactivity_task = self.bot.loop.create_task(
                    self._disconnect_after_timeout(guild, voice_client.channel)
                )
        else:
            state.cancel_inactivity_task()

    async def _disconnect_after_timeout(
        self,
        guild: discord.Guild,
        channel: discord.abc.Connectable,
    ) -> None:
        try:
            await asyncio.sleep(180)
            voice_client: Optional[discord.VoiceClient] = guild.voice_client
            if not voice_client or not voice_client.is_connected():
                return

            non_bot_members = [m for m in voice_client.channel.members if not m.bot]
            if len(non_bot_members) == 0:
                state = self.get_state(guild.id)
                text_channel = state.text_channel
                state.clear()
                await self._update_now_playing_status(guild, None)
                await voice_client.disconnect()
                if text_channel:
                    await text_channel.send(
                        embed=discord.Embed(
                            description="🔇 Me desconecté automáticamente tras **3 minutos** de estar solo en el canal de voz.",
                            color=discord.Color.light_grey(),
                        )
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"Error en tarea de desconexión por inactividad: {exc}")


class ElCompitaBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=commands.DefaultHelpCommand(no_category="Comandos Generales"),
        )

    async def setup_hook(self) -> None:
        ensure_opus_loaded()
        try:
            import davey  # type: ignore
            from importlib.metadata import version as _pkg_version

            davey_ver = getattr(davey, "__version__", None) or _pkg_version("davey")
        except Exception:
            davey_ver = "NO instalado"
        logger.info(
            f"📦 discord.py={discord.__version__} | yt-dlp={yt_dlp.version.__version__} | "
            f"opus={discord.opus.is_loaded()} | davey={davey_ver}"
        )
        await self.add_cog(MusicCog(self))
        if os.getenv("SYNC_COMMANDS", "0") == "1":
            try:
                synced = await self.tree.sync()
                logger.info(f"Se sincronizaron {len(synced)} comandos híbridos/slash.")
            except Exception as exc:
                logger.warning(f"No se pudieron sincronizar los slash commands globalmente: {exc}")

    async def on_ready(self) -> None:
        if self.user:
            logger.info(f"🤖 {self.user} (ID: {self.user.id}) está en línea y listo.")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="!play | Música + Autoplay",
            )
        )

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                embed=discord.Embed(
                    description=f"⚠️ Falta un argumento requerido: `{error.param.name}`.\nEjemplo: `!play <nombre o URL de canción/playlist>`",
                    color=discord.Color.orange(),
                )
            )
            return

        original = getattr(error, "original", error)
        logger.error(f"Error ejecutando comando {ctx.command}: {original}", exc_info=original)
        await ctx.send(
            embed=discord.Embed(
                title="⚠️ Ocurrió un error inesperado",
                description=f"`{original}`",
                color=discord.Color.red(),
            )
        )


async def _handle_http_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        req_bytes = await reader.read(1024)
        req_line = req_bytes.decode("utf-8", errors="ignore").splitlines()[0] if req_bytes else ""
        if "GET /logs" in req_line:
            log_text = "\n".join(RECENT_LOGS)
            body = log_text.encode("utf-8", errors="replace")
        else:
            body = b"OK - ElCompitaMusica is running (v2.8 POT+ResilientVoice)"
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + body
        )
        writer.write(response)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def start_healthcheck_server() -> None:
    """Mini servidor HTTP con auto-ping cada 10 min para mantener activo Render Free 24/7."""
    port = int(os.getenv("PORT", "8080"))
    try:
        await asyncio.start_server(_handle_http_client, "0.0.0.0", port)
        logger.info(f"🌐 Servidor Health-Check escuchando en el puerto {port} (compatible con Render Free).")
    except Exception as exc:
        logger.warning(f"No se pudo iniciar el servidor health-check en el puerto {port}: {exc}")
        return

    external_url = os.getenv("RENDER_EXTERNAL_URL", "https://elcompitamusica.onrender.com")
    while True:
        await asyncio.sleep(600)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, urllib.request.urlopen, external_url
            )
        except Exception:
            pass


async def async_main() -> None:
    if not DISCORD_TOKEN or DISCORD_TOKEN == "PEGA_TU_TOKEN_AQUI":
        raise RuntimeError(
            "No se encontró un DISCORD_TOKEN válido en el archivo .env. "
            "Por favor, configura DISCORD_TOKEN en .env antes de iniciar el bot."
        )

    start_bgutil_pot_server()
    asyncio.create_task(start_healthcheck_server())

    backoff = 45
    while True:
        bot = ElCompitaBot()
        try:
            async with bot:
                await bot.start(DISCORD_TOKEN, reconnect=True)
        except discord.errors.HTTPException as exc:
            if exc.status == 429:
                retry_after = 0.0
                if exc.response and exc.response.headers:
                    try:
                        retry_after = float(exc.response.headers.get("Retry-After", "0"))
                    except Exception:
                        retry_after = 0.0
                wait_s = max(int(retry_after) + 5, backoff)
                logger.warning(
                    f"⏳ IP compartida de Render limitada temporalmente por Discord (HTTP 429). "
                    f"Contenedor activo en puerto web; reintentando login en {wait_s}s..."
                )
                await asyncio.sleep(wait_s)
                backoff = min(backoff + 45, 300)
                continue
            logger.error(f"HTTPException en Discord ({exc.status}): {exc}. Reintentando en {backoff}s...")
            await asyncio.sleep(backoff)
        except Exception as exc:
            logger.error(f"Error en sesión de Discord ({exc}). Reintentando en {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff + 30, 180)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
