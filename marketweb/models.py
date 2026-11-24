from datetime import time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


# 1. Tabla: hotel
class Hotel(models.Model):
    nombre = models.CharField(max_length=100, default="Hotel W")
    direccion = models.CharField(max_length=200, default="NN")
    telefono = models.CharField(max_length=20, default="NN")
    correo = models.EmailField(default="NN")
    horario_checkin = models.TimeField(default=time(15, 0))
    horario_checkout = models.TimeField(default=time(12, 0))

    class Meta:
        db_table = 'hotel'

    def __str__(self):
        return self.nombre


# 2. Tabla: usuario
class Usuario(models.Model):
    ROLES = [
        ('ADMIN_GERENTE', 'Admin-Gerente'),
        ('RECEPCIONISTA', 'Recepcionista'),
        ('CLIENTE', 'Cliente'),
    ]
    nombres = models.CharField(max_length=80)
    apellidos = models.CharField(max_length=80)
    correo = models.EmailField(unique=True)
    contrasena = models.CharField(max_length=255)
    rol = models.CharField(max_length=20, choices=ROLES, default='RECEPCIONISTA')
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'usuario'

    def __str__(self):
        return f"{self.nombres} {self.apellidos} ({self.rol})"


# 3. Tabla: tipo_habitacion
class TipoHabitacion(models.Model):
    nombre = models.CharField(max_length=50)  # W Essential, W Comfort, ... (ver catalogo_hotel.py)
    capacidad = models.IntegerField(default=4)
    # OBSOLETO: tarifa anterior por persona/noche. Se conserva para no perder datos
    # existentes; ya no interviene en ningún cálculo. Se puede eliminar en una
    # migración posterior, cuando se confirme que ningún template la usa.
    precio_persona_noche = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('160650.00'))
    # Tarifa vigente: precio POR HABITACIÓN por noche.
    precio_noche = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    descripcion = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'tipo_habitacion'

    def __str__(self):
        return f"{self.nombre} (Capacidad: {self.capacidad} personas)"


# 4. Tabla: habitacion
class Habitacion(models.Model):
    DISPONIBLE = 'DISPONIBLE'
    OCUPADA = 'OCUPADA'
    EN_ASEO = 'EN_ASEO'
    MANTENIMIENTO = 'MANTENIMIENTO'
    FUERA_DE_SERVICIO = 'FUERA_DE_SERVICIO'

    # Estados FÍSICOS almacenados. "Reservada" NO se almacena: se calcula
    # a partir de las reservas (ver services.anotar_estados).
    ESTADOS = [
        (DISPONIBLE, 'Disponible'),
        (OCUPADA, 'Ocupada'),
        (EN_ASEO, 'En Aseo'),
        (MANTENIMIENTO, 'Mantenimiento'),
        (FUERA_DE_SERVICIO, 'Fuera de servicio'),
    ]
    ESTADOS_NO_RESERVABLES = (MANTENIMIENTO, FUERA_DE_SERVICIO)
    COLORES = {
        DISPONIBLE: '#FFFFFF',
        OCUPADA: '#1E88E5',
        EN_ASEO: '#FFB74D',
        MANTENIMIENTO: '#8D6E63',
        FUERA_DE_SERVICIO: '#757575',
    }

    numero = models.CharField(max_length=10, unique=True)
    piso = models.IntegerField(default=1)
    tipo = models.ForeignKey(TipoHabitacion, on_delete=models.CASCADE, related_name='habitaciones')
    hotel = models.ForeignKey(Hotel, on_delete=models.CASCADE, related_name='habitaciones')
    estado = models.CharField(max_length=20, choices=ESTADOS, default=DISPONIBLE)
    activa = models.BooleanField(default=True)
    codigo_color = models.CharField(max_length=10, default='#FFFFFF')

    class Meta:
        db_table = 'habitacion'
        ordering = ['piso', 'numero']
        indexes = [models.Index(fields=['estado'], name='habitacion_estado_idx')]

    def save(self, *args, **kwargs):
        # Asignación automática de códigos de color institucionales
        self.codigo_color = self.COLORES.get(self.estado, self.codigo_color)
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if not self.pk:
            return
        vigentes = Reserva.objects.filter(
            habitacion_id=self.pk, estado__in=Reserva.ESTADOS_BLOQUEANTES
        )
        if self.estado == self.DISPONIBLE and vigentes.filter(estado='CHECKIN').exists():
            raise ValidationError({
                'estado': 'No se puede marcar como Disponible: hay un huésped con check-in en esta habitación.'
            })
        sacar_de_operacion = self.estado in self.ESTADOS_NO_RESERVABLES or not self.activa
        if sacar_de_operacion and vigentes.filter(
            Q(estado='CHECKIN') | Q(fecha_salida__gte=timezone.localdate())
        ).exists():
            raise ValidationError(
                'No se puede inhabilitar la habitación (mantenimiento, fuera de servicio o inactiva) '
                'mientras tenga reservas vigentes. Cancélalas o reubícalas primero.'
            )

    def motivo_no_reservable(self, entrada, hoy=None):
        """Texto con el motivo por el que NO se puede reservar, o None si se puede."""
        hoy = hoy or timezone.localdate()
        if not self.activa:
            return f"La habitación {self.numero} está inactiva."
        if self.estado in self.ESTADOS_NO_RESERVABLES:
            return f"La habitación {self.numero} está en {self.get_estado_display().lower()} y no admite reservas."
        if entrada <= hoy and self.estado != self.DISPONIBLE:
            return (f"La habitación {self.numero} no está disponible hoy "
                    f"(estado: {self.get_estado_display().lower()}).")
        return None

    def __str__(self):
        return f"Habitación {self.numero} (Piso {self.piso})"


