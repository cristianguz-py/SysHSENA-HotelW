"""
Pruebas de W Bar Online.   Ejecutar:  python manage.py test Wbar
(sin MySQL:  python manage.py test Wbar --settings=marketweb.test_settings)
Usan una base de datos de prueba aparte: nunca tocan los datos reales.
"""
import io
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from marketweb import services as hotel
from marketweb.models import Habitacion, Hotel, Huesped, Pago, Reserva, ServicioReserva, TipoHabitacion, Usuario
from marketweb.services import ReglaDeNegocioError

from . import carrito as carro
from . import services
from .models import CategoriaProducto, DetallePedido, Pedido, PagoPedido, Producto

MEDIA_TMP = tempfile.mkdtemp(prefix='wbar_test_media_')


def _usuario(correo, rol='CLIENTE', nombres='Ana'):
    return Usuario.objects.create(nombres=nombres, apellidos='Prueba', correo=correo,
                                  contrasena=make_password('Clave-segura-123'), rol=rol)


def _entrar(correo):
    c = Client()
    assert c.post(reverse('login'), {'identificador': correo, 'contrasena': 'Clave-segura-123'}).status_code == 302
    return c


@override_settings(MEDIA_ROOT=MEDIA_TMP)
class BaseWbar(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_TMP, ignore_errors=True)

    def setUp(self):
        hoy = timezone.localdate()
        self.hotel = Hotel.objects.create()
        tipo = TipoHabitacion.objects.create(nombre='W Comfort', capacidad=3, precio_noche=Decimal('300000'))
        self.h101 = Habitacion.objects.create(numero='101', piso=1, tipo=tipo, hotel=self.hotel)
        self.h102 = Habitacion.objects.create(numero='102', piso=1, tipo=tipo, hotel=self.hotel)
        self.h103 = Habitacion.objects.create(numero='103', piso=1, tipo=tipo, hotel=self.hotel)

        self.ana = _usuario('ana@correo.com')
        self.beto = _usuario('beto@correo.com', nombres='Beto')
        self.admin = _usuario('admin@hotelw.com', 'ADMIN_GERENTE', 'Admin')
        self.recep = _usuario('recep@hotelw.com', 'RECEPCIONISTA', 'Recep')

        def reserva(usuario, hab, doc, hacer_checkin=True):
            r = hotel.crear_reserva(habitacion_id=hab.pk, datos_huesped={
                'tipo_documento': 'CC', 'numero_documento': doc, 'nombres': usuario.nombres, 'apellidos': 'P',
                'telefono': '3000000000', 'correo': usuario.correo, 'nacionalidad': 'Colombiana', 'es_extranjero': False},
                entrada=hoy, salida=hoy + timedelta(days=3), num_huespedes=1, usuario=usuario)
            if hacer_checkin:
                hotel.registrar_checkin(r.pk)
            return Reserva.objects.get(pk=r.pk)
        self.res_ana = reserva(self.ana, self.h101, '111')
        self.res_beto = reserva(self.beto, self.h102, '222')
        self.res_ana_sin_checkin = reserva(self.ana, self.h103, '333', hacer_checkin=False)

        self.cat = CategoriaProducto.objects.create(nombre='Cervezas', orden=1)
        self.cat_sin = CategoriaProducto.objects.create(nombre='Snacks', orden=2)
        self.cerveza = Producto.objects.create(categoria=self.cat, nombre='Cerveza Club', precio=Decimal('9000'), contiene_alcohol=True)
        self.agua = Producto.objects.create(categoria=self.cat_sin, nombre='Agua', precio=Decimal('4000'))

    def pedido(self, usuario=None, reserva=None, items=None, decl=True):
        return services.crear_pedido(
            usuario=usuario or self.ana, reserva_id=(reserva or self.res_ana).pk,
            lineas_carrito=items or {self.agua.pk: 2}, declaracion_mayoria_edad=decl, notas='', token=services.nuevo_token())[0]

    def llevar_a_camino(self, p):
        for e in ('ACEPTADO', 'EN_PREPARACION', 'LISTO', 'EN_CAMINO'):
            services.cambiar_estado(p.pk, e, self.recep)
        return Pedido.objects.get(pk=p.pk)


