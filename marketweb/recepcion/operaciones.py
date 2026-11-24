"""Operaciones de recepción (capa de negocio). TODA escritura pasa por aquí, dentro de transacciones.

Reutiliza marketweb.services (reservas, check-in/out, disponibilidad) sin duplicarlo y agrega lo que
faltaba: pagos y abonos, servicios adicionales, saldos y la bitácora de operaciones.
Cada función exige un usuario de recepción vigente (permisos.exigir_personal_recepcion).
"""
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .. import services
from ..models import (Habitacion, Huesped, OperacionRecepcion, Pago, Reserva, Servicio,
                      ServicioReserva)
from ..services import ReglaDeNegocioError
from . import permisos

CERO = Decimal('0.00')
MAX_PAGO = Decimal('99999999.99')      # Pago.monto: max_digits=10, decimal_places=2
LOTE = 400


def cop(valor):
    entero = int(Decimal(valor or 0).quantize(Decimal('1')))
    return f"{'-' if entero < 0 else ''}${abs(entero):,}".replace(',', '.')


def rango_del_dia(fecha):
    """(inicio, fin) aware del día local, para filtrar DateTimeField sin depender de tablas de zona horaria."""
    tz = timezone.get_current_timezone()
    inicio = timezone.make_aware(datetime.combine(fecha, time.min), tz)
    return inicio, inicio + timedelta(days=1)


# ---------------------------------------------------------------------------
# Bitácora
# ---------------------------------------------------------------------------
def registrar_operacion(usuario, tipo, descripcion, reserva=None, huesped=None, monto=None):
    if huesped is None and reserva is not None:
        huesped = reserva.huesped
    return OperacionRecepcion.objects.create(
        usuario=usuario, tipo=tipo, descripcion=descripcion[:300],
        reserva=reserva, huesped=huesped, monto=monto)


def _nombre(h):
    return f"{h.nombres} {h.apellidos}"


# ---------------------------------------------------------------------------
# Cuenta y saldos
# ---------------------------------------------------------------------------
def cuenta_reserva(reserva, ahora=None, con_cargo_tardio=False):
    """Totales de la cuenta. El cargo por salida tardía solo se incluye al hacer check-out
    (con_cargo_tardio=True) o cuando la reserva ya está finalizada."""
    cuenta = services.calcular_cuenta(reserva, ahora)
    if not con_cargo_tardio and reserva.estado != 'CHECKOUT':
        cuenta['total_general'] -= cuenta['cargo_tardio']
        cuenta['cargo_tardio'] = CERO
        cuenta['saldo_pendiente'] = cuenta['total_general'] - cuenta['total_pagos']
    return cuenta


def _lotes(ids):
    ids = list(ids)
    for i in range(0, len(ids), LOTE):
        yield ids[i:i + LOTE]


def anotar_saldos(reservas):
    """Agrega a cada reserva: total_cuenta, pagado, saldo (sin cargo tardío salvo finalizadas).
    Usa 2 consultas por lote, no una por reserva. Devuelve la lista."""
    reservas = list(reservas)
    servicios, pagos = {}, {}
    for lote in _lotes(r.pk for r in reservas):
        servicios.update(dict(ServicioReserva.objects.filter(reserva_id__in=lote)
                              .values_list('reserva_id').annotate(t=Sum('subtotal'))))
        pagos.update(dict(Pago.objects.filter(reserva_id__in=lote)
                          .values_list('reserva_id').annotate(t=Sum('monto'))))
    for r in reservas:
        total = r.calcular_alojamiento() + (servicios.get(r.pk) or CERO)
        if r.estado == 'CHECKOUT':
            total += r.cargo_tardio
        r.total_cuenta = total
        r.pagado = pagos.get(r.pk) or CERO
        r.saldo = total - r.pagado
    return reservas


# ---------------------------------------------------------------------------
# Huéspedes
# ---------------------------------------------------------------------------
@transaction.atomic
def alta_huesped(*, usuario, datos):
    usuario = permisos.exigir_personal_recepcion(usuario)
    documento = (datos.get('numero_documento') or '').strip()
    if not documento:
        raise ReglaDeNegocioError("El número de documento es obligatorio.")
    if Huesped.objects.filter(numero_documento=documento).exists():
        raise ReglaDeNegocioError(f"Ya existe un huésped con el documento {documento}.")
    try:
        with transaction.atomic():
            huesped = Huesped.objects.create(**{**datos, 'numero_documento': documento})
    except IntegrityError:
        raise ReglaDeNegocioError(f"Ya existe un huésped con el documento {documento}.")
    registrar_operacion(usuario, OperacionRecepcion.HUESPED_ALTA,
                        f"Registró al huésped {_nombre(huesped)} ({documento}).", huesped=huesped)
    return huesped


