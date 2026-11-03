"""
Reglas de negocio de habitaciones y reservas del Hotel W.

Todo cambio de estado de reserva/habitación pasa por aquí, dentro de
transaction.atomic y bloqueando la fila de la habitación (select_for_update),
de modo que dos recepcionistas no puedan reservar la misma habitación a la vez.
"""
from collections import Counter
from datetime import time
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Habitacion, Huesped, Pago, Reserva, ServicioReserva


class ReglaDeNegocioError(Exception):
    """Operación rechazada por una regla del hotel. str(e) es apto para mostrar al usuario;
    e.mensajes conserva cada motivo por separado (lista) para mostrarlos uno a uno."""

    def __init__(self, mensaje, mensajes=None):
        super().__init__(mensaje)
        self.mensajes = list(mensajes) if mensajes else [mensaje]


# ---------------------------------------------------------------------------
# Disponibilidad
# ---------------------------------------------------------------------------
def habitaciones_disponibles(entrada, salida, num_huespedes=None, hoy=None, tipo_id=None):
    """Habitaciones reservables en [entrada, salida): activas, no en
    mantenimiento/fuera de servicio, con capacidad suficiente y sin reservas
    CONFIRMADA/CHECKIN que se crucen. Si la estancia empieza hoy (o antes),
    además deben estar físicamente DISPONIBLE."""
    hoy = hoy or timezone.localdate()
    if salida <= entrada:
        return Habitacion.objects.none()

    qs = (Habitacion.objects
          .filter(activa=True, tipo__precio_noche__gt=0)
          .exclude(estado__in=Habitacion.ESTADOS_NO_RESERVABLES)
          .select_related('tipo'))
    if entrada <= hoy:
        qs = qs.filter(estado=Habitacion.DISPONIBLE)
    if num_huespedes:
        qs = qs.filter(tipo__capacidad__gte=num_huespedes)
    if tipo_id:
        qs = qs.filter(tipo_id=tipo_id)

    ocupadas = (Reserva.objects
                .filter(estado__in=Reserva.ESTADOS_BLOQUEANTES,
                        fecha_entrada__lt=salida, fecha_salida__gt=entrada)
                .values('habitacion_id'))
    return qs.exclude(id__in=ocupadas).order_by('piso', 'numero')


# ---------------------------------------------------------------------------
# Estado visual de las habitaciones (dashboard)
# ---------------------------------------------------------------------------
ETIQUETAS_ESTADO = {
    'DISPONIBLE': 'Disponible',
    'RESERVADA': 'Reservada',
    'OCUPADA': 'Ocupada',
    'EN_ASEO': 'En aseo',
    'MANTENIMIENTO': 'Mantenimiento',
    'FUERA_DE_SERVICIO': 'Fuera de servicio',
}
# Clase CSS / valor data-estado. Mantenimiento y fuera de servicio comparten
# "fuera" (no operativas) para el filtro del tablero.
CLASES_ESTADO = {
    'DISPONIBLE': 'disponible',
    'RESERVADA': 'reservada',
    'OCUPADA': 'ocupada',
    'EN_ASEO': 'aseo',
    'MANTENIMIENTO': 'fuera',
    'FUERA_DE_SERVICIO': 'fuera',
}


def anotar_estados(habitaciones, hoy=None):
    """Recibe un queryset/lista de Habitacion y le agrega, SIN consultas por
    habitación (2 consultas en total):

      estado_visual          'DISPONIBLE' | 'RESERVADA' | 'OCUPADA' | 'EN_ASEO' |
                             'MANTENIMIENTO' | 'FUERA_DE_SERVICIO'
      estado_visual_display  etiqueta legible
      estado_clase           clase CSS / data-estado
      reserva_activa         reserva en casa (CHECKIN) o la próxima CONFIRMADA

    "Ocupada" se deriva de una reserva con check-in y "Reservada" de una
    reserva confirmada vigente; nada de eso se escribe a mano.
    Devuelve (lista_de_habitaciones, reservas_activas)."""
    hoy = hoy or timezone.localdate()
    habitaciones = list(habitaciones)

    activas = list(
        Reserva.objects.filter(estado__in=Reserva.ESTADOS_BLOQUEANTES)
        .select_related('huesped', 'habitacion__tipo')
        .order_by('fecha_entrada')
    )
    en_casa, proximas = {}, {}
    for r in activas:
        if r.estado == 'CHECKIN':
            en_casa.setdefault(r.habitacion_id, r)
        elif r.fecha_salida > hoy:
            proximas.setdefault(r.habitacion_id, r)

    for hab in habitaciones:
        if not hab.activa or hab.estado == Habitacion.FUERA_DE_SERVICIO:
            visual = 'FUERA_DE_SERVICIO'
        elif hab.estado == Habitacion.MANTENIMIENTO:
            visual = 'MANTENIMIENTO'
        elif hab.estado == Habitacion.EN_ASEO:
            visual = 'EN_ASEO'
        elif hab.id in en_casa:
            visual = 'OCUPADA'
        elif hab.id in proximas:
            visual = 'RESERVADA'
        else:
            visual = 'DISPONIBLE'
        hab.estado_visual = visual
        hab.estado_visual_display = ETIQUETAS_ESTADO[visual]
        hab.estado_clase = CLASES_ESTADO[visual]
        hab.reserva_activa = en_casa.get(hab.id) or proximas.get(hab.id)
    return habitaciones, activas


