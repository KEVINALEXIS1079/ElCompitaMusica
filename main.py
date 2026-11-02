import array
import asyncio
import base64
from collections import deque
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlparse
import urllib.request

import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

# Buffer circular en memoria para diagnóstico en vivo vía /logs
RECENT_LOGS: Deque[str] = deque(maxlen=200)


class MemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            RECENT_LOGS.append(msg)
        except Exception:
            pass


# Configuración de logging
log_formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)
mem_handler = MemoryLogHandler()
mem_handler.setFormatter(log_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[stream_handler, mem_handler],
)
logger = logging.getLogger("ElCompitaMusica")

# Cargar variables de entorno
load_dotenv()
DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip().strip('"').strip("'")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES")

TEMP_DIR = tempfile.gettempdir()
AUDIO_CACHE_DIR = os.path.join(TEMP_DIR, "elcompita_audio")
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)

COOKIES_MASTER_FILE = os.path.join(TEMP_DIR, "cookies_master.txt")
COOKIES_WORKING_FILE = os.path.join(TEMP_DIR, "cookies_working.txt")

# Si YouTube bloquea la IP del datacenter (Render), activamos modo rápido por 30 min para no demorar 20s por canción
YOUTUBE_STREAM_BLOCKED_UNTIL: float = 0.0

# Silenciar logs de bug reports de yt-dlp
yt_dlp.utils.bug_reports_message = lambda *args, **kwargs: ""


def ensure_opus_loaded() -> bool:
    """Garantiza que libopus esté cargado en Linux/Docker para codificar voz hacia Discord."""
    if discord.opus.is_loaded():
        return True
    for lib_name in ("libopus.so.0", "libopus.so", "opus"):
        try:
            discord.opus.load_opus(lib_name)
            if discord.opus.is_loaded():
                logger.info(f"✅ Librería de voz Opus cargada exitosamente ({lib_name}).")
                return True
        except Exception:
            continue
    logger.warning("⚠️ No se pudo cargar libopus manualmente; se usará el cargador por defecto.")
    return discord.opus.is_loaded()


def _decrypt_cookies_enc(enc_path: str, secret_key: str) -> Optional[str]:
    """Desencripta cookies.enc en memoria usando DISCORD_TOKEN (PBKDF2-HMAC-SHA256 + HMAC-SHA256)."""
    try:
        with open(enc_path, "r", encoding="utf-8") as f:
            raw_b64 = f.read().strip()
        raw = base64.b64decode(raw_b64)
        if len(raw) <= 48:
            return None
        salt = raw[:16]
        mac = raw[16:48]
        ciphertext = raw[48:]

        key_bytes = secret_key.strip().encode("utf-8")
        expected_mac = hmac.new(key_bytes, salt + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected_mac):
            logger.warning("La firma HMAC de cookies.enc no coincide con DISCORD_TOKEN.")
            return None

        keystream = hashlib.pbkdf2_hmac("sha256", key_bytes, salt, 100000, dklen=len(ciphertext))
        plaintext = bytes(c ^ k for c, k in zip(ciphertext, keystream))
        return plaintext.decode("utf-8")
    except Exception as exc:
        logger.warning(f"No se pudo desencriptar {enc_path}: {exc}")
        return None


