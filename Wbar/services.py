"""
Reglas de negocio de W Bar Online. Toda operación que cambia pedidos, inventario, la cuenta
de la reserva o los pagos pasa por aquí, dentro de transaction.atomic y con select_for_update,
siguiendo el estilo de marketweb/services.py (se reutiliza ReglaDeNegocioError).

Integración financiera (sin sistema paralelo):
  * El CONSUMO se registra como ServicioReserva (una fila por línea del pedido) en el momento
    en que se ENTREGA el pedido; así entra solo en calcular_cuenta / check-out / factura.
  * El COBRO en la habitación crea un Pago (tabla `pago` existente) y un PagoPedido que lo
    enlaza con el pedido (responsable, comprobante). Son dos operaciones distintas.
  * Cada línea y cada pedido tienen relaciones OneToOne: no puede haber doble cargo ni doble pago.
"""
import secrets
import uuid
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from marketweb.models import Pago, Reserva, Servicio, ServicioReserva, Usuario
from marketweb.services import ReglaDeNegocioError

from . import carrito as carro
from .models import DetallePedido, EventoPedido, Pedido, PagoPedido, Producto

MAX_TOTAL_PEDIDO = Decimal('2000000.00')
MAX_PEDIDOS_ABIERTOS_POR_RESERVA = 5
METODOS_COBRO_PERMITIDOS = ('EFECTIVO', 'DATAFONO', 'TRANSFERENCIA')   # LINK_PAGO = en línea: excluido
NOMBRE_SERVICIO_WBAR = 'W Bar Online (pedido a la habitación)'


# ---------------------------------------------------------------------------
# Habitaciones / reservas autorizadas para un huésped
# ---------------------------------------------------------------------------
def reservas_habilitadas(usuario):
    """Reservas con las que ESTE usuario puede pedir: son suyas (Reserva.usuario), el huésped
    está en casa (CHECKIN) y la habitación sigue activa. De aquí sale la lista de habitaciones
    que se le ofrece; nunca de lo que envíe el navegador."""
    return (Reserva.objects.filter(usuario=usuario, estado='CHECKIN', habitacion__activa=True)
            .select_related('habitacion__tipo', 'huesped').order_by('habitacion__numero'))


def _registrar_evento(pedido, anterior, nuevo, usuario=None, nota=''):
    EventoPedido.objects.create(
        pedido=pedido, estado_anterior=anterior, estado_nuevo=nuevo,
        usuario=usuario, rol=getattr(usuario, 'rol', '') or 'SISTEMA', nota=nota[:250])