def resumen_estados(habitaciones):
    """Conteos para los KPI del dashboard, a partir de habitaciones ya anotadas."""
    c = Counter(h.estado_visual for h in habitaciones)
    return {
        'total_habitaciones': len(habitaciones),
        'disponibles': c['DISPONIBLE'],
        'reservadas': c['RESERVADA'],
        'ocupadas': c['OCUPADA'],
        'en_aseo': c['EN_ASEO'],
        'no_operativas': c['MANTENIMIENTO'] + c['FUERA_DE_SERVICIO'],
    }


# ---------------------------------------------------------------------------
# Crear reserva
# ---------------------------------------------------------------------------
@transaction.atomic
def crear_reserva(*, habitacion_id, datos_huesped, entrada, salida, num_huespedes,
                  canal='PRESENCIAL', tipo_id=None, hoy=None,
                  usuario=None, actualizar_huesped=True):
    """Única puerta de entrada para crear reservas. Todo ocurre en una transacción:
    1) bloquea la fila de la habitación (select_for_update), 2) re-comprueba fechas,
    capacidad, estado y solapamiento ya con el bloqueo tomado, 3) calcula el valor
    desde la BD (nunca desde el formulario), 4) guarda huésped y reserva.
    Si algo falla se hace rollback completo: no queda nada a medias."""
    try:
        habitacion = (Habitacion.objects.select_for_update()
                      .select_related('tipo').get(pk=habitacion_id))
    except Habitacion.DoesNotExist:
        raise ReglaDeNegocioError("La habitación seleccionada no existe.")

    errores = []
    if tipo_id is not None and habitacion.tipo_id != tipo_id:
        errores.append(f"La habitación {habitacion.numero} no pertenece al tipo de habitación seleccionado.")
    if canal not in dict(Reserva.CANALES):
        errores.append("El canal de reserva no es válido.")
    if not (datos_huesped.get('numero_documento') or '').strip():
        errores.append("El número de documento del huésped es obligatorio.")
    errores += Reserva.errores_de_reserva(
        habitacion, entrada, salida, num_huespedes,
        hoy=hoy, validar_pasado=True, bloquear=True,
    )
    if errores:
        raise ReglaDeNegocioError(' '.join(errores), errores)

    numero_documento = datos_huesped['numero_documento'].strip()
    defaults = {k: v for k, v in datos_huesped.items() if k != 'numero_documento'}
    if actualizar_huesped:
        huesped, _ = Huesped.objects.update_or_create(numero_documento=numero_documento, defaults=defaults)
    else:
        # Reservas web: un cliente NO puede sobrescribir los datos de un huésped que ya
        # existe (solo por teclear su documento). Si existe, se conserva tal cual.
        huesped, _ = Huesped.objects.get_or_create(numero_documento=numero_documento, defaults=defaults)

    reserva = Reserva(
        huesped=huesped, habitacion=habitacion,
        fecha_entrada=entrada, fecha_salida=salida,
        num_huespedes=num_huespedes, canal_reserva=canal, estado='CONFIRMADA',
        usuario=usuario,
    )
    reserva.valor_total = reserva.calcular_alojamiento()
    reserva.save()
    # La habitación NO cambia de estado: "Reservada" se deriva de esta reserva.
    return reserva


