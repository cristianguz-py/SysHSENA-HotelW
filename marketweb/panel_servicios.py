"""
Cálculos del panel de gestión de Hotel W. TODO sale de la base de datos real.

Definiciones (se muestran también en pantalla):
  * Periodo: reservas cuya FECHA DE ENTRADA cae entre «desde» y «hasta» (inclusive).
  * Confirmadas: estados CONFIRMADA, CHECKIN y CHECKOUT (reserva firme o ya vivida).
  * Canceladas: estado CANCELADA.  Pendientes: estado PENDIENTE (aún sin confirmar).
  * % de confirmación y de cancelación: sobre el total de reservas del periodo.
  * Importe reservado: suma de valor_total de reservas NO canceladas.
  * Importe confirmado: suma de valor_total de las confirmadas.
  * Recaudado: suma de los pagos REALMENTE registrados (tabla pago). Si no hay pagos, es 0.
"""
import math
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth

from .models import Habitacion, Pago, Reserva

MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
         'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
MESES_CORTOS = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
VALIDAS = ('CONFIRMADA', 'CHECKIN', 'CHECKOUT')
MAX_DIAS_PERIODO = 366
CERO = Decimal('0')


# ---------------------------------------------------------------------------
# Fechas
# ---------------------------------------------------------------------------
def fecha_o_none(texto):
    try:
        return date.fromisoformat((texto or '').strip())
    except ValueError:
        return None


def ultimo_dia_mes(d):
    siguiente = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return siguiente - timedelta(days=1)


def leer_periodo(get, hoy, clave_desde='desde', clave_hasta='hasta'):
    """Devuelve (desde, hasta, avisos). Por defecto: el mes actual. Valida fechas y tamaño."""
    avisos = []
    por_defecto = (hoy.replace(day=1), ultimo_dia_mes(hoy))
    crudo_d, crudo_h = get.get(clave_desde, ''), get.get(clave_hasta, '')
    desde, hasta = fecha_o_none(crudo_d), fecha_o_none(crudo_h)
    if (crudo_d and desde is None) or (crudo_h and hasta is None):
        avisos.append("Alguna fecha del filtro no es válida; se usó el periodo por defecto (mes actual).")
        return por_defecto[0], por_defecto[1], avisos
    desde, hasta = desde or por_defecto[0], hasta or por_defecto[1]
    if hasta < desde:
        avisos.append("La fecha «hasta» era anterior a «desde»; se usó el mes actual.")
        return por_defecto[0], por_defecto[1], avisos
    if (hasta - desde).days + 1 > MAX_DIAS_PERIODO:
        avisos.append(f"El periodo máximo es de {MAX_DIAS_PERIODO} días; se usó el mes actual.")
        return por_defecto[0], por_defecto[1], avisos
    return desde, hasta, avisos


def periodo_anterior(desde, hasta):
    dias = (hasta - desde).days + 1
    return desde - timedelta(days=dias), desde - timedelta(days=1)


# ---------------------------------------------------------------------------
# Métricas del periodo
# ---------------------------------------------------------------------------
def _del_periodo(desde, hasta):
    return Reserva.objects.filter(fecha_entrada__gte=desde, fecha_entrada__lte=hasta)


def metricas_periodo(desde, hasta):
    qs = _del_periodo(desde, hasta)
    agregados = qs.aggregate(
        total=Count('id'),
        confirmadas=Count('id', filter=Q(estado__in=VALIDAS)),
        canceladas=Count('id', filter=Q(estado='CANCELADA')),
        pendientes=Count('id', filter=Q(estado='PENDIENTE')),
        reservado=Sum('valor_total', filter=~Q(estado='CANCELADA')),
        confirmado=Sum('valor_total', filter=Q(estado__in=VALIDAS)),
    )
    total = agregados['total'] or 0
    noches = [(s - e).days or 1 for e, s in
              qs.exclude(estado='CANCELADA').values_list('fecha_entrada', 'fecha_salida')]
    recaudado = Pago.objects.filter(
        reserva__fecha_entrada__gte=desde, reserva__fecha_entrada__lte=hasta
    ).exclude(reserva__estado='CANCELADA').aggregate(t=Sum('monto'))['t'] or CERO
    return {
        'total': total,
        'confirmadas': agregados['confirmadas'] or 0,
        'canceladas': agregados['canceladas'] or 0,
        'pendientes': agregados['pendientes'] or 0,
        'pct_confirmacion': (Decimal(agregados['confirmadas'] or 0) * 100 / total) if total else None,
        'pct_cancelacion': (Decimal(agregados['canceladas'] or 0) * 100 / total) if total else None,
        'prom_noches': (Decimal(sum(noches)) / len(noches)) if noches else None,
        'reservado': agregados['reservado'] or CERO,
        'confirmado': agregados['confirmado'] or CERO,
        'recaudado': recaudado,
    }


