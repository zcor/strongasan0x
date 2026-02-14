from django import template
import pytz

register = template.Library()

@register.filter
def pacific_time(value):
    """Convert UTC datetime to Pacific time"""
    if not value:
        return value
    
    pacific_tz = pytz.timezone('US/Pacific')
    return value.astimezone(pacific_tz)

@register.filter
def pacific_time_format(value):
    """Convert UTC datetime to Pacific time and format it"""
    if not value:
        return value
    
    try:
        pacific_tz = pytz.timezone('US/Pacific')
        pacific_time = value.astimezone(pacific_tz)
        return pacific_time.strftime('%b %d, %Y %I:%M %p')
    except Exception:
        return str(value)

@register.filter
def divide(value, arg):
    """Divide value by arg"""
    try:
        return float(value) / float(arg)
    except (ValueError, TypeError, ZeroDivisionError):
        return 0