def nuevo_token():
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Crear pedido (cliente)
# ---------------------------------------------------------------------------
def crear_pedido(*, usuario, reserva_id, lineas_carrito, declaracion_mayoria_edad, notas, token):
    """Crea el pedido del huésped. Devuelve (pedido, creado). Si el mismo token ya generó un
    pedido (doble clic / reenvío del formulario) devuelve ese pedido con creado=False.

    `lineas_carrito` es {producto_id: cantidad}. Los precios se leen de la BD con las filas de
    producto bloqueadas; el navegador no aporta ningún precio, total ni estado."""
    if not token:
        raise ReglaDeNegocioError("El formulario venció. Revisa tu pedido y vuelve a confirmarlo.")
    previo = Pedido.objects.filter(token_envio=token, usuario=usuario).first()
    if previo:
        return previo, False
    if not lineas_carrito:
        raise ReglaDeNegocioError("Tu pedido está vacío.")

    try:
        with transaction.atomic():
            # 1) La reserva debe ser DEL USUARIO y estar en casa. Mensaje genérico: no se revela
            #    nada de reservas ajenas.
            reserva = (Reserva.objects.select_for_update()
                       .select_related('habitacion', 'huesped')
                       .filter(pk=reserva_id, usuario=usuario).first())
            if (reserva is None or reserva.estado != 'CHECKIN' or not reserva.habitacion.activa):
                raise ReglaDeNegocioError(
                    "Selecciona una de tus habitaciones con estadía activa. "
                    "Solo se atienden pedidos a la habitación de una reserva tuya con check-in realizado.")
            if Pedido.objects.filter(reserva=reserva, estado__in=Pedido.ESTADOS_ABIERTOS).count() >= MAX_PEDIDOS_ABIERTOS_POR_RESERVA:
                raise ReglaDeNegocioError(
                    f"Ya tienes {MAX_PEDIDOS_ABIERTOS_POR_RESERVA} pedidos en curso. "
                    "Espera a que se entreguen antes de pedir más.")

            # 2) Productos bloqueados (orden por id: evita interbloqueos) y validados.
            ids = sorted(lineas_carrito)
            productos = {p.pk: p for p in Producto.objects.select_for_update()
                         .select_related('categoria').filter(pk__in=ids).order_by('pk')}
            errores, total, tiene_alcohol = [], Decimal('0.00'), False
            for pid in ids:
                cantidad = lineas_carrito[pid]
                p = productos.get(pid)
                if p is None or not p.se_puede_pedir:
                    errores.append(f"«{p.nombre if p else 'Un producto'}» ya no está disponible.")
                    continue
                if not (1 <= cantidad <= carro.MAX_UNIDADES_POR_PRODUCTO):
                    errores.append(f"Cantidad no válida para «{p.nombre}».")
                    continue
                if p.stock is not None and cantidad > p.stock:
                    errores.append(f"De «{p.nombre}» solo quedan {p.stock}.")
                    continue
                total += p.precio * cantidad
                tiene_alcohol = tiene_alcohol or p.contiene_alcohol
            if errores:
                raise ReglaDeNegocioError(' '.join(errores), errores)
            if total > MAX_TOTAL_PEDIDO:
                raise ReglaDeNegocioError(
                    f"El valor máximo por pedido es ${MAX_TOTAL_PEDIDO:,.0f}. Divide tu pedido en dos.".replace(',', '.'))
            if tiene_alcohol and not declaracion_mayoria_edad:
                raise ReglaDeNegocioError(
                    "Para pedir bebidas alcohólicas debes declarar que eres mayor de edad. "
                    "El personal verificará tu documento al entregar.")

            # 3) Pedido, detalles (con copia de nombre/precio), inventario e historial.
            pedido = Pedido.objects.create(
                usuario=usuario, reserva=reserva, habitacion=reserva.habitacion, total=total,
                notas=(notas or '').strip()[:300], contiene_alcohol=tiene_alcohol,
                declaracion_mayoria_edad=bool(declaracion_mayoria_edad and tiene_alcohol),
                token_envio=token, codigo_semilla=secrets.token_hex(16))
            for pid in ids:
                p, cantidad = productos[pid], lineas_carrito[pid]
                DetallePedido.objects.create(
                    pedido=pedido, producto=p, nombre_producto=p.nombre,
                    categoria_nombre=p.categoria.nombre, presentacion=p.presentacion,
                    contiene_alcohol=p.contiene_alcohol, precio_unitario=p.precio, cantidad=cantidad)
                if p.stock is not None:
                    p.stock -= cantidad
                    p.save(update_fields=['stock', 'actualizado'])
            _registrar_evento(pedido, '', Pedido.PENDIENTE, usuario, 'Pedido creado por el huésped.')
            return pedido, True
    except IntegrityError:
        # Dos envíos simultáneos con el mismo token: el segundo recupera el pedido ya creado.
        previo = Pedido.objects.filter(token_envio=token, usuario=usuario).first()
        if previo:
            return previo, False
        raise


# ---------------------------------------------------------------------------
# Cambios de estado (personal)
# ---------------------------------------------------------------------------
def _pedido_bloqueado(pedido_id):
    pedido = Pedido.objects.select_for_update().select_related('habitacion', 'reserva').filter(pk=pedido_id).first()
    if pedido is None:
        raise ReglaDeNegocioError("El pedido no existe.")
    return pedido