def variacion(actual, anterior):
    """Variación porcentual real; None si el periodo anterior no tiene datos para comparar."""
    if anterior in (None, 0, CERO) or actual is None:
        return {'texto': 'Sin datos suficientes para comparar', 'clase': 'neutra', 'valor': None}
    cambio = (Decimal(actual) - Decimal(anterior)) * 100 / Decimal(anterior)
    clase = 'sube' if cambio > 0 else 'baja' if cambio < 0 else 'neutra'
    signo = '+' if cambio > 0 else ''
    return {'texto': f"{signo}{cambio:.1f} % frente al periodo anterior".replace('.', ','),
            'clase': clase, 'valor': cambio}


def conteos_de_hoy(hoy):
    return {
        'creadas_hoy': Reserva.objects.filter(fecha_creacion__date=hoy).count(),
        'entran_hoy': Reserva.objects.filter(fecha_entrada=hoy).exclude(estado='CANCELADA').count(),
        'llegadas_pendientes': Reserva.objects.filter(
            fecha_entrada=hoy, estado__in=('PENDIENTE', 'CONFIRMADA')).count(),
        'salidas_hoy': Reserva.objects.filter(fecha_salida=hoy, estado__in=('CHECKIN', 'CHECKOUT')).count(),
        'salidas_pendientes': Reserva.objects.filter(fecha_salida=hoy, estado='CHECKIN').count(),
    }


