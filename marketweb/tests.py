"""
Pruebas del módulo de creación de reservas (casos 1-14 del requerimiento).

Ejecutar:  python manage.py test marketweb
El caso 14 (concurrencia real) solo corre con MySQL; con SQLite se omite porque
SQLite ignora select_for_update.
"""
import threading
from datetime import timedelta
from decimal import Decimal

from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from unittest import skipIf

from . import services
from .models import Habitacion, Hotel, Huesped, Reserva, TipoHabitacion, Usuario
from .services import ReglaDeNegocioError


def _datos_huesped(doc='1082300123'):
    return {
        'tipo_documento': 'CC', 'numero_documento': doc, 'nombres': 'Juan', 'apellidos': 'Pérez',
        'telefono': '+57 300 000 0000', 'correo': 'juan@correo.com',
        'nacionalidad': 'Colombiana', 'es_extranjero': False,
    }


class BaseReservas:
    """Datos mínimos de prueba (NO tocan la BD real: Django usa una BD de test aparte)."""

    def preparar(self):
        self.hoy = timezone.localdate()
        self.hotel = Hotel.objects.create()
        self.ocean = TipoHabitacion.objects.create(nombre='W Ocean View', capacidad=2, precio_noche=Decimal('560000'))
        self.royal = TipoHabitacion.objects.create(nombre='W Royal Suite', capacidad=6, precio_noche=Decimal('2000000'))
        self.h705 = Habitacion.objects.create(numero='705', piso=7, tipo=self.ocean, hotel=self.hotel)
        self.h706 = Habitacion.objects.create(numero='706', piso=7, tipo=self.ocean, hotel=self.hotel)
        self.h901 = Habitacion.objects.create(numero='901', piso=9, tipo=self.royal, hotel=self.hotel)

    def d(self, dias):
        return self.hoy + timedelta(days=dias)

    def reservar(self, hab, entrada, salida, n=1, doc='1082300123', **kw):
        return services.crear_reserva(
            habitacion_id=hab.pk, datos_huesped=_datos_huesped(doc),
            entrada=entrada, salida=salida, num_huespedes=n, **kw)


