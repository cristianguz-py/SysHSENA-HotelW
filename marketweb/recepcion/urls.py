from django.urls import path

from . import vistas as v

app_name = 'recepcion'

urlpatterns = [
    path('', v.inicio, name='inicio'),

    path('huespedes/', v.huespedes_lista, name='huespedes'),
    path('huespedes/nuevo/', v.huesped_nuevo, name='huesped_nuevo'),
    path('huespedes/<int:huesped_id>/', v.huesped_detalle, name='huesped_detalle'),

    path('disponibilidad/', v.disponibilidad, name='disponibilidad'),
    path('reservas/', v.reservas_lista, name='reservas'),
    path('reservas/nueva/', v.reserva_nueva, name='reserva_nueva'),
    path('reservas/<int:reserva_id>/', v.reserva_detalle, name='reserva_detalle'),
    path('reservas/<int:reserva_id>/editar/', v.reserva_editar, name='reserva_editar'),
    path('reservas/<int:reserva_id>/confirmar/', v.reserva_confirmar, name='reserva_confirmar'),
    path('reservas/<int:reserva_id>/cancelar/', v.reserva_cancelar, name='reserva_cancelar'),

    path('llegadas/', v.llegadas, name='llegadas'),
    path('checkin/<int:reserva_id>/', v.checkin, name='checkin'),
    path('alojados/', v.alojados, name='alojados'),
    path('salidas/', v.salidas, name='salidas'),
    path('checkout/<int:reserva_id>/', v.checkout, name='checkout'),

    path('servicios/', v.servicios_lista, name='servicios'),
    path('servicios/nuevo/', v.servicio_nuevo, name='servicio_nuevo'),
    path('pagos/', v.pagos_lista, name='pagos'),
    path('pagos/nuevo/', v.pago_nuevo, name='pago_nuevo'),

    path('habitaciones/', v.habitaciones, name='habitaciones'),
    path('habitaciones/<int:habitacion_id>/habilitar/', v.habitacion_habilitar, name='habitacion_habilitar'),
    path('historial/', v.historial, name='historial'),
]
