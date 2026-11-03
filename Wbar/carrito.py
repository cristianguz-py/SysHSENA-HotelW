"""
Carrito de W Bar Online, guardado en la SESIÓN de Django (del lado del servidor).

En la sesión solo se guardan {id_producto: cantidad}. Nombres, precios y totales se leen
SIEMPRE de la base de datos cada vez que se muestra o se confirma el carrito: lo que
envíe el navegador nunca se usa como precio.
"""
from decimal import Decimal

from .models import Producto

CLAVE_SESION = 'wbar_carrito'
CLAVE_TOKEN = 'wbar_token_pedido'
MAX_UNIDADES_POR_PRODUCTO = 20
MAX_LINEAS = 25


def leer(session):
    """Carrito de la sesión depurado: solo ids enteros positivos y cantidades válidas."""
    crudo = session.get(CLAVE_SESION)
    limpio = {}
    if isinstance(crudo, dict):
        for clave, cantidad in crudo.items():
            try:
                pid, cant = int(clave), int(cantidad)
            except (TypeError, ValueError):
                continue
            if pid > 0 and 1 <= cant <= MAX_UNIDADES_POR_PRODUCTO:
                limpio[pid] = cant
    return limpio


def _guardar(session, carrito):
    session[CLAVE_SESION] = {str(pid): cant for pid, cant in carrito.items()}
    session.modified = True


def vaciar(session):
    session.pop(CLAVE_SESION, None)
    session.pop(CLAVE_TOKEN, None)
    session.modified = True


def agregar(session, producto_id, cantidad):
    """Suma `cantidad` al producto. Devuelve (ok, mensaje)."""
    producto = Producto.objects.select_related('categoria').filter(pk=producto_id).first()
    if producto is None or not producto.se_puede_pedir:
        return False, "Ese producto no está disponible en este momento."
    carrito = leer(session)
    if producto.pk not in carrito and len(carrito) >= MAX_LINEAS:
        return False, f"El pedido admite máximo {MAX_LINEAS} productos distintos."
    nueva = carrito.get(producto.pk, 0) + cantidad
    if cantidad < 1:
        return False, "La cantidad debe ser al menos 1."
    if nueva > MAX_UNIDADES_POR_PRODUCTO:
        return False, f"Máximo {MAX_UNIDADES_POR_PRODUCTO} unidades de cada producto por pedido."
    if producto.stock is not None and nueva > producto.stock:
        return False, f"Solo quedan {producto.stock} unidades de «{producto.nombre}»."
    carrito[producto.pk] = nueva
    _guardar(session, carrito)
    return True, f"«{producto.nombre}» agregado al pedido."


def fijar(session, producto_id, cantidad):
    """Fija la cantidad exacta (0 quita el producto). Devuelve (ok, mensaje)."""
    carrito = leer(session)
    if producto_id not in carrito:
        return False, "Ese producto no está en tu pedido."
    if cantidad <= 0:
        carrito.pop(producto_id)
        _guardar(session, carrito)
        return True, "Producto eliminado del pedido."
    if cantidad > MAX_UNIDADES_POR_PRODUCTO:
        return False, f"Máximo {MAX_UNIDADES_POR_PRODUCTO} unidades de cada producto por pedido."
    producto = Producto.objects.filter(pk=producto_id).first()
    if producto is None or not producto.se_puede_pedir:
        carrito.pop(producto_id)
        _guardar(session, carrito)
        return False, "Ese producto ya no está disponible y se quitó de tu pedido."
    if producto.stock is not None and cantidad > producto.stock:
        return False, f"Solo quedan {producto.stock} unidades de «{producto.nombre}»."
    carrito[producto_id] = cantidad
    _guardar(session, carrito)
    return True, "Cantidad actualizada."


def quitar(session, producto_id):
    carrito = leer(session)
    carrito.pop(producto_id, None)
    _guardar(session, carrito)


def detallar(session):
    """Devuelve (lineas, total, avisos). Cada línea: producto, cantidad, subtotal (de la BD).
    Los productos que dejaron de estar disponibles se quitan y se avisa."""
    carrito = leer(session)
    productos = {p.pk: p for p in Producto.objects.select_related('categoria').filter(pk__in=carrito)}
    lineas, avisos, total, cambio = [], [], Decimal('0.00'), False
    for pid, cantidad in carrito.items():
        p = productos.get(pid)
        if p is None or not p.se_puede_pedir:
            avisos.append(f"«{p.nombre if p else 'Un producto'}» ya no está disponible y se quitó de tu pedido.")
            cambio = True
            continue
        if p.stock is not None and cantidad > p.stock:
            avisos.append(f"De «{p.nombre}» solo quedan {p.stock}; ajustamos la cantidad.")
            cantidad, cambio = p.stock, True
        subtotal = p.precio * cantidad
        total += subtotal
        lineas.append({'producto': p, 'cantidad': cantidad, 'subtotal': subtotal})
    if cambio:
        _guardar(session, {l['producto'].pk: l['cantidad'] for l in lineas})
    return lineas, total, avisos
