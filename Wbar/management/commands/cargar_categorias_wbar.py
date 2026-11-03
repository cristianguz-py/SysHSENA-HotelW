"""Crea (si no existen) las categorías iniciales del menú del W Bar. NO crea productos ni
borra nada: es seguro ejecutarlo varias veces. Después el administrador las edita desde la web."""
from django.core.management.base import BaseCommand

from Wbar.models import CategoriaProducto

CATEGORIAS = [
    'Cervezas', 'Cócteles', 'Whisky', 'Tequila', 'Ron', 'Aguardiente', 'Vodka', 'Vinos', 'Licores',
    'Bebidas sin alcohol', 'Agua', 'Gaseosas', 'Snacks', 'Comidas ligeras',
]


class Command(BaseCommand):
    help = "Crea las categorías iniciales de W Bar (idempotente)."

    def handle(self, *args, **opciones):
        creadas = 0
        for i, nombre in enumerate(CATEGORIAS, start=1):
            _, nueva = CategoriaProducto.objects.get_or_create(nombre=nombre, defaults={'orden': i * 10})
            creadas += nueva
        self.stdout.write(self.style.SUCCESS(f"Categorías creadas: {creadas}. Ya existentes: {len(CATEGORIAS) - creadas}."))
