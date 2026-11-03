"""
Revisión de consistencia SOLO LECTURA. No modifica nada.

    python manage.py verificar_hotel

Sale con código 1 si encuentra problemas, para poder usarlo en una checklist.
"""
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand

from marketweb.catalogo_hotel import TOTAL_ESPERADO, habitaciones_oficiales
from marketweb.models import Habitacion, Reserva


class Command(BaseCommand):
    help = "Reporta inconsistencias entre habitaciones, reservas y el catálogo oficial (solo lectura)."

    def handle(self, *args, **opciones):
        problemas, info = [], []

        oficiales = {n for n, _, _ in habitaciones_oficiales()}
        numeros = list(Habitacion.objects.values_list('numero', flat=True))
        faltan = sorted(oficiales - set(numeros))
        sobran = sorted(set(numeros) - oficiales)
        if faltan:
            problemas.append(f"Faltan {len(faltan)} habitaciones oficiales: {', '.join(faltan[:10])}{'…' if len(faltan) > 10 else ''}")
        if sobran:
            problemas.append(f"Hay {len(sobran)} habitaciones fuera de la lista oficial: {', '.join(sobran[:10])}")
        if len(numeros) != len(set(numeros)):
            problemas.append("Hay números de habitación repetidos (no debería ser posible).")

        # Estados físicos que no concuerdan con las reservas
        en_casa = set(Reserva.objects.filter(estado='CHECKIN').values_list('habitacion_id', flat=True))
        for h in Habitacion.objects.select_related('tipo'):
            if h.estado == 'OCUPADA' and h.id not in en_casa:
                problemas.append(f"Habitación {h.numero} figura OCUPADA pero no tiene reserva con check-in "
                                 "(típico del flujo anterior, que la marcaba OCUPADA al reservar).")
            if h.estado in ('DISPONIBLE', 'EN_ASEO') and h.id in en_casa:
                problemas.append(f"Habitación {h.numero} figura {h.estado} pero tiene un huésped con check-in.")
            if h.tipo.precio_noche <= 0:
                problemas.append(f"Habitación {h.numero}: el tipo «{h.tipo.nombre}» no tiene precio por noche.")

        # Solapamientos entre reservas que bloquean la habitación
        por_hab = defaultdict(list)
        for r in Reserva.objects.filter(estado__in=Reserva.ESTADOS_BLOQUEANTES).order_by('fecha_entrada'):
            por_hab[r.habitacion_id].append(r)
        for lista in por_hab.values():
            maxima = None                       # (fecha_salida más lejana, reserva que la produce)
            for r in lista:
                if maxima and r.fecha_entrada < maxima[0]:
                    problemas.append(f"Reservas #{maxima[1].id} y #{r.id} se solapan en la habitación {r.habitacion_id}.")
                if maxima is None or r.fecha_salida > maxima[0]:
                    maxima = (r.fecha_salida, r)

        # Reservas vigentes: fechas, capacidad y tarifa
        for r in (Reserva.objects.filter(estado__in=Reserva.ESTADOS_BLOQUEANTES)
                .select_related('habitacion__tipo')):
            if r.fecha_salida <= r.fecha_entrada:
                problemas.append(f"Reserva #{r.id}: fecha de salida no posterior a la de entrada.")
            if r.num_huespedes > r.habitacion.tipo.capacidad:
                problemas.append(f"Reserva #{r.id}: {r.num_huespedes} huéspedes en una habitación de capacidad {r.habitacion.tipo.capacidad}.")
            esperado = r.calcular_alojamiento()
            if esperado > Decimal('0') and r.valor_total != esperado and r.estado == 'CONFIRMADA':
                info.append(f"Reserva #{r.id}: valor_total {r.valor_total} ≠ alojamiento con la tarifa actual {esperado} "
                            "(posible reserva creada con la tarifa anterior por persona).")

        self.stdout.write(f"Habitaciones en BD: {len(numeros)} (esperadas: {TOTAL_ESPERADO})")
        for i in info:
            self.stdout.write(self.style.WARNING(f"INFO: {i}"))
        if problemas:
            for p in problemas:
                self.stderr.write(self.style.ERROR(f"PROBLEMA: {p}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("OK: sin inconsistencias."))