@transaction.atomic
def cambiar_estado(pedido_id, nuevo_estado, personal, nota=''):
    """Avanza el pedido por el flujo permitido. ENTREGADO y CANCELADO NO se hacen aquí."""
    pedido = _pedido_bloqueado(pedido_id)
    permitidos = Pedido.TRANSICIONES.get(pedido.estado, ())
    if nuevo_estado not in permitidos:
        raise ReglaDeNegocioError(
            f"El pedido {pedido.numero} está «{pedido.get_estado_display()}»: no puede pasar a «{dict(Pedido.ESTADOS).get(nuevo_estado, nuevo_estado)}».")
    if pedido.estado == Pedido.PENDIENTE and pedido.reserva.estado != 'CHECKIN':
        raise ReglaDeNegocioError("La estadía de esta habitación ya no está activa; cancela el pedido.")
    anterior = pedido.estado
    pedido.estado = nuevo_estado
    if pedido.atendido_por_id is None:
        pedido.atendido_por = personal
    pedido.save(update_fields=['estado', 'atendido_por', 'actualizado'])
    _registrar_evento(pedido, anterior, nuevo_estado, personal, nota)
    return pedido


@transaction.atomic
def cancelar_pedido(pedido_id, actor, motivo='', *, como_cliente=False):
    """Cancela y devuelve el inventario. El cliente solo puede cancelar SU pedido mientras esté
    PENDIENTE; el personal puede cancelar mientras no se haya entregado."""
    pedido = _pedido_bloqueado(pedido_id)
    if como_cliente:
        if pedido.usuario_id != actor.pk:
            raise ReglaDeNegocioError("El pedido no existe.")
        if pedido.estado != Pedido.PENDIENTE:
            raise ReglaDeNegocioError("Solo puedes cancelar un pedido que aún no ha sido aceptado. Comunícate con recepción.")
    elif pedido.estado not in Pedido.ESTADOS_CANCELABLES:
        raise ReglaDeNegocioError(f"Un pedido «{pedido.get_estado_display()}» ya no se puede cancelar.")
    anterior = pedido.estado
    pedido.estado = Pedido.CANCELADO
    pedido.motivo_cancelacion = (motivo or ('Cancelado por el huésped' if como_cliente else '')).strip()[:200]
    pedido.save(update_fields=['estado', 'motivo_cancelacion', 'actualizado'])
    for d in pedido.detalles.select_related('producto'):
        if d.producto_id and d.producto.stock is not None:
            Producto.objects.select_for_update().filter(pk=d.producto_id).first()
            d.producto.stock += d.cantidad
            d.producto.save(update_fields=['stock', 'actualizado'])
    _registrar_evento(pedido, anterior, Pedido.CANCELADO, actor, pedido.motivo_cancelacion)
    return pedido


# ---------------------------------------------------------------------------
# Entrega + cobro (personal)
# ---------------------------------------------------------------------------
def _servicio_wbar():
    servicio = Servicio.objects.filter(nombre=NOMBRE_SERVICIO_WBAR).order_by('pk').first()
    if servicio is None:
        # Precio 0: es solo el "contenedor" del cargo; el precio real va en ServicioReserva.
        servicio = Servicio.objects.create(nombre=NOMBRE_SERVICIO_WBAR, categoria='MINIBAR', precio=Decimal('0.00'))
    return servicio