@transaction.atomic
def editar_huesped(*, usuario, form):
    """form: panel_forms.HuespedForm ya validado (solo contacto; el documento no se edita)."""
    usuario = permisos.exigir_personal_recepcion(usuario)
    huesped = form.save()
    registrar_operacion(usuario, OperacionRecepcion.HUESPED_EDICION,
                        f"Actualizó los datos de contacto de {_nombre(huesped)}.", huesped=huesped)
    return huesped


# ---------------------------------------------------------------------------
# Reservas
# ---------------------------------------------------------------------------
@transaction.atomic
def crear_reserva(*, usuario, habitacion_id, datos_huesped, entrada, salida, num_huespedes,
                  canal='PRESENCIAL', tipo_id=None):
    """Valida y crea con services.crear_reserva (bloqueo de habitación). Si el documento ya existe se
    conserva el huésped registrado (no se sobrescriben sus datos). Devuelve (reserva, huesped_existente|None)."""
    usuario = permisos.exigir_personal_recepcion(usuario)
    documento = (datos_huesped.get('numero_documento') or '').strip()
    existente = Huesped.objects.filter(numero_documento=documento).first() if documento else None
    reserva = services.crear_reserva(
        habitacion_id=habitacion_id, datos_huesped=datos_huesped, entrada=entrada, salida=salida,
        num_huespedes=num_huespedes, canal=canal, tipo_id=tipo_id, actualizar_huesped=False)
    registrar_operacion(
        usuario, OperacionRecepcion.RESERVA_CREADA,
        f"Creó la reserva RW-{reserva.pk:06d}: hab. {reserva.habitacion.numero}, "
        f"{entrada:%d/%m/%Y} → {salida:%d/%m/%Y}, {num_huespedes} huésped(es).",
        reserva=reserva, monto=reserva.valor_total)
    return reserva, existente


@transaction.atomic
def modificar_reserva(*, usuario, reserva_id, habitacion_id, entrada, salida, num_huespedes, canal):
    usuario = permisos.exigir_personal_recepcion(usuario)
    reserva = services.modificar_reserva(          # vuelve a validar disponibilidad (RN07)
        reserva_id, habitacion_id=habitacion_id, entrada=entrada, salida=salida,
        num_huespedes=num_huespedes, canal=canal)
    registrar_operacion(
        usuario, OperacionRecepcion.RESERVA_MODIFICADA,
        f"Modificó la reserva RW-{reserva.pk:06d}: hab. {reserva.habitacion.numero}, "
        f"{entrada:%d/%m/%Y} → {salida:%d/%m/%Y}, {num_huespedes} huésped(es).",
        reserva=reserva, monto=reserva.valor_total)
    return reserva


@transaction.atomic
def confirmar_reserva(*, usuario, reserva_id):
    usuario = permisos.exigir_personal_recepcion(usuario)
    reserva = services.confirmar_reserva(reserva_id)
    registrar_operacion(usuario, OperacionRecepcion.RESERVA_CONFIRMADA,
                        f"Confirmó la reserva RW-{reserva.pk:06d}.", reserva=reserva, monto=reserva.valor_total)
    return reserva


@transaction.atomic
def cancelar_reserva(*, usuario, reserva_id):
    """Regla aceptada: el recepcionista cancela PENDIENTE/CONFIRMADA sin pagos; con pagos, solo el administrador."""
    usuario = permisos.exigir_personal_recepcion(usuario)
    services._bloquear_reserva_y_habitaciones(reserva_id)      # bloquea antes de mirar los pagos
    if Pago.objects.filter(reserva_id=reserva_id).exists() and not permisos.es_admin(usuario):
        raise ReglaDeNegocioError(
            "La reserva tiene pagos registrados: solo un administrador puede cancelarla.")
    reserva = services.cancelar_reserva(reserva_id)
    registrar_operacion(usuario, OperacionRecepcion.RESERVA_CANCELADA,
                        f"Canceló la reserva RW-{reserva.pk:06d}.", reserva=reserva)
    return reserva


