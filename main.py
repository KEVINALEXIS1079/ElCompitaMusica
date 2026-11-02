import asyncio
from collections import deque
from dataclasses import dataclass
import logging
import os
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import parse_qs, urlparse
import urllib.request

import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

# Cargar variables de entorno
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES")
COOKIES_FILE = "cookies.txt"

# Si se configuró YOUTUBE_COOKIES como variable de entorno en la nube (Railway/Render/Fly), escribir cookies.txt
if YOUTUBE_COOKIES and not os.path.isfile(COOKIES_FILE):
    try:
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            f.write(YOUTUBE_COOKIES)
    except Exception:
        pass

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("ElCompitaMusica")

# Silenciar logs de bug reports de yt-dlp
yt_dlp.utils.bug_reports_message = lambda *args, **kwargs: ""


def build_ytdl_options(*, allow_playlist: bool = False, flat_playlist: bool = False, playlist_end: int = 100) -> Dict[str, Any]:
    """Construye las opciones de yt-dlp e inyecta cookies.txt si el usuario lo configuró."""
    opts: Dict[str, Any] = {
        "format": "bestaudio/best",
        "default_search": "ytsearch",
        "noplaylist": not allow_playlist,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": "in_playlist" if flat_playlist else False,
        "playlistend": playlist_end,
        "source_address": "0.0.0.0",
    }
    if os.path.isfile(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        opts["cookiefile"] = COOKIES_FILE
    return opts


# Opciones de FFmpeg con reconexión automática
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


def is_playlist_query(query: str) -> bool:
    """Detecta si el usuario envió una URL de playlist o mix."""
    if not query.startswith(("http://", "https://")):
        return False
    parsed = urlparse(query)
    qs = parse_qs(parsed.query)
    if "list" in qs:
        return True
    if "/playlist" in parsed.path or "/sets/" in parsed.path or "/album/" in parsed.path:
        return True
    return False


@dataclass
class Track:
    """Representa una pista de audio (con soporte para carga diferida en playlists)."""

    title: str
    webpage_url: str
    requester: discord.abc.User
    stream_url: Optional[str] = None
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


class GuildMusicState:
    """Administra la cola de reproducción, historial y Autoplay por servidor (guild_id)."""

    def __init__(self) -> None:
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None
        self.last_track: Optional[Track] = None
        self.history_ids: Deque[str] = deque(maxlen=50)
        self.autoplay: bool = True
        self.text_channel: Optional[discord.abc.Messageable] = None
        self.inactivity_task: Optional[asyncio.Task] = None

    def register_played(self, track: Track) -> None:
        self.current = track
        self.last_track = track
        if track.video_id and track.video_id not in self.history_ids:
            self.history_ids.append(track.video_id)
        elif track.webpage_url and track.webpage_url not in self.history_ids:
            self.history_ids.append(track.webpage_url)

    def clear(self) -> None:
        self.queue.clear()
        self.current = None
        self.last_track = None
        self.cancel_inactivity_task()

    def cancel_inactivity_task(self) -> None:
        if self.inactivity_task and not self.inactivity_task.done():
            self.inactivity_task.cancel()
            self.inactivity_task = None


def extract_tracks(query: str, requester: discord.abc.User) -> List[Track]:
    """
    Extrae una canción individual o una playlist completa con yt-dlp.
    Para playlists usa extract_flat='in_playlist' para agregar decenas de pistas al instante.
    """
    playlist_mode = is_playlist_query(query)
    opts = build_ytdl_options(
        allow_playlist=playlist_mode,
        flat_playlist=playlist_mode,
        playlist_end=100,
    )

    with yt_dlp.YoutubeDL(opts) as ytdl:
        info = ytdl.extract_info(query, download=False)

    if not info:
        raise ValueError("No se pudo obtener información del audio solicitado.")

    tracks: List[Track] = []

    if "entries" in info:
        entries = [e for e in info.get("entries", []) if e]
        if not entries:
            raise ValueError(f"No se encontraron resultados para: `{query}`")

        # Si fue una búsqueda por texto (ej. ytsearch), tomamos el primer resultado completo
        if not playlist_mode:
            entry = entries[0]
            webpage = entry.get("webpage_url") or entry.get("url") or query
            tracks.append(
                Track(
                    title=entry.get("title", "Título desconocido"),
                    webpage_url=webpage,
                    stream_url=entry.get("url") if entry.get("acodec") else None,
                    video_id=entry.get("id"),
                    duration=entry.get("duration"),
                    thumbnail=entry.get("thumbnail"),
                    requester=requester,
                )
            )
        else:
            # Es una playlist: creamos los Track ligeros y su stream se resuelve al tocarles turno
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
                        webpage_url=raw_url,
                        stream_url=None,
                        video_id=vid_id,
                        duration=entry.get("duration"),
                        thumbnail=thumb_url,
                        requester=requester,
                    )
                )
    else:
        webpage = info.get("webpage_url") or query
        tracks.append(
            Track(
                title=info.get("title", "Título desconocido"),
                webpage_url=webpage,
                stream_url=info.get("url"),
                video_id=info.get("id"),
                duration=info.get("duration"),
                thumbnail=info.get("thumbnail"),
                requester=requester,
            )
        )

    if not tracks:
        raise ValueError("No se encontraron pistas reproducibles en el enlace proporcionado.")

    return tracks