def confirmar_entrega(pedido_id, personal, *, codigo, edad_verificada=False, documento='',
                      forma_cobro, metodo_pago='', referencia=''):
    """Confirma la entrega en la puerta de la habitación. Exige, en este orden:
    estado EN_CAMINO, código correcto y no usado, (si hay alcohol) verificación de edad con
    documento, estadía activa y una decisión de cobro explícita (cobrar ahora o cargar a la
    cuenta). Nada se marca como pagado automáticamente.

    El conteo de códigos fallidos se confirma (commit) aunque la operación se rechace."""
    fallo = None
    with transaction.atomic():
        pedido = _pedido_bloqueado(pedido_id)
        if pedido.estado != Pedido.EN_CAMINO:
            raise ReglaDeNegocioError(
                f"El pedido {pedido.numero} está «{pedido.get_estado_display()}»; solo se entrega un pedido «En camino».")
        if pedido.codigo_usado_en is not None:
            raise ReglaDeNegocioError("El código de este pedido ya fue utilizado.")
        if pedido.codigo_bloqueado:
            raise ReglaDeNegocioError(
                "Se superó el número de intentos del código. Un administrador debe revisar este pedido.")
        if not pedido.codigo_coincide(codigo):
            pedido.intentos_codigo_fallidos += 1
            pedido.save(update_fields=['intentos_codigo_fallidos', 'actualizado'])
            _registrar_evento(pedido, pedido.estado, pedido.estado, personal,
                              f"Código de entrega incorrecto (intento {pedido.intentos_codigo_fallidos}).")
            restantes = Pedido.MAX_INTENTOS_CODIGO - pedido.intentos_codigo_fallidos
            fallo = ("El código no coincide. " + (f"Quedan {restantes} intentos." if restantes > 0
                     else "Pedido bloqueado: un administrador debe revisarlo."))
        else:
            if pedido.contiene_alcohol:
                if not edad_verificada:
                    raise ReglaDeNegocioError(
                        "Este pedido contiene alcohol: debes verificar la mayoría de edad con el documento del huésped.")
                if documento not in dict(Pedido.DOCUMENTOS):
                    raise ReglaDeNegocioError("Indica el documento con el que verificaste la edad.")
            if forma_cobro not in (Pedido.PAGO_COBRADO, Pedido.PAGO_CARGO_CUENTA):
                raise ReglaDeNegocioError("Indica si cobraste el pedido en la habitación o si se carga a la cuenta.")
            if forma_cobro == Pedido.PAGO_COBRADO and metodo_pago not in METODOS_COBRO_PERMITIDOS:
                raise ReglaDeNegocioError("Selecciona el medio con el que pagó el huésped.")

            reserva = Reserva.objects.select_for_update().get(pk=pedido.reserva_id)
            if reserva.estado != 'CHECKIN':
                raise ReglaDeNegocioError(
                    "La estadía de esta habitación ya no está activa: no se puede registrar el consumo. Cancela el pedido.")
            if ServicioReserva.objects.filter(detalle_wbar__pedido=pedido).exists() or PagoPedido.objects.filter(pedido=pedido).exists():
                raise ReglaDeNegocioError("Este pedido ya tiene cargos registrados; no se duplicarán.")

            servicio = _servicio_wbar()
            for d in pedido.detalles.all():
                d.servicio_reserva = ServicioReserva.objects.create(
                    reserva=reserva, servicio=servicio, cantidad=d.cantidad, precio_unitario=d.precio_unitario)
                d.save(update_fields=['servicio_reserva'])
            if forma_cobro == Pedido.PAGO_COBRADO:
                ref_limpia = (referencia or '').strip()
                pago = Pago.objects.create(
                    reserva=reserva, monto=pedido.total, metodo_pago=metodo_pago,
                    registrado_por=personal, referencia=ref_limpia[:60])
                PagoPedido.objects.create(
                    pedido=pedido, pago=pago, metodo=metodo_pago, monto=pedido.total,
                    referencia=ref_limpia[:40], registrado_por=personal)

            ahora = timezone.now()
            anterior = pedido.estado
            pedido.estado = Pedido.ENTREGADO
            pedido.entregado_en = ahora
            pedido.entregado_por = personal
            pedido.codigo_usado_en = ahora
            pedido.estado_pago = forma_cobro
            if pedido.contiene_alcohol:
                pedido.edad_verificada = True
                pedido.edad_verificada_por = personal
                pedido.documento_verificado = documento
            if pedido.atendido_por_id is None:
                pedido.atendido_por = personal
            pedido.save()
            _registrar_evento(pedido, anterior, Pedido.ENTREGADO, personal,
                              'Entrega verificada con código. ' + (
                                  f"Cobrado en la habitación ({metodo_pago})." if forma_cobro == Pedido.PAGO_COBRADO
                                  else 'Cargado a la cuenta de la habitación.'))
    if fallo:
        raise ReglaDeNegocioError(fallo)
    return pedido
