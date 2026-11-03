"""
Catálogo oficial del Hotel W (Ficha Maestra v1.0 + numeración confirmada).

Es la ÚNICA fuente de los datos de carga inicial. Lo usan:
  - python manage.py cargar_habitaciones
  - python manage.py verificar_hotel
  - los tests

Si cambia la ficha, se cambia aquí y se vuelve a ejecutar el comando de carga.
"""
from decimal import Decimal

# nombre -> (capacidad, precio por habitación por noche, descripción)
TIPOS = {
    'W Essential': (2, Decimal('320000.00'),
                    'Categoría estándar. Cama King, baño privado, aire acondicionado, Smart TV, '
                    'Wi-Fi, escritorio, minibar y caja de seguridad.'),
    'W Comfort': (2, Decimal('390000.00'),
                  'Categoría superior. Cama King, balcón, vista exterior, zona de descanso, '
                  'cafetera, minibar y caja de seguridad.'),
    'W Premium': (3, Decimal('480000.00'),
                  'Cama King y sofá cama, baño amplio, balcón, vista panorámica, minibar premium '
                  'y zona de trabajo.'),
    'W Ocean View': (2, Decimal('560000.00'),
                     'Vista al mar, balcón privado, cama King, baño premium con bañera, cafetera '
                     'y Room Service.'),
    'W Executive': (2, Decimal('650000.00'),
                    'Escritorio ejecutivo, zona de trabajo, sala pequeña, baño premium, acceso a '
                    'zona ejecutiva y desayuno incluido.'),
    'W Junior Suite': (3, Decimal('780000.00'),
                       'Dormitorio independiente, sala, cama King, baño premium con bañera, balcón '
                       'y zona de trabajo.'),
    'W Suite': (4, Decimal('950000.00'),
                'Habitación principal con cama King, sala independiente, comedor pequeño, '
                'vestidor, balcón y vista panorámica.'),
    'W Presidential Suite': (4, Decimal('1500000.00'),
                             'Habitación King, sala de lujo, comedor, cocina pequeña, vestidor, '
                             'jacuzzi, terraza privada y servicio personalizado.'),
    'W Royal Suite': (6, Decimal('2500000.00'),
                      'La habitación más exclusiva: dos habitaciones, sala, comedor, cocina, '
                      'terraza privada, jacuzzi, bar privado y servicio personalizado.'),
}

# (número inicial, número final, tipo) — rangos inclusivos
RANGOS = [
    (401, 425, 'W Essential'),
    (501, 505, 'W Essential'),
    (506, 525, 'W Comfort'),
    (601, 605, 'W Comfort'),
    (606, 625, 'W Premium'),
    (701, 715, 'W Ocean View'),
    (716, 725, 'W Executive'),
    (801, 808, 'W Junior Suite'),
    (809, 815, 'W Suite'),
    (816, 819, 'W Presidential Suite'),
    (820, 820, 'W Royal Suite'),
]

TOTAL_ESPERADO = 120

HOTEL = {
    'nombre': 'Hotel W',
    'direccion': 'Riohacha, La Guajira',
}


def habitaciones_oficiales():
    """Devuelve [(numero:str, piso:int, nombre_tipo:str), ...] — 120 elementos."""
    lista = []
    for inicio, fin, tipo in RANGOS:
        for n in range(inicio, fin + 1):
            lista.append((str(n), n // 100, tipo))
    assert len(lista) == TOTAL_ESPERADO, f'El catálogo define {len(lista)} habitaciones, no {TOTAL_ESPERADO}'
    assert len({n for n, _, _ in lista}) == TOTAL_ESPERADO, 'Hay números repetidos en el catálogo'
    return lista