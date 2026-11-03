"""
Modelos de W Bar Online (bar digital del Hotel W).

Se reutilizan las tablas existentes del hotel (Usuario, Reserva, Habitacion,
ServicioReserva, Pago). W Bar solo agrega lo que no existía: catálogo, pedidos y su
trazabilidad. El dinero siempre es Decimal (nunca float).
"""
import hashlib
import hmac
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.text import slugify

from marketweb.models import Habitacion, Pago, Reserva, ServicioReserva, Usuario

from .validators import validar_extension_imagen, validar_tamano_imagen


class CategoriaProducto(models.Model):
    nombre = models.CharField(max_length=60, unique=True)
    slug = models.SlugField(max_length=70, unique=True, blank=True)
    descripcion = models.CharField(max_length=200, blank=True)
    orden = models.PositiveSmallIntegerField(default=0, help_text="Menor número = aparece primero en el menú.")
    activa = models.BooleanField(default=True)
    creada = models.DateTimeField(auto_now_add=True)
    actualizada = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wbar_categoria'
        ordering = ['orden', 'nombre']
        verbose_name = 'categoría'
        verbose_name_plural = 'categorías'

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.nombre) or 'categoria'
            slug, n = base, 2
            while CategoriaProducto.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, n = f"{base}-{n}", n + 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return self.nombre


class Producto(models.Model):
    categoria = models.ForeignKey(CategoriaProducto, on_delete=models.PROTECT, related_name='productos')
    nombre = models.CharField(max_length=120)
    descripcion = models.TextField(blank=True)
    presentacion = models.CharField(max_length=60, blank=True, help_text="Ej.: botella 750 ml, copa, lata 330 ml.")
    informacion = models.TextField(blank=True, help_text="Información relevante: ingredientes, origen, alérgenos, etc.")
    precio = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    imagen = models.ImageField(
        upload_to='wbar/productos/', blank=True,
        validators=[validar_extension_imagen, validar_tamano_imagen],
        help_text="JPG, PNG o WEBP de máximo 3 MB.")
    activo = models.BooleanField(default=True, help_text="Si se desactiva, desaparece del menú pero el historial se conserva.")
    disponible = models.BooleanField(default=True, help_text="Desmarcar cuando el producto se agote temporalmente.")
    contiene_alcohol = models.BooleanField(default=False)
    stock = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Déjalo vacío si no se controla inventario. Con un número, se descuenta al pedir.")
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wbar_producto'
        ordering = ['categoria__orden', 'categoria__nombre', 'nombre']
        indexes = [models.Index(fields=['activo', 'disponible'], name='wbar_prod_activo_idx')]

    @property
    def se_puede_pedir(self):
        return self.activo and self.disponible and self.categoria.activa and (self.stock is None or self.stock > 0)

    def __str__(self):
        return f"{self.nombre} (${self.precio})"


