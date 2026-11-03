"""
Panel de gestión de Hotel W (/gestion/). Solo ADMIN_GERENTE.

Todo funciona con formularios HTML, GET para consultar y POST (con CSRF) para cambiar datos.
Los permisos se comprueban y la cuenta se relee de la BD en CADA petición.
"""
import csv
import re
from datetime import timedelta
from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import panel_servicios as ps
from . import services
from .models import (Habitacion, Hotel, Huesped, Pago, Reserva, Servicio, ServicioReserva,
                     TipoHabitacion, Usuario)
from .panel_forms import (ConsumoForm, HabitacionForm, HuespedForm, ReservaEdicionForm,
                          TipoHabitacionForm)
from .services import ReglaDeNegocioError

ROL_ADMIN = 'ADMIN_GERENTE'
POR_PAGINA = 20


def requiere_admin(vista):
    """Solo administrador/gerente con cuenta activa. Sin sesión → login; otro rol → 403."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if 'usuario_id' not in request.session:
            return redirect('login')
        if request.session.get('usuario_rol') != ROL_ADMIN:
            raise PermissionDenied
        usuario = Usuario.objects.filter(pk=request.session['usuario_id'], activo=True, rol=ROL_ADMIN).first()
        if usuario is None:
            request.session.flush()
            return redirect('login')
        request.usuario = usuario
        return vista(request, *args, **kwargs)
    return envoltura


def _ctx(request, seccion, **extra):
    base = {
        'seccion': seccion,
        'usuario_nombre': request.session.get('usuario_nombre', ''),
        'usuario_rol_texto': 'Administrador / Gerente',
        'q_global': request.GET.get('q', '') if seccion == 'buscar' else '',
        'hoy': timezone.localdate(),
    }
    base.update(extra)
    return base


def _qs_sin(request, *claves):
    """Querystring actual sin ciertas claves (para enlaces de orden y paginación)."""
    pares = [(k, v) for k, v in request.GET.items() if k not in claves and v != '']
    return urlencode(pares)


def _avisar(request, avisos):
    for a in avisos:
        messages.warning(request, a)


# ===========================================================================
# DASHBOARD
# ===========================================================================
@requiere_admin
def dashboard(request):
    hoy = timezone.localdate()
    desde, hasta, avisos = ps.leer_periodo(request.GET, hoy)
    _avisar(request, avisos)
    meses = request.GET.get('meses', '6')
    meses = int(meses) if meses in ('3', '6', '12') else 6

    # Disponibilidad para las fechas elegidas (por defecto, esta noche)
    d_entrada = ps.fecha_o_none(request.GET.get('disp_entrada')) or hoy
    d_salida = ps.fecha_o_none(request.GET.get('disp_salida')) or (d_entrada + timedelta(days=1))
    if d_salida <= d_entrada:
        messages.warning(request, "En disponibilidad, la salida debe ser posterior a la entrada; se usó una noche.")
        d_salida = d_entrada + timedelta(days=1)
    disponibles = services.habitaciones_disponibles(d_entrada, d_salida, hoy=hoy).count()
    operativas = (Habitacion.objects.filter(activa=True)
                  .exclude(estado__in=Habitacion.ESTADOS_NO_RESERVABLES).count())

    actual = ps.metricas_periodo(desde, hasta)
    ant_desde, ant_hasta = ps.periodo_anterior(desde, hasta)
    anterior = ps.metricas_periodo(ant_desde, ant_hasta)

    ctx = _ctx(
        request, 'inicio', desde=desde, hasta=hasta, meses=meses,
        d_entrada=d_entrada, d_salida=d_salida, disponibles=disponibles, operativas=operativas,
        m=actual, ant=anterior, ant_desde=ant_desde, ant_hasta=ant_hasta,
        var_reservas=ps.variacion(actual['total'], anterior['total']),
        var_reservado=ps.variacion(actual['reservado'], anterior['reservado']),
        var_cancelacion=ps.variacion(actual['pct_cancelacion'], anterior['pct_cancelacion']),
        hoy_conteos=ps.conteos_de_hoy(hoy),
        tendencia=ps.tendencia_mensual(hasta, meses),
        tipos=ps.distribucion_por_tipo(desde, hasta),
        actividad=(Reserva.objects.select_related('huesped', 'habitacion')
                   .order_by('-fecha_creacion', '-id')[:8]),
    )
    return render(request, 'panel/dashboard.html', ctx)


# ===========================================================================
# BÚSQUEDA GLOBAL
# ===========================================================================
def _filtro_texto_reservas(q):
    q = q.strip()
    codigo = re.fullmatch(r'(?:rw-?)?0*(\d+)', q, flags=re.IGNORECASE)
    if codigo:
        return (Q(pk=int(codigo.group(1))) | Q(habitacion__numero__iexact=q)
                | Q(huesped__numero_documento__icontains=q))
    filtro = Q()
    for palabra in q.split():
        filtro &= (Q(huesped__nombres__icontains=palabra) | Q(huesped__apellidos__icontains=palabra)
                   | Q(huesped__numero_documento__icontains=palabra) | Q(habitacion__numero__icontains=palabra))
    return filtro


@requiere_admin
def buscar(request):
    q = request.GET.get('q', '').strip()[:80]
    reservas, huespedes, habitaciones = [], [], []
    if q:
        reservas = list(Reserva.objects.filter(_filtro_texto_reservas(q))
                        .select_related('huesped', 'habitacion__tipo').order_by('-fecha_entrada', '-id')[:10])
        filtro_h = Q()
        for palabra in q.split():
            filtro_h &= (Q(nombres__icontains=palabra) | Q(apellidos__icontains=palabra)
                         | Q(numero_documento__icontains=palabra) | Q(correo__icontains=palabra))
        huespedes = list(Huesped.objects.filter(filtro_h).order_by('apellidos', 'nombres')[:10])
        habitaciones = list(Habitacion.objects.filter(Q(numero__icontains=q) | Q(tipo__nombre__icontains=q))
                            .select_related('tipo').order_by('piso', 'numero')[:10])
    return render(request, 'panel/buscar.html', _ctx(
        request, 'buscar', q=q, reservas=reservas, huespedes=huespedes, habitaciones=habitaciones,
        total=len(reservas) + len(huespedes) + len(habitaciones)))


# ===========================================================================
# RESERVAS
# ===========================================================================
ORDENES = {
    'codigo': 'id', 'huesped': 'huesped__apellidos', 'habitacion': 'habitacion__numero',
    'tipo': 'habitacion__tipo__nombre', 'entrada': 'fecha_entrada', 'salida': 'fecha_salida',
    'estado': 'estado', 'valor': 'valor_total', 'canal': 'canal_reserva',
}


@requiere_admin
def reservas_lista(request):
    qs = (Reserva.objects.select_related('huesped', 'habitacion__tipo')
          .annotate(pagado=Sum('pagos__monto')))
    q = request.GET.get('q', '').strip()[:80]
    if q:
        qs = qs.filter(_filtro_texto_reservas(q))
    estado = request.GET.get('estado', '')
    if estado in dict(Reserva.ESTADOS_RESERVA):
        qs = qs.filter(estado=estado)
    canal = request.GET.get('canal', '')
    if canal in dict(Reserva.CANALES):
        qs = qs.filter(canal_reserva=canal)
    tipo = request.GET.get('tipo', '')
    if tipo.isdigit():
        qs = qs.filter(habitacion__tipo_id=int(tipo))
    desde, hasta = ps.fecha_o_none(request.GET.get('desde')), ps.fecha_o_none(request.GET.get('hasta'))
    if request.GET.get('desde') and desde is None or request.GET.get('hasta') and hasta is None:
        messages.warning(request, "Alguna fecha del filtro no es válida y se ignoró.")
    if desde:
        qs = qs.filter(fecha_entrada__gte=desde)
    if hasta:
        qs = qs.filter(fecha_entrada__lte=hasta)

    orden = request.GET.get('orden', '-codigo')
    campo = orden.lstrip('-')
    if campo not in ORDENES:
        orden, campo = '-codigo', 'codigo'
    qs = qs.order_by(('-' if orden.startswith('-') else '') + ORDENES[campo], '-id')

    pagina = Paginator(qs, POR_PAGINA).get_page(request.GET.get('pagina'))
    return render(request, 'panel/reservas_lista.html', _ctx(
        request, 'reservas', pagina=pagina, q=q, estado=estado, canal=canal, tipo=tipo,
        desde=request.GET.get('desde', ''), hasta=request.GET.get('hasta', ''),
        orden=orden, estados=Reserva.ESTADOS_RESERVA, canales=Reserva.CANALES,
        tipos=TipoHabitacion.objects.order_by('nombre'),
        qs_sin_orden=_qs_sin(request, 'orden', 'pagina'), qs_sin_pagina=_qs_sin(request, 'pagina')))


@requiere_admin
def reserva_detalle(request, reserva_id):
    reserva = get_object_or_404(
        Reserva.objects.select_related('huesped', 'habitacion__tipo', 'usuario'), pk=reserva_id)
    cuenta = services.calcular_cuenta(reserva)
    pagos = list(reserva.pagos.order_by('-fecha_pago'))
    servicios = list(reserva.servicios.select_related('servicio'))
    return render(request, 'panel/reserva_detalle.html', _ctx(
        request, 'reservas', r=reserva, cuenta=cuenta, pagos=pagos, servicios=servicios,
        noches=reserva.calcular_noches(), alojamiento=reserva.calcular_alojamiento(),
        editable=reserva.estado in services.ESTADOS_EDITABLES,
        puede_confirmar=reserva.estado == 'PENDIENTE',
        puede_cancelar=reserva.estado in services.ESTADOS_EDITABLES))


@requiere_admin
def reserva_editar(request, reserva_id):
    reserva = get_object_or_404(Reserva.objects.select_related('huesped', 'habitacion__tipo'), pk=reserva_id)
    if reserva.estado not in services.ESTADOS_EDITABLES:
        messages.error(request, f"No se puede editar una reserva en estado «{reserva.get_estado_display()}».")
        return redirect('gestion_reserva_detalle', reserva_id=reserva.pk)
    inicial = {'habitacion': reserva.habitacion_id, 'fecha_entrada': reserva.fecha_entrada,
               'fecha_salida': reserva.fecha_salida, 'num_huespedes': reserva.num_huespedes,
               'canal_reserva': reserva.canal_reserva}
    form = ReservaEdicionForm(request.POST or None, initial=inicial)
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        try:
            services.modificar_reserva(
                reserva.pk, habitacion_id=d['habitacion'].pk, entrada=d['fecha_entrada'],
                salida=d['fecha_salida'], num_huespedes=d['num_huespedes'], canal=d['canal_reserva'])
        except ReglaDeNegocioError as e:
            for motivo in e.mensajes:
                form.add_error(None, motivo)
        else:
            messages.success(request, f"Reserva {reserva.pk:06d} actualizada. El importe se recalculó desde la base de datos.")
            return redirect('gestion_reserva_detalle', reserva_id=reserva.pk)
    return render(request, 'panel/reserva_form.html', _ctx(request, 'reservas', form=form, r=reserva))


def _accion_reserva(request, reserva_id, *, titulo, texto, boton, servicio, exito, peligro=False):
    reserva = get_object_or_404(Reserva.objects.select_related('huesped', 'habitacion__tipo'), pk=reserva_id)
    if request.method == 'POST':
        try:
            servicio(reserva.pk)
        except ReglaDeNegocioError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, exito.format(id=f"{reserva.pk:06d}"))
        return redirect('gestion_reserva_detalle', reserva_id=reserva.pk)
    pagos = reserva.pagos.aggregate(t=Sum('monto'))['t']
    return render(request, 'panel/confirmar_accion.html', _ctx(
        request, 'reservas', r=reserva, titulo=titulo, texto=texto, boton=boton, peligro=peligro,
        aviso_pagos=pagos if (peligro and pagos) else None))


@requiere_admin
def reserva_confirmar(request, reserva_id):
    return _accion_reserva(
        request, reserva_id, titulo="Confirmar reserva",
        texto="Se volverá a comprobar la disponibilidad de la habitación antes de confirmar.",
        boton="Confirmar reserva", servicio=services.confirmar_reserva,
        exito="Reserva RW-{id} confirmada.")


@requiere_admin
def reserva_cancelar(request, reserva_id):
    return _accion_reserva(
        request, reserva_id, titulo="Cancelar reserva",
        texto="La reserva quedará como cancelada (no se borra) y la habitación volverá a estar libre para esas fechas.",
        boton="Sí, cancelar reserva", servicio=services.cancelar_reserva,
        exito="Reserva RW-{id} cancelada.", peligro=True)


# ===========================================================================
# HUÉSPEDES
# ===========================================================================
@requiere_admin
def huespedes_lista(request):
    q = request.GET.get('q', '').strip()[:80]
    qs = Huesped.objects.annotate(total_reservas=Count('reservas'), ultima=Max('reservas__fecha_entrada'))
    for palabra in q.split():
        qs = qs.filter(Q(nombres__icontains=palabra) | Q(apellidos__icontains=palabra)
                       | Q(numero_documento__icontains=palabra) | Q(correo__icontains=palabra))
    pagina = Paginator(qs.order_by('apellidos', 'nombres'), POR_PAGINA).get_page(request.GET.get('pagina'))
    return render(request, 'panel/huespedes_lista.html', _ctx(
        request, 'huespedes', pagina=pagina, q=q, qs_sin_pagina=_qs_sin(request, 'pagina')))


@requiere_admin
def huesped_detalle(request, huesped_id):
    huesped = get_object_or_404(Huesped, pk=huesped_id)
    form = HuespedForm(request.POST or None, instance=huesped)
    if request.method == 'POST':
        if form.is_valid():
            form.save()
            messages.success(request, "Datos del huésped actualizados.")
            return redirect('gestion_huesped_detalle', huesped_id=huesped.pk)
        messages.error(request, "Revisa los campos marcados.")
    historial = (huesped.reservas.select_related('habitacion__tipo').order_by('-fecha_entrada', '-id'))
    return render(request, 'panel/huesped_detalle.html', _ctx(
        request, 'huespedes', h=huesped, form=form, historial=historial,
        total_gastado=historial.exclude(estado='CANCELADA').aggregate(t=Sum('valor_total'))['t'] or 0))


# ===========================================================================
# HABITACIONES
# ===========================================================================
SITUACIONES = [('libre', 'Libre'), ('reservada', 'Reservada'), ('ocupada', 'Ocupada'),
               ('aseo', 'En aseo'), ('mantenimiento', 'Mantenimiento'), ('fuera', 'Fuera de servicio')]


@requiere_admin
def habitaciones_lista(request):
    hoy = timezone.localdate()
    entrada = ps.fecha_o_none(request.GET.get('entrada')) or hoy
    salida = ps.fecha_o_none(request.GET.get('salida')) or (entrada + timedelta(days=1))
    if salida <= entrada:
        messages.warning(request, "La salida debe ser posterior a la entrada; se consultó una sola noche.")
        salida = entrada + timedelta(days=1)
    q = request.GET.get('q', '').strip()[:20]
    qs = Habitacion.objects.select_related('tipo')
    if q:
        qs = qs.filter(numero__icontains=q)
    tipo = request.GET.get('tipo', '')
    if tipo.isdigit():
        qs = qs.filter(tipo_id=int(tipo))
    habitaciones = ps.estado_para_periodo(qs.order_by('piso', 'numero'), entrada, salida, hoy)
    conteo = {k: sum(1 for h in habitaciones if h.estado_periodo == k) for k, _ in SITUACIONES}
    situacion = request.GET.get('situacion', '')
    if situacion in conteo:
        habitaciones = [h for h in habitaciones if h.estado_periodo == situacion]
    pagina = Paginator(habitaciones, 40).get_page(request.GET.get('pagina'))
    return render(request, 'panel/habitaciones_lista.html', _ctx(
        request, 'habitaciones', pagina=pagina, q=q, tipo=tipo, situacion=situacion, situaciones=SITUACIONES,
        conteo=conteo, entrada=entrada, salida=salida, tipos=TipoHabitacion.objects.order_by('nombre'),
        qs_sin_pagina=_qs_sin(request, 'pagina'),
        tipos_resumen=TipoHabitacion.objects.annotate(n=Count('habitaciones')).order_by('nombre')))


@requiere_admin
def habitacion_detalle(request, habitacion_id):
    hab = get_object_or_404(Habitacion.objects.select_related('tipo'), pk=habitacion_id)
    hoy = timezone.localdate()
    ps.estado_para_periodo([hab], hoy, hoy + timedelta(days=1), hoy)
    reservas = hab.reservas.select_related('huesped').order_by('-fecha_entrada', '-id')
    proximas = reservas.filter(estado__in=('PENDIENTE', 'CONFIRMADA', 'CHECKIN'), fecha_salida__gte=hoy)
    return render(request, 'panel/habitacion_detalle.html', _ctx(
        request, 'habitaciones', hab=hab, proximas=proximas.order_by('fecha_entrada'),
        historial=reservas[:15], total_historial=reservas.count()))


@requiere_admin
def habitacion_form(request, habitacion_id=None):
    hab = get_object_or_404(Habitacion, pk=habitacion_id) if habitacion_id else None
    form = HabitacionForm(request.POST or None, instance=hab)
    if request.method == 'POST':
        if form.is_valid():
            nueva = form.save(commit=False)
            if hab is None:
                hotel = Hotel.objects.first()
                if hotel is None:
                    form.add_error(None, "No hay un hotel configurado en la base de datos.")
                    nueva = None
                else:
                    nueva.hotel = hotel
            if nueva is not None:
                nueva.save()
                messages.success(request, f"Habitación {nueva.numero} {'creada' if hab is None else 'actualizada'}.")
                return redirect('gestion_habitacion_detalle', habitacion_id=nueva.pk)
        else:
            messages.error(request, "Revisa los campos marcados.")
    return render(request, 'panel/habitacion_form.html', _ctx(request, 'habitaciones', form=form, hab=hab))


@requiere_admin
def tipo_form(request, tipo_id):
    tipo = get_object_or_404(TipoHabitacion, pk=tipo_id)
    form = TipoHabitacionForm(request.POST or None, instance=tipo)
    if request.method == 'POST':
        if form.is_valid():
            form.save()
            messages.success(request, f"Tipo «{tipo.nombre}» actualizado.")
            return redirect('gestion_habitaciones')
        messages.error(request, "Revisa los campos marcados.")
    return render(request, 'panel/tipo_form.html', _ctx(request, 'habitaciones', form=form, tipo=tipo))


# ===========================================================================
# LLEGADAS Y SALIDAS
# ===========================================================================
@requiere_admin
def llegadas_salidas(request):
    hoy = timezone.localdate()
    texto = request.GET.get('fecha', '')
    fecha = ps.fecha_o_none(texto) or hoy
    if texto and ps.fecha_o_none(texto) is None:
        messages.warning(request, "La fecha no es válida; se muestra el día de hoy.")
    base = Reserva.objects.select_related('huesped', 'habitacion__tipo')
    llegadas = base.filter(fecha_entrada=fecha).exclude(estado='CANCELADA').order_by('habitacion__numero')
    salidas = base.filter(fecha_salida=fecha, estado__in=('CHECKIN', 'CHECKOUT')).order_by('habitacion__numero')
    return render(request, 'panel/llegadas_salidas.html', _ctx(
        request, 'llegadas', fecha=fecha, llegadas=llegadas, salidas=salidas,
        anterior=fecha - timedelta(days=1), siguiente=fecha + timedelta(days=1), es_hoy=fecha == hoy))


# ===========================================================================
# SERVICIOS Y LIMPIEZA
# ===========================================================================
@requiere_admin
def servicios_limpieza(request):
    form = ConsumoForm(request.POST or None) if request.method == 'POST' else ConsumoForm()
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        try:
            consumo = services.cargar_servicio(d['reserva'].pk, d['servicio'].pk, d['cantidad'])
        except ReglaDeNegocioError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"Consumo cargado a la habitación {d['reserva'].habitacion.numero}: "
                                      f"{consumo.cantidad} × {d['servicio'].nombre}.")
            return redirect('gestion_servicios')
    pendientes = (Habitacion.objects.filter(activa=True, estado__in=(Habitacion.EN_ASEO, Habitacion.MANTENIMIENTO))
                  .select_related('tipo').order_by('estado', 'piso', 'numero'))
    return render(request, 'panel/servicios_limpieza.html', _ctx(
        request, 'servicios', pendientes=pendientes, form=form,
        catalogo=Servicio.objects.order_by('categoria', 'nombre'),
        consumos=(ServicioReserva.objects
                  .filter(reserva__estado='CHECKIN').select_related('servicio', 'reserva__habitacion', 'reserva__huesped')
                  .order_by('-id')[:15])))


@requiere_admin
@require_POST
def habilitar(request, habitacion_id):
    try:
        hab = services.habilitar_habitacion(habitacion_id)
    except ReglaDeNegocioError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"Habitación {hab.numero} habilitada: ahora está disponible.")
    return redirect('gestion_servicios')


# ===========================================================================
# FINANZAS E INFORMES
# ===========================================================================
@requiere_admin
def finanzas(request):
    hoy = timezone.localdate()
    desde, hasta, avisos = ps.leer_periodo(request.GET, hoy)
    _avisar(request, avisos)
    return render(request, 'panel/finanzas.html', _ctx(
        request, 'finanzas', desde=desde, hasta=hasta, m=ps.metricas_periodo(desde, hasta),
        mensual=ps.resumen_mensual(desde, hasta), tipos=ps.distribucion_por_tipo(desde, hasta),
        ocupacion=ps.ocupacion(desde, hasta),
        pagos_total=Pago.objects.filter(reserva__fecha_entrada__gte=desde, reserva__fecha_entrada__lte=hasta)
        .exclude(reserva__estado='CANCELADA').count(),
        qs_export=urlencode({'desde': desde.isoformat(), 'hasta': hasta.isoformat()})))


@requiere_admin
def finanzas_exportar(request):
    hoy = timezone.localdate()
    desde, hasta, _ = ps.leer_periodo(request.GET, hoy)
    respuesta = HttpResponse(content_type='text/csv; charset=utf-8')
    respuesta['Content-Disposition'] = f'attachment; filename="reservas_{desde}_{hasta}.csv"'
    respuesta.write('\ufeff')
    w = csv.writer(respuesta, delimiter=';')
    w.writerow(['Codigo', 'Huesped', 'Documento', 'Tipo', 'Habitacion', 'Entrada', 'Salida', 'Noches',
                'Huespedes', 'Estado', 'Canal', 'Valor total (COP)', 'Pagos registrados (COP)'])
    qs = (Reserva.objects.filter(fecha_entrada__gte=desde, fecha_entrada__lte=hasta)
          .select_related('huesped', 'habitacion__tipo').annotate(pagado=Sum('pagos__monto')).order_by('fecha_entrada', 'id'))
    for r in qs:
        fila = [f"RW-{r.pk:06d}", f"{r.huesped.nombres} {r.huesped.apellidos}", r.huesped.numero_documento,
                r.habitacion.tipo.nombre, r.habitacion.numero, r.fecha_entrada.isoformat(),
                r.fecha_salida.isoformat(), r.calcular_noches(), r.num_huespedes, r.get_estado_display(),
                r.get_canal_reserva_display(), r.valor_total, r.pagado or 0]
        w.writerow([ps.sanear_celda_csv(c) for c in fila])
    return respuesta


# ===========================================================================
# USUARIOS Y ACCESOS
# ===========================================================================
@requiere_admin
def usuarios(request):
    q = request.GET.get('q', '').strip()[:80]
    qs = Usuario.objects.all()
    for palabra in q.split():
        qs = qs.filter(Q(nombres__icontains=palabra) | Q(apellidos__icontains=palabra) | Q(correo__icontains=palabra))
    rol = request.GET.get('rol', '')
    if rol in dict(Usuario.ROLES):
        qs = qs.filter(rol=rol)
    # Nunca se envía la contraseña a la plantilla: solo estos campos.
    qs = qs.only('id', 'nombres', 'apellidos', 'correo', 'rol', 'activo').order_by('rol', 'apellidos', 'nombres')
    pagina = Paginator(qs, POR_PAGINA).get_page(request.GET.get('pagina'))
    return render(request, 'panel/usuarios.html', _ctx(
        request, 'usuarios', pagina=pagina, q=q, rol=rol, roles=Usuario.ROLES,
        qs_sin_pagina=_qs_sin(request, 'pagina')))


@requiere_admin
@require_POST
def usuario_alternar(request, usuario_id):
    objetivo = get_object_or_404(Usuario, pk=usuario_id)
    if objetivo.pk == request.usuario.pk:
        messages.error(request, "No puedes desactivar tu propia cuenta.")
    elif objetivo.activo and objetivo.rol == ROL_ADMIN and \
            not Usuario.objects.filter(rol=ROL_ADMIN, activo=True).exclude(pk=objetivo.pk).exists():
        messages.error(request, "Debe quedar al menos un administrador activo.")
    else:
        objetivo.activo = not objetivo.activo
        objetivo.save(update_fields=['activo'])
        messages.success(request, f"Cuenta de {objetivo.nombres} {objetivo.apellidos} "
                                  f"{'activada' if objetivo.activo else 'desactivada'}.")
    return redirect('gestion_usuarios')
