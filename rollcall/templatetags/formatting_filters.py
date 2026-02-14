from django import template

register = template.Library()

@register.filter(name='intcomma')
def intcomma(value):
    """Format number with comma separators"""
    if value is None:
        return ''
    try:
        # Convert to integer first to remove decimals
        int_value = int(float(value))
        # Add commas
        return f"{int_value:,}"
    except (ValueError, TypeError):
        return value








