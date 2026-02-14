import discord
from discord.ext import commands
import asyncio
import logging
import os
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from asgiref.sync import sync_to_async
from datetime import datetime, timedelta, timezone
from .models import DiscordServer, DiscordChannel, DiscordUser, DiscordMessage, DiscordReaction, VoiceSession, VoiceTranscription
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Voice recording is not currently available in discord.py
# discord.sinks doesn't exist - voice recording requires custom implementation
# using raw UDP sockets and Opus decoding
VOICE_AVAILABLE = False
WaveSink = None
logger.info("Voice transcription is disabled. Voice recording requires custom implementation (discord.sinks doesn't exist in discord.py).")


class SummaryCog(commands.Cog):
    """Cog for summary/recap commands"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command(name='summary', aliases=['recap'])
    async def summary_command(self, ctx, *args):
        """
        Generate a fun, influencer-style summary of recent conversation in this channel 🦓
        
        Usage:
            !summary              - Summarize last 50 messages
            !summary 24           - Summarize messages from last 24 hours
            !summary 24 100       - Summarize last 100 messages from last 24 hours
        """
        async with ctx.typing():
            try:
                # Parse arguments
                hours = None
                limit = None
                
                if len(args) >= 1:
                    try:
                        hours = int(args[0])
                    except ValueError:
                        await ctx.send("🦓 **Oops!** The hours parameter should be a number. Usage: `!summary [hours] [limit]`")
                        return
                
                if len(args) >= 2:
                    try:
                        limit = int(args[1])
                    except ValueError:
                        await ctx.send("🦓 **Oops!** The limit parameter should be a number. Usage: `!summary [hours] [limit]`")
                        return
                
                # Get the channel from database
                channel = await self.bot.get_or_create_channel(ctx.channel)
                
                # Determine time range
                if hours:
                    time_cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
                else:
                    time_cutoff = None
                
                # Fetch messages
                def get_messages():
                    queryset = DiscordMessage.objects.filter(
                        channel=channel
                    ).exclude(
                        author__is_bot=True
                    ).select_related('author').order_by('-timestamp')
                    
                    if time_cutoff:
                        queryset = queryset.filter(timestamp__gte=time_cutoff)
                    
                    if limit:
                        queryset = queryset[:limit]
                    else:
                        queryset = queryset[:50]  # Default to 50 messages
                    
                    return list(queryset)
                
                messages = await sync_to_async(get_messages)()

                if not messages:
                    await ctx.send("🦓 **Hey there!** 👋 No messages found in this channel to summarize. Maybe try a different time range?")
                    return

                # Build conversation text and count unique authors from actual messages
                conversation_text = []
                unique_authors = set()
                for msg in reversed(messages):  # Reverse to get chronological order
                    author_name = msg.author.display_name or msg.author.username
                    unique_authors.add(author_name)  # Count from actual message objects
                    timestamp_str = msg.timestamp.strftime("%H:%M") if msg.timestamp else ""
                    content = msg.content.strip()
                    if content:
                        conversation_text.append(f"[{timestamp_str}] {author_name}: {content}")

                if not conversation_text:
                    await ctx.send("🦓 **Oops!** No text messages found to summarize. Everyone was just sharing images and files! 📸")
                    return

                full_conversation = "\n".join(conversation_text)

                # Generate summary using OpenAI - pass actual author count
                summary = await self.bot.generate_influencer_summary(full_conversation, len(messages), len(unique_authors))
                
                # Send summary
                embed = discord.Embed(
                    title="🦓 **Zebra Stream Recap** 🦓",
                    description=summary,
                    color=0x000000  # Black and white like a zebra!
                )
                embed.set_footer(text=f"Summarized {len(messages)} messages from #{ctx.channel.name}")
                
                await ctx.send(embed=embed)
                
            except Exception as e:
                logger.error(f"Error generating summary: {e}")
                await ctx.send(f"🦓 **Oops!** Something went wrong while creating the summary. Error: {str(e)}")


class VoiceRecorder:
    """Handles voice channel recording and transcription"""
    
    def __init__(self, bot):
        if not VOICE_AVAILABLE:
            raise ImportError(
                "Voice recording is not currently available. "
                "discord.py doesn't include voice recording capabilities (discord.sinks doesn't exist). "
                "Voice recording requires custom implementation using raw UDP sockets and Opus decoding."
            )
        
        self.bot = bot
        self.active_sessions: Dict[int, VoiceSession] = {}  # channel_id -> VoiceSession
        self.voice_clients: Dict[int, discord.VoiceClient] = {}  # channel_id -> VoiceClient
        self.audio_buffers: Dict[int, Dict[int, list]] = {}  # channel_id -> {user_id: [audio_packets]}
        self.transcription_tasks: Dict[int, asyncio.Task] = {}  # channel_id -> Task
    
    async def start_recording(self, voice_channel: discord.VoiceChannel, text_channel: discord.TextChannel) -> tuple[bool, str]:
        """Start recording a voice channel"""
        if not settings.VOICE_TRANSCRIPTION_ENABLED:
            return False, "Voice transcription is disabled"
        
        if voice_channel.id in self.active_sessions:
            return False, "Already recording in this channel"
        
        return False, (
            "🦓 **Voice recording not available**\n"
            "Voice transcription requires custom implementation. "
            "discord.py doesn't include voice recording capabilities. "
            "This feature needs to be implemented using raw UDP sockets and Opus decoding."
        )
    
    async def stop_recording(self, channel_id: int) -> tuple[bool, str]:
        """Stop recording a voice channel"""
        if channel_id not in self.active_sessions:
            return False, "No active recording in this channel"
        
        try:
            # Stop transcription task
            if channel_id in self.transcription_tasks:
                self.transcription_tasks[channel_id].cancel()
                try:
                    await self.transcription_tasks[channel_id]
                except asyncio.CancelledError:
                    pass
                del self.transcription_tasks[channel_id]
            
            # Stop recording
            if channel_id in self.voice_clients:
                voice_client = self.voice_clients[channel_id]
                await voice_client.disconnect()
                del self.voice_clients[channel_id]
            
            # Clean up audio buffers
            if channel_id in self.audio_buffers:
                del self.audio_buffers[channel_id]
            
            # Update session status
            session = self.active_sessions[channel_id]
            
            def update_session():
                session.status = 'completed'
                session.ended_at = datetime.now(timezone.utc)
                session.save()
            
            await sync_to_async(update_session)()
            del self.active_sessions[channel_id]
            
            logger.info(f"Stopped recording channel {channel_id}")
            return True, "🦓 **Recording stopped!** Generating notes..."
            
        except Exception as e:
            logger.error(f"Error stopping recording: {e}")
            return False, f"Error stopping recording: {str(e)}"
    
    async def _finished_callback(self, sink, user_id, *args):
        """Callback when audio packet is finished"""
        # This is called for each user's audio packet
        # We'll handle transcription in the main loop
        pass
    
    async def _transcribe_audio_loop(self, channel_id: int):
        """Continuously transcribe audio chunks"""
        # This would need to be implemented with custom audio packet handling
        # For now, this is a placeholder
        logger.warning("Voice transcription loop not implemented - requires custom audio packet handling")
        await asyncio.sleep(1)  # Placeholder
    
    async def _transcribe_audio(self, audio_file, user_id: int, session: VoiceSession) -> Optional[str]:
        """Transcribe audio using OpenAI Whisper"""
        if not settings.OPENAI_API_KEY:
            logger.warning("OpenAI API key not set, skipping transcription")
            return None
        
        if not os.path.exists(audio_file):
            logger.warning(f"Audio file not found: {audio_file}")
            return None
        
        try:
            from openai import AsyncOpenAI
            
            client = AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url="https://api.openai.com/v1",
                timeout=60.0
            )
            
            # Read audio file
            with open(audio_file, 'rb') as f:
                audio_data = f.read()
            
            # Skip if file is too small (likely silence or empty)
            if len(audio_data) < 1000:  # Less than 1KB is probably empty
                return None
            
            # Transcribe using Whisper
            transcript = await client.audio.transcriptions.create(
                model=settings.WHISPER_MODEL,
                file=(os.path.basename(audio_file), audio_data, 'audio/wav'),
                language='en'  # Can be made configurable
            )
            
            text = transcript.text.strip()
            if not text:
                return None
            
            return text
            
        except Exception as e:
            logger.error(f"Error in Whisper transcription: {e}")
            return None
    
    async def _store_transcription(self, text: str, session: VoiceSession, user_id: int):
        """Store transcription in database"""
        if not text:
            return
        
        try:
            # Get or create user
            def get_user():
                try:
                    return DiscordUser.objects.get(user_id=user_id)
                except ObjectDoesNotExist:
                    return None
            
            user = await sync_to_async(get_user)()
            
            def create_transcription():
                return VoiceTranscription.objects.create(
                    session=session,
                    user=user,
                    text=text,
                    timestamp=datetime.now(timezone.utc)
                )
            
            await sync_to_async(create_transcription)()
            logger.info(f"Stored transcription: {text[:50]}...")
            
        except Exception as e:
            logger.error(f"Error storing transcription: {e}")
    
    async def generate_notes(self, session_id: str) -> tuple[bool, str]:
        """Generate structured notes from a completed session"""
        try:
            def get_session():
                try:
                    return VoiceSession.objects.get(session_id=session_id, status='completed')
                except ObjectDoesNotExist:
                    return None
            
            session = await sync_to_async(get_session)()
            
            if not session:
                return False, "Session not found or not completed"
            
            if session.notes_generated:
                return True, session.notes or "Notes already generated"
            
            # Get all transcriptions
            def get_transcriptions():
                return list(VoiceTranscription.objects.filter(session=session).order_by('timestamp'))
            
            transcriptions = await sync_to_async(get_transcriptions)()
            
            if not transcriptions:
                return False, "No transcriptions found for this session"
            
            # Build full transcript
            transcript_text = []
            for trans in transcriptions:
                user_name = trans.user.display_name or trans.user.username if trans.user else "Unknown"
                timestamp_str = trans.timestamp.strftime("%H:%M:%S")
                transcript_text.append(f"[{timestamp_str}] {user_name}: {trans.text}")
            
            full_transcript = "\n".join(transcript_text)
            
            # Generate structured notes using OpenAI
            notes = await self._generate_structured_notes(full_transcript, len(transcriptions))
            
            # Store notes
            def update_session():
                session.notes = notes
                session.notes_generated = True
                session.save()
            
            await sync_to_async(update_session)()
            
            return True, notes
            
        except Exception as e:
            logger.error(f"Error generating notes: {e}")
            return False, f"Error generating notes: {str(e)}"
    
    async def _generate_structured_notes(self, transcript: str, segment_count: int) -> str:
        """Generate structured notes from transcript using OpenAI"""
        if not settings.OPENAI_API_KEY:
            return "⚠️ OpenAI API key not configured. Cannot generate structured notes."
        
        try:
            from openai import AsyncOpenAI
            
            client = AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url="https://api.openai.com/v1",
                timeout=60.0
            )
            
            prompt = f"""Analyze this voice conversation transcript and create structured notes. Extract:

