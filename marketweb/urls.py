from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from marketweb import panel_views as pv, views
from marketweb.recepcion.despacho import despachar

urlpatterns = [
    path('', views.home_publico, name='home'),

    path('buscar/', views.buscar_disponibilidad, name='buscar_disponibilidad'),
    path('busqueda/', views.busqueda_preparada, name='busqueda_preparada'),

    path('login/', views.iniciar_sesion, name='login'),
    path('registro/', views.registro, name='registro'),
    path('logout/', views.cerrar_sesion, name='logout'),

    path('cuenta/', views.cuenta_configuracion, name='cuenta'),
    path('mis-reservas/', views.mis_reservas, name='mis_reservas'),
    path('mis-reservas/<int:reserva_id>/', views.reserva_cliente_detalle, name='reserva_cliente_detalle'),
    path('reservar/', views.reservar, name='reservar'),

    # Rutas históricas: el administrador conserva su vista; el recepcionista va a su propio panel.
    path('panel/', despachar(pv.dashboard, 'recepcion:inicio'), name='inicio'),
    path('huespedes/', despachar(pv.huespedes_lista, 'recepcion:huespedes'), name='huespedes'),

    path('disponibilidad/', despachar(pv.habitaciones_lista, 'recepcion:disponibilidad'), name='disponibilidad'),
    path('reserva/crear/', views.crear_reserva, name='crear_reserva'),
    path('reserva/crear/<int:habitacion_id>/', views.crear_reserva, name='crear_reserva'),
    path('reserva/<int:reserva_id>/', views.reserva_detalle, name='reserva_detalle'),
    path('checkin/<int:reserva_id>/', views.registrar_checkin, name='checkin'),
    path('checkout/<int:reserva_id>/', views.registrar_checkout, name='checkout'),
    path('habitacion/<int:habitacion_id>/habilitar/', pv.habilitar, name='habilitar_habitacion'),  
    path('factura/<int:reserva_id>/', views.generar_factura_dian, name='factura_dian'),

    # Panel de gestión (solo ADMIN_GERENTE)
    path('gestion/', pv.dashboard, name='gestion'),
    path('gestion/buscar/', pv.buscar, name='gestion_buscar'),
    path('gestion/reservas/', pv.reservas_lista, name='gestion_reservas'),
    path('gestion/reservas/<int:reserva_id>/', pv.reserva_detalle, name='gestion_reserva_detalle'),
    path('gestion/reservas/<int:reserva_id>/editar/', pv.reserva_editar, name='gestion_reserva_editar'),
    path('gestion/reservas/<int:reserva_id>/confirmar/', pv.reserva_confirmar, name='gestion_reserva_confirmar'),
    path('gestion/reservas/<int:reserva_id>/cancelar/', pv.reserva_cancelar, name='gestion_reserva_cancelar'),
    path('gestion/huespedes/', pv.huespedes_lista, name='gestion_huespedes'),
    path('gestion/huespedes/<int:huesped_id>/', pv.huesped_detalle, name='gestion_huesped_detalle'),
    path('gestion/habitaciones/', pv.habitaciones_lista, name='gestion_habitaciones'),
    path('gestion/habitaciones/nueva/', pv.habitacion_form, name='gestion_habitacion_nueva'),
    path('gestion/habitaciones/<int:habitacion_id>/', pv.habitacion_detalle, name='gestion_habitacion_detalle'),
    path('gestion/habitaciones/<int:habitacion_id>/editar/', pv.habitacion_form, name='gestion_habitacion_editar'),
    path('gestion/habitaciones/<int:habitacion_id>/habilitar/', pv.habilitar, name='gestion_habilitar'),
    path('gestion/tipos/<int:tipo_id>/editar/', pv.tipo_form, name='gestion_tipo_editar'),
    path('gestion/llegadas-salidas/', pv.llegadas_salidas, name='gestion_llegadas'),
    path('gestion/servicios/', pv.servicios_limpieza, name='gestion_servicios'),
    path('gestion/finanzas/', pv.finanzas, name='gestion_finanzas'),
    path('gestion/finanzas/exportar/', pv.finanzas_exportar, name='gestion_finanzas_exportar'),
    path('gestion/usuarios/', pv.usuarios, name='gestion_usuarios'),
    path('gestion/usuarios/<int:usuario_id>/alternar/', pv.usuario_alternar, name='gestion_usuario_alternar'),

    path('admin/', admin.site.urls),

    # Panel de recepción (RECEPCIONISTA y ADMIN_GERENTE)
    path('recepcion/', include('marketweb.recepcion.urls')),

    # W Bar Online (app Wbar)
    path('wbar/', include('Wbar.urls')),
]

# Fotos subidas (MEDIA): solo se sirven así en desarrollo (DEBUG). En producción las sirve el servidor web.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
