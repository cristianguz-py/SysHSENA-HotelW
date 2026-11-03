"""Vistas de W Bar Online: menú y pedidos del huésped (rol CLIENTE) y operación/administración
del personal. Solo Django + HTML (formularios POST con CSRF); sin JavaScript."""
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from marketweb.services import ReglaDeNegocioError
from marketweb.views import requiere_cliente

from . import carrito as carro
from . import services
from .forms import CancelarForm, CategoriaForm, ConfirmarPedidoForm, EntregaForm, ProductoForm
from .models import CategoriaProducto, DetallePedido, Pedido, Producto
from .permisos import ADMIN, TEXTO_ROL, requiere_administrador, requiere_operacion

AVISO_ALCOHOL = "El exceso de alcohol es perjudicial para la salud."


def _entero(valor, defecto=0):
    try:
        return int(str(valor).strip())
    except (TypeError, ValueError):
        return defecto


# ===========================================================================
# HUÉSPED
# ===========================================================================
def menu(request):
    """Menú del bar. Se puede consultar sin sesión; pedir exige cuenta de huésped."""
    categorias = list(CategoriaProducto.objects.filter(activa=True).order_by('orden', 'nombre'))
    productos = (Producto.objects.filter(activo=True, categoria__activa=True)
                 .select_related('categoria').order_by('nombre'))
    por_categoria = {}
    for p in productos:
        por_categoria.setdefault(p.categoria_id, []).append(p)
    secciones = [(c, por_categoria[c.pk]) for c in categorias if c.pk in por_categoria]
    es_cliente = request.session.get('usuario_rol') == 'CLIENTE'
    return render(request, 'Wbar/menu.html', {
        'secciones': secciones, 'es_cliente': es_cliente, 'sesion': 'usuario_id' in request.session,
        'items_carrito': sum(carro.leer(request.session).values()), 'aviso_alcohol': AVISO_ALCOHOL,
    })


@requiere_cliente
@require_POST
def carrito_agregar(request):
    ok, mensaje = carro.agregar(request.session, _entero(request.POST.get('producto')),
                                _entero(request.POST.get('cantidad'), 0))
    (messages.success if ok else messages.error)(request, mensaje)
    return redirect('wbar:menu')


@requiere_cliente
def carrito(request):
    lineas, total, avisos = carro.detallar(request.session)
    for a in avisos:
        messages.warning(request, a)
    return render(request, 'Wbar/carrito.html', {
        'lineas': lineas, 'total': total, 'hay_alcohol': any(l['producto'].contiene_alcohol for l in lineas),
        'aviso_alcohol': AVISO_ALCOHOL, 'items_carrito': sum(l['cantidad'] for l in lineas)})


@requiere_cliente
@require_POST
def carrito_actualizar(request):
    ok, mensaje = carro.fijar(request.session, _entero(request.POST.get('producto')),
                              _entero(request.POST.get('cantidad'), -1) if request.POST.get('accion') != 'quitar' else 0)
    (messages.success if ok else messages.error)(request, mensaje)
    return redirect('wbar:carrito')


@requiere_cliente
@require_POST
def carrito_vaciar(request):
    carro.vaciar(request.session)
    messages.info(request, "Tu pedido quedó vacío.")
    return redirect('wbar:menu')