# 5. Tabla: huesped
class Huesped(models.Model):
    TIPOS_DOC = [
        ('CC', 'Cédula de Ciudadanía'),
        ('CE', 'Cédula de Extranjería'),
        ('PASAPORTE', 'Pasaporte'),
        ('TI', 'Tarjeta de Identidad'),
    ]
    tipo_documento = models.CharField(max_length=20, choices=TIPOS_DOC, default='CC')
    numero_documento = models.CharField(max_length=30, unique=True)
    nombres = models.CharField(max_length=80)
    apellidos = models.CharField(max_length=80)
    telefono = models.CharField(max_length=20)
    correo = models.EmailField()
    nacionalidad = models.CharField(max_length=50, default='Colombiana')
    es_extranjero = models.BooleanField(default=False)

    class Meta:
        db_table = 'huesped'

    def __str__(self):
        return f"{self.nombres} {self.apellidos} - {self.numero_documento}"


# 6. Tabla: reserva
class Reserva(models.Model):
    ESTADOS_RESERVA = [
        ('PENDIENTE', 'Pendiente Pago'),
        ('CONFIRMADA', 'Confirmada'),
        ('CHECKIN', 'Huésped en Casa'),
        ('CHECKOUT', 'Finalizada'),   # valor interno CHECKOUT; se muestra "Finalizada"
        ('CANCELADA', 'Cancelada'),
    ]
    CANALES = [
        ('DIRECTA', 'Directa / WhatsApp'),
        ('WEB', 'Sitio Web Hotel W'),
        ('BOOKING', 'Booking.com'),
        ('EXPEDIA', 'Expedia'),
        ('PRESENCIAL', 'Presencial'),
    ]
    # Estados que ocupan la habitación en el calendario (igual que antes).
    ESTADOS_BLOQUEANTES = ('CONFIRMADA', 'CHECKIN')

    huesped = models.ForeignKey(Huesped, on_delete=models.PROTECT, related_name='reservas')
    # Cuenta web que hizo la reserva. Nula en las reservas creadas por el personal
    # (y en las anteriores a esta columna): no se pierde ningún dato existente.
    usuario = models.ForeignKey(Usuario, null=True, blank=True, on_delete=models.PROTECT,
                                related_name='reservas_web')
    habitacion = models.ForeignKey(Habitacion, on_delete=models.PROTECT, related_name='reservas')
    fecha_entrada = models.DateField()
    fecha_salida = models.DateField()
    num_huespedes = models.IntegerField(default=1)
    canal_reserva = models.CharField(max_length=20, choices=CANALES, default='PRESENCIAL')
    estado = models.CharField(max_length=20, choices=ESTADOS_RESERVA, default='PENDIENTE')
    valor_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    cargo_tardio = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    checkin_real = models.DateTimeField(null=True, blank=True)
    checkout_real = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'reserva'
        indexes = [
            models.Index(fields=['habitacion', 'fecha_entrada', 'fecha_salida'], name='reserva_hab_fechas_idx'),
            models.Index(fields=['estado'], name='reserva_estado_idx'),
        ]

    @staticmethod
    def noches_entre(entrada, salida):
        noches = (salida - entrada).days
        return noches if noches > 0 else 1

    @classmethod
    def alojamiento_para(cls, tipo, entrada, salida):
        # Regla confirmada: noches × precio de la habitación por noche
        # (NO se multiplica por el número de huéspedes).
        return Decimal(cls.noches_entre(entrada, salida)) * tipo.precio_noche

    def calcular_noches(self):
        return self.noches_entre(self.fecha_entrada, self.fecha_salida)

    def calcular_alojamiento(self):
        return self.alojamiento_para(self.habitacion.tipo, self.fecha_entrada, self.fecha_salida)

    @classmethod
    def solapadas(cls, habitacion_id, entrada, salida, excluir_pk=None, bloquear=False):
        """Reservas que ocupan la habitación en [entrada, salida). El día de salida
        queda libre para la siguiente llegada. bloquear=True usa select_for_update
        (solo dentro de transaction.atomic)."""
        qs = cls.objects.filter(
            habitacion_id=habitacion_id,
            estado__in=cls.ESTADOS_BLOQUEANTES,
            fecha_entrada__lt=salida,
            fecha_salida__gt=entrada,
        )
        if excluir_pk:
            qs = qs.exclude(pk=excluir_pk)
        if bloquear:
            qs = qs.select_for_update()
        return qs

    @staticmethod
    def errores_de_fechas(entrada, salida, hoy=None, validar_pasado=False):
        """Reglas de fechas en un solo lugar (las usan el servicio y la API)."""
        errores = []
        if salida <= entrada:
            errores.append("La fecha de salida debe ser posterior a la fecha de entrada.")
        if validar_pasado and entrada < (hoy or timezone.localdate()):
            errores.append("La fecha de entrada no puede ser anterior a hoy.")
        return errores

    @classmethod
    def errores_de_reserva(cls, habitacion, entrada, salida, num_huespedes,
                           excluir_pk=None, hoy=None, verificar_estado=True,
                           validar_pasado=False, bloquear=False):
        """Reglas de la ficha (fechas, capacidad, disponibilidad) en un solo lugar.
        Devuelve una lista de mensajes; vacía si todo está bien.
        validar_pasado solo se activa al CREAR (no al editar reservas ya en curso)."""
        errores = cls.errores_de_fechas(entrada, salida, hoy, validar_pasado)
        if num_huespedes < 1:
            errores.append("Debe haber al menos un huésped.")
        elif num_huespedes > habitacion.tipo.capacidad:
            errores.append(
                f"La habitación {habitacion.numero} ({habitacion.tipo.nombre}) admite máximo "
                f"{habitacion.tipo.capacidad} huéspedes y se pidieron {num_huespedes}."
            )
        if verificar_estado:
            motivo = habitacion.motivo_no_reservable(entrada, hoy)
            if motivo:
                errores.append(motivo)
        if habitacion.tipo.precio_noche <= 0:
            errores.append(f"El tipo «{habitacion.tipo.nombre}» no tiene precio por noche configurado.")
        if salida > entrada:
            choque = cls.solapadas(habitacion.pk, entrada, salida, excluir_pk, bloquear).first()
            if choque:
                errores.append(
                    f"La habitación {habitacion.numero} ya tiene la reserva #{choque.pk} "
                    f"del {choque.fecha_entrada:%d/%m/%Y} al {choque.fecha_salida:%d/%m/%Y}."
                )
        return errores

    def clean(self):
        super().clean()
        errores = {}
        if self.habitacion_id and self.fecha_entrada and self.fecha_salida and self.num_huespedes is not None:
            if self.estado in self.ESTADOS_BLOQUEANTES:
                mensajes = self.errores_de_reserva(
                    self.habitacion, self.fecha_entrada, self.fecha_salida, self.num_huespedes,
                    excluir_pk=self.pk, verificar_estado=not self.pk,
                )
                if mensajes:
                    errores['habitacion'] = mensajes
        if self.estado in ('CHECKIN', 'CHECKOUT'):
            anterior = (Reserva.objects.filter(pk=self.pk).values_list('estado', flat=True).first()
                        if self.pk else None)
            if anterior != self.estado:
                errores['estado'] = "Check-in y check-out se registran desde el panel, no editando el estado."
        if errores:
            raise ValidationError(errores)

    def __str__(self):
        return f"Reserva #{self.id} - Hab: {self.habitacion.numero} ({self.huesped.nombres})"


