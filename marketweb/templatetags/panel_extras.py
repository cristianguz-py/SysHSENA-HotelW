"""Filtros de formato para el panel de gestión (sin JavaScript)."""
from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def cop(valor):
    """1234567.5 -> '$1.234.568' (pesos colombianos, separador de miles con punto)."""
    try:
        numero = Decimal(valor or 0)
    except (InvalidOperation, TypeError, ValueError):
        return '—'
    entero = int(numero.quantize(Decimal('1')))
    signo = '-' if entero < 0 else ''
    return f"{signo}${abs(entero):,}".replace(',', '.')


@register.filter
def codigo_reserva(pk):
    return f"RW-{int(pk):06d}"


@register.filter
def pct(valor):
    """Porcentaje con una decimal y coma: 83.333 -> '83,3 %'."""
    if valor is None:
        return '—'
    return f"{Decimal(valor):.1f}".replace('.', ',') + ' %'