1. **Action Items** - Tasks that need to be done (who, what, when)
2. **Decisions Made** - Important decisions and agreements
3. **Key Topics** - Main discussion points and themes
4. **Summary** - Brief overview of the conversation

Format the output clearly with sections. Be concise but comprehensive.

Transcript ({segment_count} segments):
{transcript[:15000]}  # Limit to avoid token limits

Create structured notes now:"""
            
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant that creates clear, structured meeting notes from transcripts."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1000,
                temperature=0.7
            )
            
            return response.choices[0].message.content.strip()
            
        except Exception as e:
            logger.error(f"Error generating structured notes: {e}")
            return f"⚠️ Error generating notes: {str(e)}"


class VoiceTranscriptionCog(commands.Cog):
    """Cog for voice channel transcription commands"""
    
    def __init__(self, bot):
        self.bot = bot
        if not VOICE_AVAILABLE:
            logger.warning("Voice recording not available - VoiceTranscriptionCog will not function")
            self.recorder = None
        else:
            self.recorder = VoiceRecorder(bot)
    
    @commands.command(name='join')
    async def join_command(self, ctx):
        """
        Join the voice channel you're currently in and start transcribing 🦓
        
        Usage:
            !join - Join your current voice channel
        """
        if not self.recorder:
            await ctx.send(
                "🦓 **Voice recording not available**\n"
                "Voice transcription requires custom implementation. "
                "discord.py doesn't include voice recording capabilities."
            )
            return
        
        if not ctx.author.voice:
            await ctx.send("🦓 **Oops!** You need to be in a voice channel first!")
            return
        
        voice_channel = ctx.author.voice.channel
        success, message = await self.recorder.start_recording(voice_channel, ctx.channel)
        await ctx.send(message)
    
    @commands.command(name='leave')
    async def leave_command(self, ctx):
        """
        Leave the current voice channel and stop recording 🦓
        
        Usage:
            !leave - Stop recording and leave voice channel
        """
        if not self.recorder:
            await ctx.send("🦓 **Voice recording not available**")
            return
        
        # Find active session in any channel the user might be in
        if ctx.author.voice:
            channel_id = ctx.author.voice.channel.id
            if channel_id in self.recorder.active_sessions:
                # Get session ID before stopping
                session = self.recorder.active_sessions.get(channel_id)
                session_id = session.session_id if session else None
                
                success, message = await self.recorder.stop_recording(channel_id)
                await ctx.send(message)
                
                # Generate notes automatically after leaving
                if session_id:
                    await asyncio.sleep(2)  # Brief delay for final transcriptions
                    notes_success, notes = await self.recorder.generate_notes(session_id)
                    if notes_success:
                        embed = discord.Embed(
                            title="🦓 **Meeting Notes** 🦓",
                            description=notes[:2000],  # Discord embed limit
                            color=0x000000
                        )
                        await ctx.send(embed=embed)
                return
        
        # If user not in voice, check if bot is recording anywhere
        if not self.recorder.active_sessions:
            await ctx.send("🦓 **Not recording** - I'm not in any voice channels right now!")
            return
        
        # Leave the first active session (or could be improved to list all)
        channel_id = list(self.recorder.active_sessions.keys())[0]
        session = self.recorder.active_sessions.get(channel_id)
        session_id = session.session_id if session else None
        
        success, message = await self.recorder.stop_recording(channel_id)
        await ctx.send(message)
        
        # Generate notes if session existed
        if session_id:
            await asyncio.sleep(2)
            notes_success, notes = await self.recorder.generate_notes(session_id)
            if notes_success:
                embed = discord.Embed(
                    title="🦓 **Meeting Notes** 🦓",
                    description=notes[:2000],
                    color=0x000000
                )
                await ctx.send(embed=embed)
    
    @commands.command(name='notes')
    async def notes_command(self, ctx, session_id: Optional[str] = None):
        """
        Generate structured notes from a completed voice session 🦓
        
        Usage:
            !notes - Generate notes for the most recent session
            !notes <session_id> - Generate notes for a specific session
        """
        async with ctx.typing():
            if session_id:
                success, notes = await self.recorder.generate_notes(session_id)
            else:
                # Get most recent completed session
                def get_recent_session():
                    return VoiceSession.objects.filter(
                        status='completed',
                        channel__server__server_id=ctx.guild.id
                    ).order_by('-ended_at').first()
                
                session = await sync_to_async(get_recent_session)()
                
                if not session:
                    await ctx.send("🦓 **No completed sessions found!** Try specifying a session ID.")
                    return
                
                success, notes = await self.recorder.generate_notes(session.session_id)
            
            if success:
                # Split if too long for embed
                if len(notes) > 2000:
                    # Send as multiple embeds or as file
                    embed = discord.Embed(
                        title="🦓 **Meeting Notes** 🦓",
                        description=notes[:2000],
                        color=0x000000
                    )
                    await ctx.send(embed=embed)
                    if len(notes) > 2000:
                        await ctx.send(f"```\n{notes[2000:4000]}\n```")
                else:
                    embed = discord.Embed(
                        title="🦓 **Meeting Notes** 🦓",
                        description=notes,
                        color=0x000000
                    )
                    await ctx.send(embed=embed)
            else:
                await ctx.send(f"🦓 **Error:** {notes}")


class DiscordIntelligenceBot(commands.Bot):
    """Discord bot for monitoring and storing server activity"""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        intents.reactions = True
        
        super().__init__(command_prefix='!', intents=intents)
    
    async def on_ready(self):
        """Called when the bot is ready"""
        logger.info(f'{self.user} has connected to Discord!')
        
        # Sync guilds and channels to database
        await self.sync_guild_data()
        
        # Load the summary command Cog
        try:
            await self.add_cog(SummaryCog(self))
            logger.info('SummaryCog loaded successfully')
        except Exception as e:
            logger.error(f'Error loading SummaryCog: {e}')
        
        # Load the voice transcription Cog
        if settings.VOICE_TRANSCRIPTION_ENABLED:
            if VOICE_AVAILABLE:
                try:
                    await self.add_cog(VoiceTranscriptionCog(self))
                    logger.info('VoiceTranscriptionCog loaded successfully')
                except Exception as e:
                    logger.error(f'Error loading VoiceTranscriptionCog: {e}')
            else:
                logger.warning('Voice transcription is enabled in settings but not available. Voice recording requires custom implementation.')
        else:
            logger.info('Voice transcription is disabled in settings')
        
        # Log registered commands for debugging (after Cog is loaded)
        logger.info(f'Registered commands: {[cmd.name for cmd in self.commands]}')
    
    async def on_message(self, message):
        """Handle incoming messages"""
        if message.author.bot:
            return
        
        await self.store_message(message)
        await self.process_commands(message)
    
    async def on_message_edit(self, before, after):
        """Handle message edits"""
        if after.author.bot:
            return
        
        try:
            discord_message = await sync_to_async(DiscordMessage.objects.get)(message_id=after.id)
            discord_message.content = after.content
            discord_message.edited_timestamp = after.edited_at
            await sync_to_async(discord_message.save)()
            logger.info(f"Updated message {after.id}")
        except ObjectDoesNotExist:
            logger.warning(f"Message {after.id} not found for edit")
    
    async def on_message_delete(self, message):
        """Handle message deletions"""
        if message.author.bot:
            return
        
        try:
            await sync_to_async(DiscordMessage.objects.filter(message_id=message.id).delete)()
            logger.info(f"Deleted message {message.id}")
        except Exception as e:
            logger.error(f"Error deleting message {message.id}: {e}")
    
    async def on_reaction_add(self, reaction, user):
        """Handle reaction additions"""
        if user.bot:
            return
        
        await self.store_reaction(reaction, user)
    
    async def on_reaction_remove(self, reaction, user):
        """Handle reaction removals"""
        if user.bot:
            return
        
        await self.update_reaction_count(reaction)
    
    async def sync_guild_data(self):
        """Sync guild, channel, and user data to database"""
        for guild in self.guilds:
            await self.store_guild(guild)
            
            for channel in guild.channels:
                await self.store_channel(channel, guild)
            
            for member in guild.members:
                await self.store_user(member)
    
    async def store_guild(self, guild):
        """Store guild information"""
        server, created = await sync_to_async(DiscordServer.objects.get_or_create)(
            server_id=guild.id,
            defaults={'name': guild.name}
        )
        if not created and server.name != guild.name:
            server.name = guild.name
            await sync_to_async(server.save)()
        
        logger.info(f"{'Created' if created else 'Updated'} guild: {guild.name}")
    
    async def store_channel(self, channel, guild):
        """Store channel information"""
        # Support TextChannel, VoiceChannel, CategoryChannel, and Thread
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, 
                                    discord.CategoryChannel, discord.Thread)):
            return
        
        # For Threads, use the thread's guild or parent channel's guild
        if isinstance(channel, discord.Thread):
            if hasattr(channel, 'guild') and channel.guild:
                guild = channel.guild
            elif hasattr(channel, 'parent') and channel.parent and hasattr(channel.parent, 'guild'):
                guild = channel.parent.guild
        
        if not guild:
            logger.warning(f"Cannot store channel {channel.id}: no guild available")
            return
        
        server = await sync_to_async(DiscordServer.objects.get)(server_id=guild.id)
        channel_type = channel.type.name if hasattr(channel.type, 'name') else str(channel.type)
        
        # For threads, use a descriptive type
        if isinstance(channel, discord.Thread):
            channel_type = f"thread_{channel_type}"
        
        discord_channel, created = await sync_to_async(DiscordChannel.objects.get_or_create)(
            channel_id=channel.id,
            defaults={
                'server': server,
                'name': channel.name,
                'channel_type': channel_type
            }
        )
        
        if not created and (discord_channel.name != channel.name or discord_channel.channel_type != channel_type):
            discord_channel.name = channel.name
            discord_channel.channel_type = channel_type
            await sync_to_async(discord_channel.save)()
        
        logger.info(f"{'Created' if created else 'Updated'} channel: #{channel.name}")
    
    async def store_user(self, member):
        """Store user information"""
        discriminator = member.discriminator if member.discriminator != '0' else ''
        
        user, created = await sync_to_async(DiscordUser.objects.get_or_create)(
            user_id=member.id,
            defaults={
                'username': member.name,
                'display_name': member.display_name,
                'discriminator': discriminator,
                'avatar_url': str(member.avatar.url) if member.avatar else '',
                'is_bot': member.bot
            }
        )
        
        if not created:
            updated = False
            if user.username != member.name:
                user.username = member.name
                updated = True
            if user.display_name != member.display_name:
                user.display_name = member.display_name
                updated = True
            if user.avatar_url != (str(member.avatar.url) if member.avatar else ''):
                user.avatar_url = str(member.avatar.url) if member.avatar else ''
                updated = True
            
            if updated:
                await sync_to_async(user.save)()
    
    async def store_message(self, message):
        """Store message information"""
        try:
            # Get or create channel
            channel = await self.get_or_create_channel(message.channel)
            
            # Get or create user
            user = await self.get_or_create_user(message.author)
            
            # Create message
            discord_message, created = await sync_to_async(DiscordMessage.objects.get_or_create)(
                message_id=message.id,
                defaults={
                    'channel': channel,
                    'author': user,
                    'content': message.content,
                    'timestamp': message.created_at,
                    'edited_timestamp': message.edited_at,
                    'is_pinned': message.pinned,
                    'has_attachments': len(message.attachments) > 0,
                    'attachment_count': len(message.attachments),
                    'has_embeds': len(message.embeds) > 0,
                    'embed_count': len(message.embeds)
                }
            )
            
            if created:
                logger.info(f"Stored message from {user.username} in #{channel.name}")
            
            # Store reactions
            for reaction in message.reactions:
                await self.store_reaction(reaction, None)
        
        except Exception as e:
            logger.error(f"Error storing message {message.id}: {e}")
    
    async def get_or_create_channel(self, channel):
        """Get or create channel in database"""
        try:
            return await sync_to_async(DiscordChannel.objects.get)(channel_id=channel.id)
        except ObjectDoesNotExist:
            # Try to store the channel
            guild = getattr(channel, 'guild', None)
            await self.store_channel(channel, guild)
            
            # Check if channel was created (store_channel might return early for unsupported types)
            try:
                return await sync_to_async(DiscordChannel.objects.get)(channel_id=channel.id)
            except ObjectDoesNotExist:
                # Fallback: For Thread channels, use the parent channel
                if isinstance(channel, discord.Thread) and hasattr(channel, 'parent') and channel.parent:
                    logger.warning(f"Thread channel {channel.id} not stored, using parent channel {channel.parent.id}")
                    return await self.get_or_create_channel(channel.parent)
                
                # If still not found and it's an unsupported type, log and raise
                logger.error(f"Could not store channel {channel.id} of type {type(channel).__name__}")
                raise
    
    async def get_or_create_user(self, member):
        """Get or create user in database"""
        try:
            return await sync_to_async(DiscordUser.objects.get)(user_id=member.id)
        except ObjectDoesNotExist:
            await self.store_user(member)
            return await sync_to_async(DiscordUser.objects.get)(user_id=member.id)
    
    async def store_reaction(self, reaction, user):
        """Store reaction information"""
        try:
            message = await sync_to_async(DiscordMessage.objects.get)(message_id=reaction.message.id)
            
            reaction_obj, created = await sync_to_async(DiscordReaction.objects.get_or_create)(
                message=message,
                emoji_name=reaction.emoji.name if reaction.emoji.name else str(reaction.emoji),
                emoji_id=reaction.emoji.id if hasattr(reaction.emoji, 'id') else None,
                defaults={'count': reaction.count}
            )
            
            if not created:
                reaction_obj.count = reaction.count
                await sync_to_async(reaction_obj.save)()
        
        except ObjectDoesNotExist:
            logger.warning(f"Message {reaction.message.id} not found for reaction")
        except Exception as e:
            logger.error(f"Error storing reaction: {e}")
    
    async def update_reaction_count(self, reaction):
        """Update reaction count when removed"""
        try:
            message = await sync_to_async(DiscordMessage.objects.get)(message_id=reaction.message.id)
            
            reaction_obj = await sync_to_async(DiscordReaction.objects.get)(
                message=message,
                emoji_name=reaction.emoji.name if reaction.emoji.name else str(reaction.emoji),
                emoji_id=reaction.emoji.id if hasattr(reaction.emoji, 'id') else None
            )
            
            reaction_obj.count = reaction.count
            await sync_to_async(reaction_obj.save)()
        
        except ObjectDoesNotExist:
            pass
        except Exception as e:
            logger.error(f"Error updating reaction count: {e}")
    
    async def generate_influencer_summary(self, conversation: str, message_count: int, author_count: int = None) -> str:
        """
        Generate an influencer-style summary using OpenAI API
        Falls back to basic summary if OpenAI is not configured
        """
        # Debug: Check if API key is loaded
        api_key_present = bool(settings.OPENAI_API_KEY)
        logger.info(f'OpenAI API key present: {api_key_present}')
        
        if not settings.OPENAI_API_KEY:
            logger.warning('OPENAI_API_KEY not set in settings, using basic summary')
            # Fallback: Basic summary without AI
            return self._generate_basic_summary(conversation, message_count, author_count)
        
        try:
            from openai import AsyncOpenAI
            
            # Initialize OpenAI client with explicit base_url
            # Explicitly set to ensure correct endpoint (some SDK versions may have issues)
            client = AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url="https://api.openai.com/v1",
                timeout=30.0
            )
            
            logger.info(f"OpenAI client base_url: {client.base_url}")
            logger.info("Attempting to call OpenAI API...")
            
            prompt = f"""You are a successful influencer Zebra 🦓 who loves to summarize Discord conversations in a fun, engaging, and entertaining way. 

