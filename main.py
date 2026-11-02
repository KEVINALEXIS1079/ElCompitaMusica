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
import tempfile
import uuid
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlparse
import urllib.request

import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("ElCompitaMusica")

# Cargar variables de entorno
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES")

TEMP_DIR = tempfile.gettempdir()
AUDIO_CACHE_DIR = os.path.join(TEMP_DIR, "elcompita_audio")
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)

COOKIES_MASTER_FILE = os.path.join(TEMP_DIR, "cookies_master.txt")
COOKIES_WORKING_FILE = os.path.join(TEMP_DIR, "cookies_working.txt")

# Silenciar logs de bug reports de yt-dlp
yt_dlp.utils.bug_reports_message = lambda *args, **kwargs: ""


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
    """
    Detecta y prepara un archivo maestro de cookies de YouTube buscando en:
    1. /etc/secrets/ (Render Secret Files)
    2. ./cookies.txt local
    3. Variable de entorno YOUTUBE_COOKIES (texto plano o Base64)
    4. ./cookies.enc (encriptado con DISCORD_TOKEN)
    """
    # 1. Revisar /etc/secrets (Render Secret Files)
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

    # 2. Revisar directorio actual por cookies.txt
    if os.path.isfile("cookies.txt") and os.path.getsize("cookies.txt") > 0:
        try:
            shutil.copyfile("cookies.txt", COOKIES_MASTER_FILE)
            shutil.copyfile(COOKIES_MASTER_FILE, COOKIES_WORKING_FILE)
            logger.info("✅ Cookies de YouTube cargadas desde ./cookies.txt")
            return COOKIES_WORKING_FILE
        except Exception as exc:
            logger.warning(f"Error copiando ./cookies.txt: {exc}")

    # 3. Revisar variable de entorno YOUTUBE_COOKIES
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

    # 4. Revisar cookies.enc encriptado con DISCORD_TOKEN
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
    """Devuelve una copia fresca del archivo de cookies para evitar que yt-dlp lo corrompa al sobreescribirlo."""
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
    custom_extractor_args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Construye opciones optimizadas de yt-dlp:
    - Prioriza streams progresivos HTTPS (opus/webm/m4a) y evita m3u8/HLS segmentados que causan cortes.
    - Activa Node.js como runtime JS (yt-dlp-ejs) para resolver el n-challenge de YouTube y evitar throttling.
    """
    opts: Dict[str, Any] = {
        "format": (
            "bestaudio[ext=webm][protocol=https]/"
            "bestaudio[ext=m4a][protocol=https]/"
            "bestaudio[protocol=https]/"
            "bestaudio[protocol^=http][protocol!=m3u8][protocol!=m3u8_native]/"
            "bestaudio/best"
        ),
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
        "concurrent_fragment_downloads": 4,
        "buffersize": 65536,
        "http_chunk_size": 10485760,
        "retries": 5,
        "fragment_retries": 5,
        "js_runtimes": {"node": {}},
    }

    if custom_extractor_args:
        opts["extractor_args"] = custom_extractor_args

    if use_cookies:
        cookie_path = get_fresh_cookie_file()
        if cookie_path and os.path.isfile(cookie_path) and os.path.getsize(cookie_path) > 0:
            opts["cookiefile"] = cookie_path

    return opts


# Opciones de FFmpeg para archivos locales en /tmp (cero latencia de red, cero cortes)
FFMPEG_LOCAL_OPTIONS = {
    "before_options": "-nostdin -thread_queue_size 4096",
    "options": "-vn -b:a 128k -bufsize 2M",
}

# Opciones de FFmpeg para streaming directo con reconexión agresiva y buffer grande
FFMPEG_STREAM_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
        "-reconnect_on_http_error 4xx,5xx -reconnect_delay_max 5 "
        "-thread_queue_size 4096 -nostdin"
    ),
    "options": "-vn -b:a 128k -bufsize 4M",
}


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
        # Si el link tiene un video (v=) y la lista es un radio automático (RD...), reproducir la canción y dejar que Autoplay siga
        if "v" in qs and (list_id.startswith("RD") or list_id.startswith("OLAK")):
            return False
        return True
    return False


def normalize_youtube_url(url: str, *, playlist_mode: bool = False) -> str:
    """
    Convierte enlaces de music.youtube.com o youtu.be a formato estándar www.youtube.com
    para máxima compatibilidad con yt-dlp y cookies de .youtube.com.
    """
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


def fetch_youtube_oembed_title(url: str) -> Optional[str]:
    """Obtiene el título y artista de un video de YouTube/YouTube Music vía oEmbed público."""
    try:
        clean_url = normalize_youtube_url(url, playlist_mode=False)
        oembed_url = f"https://www.youtube.com/oembed?url={quote(clean_url, safe='')}&format=json"
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            title = data.get("title")
            author = data.get("author_name", "").replace(" - Topic", "").strip()
            if title and author and author.lower() not in title.lower():
                return f"{author} - {title}"
            return title
    except Exception:
        return None


@dataclass
class Track:
    """Representa una pista de audio con soporte para pre-descarga en /tmp."""

    title: str
    webpage_url: str
    requester: discord.abc.User
    stream_url: Optional[str] = None
    local_file: Optional[str] = None
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


def _extract_single_youtube_info(target_query: str) -> Optional[Dict[str, Any]]:
    """
    Intenta extraer la información de un video o búsqueda de YouTube usando múltiples estrategias:
    1. Con cookies de YouTube + Node.js EJS runtime (cliente web estándar / YouTube Music).
    2. Con cookies + clientes alternativos (tv, mweb, web_creator).
    3. Sin cookies + clientes embebidos/móviles (ios, android_vr, tv_embedded).
    """
    strategies = [
        {"use_cookies": True, "custom_extractor_args": None},
        {
            "use_cookies": True,
            "custom_extractor_args": {"youtube": {"player_client": ["tv", "mweb", "web"]}},
        },
        {
            "use_cookies": False,
            "custom_extractor_args": {"youtube": {"player_client": ["ios", "android_vr", "tv_embedded"]}},
        },
    ]

    last_err = None
    for idx, strat in enumerate(strategies, start=1):
        opts = build_ytdl_options(
            allow_playlist=False,
            flat_playlist=False,
            use_cookies=strat["use_cookies"],
            custom_extractor_args=strat["custom_extractor_args"],
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ytdl:
                info = ytdl.extract_info(target_query, download=False)
            if info and "entries" in info:
                entries = [e for e in (info.get("entries") or []) if e]
                info = entries[0] if entries else None
            if info and info.get("url"):
                return info
        except Exception as exc:
            last_err = exc
            logger.debug(f"Estrategia YouTube #{idx} falló para '{target_query}': {exc}")

    if last_err:
        logger.warning(f"Todas las estrategias directas de YouTube fallaron para '{target_query}': {last_err}")
    return None


def _extract_from_soundcloud(search_text: str, requester: discord.abc.User) -> Track:
    """
    Respaldo automático con SoundCloud cuando un video específico tiene bloqueo regional/IP.
    Prioriza streams HTTP progresivos (no HLS m3u8) y salta pistas con DRM o previews de 30s.
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
        " - Topic",
    ):
        clean_query = clean_query.replace(noise, "").replace(noise.lower(), "")
    clean_query = clean_query.strip()

    if clean_query.startswith(("http://", "https://")) and "soundcloud.com" in clean_query:
        sc_single_opts = build_ytdl_options(allow_playlist=False, flat_playlist=False, use_cookies=False)
        with yt_dlp.YoutubeDL(sc_single_opts) as ytdl:
            info = ytdl.extract_info(clean_query, download=False)
        if info and info.get("url"):
            return Track(
                title=info.get("title", clean_query),
                webpage_url=info.get("webpage_url") or clean_query,
                stream_url=info["url"],
                video_id=str(info.get("id", "")),
                duration=info.get("duration"),
                thumbnail=info.get("thumbnail"),
                requester=requester,
            )

    flat_opts = build_ytdl_options(
        allow_playlist=False,
        flat_playlist=True,
        playlist_end=10,
        default_search="scsearch",
        use_cookies=False,
    )
    flat_opts["ignoreerrors"] = True

    with yt_dlp.YoutubeDL(flat_opts) as ytdl:
        search_info = ytdl.extract_info(f"scsearch10:{clean_query}", download=False)

    entries = [e for e in (search_info or {}).get("entries", []) if e]
    if not entries:
        raise ValueError(f"No se encontraron resultados disponibles para '{search_text}'.")

    stream_opts = build_ytdl_options(allow_playlist=False, flat_playlist=False, use_cookies=False)
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

            duration = track_info.get("duration") or entry.get("duration")
            candidate_track = Track(
                title=track_info.get("title") or entry.get("title") or clean_query,
                webpage_url=track_info.get("webpage_url") or cand_url,
                stream_url=track_info["url"],
                video_id=str(track_info.get("id", "")),
                duration=duration,
                thumbnail=track_info.get("thumbnail"),
                requester=requester,
            )

            if duration and 25 <= int(duration) <= 35:
                if fallback_preview_track is None:
                    fallback_preview_track = candidate_track
                continue

            return candidate_track
        except Exception as exc:
            logger.debug(f"Saltando candidato SoundCloud ({cand_url}) por DRM/error: {exc}")
            continue

    if fallback_preview_track is not None:
        return fallback_preview_track

    raise ValueError(f"Todas las versiones encontradas para '{clean_query}' tenían restricción DRM.")