class PedidoYHabitacionTests(BaseWbar):
    def test_pedido_en_habitacion_autorizada(self):
        p = self.pedido(items={self.agua.pk: 2})
        self.assertEqual(p.total, Decimal('8000.00'))
        self.assertEqual(p.habitacion, self.h101)
        self.assertEqual(p.estado, 'PENDIENTE')
        self.assertEqual(p.eventos.count(), 1)

    def test_no_se_puede_pedir_a_habitacion_ajena(self):
        with self.assertRaises(ReglaDeNegocioError):
            self.pedido(usuario=self.ana, reserva=self.res_beto)
        self.assertEqual(Pedido.objects.count(), 0)

    def test_no_se_puede_pedir_sin_checkin(self):
        with self.assertRaises(ReglaDeNegocioError):
            self.pedido(reserva=self.res_ana_sin_checkin)

    def test_no_se_puede_pedir_tras_checkout(self):
        hotel.registrar_checkout(self.res_ana.pk)
        with self.assertRaises(ReglaDeNegocioError):
            self.pedido()

    def test_lista_de_habitaciones_solo_las_propias(self):
        ids = set(services.reservas_habilitadas(self.ana).values_list('pk', flat=True))
        self.assertEqual(ids, {self.res_ana.pk})

    def test_formulario_rechaza_reserva_ajena_por_post(self):
        c = _entrar('ana@correo.com')
        c.post(reverse('wbar:carrito_agregar'), {'producto': self.agua.pk, 'cantidad': 1})
        c.get(reverse('wbar:confirmar'))
        token = c.session.get('x') or services.nuevo_token()
        r = c.post(reverse('wbar:confirmar'), {'reserva': self.res_beto.pk, 'token': token})
        self.assertEqual(r.status_code, 200)          # vuelve al formulario con error
        self.assertEqual(Pedido.objects.count(), 0)

    def test_precio_manipulado_se_ignora(self):
        c = _entrar('ana@correo.com')
        c.post(reverse('wbar:carrito_agregar'), {'producto': self.agua.pk, 'cantidad': 1, 'precio': '1', 'total': '1'})
        r = c.get(reverse('wbar:confirmar'))
        token = r.context['form'].initial['token']
        c.post(reverse('wbar:confirmar'), {'reserva': self.res_ana.pk, 'token': token, 'precio': '1', 'total': '1', 'estado': 'ENTREGADO'})
        p = Pedido.objects.get()
        self.assertEqual(p.total, Decimal('4000.00'))
        self.assertEqual(p.estado, 'PENDIENTE')

    def test_doble_envio_no_duplica_pedido(self):
        c = _entrar('ana@correo.com')
        c.post(reverse('wbar:carrito_agregar'), {'producto': self.agua.pk, 'cantidad': 1})
        token = c.get(reverse('wbar:confirmar')).context['form'].initial['token']
        datos = {'reserva': self.res_ana.pk, 'token': token}
        c.post(reverse('wbar:confirmar'), datos)
        c.post(reverse('wbar:confirmar'), datos)
        self.assertEqual(Pedido.objects.count(), 1)

    def test_alcohol_exige_declaracion(self):
        with self.assertRaises(ReglaDeNegocioError):
            self.pedido(items={self.cerveza.pk: 1}, decl=False)
        self.assertTrue(self.pedido(items={self.cerveza.pk: 1}, decl=True).contiene_alcohol)

    def test_historial_conserva_precio_original(self):
        p = self.pedido()
        self.agua.precio = Decimal('9999')
        self.agua.save()
        d = p.detalles.get()
        self.assertEqual(d.precio_unitario, Decimal('4000.00'))
        self.assertEqual(d.nombre_producto, 'Agua')

    def test_stock_se_descuenta_y_se_devuelve(self):
        self.agua.stock = 5
        self.agua.save()
        p = self.pedido(items={self.agua.pk: 3})
        self.agua.refresh_from_db()
        self.assertEqual(self.agua.stock, 2)
        with self.assertRaises(ReglaDeNegocioError):
            self.pedido(items={self.agua.pk: 3})
        services.cancelar_pedido(p.pk, self.recep, 'prueba')
        self.agua.refresh_from_db()
        self.assertEqual(self.agua.stock, 5)

    def test_producto_inactivo_no_se_pide(self):
        self.agua.activo = False
        self.agua.save()
        with self.assertRaises(ReglaDeNegocioError):
            self.pedido()


