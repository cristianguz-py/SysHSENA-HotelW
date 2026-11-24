"""Vistas del panel de recepción (/recepcion/). GET consulta; POST (con CSRF) modifica.
Cada vista lleva @requiere_recepcion y cada operación vuelve a comprobar permisos en operaciones.py."""
import uuid
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import panel_servicios as ps
from .. import services
from ..models import (Habitacion, Huesped, OperacionRecepcion, Pago, Reserva, Servicio,
                      ServicioReserva, TipoHabitacion)
from ..panel_forms import HuespedForm, ReservaEdicionForm
from ..panel_views import _filtro_texto_reservas
from ..services import ReglaDeNegocioError
from . import operaciones as op
from .forms import (CheckinForm, CheckoutForm, HuespedAltaForm, PagoForm, ReservaRecepcionForm,
                    ServicioForm)
from .permisos import TEXTO_ROL, es_admin, requiere_recepcion

POR_PAGINA = 20


# ---------------------------------------------------------------------------
# Ayudas
# ---------------------------------------------------------------------------
def _ctx(request, seccion, **extra):
    base = {'seccion': seccion, 'usuario_nombre': request.session.get('usuario_nombre', ''),
            'usuario_rol_texto': TEXTO_ROL.get(request.session.get('usuario_rol'), ''),
            'hoy': timezone.localdate()}
    base.update(extra)
    return base


def _qs_sin(request, *claves):
    return urlencode([(k, v) for k, v in request.GET.items() if k not in claves and v != ''])


def _emitir_token(request, clave):
    """Token de un solo uso por formulario: evita que un doble clic o un reenvío repita la operación."""
    tokens = request.session.get('rc_tokens', {})
    token = uuid.uuid4().hex
    tokens[clave] = (tokens.get(clave, [])[-9:]) + [token]
    request.session['rc_tokens'] = tokens
    return token


def _consumir_token(request, clave):
    tokens = request.session.get('rc_tokens', {})
    lista = tokens.get(clave, [])
    token = request.POST.get('token', '')
    if not token or token not in lista:
        return False
    lista.remove(token)
    tokens[clave] = lista
    request.session['rc_tokens'] = tokens
    return True


def _ya_procesado(request, destino, **kw):
    messages.warning(request, "Esta operación ya se había procesado o el formulario caducó. "
                              "Revisa el resultado antes de repetirla.")
    return redirect(destino, **kw)


def _fecha(texto):
    return ps.fecha_o_none(texto)


def _reserva_o_404(reserva_id):
    return get_object_or_404(Reserva.objects.select_related('huesped', 'habitacion__tipo'), pk=reserva_id)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@requiere_recepcion
def inicio(request):
    return render(request, 'recepcion/inicio.html', _ctx(request, 'inicio', d=op.resumen_dashboard()))


# ---------------------------------------------------------------------------
# Huéspedes
# ---------------------------------------------------------------------------
@requiere_recepcion
def huespedes_lista(request):
    q = request.GET.get('q', '').strip()[:80]
    qs = Huesped.objects.all()
    for palabra in q.split():
        qs = qs.filter(Q(nombres__icontains=palabra) | Q(apellidos__icontains=palabra)
                       | Q(numero_documento__icontains=palabra) | Q(correo__icontains=palabra)
                       | Q(telefono__icontains=palabra))
    pagina = Paginator(qs.order_by('apellidos', 'nombres', 'id'), POR_PAGINA).get_page(request.GET.get('pagina'))
    return render(request, 'recepcion/huespedes_lista.html', _ctx(
        request, 'huespedes', pagina=pagina, q=q, qs_sin_pagina=_qs_sin(request, 'pagina')))


@requiere_recepcion
def huesped_nuevo(request):
    form = HuespedAltaForm(request.POST or None, initial={'tipo_documento': 'CC', 'nacionalidad': 'Colombiana'})
    if request.method == 'POST' and form.is_valid():
        try:
            huesped = op.alta_huesped(usuario=request.usuario, datos=form.datos())
        except ReglaDeNegocioError as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"Huésped {huesped.nombres} {huesped.apellidos} registrado.")
            return redirect('recepcion:huesped_detalle', huesped_id=huesped.pk)
    return render(request, 'recepcion/formulario.html', _ctx(
        request, 'huespedes', form=form, titulo="Registrar huésped",
        subtitulo="Es el registro de la persona (no crea una cuenta de acceso al sistema).",
        boton="Registrar huésped", volver='recepcion:huespedes'))