@requiere_cliente
def confirmar(request):
    lineas, total, avisos = carro.detallar(request.session)
    for a in avisos:
        messages.warning(request, a)
    if request.method == 'POST':
        form = ConfirmarPedidoForm(request.POST, usuario=request.usuario)
        if form.is_valid():
            try:
                # La reserva sale del queryset del propio usuario (ver el formulario) y se
                # vuelve a validar con bloqueo dentro de crear_pedido.
                pedido, creado = services.crear_pedido(
                    usuario=request.usuario, reserva_id=form.cleaned_data['reserva'].pk,
                    lineas_carrito=carro.leer(request.session),
                    declaracion_mayoria_edad=form.cleaned_data['declaracion_mayoria_edad'],
                    notas=form.cleaned_data['notas'], token=form.cleaned_data['token'])
            except ReglaDeNegocioError as e:
                for m in e.mensajes:
                    messages.error(request, m)
            else:
                carro.vaciar(request.session)
                if creado:
                    messages.success(request, f"Pedido {pedido.numero} recibido. Te lo llevamos a tu habitación.")
                return redirect('wbar:pedido_detalle', pedido_id=pedido.pk)
        else:
            if 'token' in form.errors:
                messages.error(request, "El formulario venció. Vuelve a confirmar tu pedido.")
    else:
        if not lineas:
            messages.info(request, "Agrega productos del menú antes de confirmar.")
            return redirect('wbar:menu')
        form = ConfirmarPedidoForm(usuario=request.usuario, initial={'token': services.nuevo_token()})
    habilitadas = services.reservas_habilitadas(request.usuario)
    return render(request, 'Wbar/confirmar.html', {
        'form': form, 'lineas': lineas, 'total': total, 'hay_alcohol': any(l['producto'].contiene_alcohol for l in lineas),
        'sin_habitacion': not habilitadas.exists(), 'aviso_alcohol': AVISO_ALCOHOL,
        'items_carrito': sum(l['cantidad'] for l in lineas)})


@requiere_cliente
def mis_pedidos(request):
    pedidos = (Pedido.objects.filter(usuario=request.usuario).select_related('habitacion')
               .prefetch_related('detalles').order_by('-creado', '-id'))
    return render(request, 'Wbar/mis_pedidos.html', {
        'pagina': Paginator(pedidos, 10).get_page(request.GET.get('page')),
        'items_carrito': sum(carro.leer(request.session).values())})


@requiere_cliente
def pedido_detalle(request, pedido_id):
    # Filtrar por usuario: el pedido de otra persona responde 404 (no se confirma que exista).
    pedido = get_object_or_404(Pedido.objects.select_related('habitacion', 'reserva')
                               .prefetch_related('detalles', 'eventos'), pk=pedido_id, usuario=request.usuario)
    mostrar_codigo = pedido.estado in (Pedido.ACEPTADO, Pedido.EN_PREPARACION, Pedido.LISTO, Pedido.EN_CAMINO, Pedido.PENDIENTE)
    return render(request, 'Wbar/pedido_detalle.html', {
        'pedido': pedido, 'codigo': pedido.codigo_entrega() if mostrar_codigo else None,
        'puede_cancelar': pedido.estado == Pedido.PENDIENTE, 'aviso_alcohol': AVISO_ALCOHOL,
        'items_carrito': sum(carro.leer(request.session).values())})


@requiere_cliente
@require_POST
def pedido_cancelar(request, pedido_id):
    pedido = get_object_or_404(Pedido, pk=pedido_id, usuario=request.usuario)
    try:
        services.cancelar_pedido(pedido.pk, request.usuario, 'Cancelado por el huésped', como_cliente=True)
        messages.success(request, f"Pedido {pedido.numero} cancelado.")
    except ReglaDeNegocioError as e:
        messages.error(request, str(e))
    return redirect('wbar:pedido_detalle', pedido_id=pedido.pk)


# ===========================================================================
# PERSONAL (ADMIN_GERENTE y RECEPCIONISTA): pedidos
# ===========================================================================
def _ctx(request, seccion, **extra):
    base = {'seccion': seccion, 'usuario_nombre': request.session.get('usuario_nombre', ''),
            'usuario_rol_texto': TEXTO_ROL.get(request.usuario.rol, ''), 'es_admin': request.usuario.rol == ADMIN}
    base.update(extra)
    return base


