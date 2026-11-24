"""
Pruebas del módulo de recepción (rol RECEPCIONISTA).

Ejecutar:  python manage.py test marketweb Wbar --settings=marketweb.test_settings
El reloj se fija a las 10:00 (hora local) para que el cargo por salida tardía no dependa de la hora real.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.hashers import make_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from . import services
from .models import (Habitacion, Hotel, Huesped, OperacionRecepcion, Pago, Reserva, Servicio,
                     ServicioReserva, TipoHabitacion, Usuario)
from .recepcion import operaciones as op
from .services import ReglaDeNegocioError

CLAVE = 'Clave12345!'


def _huesped(doc='1082300123', **kw):
    d = {'tipo_documento': 'CC', 'numero_documento': doc, 'nombres': 'Juan', 'apellidos': 'Pérez',
         'telefono': '3001234567', 'correo': 'juan@correo.com', 'nacionalidad': 'Colombiana', 'es_extranjero': False}
    d.update(kw)
    return d


class Base(TestCase):
    def setUp(self):
        self.tz = timezone.get_current_timezone()
        self.hoy = timezone.localdate()
        self.reloj = timezone.make_aware(datetime.combine(self.hoy, time(10, 0)), self.tz)
        p = mock.patch('django.utils.timezone.now', return_value=self.reloj)
        p.start()
        self.addCleanup(p.stop)

        self.rec = self.usuario('rec@hotel.co', 'RECEPCIONISTA', 'Rita')
        self.admin = self.usuario('adm@hotel.co', 'ADMIN_GERENTE', 'Ana')
        self.cliente = self.usuario('cli@hotel.co', 'CLIENTE', 'Carlos')
        self.hotel = Hotel.objects.create()
        self.ocean = TipoHabitacion.objects.create(nombre='W Ocean View', capacidad=2, precio_noche=Decimal('560000'))
        self.royal = TipoHabitacion.objects.create(nombre='W Royal Suite', capacidad=6, precio_noche=Decimal('2000000'))
        self.h705 = Habitacion.objects.create(numero='705', piso=7, tipo=self.ocean, hotel=self.hotel)
        self.h706 = Habitacion.objects.create(numero='706', piso=7, tipo=self.ocean, hotel=self.hotel)
        self.h901 = Habitacion.objects.create(numero='901', piso=9, tipo=self.royal, hotel=self.hotel)
        self.minibar = Servicio.objects.create(nombre='Agua', categoria='MINIBAR', precio=Decimal('10000'))

    def usuario(self, correo, rol, nombre='X', activo=True):
        return Usuario.objects.create(nombres=nombre, apellidos='Prueba', correo=correo,
                                      contrasena=make_password(CLAVE), rol=rol, activo=activo)

    def cliente_http(self, usuario):
        c = Client()
        s = c.session
        s['usuario_id'] = usuario.pk
        s['usuario_rol'] = usuario.rol
        s['usuario_nombre'] = usuario.nombres
        s.save()
        return c

    def d(self, dias):
        return self.hoy + timedelta(days=dias)

    def reservar(self, hab=None, entrada=None, salida=None, n=1, doc='1082300123', usuario=None):
        reserva, _ = op.crear_reserva(
            usuario=usuario or self.rec, habitacion_id=(hab or self.h705).pk, datos_huesped=_huesped(doc),
            entrada=entrada or self.hoy, salida=salida or self.d(2), num_huespedes=n)
        return reserva

    def en_casa(self, **kw):
        r = self.reservar(**kw)
        op.hacer_checkin(usuario=self.rec, reserva_id=r.pk)
        return Reserva.objects.get(pk=r.pk)


# ======================================================================================
# PERMISOS
# ======================================================================================
class PermisosTests(Base):
    def rutas(self, reserva, huesped):
        n = 'recepcion:'
        return [reverse(n + 'inicio'), reverse(n + 'huespedes'), reverse(n + 'huesped_nuevo'),
                reverse(n + 'huesped_detalle', args=[huesped.pk]), reverse(n + 'disponibilidad'),
                reverse(n + 'reservas'), reverse(n + 'reserva_nueva') + f'?habitacion={self.h705.pk}',
                reverse(n + 'reserva_detalle', args=[reserva.pk]), reverse(n + 'reserva_editar', args=[reserva.pk]),
                reverse(n + 'reserva_confirmar', args=[reserva.pk]), reverse(n + 'reserva_cancelar', args=[reserva.pk]),
                reverse(n + 'llegadas'), reverse(n + 'checkin', args=[reserva.pk]), reverse(n + 'alojados'),
                reverse(n + 'salidas'), reverse(n + 'checkout', args=[reserva.pk]), reverse(n + 'servicios'),
                reverse(n + 'servicio_nuevo'), reverse(n + 'pagos'), reverse(n + 'pago_nuevo'),
                reverse(n + 'habitaciones'), reverse(n + 'historial')]

    def test_anonimo_va_a_login_en_todas_las_rutas(self):
        r = self.reservar()
        for url in self.rutas(r, r.huesped):
            resp = Client().get(url)
            self.assertRedirects(resp, reverse('login'), fetch_redirect_response=False, msg_prefix=url)

    def test_cliente_recibe_403_en_todas_las_rutas(self):
        r = self.reservar()
        c = self.cliente_http(self.cliente)
        for url in self.rutas(r, r.huesped):
            self.assertEqual(c.get(url).status_code, 403, url)

    def test_cliente_no_puede_hacer_post(self):
        r = self.reservar()
        c = self.cliente_http(self.cliente)
        self.assertEqual(c.post(reverse('recepcion:pago_nuevo'),
                                {'reserva': r.pk, 'monto': '1000', 'metodo_pago': 'EFECTIVO'}).status_code, 403)
        self.assertEqual(Pago.objects.count(), 0)

    def test_recepcionista_y_admin_acceden_a_todas_las_paginas(self):
        r = self.reservar()
        for usuario in (self.rec, self.admin):
            c = self.cliente_http(usuario)
            for url in self.rutas(r, r.huesped):
                self.assertEqual(c.get(url).status_code, 200, f'{usuario.rol} {url}')

    def test_recepcionista_bloqueado_en_urls_administrativas(self):
        c = self.cliente_http(self.rec)
        for url in ['/gestion/', '/gestion/reservas/', '/gestion/usuarios/', '/gestion/finanzas/',
                    '/gestion/finanzas/exportar/', '/gestion/habitaciones/nueva/', '/gestion/servicios/',
                    '/wbar/gestion/categorias/', '/wbar/gestion/productos/', '/wbar/gestion/estadisticas/']:
            self.assertEqual(c.get(url).status_code, 403, url)
        r = c.post(f'/gestion/usuarios/{self.admin.pk}/alternar/')
        self.assertEqual(r.status_code, 403)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.activo)
        self.assertEqual(c.get('/admin/').status_code, 302)        # el admin de Django exige is_staff

    def test_rol_manipulado_en_sesion_no_sirve(self):
        """La sesión dice RECEPCIONISTA pero la BD dice CLIENTE: se cierra la sesión."""
        c = Client()
        s = c.session
        s['usuario_id'] = self.cliente.pk
        s['usuario_rol'] = 'RECEPCIONISTA'
        s.save()
        resp = c.get(reverse('recepcion:inicio'))
        self.assertRedirects(resp, reverse('login'), fetch_redirect_response=False)

    def test_cuenta_desactivada_pierde_acceso(self):
        c = self.cliente_http(self.rec)
        self.assertEqual(c.get(reverse('recepcion:inicio')).status_code, 200)
        self.rec.activo = False
        self.rec.save()
        self.assertRedirects(c.get(reverse('recepcion:inicio')), reverse('login'), fetch_redirect_response=False)

    def test_backend_exige_usuario_autorizado(self):
        r = self.reservar()
        for malo in (None, self.cliente):
            with self.assertRaises(PermissionDenied):
                op.registrar_pago(usuario=malo, reserva_id=r.pk, monto=Decimal('1000'), metodo='EFECTIVO')
            with self.assertRaises(PermissionDenied):
                op.hacer_checkin(usuario=malo, reserva_id=r.pk)
            with self.assertRaises(PermissionDenied):
                op.cancelar_reserva(usuario=malo, reserva_id=r.pk)
            with self.assertRaises(PermissionDenied):
                op.cargar_servicio(usuario=malo, reserva_id=r.pk, servicio_id=self.minibar.pk, cantidad=1)
        inactivo = self.usuario('off@hotel.co', 'RECEPCIONISTA', activo=False)
        with self.assertRaises(PermissionDenied):
            op.alta_huesped(usuario=inactivo, datos=_huesped('555'))
        self.assertEqual(Pago.objects.count(), 0)
        self.assertEqual(OperacionRecepcion.objects.filter(tipo='PAGO').count(), 0)

    def test_login_del_recepcionista_aterriza_en_su_panel(self):
        c = Client()
        r = c.post(reverse('login'), {'identificador': 'rec@hotel.co', 'contrasena': CLAVE})
        self.assertRedirects(r, '/panel/', fetch_redirect_response=False)
        self.assertRedirects(c.get('/panel/'), reverse('recepcion:inicio'), fetch_redirect_response=False)
        self.assertRedirects(c.get('/huespedes/'), reverse('recepcion:huespedes'), fetch_redirect_response=False)
        self.assertRedirects(c.get('/disponibilidad/'), reverse('recepcion:disponibilidad'), fetch_redirect_response=False)
        self.assertEqual(c.get(reverse('recepcion:inicio')).status_code, 200)

    def test_campo_rol_enviado_en_formularios_se_ignora(self):
        c = self.cliente_http(self.rec)
        datos = {**_huesped('777777'), 'rol': 'ADMIN_GERENTE', 'activo': 'on'}
        c.post(reverse('recepcion:huesped_nuevo'), datos)
        self.assertTrue(Huesped.objects.filter(numero_documento='777777').exists())
        self.assertEqual(Usuario.objects.filter(rol='ADMIN_GERENTE').count(), 1)
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.rol, 'RECEPCIONISTA')


# ======================================================================================
# HUÉSPEDES
# ======================================================================================
class HuespedesTests(Base):
    def setUp(self):
        super().setUp()
        self.c = self.cliente_http(self.rec)

    def test_alta_valida_y_bitacora(self):
        r = self.c.post(reverse('recepcion:huesped_nuevo'), _huesped('99887766'))
        h = Huesped.objects.get(numero_documento='99887766')
        self.assertRedirects(r, reverse('recepcion:huesped_detalle', args=[h.pk]))
        o = OperacionRecepcion.objects.get(tipo='HUESPED_ALTA')
        self.assertEqual((o.usuario, o.huesped), (self.rec, h))
        self.assertFalse(Usuario.objects.filter(correo='juan@correo.com').exists())   # no crea cuenta de acceso

    def test_documento_duplicado_se_rechaza(self):
        self.c.post(reverse('recepcion:huesped_nuevo'), _huesped('123456'))
        r = self.c.post(reverse('recepcion:huesped_nuevo'), _huesped('123456', nombres='Otro'))
        self.assertContains(r, 'Ya existe un huésped registrado con este documento')
        self.assertEqual(Huesped.objects.filter(numero_documento='123456').count(), 1)

    def test_datos_invalidos(self):
        for malo in ({'numero_documento': 'a b'}, {'telefono': 'abc'}, {'correo': 'sin-arroba'},
                     {'nombres': 'Juan123'}, {'nombres': ''}):
            self.c.post(reverse('recepcion:huesped_nuevo'), _huesped('424242', **malo))
            self.assertFalse(Huesped.objects.filter(numero_documento='424242').exists(), malo)

    def test_busqueda_por_cada_campo(self):
        Huesped.objects.create(**_huesped('111', nombres='Marta', apellidos='Gómez', telefono='3115550000', correo='marta@x.com'))
        Huesped.objects.create(**_huesped('222', nombres='Pedro', apellidos='Ruiz', telefono='3209990000', correo='pedro@x.com'))
        for q, esperado in [('Marta', 'Marta'), ('Gómez', 'Marta'), ('111', 'Marta'), ('marta@x.com', 'Marta'),
                            ('3115550000', 'Marta'), ('Ruiz', 'Pedro'), ('3209990000', 'Pedro')]:
            r = self.c.get(reverse('recepcion:huespedes'), {'q': q})
            nombres = [h.nombres for h in r.context['pagina']]
            self.assertEqual(nombres, [esperado], q)

    def test_modificar_contacto_y_documento_inmutable(self):
        h = Huesped.objects.create(**_huesped('333'))
        r = self.c.post(reverse('recepcion:huesped_detalle', args=[h.pk]), {
            'nombres': 'Juan', 'apellidos': 'Pérez', 'telefono': '3000000000', 'correo': 'nuevo@x.com',
            'nacionalidad': 'Colombiana', 'numero_documento': '999'})
        h.refresh_from_db()
        self.assertEqual((h.telefono, h.correo, h.numero_documento), ('3000000000', 'nuevo@x.com', '333'))
        self.assertTrue(OperacionRecepcion.objects.filter(tipo='HUESPED_EDICION', huesped=h).exists())

    def test_detalle_muestra_historial_estadias_y_pagos(self):
        r = self.en_casa()
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('100000'), metodo='EFECTIVO')
        resp = self.c.get(reverse('recepcion:huesped_detalle', args=[r.huesped.pk]))
        self.assertEqual(len(resp.context['reservas']), 1)
        self.assertEqual(len(resp.context['estadias']), 1)
        self.assertEqual(len(resp.context['pagos']), 1)


# ======================================================================================
# DISPONIBILIDAD Y RESERVAS
# ======================================================================================
class ReservasTests(Base):
    def setUp(self):
        super().setUp()
        self.c = self.cliente_http(self.rec)

    def _crear_por_vista(self, hab=None, entrada=None, salida=None, n=2, doc='1082300123', **extra):
        hab = hab or self.h705
        get = self.c.get(reverse('recepcion:reserva_nueva'), {'habitacion': hab.pk})
        token = get.context['token']
        datos = {**_huesped(doc), 'es_extranjero': '', 'fecha_entrada': (entrada or self.d(10)).isoformat(),
                 'fecha_salida': (salida or self.d(13)).isoformat(), 'tipo_habitacion': hab.tipo_id,
                 'habitacion': hab.pk, 'num_huespedes': n, 'canal_reserva': 'PRESENCIAL', 'token': token}
        datos.update(extra)
        return self.c.post(reverse('recepcion:reserva_nueva'), datos), token, datos

    def test_flujo_completo_crear_reserva_por_vista(self):
        r, _, _ = self._crear_por_vista()
        reserva = Reserva.objects.get()
        self.assertRedirects(r, reverse('recepcion:reserva_detalle', args=[reserva.pk]))
        self.assertEqual((reserva.estado, reserva.valor_total), ('CONFIRMADA', Decimal('1680000')))
        self.assertIsNone(reserva.usuario)                       # reserva de personal, no de cuenta web
        self.h705.refresh_from_db()
        self.assertEqual(self.h705.estado, Habitacion.DISPONIBLE)
        self.assertTrue(OperacionRecepcion.objects.filter(tipo='RESERVA_CREADA', reserva=reserva, usuario=self.rec).exists())

    def test_precio_y_estado_del_navegador_se_ignoran(self):
        self._crear_por_vista(valor_total='1', estado='CHECKIN', precio_noche='1')
        reserva = Reserva.objects.get()
        self.assertEqual((reserva.estado, reserva.valor_total), ('CONFIRMADA', Decimal('1680000')))

    def test_reenvio_duplicado_no_crea_segunda_reserva(self):
        _, token, datos = self._crear_por_vista()
        self.c.post(reverse('recepcion:reserva_nueva'), datos)
        self.assertEqual(Reserva.objects.count(), 1)

    def test_rn01_conflicto_de_fechas(self):
        self._crear_por_vista()
        for e, s in [(self.d(12), self.d(14)), (self.d(8), self.d(11)), (self.d(10), self.d(13))]:
            r, _, _ = self._crear_por_vista(entrada=e, salida=s, doc='999', n=1)
            self.assertContains(r, 'ya tiene la reserva')
        self.assertEqual(Reserva.objects.count(), 1)
        self._crear_por_vista(entrada=self.d(13), salida=self.d(15), doc='999', n=1)     # el día de salida queda libre
        self.assertEqual(Reserva.objects.count(), 2)

    def test_rn02_capacidad(self):
        r, _, _ = self._crear_por_vista(n=5)
        self.assertContains(r, 'admite máximo 2 huéspedes')
        self.assertEqual(Reserva.objects.count(), 0)

    def test_rn03_fechas_invalidas(self):
        for e, s in [(self.d(5), self.d(5)), (self.d(5), self.d(3)), (self.d(-2), self.d(1))]:
            self._crear_por_vista(entrada=e, salida=s)
        self.assertEqual(Reserva.objects.count(), 0)

    def test_rn04_rn06_reserva_incompleta_o_invalida(self):
        r, _, _ = self._crear_por_vista(numero_documento='')
        self.assertEqual(r.status_code, 200)
        self._crear_por_vista(habitacion='999999')
        self._crear_por_vista(canal_reserva='XX')
        self.assertEqual(Reserva.objects.count(), 0)
        self.assertFalse(Huesped.objects.exists())

    def test_habitacion_fuera_de_servicio_o_mantenimiento(self):
        for estado in (Habitacion.FUERA_DE_SERVICIO, Habitacion.MANTENIMIENTO):
            self.h705.estado = estado
            self.h705.save()
            self._crear_por_vista()
        self.assertEqual(Reserva.objects.count(), 0)

    def test_huesped_existente_se_reutiliza_sin_sobrescribirlo(self):
        h = Huesped.objects.create(**_huesped('5555', nombres='Original', telefono='3110000000'))
        r, _, _ = self._crear_por_vista(doc='5555', nombres='Cambiado', telefono='3999999999')
        h.refresh_from_db()
        self.assertEqual((h.nombres, h.telefono), ('Original', '3110000000'))
        self.assertEqual(Huesped.objects.filter(numero_documento='5555').count(), 1)
        self.assertEqual(Reserva.objects.get().huesped, h)

    def test_nueva_sin_habitacion_redirige_a_disponibilidad(self):
        self.assertRedirects(self.c.get(reverse('recepcion:reserva_nueva')), reverse('recepcion:disponibilidad'))

    def test_prellenado_desde_huesped(self):
        h = Huesped.objects.create(**_huesped('6666', nombres='Lucía'))
        r = self.c.get(reverse('recepcion:reserva_nueva'), {'habitacion': self.h705.pk, 'huesped': h.pk})
        self.assertEqual(r.context['form'].initial['nombres'], 'Lucía')

    def test_disponibilidad_excluye_cruces_y_respeta_capacidad(self):
        self.reservar(self.h705, self.d(10), self.d(15))
        def libres(e, s, n=1, tipo=''):
            r = self.c.get(reverse('recepcion:disponibilidad'), {'entrada': e.isoformat(), 'salida': s.isoformat(),
                                                                'huespedes': n, 'tipo': tipo})
            return {h.numero for h in r.context['habitaciones']}
        self.assertEqual(libres(self.d(12), self.d(14), tipo=self.ocean.pk), {'706'})
        self.assertEqual(libres(self.d(15), self.d(18), tipo=self.ocean.pk), {'705', '706'})   # salida libre
        self.assertEqual(libres(self.d(12), self.d(14), n=4), {'901'})                          # capacidad
        self.assertEqual(libres(self.d(12), self.d(14)), {'706', '901'})

    def test_disponibilidad_muestra_datos_de_la_habitacion(self):
        r = self.c.get(reverse('recepcion:disponibilidad'), {'entrada': self.d(5).isoformat(), 'salida': self.d(7).isoformat(), 'huespedes': 1})
        self.assertContains(r, '705')
        self.assertContains(r, 'W Ocean View')
        self.assertContains(r, '$560.000')
        self.assertContains(r, '$1.120.000')

    def test_disponibilidad_fechas_invalidas(self):
        r = self.c.get(reverse('recepcion:disponibilidad'), {'entrada': self.d(5).isoformat(), 'salida': self.d(3).isoformat()})
        self.assertEqual(list(r.context['habitaciones']), [])
        self.assertContains(r, 'posterior')

    def test_cancelada_libera_disponibilidad_rn05(self):
        r = self.reservar(self.h705, self.d(10), self.d(12))
        op.cancelar_reserva(usuario=self.rec, reserva_id=r.pk)
        self.reservar(self.h705, self.d(10), self.d(12), doc='222')       # ya no choca
        self.assertEqual(Reserva.objects.filter(estado='CONFIRMADA').count(), 1)

    def test_rn07_modificar_revalida_disponibilidad(self):
        a = self.reservar(self.h705, self.d(10), self.d(12))
        self.reservar(self.h706, self.d(10), self.d(12), doc='222')
        r = self.c.post(reverse('recepcion:reserva_editar', args=[a.pk]), {
            'habitacion': self.h706.pk, 'fecha_entrada': self.d(10).isoformat(), 'fecha_salida': self.d(12).isoformat(),
            'num_huespedes': 1, 'canal_reserva': 'DIRECTA'})
        self.assertEqual(r.status_code, 200)
        a.refresh_from_db()
        self.assertEqual(a.habitacion, self.h705)
        r = self.c.post(reverse('recepcion:reserva_editar', args=[a.pk]), {
            'habitacion': self.h705.pk, 'fecha_entrada': self.d(10).isoformat(), 'fecha_salida': self.d(14).isoformat(),
            'num_huespedes': 2, 'canal_reserva': 'DIRECTA'})
        self.assertRedirects(r, reverse('recepcion:reserva_detalle', args=[a.pk]))
        a.refresh_from_db()
        self.assertEqual((a.fecha_salida, a.valor_total, a.canal_reserva), (self.d(14), Decimal('2240000'), 'DIRECTA'))
        self.assertTrue(OperacionRecepcion.objects.filter(tipo='RESERVA_MODIFICADA', reserva=a).exists())

    def test_no_se_edita_reserva_con_checkin(self):
        r = self.en_casa()
        resp = self.c.get(reverse('recepcion:reserva_editar', args=[r.pk]))
        self.assertRedirects(resp, reverse('recepcion:reserva_detalle', args=[r.pk]))

    def test_cancelacion_por_vista(self):
        r = self.reservar(self.h705, self.d(10), self.d(12))
        self.assertEqual(self.c.get(reverse('recepcion:reserva_cancelar', args=[r.pk])).status_code, 200)
        self.c.post(reverse('recepcion:reserva_cancelar', args=[r.pk]))
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CANCELADA')
        self.assertTrue(OperacionRecepcion.objects.filter(tipo='RESERVA_CANCELADA', reserva=r).exists())

    def test_cancelar_con_pagos_solo_admin(self):
        r = self.reservar(self.h705, self.d(10), self.d(12))
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('100000'), metodo='EFECTIVO')
        self.c.post(reverse('recepcion:reserva_cancelar', args=[r.pk]))
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CONFIRMADA')                           # recepcionista: bloqueado
        with self.assertRaises(ReglaDeNegocioError):
            op.cancelar_reserva(usuario=self.rec, reserva_id=r.pk)
        op.cancelar_reserva(usuario=self.admin, reserva_id=r.pk)           # administrador: permitido
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CANCELADA')
        self.assertEqual(r.pagos.count(), 1)                               # el pago no se borra

    def test_no_se_cancela_reserva_en_curso(self):
        r = self.en_casa()
        with self.assertRaises(ReglaDeNegocioError):
            op.cancelar_reserva(usuario=self.rec, reserva_id=r.pk)

    def test_listado_filtros(self):
        a = self.reservar(self.h705, self.d(1), self.d(2), doc='111')
        b = self.reservar(self.h706, self.d(10), self.d(12), doc='222')
        op.cancelar_reserva(usuario=self.rec, reserva_id=b.pk)
        def ids(**f):
            return {x.pk for x in self.c.get(reverse('recepcion:reservas'), f).context['pagina']}
        self.assertEqual(ids(estado='CANCELADA'), {b.pk})
        self.assertEqual(ids(habitacion='705'), {a.pk})
        self.assertEqual(ids(q='222'), {b.pk})
        self.assertEqual(ids(desde=self.d(5).isoformat()), {b.pk})
        self.assertEqual(ids(hasta=self.d(5).isoformat()), {a.pk})
        self.assertEqual(ids(q=f'RW-{a.pk:06d}'), {a.pk})
        self.assertEqual(ids(), {a.pk, b.pk})

    def test_confirmar_reserva_pendiente(self):
        r = self.reservar(self.h705, self.d(5), self.d(6))
        Reserva.objects.filter(pk=r.pk).update(estado='PENDIENTE')
        self.c.post(reverse('recepcion:reserva_confirmar', args=[r.pk]))
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CONFIRMADA')


# ======================================================================================
# CHECK-IN
# ======================================================================================
class CheckinTests(Base):
    def setUp(self):
        super().setUp()
        self.c = self.cliente_http(self.rec)

    def test_checkin_por_vista(self):
        r = self.reservar()
        self.assertEqual(self.c.get(reverse('recepcion:checkin', args=[r.pk])).status_code, 200)
        resp = self.c.post(reverse('recepcion:checkin', args=[r.pk]), {'observaciones': 'Llega con equipaje extra'})
        self.assertRedirects(resp, reverse('recepcion:reserva_detalle', args=[r.pk]))
        r.refresh_from_db()
        self.h705.refresh_from_db()
        self.assertEqual(r.estado, 'CHECKIN')
        self.assertEqual(r.checkin_real, self.reloj)
        self.assertEqual(self.h705.estado, Habitacion.OCUPADA)
        o = OperacionRecepcion.objects.get(tipo='CHECKIN')
        self.assertIn('equipaje extra', o.descripcion)
        self.assertEqual(o.usuario, self.rec)

    def test_listado_de_llegadas(self):
        a = self.reservar(self.h705, self.hoy, self.d(2))
        self.reservar(self.h706, self.d(3), self.d(5), doc='222')
        r = self.c.get(reverse('recepcion:llegadas'))
        self.assertEqual([x.pk for x in r.context['pendientes']], [a.pk])
        self.assertEqual(len(r.context['proximas']), 1)

    def test_no_checkin_de_cancelada(self):
        r = self.reservar()
        op.cancelar_reserva(usuario=self.rec, reserva_id=r.pk)
        with self.assertRaises(ReglaDeNegocioError):
            op.hacer_checkin(usuario=self.rec, reserva_id=r.pk)
        self.c.post(reverse('recepcion:checkin', args=[r.pk]))
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CANCELADA')
        self.assertFalse(OperacionRecepcion.objects.filter(tipo='CHECKIN').exists())

    def test_no_checkin_antes_de_la_fecha_ni_pendiente_ni_doble(self):
        futura = self.reservar(self.h706, self.d(3), self.d(5), doc='222')
        with self.assertRaises(ReglaDeNegocioError):
            op.hacer_checkin(usuario=self.rec, reserva_id=futura.pk)
        pend = self.reservar(self.h705, self.hoy, self.d(1), doc='333')
        Reserva.objects.filter(pk=pend.pk).update(estado='PENDIENTE')
        with self.assertRaises(ReglaDeNegocioError):
            op.hacer_checkin(usuario=self.rec, reserva_id=pend.pk)
        Reserva.objects.filter(pk=pend.pk).update(estado='CONFIRMADA')
        op.hacer_checkin(usuario=self.rec, reserva_id=pend.pk)
        with self.assertRaises(ReglaDeNegocioError):
            op.hacer_checkin(usuario=self.rec, reserva_id=pend.pk)

    def test_no_checkin_con_habitacion_en_aseo(self):
        r = self.reservar()
        self.h705.estado = Habitacion.EN_ASEO
        self.h705.save()
        with self.assertRaises(ReglaDeNegocioError):
            op.hacer_checkin(usuario=self.rec, reserva_id=r.pk)

    def test_alojados_lista_con_saldo(self):
        r = self.en_casa()
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('120000'), metodo='EFECTIVO')
        resp = self.c.get(reverse('recepcion:alojados'))
        fila = list(resp.context['pagina'])[0]
        self.assertEqual((fila.pk, fila.saldo), (r.pk, Decimal('1000000')))       # 2 × 560.000 − 120.000


# ======================================================================================
# SERVICIOS ADICIONALES
# ======================================================================================
class ServiciosTests(Base):
    def setUp(self):
        super().setUp()
        self.c = self.cliente_http(self.rec)

    def _post(self, reserva_id, cantidad=3, **extra):
        token = self.c.get(reverse('recepcion:servicio_nuevo')).context['token']
        return self.c.post(reverse('recepcion:servicio_nuevo'), {
            'reserva': reserva_id, 'servicio': self.minibar.pk, 'cantidad': cantidad, 'token': token, **extra})

    def test_cargar_servicio_suma_a_la_cuenta(self):
        r = self.en_casa()
        resp = self._post(r.pk, precio_unitario='1', subtotal='1')
        self.assertRedirects(resp, reverse('recepcion:reserva_detalle', args=[r.pk]))
        s = ServicioReserva.objects.get()
        self.assertEqual((s.cantidad, s.precio_unitario, s.subtotal), (3, Decimal('10000'), Decimal('30000')))
        cuenta = op.cuenta_reserva(Reserva.objects.get(pk=r.pk))
        self.assertEqual((cuenta['total_servicios'], cuenta['total_general']), (Decimal('30000'), Decimal('1150000')))
        self.assertTrue(OperacionRecepcion.objects.filter(tipo='SERVICIO', monto=Decimal('30000')).exists())

    def test_se_permite_en_confirmada_pero_no_en_otros_estados(self):
        r = self.reservar(self.h705, self.d(4), self.d(5))
        self._post(r.pk, cantidad=1)
        self.assertEqual(ServicioReserva.objects.count(), 1)
        Reserva.objects.filter(pk=r.pk).update(estado='PENDIENTE')
        with self.assertRaises(ReglaDeNegocioError):
            op.cargar_servicio(usuario=self.rec, reserva_id=r.pk, servicio_id=self.minibar.pk, cantidad=1)
        Reserva.objects.filter(pk=r.pk).update(estado='CANCELADA')
        with self.assertRaises(ReglaDeNegocioError):
            op.cargar_servicio(usuario=self.rec, reserva_id=r.pk, servicio_id=self.minibar.pk, cantidad=1)

    def test_sin_reserva_valida_o_cantidad_invalida(self):
        r = self.en_casa()
        self._post(999999)
        self._post(r.pk, cantidad=0)
        self._post(r.pk, cantidad=100)
        self.assertEqual(ServicioReserva.objects.count(), 0)

    def test_doble_envio_no_duplica(self):
        r = self.en_casa()
        token = self.c.get(reverse('recepcion:servicio_nuevo')).context['token']
        datos = {'reserva': r.pk, 'servicio': self.minibar.pk, 'cantidad': 1, 'token': token}
        self.c.post(reverse('recepcion:servicio_nuevo'), datos)
        self.c.post(reverse('recepcion:servicio_nuevo'), datos)
        self.assertEqual(ServicioReserva.objects.count(), 1)

    def test_listado(self):
        r = self.en_casa()
        self._post(r.pk, cantidad=2)
        resp = self.c.get(reverse('recepcion:servicios'), {'q': '705'})
        self.assertEqual(len(resp.context['pagina']), 1)


# ======================================================================================
# PAGOS Y ABONOS
# ======================================================================================
class PagosTests(Base):
    def setUp(self):
        super().setUp()
        self.c = self.cliente_http(self.rec)

    def _post(self, reserva_id, monto, metodo='EFECTIVO', **extra):
        token = self.c.get(reverse('recepcion:pago_nuevo')).context['token']
        return self.c.post(reverse('recepcion:pago_nuevo'), {
            'reserva': reserva_id, 'monto': monto, 'metodo_pago': metodo, 'token': token, **extra})

    def test_abono_con_trazabilidad(self):
        r = self.reservar()                                                   # 2 noches × 560.000 = 1.120.000
        resp = self._post(r.pk, '1000000', 'TRANSFERENCIA', referencia='TRF-889')
        self.assertRedirects(resp, reverse('recepcion:reserva_detalle', args=[r.pk]))
        p = Pago.objects.get()
        self.assertEqual((p.monto, p.metodo_pago, p.referencia, p.registrado_por), (Decimal('1000000.00'), 'TRANSFERENCIA', 'TRF-889', self.rec))
        self.assertEqual(p.fecha_pago, self.reloj)
        cuenta = op.cuenta_reserva(r)
        self.assertEqual(cuenta['saldo_pendiente'], Decimal('120000'))
        o = OperacionRecepcion.objects.get(tipo='PAGO')
        self.assertEqual((o.usuario, o.monto, o.reserva), (self.rec, Decimal('1000000.00'), r))

    def test_no_negativos_ni_cero(self):
        r = self.reservar()
        for malo in ('-5000', '0', '0.00', '', 'abc'):
            self._post(r.pk, malo)
        self.assertEqual(Pago.objects.count(), 0)
        for malo in (Decimal('0'), Decimal('-1')):
            with self.assertRaises(ReglaDeNegocioError):
                op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=malo, metodo='EFECTIVO')

    def test_no_supera_el_saldo(self):
        r = self.reservar()
        resp = self._post(r.pk, '1120001')
        self.assertContains(resp, 'supera el saldo pendiente')
        self.assertEqual(Pago.objects.count(), 0)
        self._post(r.pk, '1120000')
        self.assertEqual(op.cuenta_reserva(r)['saldo_pendiente'], 0)
        with self.assertRaises(ReglaDeNegocioError):                          # ya pagada
            op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('1'), metodo='EFECTIVO')

    def test_pago_sin_reserva_o_reserva_inexistente(self):
        self._post('', '1000')
        self._post(424242, '1000')
        self.assertEqual(Pago.objects.count(), 0)

    def test_no_en_canceladas_ni_cerradas(self):
        r = self.reservar()
        op.cancelar_reserva(usuario=self.rec, reserva_id=r.pk)
        with self.assertRaises(ReglaDeNegocioError):
            op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('1000'), metodo='EFECTIVO')
        c = self.en_casa(hab=self.h706, doc='222')
        op.registrar_pago(usuario=self.rec, reserva_id=c.pk, monto=Decimal('1120000'), metodo='EFECTIVO')
        op.hacer_checkout(usuario=self.rec, reserva_id=c.pk)
        with self.assertRaises(ReglaDeNegocioError):
            op.registrar_pago(usuario=self.rec, reserva_id=c.pk, monto=Decimal('1000'), metodo='EFECTIVO')

    def test_metodo_invalido(self):
        r = self.reservar()
        self._post(r.pk, '1000', 'BITCOIN')
        self.assertEqual(Pago.objects.count(), 0)

    def test_pagos_historicos_no_se_alteran(self):
        r = self.reservar()
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('300000'), metodo='EFECTIVO')
        primero = Pago.objects.get()
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('100000'), metodo='DATAFONO')
        primero_despues = Pago.objects.get(pk=primero.pk)
        self.assertEqual((primero.monto, primero.fecha_pago, primero.metodo_pago),
                         (primero_despues.monto, primero_despues.fecha_pago, primero_despues.metodo_pago))
        self.assertEqual(Pago.objects.count(), 2)

    def test_no_existen_rutas_para_editar_o_borrar_pagos(self):
        r = self.reservar()
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('1000'), metodo='EFECTIVO')
        p = Pago.objects.get()
        for url in (f'/recepcion/pagos/{p.pk}/', f'/recepcion/pagos/{p.pk}/editar/', f'/recepcion/pagos/{p.pk}/eliminar/'):
            self.assertEqual(self.c.post(url, {'monto': '1'}).status_code, 404)
        p.refresh_from_db()
        self.assertEqual(p.monto, Decimal('1000.00'))

    def test_doble_envio_no_duplica_el_pago(self):
        r = self.reservar()
        token = self.c.get(reverse('recepcion:pago_nuevo')).context['token']
        datos = {'reserva': r.pk, 'monto': '500000', 'metodo_pago': 'EFECTIVO', 'token': token}
        self.c.post(reverse('recepcion:pago_nuevo'), datos)
        self.c.post(reverse('recepcion:pago_nuevo'), datos)
        self.assertEqual(Pago.objects.count(), 1)

    def test_saldo_incluye_servicios_y_se_mantiene_consistente(self):
        r = self.en_casa()
        op.cargar_servicio(usuario=self.rec, reserva_id=r.pk, servicio_id=self.minibar.pk, cantidad=2)
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('500000'), metodo='EFECTIVO')
        c = op.cuenta_reserva(Reserva.objects.get(pk=r.pk))
        self.assertEqual((c['total_general'], c['total_pagos'], c['saldo_pendiente']),
                         (Decimal('1140000'), Decimal('500000'), Decimal('640000')))
        lista = op.anotar_saldos([Reserva.objects.get(pk=r.pk)])[0]
        self.assertEqual(lista.saldo, c['saldo_pendiente'])                   # listado y detalle coinciden

    def test_listado_y_filtros(self):
        r = self.reservar()
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('1000'), metodo='EFECTIVO')
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('2000'), metodo='DATAFONO')
        resp = self.c.get(reverse('recepcion:pagos'), {'metodo': 'DATAFONO'})
        self.assertEqual(len(resp.context['pagina']), 1)
        self.assertEqual(resp.context['total'], Decimal('2000'))
        self.assertEqual(self.c.get(reverse('recepcion:pagos'), {'desde': self.hoy.isoformat(), 'hasta': self.hoy.isoformat()}).context['hoy_total'], Decimal('3000'))
        self.assertEqual(len(self.c.get(reverse('recepcion:pagos'), {'desde': self.d(1).isoformat()}).context['pagina']), 0)


# ======================================================================================
# CHECK-OUT
# ======================================================================================
class CheckoutTests(Base):
    def setUp(self):
        super().setUp()
        self.c = self.cliente_http(self.rec)

    def _checkout(self, reserva, **datos):
        token = self.c.get(reverse('recepcion:checkout', args=[reserva.pk])).context['token']
        return self.c.post(reverse('recepcion:checkout', args=[reserva.pk]), {'token': token, **datos})

    def test_rn06_no_checkout_sin_checkin(self):
        r = self.reservar()
        with self.assertRaises(ReglaDeNegocioError):
            op.hacer_checkout(usuario=self.rec, reserva_id=r.pk)
        self._checkout(r)
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CONFIRMADA')
        self.assertFalse(OperacionRecepcion.objects.filter(tipo='CHECKOUT').exists())

    def test_no_cierra_con_saldo_pendiente(self):
        r = self.en_casa()
        resp = self._checkout(r)
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CHECKIN')
        self.assertContains(self.c.get(reverse('recepcion:checkout', args=[r.pk])), 'saldo de')
        with self.assertRaises(ReglaDeNegocioError):
            op.hacer_checkout(usuario=self.rec, reserva_id=r.pk)

    def test_checkout_con_pago_final_en_un_paso(self):
        r = self.en_casa()
        resp = self._checkout(r, monto='1120000', metodo_pago='EFECTIVO', referencia='CAJA-1')
        self.assertRedirects(resp, reverse('recepcion:reserva_detalle', args=[r.pk]))
        r.refresh_from_db()
        self.h705.refresh_from_db()
        self.assertEqual(r.estado, 'CHECKOUT')
        self.assertEqual(r.checkout_real, self.reloj)
        self.assertEqual(r.valor_total, Decimal('1120000'))
        self.assertEqual(self.h705.estado, Habitacion.EN_ASEO)
        self.assertEqual(Pago.objects.get().registrado_por, self.rec)
        self.assertTrue(OperacionRecepcion.objects.filter(tipo='CHECKOUT', reserva=r).exists())
        self.assertEqual(op.cuenta_reserva(r)['saldo_pendiente'], 0)

    def test_checkout_de_cuenta_ya_pagada_con_servicios(self):
        r = self.en_casa()
        op.cargar_servicio(usuario=self.rec, reserva_id=r.pk, servicio_id=self.minibar.pk, cantidad=1)
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('1130000'), metodo='DATAFONO')
        self._checkout(r)
        r.refresh_from_db()
        self.assertEqual((r.estado, r.valor_total), ('CHECKOUT', Decimal('1130000')))

    def test_pago_parcial_en_checkout_se_conserva_y_no_cierra(self):
        r = self.en_casa()
        resp = self._checkout(r, monto='500000', metodo_pago='EFECTIVO')
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CHECKIN')
        self.assertEqual(Pago.objects.get().monto, Decimal('500000.00'))      # el dinero recibido queda registrado

    def test_pago_final_exige_metodo(self):
        r = self.en_casa()
        self._checkout(r, monto='1120000')
        self.assertEqual(Pago.objects.count(), 0)

    def test_cargo_por_salida_tardia_se_cobra_antes_de_cerrar(self):
        r = self.en_casa()
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('1120000'), metodo='EFECTIVO')
        tarde = timezone.make_aware(datetime.combine(self.hoy, time(12, 20)), self.tz)
        with mock.patch('django.utils.timezone.now', return_value=tarde):
            with self.assertRaises(ReglaDeNegocioError) as cm:
                op.hacer_checkout(usuario=self.rec, reserva_id=r.pk)
            self.assertIn('$30.000', str(cm.exception))
            op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('30000'), metodo='EFECTIVO', con_cargo_tardio=True)
            op.hacer_checkout(usuario=self.rec, reserva_id=r.pk)
        r.refresh_from_db()
        self.assertEqual((r.estado, r.cargo_tardio, r.valor_total), ('CHECKOUT', Decimal('30000'), Decimal('1150000')))

    def test_saldo_a_favor_requiere_administrador(self):
        r = self.en_casa()
        Pago.objects.create(reserva=r, monto=Decimal('1500000'), metodo_pago='EFECTIVO')   # inconsistencia heredada
        with self.assertRaises(ReglaDeNegocioError) as cm:
            op.hacer_checkout(usuario=self.rec, reserva_id=r.pk)
        self.assertIn('saldo a favor', str(cm.exception))

    def test_listado_de_salidas(self):
        r = self.en_casa()
        resp = self.c.get(reverse('recepcion:salidas'))
        self.assertEqual(len(resp.context['proximas']) + len(resp.context['hoy_y_vencidas']), 1)
        self.en_casa(hab=self.h706, entrada=self.hoy, salida=self.hoy + timedelta(days=1), doc='222')
        resp = self.c.get(reverse('recepcion:salidas'))
        self.assertEqual(len(resp.context['hoy_y_vencidas']), 0)

    def test_habilitar_habitacion_despues_del_checkout(self):
        r = self.en_casa()
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('1120000'), metodo='EFECTIVO')
        op.hacer_checkout(usuario=self.rec, reserva_id=r.pk)
        self.assertEqual(self.c.get(reverse('recepcion:habitacion_habilitar', args=[self.h705.pk])).status_code, 405)   # solo POST
        self.c.post(reverse('recepcion:habitacion_habilitar', args=[self.h705.pk]))
        self.h705.refresh_from_db()
        self.assertEqual(self.h705.estado, Habitacion.DISPONIBLE)
        self.assertTrue(OperacionRecepcion.objects.filter(tipo='HABITACION_HABILITADA').exists())

    def test_mantenimiento_solo_lo_habilita_administracion(self):
        self.h705.estado = Habitacion.MANTENIMIENTO
        self.h705.save()
        with self.assertRaises(ReglaDeNegocioError):
            op.habilitar_habitacion(usuario=self.rec, habitacion_id=self.h705.pk)
        op.habilitar_habitacion(usuario=self.admin, habitacion_id=self.h705.pk)
        self.h705.refresh_from_db()
        self.assertEqual(self.h705.estado, Habitacion.DISPONIBLE)


# ======================================================================================
# DASHBOARD, HISTORIAL E INTEGRIDAD
# ======================================================================================
class DashboardYHistorialTests(Base):
    def setUp(self):
        super().setUp()
        self.c = self.cliente_http(self.rec)

    def test_dashboard_vacio(self):
        d = self.c.get(reverse('recepcion:inicio')).context['d']
        self.assertEqual((d['reservas_hoy'], d['alojados'], d['pagos_hoy_total'], d['saldo_por_cobrar']), (0, 0, 0, 0))
        self.assertEqual(d['estados']['disponibles'], 3)

    def test_dashboard_con_datos_reales(self):
        a = self.en_casa(hab=self.h705, entrada=self.hoy, salida=self.d(1), doc='111')       # sale hoy... mañana
        b = self.reservar(hab=self.h706, entrada=self.hoy, salida=self.d(3), doc='222')     # llegada pendiente
        pend = self.reservar(hab=self.h901, entrada=self.d(5), salida=self.d(6), doc='333')
        Reserva.objects.filter(pk=pend.pk).update(estado='PENDIENTE')
        op.registrar_pago(usuario=self.rec, reserva_id=a.pk, monto=Decimal('300000'), metodo='EFECTIVO')
        d = self.c.get(reverse('recepcion:inicio')).context['d']
        self.assertEqual(d['reservas_hoy'], 2)
        self.assertEqual(d['llegadas_pendientes'], 1)
        self.assertEqual(d['alojados'], 1)
        self.assertEqual(d['pendientes_confirmar'], 1)
        self.assertEqual(d['estados']['ocupadas'], 1)
        self.assertEqual(d['estados']['disponibles'], 1)       # 706 queda «reservada», 901 libre
        self.assertEqual(d['estados']['reservadas'], 1)
        self.assertEqual(d['pagos_hoy_total'], Decimal('300000'))
        self.assertEqual(d['pagos_hoy_n'], 1)
        self.assertEqual(d['reservas_con_saldo'], 2)
        self.assertEqual(d['saldo_por_cobrar'], Decimal('260000') + Decimal('1680000'))      # a: 560.000−300.000 · b: 3×560.000
        self.assertEqual({r.pk for r in d['proximas_salidas']}, {a.pk})
        self.assertEqual({r.pk for r in d['proximas_llegadas']}, {b.pk, pend.pk})
        self.assertEqual(len(d['recientes']), 3)

    def test_dashboard_estados_en_aseo_y_mantenimiento(self):
        self.h705.estado = Habitacion.EN_ASEO
        self.h705.save()
        self.h706.estado = Habitacion.MANTENIMIENTO
        self.h706.save()
        e = self.c.get(reverse('recepcion:inicio')).context['d']['estados']
        self.assertEqual((e['en_aseo'], e['no_operativas'], e['disponibles']), (1, 1, 1))

    def test_historial_registra_y_filtra(self):
        r = self.en_casa()
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('1000'), metodo='EFECTIVO')
        op.registrar_pago(usuario=self.admin, reserva_id=r.pk, monto=Decimal('2000'), metodo='EFECTIVO')
        resp = self.c.get(reverse('recepcion:historial'), {'tipo': 'PAGO'})
        self.assertEqual(len(resp.context['pagina']), 2)
        resp = self.c.get(reverse('recepcion:historial'), {'tipo': 'PAGO', 'mias': '1'})
        self.assertEqual(len(resp.context['pagina']), 1)
        resp = self.c.get(reverse('recepcion:historial'), {'q': '1082300123'})
        self.assertGreaterEqual(len(resp.context['pagina']), 3)
        self.assertContains(resp, 'Registró un pago')

    def test_historial_es_inmutable(self):
        o = op.registrar_operacion(self.rec, 'PAGO', 'x')
        o.descripcion = 'cambiada'
        with self.assertRaises(ValidationError):
            o.save()
        with self.assertRaises(ValidationError):
            o.delete()
        for url in ('/recepcion/historial/1/eliminar/', '/recepcion/historial/1/editar/'):
            self.assertEqual(self.c.post(url).status_code, 404)

    def test_get_no_modifica_datos(self):
        r = self.en_casa()
        antes = (Reserva.objects.count(), Pago.objects.count(), OperacionRecepcion.objects.count())
        for name, args in [('checkout', [r.pk]), ('checkin', [r.pk]), ('reserva_cancelar', [r.pk]),
                           ('pago_nuevo', []), ('servicio_nuevo', []), ('inicio', [])]:
            self.c.get(reverse('recepcion:' + name, args=args))
        self.assertEqual(antes, (Reserva.objects.count(), Pago.objects.count(), OperacionRecepcion.objects.count()))
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CHECKIN')

    def test_post_sin_csrf_es_rechazado(self):
        c = Client(enforce_csrf_checks=True)
        s = c.session
        s['usuario_id'], s['usuario_rol'] = self.rec.pk, 'RECEPCIONISTA'
        s.save()
        r = self.reservar()
        self.assertEqual(c.post(reverse('recepcion:pago_nuevo'), {'reserva': r.pk, 'monto': '1000', 'metodo_pago': 'EFECTIVO'}).status_code, 403)
        self.assertEqual(Pago.objects.count(), 0)


# ======================================================================================
# OTROS ROLES SIGUEN FUNCIONANDO + MIGRACIONES
# ======================================================================================
class RegresionOtrosRolesTests(Base):
    def test_administrador_conserva_su_panel(self):
        c = self.cliente_http(self.admin)
        for url in ('/panel/', '/gestion/', '/gestion/reservas/', '/gestion/huespedes/', '/huespedes/',
                    '/disponibilidad/', '/gestion/habitaciones/', '/gestion/finanzas/', '/gestion/usuarios/',
                    '/wbar/gestion/', '/wbar/gestion/categorias/'):
            self.assertEqual(c.get(url).status_code, 200, url)

    def test_cliente_conserva_sus_pantallas_y_sigue_sin_panel(self):
        c = self.cliente_http(self.cliente)
        self.assertEqual(c.get(reverse('mis_reservas')).status_code, 200)
        self.assertEqual(c.get(reverse('cuenta')).status_code, 200)
        self.assertEqual(c.get('/gestion/').status_code, 403)
        self.assertEqual(c.get('/recepcion/').status_code, 403)

    def test_rutas_historicas_de_recepcion_siguen_vivas(self):
        r = self.reservar()
        c = self.cliente_http(self.rec)
        self.assertEqual(c.get(reverse('crear_reserva')).status_code, 200)
        self.assertEqual(c.get(reverse('reserva_detalle', args=[r.pk])).status_code, 200)
        self.assertEqual(c.get(reverse('checkin', args=[r.pk])).status_code, 200)

    # --- Ruta antigua /checkout/<id>/ ---
    def test_ruta_antigua_checkout_recepcionista_no_cierra_con_saldo(self):
        r = self.en_casa()
        c = self.cliente_http(self.rec)
        resp = c.post(reverse('checkout', args=[r.pk]))
        self.assertRedirects(resp, reverse('recepcion:checkout', args=[r.pk]))
        r.refresh_from_db()
        self.h705.refresh_from_db()
        self.assertEqual((r.estado, self.h705.estado), ('CHECKIN', Habitacion.OCUPADA))
        self.assertFalse(OperacionRecepcion.objects.filter(tipo='CHECKOUT').exists())

    def test_ruta_antigua_checkout_recepcionista_get_va_al_flujo_nuevo(self):
        r = self.en_casa()
        c = self.cliente_http(self.rec)
        self.assertRedirects(c.get(reverse('checkout', args=[r.pk])), reverse('recepcion:checkout', args=[r.pk]))

    def test_ruta_antigua_checkout_recepcionista_cierra_si_saldo_cero(self):
        r = self.en_casa()
        op.registrar_pago(usuario=self.rec, reserva_id=r.pk, monto=Decimal('1120000'), metodo='EFECTIVO')
        c = self.cliente_http(self.rec)
        resp = c.post(reverse('checkout', args=[r.pk]))
        self.assertRedirects(resp, reverse('recepcion:reserva_detalle', args=[r.pk]))
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CHECKOUT')
        self.assertTrue(OperacionRecepcion.objects.filter(tipo='CHECKOUT', reserva=r, usuario=self.rec).exists())

    def test_ruta_antigua_checkout_recepcionista_sin_checkin(self):
        r = self.reservar()
        c = self.cliente_http(self.rec)
        c.post(reverse('checkout', args=[r.pk]))
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CONFIRMADA')

    def test_ruta_antigua_checkout_administrador_conserva_su_comportamiento(self):
        r = self.en_casa()
        c = self.cliente_http(self.admin)
        self.assertEqual(c.get(reverse('checkout', args=[r.pk])).status_code, 200)
        self.assertRedirects(c.post(reverse('checkout', args=[r.pk])), reverse('inicio'), fetch_redirect_response=False)
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CHECKOUT')            # comportamiento histórico del administrador

    def test_ruta_antigua_checkout_cliente_sigue_bloqueado(self):
        r = self.en_casa()
        c = self.cliente_http(self.cliente)
        self.assertEqual(c.post(reverse('checkout', args=[r.pk])).status_code, 403)
        r.refresh_from_db()
        self.assertEqual(r.estado, 'CHECKIN')

    def test_wbar_sigue_cobrando_pagos_sin_usuario_de_recepcion(self):
        r = self.en_casa()
        p = Pago.objects.create(reserva=r, monto=Decimal('1000'), metodo_pago='EFECTIVO')   # forma en que lo hace Wbar
        self.assertIsNone(p.registrado_por)
        self.assertEqual(p.referencia, '')

    def test_modelos_y_migraciones_sincronizados(self):
        call_command('makemigrations', '--check', '--dry-run', verbosity=0)