def download_track_to_tmp(track: Track) -> Track:
    """
    Descarga la pista de audio en /tmp/elcompita_audio antes de reproducirla (si dura <= 25 min).
    Al reproducir desde un archivo local en /tmp en lugar de la red en vivo:
    - Se elimina al 100% el tartamudeo / congelamiento ("que no se trabe la canción").
    - Se evita el throttling de ancho de banda de YouTube a mitad de la canción.
    """
    if track.local_file and os.path.isfile(track.local_file) and os.path.getsize(track.local_file) > 1024:
        return track

    # Si es una transmisión en vivo o dura más de 25 minutos, usar stream directo
    if track.duration and int(track.duration) > 1500:
        return resolve_track_stream(track)

    uid = f"{track.video_id or uuid.uuid4().hex[:8]}_{uuid.uuid4().hex[:6]}"
    outtmpl = os.path.join(AUDIO_CACHE_DIR, f"{uid}.%(ext)s")

    target = track.webpage_url
    if not target or not target.startswith(("http://", "https://")):
        if track.video_id and len(str(track.video_id)) == 11:
            target = f"https://www.youtube.com/watch?v={track.video_id}"
        else:
            target = f"ytsearch1:{track.title}"
    else:
        target = normalize_youtube_url(target, playlist_mode=False)

    dl_strategies = [
        {"use_cookies": True, "custom_extractor_args": None},
        {
            "use_cookies": True,
            "custom_extractor_args": {"youtube": {"player_client": ["tv", "mweb", "web"]}},
        },
        {
            "use_cookies": False,
            "custom_extractor_args": {"youtube": {"player_client": ["ios", "android_vr", "tv_embedded"]}},
        },
    ]

    for strat in dl_strategies:
        opts = build_ytdl_options(
            allow_playlist=False,
            flat_playlist=False,
            use_cookies=strat["use_cookies"],
            custom_extractor_args=strat["custom_extractor_args"],
        )
        opts["outtmpl"] = outtmpl
        try:
            with yt_dlp.YoutubeDL(opts) as ytdl:
                info = ytdl.extract_info(target, download=True)
                if info and "entries" in info:
                    entries = [e for e in (info.get("entries") or []) if e]
                    info = entries[0] if entries else None
                if info:
                    downloaded_path = ytdl.prepare_filename(info)
                    if os.path.isfile(downloaded_path) and os.path.getsize(downloaded_path) > 1024:
                        track.local_file = downloaded_path
                        track.stream_url = info.get("url") or track.stream_url
                        track.title = info.get("title") or track.title
                        track.webpage_url = info.get("webpage_url") or track.webpage_url
                        track.video_id = str(info.get("id") or track.video_id or "")
                        track.duration = info.get("duration") or track.duration
                        track.thumbnail = info.get("thumbnail") or track.thumbnail
                        logger.info(
                            f"⚡ Audio descargado en caché local ({os.path.getsize(downloaded_path) // 1024} KB): {track.title}"
                        )
                        return track
        except Exception as exc:
            logger.debug(f"Intento de descarga a /tmp falló para '{target}': {exc}")

    # Si YouTube falló en descarga directa, resolver vía respaldo y descargar también a /tmp
    track = resolve_track_stream(track)
    if track.webpage_url and "soundcloud.com" in track.webpage_url:
        try:
            sc_opts = build_ytdl_options(allow_playlist=False, flat_playlist=False, use_cookies=False)
            sc_opts["outtmpl"] = outtmpl
            with yt_dlp.YoutubeDL(sc_opts) as ytdl:
                info = ytdl.extract_info(track.webpage_url, download=True)
                if info:
                    downloaded_path = ytdl.prepare_filename(info)
                    if os.path.isfile(downloaded_path) and os.path.getsize(downloaded_path) > 1024:
                        track.local_file = downloaded_path
                        logger.info(
                            f"⚡ Audio de respaldo descargado en caché local ({os.path.getsize(downloaded_path) // 1024} KB): {track.title}"
                        )
        except Exception:
            pass

    return track