class ReservasTests(BaseReservas, TestCase):
    def setUp(self):
        self.preparar()

    # Caso 1 / 4
    def test_01_reserva_valida_y_04_fechas_futuras(self):
        r = self.reservar(self.h705, self.d(10), self.d(13), n=2, tipo_id=self.ocean.pk)
        self.assertEqual(r.estado, 'CONFIRMADA')
        self.assertEqual(r.calcular_noches(), 3)
        self.assertEqual(r.valor_total, Decimal('1680000'))      # 3 × 560.000, por habitación
        self.h705.refresh_from_db()
        self.assertEqual(self.h705.estado, Habitacion.DISPONIBLE)  # NO pasa a OCUPADA

    # Casos 2 y 3
    def test_02_03_solapamiento(self):
        self.reservar(self.h705, self.d(10), self.d(15))
        for entrada, salida in [(self.d(12), self.d(14)), (self.d(8), self.d(11)), (self.d(10), self.d(15))]:
            with self.assertRaises(ReglaDeNegocioError):
                self.reservar(self.h705, entrada, salida, doc='999')
        # el día de salida queda libre para la siguiente llegada
        self.reservar(self.h705, self.d(15), self.d(18), doc='999')

    # Caso 5
    def test_05_capacidad(self):
        with self.assertRaises(ReglaDeNegocioError):
            self.reservar(self.h705, self.d(5), self.d(6), n=5)

    # Casos 6 y 7
    def test_06_07_fechas_invalidas(self):
        with self.assertRaises(ReglaDeNegocioError):
            self.reservar(self.h705, self.d(5), self.d(3))
        with self.assertRaises(ReglaDeNegocioError):
            self.reservar(self.h705, self.d(5), self.d(5))
        with self.assertRaises(ReglaDeNegocioError):      # pasado
            self.reservar(self.h705, self.d(-2), self.d(1))

    # Casos 8 y 9
    def test_08_09_fuera_de_servicio_y_mantenimiento(self):
        for estado in (Habitacion.FUERA_DE_SERVICIO, Habitacion.MANTENIMIENTO):
            self.h705.estado = estado
            self.h705.save()
            with self.assertRaises(ReglaDeNegocioError):
                self.reservar(self.h705, self.d(5), self.d(6))
        self.h705.estado, self.h705.activa = Habitacion.DISPONIBLE, False
        self.h705.save()
        with self.assertRaises(ReglaDeNegocioError):
            self.reservar(self.h705, self.d(5), self.d(6))

    def test_habitacion_de_otro_tipo_se_rechaza(self):
        with self.assertRaises(ReglaDeNegocioError):
            self.reservar(self.h705, self.d(5), self.d(6), tipo_id=self.royal.pk)

    def test_huesped_existente_no_se_duplica(self):
        self.reservar(self.h705, self.d(5), self.d(6), doc='123')
        self.reservar(self.h706, self.d(5), self.d(6), doc='123')
        self.assertEqual(Huesped.objects.filter(numero_documento='123').count(), 1)

    def test_error_no_deja_reserva_parcial(self):
        with self.assertRaises(ReglaDeNegocioError):
            self.reservar(self.h705, self.d(5), self.d(6), n=9, doc='777')
        self.assertEqual(Reserva.objects.count(), 0)
        self.assertFalse(Huesped.objects.filter(numero_documento='777').exists())

    def test_disponibilidad_excluye_solapadas(self):
        self.reservar(self.h705, self.d(10), self.d(15))
        ids = set(services.habitaciones_disponibles(
            self.d(12), self.d(14), 1, hoy=self.hoy, tipo_id=self.ocean.pk).values_list('pk', flat=True))
        self.assertEqual(ids, {self.h706.pk})
        ids = set(services.habitaciones_disponibles(
            self.d(15), self.d(18), 1, hoy=self.hoy, tipo_id=self.ocean.pk).values_list('pk', flat=True))
        self.assertEqual(ids, {self.h705.pk, self.h706.pk})

    # Casos 10-13: ciclo completo + dashboard
    def test_10_a_13_ciclo_completo(self):
        r = self.reservar(self.h705, self.hoy, self.d(2))
        self.assertEqual(self.h705.estado, Habitacion.DISPONIBLE)

        habs, activas = services.anotar_estados(Habitacion.objects.select_related('tipo'), hoy=self.hoy)
        h = next(x for x in habs if x.pk == self.h705.pk)
        self.assertEqual(h.estado_visual, 'RESERVADA')                      # caso 10
        self.assertIn(r.pk, [a.pk for a in activas if a.estado == 'CONFIRMADA'])

        services.registrar_checkin(r.pk)                                    # caso 11
        self.h705.refresh_from_db()
        self.assertEqual(self.h705.estado, Habitacion.OCUPADA)

        services.registrar_checkout(r.pk)                                   # caso 12
        self.h705.refresh_from_db()
        self.assertEqual(self.h705.estado, Habitacion.EN_ASEO)

        services.habilitar_habitacion(self.h705.pk)                         # caso 13
        self.h705.refresh_from_db()
        self.assertEqual(self.h705.estado, Habitacion.DISPONIBLE)