def crear_reserva_cliente(*, usuario, tipo_id, entrada, salida, num_huespedes, datos_huesped, hoy=None):
    """Reserva web de un cliente: el SERVIDOR elige la habitación (el navegador no envía
    ninguna). Prueba las habitaciones libres del tipo en orden; cada intento es su propia
    transacción con bloqueo (crear_reserva), así que si otra persona se adelanta se pasa a
    la siguiente. Si ninguna se puede, el mensaje al cliente es genérico: los mensajes
    internos citan números de habitación y reservas de otros huéspedes."""
    candidatas = list(
        habitaciones_disponibles(entrada, salida, num_huespedes, hoy=hoy, tipo_id=tipo_id)
        .values_list('pk', flat=True)[:10]
    )
    for habitacion_id in candidatas:
        try:
            return crear_reserva(
                habitacion_id=habitacion_id, datos_huesped=datos_huesped,
                entrada=entrada, salida=salida, num_huespedes=num_huespedes,
                canal='WEB', tipo_id=tipo_id, hoy=hoy,
                usuario=usuario, actualizar_huesped=False,
            )
        except ReglaDeNegocioError:
            continue
    raise ReglaDeNegocioError(
        "Ya no hay habitaciones disponibles de este tipo para esas fechas. "
        "No se guardó ninguna reserva; puedes cambiar las fechas o el tipo de habitación."
    )


# ---------------------------------------------------------------------------
# Bloqueo ordenado: primero habitación, luego reserva (evita deadlocks)
# ---------------------------------------------------------------------------
def _bloquear(reserva_id):
    try:
        habitacion_id = Reserva.objects.values_list('habitacion_id', flat=True).get(pk=reserva_id)
    except Reserva.DoesNotExist:
        raise ReglaDeNegocioError("La reserva no existe.")
    habitacion = Habitacion.objects.select_for_update().select_related('tipo').get(pk=habitacion_id)
    reserva = (Reserva.objects.select_for_update().get(pk=reserva_id))
    reserva.habitacion = habitacion
    return reserva, habitacion


# ---------------------------------------------------------------------------
# Check-in
# ---------------------------------------------------------------------------
@transaction.atomic
def registrar_checkin(reserva_id, ahora=None, hoy=None):
    ahora = ahora or timezone.now()
    hoy = hoy or timezone.localdate()
    reserva, habitacion = _bloquear(reserva_id)

    if reserva.estado == 'CANCELADA':
        raise ReglaDeNegocioError("No se puede hacer check-in sobre una reserva cancelada.")
    if reserva.estado == 'CHECKIN':
        raise ReglaDeNegocioError("Esta reserva ya tiene check-in.")
    if reserva.estado != 'CONFIRMADA':
        raise ReglaDeNegocioError(
            f"Solo se puede hacer check-in sobre reservas confirmadas (estado actual: {reserva.get_estado_display()}).")
    if reserva.fecha_entrada > hoy:
        raise ReglaDeNegocioError(
            f"La reserva empieza el {reserva.fecha_entrada:%d/%m/%Y}; el check-in no puede hacerse antes de esa fecha.")
    if reserva.fecha_salida <= hoy:
        raise ReglaDeNegocioError("La estancia de esta reserva ya terminó; no se puede hacer check-in.")
    if not habitacion.activa or habitacion.estado in Habitacion.ESTADOS_NO_RESERVABLES:
        raise ReglaDeNegocioError(
            f"La habitación {habitacion.numero} no está operativa ({habitacion.get_estado_display().lower()}).")
    if habitacion.estado == Habitacion.EN_ASEO:
        raise ReglaDeNegocioError(
            f"La habitación {habitacion.numero} está en aseo; habilítala antes del check-in.")
    if Reserva.objects.filter(habitacion=habitacion, estado='CHECKIN').exclude(pk=reserva.pk).exists():
        raise ReglaDeNegocioError(f"La habitación {habitacion.numero} ya tiene otro huésped con check-in.")

    reserva.estado = 'CHECKIN'
    reserva.checkin_real = ahora
    reserva.save()
    habitacion.estado = Habitacion.OCUPADA
    habitacion.save()
    return reserva


# ---------------------------------------------------------------------------
# Cuenta y check-out
# ---------------------------------------------------------------------------
def calcular_cargo_tardio(hora, num_huespedes):
    """Regla existente, sin cambios: 12:00–12:30 → $30.000; 12:30–13:00 → $60.000;
    después de 13:00 → $160.650 × huéspedes."""
    if time(12, 0) < hora <= time(12, 30):
        return Decimal('30000.00')
    if time(12, 30) < hora <= time(13, 0):
        return Decimal('60000.00')
    if hora > time(13, 0):
        return Decimal('160650.00') * Decimal(num_huespedes)
    return Decimal('0.00')