@requiere_operacion
def gestion_pedidos(request):
    estado = request.GET.get('estado', 'ABIERTOS')
    q = request.GET.get('q', '').strip()[:40]
    qs = Pedido.objects.select_related('habitacion', 'usuario', 'atendido_por').prefetch_related('detalles')
    if estado == 'ABIERTOS':
        qs = qs.filter(estado__in=Pedido.ESTADOS_ABIERTOS)
    elif estado in dict(Pedido.ESTADOS):
        qs = qs.filter(estado=estado)
    if q:
        filtro = Q(habitacion__numero__icontains=q) | Q(usuario__nombres__icontains=q) | Q(usuario__apellidos__icontains=q)
        if q.upper().startswith('WB-'):
            q = q[3:]
        if q.isdigit():
            filtro |= Q(pk=int(q))
        qs = qs.filter(filtro)
    pagina = Paginator(qs.order_by('-creado', '-id'), 20).get_page(request.GET.get('page'))
    return render(request, 'Wbar/gestion_pedidos.html', _ctx(
        request, 'pedidos', pagina=pagina, estado=estado, q=request.GET.get('q', '')[:40],
        estados=Pedido.ESTADOS, abiertos=Pedido.objects.filter(estado__in=Pedido.ESTADOS_ABIERTOS).count()))


@requiere_operacion
def gestion_pedido(request, pedido_id):
    pedido = get_object_or_404(
        Pedido.objects.select_related('habitacion', 'reserva__huesped', 'usuario', 'atendido_por', 'entregado_por')
        .prefetch_related('detalles', 'eventos__usuario'), pk=pedido_id)
    siguientes = Pedido.TRANSICIONES.get(pedido.estado, ())
    return render(request, 'Wbar/gestion_pedido.html', _ctx(
        request, 'pedidos', pedido=pedido, siguientes=[(e, dict(Pedido.ESTADOS)[e]) for e in siguientes],
        form_entrega=EntregaForm(), form_cancelar=CancelarForm(),
        puede_entregar=pedido.estado == Pedido.EN_CAMINO and not pedido.codigo_bloqueado,
        puede_cancelar=pedido.estado in Pedido.ESTADOS_CANCELABLES, aviso_alcohol=AVISO_ALCOHOL,
        pago=getattr(pedido, 'pago_registrado', None)))


@requiere_operacion
@require_POST
def gestion_estado(request, pedido_id):
    try:
        pedido = services.cambiar_estado(pedido_id, request.POST.get('estado', ''), request.usuario)
        messages.success(request, f"Pedido {pedido.numero}: ahora está «{pedido.get_estado_display()}».")
    except ReglaDeNegocioError as e:
        messages.error(request, str(e))
    return redirect('wbar:gestion_pedido', pedido_id=pedido_id)


@requiere_operacion
@require_POST
def gestion_entrega(request, pedido_id):
    form = EntregaForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        try:
            pedido = services.confirmar_entrega(
                pedido_id, request.usuario, codigo=d['codigo'], edad_verificada=d['edad_verificada'],
                documento=d['documento'], forma_cobro=d['forma_cobro'], metodo_pago=d['metodo_pago'],
                referencia=d['referencia'])
            messages.success(request, f"Entrega del pedido {pedido.numero} confirmada.")
        except ReglaDeNegocioError as e:
            messages.error(request, str(e))
    else:
        messages.error(request, "Revisa los datos de la entrega: el código tiene 6 dígitos y debes indicar el cobro.")
    return redirect('wbar:gestion_pedido', pedido_id=pedido_id)


@requiere_operacion
@require_POST
def gestion_cancelar(request, pedido_id):
    form = CancelarForm(request.POST)
    motivo = form.cleaned_data['motivo'] if form.is_valid() else ''
    if not motivo.strip():
        messages.error(request, "Escribe el motivo de la cancelación.")
        return redirect('wbar:gestion_pedido', pedido_id=pedido_id)
    try:
        pedido = services.cancelar_pedido(pedido_id, request.usuario, motivo)
        messages.success(request, f"Pedido {pedido.numero} cancelado.")
    except ReglaDeNegocioError as e:
        messages.error(request, str(e))
    return redirect('wbar:gestion_pedido', pedido_id=pedido_id)


# ===========================================================================
# ADMINISTRADOR: catálogo y estadísticas
# ===========================================================================
@requiere_administrador
def gestion_categorias(request):
    cats = CategoriaProducto.objects.annotate(n=Count('productos')).order_by('orden', 'nombre')
    return render(request, 'Wbar/gestion_categorias.html', _ctx(request, 'categorias', categorias=cats))


