from django.contrib import admin

from .models import CategoriaProducto, DetallePedido, EventoPedido, Pedido, PagoPedido, Producto


@admin.register(CategoriaProducto)
class CategoriaAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'orden', 'activa')
    list_editable = ('orden', 'activa')
    search_fields = ('nombre',)


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'categoria', 'precio', 'contiene_alcohol', 'activo', 'disponible', 'stock')
    list_filter = ('categoria', 'contiene_alcohol', 'activo', 'disponible')
    search_fields = ('nombre',)
    list_select_related = ('categoria',)
    readonly_fields = ('creado', 'actualizado')

    def has_delete_permission(self, request, obj=None):
        return False   # los productos se desactivan; el historial de pedidos debe conservarse


class _SoloLectura(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class DetalleInline(admin.TabularInline):
    model = DetallePedido
    extra = 0
    can_delete = False
    readonly_fields = ('nombre_producto', 'precio_unitario', 'cantidad', 'subtotal', 'servicio_reserva')
    exclude = ('producto',)

    def has_add_permission(self, request, obj=None):
        return False


class EventoInline(admin.TabularInline):
    model = EventoPedido
    extra = 0
    can_delete = False
    readonly_fields = ('estado_anterior', 'estado_nuevo', 'usuario', 'rol', 'nota', 'fecha')

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Pedido)
class PedidoAdmin(_SoloLectura):
    """Consulta de auditoría. Los cambios de estado, entregas y cobros se hacen desde W Bar > Gestión."""
    list_display = ('id', 'habitacion', 'usuario', 'estado', 'estado_pago', 'total', 'contiene_alcohol', 'creado')
    list_filter = ('estado', 'estado_pago', 'contiene_alcohol')
    search_fields = ('habitacion__numero', 'usuario__correo')
    exclude = ('codigo_semilla', 'token_envio')
    inlines = [DetalleInline, EventoInline]


@admin.register(PagoPedido)
class PagoPedidoAdmin(_SoloLectura):
    list_display = ('pedido', 'metodo', 'monto', 'registrado_por', 'fecha')