# ---------------------------------------------------------------------------
# Tendencia de reservas por mes (SVG generado en el servidor)
# ---------------------------------------------------------------------------
def _sumar_meses(d, n):
    indice = d.year * 12 + (d.month - 1) + n
    return date(indice // 12, indice % 12 + 1, 1)


def _techo_agradable(maximo):
    if maximo <= 4:
        return 4
    magnitud = 10 ** int(math.log10(maximo))
    for paso in (1, 2, 2.5, 5, 10):
        if paso * magnitud >= maximo:
            return int(paso * magnitud) or 4
    return int(maximo)


def tendencia_mensual(hasta, meses):
    """Reservas confirmadas y canceladas por mes (por fecha de entrada), terminando en el mes de «hasta»."""
    primero = _sumar_meses(hasta.replace(day=1), -(meses - 1))
    filas = (Reserva.objects.filter(fecha_entrada__gte=primero, fecha_entrada__lte=ultimo_dia_mes(hasta))
             .annotate(mes=TruncMonth('fecha_entrada')).values('mes', 'estado').annotate(n=Count('id')))
    conf, canc = defaultdict(int), defaultdict(int)
    for f in filas:
        clave = (f['mes'].year, f['mes'].month)
        if f['estado'] in VALIDAS:
            conf[clave] += f['n']
        elif f['estado'] == 'CANCELADA':
            canc[clave] += f['n']

    meses_lista = []
    for i in range(meses):
        d = _sumar_meses(primero, i)
        meses_lista.append({'clave': (d.year, d.month), 'etiqueta': f"{MESES_CORTOS[d.month - 1]} {d.year % 100:02d}",
                            'nombre': f"{MESES[d.month - 1].capitalize()} {d.year}",
                            'confirmadas': conf[(d.year, d.month)], 'canceladas': canc[(d.year, d.month)]})

    ancho, alto, izq, der, arriba, abajo = 760, 320, 46, 14, 20, 46
    util_w, util_h = ancho - izq - der, alto - arriba - abajo
    maximo = max([m['confirmadas'] for m in meses_lista] + [m['canceladas'] for m in meses_lista] + [0])
    techo = _techo_agradable(maximo)
    grupo = util_w / meses
    barra = min(30.0, grupo * 0.32)
    base_y = arriba + util_h
    for i, m in enumerate(meses_lista):
        cx = izq + grupo * (i + 0.5)
        for clave, desplazamiento in (('confirmadas', -barra - 2), ('canceladas', 2)):
            h = util_h * m[clave] / techo
            m[f'{clave}_svg'] = {
                'x': f"{cx + desplazamiento:.1f}", 'y': f"{base_y - h:.1f}", 'w': f"{barra:.1f}",
                'h': f"{h:.1f}", 'tx': f"{cx + desplazamiento + barra / 2:.1f}",
                'ty': f"{base_y - h - 5:.1f}"}
        m['etiqueta_x'] = f"{cx:.1f}"
    ticks = [{'y': f"{base_y - util_h * k / 4:.1f}", 'valor': techo * k // 4 if techo % 4 == 0 else round(techo * k / 4, 1)}
             for k in range(5)]
    return {'meses': meses_lista, 'ticks': ticks, 'ancho': ancho, 'alto': alto, 'izq': izq,
            'der_x': ancho - der, 'base_y': f"{base_y:.1f}", 'y_etiqueta': f"{base_y + 20:.1f}",
            'hay_datos': maximo > 0, 'total_confirmadas': sum(m['confirmadas'] for m in meses_lista),
            'total_canceladas': sum(m['canceladas'] for m in meses_lista)}


# ---------------------------------------------------------------------------
# Habitaciones más reservadas, por tipo (anillo SVG)
# ---------------------------------------------------------------------------
COLORES_TIPOS = ['#0A0A0A', '#8A7B66', '#D8C7AE', '#5C5346', '#B89B6A', '#BDAE96', '#2B2A27', '#E5D8C5', '#7A6A52']


def distribucion_por_tipo(desde, hasta):
    filas = list(_del_periodo(desde, hasta).exclude(estado='CANCELADA')
                 .values('habitacion__tipo__nombre').annotate(n=Count('id')).order_by('-n', 'habitacion__tipo__nombre'))
    total = sum(f['n'] for f in filas)
    radio = 70
    circunferencia = 2 * math.pi * radio
    acumulado = 0.0
    segmentos = []
    for i, f in enumerate(filas):
        porcentaje = f['n'] * 100 / total
        largo = circunferencia * f['n'] / total
        segmentos.append({
            'nombre': f['habitacion__tipo__nombre'], 'cantidad': f['n'], 'porcentaje': Decimal(porcentaje),
            'color': COLORES_TIPOS[i % len(COLORES_TIPOS)],
            'dash': f"{largo:.2f} {circunferencia - largo:.2f}", 'offset': f"{-acumulado:.2f}",
        })
        acumulado += largo
    return {'segmentos': segmentos, 'total': total, 'radio': radio}


# ---------------------------------------------------------------------------
# Habitaciones: estado para un periodo
# ---------------------------------------------------------------------------
def estado_para_periodo(habitaciones, entrada, salida, hoy):
    """Añade a cada habitación 'estado_periodo' (clave) y 'estado_periodo_texto'.
    Libre/Reservada dependen de las fechas consultadas; Ocupada y Fuera de servicio son del estado real."""
    habitaciones = list(habitaciones)
    choques = {}
    for r in (Reserva.objects.filter(estado__in=Reserva.ESTADOS_BLOQUEANTES,
                                     fecha_entrada__lt=salida, fecha_salida__gt=entrada)
              .select_related('huesped')):
        choques.setdefault(r.habitacion_id, r)
    for h in habitaciones:
        reserva = choques.get(h.pk)
        if not h.activa or h.estado == Habitacion.FUERA_DE_SERVICIO:
            clave, texto = 'fuera', 'Fuera de servicio'
        elif h.estado == Habitacion.MANTENIMIENTO:
            clave, texto = 'mantenimiento', 'Mantenimiento'
        elif reserva is not None and reserva.estado == 'CHECKIN':
            clave, texto = 'ocupada', 'Ocupada'
        elif reserva is not None:
            clave, texto = 'reservada', 'Reservada'
        elif h.estado == Habitacion.OCUPADA and entrada <= hoy:
            clave, texto = 'ocupada', 'Ocupada'
        elif h.estado == Habitacion.EN_ASEO and entrada <= hoy:
            clave, texto = 'aseo', 'En aseo'
        else:
            clave, texto = 'libre', 'Libre'
        h.estado_periodo, h.estado_periodo_texto, h.reserva_periodo = clave, texto, reserva
    return habitaciones


# ---------------------------------------------------------------------------
# Finanzas
# ---------------------------------------------------------------------------
def resumen_mensual(desde, hasta):
    filas = (_del_periodo(desde, hasta).annotate(mes=TruncMonth('fecha_entrada'))
             .values('mes').annotate(
                 total=Count('id'),
                 confirmadas=Count('id', filter=Q(estado__in=VALIDAS)),
                 canceladas=Count('id', filter=Q(estado='CANCELADA')),
                 pendientes=Count('id', filter=Q(estado='PENDIENTE')),
                 reservado=Sum('valor_total', filter=~Q(estado='CANCELADA')),
                 confirmado=Sum('valor_total', filter=Q(estado__in=VALIDAS)),
             ).order_by('mes'))
    salida = []
    for f in filas:
        f['nombre'] = f"{MESES[f['mes'].month - 1].capitalize()} {f['mes'].year}"
        f['reservado'] = f['reservado'] or CERO
        f['confirmado'] = f['confirmado'] or CERO
        salida.append(f)
    return salida


def ocupacion(desde, hasta):
    """Noches vendidas (reservas confirmadas/en casa/finalizadas) sobre noches disponibles
    (habitaciones activas actuales × días del periodo)."""
    dias = (hasta - desde).days + 1
    habitaciones = Habitacion.objects.filter(activa=True).exclude(
        estado__in=Habitacion.ESTADOS_NO_RESERVABLES).count()
    fin = hasta + timedelta(days=1)
    vendidas = 0
    for e, s in (Reserva.objects.filter(estado__in=VALIDAS, fecha_entrada__lt=fin, fecha_salida__gt=desde)
                 .values_list('fecha_entrada', 'fecha_salida')):
        vendidas += max(0, (min(s, fin) - max(e, desde)).days)
    disponibles = habitaciones * dias
    return {'noches_vendidas': vendidas, 'noches_disponibles': disponibles, 'habitaciones': habitaciones,
            'porcentaje': (Decimal(vendidas) * 100 / disponibles) if disponibles else None}


def sanear_celda_csv(valor):
    """Evita inyección de fórmulas al abrir el CSV en Excel/Sheets."""
    texto = '' if valor is None else str(valor)
    return "'" + texto if texto[:1] in ('=', '+', '-', '@', '\t', '\r') else texto