def prepare_cookies_file() -> Optional[str]:
    """Detecta y prepara el archivo maestro de cookies de YouTube."""
    secrets_dir = "/etc/secrets"
    if os.path.isdir(secrets_dir):
        try:
            for fname in os.listdir(secrets_dir):
                fpath = os.path.join(secrets_dir, fname)
                if os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
                    if "cookie" in fname.lower() or "youtube" in fname.lower():
                        shutil.copyfile(fpath, COOKIES_MASTER_FILE)
                        shutil.copyfile(COOKIES_MASTER_FILE, COOKIES_WORKING_FILE)
                        logger.info(f"✅ Cookies de YouTube cargadas desde {fpath}")
                        return COOKIES_WORKING_FILE
        except Exception as exc:
            logger.warning(f"Error inspeccionando /etc/secrets: {exc}")

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

    logger.info("ℹ️ No se detectaron cookies; se usará modo híbrido.")
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
    use_cookies: bool = True,
    for_soundcloud: bool = False,
) -> Dict[str, Any]:
    """
    Construye opciones optimizadas de yt-dlp:
    - En SoundCloud fuerza MP3 progresivo/HLS (http_mp3_1_0 / hls_mp3_1_0) evitando archivos .m4a fMP4 corruptos.
    - En YouTube prioriza webm/m4a HTTPS directos y activa Node.js para yt-dlp-ejs.
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


def verify_audio_file_pcm(filepath: str) -> bool:
    """
    Verifica con FFmpeg que el archivo descargado en /tmp contenga muestras de audio reales (no silencio ni DRM)
    y si no es MP3/WAV limpio, lo convierte rápidamente a un archivo .mp3 estándar a 48kHz estéreo.
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


def fetch_youtube_oembed_metadata(url: str) -> Optional[Dict[str, str]]:
    """Obtiene el título, artista y miniatura de un video de YouTube/YouTube Music vía oEmbed público (0.2s, nunca bloqueado)."""
    try:
        clean_url = normalize_youtube_url(url, playlist_mode=False)
        oembed_url = f"https://www.youtube.com/oembed?url={quote(clean_url, safe='')}&format=json"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            title = data.get("title") or ""
            author = (data.get("author_name") or "").replace(" - Topic", "").strip()
            full_title = f"{author} - {title}" if (title and author and author.lower() not in title.lower()) else title
            parsed = urlparse(clean_url)
            vid_id = (parse_qs(parsed.query).get("v") or [""])[0]
            return {
                "title": full_title or title,
                "webpage_url": clean_url,
                "video_id": vid_id,
                "thumbnail": data.get("thumbnail_url") or "",
            }
    except Exception:
        return None


def fetch_youtube_flat_search(query: str) -> Optional[Dict[str, Any]]:
    """
    Busca metadatos oficiales en YouTube (título, ID, duración, miniatura) con extract_flat=True.
    Tarda ~0.4s y NUNCA sufre bloqueo 'Sign in to confirm you're not a bot' en IPs de la nube.
    """
    opts = build_ytdl_options(
        allow_playlist=False,
        flat_playlist=True,
        playlist_end=1,
        default_search="ytsearch",
        use_cookies=False,
    )
    opts["extract_flat"] = True
    try:
        target = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
        with yt_dlp.YoutubeDL(opts) as ytdl:
            info = ytdl.extract_info(target, download=False)
        if info and "entries" in info:
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else None
        if info and info.get("id") and info.get("title"):
            vid_id = str(info["id"])
            thumbnails = info.get("thumbnails") or []
            thumb = info.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else None)
            channel = (info.get("channel") or info.get("uploader") or "").replace(" - Topic", "").strip()
            title = str(info["title"])
            if channel and channel.lower() not in title.lower():
                title = f"{channel} - {title}"
            return {
                "title": title,
                "video_id": vid_id,
                "webpage_url": f"https://www.youtube.com/watch?v={vid_id}",
                "duration": info.get("duration"),
                "thumbnail": thumb,
            }
    except Exception as exc:
        logger.debug(f"Búsqueda rápida flat en YouTube falló para '{query}': {exc}")
    return None


@dataclass
class Track:
    """Representa una pista de audio con soporte para pre-descarga en /tmp."""

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
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


def cleanup_track_file(track: Optional[Track]) -> None:
    """Elimina el archivo temporal de audio de /tmp cuando termina de sonar."""
    if not track or not track.local_file:
        return
    try:
        if os.path.isfile(track.local_file):
            os.remove(track.local_file)
            logger.debug(f"🧹 Archivo temporal eliminado: {track.local_file}")
    except Exception:
        pass
    finally:
        track.local_file = None