class AccesoTests(BaseWbar):
    def test_cliente_no_ve_pedido_ajeno(self):
        p = self.pedido(usuario=self.beto, reserva=self.res_beto)
        c = _entrar('ana@correo.com')
        self.assertEqual(c.get(reverse('wbar:pedido_detalle', args=[p.pk])).status_code, 404)
        self.assertEqual(c.post(reverse('wbar:pedido_cancelar', args=[p.pk])).status_code, 404)
        p.refresh_from_db()
        self.assertEqual(p.estado, 'PENDIENTE')

    def test_cliente_ve_su_pedido_y_su_codigo(self):
        p = self.pedido()
        r = _entrar('ana@correo.com').get(reverse('wbar:pedido_detalle', args=[p.pk]))
        self.assertContains(r, p.codigo_entrega())

    def test_cliente_no_entra_a_gestion(self):
        c = _entrar('ana@correo.com')
        for nombre in ('wbar:gestion', 'wbar:gestion_productos', 'wbar:gestion_categorias', 'wbar:gestion_estadisticas'):
            self.assertEqual(c.get(reverse(nombre)).status_code, 403, nombre)
        p = self.pedido()
        for nombre in ('wbar:gestion_estado', 'wbar:gestion_entrega', 'wbar:gestion_cancelar'):
            self.assertEqual(c.post(reverse(nombre, args=[p.pk]), {'estado': 'ENTREGADO'}).status_code, 403)
        p.refresh_from_db()
        self.assertEqual(p.estado, 'PENDIENTE')

    def test_sin_sesion_redirige_a_login(self):
        self.assertEqual(Client().get(reverse('wbar:carrito')).status_code, 302)
        self.assertEqual(Client().get(reverse('wbar:gestion')).status_code, 302)

    def test_recepcionista_gestiona_pedidos_pero_no_catalogo(self):
        c = _entrar('recep@hotelw.com')
        self.assertEqual(c.get(reverse('wbar:gestion')).status_code, 200)
        for nombre in ('wbar:gestion_productos', 'wbar:gestion_categorias', 'wbar:gestion_estadisticas',
                       'wbar:gestion_producto_nuevo', 'wbar:gestion_categoria_nueva'):
            self.assertEqual(c.get(reverse(nombre)).status_code, 403, nombre)
        self.assertEqual(c.post(reverse('wbar:gestion_producto_alternar', args=[self.agua.pk]), {'campo': 'activo'}).status_code, 403)

    def test_administrador_accede_a_todo(self):
        c = _entrar('admin@hotelw.com')
        for nombre in ('wbar:gestion', 'wbar:gestion_productos', 'wbar:gestion_categorias', 'wbar:gestion_estadisticas'):
            self.assertEqual(c.get(reverse(nombre)).status_code, 200, nombre)

    def test_cuenta_desactivada_pierde_acceso(self):
        c = _entrar('recep@hotelw.com')
        Usuario.objects.filter(pk=self.recep.pk).update(activo=False)
        self.assertEqual(c.get(reverse('wbar:gestion')).status_code, 302)

    def test_menu_publico_y_pedir_requiere_cliente(self):
        self.assertEqual(Client().get(reverse('wbar:menu')).status_code, 200)
        r = Client().post(reverse('wbar:carrito_agregar'), {'producto': self.agua.pk})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Pedido.objects.count(), 0)

    def test_get_no_cambia_nada(self):
        p = self.pedido()
        c = _entrar('recep@hotelw.com')
        self.assertEqual(c.get(reverse('wbar:gestion_estado', args=[p.pk])).status_code, 405)