@requiere_recepcion
def huesped_detalle(request, huesped_id):
    huesped = get_object_or_404(Huesped, pk=huesped_id)
    form = HuespedForm(request.POST or None, instance=huesped)
    if request.method == 'POST':
        if form.is_valid():
            op.editar_huesped(usuario=request.usuario, form=form)
            messages.success(request, "Datos de contacto actualizados.")
            return redirect('recepcion:huesped_detalle', huesped_id=huesped.pk)
        messages.error(request, "Revisa los campos marcados.")
    reservas = op.anotar_saldos(huesped.reservas.select_related('habitacion__tipo').order_by('-fecha_entrada', '-id'))
    estadias = [r for r in reservas if r.estado in ('CHECKIN', 'CHECKOUT')]
    pagos = (Pago.objects.filter(reserva__huesped=huesped).select_related('reserva__habitacion', 'registrado_por')
             .order_by('-fecha_pago', '-id')[:30])
    return render(request, 'recepcion/huesped_detalle.html', _ctx(
        request, 'huespedes', h=huesped, form=form, reservas=reservas, estadias=estadias, pagos=pagos))


# ---------------------------------------------------------------------------
# Disponibilidad y nueva reserva
# ---------------------------------------------------------------------------
@requiere_recepcion
def disponibilidad(request):
    hoy = timezone.localdate()
    g = request.GET
    consultado = any(k in g for k in ('entrada', 'salida'))
    entrada = _fecha(g.get('entrada')) or hoy
    salida = _fecha(g.get('salida')) or (entrada + timedelta(days=1))
    huespedes = int(g['huespedes']) if g.get('huespedes', '').isdigit() and 0 < int(g['huespedes']) <= 20 else 1
    tipo = g.get('tipo', '')
    habitaciones, errores = [], []
    if consultado:
        if (g.get('entrada') and _fecha(g.get('entrada')) is None) or (g.get('salida') and _fecha(g.get('salida')) is None):
            errores.append("Alguna fecha no es válida.")
        errores += Reserva.errores_de_fechas(entrada, salida, hoy, validar_pasado=True)
        if not errores:
            qs = services.habitaciones_disponibles(entrada, salida, huespedes, hoy=hoy,
                                                   tipo_id=int(tipo) if tipo.isdigit() else None)
            habitaciones = list(qs)
    noches = Reserva.noches_entre(entrada, salida) if salida > entrada else 0
    for h in habitaciones:
        h.total_estancia = Reserva.alojamiento_para(h.tipo, entrada, salida)
        h.enlace = '?' + urlencode({'habitacion': h.pk, 'entrada': entrada.isoformat(),
                                    'salida': salida.isoformat(), 'huespedes': huespedes})
    for e in errores:
        messages.error(request, e)
    return render(request, 'recepcion/disponibilidad.html', _ctx(
        request, 'disponibilidad', entrada=entrada, salida=salida, huespedes=huespedes, tipo=tipo,
        tipos=TipoHabitacion.objects.order_by('precio_noche', 'id'), habitaciones=habitaciones,
        consultado=consultado and not errores, noches=noches))


