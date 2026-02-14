# Discord Intelligence Dashboard 🦓

![ZBOT Logo](ZBOT.jpg)

A Django-based project intelligence dashboard that monitors Discord server activity and provides insights into team coordination. 🦓🦓🦓

## Features 🦓

- **Real-time Discord Monitoring**: Bot listens to all server conversations 🦓
- **Activity Dashboard**: Timeline view of recent messages with channel breakdown
- **Statistics & Analytics**: Message counts, active users, channel activity
- **Database Flexibility**: SQLite for development, PostgreSQL for production
- **Modern UI**: Responsive Bootstrap-based interface with auto-refresh

## Quick Start 🦓

1. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment**
   ```bash
   cp env.example .env
   # Edit .env with your Discord bot token and settings
   ```

3. **Set Up Database**
   ```bash
   python manage.py migrate
   ```

4. **Create Superuser (Optional)**
   ```bash
   python manage.py createsuperuser
   ```

5. **Run the Application**
   ```bash
   # Start Django server
   python manage.py runserver
   
   # In another terminal, start the Discord bot
   python manage.py runbot --daemon
   ```

6. **Access Dashboard** 🦓
   - Dashboard: http://localhost:8000/
   - Admin: http://localhost:8000/admin/

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DISCORD_BOT_TOKEN` | Your Discord bot token | Required |
| `DISCORD_GUILD_ID` | Discord server ID | Required |
| `USE_POSTGRESQL` | Use PostgreSQL instead of SQLite | False |
| `SECRET_KEY` | Django secret key | Generated |
| `DEBUG` | Enable debug mode | True |

## Database Models

- **DiscordServer**: Server/guild information
- **DiscordChannel**: Channel details and types
- **DiscordUser**: User profiles and metadata
- **DiscordMessage**: Message content, timestamps, attachments
- **DiscordReaction**: Message reactions and emoji data

## Bot Features 🦓

- Monitors all channels in the server 🦓
- Stores messages, reactions, and user activity
- Handles message edits and deletions
- Syncs server, channel, and user data
- Runs asynchronously with Django

## Dashboard Features 🦓

- **Timeline**: Recent messages with author and channel info
- **Statistics**: Total messages, users, daily activity 🦓
- **Top Channels**: Most active channels by message count
- **Top Users**: Most active users (excluding bots)
- **Auto-refresh**: Updates every 30 seconds

## Production Deployment

1. Set `USE_POSTGRESQL=True` in `.env`
2. Configure PostgreSQL database settings
3. Set `DEBUG=False` and configure `ALLOWED_HOSTS`
4. Run `python manage.py collectstatic`
5. Use a production WSGI server (gunicorn, uwsgi)

## Management Commands

- `python manage.py runbot`: Start Discord bot
- `python manage.py runbot --daemon`: Run bot in background
- `python manage.py migrate`: Apply database migrations
- `python manage.py createsuperuser`: Create admin user

## API Endpoints

- `GET /api/messages/`: Recent messages (supports `limit`, `channel_id`, `user_id`)
- `GET /api/stats/`: Dashboard statistics and analytics

## Architecture

```
discord_intelligence/
├── bot/                    # Discord bot logic
│   ├── models.py          # Database models
│   ├── discord_bot.py     # Bot implementation
│   └── management/        # Django commands
├── dashboard/             # Web interface
│   ├── views.py           # Dashboard logic
│   ├── templates/         # HTML templates
│   └── urls.py           # URL routing
├── analytics/             # Future analytics features
└── discord_intelligence/  # Django settings
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## License

MIT License - see LICENSE file for details.


