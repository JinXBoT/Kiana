"""
Music Cog for Logiq
Music player with YouTube support (yt-dlp + FFmpeg)
"""

import asyncio
import functools
import logging
from typing import Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from utils.embeds import EmbedFactory, EmbedColor
from utils.permissions import is_admin
from database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# yt-dlp / ffmpeg configuration
# ---------------------------------------------------------------------------

YTDL_FORMAT_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",  # avoid ipv6 issues on some hosts
    "extract_flat": False,
}

# "-vn" = no video. The reconnect flags matter a lot on cloud hosts (Railway,
# Render, etc.) where the stream can hiccup - without them ffmpeg just dies
# and the bot looks like it "joins and immediately stops".
FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
FFMPEG_OPTIONS = {
    "before_options": FFMPEG_BEFORE_OPTIONS,
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)


class Track:
    """Represents a single resolved track ready to be played"""

    __slots__ = ("title", "webpage_url", "stream_url", "duration", "requester")

    def __init__(self, title: str, webpage_url: str, stream_url: str,
                 duration: Optional[int], requester: discord.Member):
        self.title = title
        self.webpage_url = webpage_url
        self.stream_url = stream_url
        self.duration = duration
        self.requester = requester

    def __str__(self):
        return self.title


class MusicQueue:
    """Music queue manager"""

    def __init__(self):
        self.queue = []
        self.current: Optional[Track] = None
        self.loop = False

    def add(self, track: Track):
        """Add track to queue"""
        self.queue.append(track)

    def next(self) -> Optional[Track]:
        """Get next track"""
        if self.loop and self.current:
            return self.current
        if self.queue:
            self.current = self.queue.pop(0)
            return self.current
        self.current = None
        return None

    def clear(self):
        """Clear queue"""
        self.queue = []
        self.current = None

    def skip(self):
        """Peek what will play after a skip (does not pop)"""
        if self.queue:
            return self.queue[0]
        return None