@requiere_recepcion
def reserva_nueva(request):
    hoy = timezone.localdate()
    clave = 'reserva_nueva'
    if request.method == 'POST':
        form = ReservaRecepcionForm(request.POST)
        habitacion = Habitacion.objects.select_related('tipo').filter(pk=request.POST.get('habitacion') or 0).first() \
            if (request.POST.get('habitacion') or '').isdigit() else None
        if form.is_valid():
            cd = form.cleaned_data
            if not _consumir_token(request, clave):
                return _ya_procesado(request, 'recepcion:reservas')
            try:
                reserva, existente = op.crear_reserva(
                    usuario=request.usuario, habitacion_id=cd['habitacion'], tipo_id=cd['tipo_habitacion'].pk,
                    datos_huesped=form.datos_huesped(), entrada=cd['fecha_entrada'], salida=cd['fecha_salida'],
                    num_huespedes=cd['num_huespedes'], canal=cd['canal_reserva'])
            except ReglaDeNegocioError as e:
                for motivo in e.mensajes:
                    form.add_error(None, motivo)
            else:
                if existente is not None:
                    messages.info(request, f"El documento ya estaba registrado a nombre de {existente.nombres} "
                                           f"{existente.apellidos}: se usó ese huésped y sus datos no se modificaron.")
                messages.success(request, f"Reserva RW-{reserva.pk:06d} creada y confirmada.")
                return redirect('recepcion:reserva_detalle', reserva_id=reserva.pk)
    else:
        habitacion = None
        if (request.GET.get('habitacion') or '').isdigit():
            habitacion = Habitacion.objects.select_related('tipo').filter(pk=int(request.GET['habitacion'])).first()
        if habitacion is None:
            messages.info(request, "Primero elige una habitación disponible para las fechas de la estancia.")
            return redirect('recepcion:disponibilidad')
        inicial = {'tipo_documento': 'CC', 'nacionalidad': 'Colombiana', 'canal_reserva': 'PRESENCIAL',
                   'fecha_entrada': (_fecha(request.GET.get('entrada')) or hoy).isoformat(),
                   'fecha_salida': (_fecha(request.GET.get('salida')) or hoy + timedelta(days=1)).isoformat(),
                   'num_huespedes': request.GET.get('huespedes') if (request.GET.get('huespedes') or '').isdigit() else 1,
                   'tipo_habitacion': habitacion.tipo_id, 'habitacion': habitacion.pk}
        if (request.GET.get('huesped') or '').isdigit():
            h = Huesped.objects.filter(pk=int(request.GET['huesped'])).first()
            if h:
                inicial.update({'tipo_documento': h.tipo_documento, 'numero_documento': h.numero_documento,
                                'nombres': h.nombres, 'apellidos': h.apellidos, 'telefono': h.telefono,
                                'correo': h.correo, 'nacionalidad': h.nacionalidad, 'es_extranjero': h.es_extranjero})
        form = ReservaRecepcionForm(initial=inicial)
    return render(request, 'recepcion/reserva_nueva.html', _ctx(
        request, 'reservas', form=form, habitacion=habitacion, token=_emitir_token(request, clave)))


# ---------------------------------------------------------------------------
# Reservas
# ---------------------------------------------------------------------------
@requiere_recepcion
def reservas_lista(request):
    qs = Reserva.objects.select_related('huesped', 'habitacion__tipo')
    q = request.GET.get('q', '').strip()[:80]
    if q:
        qs = qs.filter(_filtro_texto_reservas(q))
    estado = request.GET.get('estado', '')
    if estado in dict(Reserva.ESTADOS_RESERVA):
        qs = qs.filter(estado=estado)
    habitacion = request.GET.get('habitacion', '').strip()[:10]
    if habitacion:
        qs = qs.filter(habitacion__numero__iexact=habitacion)
    desde, hasta = _fecha(request.GET.get('desde')), _fecha(request.GET.get('hasta'))
    if (request.GET.get('desde') and desde is None) or (request.GET.get('hasta') and hasta is None):
        messages.warning(request, "Alguna fecha del filtro no es válida y se ignoró.")
    if desde:
        qs = qs.filter(fecha_entrada__gte=desde)
    if hasta:
        qs = qs.filter(fecha_entrada__lte=hasta)
    pagina = Paginator(qs.order_by('-fecha_entrada', '-id'), POR_PAGINA).get_page(request.GET.get('pagina'))
    pagina.object_list = op.anotar_saldos(pagina.object_list)
    return render(request, 'recepcion/reservas_lista.html', _ctx(
        request, 'reservas', pagina=pagina, q=q, estado=estado, habitacion=habitacion,
        desde=request.GET.get('desde', ''), hasta=request.GET.get('hasta', ''),
        estados=Reserva.ESTADOS_RESERVA, qs_sin_pagina=_qs_sin(request, 'pagina')))


