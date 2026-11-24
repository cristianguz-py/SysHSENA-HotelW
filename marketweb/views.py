from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth.hashers import check_password, identify_hasher, make_password
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.http import require_GET, require_POST

from . import services
from .busqueda import CLAVE_SESION, BusquedaDisponibilidadForm
from .forms import CambiarContrasenaForm, RegistroHuespedForm, ReservaClienteForm, ReservaForm
from .models import Factura, Habitacion, Hotel, Huesped, Reserva, TipoHabitacion, Usuario
from .services import ReglaDeNegocioError


# Roles del personal del hotel. CLIENTE (huésped registrado) NO está aquí a propósito.
ROLES_PERSONAL = ('ADMIN_GERENTE', 'RECEPCIONISTA')


def requiere_sesion(vista):
    """Cualquier sesión iniciada (huésped o personal): sesión con usuario_id."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if 'usuario_id' not in request.session:
            return redirect('login')
        return vista(request, *args, **kwargs)
    return envoltura


def requiere_cliente(vista):
    """Solo huéspedes (rol CLIENTE) con cuenta activa. El personal usa su propio panel.
    La cuenta se vuelve a leer de la BD: una cuenta desactivada pierde el acceso."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if 'usuario_id' not in request.session:
            return redirect('login')
        if request.session.get('usuario_rol') != 'CLIENTE':
            messages.info(request, "Las reservas web son para cuentas de huésped. Usa el panel del hotel.")
            return redirect('inicio')
        usuario = Usuario.objects.filter(pk=request.session['usuario_id'], activo=True, rol='CLIENTE').first()
        if usuario is None:
            request.session.flush()
            return redirect('login')
        request.usuario = usuario
        return vista(request, *args, **kwargs)
    return envoltura


