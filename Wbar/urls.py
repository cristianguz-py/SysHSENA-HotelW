from django.urls import path

from . import views

app_name = 'wbar'

urlpatterns = [
    # Huésped
    path('', views.menu, name='menu'),
    path('carrito/', views.carrito, name='carrito'),
    path('carrito/agregar/', views.carrito_agregar, name='carrito_agregar'),
    path('carrito/actualizar/', views.carrito_actualizar, name='carrito_actualizar'),
    path('carrito/vaciar/', views.carrito_vaciar, name='carrito_vaciar'),
    path('confirmar/', views.confirmar, name='confirmar'),
    path('pedidos/', views.mis_pedidos, name='mis_pedidos'),
    path('pedidos/<int:pedido_id>/', views.pedido_detalle, name='pedido_detalle'),
    path('pedidos/<int:pedido_id>/cancelar/', views.pedido_cancelar, name='pedido_cancelar'),

    # Personal
    path('gestion/', views.gestion_pedidos, name='gestion'),
    path('gestion/pedidos/<int:pedido_id>/', views.gestion_pedido, name='gestion_pedido'),
    path('gestion/pedidos/<int:pedido_id>/estado/', views.gestion_estado, name='gestion_estado'),
    path('gestion/pedidos/<int:pedido_id>/entrega/', views.gestion_entrega, name='gestion_entrega'),
    path('gestion/pedidos/<int:pedido_id>/cancelar/', views.gestion_cancelar, name='gestion_cancelar'),

    # Solo administrador
    path('gestion/categorias/', views.gestion_categorias, name='gestion_categorias'),
    path('gestion/categorias/nueva/', views.gestion_categoria_form, name='gestion_categoria_nueva'),
    path('gestion/categorias/<int:categoria_id>/editar/', views.gestion_categoria_form, name='gestion_categoria_editar'),
    path('gestion/productos/', views.gestion_productos, name='gestion_productos'),
    path('gestion/productos/nuevo/', views.gestion_producto_form, name='gestion_producto_nuevo'),
    path('gestion/productos/<int:producto_id>/editar/', views.gestion_producto_form, name='gestion_producto_editar'),
    path('gestion/productos/<int:producto_id>/alternar/', views.gestion_producto_alternar, name='gestion_producto_alternar'),
    path('gestion/estadisticas/', views.gestion_estadisticas, name='gestion_estadisticas'),
]