@requiere_recepcion
def reserva_detalle(request, reserva_id):
    reserva = _reserva_o_404(reserva_id)
    cuenta = op.cuenta_reserva(reserva)
    hoy = timezone.localdate()
    tiene_pagos = reserva.pagos.exists()
    return render(request, 'recepcion/reserva_detalle.html', _ctx(
        request, 'reservas', r=reserva, cuenta=cuenta, noches=reserva.calcular_noches(),
        pagos=reserva.pagos.select_related('registrado_por').order_by('-fecha_pago', '-id'),
        servicios=reserva.servicios.select_related('servicio').order_by('-id'),
        operaciones=reserva.operaciones.select_related('usuario')[:15],
        editable=reserva.estado in services.ESTADOS_EDITABLES,
        puede_confirmar=reserva.estado == 'PENDIENTE',
        puede_cancelar=reserva.estado in services.ESTADOS_EDITABLES and (es_admin(request.usuario) or not tiene_pagos),
        cancelar_bloqueado=reserva.estado in services.ESTADOS_EDITABLES and tiene_pagos and not es_admin(request.usuario),
        puede_checkin=reserva.estado == 'CONFIRMADA' and reserva.fecha_entrada <= hoy < reserva.fecha_salida,
        puede_pagar=reserva.estado in ('PENDIENTE', 'CONFIRMADA', 'CHECKIN') and cuenta['saldo_pendiente'] > 0,
        puede_servicio=reserva.estado in ('CONFIRMADA', 'CHECKIN'),
        puede_checkout=reserva.estado == 'CHECKIN'))


@requiere_recepcion
def reserva_editar(request, reserva_id):
    reserva = _reserva_o_404(reserva_id)
    if reserva.estado not in services.ESTADOS_EDITABLES:
        messages.error(request, f"No se puede editar una reserva en estado «{reserva.get_estado_display()}».")
        return redirect('recepcion:reserva_detalle', reserva_id=reserva.pk)
    inicial = {'habitacion': reserva.habitacion_id, 'fecha_entrada': reserva.fecha_entrada,
               'fecha_salida': reserva.fecha_salida, 'num_huespedes': reserva.num_huespedes,
               'canal_reserva': reserva.canal_reserva}
    form = ReservaEdicionForm(request.POST or None, initial=inicial)
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        try:
            op.modificar_reserva(usuario=request.usuario, reserva_id=reserva.pk, habitacion_id=d['habitacion'].pk,
                                 entrada=d['fecha_entrada'], salida=d['fecha_salida'],
                                 num_huespedes=d['num_huespedes'], canal=d['canal_reserva'])
        except ReglaDeNegocioError as e:
            for motivo in e.mensajes:
                form.add_error(None, motivo)
        else:
            messages.success(request, f"Reserva RW-{reserva.pk:06d} actualizada. El importe se recalculó desde la base de datos.")
            return redirect('recepcion:reserva_detalle', reserva_id=reserva.pk)
    return render(request, 'recepcion/formulario.html', _ctx(
        request, 'reservas', form=form, titulo=f"Editar reserva RW-{reserva.pk:06d}",
        subtitulo=f"{reserva.huesped.nombres} {reserva.huesped.apellidos}",
        nota="Al guardar se vuelve a validar la disponibilidad y el importe se recalcula con la tarifa vigente.",
        boton="Guardar cambios", volver='recepcion:reserva_detalle', volver_args=[reserva.pk]))


def _accion_reserva(request, reserva_id, *, titulo, texto, boton, funcion, exito, peligro=False):
    reserva = _reserva_o_404(reserva_id)
    if request.method == 'POST':
        try:
            funcion(usuario=request.usuario, reserva_id=reserva.pk)
        except ReglaDeNegocioError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, exito.format(id=f"{reserva.pk:06d}"))
        return redirect('recepcion:reserva_detalle', reserva_id=reserva.pk)
    return render(request, 'recepcion/confirmar_accion.html', _ctx(
        request, 'reservas', r=reserva, titulo=titulo, texto=texto, boton=boton, peligro=peligro,
        aviso_pagos=(reserva.pagos.aggregate(t=Sum('monto'))['t'] if peligro else None)))