def resolve_track_stream(track: Track) -> Track:
    """Resuelve el stream de audio real de un Track si provino de una playlist o de Autoplay."""
    if track.stream_url:
        return track

    opts = build_ytdl_options(allow_playlist=False, flat_playlist=False)
    target = track.webpage_url or (f"https://www.youtube.com/watch?v={track.video_id}" if track.video_id else track.title)

    with yt_dlp.YoutubeDL(opts) as ytdl:
        info = ytdl.extract_info(target, download=False)

    if not info:
        raise ValueError(f"No se pudo resolver el stream para {track.title}")

    if "entries" in info:
        entries = [e for e in info.get("entries", []) if e]
        if not entries:
            raise ValueError(f"Sin resultados al resolver {track.title}")
        info = entries[0]

    stream_url = info.get("url")
    if not stream_url:
        raise ValueError(f"No se encontró URL de audio para {track.title}")

    track.stream_url = stream_url
    track.title = info.get("title") or track.title
    track.webpage_url = info.get("webpage_url") or track.webpage_url
    track.video_id = info.get("id") or track.video_id
    track.duration = info.get("duration") or track.duration
    track.thumbnail = info.get("thumbnail") or track.thumbnail
    return track


def fetch_recommended_track(
    last_track: Track,
    history_ids: List[str],
    bot_user: discord.abc.User,
) -> Optional[Track]:
    """
    Obtiene una canción recomendada automáticamente usando el Mix/Radio oficial de YouTube (RD<video_id>)
    o búsqueda relacionada si no hay ID de YouTube, evitando repetir canciones recientes.
    """
    history_set = set(history_ids)
    if last_track.video_id:
        history_set.add(last_track.video_id)
    if last_track.webpage_url:
        history_set.add(last_track.webpage_url)

    # 1. Intentar obtener el Mix/Radio de recomendados de YouTube (list=RD<video_id>)
    if last_track.video_id:
        mix_url = f"https://www.youtube.com/watch?v={last_track.video_id}&list=RD{last_track.video_id}"
        mix_opts = build_ytdl_options(allow_playlist=True, flat_playlist=True, playlist_end=20)
        try:
            with yt_dlp.YoutubeDL(mix_opts) as ytdl:
                mix_info = ytdl.extract_info(mix_url, download=False)
            if mix_info and "entries" in mix_info:
                for entry in mix_info.get("entries") or []:
                    if not entry:
                        continue
                    cand_id = entry.get("id")
                    cand_url = entry.get("webpage_url") or entry.get("url")
                    if cand_id and cand_id not in history_set:
                        webpage = f"https://www.youtube.com/watch?v={cand_id}"
                        candidate = Track(
                            title=entry.get("title") or "Recomendación",
                            webpage_url=webpage,
                            video_id=cand_id,
                            duration=entry.get("duration"),
                            requester=bot_user,
                            is_autoplay=True,
                            based_on_title=last_track.title,
                        )
                        return resolve_track_stream(candidate)
        except Exception as exc:
            logger.warning(f"Fallo al consultar Mix RD para {last_track.video_id}: {exc}")

    # 2. Respaldo: buscar pistas similares en YouTube
    fallback_opts = build_ytdl_options(allow_playlist=False, flat_playlist=True, playlist_end=10)
    search_query = f"ytsearch10:{last_track.title} mix"
    try:
        with yt_dlp.YoutubeDL(fallback_opts) as ytdl:
            search_info = ytdl.extract_info(search_query, download=False)
        if search_info and "entries" in search_info:
            for entry in search_info.get("entries") or []:
                if not entry:
                    continue
                cand_id = entry.get("id")
                cand_title = entry.get("title", "")
                if cand_id and cand_id not in history_set and cand_title.lower() != last_track.title.lower():
                    webpage = f"https://www.youtube.com/watch?v={cand_id}"
                    candidate = Track(
                        title=cand_title or "Recomendación",
                        webpage_url=webpage,
                        video_id=cand_id,
                        duration=entry.get("duration"),
                        requester=bot_user,
                        is_autoplay=True,
                        based_on_title=last_track.title,
                    )
                    return resolve_track_stream(candidate)
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

    async def play_next(self, guild: discord.Guild, error: Optional[Exception] = None) -> None:
        """Reproduce la siguiente canción en la cola o busca una recomendada si Autoplay está activo."""
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
            # Si la cola se vació y Autoplay está activado, buscamos una canción recomendada
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

        # Si el track viene de una playlist (lazy loading), resolvemos su stream ahora
        if not next_track.stream_url:
            try:
                next_track = await self.bot.loop.run_in_executor(
                    None, resolve_track_stream, next_track
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

        try:
            source = discord.FFmpegPCMAudio(next_track.stream_url, **FFMPEG_OPTIONS)
            audio_source = discord.PCMVolumeTransformer(source, volume=0.8)
        except Exception as exc:
            logger.exception("Fallo al inicializar FFmpegPCMAudio:")
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
                self.play_next(guild, play_error),
                self.bot.loop,
            )
            try:
                fut.result()
            except Exception as cb_exc:
                logger.error(f"Excepción en el callback after_playback: {cb_exc}")

        voice_client.play(audio_source, after=after_playback)

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
        description="Reproduce una canción o playlist completa de YouTube/SoundCloud.",
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
        voice_client.stop()  # Dispara after_playback -> play_next
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
                f"`{idx}.` **[{track.title}]({track.webpage_url})** (`{track.formatted_duration}`) — {track.requester.mention}"
                for idx, track in enumerate(upcoming, start=1)
            ]
            embed.add_field(
                name=f"⏳ Próximas canciones (Mostrando {len(upcoming)} de {len(state.queue)})",
                value="\n".join(lines),
                inline=False,
            )
        else:
            autoplay_status = "Activado ✅ (pondrá recomendados al terminar)" if state.autoplay else "Desactivado ⛔"
            embed.add_field(
                name="⏳ Próximas canciones",
                value=f"_No hay más canciones en cola._\n📻 **Autoplay:** {autoplay_status}",
                inline=False,
            )

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="stop",
        description="Limpia la cola y detiene el audio por completo.",
    )
    async def stop(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            return
        voice_client: Optional[discord.VoiceClient] = ctx.guild.voice_client
        state = self.get_state(ctx.guild.id)
        state.queue.clear()
        state.current = None
        state.last_track = None  # Evita que se dispare Autoplay tras hacer !stop

        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()

        await ctx.send(
            embed=discord.Embed(
                description="⏹️ Se detuvo la reproducción y se limpió la cola.",
                color=discord.Color.orange(),
            )
        )

    @commands.hybrid_command(
        name="leave",
        aliases=["dc"],
        description="Limpia la cola y desconecta al bot del canal de voz.",
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
                description="👋 Me desconecté del canal de voz y limpié la cola.",
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
        """Desconecta automáticamente al bot si se queda solo en el canal de voz por más de 3 minutos."""
        voice_client: Optional[discord.VoiceClient] = member.guild.voice_client
        if not voice_client or not voice_client.channel:
            return

        state = self.get_state(member.guild.id)

        # Si el propio bot fue desconectado manualmente o movido fuera
        if self.bot.user and member.id == self.bot.user.id and after.channel is None:
            state.clear()
            return

        non_bot_members = [m for m in voice_client.channel.members if not m.bot]

        if len(non_bot_members) == 0:
            if state.inactivity_task is None or state.inactivity_task.done():
                state.inactivity_task = self.bot.loop.create_task(
                    self._disconnect_after_inactivity(member.guild)
                )
        else:
            state.cancel_inactivity_task()

    async def _disconnect_after_inactivity(self, guild: discord.Guild) -> None:
        try:
            await asyncio.sleep(180)  # 3 minutos
            voice_client: Optional[discord.VoiceClient] = guild.voice_client
            if voice_client and voice_client.is_connected() and voice_client.channel:
                non_bot_members = [m for m in voice_client.channel.members if not m.bot]
                if len(non_bot_members) == 0:
                    state = self.get_state(guild.id)
                    text_channel = state.text_channel
                    state.clear()
                    await voice_client.disconnect()
                    if text_channel:
                        await text_channel.send(
                            embed=discord.Embed(
                                description="💤 Me desconecté automáticamente tras pasar 3 minutos solo en el canal de voz.",
                                color=discord.Color.light_grey(),
                            )
                        )
        except asyncio.CancelledError:
            pass


class ElCompitaBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=commands.DefaultHelpCommand(no_category="Comandos Generales"),
        )

    async def setup_hook(self) -> None:
        await self.add_cog(MusicCog(self))
        self.loop.create_task(start_healthcheck_server())
        try:
            synced = await self.tree.sync()
            logger.info(f"Se sincronizaron {len(synced)} comandos híbridos/slash.")
        except Exception as exc:
            logger.warning(f"No se pudieron sincronizar los comandos slash: {exc}")

    async def on_ready(self) -> None:
        if self.user:
            logger.info(f"🤖 {self.user} (ID: {self.user.id}) está en línea y listo.")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="!play | Playlists & Autoplay",
            )
        )

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        """Manejo global de errores de comandos."""
        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                embed=discord.Embed(
                    description=f"⚠️ Falta el argumento requerido: `{error.param.name}`.\nUso: `!{ctx.command.qualified_name} {ctx.command.signature}`",
                    color=discord.Color.gold(),
                )
            )
            return

        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ Este comando solo puede utilizarse dentro de un servidor.")
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
    """Mini servidor HTTP para poder usar el Plan Gratis ($0) de Render.com / Koyeb."""
    port = int(os.getenv("PORT", "8080"))
    try:
        server = await asyncio.start_server(_handle_http_client, "0.0.0.0", port)
        logger.info(f"🌐 Servidor Health-Check escuchando en el puerto {port} (compatible con Render Free).")
    except Exception as exc:
        logger.warning(f"No se pudo iniciar el servidor health-check en el puerto {port}: {exc}")
        return

    # Auto-ping cada 10 minutos si RENDER_EXTERNAL_URL está definido para evitar suspensión
    external_url = os.getenv("RENDER_EXTERNAL_URL")
    if external_url:
        while True:
            await asyncio.sleep(600)
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, urllib.request.urlopen, external_url
                )
            except Exception:
                pass
    else:
        async with server:
            await server.serve_forever()


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