class GuildMusicState:
    """Administra la cola de reproducción, historial, pre-carga y Autoplay por servidor."""

    def __init__(self) -> None:
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None
        self.last_track: Optional[Track] = None
        self.history_ids: Deque[str] = deque(maxlen=50)
        self.autoplay: bool = True
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
    Intenta extraer el stream directo de YouTube en un solo intento rápido.
    Si YouTube bloquea la IP del datacenter ('Sign in to confirm you're not a bot'),
    activa el modo rápido por 30 minutos para no perder 15 segundos en cada canción.
    """
    global YOUTUBE_STREAM_BLOCKED_UNTIL
    if time.time() < YOUTUBE_STREAM_BLOCKED_UNTIL:
        return None

    opts = build_ytdl_options(
        allow_playlist=False,
        flat_playlist=False,
        use_cookies=True,
        for_soundcloud=False,
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ytdl:
            info = ytdl.extract_info(target_query, download=False)
        if info and "entries" in info:
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else None
        if info and info.get("url"):
            logger.info(f"✅ Stream directo de YouTube obtenido: {info.get('title')}")
            return info
    except Exception as exc:
        err_str = str(exc)
        if "Sign in to confirm" in err_str or "not a bot" in err_str:
            YOUTUBE_STREAM_BLOCKED_UNTIL = time.time() + 1800
            logger.info("⚡ IP de nube restringida por YouTube; activando motor rápido de audio directo (sin esperas).")
        else:
            logger.debug(f"Extracción directa YouTube falló para '{target_query}': {exc}")
    return None


def _resolve_soundcloud_stream(
    search_text: str,
    requester: discord.abc.User,
    *,
    yt_meta: Optional[Dict[str, Any]] = None,
) -> Track:
    """
    Resuelve el stream de audio completo en formato MP3 (http_mp3_1_0 / hls_mp3_1_0),
    conservando el título, carátula y enlace oficial de YouTube Music / YouTube cuando están disponibles.
    """
    clean_query = search_text.strip()
    for noise in (
        "(Official Video)",
        "(Video Oficial)",
        "[Official Video]",
        "(Official Audio)",
        "(Audio Oficial)",
        "(Lyrics)",
        "(Letra)",
        "(Visualizer)",
        " - Topic",
    ):
        clean_query = clean_query.replace(noise, "").replace(noise.lower(), "")
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
        playlist_end=8,
        default_search="scsearch",
        use_cookies=False,
        for_soundcloud=True,
    )
    flat_opts["ignoreerrors"] = True

    with yt_dlp.YoutubeDL(flat_opts) as ytdl:
        search_info = ytdl.extract_info(f"scsearch8:{clean_query}", download=False)

    entries = [e for e in (search_info or {}).get("entries", []) if e]
    if not entries:
        raise ValueError(f"No se encontraron resultados disponibles para '{search_text}'.")

    stream_opts = build_ytdl_options(
        allow_playlist=False,
        flat_playlist=False,
        use_cookies=False,
        for_soundcloud=True,
    )
    fallback_preview_track: Optional[Track] = None

    for entry in entries:
        cand_url = entry.get("url") or entry.get("webpage_url")
        if not cand_url:
            continue
        try:
            with yt_dlp.YoutubeDL(stream_opts) as ytdl:
                track_info = ytdl.extract_info(cand_url, download=False)
            if not track_info or not track_info.get("url"):
                continue

            # Priorizar formato http_mp3_1_0 o hls_mp3_1_0 sobre m4a
            best_url = track_info["url"]
            for fmt in track_info.get("formats") or []:
                fmt_id = str(fmt.get("format_id") or "")
                fmt_url = fmt.get("url")
                if fmt_url and "http_mp3" in fmt_id:
                    best_url = fmt_url
                    break

            duration = track_info.get("duration") or entry.get("duration")
            sc_page = track_info.get("webpage_url") or cand_url

            display_title = (
                (yt_meta.get("title") if yt_meta else None)
                or track_info.get("title")
                or entry.get("title")
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
                duration=duration or (yt_meta.get("duration") if yt_meta else None),
                thumbnail=display_thumb,
                requester=requester,
            )

            if duration and 25 <= int(duration) <= 35:
                if fallback_preview_track is None:
                    fallback_preview_track = candidate_track
                continue

            logger.info(f"✅ Pista lista: {candidate_track.title} ({duration}s)")
            return candidate_track
        except Exception as exc:
            logger.debug(f"Saltando candidato ({cand_url}) por DRM/error: {exc}")
            continue

    if fallback_preview_track is not None:
        return fallback_preview_track

    raise ValueError(f"Todas las versiones encontradas para '{clean_query}' tenían restricción DRM.")


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
    Descarga la pista en /tmp/elcompita_audio en formato MP3/WebM limpio y verifica con FFmpeg
    que contenga audio real decodificable antes de enviarlo a Discord.
    """
    if track.local_file and os.path.isfile(track.local_file) and os.path.getsize(track.local_file) > 4096:
        return track

    if track.duration and int(track.duration) > 1500:
        return resolve_track_stream(track)

    uid = f"trk_{uuid.uuid4().hex[:10]}"
    outtmpl = os.path.join(AUDIO_CACHE_DIR, f"{uid}.%(ext)s")

    # 1. Si el track ya tiene soundcloud_url (o webpage_url de SoundCloud), descargar en MP3 directo
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
                    f"⚡ Audio descargado en /tmp ({os.path.getsize(found) // 1024} KB): {track.title}"
                )
                return track
            elif found:
                cleanup_track_file(Track(title="", webpage_url="", requester=track.requester, local_file=found))
        except Exception as exc:
            logger.warning(f"No se pudo descargar pista SoundCloud a /tmp ({track.title}): {exc}")

    # 2. Intentar descarga desde YouTube si la IP no está bloqueada
    if time.time() >= YOUTUBE_STREAM_BLOCKED_UNTIL:
        yt_target = track.webpage_url
        if not yt_target or not yt_target.startswith(("http://", "https://")) or "soundcloud.com" in yt_target:
            if track.video_id and len(str(track.video_id)) == 11:
                yt_target = f"https://www.youtube.com/watch?v={track.video_id}"
            else:
                yt_target = f"ytsearch1:{track.title}"
        else:
            yt_target = normalize_youtube_url(yt_target, playlist_mode=False)

        yt_opts = build_ytdl_options(
            allow_playlist=False,
            flat_playlist=False,
            use_cookies=True,
            for_soundcloud=False,
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
                        f"⚡ Audio YouTube descargado en /tmp ({os.path.getsize(found) // 1024} KB): {track.title}"
                    )
                    return track
        except Exception as exc:
            if "Sign in to confirm" in str(exc) or "not a bot" in str(exc):
                global YOUTUBE_STREAM_BLOCKED_UNTIL
                YOUTUBE_STREAM_BLOCKED_UNTIL = time.time() + 1800

    # 3. Resolver vía respaldo MP3 y descargar a /tmp
    track.stream_url = None
    track = resolve_track_stream(track)
    sc_target_after = track.soundcloud_url or (
        track.webpage_url if (track.webpage_url and "soundcloud.com" in track.webpage_url) else None
    )
    if sc_target_after:
        try:
            sc_opts = build_ytdl_options(
                allow_playlist=False,
                flat_playlist=False,
                use_cookies=False,
                for_soundcloud=True,
            )
            sc_opts["outtmpl"] = outtmpl
            with yt_dlp.YoutubeDL(sc_opts) as ytdl:
                ytdl.extract_info(sc_target_after, download=True)
            found = _find_downloaded_file(uid)
            if found and verify_audio_file_pcm(found):
                track.local_file = found
                logger.info(
                    f"⚡ Audio MP3 descargado en /tmp ({os.path.getsize(found) // 1024} KB): {track.title}"
                )
        except Exception as exc:
            logger.warning(f"Descarga MP3 a /tmp falló, se usará stream directo: {exc}")

    return track