@requiere_recepcion
def reserva_confirmar(request, reserva_id):
    return _accion_reserva(request, reserva_id, titulo="Confirmar reserva",
                           texto="Se volverá a comprobar la disponibilidad antes de confirmar.",
                           boton="Confirmar reserva", funcion=op.confirmar_reserva, exito="Reserva RW-{id} confirmada.")


@requiere_recepcion
def reserva_cancelar(request, reserva_id):
    return _accion_reserva(request, reserva_id, titulo="Cancelar reserva",
                           texto="La reserva quedará como cancelada (no se borra) y la habitación volverá a estar libre para esas fechas.",
                           boton="Sí, cancelar reserva", funcion=op.cancelar_reserva,
                           exito="Reserva RW-{id} cancelada.", peligro=True)


# ---------------------------------------------------------------------------
# Check-in
# ---------------------------------------------------------------------------
@requiere_recepcion
def llegadas(request):
    hoy = timezone.localdate()
    q = request.GET.get('q', '').strip()[:80]
    base = Reserva.objects.select_related('huesped', 'habitacion__tipo')
    if q:
        base = base.filter(_filtro_texto_reservas(q))
    pendientes = base.filter(estado__in=('PENDIENTE', 'CONFIRMADA'), fecha_entrada__lte=hoy,
                             fecha_salida__gt=hoy).order_by('fecha_entrada', 'habitacion__numero')
    proximas = base.filter(estado__in=('PENDIENTE', 'CONFIRMADA'), fecha_entrada__gt=hoy,
                           fecha_entrada__lte=hoy + timedelta(days=7)).order_by('fecha_entrada', 'habitacion__numero')
    return render(request, 'recepcion/llegadas.html', _ctx(
        request, 'llegadas', q=q, pendientes=op.anotar_saldos(pendientes[:100]), proximas=op.anotar_saldos(proximas[:100])))


@requiere_recepcion
def checkin(request, reserva_id):
    reserva = _reserva_o_404(reserva_id)
    clave = f'checkin{reserva.pk}'
    form = CheckinForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            op.hacer_checkin(usuario=request.usuario, reserva_id=reserva.pk,
                             observaciones=form.cleaned_data['observaciones'])
        except ReglaDeNegocioError as e:
            messages.error(request, str(e))
            return redirect('recepcion:checkin', reserva_id=reserva.pk)
        messages.success(request, f"Check-in realizado: habitación {reserva.habitacion.numero} ocupada.")
        return redirect('recepcion:reserva_detalle', reserva_id=reserva.pk)
    cuenta = op.cuenta_reserva(reserva)
    return render(request, 'recepcion/checkin.html', _ctx(
        request, 'llegadas', r=reserva, form=form, cuenta=cuenta, noches=reserva.calcular_noches(),
        habilitada=reserva.estado == 'CONFIRMADA' and reserva.fecha_entrada <= timezone.localdate() < reserva.fecha_salida))


# ---------------------------------------------------------------------------
# Alojados, salidas y check-out
# ---------------------------------------------------------------------------
@requiere_recepcion
def alojados(request):
    q = request.GET.get('q', '').strip()[:80]
    qs = Reserva.objects.filter(estado='CHECKIN').select_related('huesped', 'habitacion__tipo')
    if q:
        qs = qs.filter(_filtro_texto_reservas(q))
    pagina = Paginator(qs.order_by('fecha_salida', 'habitacion__numero'), POR_PAGINA).get_page(request.GET.get('pagina'))
    pagina.object_list = op.anotar_saldos(pagina.object_list)
    return render(request, 'recepcion/alojados.html', _ctx(
        request, 'alojados', pagina=pagina, q=q, qs_sin_pagina=_qs_sin(request, 'pagina')))


@requiere_recepcion
def salidas(request):
    hoy = timezone.localdate()
    q = request.GET.get('q', '').strip()[:80]
    base = Reserva.objects.filter(estado='CHECKIN').select_related('huesped', 'habitacion__tipo')
    if q:
        base = base.filter(_filtro_texto_reservas(q))
    hoy_y_vencidas = base.filter(fecha_salida__lte=hoy).order_by('fecha_salida', 'habitacion__numero')
    proximas = base.filter(fecha_salida__gt=hoy, fecha_salida__lte=hoy + timedelta(days=7)).order_by('fecha_salida', 'habitacion__numero')
    return render(request, 'recepcion/salidas.html', _ctx(
        request, 'salidas', q=q, hoy_y_vencidas=op.anotar_saldos(hoy_y_vencidas[:100]), proximas=op.anotar_saldos(proximas[:100])))