class FlujoEstadosTests(BaseWbar):
    def test_flujo_completo_y_saltos_invalidos(self):
        p = self.pedido()
        with self.assertRaises(ReglaDeNegocioError):
            services.cambiar_estado(p.pk, 'LISTO', self.recep)          # salto
        with self.assertRaises(ReglaDeNegocioError):
            services.cambiar_estado(p.pk, 'ENTREGADO', self.recep)      # solo con código
        p = self.llevar_a_camino(p)
        self.assertEqual(p.estado, 'EN_CAMINO')
        self.assertEqual(p.atendido_por, self.recep)

    def test_cliente_solo_cancela_pendiente(self):
        p = self.pedido()
        services.cancelar_pedido(p.pk, self.ana, como_cliente=True)
        self.assertEqual(Pedido.objects.get(pk=p.pk).estado, 'CANCELADO')
        p2 = self.pedido()
        services.cambiar_estado(p2.pk, 'ACEPTADO', self.recep)
        with self.assertRaises(ReglaDeNegocioError):
            services.cancelar_pedido(p2.pk, self.ana, como_cliente=True)

    def test_cliente_no_puede_cambiar_estado_por_post(self):
        p = self.pedido()
        c = _entrar('ana@correo.com')
        c.post(reverse('wbar:gestion_estado', args=[p.pk]), {'estado': 'ACEPTADO'})
        self.assertEqual(Pedido.objects.get(pk=p.pk).estado, 'PENDIENTE')