Your personality:
- Energetic and enthusiastic
- Uses emojis naturally (especially 🦓)
- Makes things sound exciting and interesting
- Uses modern influencer language (but keep it PG)
- Highlights the most interesting parts of the conversation
- Makes it feel like you're recapping something epic

Here's a Discord conversation from the last {message_count} messages with {author_count or 'several'} people. Create a fun, engaging summary in the style of a successful influencer Zebra:

{conversation[:8000]}  # Limit to avoid token limits

Create a summary that's:
- 2-4 paragraphs long
- Engaging and fun to read
- Highlights key topics and interesting moments
- Uses your influencer Zebra personality 🦓
- Ends with something encouraging or positive

Start with something catchy and energetic!"""
            
            # Use chat completions endpoint per OpenAI API documentation
            response = await client.chat.completions.create(
                model="gpt-4o-mini",  # Using mini for cost efficiency
                messages=[
                    {"role": "system", "content": "You are a successful influencer Zebra who loves to summarize Discord conversations in a fun, engaging way. You use emojis naturally and make everything sound exciting!"},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=500,
                temperature=0.8
            )
            
            summary = response.choices[0].message.content.strip()
            return summary
            
        except ImportError:
            logger.warning("OpenAI package not installed, using basic summary")
            return self._generate_basic_summary(conversation, message_count, author_count)
        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__
            logger.error(f"Error calling OpenAI API: {error_msg}")
            logger.error(f"Error type: {error_type}")
            
            # Try to get more details from the error object
            try:
                if hasattr(e, 'response') and e.response:
                    logger.error(f"Full error response: {e.response}")
                if hasattr(e, 'body') and e.body:
                    logger.error(f"Error body: {e.body}")
                if hasattr(e, 'status_code'):
                    logger.error(f"Status code: {e.status_code}")
            except:
                pass
            
            # Check for quota/billing issues
            if "quota" in error_msg.lower() or "insufficient_quota" in error_msg.lower() or "429" in error_msg:
                logger.error("=" * 60)
                logger.error("OPENAI QUOTA/BILLING ISSUE DETECTED")
                logger.error(f"Error details: {error_msg}")
                logger.error("")
                logger.error("NOTE: Even if your dashboard shows budget available, you may need:")
                logger.error("1. Verify your payment method is active and valid")
                logger.error("2. Check for account-level soft limits (separate from project budget)")
                logger.error("3. Ensure the API key belongs to the same project with the budget")
                logger.error("4. Check account verification status at https://platform.openai.com/account")
                logger.error("5. Try regenerating your API key at https://platform.openai.com/api-keys")
                logger.error("")
                logger.error("Common causes:")
                logger.error("- Payment method needs verification despite having budget")
                logger.error("- Account has soft limits enabled")
                logger.error("- API key is from a different project than the budget")
                logger.error("=" * 60)
            
            # Check if it's a 404 or nginx error (proxy/firewall issue)
            elif "404" in error_msg or "Not Found" in error_msg or "nginx" in error_msg.lower():
                logger.error("=" * 60)
                logger.error("NETWORK/PROXY ISSUE DETECTED")
                logger.error("The nginx 404 error indicates a proxy or firewall is blocking OpenAI API requests.")
                logger.error("This is NOT a code issue - it's a network infrastructure problem.")
                logger.error("Possible solutions:")
                logger.error("1. Check if you're behind a corporate firewall/proxy")
                logger.error("2. Try using a VPN or different network")
                logger.error("3. Contact your network administrator about allowing api.openai.com")
                logger.error("4. Check if there's a proxy configuration blocking the requests")
                logger.error("=" * 60)
            
            # Check if it's a quota issue for user-facing message
            is_quota_issue = "quota" in error_msg.lower() or "insufficient_quota" in error_msg.lower()
            
            # Still return basic summary but indicate API key was set and the specific issue
            return self._generate_basic_summary(
                conversation, 
                message_count, 
                author_count, 
                api_key_was_set=True, 
                quota_issue=is_quota_issue
            )
    
    def _generate_basic_summary(self, conversation: str, message_count: int, author_count: int = None, api_key_was_set: bool = False, quota_issue: bool = False) -> str:
        """Generate a basic summary without AI"""
        # Use provided author_count if available, otherwise try to parse from conversation
        if author_count is None:
            lines = conversation.split('\n')
            unique_authors = set()
            for line in lines:
                if ':' in line:
                    author = line.split(':')[0].split('] ')[-1] if '] ' in line else line.split(':')[0]
                    unique_authors.add(author.strip())
            author_count = len(unique_authors)
        
        base_message = f"""🦓 **Hey everyone!** 👋 Just caught up on the conversation and wow, there's been some action! 

