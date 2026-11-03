"""Validaciones de las fotografías de los productos del W Bar."""
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator

EXTENSIONES_PERMITIDAS = ['jpg', 'jpeg', 'png', 'webp']
TAMANO_MAXIMO_MB = 3

validar_extension_imagen = FileExtensionValidator(allowed_extensions=EXTENSIONES_PERMITIDAS)


def validar_tamano_imagen(archivo):
    """Rechaza fotografías de más de TAMANO_MAXIMO_MB megabytes."""
    if archivo.size > TAMANO_MAXIMO_MB * 1024 * 1024:
        raise ValidationError(f"La fotografía pesa demasiado: el máximo es {TAMANO_MAXIMO_MB} MB.")