class EntregaYPagoTests(BaseWbar):
    def entregar(self, p, **kw):
        datos = dict(codigo=p.codigo_entrega(), forma_cobro='COBRADO', metodo_pago='EFECTIVO')
        datos.update(kw)
        return services.confirmar_entrega(p.pk, self.recep, **datos)

    def test_entrega_con_pago_en_habitacion(self):
        p = self.llevar_a_camino(self.pedido())
        self.entregar(p)
        p.refresh_from_db()
        self.assertEqual(p.estado, 'ENTREGADO')
        self.assertEqual(p.estado_pago, 'COBRADO')
        self.assertEqual(p.entregado_por, self.recep)
        pago = p.pago_registrado
        self.assertEqual(pago.pago.monto, p.total)
        self.assertEqual(pago.registrado_por, self.recep)
        cuenta = hotel.calcular_cuenta(Reserva.objects.get(pk=self.res_ana.pk))
        self.assertEqual(cuenta['total_servicios'], p.total)
        self.assertEqual(cuenta['total_pagos'], p.total)
        base = cuenta['total_general'] - cuenta['total_servicios']
        self.assertEqual(cuenta['saldo_pendiente'], base)            # el bar quedó saldado, sin doble cargo

    def test_cargo_a_cuenta_no_crea_pago(self):
        p = self.llevar_a_camino(self.pedido())
        self.entregar(p, forma_cobro='CARGO_CUENTA', metodo_pago='')
        p.refresh_from_db()
        self.assertEqual(p.estado_pago, 'CARGO_CUENTA')
        self.assertFalse(Pago.objects.exists())
        cuenta = hotel.calcular_cuenta(Reserva.objects.get(pk=self.res_ana.pk))
        self.assertEqual(cuenta['total_servicios'], p.total)
        self.assertEqual(cuenta['total_pagos'], Decimal('0'))

    def test_no_se_marca_pagado_sin_decision_de_cobro(self):
        p = self.llevar_a_camino(self.pedido())
        with self.assertRaises(ReglaDeNegocioError):
            self.entregar(p, forma_cobro='', metodo_pago='')
        with self.assertRaises(ReglaDeNegocioError):
            self.entregar(p, metodo_pago='LINK_PAGO')               # pagos en línea no existen
        p.refresh_from_db()
        self.assertEqual(p.estado, 'EN_CAMINO')
        self.assertFalse(Pago.objects.exists() or ServicioReserva.objects.exists())

    def test_codigo_incorrecto_y_bloqueo(self):
        p = self.llevar_a_camino(self.pedido())
        for _ in range(Pedido.MAX_INTENTOS_CODIGO):
            with self.assertRaises(ReglaDeNegocioError):
                self.entregar(p, codigo='000000' if p.codigo_entrega() != '000000' else '000001')
        p.refresh_from_db()
        self.assertEqual(p.intentos_codigo_fallidos, Pedido.MAX_INTENTOS_CODIGO)   # el conteo SÍ se guardó
        with self.assertRaises(ReglaDeNegocioError):
            self.entregar(p)                                         # ni con el código correcto
        self.assertEqual(Pedido.objects.get(pk=p.pk).estado, 'EN_CAMINO')

    def test_codigo_no_se_reutiliza(self):
        p = self.llevar_a_camino(self.pedido())
        self.entregar(p)
        with self.assertRaises(ReglaDeNegocioError):
            self.entregar(p)
        self.assertEqual(Pago.objects.count(), 1)
        self.assertEqual(ServicioReserva.objects.count(), 1)

    def test_codigo_de_otro_pedido_no_sirve(self):
        p1 = self.llevar_a_camino(self.pedido())
        p2 = self.llevar_a_camino(self.pedido())
        self.assertNotEqual(p1.codigo_entrega(), p2.codigo_entrega())
        with self.assertRaises(ReglaDeNegocioError):
            self.entregar(p1, codigo=p2.codigo_entrega())

    def test_codigo_no_esta_guardado_en_claro(self):
        p = self.pedido()
        valores = [str(v) for v in Pedido.objects.filter(pk=p.pk).values()[0].values()]
        self.assertNotIn(p.codigo_entrega(), valores)

    def test_alcohol_exige_verificar_edad(self):
        p = self.llevar_a_camino(self.pedido(items={self.cerveza.pk: 2}))
        with self.assertRaises(ReglaDeNegocioError):
            self.entregar(p)
        with self.assertRaises(ReglaDeNegocioError):
            self.entregar(p, edad_verificada=True, documento='')
        self.entregar(p, edad_verificada=True, documento='CC')
        p.refresh_from_db()
        self.assertTrue(p.edad_verificada)
        self.assertEqual(p.edad_verificada_por, self.recep)
        self.assertEqual(p.documento_verificado, 'CC')

    def test_entrega_solo_si_en_camino(self):
        p = self.pedido()
        with self.assertRaises(ReglaDeNegocioError):
            self.entregar(p)

    def test_no_entrega_si_la_estadia_termino(self):
        p = self.llevar_a_camino(self.pedido())
        hotel.registrar_checkout(self.res_ana.pk)
        with self.assertRaises(ReglaDeNegocioError):
            self.entregar(p)
        self.assertFalse(ServicioReserva.objects.filter(detalle_wbar__isnull=False).exists())

    def test_entrega_por_http_end_to_end(self):
        p = self.llevar_a_camino(self.pedido())
        c = _entrar('recep@hotelw.com')
        r = c.post(reverse('wbar:gestion_entrega', args=[p.pk]), {
            'codigo': p.codigo_entrega(), 'forma_cobro': 'COBRADO', 'metodo_pago': 'DATAFONO', 'referencia': 'A123'})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual((p.estado, p.estado_pago), ('ENTREGADO', 'COBRADO'))
        self.assertEqual(p.pago_registrado.referencia, 'A123')

    def test_checkout_incluye_consumo_del_bar(self):
        p = self.llevar_a_camino(self.pedido())
        self.entregar(p, forma_cobro='CARGO_CUENTA', metodo_pago='')
        r = hotel.registrar_checkout(self.res_ana.pk)
        esperado = r.calcular_alojamiento() + p.total + r.cargo_tardio
        self.assertEqual(r.valor_total, esperado)


