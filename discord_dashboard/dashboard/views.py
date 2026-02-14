from django.shortcuts import render
from django.http import JsonResponse
from django.db.models import Count, Q
from django.utils import timezone
from datetime import timedelta
from bot.models import DiscordMessage, DiscordChannel, DiscordUser, DiscordServer


def home(request):
    """Main dashboard homepage"""
    # Get recent messages (last 24 hours)
    recent_cutoff = timezone.now() - timedelta(hours=24)
    recent_messages = DiscordMessage.objects.filter(
        timestamp__gte=recent_cutoff
    ).select_related('author', 'channel', 'channel__server').order_by('-timestamp')[:50]
    
    # Get statistics
    stats = get_dashboard_stats()
    
    # Get channel activity
    channel_stats = get_channel_stats()
    
    # Get user activity
    user_stats = get_user_stats()
    
    context = {
        'recent_messages': recent_messages,
        'stats': stats,
        'channel_stats': channel_stats,
        'user_stats': user_stats,
    }
    
    return render(request, 'dashboard/home.html', context)


def get_dashboard_stats():
    """Get overall dashboard statistics"""
    now = timezone.now()
    today = now.date()
    week_ago = now - timedelta(days=7)
    day_ago = now - timedelta(days=1)
    
    total_messages = DiscordMessage.objects.count()
    total_users = DiscordUser.objects.count()
    total_channels = DiscordChannel.objects.count()
    total_servers = DiscordServer.objects.count()
    
    messages_today = DiscordMessage.objects.filter(timestamp__date=today).count()
    messages_this_week = DiscordMessage.objects.filter(timestamp__gte=week_ago).count()
    messages_last_24h = DiscordMessage.objects.filter(timestamp__gte=day_ago).count()
    
    active_users_today = DiscordUser.objects.filter(
        messages__timestamp__date=today
    ).distinct().count()
    
    active_users_week = DiscordUser.objects.filter(
        messages__timestamp__gte=week_ago
    ).distinct().count()
    
    return {
        'total_messages': total_messages,
        'total_users': total_users,
        'total_channels': total_channels,
        'total_servers': total_servers,
        'messages_today': messages_today,
        'messages_this_week': messages_this_week,
        'messages_last_24h': messages_last_24h,
        'active_users_today': active_users_today,
        'active_users_week': active_users_week,
    }


def get_channel_stats():
    """Get channel activity statistics"""
    now = timezone.now()
    week_ago = now - timedelta(days=7)
    
    # Top channels by message count (last 7 days)
    top_channels = DiscordChannel.objects.annotate(
        message_count=Count('messages', filter=Q(messages__timestamp__gte=week_ago))
    ).filter(message_count__gt=0).order_by('-message_count')[:10]
    
    # Channel activity over time (last 7 days)
    channel_activity = []
    for i in range(7):
        date = (now - timedelta(days=i)).date()
        day_messages = DiscordMessage.objects.filter(
            timestamp__date=date
        ).values('channel__name').annotate(
            count=Count('id')
        ).order_by('-count')[:5]
        
        channel_activity.append({
            'date': date.strftime('%Y-%m-%d'),
            'channels': list(day_messages)
        })
    
    return {
        'top_channels': top_channels,
        'channel_activity': channel_activity,
    }


def get_user_stats():
    """Get user activity statistics"""
    now = timezone.now()
    week_ago = now - timedelta(days=7)
    
    # Top users by message count (last 7 days)
    top_users = DiscordUser.objects.annotate(
        message_count=Count('messages', filter=Q(messages__timestamp__gte=week_ago))
    ).filter(message_count__gt=0, is_bot=False).order_by('-message_count')[:10]
    
    # User activity over time (last 7 days)
    user_activity = []
    for i in range(7):
        date = (now - timedelta(days=i)).date()
        day_messages = DiscordMessage.objects.filter(
            timestamp__date=date
        ).values('author__username').annotate(
            count=Count('id')
        ).order_by('-count')[:5]
        
        user_activity.append({
            'date': date.strftime('%Y-%m-%d'),
            'users': list(day_messages)
        })
    
    return {
        'top_users': top_users,
        'user_activity': user_activity,
    }


def api_messages(request):
    """API endpoint for recent messages"""
    limit = int(request.GET.get('limit', 50))
    channel_id = request.GET.get('channel_id')
    user_id = request.GET.get('user_id')
    
    messages = DiscordMessage.objects.select_related('author', 'channel', 'channel__server')
    
    if channel_id:
        messages = messages.filter(channel__channel_id=channel_id)
    
    if user_id:
        messages = messages.filter(author__user_id=user_id)
    
    messages = messages.order_by('-timestamp')[:limit]
    
    data = []
    for message in messages:
        data.append({
            'id': message.message_id,
            'content': message.content,
            'timestamp': message.timestamp.isoformat(),
            'author': {
                'id': message.author.user_id,
                'username': message.author.username,
                'display_name': message.author.display_name,
                'avatar_url': message.author.avatar_url,
            },
            'channel': {
                'id': message.channel.channel_id,
                'name': message.channel.name,
                'server': message.channel.server.name,
            },
            'has_attachments': message.has_attachments,
            'attachment_count': message.attachment_count,
            'has_embeds': message.has_embeds,
            'embed_count': message.embed_count,
        })
    
    return JsonResponse({'messages': data})


def api_stats(request):
    """API endpoint for statistics"""
    stats = get_dashboard_stats()
    channel_stats = get_channel_stats()
    user_stats = get_user_stats()
    
    return JsonResponse({
        'stats': stats,
        'channel_stats': channel_stats,
        'user_stats': user_stats,
    })