# ---------------------------------------------------------------------------
# Check-in / check-out
# ---------------------------------------------------------------------------
@transaction.atomic
def hacer_checkin(*, usuario, reserva_id, observaciones='', ahora=None, hoy=None):
    usuario = permisos.exigir_personal_recepcion(usuario)
    reserva = services.registrar_checkin(reserva_id, ahora=ahora, hoy=hoy)
    obs = (observaciones or '').strip()[:200]
    registrar_operacion(
        usuario, OperacionRecepcion.CHECKIN,
        f"Check-in de la reserva RW-{reserva.pk:06d} en la habitación {reserva.habitacion.numero}."
        + (f" Observaciones: {obs}" if obs else ''), reserva=reserva)
    return reserva


@transaction.atomic
def hacer_checkout(*, usuario, reserva_id, ahora=None):
    """Check-out con control financiero: exige check-in previo (RN06) y saldo EXACTAMENTE en cero
    (alojamiento + servicios + cargo tardío − pagos). Si hay saldo, primero se registra el pago."""
    usuario = permisos.exigir_personal_recepcion(usuario)
    ahora = ahora or timezone.now()
    reserva, _ = services._bloquear(reserva_id)
    if reserva.estado != 'CHECKIN':
        raise ReglaDeNegocioError("No se puede hacer check-out: la reserva no tiene check-in activo.")
    cuenta = services.calcular_cuenta(reserva, ahora)
    saldo = cuenta['saldo_pendiente']
    if saldo > 0:
        raise ReglaDeNegocioError(
            f"La cuenta tiene un saldo pendiente de {cop(saldo)}. Registra el pago final antes del check-out.")
    if saldo < 0:
        raise ReglaDeNegocioError(
            f"La cuenta tiene un saldo a favor de {cop(-saldo)}: es una inconsistencia financiera "
            "que debe revisar un administrador antes de cerrar.")
    reserva = services.registrar_checkout(reserva_id, ahora=ahora)
    registrar_operacion(
        usuario, OperacionRecepcion.CHECKOUT,
        f"Check-out de la reserva RW-{reserva.pk:06d}; habitación {reserva.habitacion.numero} pasó a aseo. "
        f"Total de la cuenta {cop(cuenta['total_general'])}.", reserva=reserva, monto=cuenta['total_general'])
    return reserva


# ---------------------------------------------------------------------------
# Pagos y abonos
# ---------------------------------------------------------------------------
@transaction.atomic
def registrar_pago(*, usuario, reserva_id, monto, metodo, referencia='', ahora=None, con_cargo_tardio=False):
    """Registra un pago/abono. Nunca modifica pagos anteriores. Bloquea habitación y reserva (mismo orden
    que el resto de servicios) para que dos pagos simultáneos no superen el saldo."""
    usuario = permisos.exigir_personal_recepcion(usuario)
    if metodo not in dict(Pago.METODOS):
        raise ReglaDeNegocioError("El método de pago no es válido.")
    try:
        monto = Decimal(monto).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        raise ReglaDeNegocioError("El valor del pago no es válido.")
    if monto <= 0:
        raise ReglaDeNegocioError("El valor del pago debe ser mayor que cero.")
    if monto > MAX_PAGO:
        raise ReglaDeNegocioError("El valor del pago es demasiado grande.")
    reserva, _ = services._bloquear(reserva_id)
    if reserva.estado == 'CANCELADA':
        raise ReglaDeNegocioError("No se registran pagos en una reserva cancelada.")
    if reserva.estado == 'CHECKOUT':
        raise ReglaDeNegocioError("La cuenta de esta reserva ya está cerrada (check-out realizado).")
    saldo = cuenta_reserva(reserva, ahora, con_cargo_tardio)['saldo_pendiente']
    if saldo <= 0:
        raise ReglaDeNegocioError("Esta reserva no tiene saldo pendiente.")
    if monto > saldo:
        raise ReglaDeNegocioError(
            f"El pago de {cop(monto)} supera el saldo pendiente de {cop(saldo)}.")
    pago = Pago.objects.create(reserva=reserva, monto=monto, metodo_pago=metodo,
                               registrado_por=usuario, referencia=(referencia or '').strip()[:60])
    registrar_operacion(
        usuario, OperacionRecepcion.PAGO,
        f"Registró un pago de {cop(monto)} ({pago.get_metodo_pago_display()}) en la reserva "
        f"RW-{reserva.pk:06d}. Saldo restante: {cop(saldo - monto)}.", reserva=reserva, monto=monto)
    return pago