def calcular_cuenta(reserva, ahora=None):
    """Totales de la cuenta SIN escribir en la base de datos."""
    total_alojamiento = reserva.calcular_alojamiento()
    total_servicios = sum((s.subtotal for s in ServicioReserva.objects.filter(reserva=reserva)), Decimal('0.00'))
    total_pagos = sum((p.monto for p in Pago.objects.filter(reserva=reserva)), Decimal('0.00'))
    if reserva.estado == 'CHECKOUT':
        cargo_tardio = reserva.cargo_tardio           # ya cerrada: no recalcular con la hora actual
    else:
        hora = timezone.localtime(ahora or timezone.now()).time()
        cargo_tardio = calcular_cargo_tardio(hora, reserva.num_huespedes)
    total_general = total_alojamiento + total_servicios + cargo_tardio
    return {
        'total_alojamiento': total_alojamiento,
        'total_servicios': total_servicios,
        'cargo_tardio': cargo_tardio,
        'total_general': total_general,
        'total_pagos': total_pagos,
        'saldo_pendiente': total_general - total_pagos,
    }


@transaction.atomic
def registrar_checkout(reserva_id, ahora=None):
    ahora = ahora or timezone.now()
    reserva, habitacion = _bloquear(reserva_id)

    if reserva.estado == 'CHECKOUT':
        raise ReglaDeNegocioError("Esta reserva ya fue finalizada.")
    if reserva.estado != 'CHECKIN':
        raise ReglaDeNegocioError("No se puede hacer check-out: la reserva no tiene check-in activo.")

    cuenta = calcular_cuenta(reserva, ahora)
    reserva.cargo_tardio = cuenta['cargo_tardio']
    reserva.valor_total = cuenta['total_general']
    reserva.estado = 'CHECKOUT'
    reserva.checkout_real = ahora
    reserva.save()
    habitacion.estado = Habitacion.EN_ASEO      # luego "Habilitar" → DISPONIBLE
    habitacion.save()
    return reserva


# ---------------------------------------------------------------------------
# Habilitar (EN_ASEO / MANTENIMIENTO → DISPONIBLE)
# ---------------------------------------------------------------------------
@transaction.atomic
def habilitar_habitacion(habitacion_id):
    try:
        habitacion = Habitacion.objects.select_for_update().get(pk=habitacion_id)
    except Habitacion.DoesNotExist:
        raise ReglaDeNegocioError("La habitación no existe.")
    if not habitacion.activa:
        raise ReglaDeNegocioError(f"La habitación {habitacion.numero} está inactiva.")
    if habitacion.estado not in (Habitacion.EN_ASEO, Habitacion.MANTENIMIENTO):
        raise ReglaDeNegocioError(
            f"Solo se habilitan habitaciones en aseo o mantenimiento "
            f"(la {habitacion.numero} está {habitacion.get_estado_display().lower()}).")
    habitacion.estado = Habitacion.DISPONIBLE
    habitacion.save()
    return habitacion

# ---------------------------------------------------------------------------
# Gestión administrativa de reservas (panel de gestión)
# ---------------------------------------------------------------------------
ESTADOS_EDITABLES = ('PENDIENTE', 'CONFIRMADA')


def _bloquear_reserva_y_habitaciones(reserva_id, habitacion_destino_id=None):
    """Bloquea habitaciones (siempre en orden de id, para evitar deadlocks) y luego la reserva."""
    try:
        actual_id = Reserva.objects.values_list('habitacion_id', flat=True).get(pk=reserva_id)
    except Reserva.DoesNotExist:
        raise ReglaDeNegocioError("La reserva no existe.")
    ids = sorted({actual_id, habitacion_destino_id or actual_id})
    habitaciones = {h.pk: h for h in
                    Habitacion.objects.select_for_update().select_related('tipo').filter(pk__in=ids).order_by('pk')}
    if (habitacion_destino_id or actual_id) not in habitaciones:
        raise ReglaDeNegocioError("La habitación seleccionada no existe.")
    reserva = Reserva.objects.select_for_update().get(pk=reserva_id)
    return reserva, habitaciones[actual_id], habitaciones[habitacion_destino_id or actual_id]


@transaction.atomic
def confirmar_reserva(reserva_id, hoy=None):
    """PENDIENTE -> CONFIRMADA. Vuelve a validar disponibilidad con la habitación bloqueada
    y recalcula el valor desde la BD."""
    reserva, _, habitacion = _bloquear_reserva_y_habitaciones(reserva_id)
    if reserva.estado != 'PENDIENTE':
        raise ReglaDeNegocioError(
            f"Solo se confirman reservas pendientes (estado actual: {reserva.get_estado_display()}).")
    errores = Reserva.errores_de_reserva(
        habitacion, reserva.fecha_entrada, reserva.fecha_salida, reserva.num_huespedes,
        excluir_pk=reserva.pk, hoy=hoy, validar_pasado=True, bloquear=True,
    )
    if errores:
        raise ReglaDeNegocioError(' '.join(errores), errores)
    reserva.habitacion = habitacion
    reserva.estado = 'CONFIRMADA'
    reserva.valor_total = reserva.calcular_alojamiento()
    reserva.save()
    return reserva