class MusicControlView(discord.ui.View):
    """Music player controls"""

    def __init__(self, cog: "Music"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="⏸️ Pause", style=discord.ButtonStyle.primary, custom_id="music_pause")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Pause/Resume music"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if vc.is_playing():
            vc.pause()
            button.label = "▶️ Resume"
            await interaction.response.edit_message(view=self)
        elif vc.is_paused():
            vc.resume()
            button.label = "⏸️ Pause"
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="music_skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Skip current track"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            vc.stop()  # triggers the `after` callback -> plays next track
            await interaction.response.send_message(
                embed=EmbedFactory.success("Skipped", "Skipped current track"),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, custom_id="music_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Stop music and disconnect"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id
        if guild_id in self.cog.queues:
            self.cog.queues[guild_id].clear()

        vc = interaction.guild.voice_client
        await vc.disconnect(force=True)
        await interaction.response.send_message(
            embed=EmbedFactory.success("Stopped", "Music stopped and disconnected"),
            ephemeral=True
        )


class Music(commands.Cog):
    """Music player cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get("modules", {}).get("music", {})
        self.queues = {}  # guild_id: MusicQueue

        if not discord.opus.is_loaded():
            # On most Linux hosts this loads fine automatically once PyNaCl
            # is installed, but we try a couple of common lib names as a
            # safety net so voice doesn't silently fail to send audio.
            for libname in ("opus", "libopus.so.0", "libopus-0.dll"):
                try:
                    discord.opus.load_opus(libname)
                    break
                except OSError:
                    continue

    def get_queue(self, guild_id: int) -> MusicQueue:
        """Get or create queue for guild"""
        if guild_id not in self.queues:
            self.queues[guild_id] = MusicQueue()
        return self.queues[guild_id]

    # ------------------------------------------------------------------
    # yt-dlp helpers
    # ------------------------------------------------------------------

    async def resolve_track(self, query: str, requester: discord.Member) -> Track:
        """Resolve a search query or URL into a playable Track using yt-dlp.
        Runs in an executor thread since yt-dlp is blocking/synchronous."""
        loop = asyncio.get_event_loop()
        partial = functools.partial(ytdl.extract_info, query, download=False)
        data = await loop.run_in_executor(None, partial)

        if data is None:
            raise ValueError("Could not find that track")

        # If it's a search result, take the first entry
        if "entries" in data:
            entries = [e for e in data["entries"] if e is not None]
            if not entries:
                raise ValueError("No results found")
            data = entries[0]

        return Track(
            title=data.get("title", "Unknown title"),
            webpage_url=data.get("webpage_url", query),
            stream_url=data["url"],
            duration=data.get("duration"),
            requester=requester,
        )

    async def start_playback(self, guild: discord.Guild):
        """Play the current/next track in the guild's queue"""
        vc = guild.voice_client
        if vc is None:
            return

        music_queue = self.get_queue(guild.id)
        track = music_queue.next()

        if track is None:
            # Nothing left to play - leave the voice channel after idling
            await self._start_idle_timer(guild)
            return

        source = discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTIONS)

        def _after_playback(error, guild=guild):
            if error:
                logger.error(f"Playback error in guild {guild.id}: {error}")
            fut = asyncio.run_coroutine_threadsafe(self.start_playback(guild), self.bot.loop)
            try:
                fut.result()
            except Exception as e:
                logger.error(f"Error advancing queue in guild {guild.id}: {e}")

        vc.play(source, after=_after_playback)
        logger.info(f"Now playing in guild {guild.id}: {track.title}")

    async def _start_idle_timer(self, guild: discord.Guild, timeout: int = 180):
        """Disconnect after sitting idle with an empty queue for `timeout` seconds"""
        await asyncio.sleep(timeout)
        vc = guild.voice_client
        if vc and not vc.is_playing() and not vc.is_paused():
            queue = self.get_queue(guild.id)
            if not queue.current and not queue.queue:
                await vc.disconnect(force=True)
                logger.info(f"Disconnected from guild {guild.id} due to inactivity")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @app_commands.command(name="play", description="Play music from YouTube")
    @app_commands.describe(query="Song name or YouTube URL")
    async def play(self, interaction: discord.Interaction, query: str):
        """Play music from YouTube"""
        if not interaction.user.voice:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not in Voice", "You must be in a voice channel to use this command"),
                ephemeral=True
            )
            return

        await interaction.response.defer()

        # Connect (or move) to the user's voice channel
        if not interaction.guild.voice_client:
            try:
                await interaction.user.voice.channel.connect(timeout=15, reconnect=True)
            except discord.errors.ClientException as e:
                await interaction.followup.send(
                    embed=EmbedFactory.error(
                        "Voice Setup Error",
                        f"{e}\n\nMake sure `PyNaCl` is installed and `ffmpeg` is available on this host."
                    ),
                    ephemeral=True
                )
                return
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    embed=EmbedFactory.error(
                        "Connection Timed Out",
                        "Couldn't establish a voice connection in time. This usually means the "
                        "host is blocking the UDP traffic Discord voice needs (common on some "
                        "cloud/container hosts). Try a host that supports outbound UDP."
                    ),
                    ephemeral=True
                )
                return
            except Exception as e:
                await interaction.followup.send(
                    embed=EmbedFactory.error("Connection Failed", f"Could not join voice channel: {str(e)}"),
                    ephemeral=True
                )
                return
        elif interaction.guild.voice_client.channel != interaction.user.voice.channel:
            await interaction.guild.voice_client.move_to(interaction.user.voice.channel)

        # Resolve the track with yt-dlp
        try:
            track = await self.resolve_track(query, interaction.user)
        except Exception as e:
            logger.error(f"yt-dlp resolution failed for '{query}': {e}")
            await interaction.followup.send(
                embed=EmbedFactory.error("Search Failed", f"Could not find/resolve: {query}"),
                ephemeral=True
            )
            return

        queue = self.get_queue(interaction.guild.id)
        queue.add(track)

        vc = interaction.guild.voice_client
        started_now = not vc.is_playing() and not vc.is_paused() and queue.current is None

        if not vc.is_playing() and not vc.is_paused():
            await self.start_playback(interaction.guild)

        if started_now:
            embed = EmbedFactory.success(
                "Now Playing",
                f"**Track:** {track.title}\n**Requested by:** {interaction.user.mention}"
            )
        else:
            embed = EmbedFactory.success(
                "Added to Queue",
                f"**Track:** {track.title}\n"
                f"**Requested by:** {interaction.user.mention}\n"
                f"**Position in queue:** {len(queue.queue)}"
            )

        await interaction.followup.send(embed=embed)
        logger.info(f"Queued by {interaction.user}: {track.title}")

    @app_commands.command(name="join", description="Join your voice channel")
    async def join(self, interaction: discord.Interaction):
        """Join voice channel"""
        if not interaction.user.voice:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not in Voice", "You must be in a voice channel"),
                ephemeral=True
            )
            return

        channel = interaction.user.voice.channel
        await interaction.response.defer()

        try:
            if interaction.guild.voice_client:
                await interaction.guild.voice_client.move_to(channel)
            else:
                await channel.connect(timeout=15, reconnect=True)
        except discord.errors.ClientException as e:
            await interaction.followup.send(
                embed=EmbedFactory.error(
                    "Voice Setup Error",
                    f"{e}\n\nMake sure `PyNaCl` is installed and `ffmpeg` is available on this host."
                ),
                ephemeral=True
            )
            return
        except asyncio.TimeoutError:
            await interaction.followup.send(
                embed=EmbedFactory.error(
                    "Connection Timed Out",
                    "Couldn't establish a voice connection in time. This usually means the "
                    "host is blocking the UDP traffic Discord voice needs (common on some "
                    "cloud/container hosts). Try a host that supports outbound UDP."
                ),
                ephemeral=True
            )
            return
        except Exception as e:
            await interaction.followup.send(
                embed=EmbedFactory.error("Connection Failed", f"Could not join voice channel: {str(e)}"),
                ephemeral=True
            )
            return

        embed = EmbedFactory.success("Joined", f"Joined {channel.mention}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="leave", description="Leave voice channel")
    async def leave(self, interaction: discord.Interaction):
        """Leave voice channel"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Connected", "I'm not in a voice channel"),
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id
        if guild_id in self.queues:
            self.queues[guild_id].clear()

        await interaction.guild.voice_client.disconnect(force=True)
        embed = EmbedFactory.success("Disconnected", "Left voice channel")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="queue", description="View music queue")
    async def view_queue(self, interaction: discord.Interaction):
        """View music queue"""
        guild_id = interaction.guild.id
        queue = self.get_queue(guild_id)

        if not queue.current and not queue.queue:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Empty Queue", "The music queue is empty"),
                ephemeral=True
            )
            return

        description = ""
        if queue.current:
            description += f"**Now Playing:**\n{queue.current}\n\n"

        if queue.queue:
            description += "**Up Next:**\n"
            for i, track in enumerate(queue.queue[:10], 1):
                description += f"{i}. {track}\n"

        embed = EmbedFactory.create(
            title="🎵 Music Queue",
            description=description,
            color=EmbedColor.INFO
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="skip", description="Skip current track")
    async def skip(self, interaction: discord.Interaction):
        """Skip current track"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            vc.stop()  # triggers `after` -> advances queue automatically
            embed = EmbedFactory.success("Skipped", "Skipped current track")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )

    @app_commands.command(name="pause", description="Pause music")
    async def pause(self, interaction: discord.Interaction):
        """Pause music"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if vc.is_playing():
            vc.pause()
            embed = EmbedFactory.success("Paused", "Music paused")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )

    @app_commands.command(name="resume", description="Resume music")
    async def resume(self, interaction: discord.Interaction):
        """Resume music"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is paused"),
                ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if vc.is_paused():
            vc.resume()
            embed = EmbedFactory.success("Resumed", "Music resumed")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Paused", "Music is not paused"),
                ephemeral=True
            )

    @app_commands.command(name="volume", description="Set volume (Admin)")
    @app_commands.describe(volume="Volume level (0-100)")
    @is_admin()
    async def volume(self, interaction: discord.Interaction, volume: int):
        """Set volume"""
        if volume < 0 or volume > 100:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Volume", "Volume must be between 0 and 100"),
                ephemeral=True
            )
            return

        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = volume / 100
        elif vc.source is not None:
            # wrap the existing source so volume becomes controllable
            vc.source = discord.PCMVolumeTransformer(vc.source, volume=volume / 100)

        embed = EmbedFactory.success("Volume", f"Volume set to {volume}%")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="Show currently playing track")
    async def nowplaying(self, interaction: discord.Interaction):
        """Show currently playing track"""
        guild_id = interaction.guild.id
        queue = self.get_queue(guild_id)

        if not queue.current:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Nothing Playing", "No music is currently playing"),
                ephemeral=True
            )
            return

        embed = EmbedFactory.create(
            title="🎵 Now Playing",
            description=f"[{queue.current.title}]({queue.current.webpage_url})",
            color=EmbedColor.INFO
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Music(bot, bot.db, bot.config))