We had **{message_count} messages** with **{author_count} different people** chiming in. The conversation covered a bunch of topics - definitely some interesting discussions happening!"""
        
        if api_key_was_set:
            if quota_issue:
                return f"""{base_message}

⚠️ **AI Summary Unavailable**: Even though your dashboard shows budget available, OpenAI is reporting insufficient quota. This usually means:
• Payment method needs verification (check https://platform.openai.com/account/billing)
• Account-level soft limits are blocking requests (separate from project budget)
• API key might be from a different project than your budget
• Try regenerating your API key at https://platform.openai.com/api-keys

Check the bot logs for detailed error info! 🦓✨"""
            else:
                return f"""{base_message}

⚠️ **AI Summary Unavailable**: Your API key is configured, but OpenAI API requests are failing. Check your network settings or OpenAI API status. 🦓✨"""
        else:
            return f"""{base_message}

Want a more detailed AI-powered summary? Set up your OPENAI_API_KEY in the .env file and I'll give you the full influencer Zebra treatment! 🦓✨"""
    
    async def assign_admin_role(self, user_id, guild_id=None, reason=None):
        """
        Assign admin role to a user
        
        Args:
            user_id: Discord user ID to assign the role to
            guild_id: Optional guild ID (uses DISCORD_GUILD_ID from settings if not provided)
            reason: Optional reason for the role assignment
        
        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            # Get guild ID
            if not guild_id:
                if not settings.DISCORD_GUILD_ID:
                    return False, "DISCORD_GUILD_ID not set in settings"
                guild_id = int(settings.DISCORD_GUILD_ID)
            else:
                guild_id = int(guild_id)
            
            # Get the guild
            guild = self.get_guild(guild_id)
            if not guild:
                return False, f"Guild {guild_id} not found"
            
            # Get the member
            member = guild.get_member(user_id)
            if not member:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    return False, f"User {user_id} not found in guild"
            
            # Find admin role
            admin_role = None
            
            # First, try to find role by name (case-insensitive)
            for role in guild.roles:
                if role.name.lower() in ['admin', 'administrator']:
                    admin_role = role
                    break
            
            # If not found by name, find role with administrator permissions
            if not admin_role:
                for role in guild.roles:
                    if role.permissions.administrator:
                        admin_role = role
                        break
            
            if not admin_role:
                return False, "Admin role not found in guild"
            
            # Check if user already has the role
            if admin_role in member.roles:
                return True, f"{member.display_name} already has the {admin_role.name} role"
            
            # Assign the role
            await member.add_roles(admin_role, reason=reason or 'Admin role assignment')
            logger.info(f"Assigned {admin_role.name} role to {member.display_name} (ID: {user_id})")
            return True, f"Successfully assigned {admin_role.name} role to {member.display_name}"
            
        except discord.Forbidden:
            return False, "Bot does not have permission to assign roles"
        except discord.HTTPException as e:
            return False, f"Discord API error: {e}"
        except Exception as e:
            logger.error(f"Error assigning admin role: {e}")
            return False, f"Error: {e}"


# Global bot instance
bot = None


async def start_bot():
    """Start the Discord bot"""
    global bot
    
    if not settings.DISCORD_BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN not set in environment variables")
        return
    
    bot = DiscordIntelligenceBot()
    
    try:
        await bot.start(settings.DISCORD_BOT_TOKEN)
    except Exception as e:
        logger.error(f"Error starting bot: {e}")


def run_bot():
    """Run the bot in the event loop"""
    asyncio.run(start_bot())