class CatalogoTests(BaseWbar):
    def _png(self, nombre='x.png', relleno=0):
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (10, 10), (200, 180, 150)).save(buf, 'PNG')
        return SimpleUploadedFile(nombre, buf.getvalue() + b'\0' * relleno, content_type='image/png')

    def datos(self, **extra):
        d = {'categoria': self.cat.pk, 'nombre': 'Whisky 12', 'precio': '250000', 'descripcion': '', 'presentacion': '',
             'informacion': '', 'activo': 'on', 'disponible': 'on'}
        d.update(extra)
        return d

    def test_admin_crea_producto_con_foto(self):
        c = _entrar('admin@hotelw.com')
        r = c.post(reverse('wbar:gestion_producto_nuevo'), {**self.datos(), 'imagen': self._png()})
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['form'].errors)
        p = Producto.objects.get(nombre='Whisky 12')
        self.assertTrue(p.imagen.name.startswith('wbar/productos/'))
        self.assertContains(c.get(reverse('wbar:menu')), p.imagen.url)

    def test_rechaza_archivo_no_imagen_y_extension(self):
        c = _entrar('admin@hotelw.com')
        falso = SimpleUploadedFile('virus.png', b'esto no es una imagen', content_type='image/png')
        self.assertEqual(c.post(reverse('wbar:gestion_producto_nuevo'), {**self.datos(), 'imagen': falso}).status_code, 200)
        gif = SimpleUploadedFile('a.gif', self._png().read(), content_type='image/gif')
        self.assertEqual(c.post(reverse('wbar:gestion_producto_nuevo'), {**self.datos(), 'imagen': gif}).status_code, 200)
        self.assertFalse(Producto.objects.filter(nombre='Whisky 12').exists())

    def test_rechaza_imagen_muy_pesada(self):
        c = _entrar('admin@hotelw.com')
        grande = self._png(relleno=3 * 1024 * 1024 + 10)
        self.assertEqual(c.post(reverse('wbar:gestion_producto_nuevo'), {**self.datos(), 'imagen': grande}).status_code, 200)
        self.assertFalse(Producto.objects.filter(nombre='Whisky 12').exists())

    def test_precio_invalido(self):
        c = _entrar('admin@hotelw.com')
        for malo in ('0', '-5', 'abc'):
            self.assertEqual(c.post(reverse('wbar:gestion_producto_nuevo'), self.datos(precio=malo)).status_code, 200)
        self.assertFalse(Producto.objects.filter(nombre='Whisky 12').exists())

    def test_producto_sin_foto_se_muestra(self):
        self.assertContains(Client().get(reverse('wbar:menu')), 'Agua')

    def test_desactivar_oculta_del_menu_y_conserva_historial(self):
        p = self.pedido()
        c = _entrar('admin@hotelw.com')
        c.post(reverse('wbar:gestion_producto_alternar', args=[self.agua.pk]), {'campo': 'activo'})
        self.assertNotContains(Client().get(reverse('wbar:menu')), 'Agua')
        self.assertEqual(p.detalles.get().nombre_producto, 'Agua')

    def test_categoria_inactiva_oculta_productos(self):
        self.cat_sin.activa = False
        self.cat_sin.save()
        self.assertNotContains(Client().get(reverse('wbar:menu')), 'Agua')

    def test_categorias_administrables(self):
        c = _entrar('admin@hotelw.com')
        r = c.post(reverse('wbar:gestion_categoria_nueva'), {'nombre': 'Mocktails', 'orden': 5, 'activa': 'on'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(CategoriaProducto.objects.filter(nombre='Mocktails', slug='mocktails').exists())

    def test_aviso_de_alcohol_visible(self):
        self.assertContains(Client().get(reverse('wbar:menu')), 'El exceso de alcohol es perjudicial para la salud')

    def test_comando_categorias_idempotente(self):
        from django.core.management import call_command
        call_command('cargar_categorias_wbar', verbosity=0)
        n = CategoriaProducto.objects.count()
        call_command('cargar_categorias_wbar', verbosity=0)
        self.assertEqual(CategoriaProducto.objects.count(), n)


class CarritoTests(BaseWbar):
    def test_agregar_cambiar_quitar(self):
        c = _entrar('ana@correo.com')
        c.post(reverse('wbar:carrito_agregar'), {'producto': self.agua.pk, 'cantidad': 2})
        c.post(reverse('wbar:carrito_agregar'), {'producto': self.cerveza.pk, 'cantidad': 1})
        r = c.get(reverse('wbar:carrito'))
        self.assertEqual(r.context['total'], Decimal('17000'))
        c.post(reverse('wbar:carrito_actualizar'), {'producto': self.agua.pk, 'cantidad': 5})
        self.assertEqual(c.get(reverse('wbar:carrito')).context['total'], Decimal('29000'))
        c.post(reverse('wbar:carrito_actualizar'), {'producto': self.cerveza.pk, 'accion': 'quitar'})
        self.assertEqual(c.get(reverse('wbar:carrito')).context['total'], Decimal('20000'))

    def test_cantidades_invalidas(self):
        c = _entrar('ana@correo.com')
        for malo in ('0', '-3', '999', 'x'):
            c.post(reverse('wbar:carrito_agregar'), {'producto': self.agua.pk, 'cantidad': malo})
        self.assertEqual(c.get(reverse('wbar:carrito')).context['lineas'], [])
        c.post(reverse('wbar:carrito_agregar'), {'producto': 99999, 'cantidad': 1})
        self.assertEqual(c.get(reverse('wbar:carrito')).context['lineas'], [])

    def test_producto_que_se_agota_se_quita(self):
        c = _entrar('ana@correo.com')
        c.post(reverse('wbar:carrito_agregar'), {'producto': self.agua.pk, 'cantidad': 1})
        self.agua.disponible = False
        self.agua.save()
        self.assertEqual(c.get(reverse('wbar:carrito')).context['lineas'], [])


class PaginasTests(BaseWbar):
    """Todas las páginas se renderizan sin error (revisión de templates y rutas)."""
    def test_paginas_del_huesped(self):
        p = self.llevar_a_camino(self.pedido(items={self.cerveza.pk: 1}))
        c = _entrar('ana@correo.com')
        c.post(reverse('wbar:carrito_agregar'), {'producto': self.cerveza.pk, 'cantidad': 1})
        for url in (reverse('wbar:menu'), reverse('wbar:carrito'), reverse('wbar:confirmar'),
                    reverse('wbar:mis_pedidos'), reverse('wbar:pedido_detalle', args=[p.pk])):
            self.assertEqual(c.get(url).status_code, 200, url)
        self.assertContains(c.get(reverse('wbar:menu')), 'Se prohíbe la venta de bebidas alcohólicas a menores de edad')

    def test_paginas_del_personal(self):
        p = self.llevar_a_camino(self.pedido(items={self.cerveza.pk: 1}))
        c = _entrar('admin@hotelw.com')
        urls = [reverse('wbar:gestion'), reverse('wbar:gestion') + '?estado=TODOS&q=WB-1', reverse('wbar:gestion_pedido', args=[p.pk]),
                reverse('wbar:gestion_categoria_editar', args=[self.cat.pk]), reverse('wbar:gestion_producto_editar', args=[self.agua.pk]),
                reverse('wbar:gestion_productos') + '?q=agua&categoria=1']
        for url in urls:
            self.assertEqual(c.get(url).status_code, 200, url)
        self.assertContains(c.get(reverse('wbar:gestion_pedido', args=[p.pk])), 'Confirmar entrega')

    def test_enlaces_de_integracion(self):
        self.assertContains(Client().get(reverse('home')), reverse('wbar:menu'))
        self.assertContains(_entrar('admin@hotelw.com').get(reverse('gestion')), reverse('wbar:gestion'))

    def test_admin_de_django_registra_modelos(self):
        from django.contrib import admin
        for m in (CategoriaProducto, Producto, Pedido, PagoPedido):
            self.assertIn(m, admin.site._registry)