# 7. Tabla: servicio (Catálogo de minibar y lavandería)
class Servicio(models.Model):
    CATEGORIAS = [
        ('LAVANDERIA', 'Servicio de Lavandería'),
        ('MINIBAR', 'Consumo de Minibar'),
    ]
    nombre = models.CharField(max_length=100)
    categoria = models.CharField(max_length=20, choices=CATEGORIAS, default='MINIBAR')
    precio = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = 'servicio'

    def __str__(self):
        return f"{self.nombre} (${self.precio})"


# 8. Tabla: servicio_reserva
class ServicioReserva(models.Model):
    reserva = models.ForeignKey(Reserva, on_delete=models.CASCADE, related_name='servicios')
    servicio = models.ForeignKey(Servicio, on_delete=models.CASCADE)
    cantidad = models.IntegerField(default=1)
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)

    def save(self, *args, **kwargs):
        self.subtotal = Decimal(self.cantidad) * Decimal(self.precio_unitario)
        super().save(*args, **kwargs)

    class Meta:
        db_table = 'servicio_reserva'


# 9. Tabla: pago
class Pago(models.Model):
    METODOS = [
        ('EFECTIVO', 'Efectivo'),
        ('TRANSFERENCIA', 'Transferencia Bancaria'),
        ('DATAFONO', 'Datáfono'),
        ('LINK_PAGO', 'Link de Pago'),
    ]
    reserva = models.ForeignKey(Reserva, on_delete=models.CASCADE, related_name='pagos')
    monto = models.DecimalField(max_digits=10, decimal_places=2)
    metodo_pago = models.CharField(max_length=20, choices=METODOS)
    fecha_pago = models.DateTimeField(auto_now_add=True)
    # Trazabilidad (recepción). Nulos/vacíos en los pagos anteriores: no se pierde ningún dato.
    registrado_por = models.ForeignKey(Usuario, null=True, blank=True, db_constraint=False, on_delete=models.PROTECT,
    related_name='pagos_registrados')
    referencia = models.CharField(max_length=60, blank=True, default='')

    class Meta:
        db_table = 'pago'