@transaction.atomic
def cancelar_reserva(reserva_id):
    """PENDIENTE o CONFIRMADA -> CANCELADA. No borra nada: la reserva queda en el historial
    y libera la habitación porque CANCELADA no es un estado bloqueante."""
    reserva, _, _ = _bloquear_reserva_y_habitaciones(reserva_id)
    if reserva.estado == 'CANCELADA':
        raise ReglaDeNegocioError("Esta reserva ya estaba cancelada.")
    if reserva.estado not in ESTADOS_EDITABLES:
        raise ReglaDeNegocioError(
            f"No se puede cancelar una reserva en estado «{reserva.get_estado_display()}». "
            "Las estancias en curso o finalizadas no se cancelan.")
    reserva.estado = 'CANCELADA'
    reserva.save()
    return reserva


@transaction.atomic
def modificar_reserva(reserva_id, *, habitacion_id, entrada, salida, num_huespedes, canal, hoy=None):
    """Edición administrativa de una reserva PENDIENTE o CONFIRMADA. Solo se tocan los campos
    permitidos; el valor se recalcula siempre desde la BD (nunca desde el formulario)."""
    reserva, _, habitacion = _bloquear_reserva_y_habitaciones(reserva_id, habitacion_id)
    if reserva.estado not in ESTADOS_EDITABLES:
        raise ReglaDeNegocioError(
            f"No se puede editar una reserva en estado «{reserva.get_estado_display()}».")
    if canal not in dict(Reserva.CANALES):
        raise ReglaDeNegocioError("El canal de reserva no es válido.")

    cambia_cuando = (entrada != reserva.fecha_entrada or salida != reserva.fecha_salida)
    cambia_donde = habitacion.pk != reserva.habitacion_id
    errores = Reserva.errores_de_fechas(entrada, salida, hoy, validar_pasado=cambia_cuando)
    if reserva.estado == 'CONFIRMADA':
        errores = Reserva.errores_de_reserva(
            habitacion, entrada, salida, num_huespedes, excluir_pk=reserva.pk, hoy=hoy,
            verificar_estado=(cambia_cuando or cambia_donde),
            validar_pasado=cambia_cuando, bloquear=True)
    else:
        # PENDIENTE no bloquea la habitación, pero la capacidad y el precio sí deben ser válidos.
        if num_huespedes < 1:
            errores.append("Debe haber al menos un huésped.")
        elif num_huespedes > habitacion.tipo.capacidad:
            errores.append(f"La habitación {habitacion.numero} ({habitacion.tipo.nombre}) admite "
                           f"máximo {habitacion.tipo.capacidad} huéspedes.")
        if habitacion.tipo.precio_noche <= 0:
            errores.append(f"El tipo «{habitacion.tipo.nombre}» no tiene precio por noche configurado.")
    if errores:
        raise ReglaDeNegocioError(' '.join(errores), errores)

    reserva.habitacion = habitacion
    reserva.fecha_entrada = entrada
    reserva.fecha_salida = salida
    reserva.num_huespedes = num_huespedes
    reserva.canal_reserva = canal
    reserva.valor_total = reserva.calcular_alojamiento()
    reserva.save()
    return reserva


@transaction.atomic
def cargar_servicio(reserva_id, servicio_id, cantidad):
    """Registra un consumo (minibar/lavandería) en una reserva con huésped en casa.
    El precio sale del catálogo en la BD, no del navegador."""
    from .models import Servicio
    reserva = Reserva.objects.select_for_update().filter(pk=reserva_id).first()
    if reserva is None:
        raise ReglaDeNegocioError("La reserva no existe.")
    if reserva.estado != 'CHECKIN':
        raise ReglaDeNegocioError("Solo se cargan consumos a reservas con huésped en casa.")
    servicio = Servicio.objects.filter(pk=servicio_id).first()
    if servicio is None:
        raise ReglaDeNegocioError("El servicio seleccionado no existe.")
    if cantidad < 1 or cantidad > 99:
        raise ReglaDeNegocioError("La cantidad debe estar entre 1 y 99.")
    return ServicioReserva.objects.create(
        reserva=reserva, servicio=servicio, cantidad=cantidad, precio_unitario=servicio.precio)