@requiere_recepcion
def checkout(request, reserva_id):
    reserva = _reserva_o_404(reserva_id)
    clave = f'checkout{reserva.pk}'
    form = CheckoutForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        if not _consumir_token(request, clave):
            return _ya_procesado(request, 'recepcion:reserva_detalle', reserva_id=reserva.pk)
        monto, metodo = form.cleaned_data.get('monto'), form.cleaned_data.get('metodo_pago')
        ahora = timezone.now()
        pago_hecho = False
        try:
            if monto and monto > 0:
                op.registrar_pago(usuario=request.usuario, reserva_id=reserva.pk, monto=monto, metodo=metodo,
                                  referencia=form.cleaned_data.get('referencia', ''), ahora=ahora, con_cargo_tardio=True)
                pago_hecho = True
            op.hacer_checkout(usuario=request.usuario, reserva_id=reserva.pk, ahora=ahora)
        except ReglaDeNegocioError as e:
            if pago_hecho:
                messages.success(request, f"Pago de {op.cop(monto)} registrado.")
            messages.error(request, str(e))
            return redirect('recepcion:checkout', reserva_id=reserva.pk)
        messages.success(request, f"Check-out completado. La habitación {reserva.habitacion.numero} pasó a estado EN ASEO.")
        return redirect('recepcion:reserva_detalle', reserva_id=reserva.pk)
    if reserva.estado != 'CHECKIN':
        messages.warning(request, "Esta reserva no tiene un check-in activo; no se puede hacer check-out.")
    cuenta = services.calcular_cuenta(reserva)            # incluye cargo por salida tardía a esta hora
    if request.method != 'POST' and cuenta['saldo_pendiente'] > 0:
        form = CheckoutForm(initial={'monto': cuenta['saldo_pendiente']})
    return render(request, 'recepcion/checkout.html', _ctx(
        request, 'salidas', r=reserva, form=form, cuenta=cuenta, noches=reserva.calcular_noches(),
        pagos=reserva.pagos.select_related('registrado_por').order_by('-fecha_pago'),
        servicios=reserva.servicios.select_related('servicio'), token=_emitir_token(request, clave)))


# ---------------------------------------------------------------------------
# Servicios adicionales
# ---------------------------------------------------------------------------
@requiere_recepcion
def servicios_lista(request):
    q = request.GET.get('q', '').strip()[:80]
    qs = ServicioReserva.objects.select_related('servicio', 'reserva__habitacion', 'reserva__huesped')
    if q:
        qs = qs.filter(_filtro_texto_reservas_sr(q))
    pagina = Paginator(qs.order_by('-id'), POR_PAGINA).get_page(request.GET.get('pagina'))
    return render(request, 'recepcion/servicios_lista.html', _ctx(
        request, 'servicios', pagina=pagina, q=q, catalogo=Servicio.objects.order_by('categoria', 'nombre'),
        qs_sin_pagina=_qs_sin(request, 'pagina')))


def _filtro_texto_reservas_sr(q):
    """Mismo buscador de reservas, aplicado a través de ServicioReserva.reserva."""
    ids = Reserva.objects.filter(_filtro_texto_reservas(q)).values('pk')
    return Q(reserva_id__in=ids)