# 10. Tabla: factura
class Factura(models.Model):
    reserva = models.OneToOneField(Reserva, on_delete=models.CASCADE, related_name='factura')
    numero_dian = models.CharField(max_length=30, null=True, blank=True)
    fecha_emision = models.DateTimeField(auto_now_add=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    iva = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    exento_iva = models.BooleanField(default=False)  # Exención para huéspedes extranjeros (RN17)

    class Meta:
        db_table = 'factura'

    def __str__(self):
        return f"Factura DIAN {self.numero_dian} - Reserva #{self.reserva.id}"

# 11. Tabla: operacion_recepcion (historial de operaciones del personal de recepción)
class OperacionRecepcion(models.Model):
    """Bitácora de solo lectura: cada operación de recepción deja una fila que no se edita ni se borra."""
    HUESPED_ALTA = 'HUESPED_ALTA'
    HUESPED_EDICION = 'HUESPED_EDICION'
    RESERVA_CREADA = 'RESERVA_CREADA'
    RESERVA_MODIFICADA = 'RESERVA_MODIFICADA'
    RESERVA_CONFIRMADA = 'RESERVA_CONFIRMADA'
    RESERVA_CANCELADA = 'RESERVA_CANCELADA'
    CHECKIN = 'CHECKIN'
    CHECKOUT = 'CHECKOUT'
    SERVICIO = 'SERVICIO'
    PAGO = 'PAGO'
    HABITACION_HABILITADA = 'HABITACION_HABILITADA'
    TIPOS = [
        (HUESPED_ALTA, 'Huésped registrado'),
        (HUESPED_EDICION, 'Huésped actualizado'),
        (RESERVA_CREADA, 'Reserva creada'),
        (RESERVA_MODIFICADA, 'Reserva modificada'),
        (RESERVA_CONFIRMADA, 'Reserva confirmada'),
        (RESERVA_CANCELADA, 'Reserva cancelada'),
        (CHECKIN, 'Check-in'),
        (CHECKOUT, 'Check-out'),
        (SERVICIO, 'Servicio adicional'),
        (PAGO, 'Pago / abono'),
        (HABITACION_HABILITADA, 'Habitación habilitada'),
    ]

    usuario = models.ForeignKey(Usuario, null=True, on_delete=models.PROTECT, related_name='operaciones_recepcion')
    tipo = models.CharField(max_length=24, choices=TIPOS)
    fecha = models.DateTimeField(auto_now_add=True)
    reserva = models.ForeignKey(Reserva, null=True, blank=True, on_delete=models.PROTECT, related_name='operaciones')
    huesped = models.ForeignKey(Huesped, null=True, blank=True, on_delete=models.PROTECT, related_name='operaciones')
    monto = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    descripcion = models.CharField(max_length=300)

    class Meta:
        db_table = 'operacion_recepcion'
        ordering = ['-fecha', '-id']
        indexes = [
            models.Index(fields=['fecha'], name='oprec_fecha_idx'),
            models.Index(fields=['tipo'], name='oprec_tipo_idx'),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('El historial de operaciones no se puede modificar.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('El historial de operaciones no se puede eliminar.')

    def __str__(self):
        return f"{self.get_tipo_display()} · {self.fecha:%d/%m/%Y %H:%M}"