def requiere_personal(vista):
    """Solo personal del hotel. Un huésped con sesión recibe 403 (no el panel).
    El rol sale de la sesión del servidor, que se llena únicamente al iniciar sesión."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if 'usuario_id' not in request.session:
            return redirect('login')
        if request.session.get('usuario_rol') not in ROLES_PERSONAL:
            raise PermissionDenied
        return vista(request, *args, **kwargs)
    return envoltura


# ----------------------------------------------------
# BÚSQUEDA DE DISPONIBILIDAD (página de inicio pública) — ayudas
# ----------------------------------------------------
def _leer_busqueda(request):
    """Búsqueda guardada en la sesión, revalidada contra la BD y la fecha de hoy.
    Devuelve {'llegada', 'salida', 'noches', 'tipo'} o None (y limpia la sesión si
    los datos ya no sirven: no se confía en lo que quedó guardado)."""
    guardada = request.session.get(CLAVE_SESION)
    if not isinstance(guardada, dict):
        return None
    try:
        llegada = datetime.strptime(guardada.get('llegada', ''), '%Y-%m-%d').date()
        salida = datetime.strptime(guardada.get('salida', ''), '%Y-%m-%d').date()
        tipo = TipoHabitacion.objects.filter(pk=int(guardada.get('tipo_id'))).first()
    except (ValueError, TypeError):
        tipo = None
    else:
        if tipo is None or llegada < timezone.localdate() or salida <= llegada:
            tipo = None
    if tipo is None:
        request.session.pop(CLAVE_SESION, None)
        return None
    return {'llegada': llegada, 'salida': salida, 'noches': (salida - llegada).days, 'tipo': tipo}


def _destino_tras_login(request):
    """Nombre de ruta (lista fija; nunca una URL recibida del navegador) a la que va
    una sesión iniciada: etapa provisional si hay búsqueda pendiente o si es un
    huésped (CLIENTE); si no, el panel de recepción de siempre."""
    if CLAVE_SESION in request.session or request.session.get('usuario_rol') == 'CLIENTE':
        return 'busqueda_preparada'
    return 'inicio'


# ----------------------------------------------------
# 1. MÓDULO LOG IN (Iniciar Sesión)
# ----------------------------------------------------
# Hash de relleno: cuando el usuario no existe se hace igualmente una verificación
# de contraseña para que la respuesta tarde parecido y no delate si la cuenta existe.
_HASH_RELLENO = make_password('relleno-hotel-w')


def _buscar_usuario_login(identificador):
    """Correo (único) para cualquier cuenta. El NOMBRE solo sirve para cuentas del
    personal y solo si identifica a una única cuenta: los nombres no son únicos y,
    con registro abierto, un huésped podría escoger el mismo nombre que un
    administrador y estorbar su acceso."""
    if not identificador:
        return None
    activos = Usuario.objects.filter(activo=True)
    usuario = activos.filter(correo__iexact=identificador).first()
    if usuario:
        return usuario
    por_nombre = list(activos.filter(nombres__iexact=identificador, rol__in=ROLES_PERSONAL)[:2])
    return por_nombre[0] if len(por_nombre) == 1 else None


def _clave_correcta(usuario, clave):
    """Verifica con el sistema de hashes de Django. Una contraseña guardada que NO
    tiene formato de hash (cuenta antigua en texto plano) se compara en tiempo
    constante y, si coincide, se convierte a hash en ese mismo momento."""
    guardada = usuario.contrasena or ''
    try:
        identify_hasher(guardada)
    except ValueError:
        if guardada and constant_time_compare(guardada, clave):
            usuario.contrasena = make_password(clave)
            usuario.save(update_fields=['contrasena'])
            return True
        return False

    def actualizar_hash(clave_en_claro):  # Django lo llama si el algoritmo quedó viejo
        usuario.contrasena = make_password(clave_en_claro)
        usuario.save(update_fields=['contrasena'])

    return check_password(clave, guardada, setter=actualizar_hash)


def _abrir_sesion(request, usuario):
    """Inicia la sesión del usuario. cycle_key() cambia el identificador de sesión
    (evita fijación de sesión) y CONSERVA los datos, como la búsqueda pendiente."""
    request.session.cycle_key()
    request.session['usuario_id'] = usuario.id
    request.session['usuario_nombre'] = f"{usuario.nombres} {usuario.apellidos}"
    request.session['usuario_rol'] = usuario.rol


def iniciar_sesion(request):
    # Si ya tiene una sesión activa, lo envía a su destino (panel o búsqueda preparada)
    if 'usuario_id' in request.session:
        return redirect(_destino_tras_login(request))

    identificador = ''
    if request.method == 'POST':
        identificador = request.POST.get('identificador', '').strip()[:254]
        clave = request.POST.get('contrasena', '')  # sin strip: los espacios cuentan
        usuario = _buscar_usuario_login(identificador)

        if usuario is not None and len(clave) <= 256 and _clave_correcta(usuario, clave):
            _abrir_sesion(request, usuario)
            messages.success(request, f"¡Bienvenido(a), {usuario.nombres}!")
            # El destino sale de una lista fija de rutas: no se lee ningún "next" del navegador.
            return redirect(_destino_tras_login(request))

        if usuario is None:
            check_password(clave[:256], _HASH_RELLENO)
        # Mismo mensaje para "no existe", "contraseña mala" y "cuenta inactiva".
        messages.error(request, "Credenciales incorrectas o cuenta inactiva.")

    return render(request, 'login.html', {
        'busqueda': _leer_busqueda(request),
        'identificador': identificador,
    })


# ----------------------------------------------------
# REGISTRO DE HUÉSPEDES (crea Usuario con rol CLIENTE, inicia sesión y sigue
# a la etapa protegida; la búsqueda pendiente se conserva en la sesión)
# ----------------------------------------------------
def registro(request):
    if 'usuario_id' in request.session:
        return redirect(_destino_tras_login(request))

    if request.method == 'POST':
        form = RegistroHuespedForm(request.POST)
        if form.is_valid():
            usuario = form.guardar()
            if usuario is not None:
                _abrir_sesion(request, usuario)
                return redirect(_destino_tras_login(request))
    else:
        form = RegistroHuespedForm()

    return render(request, 'registro.html', {'form': form, 'busqueda': _leer_busqueda(request)})


# ----------------------------------------------------
# 2. MÓDULO LOG OUT (Cerrar Sesión)
# ----------------------------------------------------
@require_POST
def cerrar_sesion(request):
    # Solo POST (con CSRF): un enlace externo no puede cerrar la sesión de nadie.
    # Elimina todas las variables guardadas en la sesión del servidor
    request.session.flush()
    messages.info(request, "Has cerrado sesión correctamente.")
    return redirect('login')


def home_publico(request):
    hotel = Hotel.objects.first()  # datos reales si existen (el template ignora los "NN")
    return render(request, 'home.html', {'hotel': hotel, 'form_busqueda': BusquedaDisponibilidadForm()})


# ----------------------------------------------------
# FORMULARIO DE DISPONIBILIDAD (inicio público) → login → etapa provisional
# No crea reservas, no bloquea habitaciones, no cobra.
# ----------------------------------------------------
def buscar_disponibilidad(request):
    if request.method != 'POST':
        return redirect(f"{reverse('home')}#reservar")

    form = BusquedaDisponibilidadForm(request.POST)
    if not form.is_valid():
        return render(request, 'home.html', {
            'hotel': Hotel.objects.first(),
            'form_busqueda': form,
        })

    request.session[CLAVE_SESION] = form.para_sesion()
    if 'usuario_id' in request.session:
        return redirect('busqueda_preparada')
    return redirect('login')


@requiere_sesion
def busqueda_preparada(request):
    """Resultado de la búsqueda: consulta la disponibilidad REAL (misma función que usa
    el guardado) con las fechas y el tipo guardados en la sesión. No reserva ni bloquea
    nada: la disponibilidad se vuelve a comprobar al confirmar."""
    busqueda = _leer_busqueda(request)
    contexto = {
        'busqueda': busqueda,
        'usuario_nombre': request.session.get('usuario_nombre'),
        'es_cliente': request.session.get('usuario_rol') == 'CLIENTE',
    }
    if busqueda:
        tipo = busqueda['tipo']
        hay = services.habitaciones_disponibles(
            busqueda['llegada'], busqueda['salida'], tipo_id=tipo.pk
        ).exists()
        contexto.update({
            'hay_disponibilidad': hay,
            'precio_noche': tipo.precio_noche,
            'total_alojamiento': Reserva.alojamiento_para(tipo, busqueda['llegada'], busqueda['salida']),
        })
    return render(request, 'busqueda_preparada.html', contexto)


# ----------------------------------------------------
# RESERVA DEL CLIENTE: formulario → resumen → confirmación (guarda en la BD)
# ----------------------------------------------------
def _huella_busqueda(busqueda):
    return f"{busqueda['llegada']:%Y-%m-%d}|{busqueda['salida']:%Y-%m-%d}|{busqueda['tipo'].pk}"


@requiere_cliente
def reservar(request):
    busqueda = _leer_busqueda(request)
    if busqueda is None:
        messages.info(request, "Elige tus fechas y el tipo de habitación para comenzar una reserva.")
        return redirect(f"{reverse('home')}#reservar")

    usuario, tipo = request.usuario, busqueda['tipo']
    hoy = timezone.localdate()
    errores_negocio, resumen = [], None

    if request.method == 'POST':
        form = ReservaClienteForm(request.POST, tipo=tipo)
        # Lo que el cliente vio debe ser lo que se confirma: si la búsqueda de la sesión
        # cambió entre el resumen y la confirmación (otra pestaña), se vuelve a empezar.
        if request.POST.get('paso') == 'confirmar' and request.POST.get('huella') != _huella_busqueda(busqueda):
            messages.warning(request, "Tu búsqueda cambió mientras reservabas. Revisa los datos antes de confirmar.")
            return redirect('busqueda_preparada')
        if form.is_valid():
            cd = form.cleaned_data
            disponibles = services.habitaciones_disponibles(
                busqueda['llegada'], busqueda['salida'], cd['num_huespedes'], hoy=hoy, tipo_id=tipo.pk)
            if not disponibles.exists():
                errores_negocio = ["Ya no hay habitaciones disponibles de este tipo para esas fechas."]
            elif request.POST.get('paso') == 'confirmar':
                try:
                    reserva = services.crear_reserva_cliente(
                        usuario=usuario, tipo_id=tipo.pk,
                        entrada=busqueda['llegada'], salida=busqueda['salida'],
                        num_huespedes=cd['num_huespedes'], datos_huesped=form.datos_huesped(usuario), hoy=hoy,
                    )
                except ReglaDeNegocioError as e:
                    errores_negocio = e.mensajes
                else:
                    request.session.pop(CLAVE_SESION, None)
                    return redirect(f"{reverse('reserva_cliente_detalle', args=[reserva.pk])}?nueva=1")
            else:
                resumen = {
                    'noches': busqueda['noches'],
                    'precio_noche': tipo.precio_noche,
                    'total': Reserva.alojamiento_para(tipo, busqueda['llegada'], busqueda['salida']),
                }
    else:
        form = ReservaClienteForm(tipo=tipo, initial={
            'tipo_documento': 'CC', 'nacionalidad': 'Colombiana', 'num_huespedes': 1,
        })

    return render(request, 'reservar.html', {
        'form': form, 'busqueda': busqueda, 'tipo': tipo, 'resumen': resumen,
        'errores_negocio': errores_negocio, 'huella': _huella_busqueda(busqueda),
        'usuario': usuario,
    })


@requiere_cliente
def reserva_cliente_detalle(request, reserva_id):
    # El filtro por usuario hace que la reserva de otra persona responda 404, no 403.
    reserva = get_object_or_404(
        Reserva.objects.select_related('habitacion__tipo'), pk=reserva_id, usuario=request.usuario)
    return render(request, 'reserva_cliente_detalle.html', {
        'reserva': reserva, 'nueva': request.GET.get('nueva') == '1',
        'noches': reserva.calcular_noches(),
    })


@requiere_cliente
def mis_reservas(request):
    reservas = (Reserva.objects.filter(usuario=request.usuario)
                .select_related('habitacion__tipo').order_by('-fecha_creacion', '-id'))
    pagina = Paginator(reservas, 10).get_page(request.GET.get('page'))
    return render(request, 'mis_reservas.html', {'pagina': pagina})


# ----------------------------------------------------
# CONFIGURACIÓN DE LA CUENTA (cualquier sesión iniciada) + cambio de contraseña
# ----------------------------------------------------
@requiere_sesion
def cuenta_configuracion(request):
    usuario = Usuario.objects.filter(pk=request.session['usuario_id'], activo=True).first()
    if usuario is None:
        request.session.flush()
        return redirect('login')

    if request.method == 'POST':
        form = CambiarContrasenaForm(usuario, request.POST)
        if form.is_valid():
            form.guardar()
            request.session.cycle_key()  # nuevo identificador de sesión; la sesión continúa
            messages.success(request, "Tu contraseña se actualizó correctamente.")
            return redirect('cuenta')
        messages.error(request, "No se cambió la contraseña. Revisa los campos marcados.")
    else:
        form = CambiarContrasenaForm(usuario)

    return render(request, 'cuenta.html', {'form': form, 'usuario': usuario})


# ----------------------------------------------------
# CREAR RESERVA
# ----------------------------------------------------
def _fecha_iso(valor, defecto):
    """Devuelve 'YYYY-MM-DD' si el texto es una fecha válida; si no, el valor por defecto."""
    try:
        return datetime.strptime(valor or '', '%Y-%m-%d').date().isoformat()
    except ValueError:
        return defecto.isoformat()


@requiere_personal
def crear_reserva(request, habitacion_id=None):
    """Módulo de creación de reservas. Con habitacion_id (desde Disponibilidad) llega
    la habitación preseleccionada; sin él, el usuario elige fechas y tipo y la lista
    de habitaciones libres se pide por AJAX. En ambos casos la autoridad es
    services.crear_reserva (backend)."""
    hoy = timezone.localdate()
    habitacion_inicial = None
    if habitacion_id is not None:
        habitacion_inicial = get_object_or_404(Habitacion.objects.select_related('tipo'), pk=habitacion_id)

    errores_negocio = []
    if request.method == 'POST':
        form = ReservaForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                reserva = services.crear_reserva(
                    habitacion_id=cd['habitacion'], tipo_id=cd['tipo_habitacion'].pk,
                    datos_huesped=form.datos_huesped(),
                    entrada=cd['fecha_entrada'], salida=cd['fecha_salida'],
                    num_huespedes=cd['num_huespedes'], canal=cd['canal_reserva'],
                )
            except ReglaDeNegocioError as e:
                errores_negocio = e.mensajes
            else:
                return redirect(f"{reverse('reserva_detalle', args=[reserva.pk])}?nueva=1")
    else:
        form = ReservaForm(initial={
            'tipo_documento': 'CC',
            'nacionalidad': 'Colombiana',
            'canal_reserva': 'PRESENCIAL',
            'fecha_entrada': _fecha_iso(request.GET.get('fecha_entrada'), hoy),
            'fecha_salida': _fecha_iso(request.GET.get('fecha_salida'), hoy + timedelta(days=1)),
            'num_huespedes': request.GET.get('num_huespedes') if (request.GET.get('num_huespedes') or '').isdigit() else 1,
            'tipo_habitacion': habitacion_inicial.tipo_id if habitacion_inicial else None,
            'habitacion': habitacion_inicial.pk if habitacion_inicial else None,
        })

    return render(request, 'crear_reserva.html', {
        'form': form,
        'errores_negocio': errores_negocio,
        'tipos': TipoHabitacion.objects.order_by('precio_noche', 'id'),
        'canales': Reserva.CANALES,
        'tipos_documento': Huesped.TIPOS_DOC,
        'habitacion_inicial': habitacion_inicial,
        'hoy': hoy.isoformat(),
    })


# ----------------------------------------------------
# DETALLE / CONFIRMACIÓN DE RESERVA
# ----------------------------------------------------
@requiere_personal
def reserva_detalle(request, reserva_id):
    reserva = get_object_or_404(
        Reserva.objects.select_related('huesped', 'habitacion__tipo'), pk=reserva_id
    )
    hoy = timezone.localdate()
    return render(request, 'reserva_detalle.html', {
        'reserva': reserva,
        'nueva': request.GET.get('nueva') == '1',
        'noches': reserva.calcular_noches(),
        'precio_noche': reserva.habitacion.tipo.precio_noche,
        'alojamiento': reserva.calcular_alojamiento(),
        'checkin_habilitado': reserva.estado == 'CONFIRMADA' and reserva.fecha_entrada <= hoy,
    })


# ----------------------------------------------------
# CHECK-IN
# ----------------------------------------------------
@requiere_personal
def registrar_checkin(request, reserva_id):
    reserva = get_object_or_404(
        Reserva.objects.select_related('huesped', 'habitacion__tipo'), pk=reserva_id
    )

    if request.method == 'POST':
        try:
            reserva = services.registrar_checkin(reserva_id)
        except ReglaDeNegocioError as e:
            messages.error(request, str(e))
            return redirect('checkin', reserva_id=reserva_id)
        messages.success(request, f"Check-in realizado para la Habitación {reserva.habitacion.numero}.")
        return redirect('inicio')

    return render(request, ['marketweb/checkin.html', 'checkin.html'], {'reserva': reserva})


# ----------------------------------------------------
# CHECK-OUT  (el GET ya NO escribe en la base de datos)
# ----------------------------------------------------
@requiere_personal
def registrar_checkout(request, reserva_id):
    reserva = get_object_or_404(
        Reserva.objects.select_related('huesped', 'habitacion__tipo'), pk=reserva_id
    )

    es_recepcion = request.session.get('usuario_rol') == 'RECEPCIONISTA'
    if es_recepcion and request.method != 'POST':
        # El recepcionista usa el flujo con pago final y control de saldo del panel de recepción.
        return redirect('recepcion:checkout', reserva_id=reserva_id)

    if request.method == 'POST':
        try:
            if es_recepcion:
                # Ruta antigua: para RECEPCIONISTA solo cierra con el flujo seguro (check-in previo,
                # saldo exactamente en cero y registro en el historial). El administrador conserva
                # el comportamiento anterior.
                from .recepcion import operaciones as op_recepcion
                usuario = Usuario.objects.filter(pk=request.session['usuario_id']).first()
                reserva = op_recepcion.hacer_checkout(usuario=usuario, reserva_id=reserva_id)
            else:
                reserva = services.registrar_checkout(reserva_id)
        except ReglaDeNegocioError as e:
            messages.error(request, str(e))
            return redirect('recepcion:checkout' if es_recepcion else 'checkout', reserva_id=reserva_id)
        messages.success(
            request,
            f"Check-out completado. Habitación {reserva.habitacion.numero} pasó a estado EN ASEO."
        )
        return redirect('recepcion:reserva_detalle', reserva_id=reserva_id) if es_recepcion else redirect('inicio')

    if reserva.estado != 'CHECKIN':
        messages.warning(request, "Esta reserva no tiene un check-in activo; no se puede hacer check-out.")

    cuenta = services.calcular_cuenta(reserva)
    return render(request, ['marketweb/checkout.html', 'checkout.html'], {
        'reserva': reserva,
        'total_alojamiento': cuenta['total_alojamiento'],
        'total_servicios': cuenta['total_servicios'],
        'cargo_tardio': cuenta['cargo_tardio'],
        'total_general': cuenta['total_general'],
        'total_pagos': cuenta['total_pagos'],
        'saldo_pendiente': cuenta['saldo_pendiente'],
    })

# ----------------------------------------------------
# FACTURA (ahora solo personal: antes se podía abrir sin sesión por número de reserva)
# ----------------------------------------------------
@requiere_personal
def generar_factura_dian(request, reserva_id):
    reserva = get_object_or_404(Reserva, pk=reserva_id)
    huesped = reserva.huesped

    # Lógica de IVA
    exento = huesped.es_extranjero
    subtotal = reserva.valor_total / Decimal('1.19') if not exento else reserva.valor_total
    iva = reserva.valor_total - subtotal if not exento else Decimal('0.00')

    num_dian_generado = f"SETT-{reserva.id:06d}"

    factura, created = Factura.objects.get_or_create(
        reserva=reserva,
        defaults={
            'numero_dian': num_dian_generado,
            'subtotal': subtotal,
            'iva': iva,
            'total': reserva.valor_total,
            'exento_iva': exento
        }
    )

    return render(request, 'marketweb/factura.html', {'factura': factura, 'reserva': reserva})


# ----------------------------------------------------
# MÓDULO HUÉSPEDES (consulta + búsqueda; solo lee el modelo Huesped)
# ----------------------------------------------------
@requiere_personal
def huespedes_lista(request):
    q = request.GET.get('q', '').strip()

    # Última reserva de cada huésped (para el enlace al detalle ya existente)
    ultima = Reserva.objects.filter(huesped=OuterRef('pk')).order_by('-fecha_entrada', '-id')
    huespedes = Huesped.objects.annotate(
        total_reservas=Count('reservas'),
        ultima_reserva_id=Subquery(ultima.values('id')[:1]),
        ultima_entrada=Subquery(ultima.values('fecha_entrada')[:1]),
    ).order_by('apellidos', 'nombres')

    # Buscador real: cada palabra debe coincidir con nombre, apellido o documento
    for termino in q.split():
        huespedes = huespedes.filter(
            Q(nombres__icontains=termino)
            | Q(apellidos__icontains=termino)
            | Q(numero_documento__icontains=termino)
        )

    pagina = Paginator(huespedes, 20).get_page(request.GET.get('page'))

    return render(request, 'huespedes_lista.html', {
        'pagina': pagina,
        'q': q,
        'total_huespedes': Huesped.objects.count(),
        'total_extranjeros': Huesped.objects.filter(es_extranjero=True).count(),
        'usuario_nombre': request.session.get('usuario_nombre'),
        'usuario_rol': request.session.get('usuario_rol'),
    })