# ---------------------------------------------------------------------------
# Servicios adicionales
# ---------------------------------------------------------------------------
@transaction.atomic
def cargar_servicio(*, usuario, reserva_id, servicio_id, cantidad):
    """Consumo de catálogo asociado a una reserva CONFIRMADA o con huésped en casa. El precio sale de la BD."""
    usuario = permisos.exigir_personal_recepcion(usuario)
    reserva, _ = services._bloquear(reserva_id)
    if reserva.estado not in ('CONFIRMADA', 'CHECKIN'):
        raise ReglaDeNegocioError(
            "Solo se cargan servicios a reservas confirmadas o con huésped en casa "
            f"(estado actual: {reserva.get_estado_display()}).")
    servicio = Servicio.objects.filter(pk=servicio_id).first()
    if servicio is None:
        raise ReglaDeNegocioError("El servicio seleccionado no existe.")
    if not isinstance(cantidad, int) or cantidad < 1 or cantidad > 99:
        raise ReglaDeNegocioError("La cantidad debe estar entre 1 y 99.")
    if servicio.precio <= 0:
        raise ReglaDeNegocioError("El servicio no tiene un precio válido.")
    consumo = ServicioReserva.objects.create(
        reserva=reserva, servicio=servicio, cantidad=cantidad, precio_unitario=servicio.precio)
    registrar_operacion(
        usuario, OperacionRecepcion.SERVICIO,
        f"Cargó {cantidad} × {servicio.nombre} a la reserva RW-{reserva.pk:06d} ({cop(consumo.subtotal)}).",
        reserva=reserva, monto=consumo.subtotal)
    return consumo


# ---------------------------------------------------------------------------
# Habitaciones
# ---------------------------------------------------------------------------
@transaction.atomic
def habilitar_habitacion(*, usuario, habitacion_id):
    """EN_ASEO → DISPONIBLE. Mantenimiento lo habilita solo administración."""
    usuario = permisos.exigir_personal_recepcion(usuario)
    hab = Habitacion.objects.filter(pk=habitacion_id).first()
    if hab is not None and hab.estado == Habitacion.MANTENIMIENTO and not permisos.es_admin(usuario):
        raise ReglaDeNegocioError("Las habitaciones en mantenimiento las habilita administración.")
    hab = services.habilitar_habitacion(habitacion_id)
    registrar_operacion(usuario, OperacionRecepcion.HABITACION_HABILITADA,
                        f"Habilitó la habitación {hab.numero}: ahora está disponible.")
    return hab


# ---------------------------------------------------------------------------
# Dashboard (todo sale de la BD)
# ---------------------------------------------------------------------------
def resumen_dashboard(hoy=None):
    hoy = hoy or timezone.localdate()
    inicio, fin = rango_del_dia(hoy)
    base = Reserva.objects.select_related('huesped', 'habitacion__tipo')

    habitaciones, _ = services.anotar_estados(Habitacion.objects.select_related('tipo'), hoy=hoy)
    estados = services.resumen_estados(habitaciones)

    activas = anotar_saldos(base.filter(estado__in=('CONFIRMADA', 'CHECKIN')))
    con_saldo = [r for r in activas if r.saldo > 0]

    pagos_hoy = Pago.objects.filter(fecha_pago__gte=inicio, fecha_pago__lt=fin)
    agg = pagos_hoy.aggregate(total=Sum('monto'), n=Count('id'))

    return {
        'hoy': hoy,
        'reservas_hoy': base.filter(fecha_entrada=hoy).exclude(estado='CANCELADA').count(),
        'llegadas_pendientes': base.filter(fecha_entrada=hoy, estado__in=('PENDIENTE', 'CONFIRMADA')).count(),
        'salidas_hoy': base.filter(fecha_salida=hoy, estado='CHECKIN').count(),
        'alojados': base.filter(estado='CHECKIN').count(),
        'pendientes_confirmar': base.filter(estado='PENDIENTE').count(),
        'estados': estados,
        'reservas_con_saldo': len(con_saldo),
        'saldo_por_cobrar': sum((r.saldo for r in con_saldo), CERO),
        'pagos_hoy_total': agg['total'] or CERO,
        'pagos_hoy_n': agg['n'] or 0,
        'proximas_llegadas': list(base.filter(estado__in=('PENDIENTE', 'CONFIRMADA'), fecha_entrada__gte=hoy)
                                  .order_by('fecha_entrada', 'habitacion__numero')[:8]),
        'proximas_salidas': list(base.filter(estado='CHECKIN').order_by('fecha_salida', 'habitacion__numero')[:8]),
        'recientes': list(base.order_by('-fecha_creacion', '-id')[:8]),
    }