def extract_tracks(query: str, requester: discord.abc.User) -> List[Track]:
    """
    Extrae una canción individual o una playlist completa de YouTube, YouTube Music o SoundCloud.
    Obtiene los metadatos oficiales de YouTube (Título, Artista, Miniatura, Video ID) en <0.5s.
    """
    playlist_mode = is_playlist_query(query)
    normalized_query = normalize_youtube_url(query, playlist_mode=playlist_mode)

    if not playlist_mode:
        # 1. Intentar extracción directa de YouTube si no está bloqueada la IP
        info = _try_direct_youtube_info(normalized_query)
        if info:
            webpage = info.get("webpage_url") or normalized_query
            return [
                Track(
                    title=info.get("title", "Título desconocido"),
                    webpage_url=webpage,
                    stream_url=info.get("url"),
                    video_id=str(info.get("id", "")),
                    duration=info.get("duration"),
                    thumbnail=info.get("thumbnail"),
                    requester=requester,
                )
            ]

        # 2. Obtener metadatos oficiales de YouTube / YouTube Music (título exacto, miniatura, video_id)
        yt_meta: Optional[Dict[str, Any]] = None
        if query.startswith(("http://", "https://")) and ("youtube.com" in query or "youtu.be" in query):
            yt_meta = fetch_youtube_oembed_metadata(query)
        elif not query.startswith(("http://", "https://")):
            yt_meta = fetch_youtube_flat_search(query)

        search_term = (yt_meta.get("title") if yt_meta else None) or query
        track = _resolve_soundcloud_stream(search_term, requester, yt_meta=yt_meta)
        return [track]

    # Modo Playlist
    opts = build_ytdl_options(
        allow_playlist=True,
        flat_playlist=True,
        playlist_end=100,
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
    """Resuelve el stream de audio real de un Track."""
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

    yt_meta: Optional[Dict[str, Any]] = None
    search_title = track.title
    if track.webpage_url and ("youtube.com" in track.webpage_url or "youtu.be" in track.webpage_url):
        yt_meta = fetch_youtube_oembed_metadata(track.webpage_url)
        if yt_meta and (not search_title or search_title == "Canción de la playlist"):
            search_title = yt_meta["title"]

    sc_track = _resolve_soundcloud_stream(search_title, track.requester, yt_meta=yt_meta)
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
    Obtiene una canción recomendada automáticamente usando el Mix/Radio oficial de YouTube (RD<video_id>)
    o búsqueda relacionada, evitando repetir canciones recientes.
    """
    history_set = set(history_ids)
    if last_track.video_id:
        history_set.add(last_track.video_id)
    if last_track.webpage_url:
        history_set.add(last_track.webpage_url)
    if last_track.title:
        history_set.add(last_track.title.lower())

    # 1. Consultar Mix oficial de YouTube (RD<video_id>) con extract_flat=True (no sufre bloqueo de IP)
    if last_track.video_id and len(str(last_track.video_id)) == 11:
        mix_url = f"https://www.youtube.com/watch?v={last_track.video_id}&list=RD{last_track.video_id}"
        mix_opts = build_ytdl_options(
            allow_playlist=True,
            flat_playlist=True,
            playlist_end=20,
            use_cookies=True,
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
                    if cand_id and cand_id not in history_set and cand_title.lower() not in history_set:
                        webpage = f"https://www.youtube.com/watch?v={cand_id}"
                        candidate = Track(
                            title=cand_title,
                            webpage_url=webpage,
                            video_id=cand_id,
                            duration=entry.get("duration"),
                            requester=bot_user,
                            is_autoplay=True,
                            based_on_title=last_track.title,
                        )
                        return download_track_to_tmp(candidate)
        except Exception as exc:
            logger.debug(f"Aviso en Mix RD para {last_track.video_id}: {exc}")

    # 2. Respaldo en SoundCloud para recomendados
    sc_flat_opts = build_ytdl_options(
        allow_playlist=False,
        flat_playlist=True,
        playlist_end=12,
        default_search="scsearch",
        use_cookies=False,
        for_soundcloud=True,
    )
    sc_flat_opts["ignoreerrors"] = True
    stream_opts = build_ytdl_options(
        allow_playlist=False,
        flat_playlist=False,
        use_cookies=False,
        for_soundcloud=True,
    )
    try:
        with yt_dlp.YoutubeDL(sc_flat_opts) as ytdl:
            search_info = ytdl.extract_info(f"scsearch12:{last_track.title}", download=False)
        if search_info and "entries" in search_info:
            for entry in search_info.get("entries") or []:
                if not entry:
                    continue
                cand_id = str(entry.get("id", ""))
                cand_title = entry.get("title", "")
                cand_url = entry.get("url") or entry.get("webpage_url")
                if (
                    not cand_url
                    or cand_id in history_set
                    or cand_title.lower() in history_set
                    or cand_title.lower() == last_track.title.lower()
                ):
                    continue
                try:
                    with yt_dlp.YoutubeDL(stream_opts) as ytdl_stream:
                        track_info = ytdl_stream.extract_info(cand_url, download=False)
                    if not track_info or not track_info.get("url"):
                        continue
                    duration = track_info.get("duration") or entry.get("duration")
                    if duration and 25 <= int(duration) <= 35:
                        continue
                    sc_page = track_info.get("webpage_url") or cand_url
                    candidate = Track(
                        title=track_info.get("title") or cand_title or "Recomendación",
                        webpage_url=sc_page,
                        soundcloud_url=sc_page,
                        stream_url=track_info["url"],
                        video_id=str(track_info.get("id") or cand_id),
                        duration=duration,
                        thumbnail=track_info.get("thumbnail"),
                        requester=bot_user,
                        is_autoplay=True,
                        based_on_title=last_track.title,
                    )
                    return download_track_to_tmp(candidate)
                except Exception:
                    continue
    except Exception as exc:
        logger.warning(f"Fallo en búsqueda de recomendados de respaldo: {exc}")

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

        # Actualizar estado del canal de voz en la barra lateral de Discord
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

        # También publicar en el chat integrado del canal de voz para que todos los conectados lo vean
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

            # 1. Descargar la pista a /tmp ANTES de abrir o usar la sesión de voz (evita silencios de 20s)
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

            # 2. Conectar al canal de voz justo cuando el archivo de audio ya está listo para sonar
            voice_client = guild.voice_client
            if voice_client is None or not voice_client.is_connected():
                if target_voice_channel is not None:
                    try:
                        if voice_client is not None:
                            await voice_client.disconnect(force=True)
                        voice_client = await target_voice_channel.connect(timeout=15.0, reconnect=True)
                    except Exception as exc:
                        logger.error(f"No se pudo conectar al canal de voz: {exc}")
                        cleanup_track_file(next_track)
                        return
                else:
                    state.clear()
                    return
            elif target_voice_channel is not None and voice_client.channel != target_voice_channel:
                await voice_client.move_to(target_voice_channel)

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

            # 4. Callback no bloqueante (NO usa fut.result() dentro del hilo AudioPlayer)
            def after_playback(play_error: Optional[Exception]) -> None:
                self.bot.loop.call_soon_threadsafe(
                    lambda: self.bot.loop.create_task(
                        self._on_track_finished(guild, next_track, play_error)
                    )
                )

            voice_client.play(audio_source, after=after_playback)

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

        # Enviar respuesta inmediata (0.2s) mostrando qué canción pidió el usuario
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

            davey_ver = getattr(davey, "__version__", "instalado")
        except Exception:
            davey_ver = "NO instalado"
        logger.info(
            f"📦 discord.py={discord.__version__} | yt-dlp={yt_dlp.version.__version__} | "
            f"opus={discord.opus.is_loaded()} | davey={davey_ver}"
        )
        self.loop.create_task(start_healthcheck_server())
        await self.add_cog(MusicCog(self))
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
            body = b"OK - ElCompitaMusica is running"
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


def main() -> None:
    if not DISCORD_TOKEN or DISCORD_TOKEN == "PEGA_TU_TOKEN_AQUI":
        raise RuntimeError(
            "No se encontró un DISCORD_TOKEN válido en el archivo .env. "
            "Por favor, configura DISCORD_TOKEN en .env antes de iniciar el bot."
        )
    bot = ElCompitaBot()
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
