from django.contrib import admin

from .models import Habitacion, Hotel, Huesped, OperacionRecepcion, Reserva, TipoHabitacion, Usuario


@admin.register(Hotel)
class HotelAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'direccion', 'horario_checkin', 'horario_checkout')


@admin.register(TipoHabitacion)
class TipoHabitacionAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'capacidad', 'precio_noche')
    search_fields = ('nombre',)


@admin.register(Habitacion)
class HabitacionAdmin(admin.ModelAdmin):
    list_display = ('numero', 'piso', 'tipo', 'estado', 'activa')
    list_filter = ('piso', 'tipo', 'estado', 'activa')
    search_fields = ('numero',)
    list_select_related = ('tipo',)
    ordering = ('piso', 'numero')


@admin.register(Huesped)
class HuespedAdmin(admin.ModelAdmin):
    list_display = ('numero_documento', 'nombres', 'apellidos', 'telefono', 'es_extranjero')
    search_fields = ('numero_documento', 'nombres', 'apellidos')


@admin.register(Reserva)
class ReservaAdmin(admin.ModelAdmin):
    list_display = ('id', 'usuario', 'habitacion', 'huesped', 'fecha_entrada', 'fecha_salida',
                    'estado', 'canal_reserva', 'fecha_creacion', 'checkin_real', 'checkout_real')
    list_filter = ('estado', 'canal_reserva', 'fecha_entrada')
    search_fields = ('huesped__numero_documento', 'huesped__nombres', 'huesped__apellidos', 'habitacion__numero',
                     'usuario__correo', 'usuario__nombres', 'usuario__apellidos')
    list_select_related = ('habitacion', 'huesped', 'usuario')
    autocomplete_fields = ('habitacion', 'huesped')
    raw_id_fields = ('usuario',)
    readonly_fields = ('checkin_real', 'checkout_real', 'fecha_creacion')
    date_hierarchy = 'fecha_entrada'

@admin.register(Usuario)
class UsuarioAdmin(admin.ModelAdmin):
    # Sin la contraseña en pantalla; se muestra solo lo necesario para localizar cuentas.
    list_display = ('correo', 'nombres', 'apellidos', 'rol', 'activo')
    list_filter = ('rol', 'activo')
    search_fields = ('correo', 'nombres', 'apellidos')
    exclude = ('contrasena',)

@admin.register(OperacionRecepcion)
class OperacionRecepcionAdmin(admin.ModelAdmin):
    """Historial de recepción: solo consulta. Nadie lo agrega, edita ni borra desde el admin."""
    list_display = ('fecha', 'usuario', 'tipo', 'reserva', 'huesped', 'monto', 'descripcion')
    list_filter = ('tipo', 'fecha')
    search_fields = ('descripcion', 'huesped__numero_documento', 'huesped__nombres', 'huesped__apellidos')
    list_select_related = ('usuario', 'reserva', 'huesped')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