@requiere_recepcion
def servicio_nuevo(request):
    clave = 'servicio_nuevo'
    inicial = {'cantidad': 1}
    if (request.GET.get('reserva') or '').isdigit():
        inicial['reserva'] = int(request.GET['reserva'])
    form = ServicioForm(request.POST or None, initial=inicial)
    reserva = None
    if request.method == 'POST' and form.is_valid():
        cd = form.cleaned_data
        if not _consumir_token(request, clave):
            return _ya_procesado(request, 'recepcion:servicios')
        try:
            consumo = op.cargar_servicio(usuario=request.usuario, reserva_id=cd['reserva'],
                                         servicio_id=cd['servicio'].pk, cantidad=cd['cantidad'])
        except ReglaDeNegocioError as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"Cargado: {consumo.cantidad} × {cd['servicio'].nombre} = {op.cop(consumo.subtotal)} "
                                      f"a la reserva RW-{cd['reserva']:06d}.")
            return redirect('recepcion:reserva_detalle', reserva_id=cd['reserva'])
    ref = form['reserva'].value()
    if str(ref or '').isdigit():
        reserva = Reserva.objects.select_related('huesped', 'habitacion').filter(pk=int(ref)).first()
    return render(request, 'recepcion/formulario.html', _ctx(
        request, 'servicios', form=form, titulo="Registrar servicio adicional",
        subtitulo="El precio sale del catálogo; el subtotal = cantidad × precio y se suma a la cuenta de la reserva.",
        boton="Cargar servicio", volver='recepcion:servicios', reserva=reserva, token=_emitir_token(request, clave)))


# ---------------------------------------------------------------------------
# Pagos y abonos
# ---------------------------------------------------------------------------
@requiere_recepcion
def pagos_lista(request):
    hoy = timezone.localdate()
    qs = Pago.objects.select_related('reserva__huesped', 'reserva__habitacion', 'registrado_por')
    q = request.GET.get('q', '').strip()[:80]
    if q:
        qs = qs.filter(reserva_id__in=Reserva.objects.filter(_filtro_texto_reservas(q)).values('pk'))
    metodo = request.GET.get('metodo', '')
    if metodo in dict(Pago.METODOS):
        qs = qs.filter(metodo_pago=metodo)
    desde, hasta = _fecha(request.GET.get('desde')), _fecha(request.GET.get('hasta'))
    if (request.GET.get('desde') and desde is None) or (request.GET.get('hasta') and hasta is None):
        messages.warning(request, "Alguna fecha del filtro no es válida y se ignoró.")
    if desde:
        qs = qs.filter(fecha_pago__gte=op.rango_del_dia(desde)[0])
    if hasta:
        qs = qs.filter(fecha_pago__lt=op.rango_del_dia(hasta)[1])
    total = qs.aggregate(t=Sum('monto'))['t'] or Decimal('0')
    inicio, fin = op.rango_del_dia(hoy)
    hoy_total = Pago.objects.filter(fecha_pago__gte=inicio, fecha_pago__lt=fin).aggregate(t=Sum('monto'))['t'] or Decimal('0')
    pagina = Paginator(qs.order_by('-fecha_pago', '-id'), POR_PAGINA).get_page(request.GET.get('pagina'))
    return render(request, 'recepcion/pagos_lista.html', _ctx(
        request, 'pagos', pagina=pagina, q=q, metodo=metodo, metodos=Pago.METODOS,
        desde=request.GET.get('desde', ''), hasta=request.GET.get('hasta', ''),
        total=total, hoy_total=hoy_total, qs_sin_pagina=_qs_sin(request, 'pagina')))


@requiere_recepcion
def pago_nuevo(request):
    clave = 'pago_nuevo'
    inicial = {'metodo_pago': 'EFECTIVO'}
    if (request.GET.get('reserva') or '').isdigit():
        inicial['reserva'] = int(request.GET['reserva'])
    form = PagoForm(request.POST or None, initial=inicial)
    if request.method == 'POST' and form.is_valid():
        cd = form.cleaned_data
        if not _consumir_token(request, clave):
            return _ya_procesado(request, 'recepcion:pagos')
        try:
            pago = op.registrar_pago(usuario=request.usuario, reserva_id=cd['reserva'], monto=cd['monto'],
                                     metodo=cd['metodo_pago'], referencia=cd['referencia'])
        except ReglaDeNegocioError as e:
            form.add_error(None, str(e))
        else:
            messages.success(request, f"Pago de {op.cop(pago.monto)} registrado en la reserva RW-{cd['reserva']:06d}.")
            return redirect('recepcion:reserva_detalle', reserva_id=cd['reserva'])
    reserva, cuenta = None, None
    ref = form['reserva'].value()
    if str(ref or '').isdigit():
        reserva = Reserva.objects.select_related('huesped', 'habitacion').filter(pk=int(ref)).first()
        if reserva:
            cuenta = op.cuenta_reserva(reserva)
    return render(request, 'recepcion/formulario.html', _ctx(
        request, 'pagos', form=form, titulo="Registrar pago o abono",
        subtitulo="Los pagos no se editan ni se eliminan: un abono no puede superar el saldo pendiente.",
        boton="Registrar pago", volver='recepcion:pagos', reserva=reserva, cuenta=cuenta,
        token=_emitir_token(request, clave)))


