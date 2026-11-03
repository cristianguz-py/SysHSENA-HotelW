from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def cop(valor):
    """12500 -> $ 12.500 (pesos colombianos, sin decimales)."""
    try:
        entero = int(Decimal(valor).quantize(Decimal('1')))
    except (InvalidOperation, TypeError, ValueError):
        return ''
    return '$ ' + f"{entero:,}".replace(',', '.')