def extract_tracks(query: str, requester: discord.abc.User) -> List[Track]:
    """
    Extrae una canción individual o una playlist completa de YouTube, YouTube Music o SoundCloud.
    Normaliza enlaces de music.youtube.com para evitar errores con parámetros si= o list=RD...
    """
    playlist_mode = is_playlist_query(query)
    normalized_query = normalize_youtube_url(query, playlist_mode=playlist_mode)

    if not playlist_mode:
        info = _extract_single_youtube_info(normalized_query)
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

        # Respaldo automático si un video específico fue bloqueado
        fallback_query = query
        if query.startswith(("http://", "https://")) and ("youtube.com" in query or "youtu.be" in query):
            oembed_title = fetch_youtube_oembed_title(query)
            if oembed_title:
                fallback_query = oembed_title
            else:
                raise ValueError("No se pudo obtener información del enlace de YouTube proporcionado.")
        logger.info(f"🔄 Usando respaldo automático para: '{fallback_query}'")
        track = _extract_from_soundcloud(fallback_query, requester)
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

    info = _extract_single_youtube_info(target)
    if info and info.get("url"):
        track.stream_url = info.get("url")
        track.title = info.get("title") or track.title
        track.webpage_url = info.get("webpage_url") or track.webpage_url
        track.video_id = str(info.get("id") or track.video_id or "")
        track.duration = info.get("duration") or track.duration
        track.thumbnail = info.get("thumbnail") or track.thumbnail
        return track

    search_title = track.title
    if (not search_title or search_title == "Canción de la playlist") and track.webpage_url:
        oembed_title = fetch_youtube_oembed_title(track.webpage_url)
        if oembed_title:
            search_title = oembed_title

    sc_track = _extract_from_soundcloud(search_title, track.requester)
    track.stream_url = sc_track.stream_url
    track.webpage_url = sc_track.webpage_url or track.webpage_url
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

    # 1. Intentar obtener el Mix/Radio de recomendados de YouTube (list=RD<video_id>)
    if last_track.video_id and len(str(last_track.video_id)) == 11:
        mix_url = f"https://www.youtube.com/watch?v={last_track.video_id}&list=RD{last_track.video_id}"
        mix_opts = build_ytdl_options(allow_playlist=True, flat_playlist=True, playlist_end=20)
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
            logger.warning(f"Fallo al consultar Mix RD para {last_track.video_id}: {exc}")

    # 2. Respaldo por búsqueda en YouTube
    yt_flat_opts = build_ytdl_options(
        allow_playlist=False,
        flat_playlist=True,
        playlist_end=8,
        default_search="ytsearch",
    )
    yt_flat_opts["ignoreerrors"] = True
    try:
        with yt_dlp.YoutubeDL(yt_flat_opts) as ytdl:
            search_info = ytdl.extract_info(f"ytsearch8:{last_track.title} mix", download=False)
        for entry in (search_info or {}).get("entries") or []:
            if not entry:
                continue
            cand_id = str(entry.get("id", ""))
            cand_title = entry.get("title") or ""
            if (
                not cand_id
                or cand_id in history_set
                or cand_title.lower() in history_set
                or cand_title.lower() == last_track.title.lower()
            ):
                continue
            candidate = Track(
                title=cand_title,
                webpage_url=f"https://www.youtube.com/watch?v={cand_id}",
                video_id=cand_id,
                duration=entry.get("duration"),
                requester=bot_user,
                is_autoplay=True,
                based_on_title=last_track.title,
            )
            return download_track_to_tmp(candidate)
    except Exception as exc:
        logger.debug(f"Fallo en búsqueda YouTube para recomendados: {exc}")

    # 3. Respaldo en SoundCloud
    sc_flat_opts = build_ytdl_options(
        allow_playlist=False,
        flat_playlist=True,
        playlist_end=12,
        default_search="scsearch",
        use_cookies=False,
    )
    sc_flat_opts["ignoreerrors"] = True
    stream_opts = build_ytdl_options(allow_playlist=False, flat_playlist=False, use_cookies=False)
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
                    candidate = Track(
                        title=track_info.get("title") or cand_title or "Recomendación",
                        webpage_url=track_info.get("webpage_url") or cand_url,
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

    async def play_next(
        self,
        guild: discord.Guild,
        finished_track: Optional[Track] = None,
        error: Optional[Exception] = None,
    ) -> None:
        """Limpia el archivo anterior y reproduce la siguiente canción en la cola o Autoplay."""
        if finished_track:
            cleanup_track_file(finished_track)

        if error:
            logger.error(f"Error en el reproductor de {guild.name} ({guild.id}): {error}")

        state = self.get_state(guild.id)
        voice_client: Optional[discord.VoiceClient] = guild.voice_client

        if not voice_client or not voice_client.is_connected():
            state.clear()
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
            return

        # Preparar audio local en /tmp (o stream de respaldo si es muy largo)
        try:
            next_track = await self.bot.loop.run_in_executor(
                None, download_track_to_tmp, next_track
            )
        except Exception as exc:
            logger.warning(f"No se pudo cargar '{next_track.title}': {exc}")
            if state.text_channel:
                await state.text_channel.send(
                    embed=discord.Embed(
                        description=f"⚠️ No se pudo reproducir **{next_track.title}**. Saltando a la siguiente...",
                        color=discord.Color.red(),
                    )
                )
            await self.play_next(guild)
            return

        state.register_played(next_track)

        # Usar FFmpegOpusAudio (evita re-codificación PCM en Python y elimina cortes de CPU en la nube)
        try:
            if next_track.local_file and os.path.isfile(next_track.local_file):
                audio_source = await discord.FFmpegOpusAudio.from_probe(
                    next_track.local_file,
                    method="fallback",
                    **FFMPEG_LOCAL_OPTIONS,
                )
            elif next_track.stream_url:
                audio_source = await discord.FFmpegOpusAudio.from_probe(
                    next_track.stream_url,
                    method="fallback",
                    **FFMPEG_STREAM_OPTIONS,
                )
            else:
                raise ValueError("No se encontró fuente de audio reproducible.")
        except Exception as exc:
            logger.exception("Fallo al inicializar FFmpegOpusAudio:")
            cleanup_track_file(next_track)
            if state.text_channel:
                await state.text_channel.send(
                    embed=discord.Embed(
                        description=f"⚠️ Error al iniciar audio de **{next_track.title}**: `{exc}`. Saltando...",
                        color=discord.Color.red(),
                    )
                )
            await self.play_next(guild)
            return

        def after_playback(play_error: Optional[Exception]) -> None:
            fut = asyncio.run_coroutine_threadsafe(
                self.play_next(guild, next_track, play_error),
                self.bot.loop,
            )
            try:
                fut.result()
            except Exception as cb_exc:
                logger.error(f"Excepción en el callback after_playback: {cb_exc}")

        voice_client.play(audio_source, after=after_playback)

        # Iniciar pre-descarga en segundo plano de la siguiente canción de la cola
        state.cancel_prefetch_task()
        if state.queue:
            state.prefetch_task = self.bot.loop.create_task(self._prefetch_next_in_queue(guild))

        if state.text_channel:
            if next_track.is_autoplay:
                embed = discord.Embed(
                    title="✨ Recomendado automático (Autoplay)",
                    description=f"**[{next_track.title}]({next_track.webpage_url})**",
                    color=discord.Color.purple(),
                )
                embed.add_field(name="⏱️ Duración", value=next_track.formatted_duration, inline=True)
                if next_track.based_on_title:
                    embed.add_field(
                        name="📻 Basado en",
                        value=next_track.based_on_title[:60],
                        inline=True,
                    )
                embed.set_footer(text="Usa !autoplay para activar/desactivar las recomendaciones automáticas.")
            else:
                embed = discord.Embed(
                    title="🎶 Reproduciendo ahora",
                    description=f"**[{next_track.title}]({next_track.webpage_url})**",
                    color=discord.Color.blurple(),
                )
                embed.add_field(name="⏱️ Duración", value=next_track.formatted_duration, inline=True)
                embed.add_field(name="🙋 Pedido por", value=next_track.requester.mention, inline=True)

            if next_track.thumbnail:
                embed.set_thumbnail(url=next_track.thumbnail)
            await state.text_channel.send(embed=embed)

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

        await ctx.defer()

        target_channel = ctx.author.voice.channel
        voice_client: Optional[discord.VoiceClient] = ctx.guild.voice_client

        if voice_client is None:
            voice_client = await target_channel.connect()
        elif voice_client.channel != target_channel:
            await voice_client.move_to(target_channel)

        state = self.get_state(ctx.guild.id)
        state.text_channel = ctx.channel

        try:
            tracks = await self.bot.loop.run_in_executor(
                None, extract_tracks, query, ctx.author
            )
        except Exception as exc:
            await ctx.send(
                embed=discord.Embed(
                    title="❌ Error al buscar el audio",
                    description=f"No se pudo procesar `{query}`.\nDetalle: `{exc}`",
                    color=discord.Color.red(),
                )
            )
            return

        was_playing = voice_client.is_playing() or voice_client.is_paused()

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
            await ctx.send(embed=embed)

            if not was_playing:
                await self.play_next(ctx.guild)
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
                await ctx.send(embed=embed)

                if len(state.queue) == 1:
                    state.cancel_prefetch_task()
                    state.prefetch_task = self.bot.loop.create_task(self._prefetch_next_in_queue(ctx.guild))
            else:
                if ctx.interaction:
                    embed = discord.Embed(
                        description=f"🔍 Cargando **{track.title}** (`{track.formatted_duration}`)...",
                        color=discord.Color.blurple(),
                    )
                    await ctx.send(embed=embed)
                await self.play_next(ctx.guild)

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

        if member.id == self.bot.user.id and after.channel is None:
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
        await reader.read(1024)
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

    external_url = os.getenv("RENDER_EXTERNAL_URL", "https://elcompitamusica-1.onrender.com")
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