class Pedido(models.Model):
    PENDIENTE = 'PENDIENTE'
    ACEPTADO = 'ACEPTADO'
    EN_PREPARACION = 'EN_PREPARACION'
    LISTO = 'LISTO'
    EN_CAMINO = 'EN_CAMINO'
    ENTREGADO = 'ENTREGADO'
    CANCELADO = 'CANCELADO'
    ESTADOS = [
        (PENDIENTE, 'Pendiente'), (ACEPTADO, 'Aceptado'), (EN_PREPARACION, 'En preparación'),
        (LISTO, 'Listo'), (EN_CAMINO, 'En camino'), (ENTREGADO, 'Entregado'), (CANCELADO, 'Cancelado'),
    ]
    # Transiciones que el PERSONAL puede hacer con los botones de estado. ENTREGADO no está:
    # solo se alcanza verificando el código (services.confirmar_entrega).
    TRANSICIONES = {
        PENDIENTE: (ACEPTADO,),
        ACEPTADO: (EN_PREPARACION,),
        EN_PREPARACION: (LISTO,),
        LISTO: (EN_CAMINO,),
        EN_CAMINO: (),
    }
    ESTADOS_ABIERTOS = (PENDIENTE, ACEPTADO, EN_PREPARACION, LISTO, EN_CAMINO)
    # Estados desde los que el personal aún puede cancelar.
    ESTADOS_CANCELABLES = ESTADOS_ABIERTOS

    PAGO_PENDIENTE = 'PENDIENTE'
    PAGO_COBRADO = 'COBRADO'
    PAGO_CARGO_CUENTA = 'CARGO_CUENTA'
    ESTADOS_PAGO = [
        (PAGO_PENDIENTE, 'Por cobrar'),
        (PAGO_COBRADO, 'Pagado en la habitación'),
        (PAGO_CARGO_CUENTA, 'Cargado a la cuenta de la habitación'),
    ]

    DOCUMENTOS = [('CC', 'Cédula de ciudadanía'), ('CE', 'Cédula de extranjería'), ('PASAPORTE', 'Pasaporte')]

    usuario = models.ForeignKey(Usuario, on_delete=models.PROTECT, related_name='pedidos_wbar')
    reserva = models.ForeignKey(Reserva, on_delete=models.PROTECT, related_name='pedidos_wbar')
    # Copia de la habitación al momento del pedido (la reserva podría reubicarse después).
    habitacion = models.ForeignKey(Habitacion, on_delete=models.PROTECT, related_name='pedidos_wbar')
    estado = models.CharField(max_length=20, choices=ESTADOS, default=PENDIENTE)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    notas = models.CharField(max_length=300, blank=True)
    contiene_alcohol = models.BooleanField(default=False)
    declaracion_mayoria_edad = models.BooleanField(
        default=False, help_text="Declaración del cliente. NO prueba la edad: la verifica el personal al entregar.")
    # Idempotencia: el formulario de confirmación lleva un token de un solo uso.
    token_envio = models.CharField(max_length=40, unique=True)

    # Código de entrega: NO se guarda el código; se guarda una semilla y el código se deriva
    # con HMAC(SECRET_KEY, semilla). Quien vea la base de datos no puede reconstruirlo.
    codigo_semilla = models.CharField(max_length=40)
    intentos_codigo_fallidos = models.PositiveSmallIntegerField(default=0)
    codigo_usado_en = models.DateTimeField(null=True, blank=True)

    atendido_por = models.ForeignKey(Usuario, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name='pedidos_wbar_atendidos')
    entregado_en = models.DateTimeField(null=True, blank=True)
    entregado_por = models.ForeignKey(Usuario, null=True, blank=True, on_delete=models.PROTECT,
                                      related_name='pedidos_wbar_entregados')
    edad_verificada = models.BooleanField(default=False)
    edad_verificada_por = models.ForeignKey(Usuario, null=True, blank=True, on_delete=models.PROTECT,
                                            related_name='+')
    documento_verificado = models.CharField(max_length=12, choices=DOCUMENTOS, blank=True)
    estado_pago = models.CharField(max_length=15, choices=ESTADOS_PAGO, default=PAGO_PENDIENTE)
    motivo_cancelacion = models.CharField(max_length=200, blank=True)

    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wbar_pedido'
        ordering = ['-creado', '-id']
        indexes = [
            models.Index(fields=['estado', 'creado'], name='wbar_pedido_estado_idx'),
            models.Index(fields=['usuario', 'creado'], name='wbar_pedido_usuario_idx'),
        ]

    MAX_INTENTOS_CODIGO = 5

    @property
    def numero(self):
        return f"WB-{self.pk:06d}" if self.pk else 'WB-NUEVO'

    @property
    def esta_abierto(self):
        return self.estado in self.ESTADOS_ABIERTOS

    @property
    def codigo_bloqueado(self):
        return self.intentos_codigo_fallidos >= self.MAX_INTENTOS_CODIGO

    @staticmethod
    def _derivar_codigo(semilla):
        clave = hashlib.sha256(('wbar-codigo-entrega|' + settings.SECRET_KEY).encode()).digest()
        digest = hmac.new(clave, semilla.encode(), hashlib.sha256).digest()
        return f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"

    def codigo_entrega(self):
        """Código de 6 dígitos que ve el cliente dueño del pedido."""
        return self._derivar_codigo(self.codigo_semilla)

    def codigo_coincide(self, texto):
        return constant_time_compare(self.codigo_entrega(), (texto or '').strip())

    def __str__(self):
        return f"{self.numero} · Hab. {self.habitacion.numero} · {self.get_estado_display()}"


class DetallePedido(models.Model):
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name='detalles')
    producto = models.ForeignKey(Producto, null=True, on_delete=models.PROTECT, related_name='detalles_pedido')
    # Copias históricas: si el producto cambia de precio o desaparece, el pedido no se altera.
    nombre_producto = models.CharField(max_length=120)
    categoria_nombre = models.CharField(max_length=60, blank=True)
    presentacion = models.CharField(max_length=60, blank=True)
    contiene_alcohol = models.BooleanField(default=False)
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2)
    cantidad = models.PositiveSmallIntegerField()
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    # Consumo registrado en la cuenta de la reserva al entregar (una sola vez).
    servicio_reserva = models.OneToOneField(ServicioReserva, null=True, blank=True, on_delete=models.SET_NULL,
                                            related_name='detalle_wbar')

    class Meta:
        db_table = 'wbar_detalle_pedido'

    def save(self, *args, **kwargs):
        self.subtotal = Decimal(self.cantidad) * Decimal(self.precio_unitario)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.cantidad} × {self.nombre_producto}"


class EventoPedido(models.Model):
    """Historial inmutable de cambios del pedido (auditoría)."""
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name='eventos')
    estado_anterior = models.CharField(max_length=20, blank=True, choices=Pedido.ESTADOS)
    estado_nuevo = models.CharField(max_length=20, blank=True, choices=Pedido.ESTADOS)
    usuario = models.ForeignKey(Usuario, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    rol = models.CharField(max_length=20, blank=True)
    nota = models.CharField(max_length=250, blank=True)
    fecha = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'wbar_evento_pedido'
        ordering = ['fecha', 'id']

    def __str__(self):
        return f"{self.pedido.numero}: {self.estado_anterior} → {self.estado_nuevo}"


class PagoPedido(models.Model):
    """Une un pedido con el Pago (tabla `pago` existente) que lo cobró en la habitación.
    OneToOne en ambos sentidos: un pedido no se cobra dos veces ni un pago sirve a dos pedidos."""
    pedido = models.OneToOneField(Pedido, on_delete=models.PROTECT, related_name='pago_registrado')
    pago = models.OneToOneField(Pago, on_delete=models.PROTECT, related_name='pedido_wbar')
    metodo = models.CharField(max_length=20, choices=Pago.METODOS)
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    referencia = models.CharField(max_length=40, blank=True, help_text="Número de comprobante del datáfono, si aplica.")
    registrado_por = models.ForeignKey(Usuario, on_delete=models.PROTECT, related_name='+')
    fecha = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'wbar_pago_pedido'
