"""Control de acceso de W Bar. Se comprueba en CADA petición y la cuenta se relee de la BD
(una cuenta desactivada o con otro rol pierde el acceso al instante). Ocultar un botón no
reemplaza esta comprobación."""
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from marketweb.models import Usuario

ADMIN = 'ADMIN_GERENTE'
RECEPCION = 'RECEPCIONISTA'
CLIENTE = 'CLIENTE'
TEXTO_ROL = {ADMIN: 'Administrador / Gerente', RECEPCION: 'Recepcionista'}


def requiere_rol(*roles):
    def decorador(vista):
        @wraps(vista)
        def envoltura(request, *args, **kwargs):
            usuario_id = request.session.get('usuario_id')
            if not usuario_id:
                return redirect('login')
            if request.session.get('usuario_rol') not in roles:
                raise PermissionDenied
            usuario = Usuario.objects.filter(pk=usuario_id, activo=True, rol__in=roles).first()
            if usuario is None:
                request.session.flush()
                return redirect('login')
            request.usuario = usuario
            return vista(request, *args, **kwargs)
        return envoltura
    return decorador


# Operación de pedidos: administrador y recepcionista. Catálogo y estadísticas: solo administrador.
requiere_operacion = requiere_rol(ADMIN, RECEPCION)
requiere_administrador = requiere_rol(ADMIN)
