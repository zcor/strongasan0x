from django.db import models


class DiscordServer(models.Model):
    """Represents a Discord server/guild"""
    server_id = models.BigIntegerField(unique=True)
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'discord_servers'
    
    def __str__(self):
        return self.name


class DiscordChannel(models.Model):
    """Represents a Discord channel"""
    channel_id = models.BigIntegerField(unique=True)
    server = models.ForeignKey(DiscordServer, on_delete=models.CASCADE, related_name='channels')
    name = models.CharField(max_length=100)
    channel_type = models.CharField(max_length=20)  # text, voice, category, etc.
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'discord_channels'
    
    def __str__(self):
        return f"{self.server.name} - #{self.name}"


class DiscordUser(models.Model):
    """Represents a Discord user"""
    user_id = models.BigIntegerField(unique=True)
    username = models.CharField(max_length=100)
    display_name = models.CharField(max_length=100, blank=True)
    discriminator = models.CharField(max_length=10, blank=True)
    avatar_url = models.URLField(blank=True)
    is_bot = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'discord_users'
    
    def __str__(self):
        return f"{self.username}#{self.discriminator}" if self.discriminator else self.username


class DiscordMessage(models.Model):
    """Represents a Discord message"""
    message_id = models.BigIntegerField(unique=True)
    channel = models.ForeignKey(DiscordChannel, on_delete=models.CASCADE, related_name='messages')
    author = models.ForeignKey(DiscordUser, on_delete=models.CASCADE, related_name='messages')
    content = models.TextField()
    timestamp = models.DateTimeField()
    edited_timestamp = models.DateTimeField(null=True, blank=True)
    is_pinned = models.BooleanField(default=False)
    has_attachments = models.BooleanField(default=False)
    attachment_count = models.PositiveIntegerField(default=0)
    has_embeds = models.BooleanField(default=False)
    embed_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'discord_messages'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['timestamp']),
            models.Index(fields=['channel', 'timestamp']),
            models.Index(fields=['author', 'timestamp']),
        ]
    
    def __str__(self):
        return f"{self.author.username}: {self.content[:50]}..."


class DiscordReaction(models.Model):
    """Represents reactions on Discord messages"""
    message = models.ForeignKey(DiscordMessage, on_delete=models.CASCADE, related_name='reactions')
    emoji_name = models.CharField(max_length=100)
    emoji_id = models.BigIntegerField(null=True, blank=True)
    count = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'discord_reactions'
        unique_together = ['message', 'emoji_name', 'emoji_id']
    
    def __str__(self):
        return f"{self.emoji_name} x{self.count} on message {self.message.message_id}"


class VoiceSession(models.Model):
    """Represents a voice channel recording session"""
    session_id = models.CharField(max_length=100, unique=True)
    channel = models.ForeignKey(DiscordChannel, on_delete=models.CASCADE, related_name='voice_sessions')
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default='active')  # active, completed, cancelled
    notes = models.TextField(blank=True, null=True)
    notes_generated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'voice_sessions'
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['channel', 'started_at']),
            models.Index(fields=['status']),
        ]
    
    def __str__(self):
        return f"Voice session in #{self.channel.name} - {self.started_at}"


class VoiceTranscription(models.Model):
    """Represents a transcription segment from a voice session"""
    session = models.ForeignKey(VoiceSession, on_delete=models.CASCADE, related_name='transcriptions')
    user = models.ForeignKey(DiscordUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='voice_transcriptions')
    text = models.TextField()
    timestamp = models.DateTimeField()
    duration = models.FloatField(null=True, blank=True)  # Duration in seconds
    confidence = models.FloatField(null=True, blank=True)  # Transcription confidence score
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'voice_transcriptions'
        ordering = ['timestamp']
        indexes = [
            models.Index(fields=['session', 'timestamp']),
            models.Index(fields=['user', 'timestamp']),
        ]
    
    def __str__(self):
        user_name = self.user.username if self.user else "Unknown"
        return f"{user_name}: {self.text[:50]}..."