class ReservasVistasTests(BaseReservas, TestCase):
    def setUp(self):
        self.preparar()
        self.c = Client()
        sesion = self.c.session
        sesion['usuario_id'] = 1
        sesion['usuario_rol'] = 'RECEPCIONISTA'
        sesion.save()

    def _post(self, **extra):
        datos = {
            **_datos_huesped(), 'es_extranjero': '', 'fecha_entrada': self.d(10).isoformat(),
            'fecha_salida': self.d(13).isoformat(), 'tipo_habitacion': self.ocean.pk,
            'habitacion': self.h705.pk, 'num_huespedes': 2, 'canal_reserva': 'PRESENCIAL',
        }
        datos.update(extra)
        return self.c.post(reverse('crear_reserva'), datos)

    def test_post_valido_redirige_al_detalle_y_guarda(self):
        r = self._post()
        reserva = Reserva.objects.get()
        self.assertRedirects(r, f"{reverse('reserva_detalle', args=[reserva.pk])}?nueva=1")
        self.assertEqual(reserva.valor_total, Decimal('1680000'))

    def test_precio_y_estado_enviados_por_el_navegador_se_ignoran(self):
        self._post(valor_total='1', estado='CHECKIN', precio_noche='1')
        reserva = Reserva.objects.get()
        self.assertEqual(reserva.estado, 'CONFIRMADA')
        self.assertEqual(reserva.valor_total, Decimal('1680000'))

    def test_post_invalido_muestra_errores_y_no_guarda(self):
        r = self._post(num_huespedes=5)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'admite máximo 2 huéspedes')
        self.assertEqual(Reserva.objects.count(), 0)

    def test_sin_sesion_redirige_a_login(self):
        r = Client().post(reverse('crear_reserva'), {})
        self.assertRedirects(r, reverse('login'), fetch_redirect_response=False)

    def test_ruta_habilitar_habitacion(self):
        admin = Usuario.objects.create(
            nombres='Admin', apellidos='Gerente', correo='admin_hab@test.com',
            contrasena='hash', rol='ADMIN_GERENTE', activo=True
        )
        self.h705.estado = Habitacion.EN_ASEO
        self.h705.save()

        c = Client()
        s = c.session
        s['usuario_id'] = admin.pk
        s['usuario_rol'] = 'ADMIN_GERENTE'
        s.save()

        r = c.post(reverse('habilitar_habitacion', args=[self.h705.pk]))
        self.assertRedirects(r, reverse('gestion_servicios'))
        self.h705.refresh_from_db()
        self.assertEqual(self.h705.estado, Habitacion.DISPONIBLE)

    def test_generar_factura_dian_cuantizacion(self):
        admin = Usuario.objects.create(
            nombres='Admin', apellidos='Gerente', correo='admin_fac@test.com',
            contrasena='hash', rol='ADMIN_GERENTE', activo=True
        )
        reserva = self.reservar(self.h705, self.d(10), self.d(13), n=1)
        reserva.valor_total = Decimal('100000.00')
        reserva.save()

        c = Client()
        s = c.session
        s['usuario_id'] = admin.pk
        s['usuario_rol'] = 'ADMIN_GERENTE'
        s.save()

        r = c.get(reverse('factura_dian', args=[reserva.pk]))
        self.assertEqual(r.status_code, 200)
        from .models import Factura
        factura = Factura.objects.get(reserva=reserva)
        self.assertEqual(factura.subtotal, Decimal('84033.61'))
        self.assertEqual(factura.iva, Decimal('15966.39'))
        self.assertEqual(factura.total, Decimal('100000.00'))


@skipIf(connection.vendor == 'sqlite', 'La concurrencia real requiere MySQL (select_for_update).')
class DobleReservaConcurrenteTests(BaseReservas, TransactionTestCase):
    # Caso 14
    def test_14_dos_reservas_simultaneas_misma_habitacion(self):
        self.preparar()
        resultados, barrera = [], threading.Barrier(2)

        def intentar(doc):
            try:
                barrera.wait()
                self.reservar(self.h705, self.d(10), self.d(13), doc=doc)
                resultados.append('ok')
            except ReglaDeNegocioError:
                resultados.append('rechazada')
            finally:
                connections.close_all()

        hilos = [threading.Thread(target=intentar, args=(d,)) for d in ('111', '222')]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        self.assertEqual(sorted(resultados), ['ok', 'rechazada'])
        self.assertEqual(Reserva.objects.filter(habitacion=self.h705).count(), 1)