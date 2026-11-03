"""
Carga inicial IDEMPOTENTE de tipos de habitación y de las 120 habitaciones del Hotel W.

    python manage.py cargar_habitaciones --dry-run   # solo informa, no escribe
    python manage.py cargar_habitaciones             # carga

Garantías:
  * Solo CREA lo que falta. Nunca borra ni sobrescribe datos existentes.
  * Si algo existente choca con el catálogo (mismo nombre de tipo con otra capacidad
    o precio, o un número de habitación con otro tipo/piso), NO escribe nada y
    lista los conflictos para que se resuelvan a mano.
  * Ejecutarlo dos veces no crea duplicados.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from marketweb.catalogo_hotel import HOTEL, TIPOS, habitaciones_oficiales
from marketweb.models import Habitacion, Hotel, TipoHabitacion
from datetime import time


class Command(BaseCommand):
    help = "Crea (sin duplicar) los tipos de habitación y las 120 habitaciones oficiales del Hotel W."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Solo reporta; no escribe en la BD.')

    def handle(self, *args, **opciones):
        oficiales = habitaciones_oficiales()
        conflictos, avisos = [], []

        # ---- Hotel -------------------------------------------------------
        hotel = Hotel.objects.order_by('id').first()
        if hotel:
            if hotel.nombre != HOTEL['nombre']:
                avisos.append(f"Hotel existente se llama «{hotel.nombre}» (catálogo: «{HOTEL['nombre']}»). No se modifica.")
            if hotel.horario_checkin != time(15, 0) or hotel.horario_checkout != time(12, 0):
                avisos.append(f"Hotel existente tiene check-in {hotel.horario_checkin} / check-out "
                              f"{hotel.horario_checkout} (ficha: 15:00 / 12:00). No se modifica.")

        # ---- Tipos -------------------------------------------------------
        tipos_existentes = {}
        for t in TipoHabitacion.objects.all():
            tipos_existentes.setdefault(t.nombre, []).append(t)

        tipos_por_crear, tipos_por_completar = [], []
        for nombre, (capacidad, precio, _desc) in TIPOS.items():
            filas = tipos_existentes.get(nombre, [])
            if not filas:
                tipos_por_crear.append(nombre)
            elif len(filas) > 1:
                conflictos.append(f"Hay {len(filas)} tipos llamados «{nombre}» (ids {[f.id for f in filas]}).")
            else:
                t = filas[0]
                if t.capacidad != capacidad:
                    conflictos.append(f"Tipo «{nombre}»: capacidad {t.capacidad} en BD vs {capacidad} en la ficha.")
                if t.precio_noche == 0:
                    tipos_por_completar.append(nombre)       # solo tenía el valor por defecto
                elif t.precio_noche != precio:
                    conflictos.append(f"Tipo «{nombre}»: precio por noche {t.precio_noche} en BD vs {precio} en la ficha.")
        otros = sorted(n for n in tipos_existentes if n not in TIPOS)
        if otros:
            avisos.append(f"Tipos existentes fuera del catálogo (no se tocan): {', '.join(otros)}.")

        # ---- Habitaciones ------------------------------------------------
        existentes = {h.numero: h for h in Habitacion.objects.select_related('tipo')}
        por_crear = []
        for numero, piso, nombre_tipo in oficiales:
            h = existentes.get(numero)
            if h is None:
                por_crear.append((numero, piso, nombre_tipo))
                continue
            if h.tipo.nombre != nombre_tipo:
                conflictos.append(f"Habitación {numero}: tipo «{h.tipo.nombre}» en BD vs «{nombre_tipo}» en la ficha.")
            if h.piso != piso:
                conflictos.append(f"Habitación {numero}: piso {h.piso} en BD vs {piso} en la ficha.")
        oficiales_numeros = {n for n, _, _ in oficiales}
        sobrantes = sorted(n for n in existentes if n not in oficiales_numeros)
        if sobrantes:
            avisos.append(f"Habitaciones existentes que NO están en la lista oficial (no se tocan): {', '.join(sobrantes)}.")

        # ---- Reporte -----------------------------------------------------
        for a in avisos:
            self.stdout.write(self.style.WARNING(f"AVISO: {a}"))
        if conflictos:
            for c in conflictos:
                self.stderr.write(self.style.ERROR(f"CONFLICTO: {c}"))
            raise CommandError(f"{len(conflictos)} conflicto(s) con datos existentes. No se escribió nada. "
                               "Resuélvelos (o consúltalos) antes de volver a ejecutar.")

        resumen = (f"Tipos por crear: {len(tipos_por_crear)} · precios por completar: {len(tipos_por_completar)} · "
                   f"habitaciones por crear: {len(por_crear)} · ya existentes correctas: {len(oficiales) - len(por_crear)}")
        if opciones['dry_run']:
            self.stdout.write(self.style.NOTICE(f"[dry-run] {resumen}"))
            return

        # ---- Escritura (todo o nada) ---------------------------------------
        with transaction.atomic():
            if hotel is None:
                hotel = Hotel.objects.create(
                    nombre=HOTEL['nombre'], direccion=HOTEL['direccion'],
                    horario_checkin=time(15, 0), horario_checkout=time(12, 0),
                )
            tipos = {}
            for nombre, (capacidad, precio, descripcion) in TIPOS.items():
                t = TipoHabitacion.objects.filter(nombre=nombre).first()
                if t is None:
                    t = TipoHabitacion.objects.create(nombre=nombre, capacidad=capacidad,
                                                      precio_noche=precio, descripcion=descripcion)
                elif t.precio_noche == 0:
                    t.precio_noche = precio
                    t.save(update_fields=['precio_noche'])
                tipos[nombre] = t
            for numero, piso, nombre_tipo in por_crear:
                Habitacion.objects.create(numero=numero, piso=piso, tipo=tipos[nombre_tipo], hotel=hotel)

        presentes = Habitacion.objects.filter(numero__in=oficiales_numeros).count()
        self.stdout.write(self.style.SUCCESS(
            f"OK: {presentes} de {len(oficiales)} habitaciones oficiales presentes "
            f"(creadas ahora: {len(por_crear)})."))