from django.core.management.base import BaseCommand
from django.conf import settings
from django.db.models import Q
from asgiref.sync import sync_to_async
import discord
from discord.ext import commands
import asyncio
import logging
from bot.models import DiscordUser

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Assign Discord admin role to Mr. Rex (co-founder)'

    def handle(self, *args, **options):
        # Check required settings
        if not settings.DISCORD_BOT_TOKEN:
            self.stdout.write(
                self.style.ERROR('DISCORD_BOT_TOKEN not set in environment variables')
            )
            return

        if not settings.DISCORD_GUILD_ID:
            self.stdout.write(
                self.style.ERROR('DISCORD_GUILD_ID not set in environment variables')
            )
            return

        # Run the async function
        asyncio.run(self.assign_admin_role())

    async def assign_admin_role(self):
        """Assign admin role to Mr. Rex"""
        # Capture stdout and style for use in nested functions
        stdout = self.stdout
        style = self.style
        
        # Find Mr. Rex in the database
        try:
            # Search for user by username (case-insensitive) - use sync_to_async for Django ORM
            def get_user():
                return DiscordUser.objects.filter(
                    Q(username__icontains='rex') | Q(display_name__icontains='rex'),
                    is_bot=False
                ).first()
            
            user = await sync_to_async(get_user)()

            if not user:
                stdout.write(
                    style.ERROR('Mr. Rex not found in database')
                )
                return

            stdout.write(f'Found user: {user.username} (ID: {user.user_id})')

            # Create bot instance with required intents
            intents = discord.Intents.default()
            intents.guilds = True
            intents.members = True
            bot = commands.Bot(intents=intents, command_prefix='!')

            @bot.event
            async def on_ready():
                try:
                    # Get the guild
                    guild_id = int(settings.DISCORD_GUILD_ID)
                    guild = bot.get_guild(guild_id)
                    
                    if not guild:
                        stdout.write(
                            style.ERROR(f'Guild {guild_id} not found')
                        )
                        await bot.close()
                        return

                    stdout.write(f'Connected to guild: {guild.name}')

                    # Get the member
                    member = guild.get_member(user.user_id)
                    if not member:
                        # Try fetching if not in cache
                        try:
                            member = await guild.fetch_member(user.user_id)
                        except discord.NotFound:
                            stdout.write(
                                style.ERROR(f'User {user.username} not found in guild')
                            )
                            await bot.close()
                            return

                    stdout.write(f'Found member: {member.display_name}')

                    # Check bot permissions
                    bot_member = guild.get_member(bot.user.id)
                    if not bot_member:
                        stdout.write(
                            style.ERROR('Bot member not found in guild')
                        )
                        await bot.close()
                        return
                    
                    if not bot_member.guild_permissions.manage_roles:
                        stdout.write(
                            style.ERROR('Bot does not have "Manage Roles" permission in this server')
                        )
                        stdout.write(
                            style.WARNING('Please ensure the bot role has "Manage Roles" enabled in Server Settings > Roles')
                        )
                        await bot.close()
                        return
                    
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
                        stdout.write(
                            style.ERROR('Admin role not found in guild')
                        )
                        stdout.write(
                            style.WARNING('Please create a role named "Admin" or "Administrator" with administrator permissions')
                        )
                        await bot.close()
                        return

                    stdout.write(f'Found admin role: {admin_role.name} (Position: {admin_role.position})')
                    stdout.write(f'Bot role position: {bot_member.top_role.position}')

                    # Check if bot's role is high enough to assign this role
                    if bot_member.top_role.position <= admin_role.position:
                        stdout.write(
                            style.ERROR(
                                f'Bot role position ({bot_member.top_role.position}) must be higher than '
                                f'admin role position ({admin_role.position}) to assign it'
                            )
                        )
                        stdout.write(
                            style.WARNING(
                                'Please move the bot role above the admin role in Server Settings > Roles'
                            )
                        )
                        await bot.close()
                        return

                    # Check if role is managed by integration
                    if admin_role.managed:
                        stdout.write(
                            style.ERROR(
                                f'Admin role "{admin_role.name}" is managed by an integration and cannot be assigned by bots'
                            )
                        )
                        await bot.close()
                        return

                    # Check if user already has the role
                    if admin_role in member.roles:
                        stdout.write(
                            style.WARNING(f'{member.display_name} already has the {admin_role.name} role')
                        )
                        await bot.close()
                        return

                    # Assign the role
                    await member.add_roles(admin_role, reason='Co-founder admin assignment')
                    stdout.write(
                        style.SUCCESS(
                            f'Successfully assigned {admin_role.name} role to {member.display_name}'
                        )
                    )

                    await bot.close()
                except discord.Forbidden as e:
                    stdout.write(
                        style.ERROR(f'Discord API Forbidden error: {e}')
                    )
                    stdout.write(
                        style.WARNING('This usually means:')
                    )
                    stdout.write(
                        style.WARNING('  1. Bot lacks "Manage Roles" permission')
                    )
                    stdout.write(
                        style.WARNING('  2. Bot role is not high enough in hierarchy')
                    )
                    stdout.write(
                        style.WARNING('  3. Target role is managed by an integration')
                    )
                    await bot.close()
                except discord.HTTPException as e:
                    stdout.write(
                        style.ERROR(f'Discord API error: {e}')
                    )
                    await bot.close()
                except Exception as e:
                    stdout.write(
                        style.ERROR(f'Error: {e}')
                    )
                    await bot.close()

            # Start the bot
            try:
                await bot.start(settings.DISCORD_BOT_TOKEN)
            except Exception as e:
                stdout.write(
                    style.ERROR(f'Failed to start bot: {e}')
                )

        except Exception as e:
            stdout.write(
                style.ERROR(f'Error: {e}')
            )