@requiere_administrador
def gestion_categoria_form(request, categoria_id=None):
    cat = get_object_or_404(CategoriaProducto, pk=categoria_id) if categoria_id else None
    form = CategoriaForm(request.POST or None, instance=cat)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, "Categoría guardada.")
        return redirect('wbar:gestion_categorias')
    return render(request, 'Wbar/gestion_form.html', _ctx(
        request, 'categorias', form=form, titulo='Editar categoría' if cat else 'Nueva categoría',
        volver='wbar:gestion_categorias', multipart=False))


@requiere_administrador
def gestion_productos(request):
    q = request.GET.get('q', '').strip()[:60]
    cat = _entero(request.GET.get('categoria'))
    qs = Producto.objects.select_related('categoria')
    if q:
        qs = qs.filter(nombre__icontains=q)
    if cat:
        qs = qs.filter(categoria_id=cat)
    return render(request, 'Wbar/gestion_productos.html', _ctx(
        request, 'productos', pagina=Paginator(qs, 20).get_page(request.GET.get('page')), q=q, cat=cat,
        categorias=CategoriaProducto.objects.order_by('orden', 'nombre')))


@requiere_administrador
def gestion_producto_form(request, producto_id=None):
    prod = get_object_or_404(Producto, pk=producto_id) if producto_id else None
    form = ProductoForm(request.POST or None, request.FILES or None, instance=prod)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, "Producto guardado.")
        return redirect('wbar:gestion_productos')
    return render(request, 'Wbar/gestion_form.html', _ctx(
        request, 'productos', form=form, titulo='Editar producto' if prod else 'Nuevo producto',
        volver='wbar:gestion_productos', multipart=True, objeto=prod))


@requiere_administrador
@require_POST
def gestion_producto_alternar(request, producto_id):
    """Activa/desactiva (campo 'activo') o marca disponible/agotado. Nunca borra: el historial se conserva."""
    prod = get_object_or_404(Producto, pk=producto_id)
    campo = request.POST.get('campo')
    if campo not in ('activo', 'disponible'):
        messages.error(request, "Acción no válida.")
    else:
        setattr(prod, campo, not getattr(prod, campo))
        prod.save(update_fields=[campo, 'actualizado'])
        messages.success(request, f"«{prod.nombre}»: {campo} = {'sí' if getattr(prod, campo) else 'no'}.")
    return redirect('wbar:gestion_productos')


@requiere_administrador
def gestion_estadisticas(request):
    hoy = timezone.localdate()
    desde = hoy - timedelta(days=30)
    validos = Pedido.objects.exclude(estado=Pedido.CANCELADO).filter(creado__date__gte=desde)
    resumen = validos.aggregate(n=Count('id'), total=Sum('total'))
    entregados = Pedido.objects.filter(estado=Pedido.ENTREGADO, creado__date__gte=desde)
    cobrado = entregados.filter(estado_pago=Pedido.PAGO_COBRADO).aggregate(t=Sum('total'))['t'] or Decimal('0')
    cuenta = entregados.filter(estado_pago=Pedido.PAGO_CARGO_CUENTA).aggregate(t=Sum('total'))['t'] or Decimal('0')
    top = (DetallePedido.objects.filter(pedido__in=entregados)
           .values('nombre_producto').annotate(u=Sum('cantidad'), ingresos=Sum('subtotal')).order_by('-u')[:10])
    por_estado = Pedido.objects.filter(creado__date__gte=desde).values('estado').annotate(n=Count('id'))
    etiquetas = dict(Pedido.ESTADOS)
    return render(request, 'Wbar/gestion_estadisticas.html', _ctx(
        request, 'estadisticas', desde=desde, hoy=hoy, n_pedidos=resumen['n'] or 0, total=resumen['total'] or Decimal('0'),
        cobrado=cobrado, cuenta=cuenta, top=top,
        por_estado=[(etiquetas.get(e['estado'], e['estado']), e['n']) for e in por_estado]))