# ---------------------------------------------------------------------------
# Habitaciones
# ---------------------------------------------------------------------------
SITUACIONES = [('disponible', 'Disponible'), ('reservada', 'Reservada'), ('ocupada', 'Ocupada'),
               ('aseo', 'En aseo'), ('fuera', 'Mantenimiento / fuera de servicio')]


@requiere_recepcion
def habitaciones(request):
    q = request.GET.get('q', '').strip()[:20]
    qs = Habitacion.objects.select_related('tipo')
    if q:
        qs = qs.filter(numero__icontains=q)
    tipo = request.GET.get('tipo', '')
    if tipo.isdigit():
        qs = qs.filter(tipo_id=int(tipo))
    lista, _ = services.anotar_estados(qs.order_by('piso', 'numero'))
    conteo = {k: sum(1 for h in lista if h.estado_clase == k) for k, _ in SITUACIONES}
    situacion = request.GET.get('situacion', '')
    if situacion in conteo:
        lista = [h for h in lista if h.estado_clase == situacion]
    pagina = Paginator(lista, 40).get_page(request.GET.get('pagina'))
    return render(request, 'recepcion/habitaciones.html', _ctx(
        request, 'habitaciones', pagina=pagina, q=q, tipo=tipo, situacion=situacion, situaciones=SITUACIONES,
        conteo=conteo, tipos=TipoHabitacion.objects.order_by('nombre'), qs_sin_pagina=_qs_sin(request, 'pagina')))


@requiere_recepcion
@require_POST
def habitacion_habilitar(request, habitacion_id):
    try:
        hab = op.habilitar_habitacion(usuario=request.usuario, habitacion_id=habitacion_id)
    except ReglaDeNegocioError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"Habitación {hab.numero} habilitada: ahora está disponible.")
    return redirect('recepcion:habitaciones')


# ---------------------------------------------------------------------------
# Historial (solo lectura)
# ---------------------------------------------------------------------------
@requiere_recepcion
def historial(request):
    qs = OperacionRecepcion.objects.select_related('usuario', 'reserva', 'huesped')
    tipo = request.GET.get('tipo', '')
    if tipo in dict(OperacionRecepcion.TIPOS):
        qs = qs.filter(tipo=tipo)
    q = request.GET.get('q', '').strip()[:80]
    if q:
        qs = qs.filter(Q(reserva_id__in=Reserva.objects.filter(_filtro_texto_reservas(q)).values('pk'))
                       | Q(huesped__numero_documento__icontains=q) | Q(huesped__nombres__icontains=q)
                       | Q(huesped__apellidos__icontains=q))
    desde, hasta = _fecha(request.GET.get('desde')), _fecha(request.GET.get('hasta'))
    if (request.GET.get('desde') and desde is None) or (request.GET.get('hasta') and hasta is None):
        messages.warning(request, "Alguna fecha del filtro no es válida y se ignoró.")
    if desde:
        qs = qs.filter(fecha__gte=op.rango_del_dia(desde)[0])
    if hasta:
        qs = qs.filter(fecha__lt=op.rango_del_dia(hasta)[1])
    mias = request.GET.get('mias') == '1'
    if mias:
        qs = qs.filter(usuario=request.usuario)
    pagina = Paginator(qs, 25).get_page(request.GET.get('pagina'))
    return render(request, 'recepcion/historial.html', _ctx(
        request, 'historial', pagina=pagina, tipo=tipo, q=q, mias=mias, tipos=OperacionRecepcion.TIPOS,
        desde=request.GET.get('desde', ''), hasta=request.GET.get('hasta', ''),
        qs_sin_pagina=_qs_sin(request, 'pagina')))
