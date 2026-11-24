"""Control de acceso del módulo de recepción.

Dos capas (ocultar botones NO cuenta):
  1. requiere_recepcion: decorador de las vistas. Relee la cuenta de la BD en CADA petición.
  2. exigir_personal_recepcion: la llamada que abre CADA operación del backend, de modo que
     ninguna función de operaciones.py funciona si se invoca sin un usuario autorizado.
El rol sale siempre de la BD o de la sesión del servidor; nunca de datos enviados por el navegador.
"""
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from ..models import Usuario

ROL_RECEPCION = 'RECEPCIONISTA'
ROL_ADMIN = 'ADMIN_GERENTE'
ROLES_RECEPCION = (ROL_RECEPCION, ROL_ADMIN)
TEXTO_ROL = {ROL_RECEPCION: 'Recepcionista', ROL_ADMIN: 'Administrador / Gerente'}


def exigir_personal_recepcion(usuario):
    """Devuelve el usuario releído de la BD o lanza PermissionDenied (activo y con rol de recepción)."""
    if usuario is None or getattr(usuario, 'pk', None) is None:
        raise PermissionDenied('Operación sin usuario autenticado.')
    vigente = Usuario.objects.filter(pk=usuario.pk, activo=True, rol__in=ROLES_RECEPCION).first()
    if vigente is None:
        raise PermissionDenied('Tu cuenta no tiene permiso para operar en recepción.')
    return vigente


def es_admin(usuario):
    return getattr(usuario, 'rol', None) == ROL_ADMIN


def requiere_recepcion(vista):
    """Sin sesión → login. Rol distinto (p. ej. CLIENTE) → 403. Cuenta desactivada → sesión cerrada."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if 'usuario_id' not in request.session:
            return redirect('login')
        if request.session.get('usuario_rol') not in ROLES_RECEPCION:
            raise PermissionDenied
        usuario = Usuario.objects.filter(pk=request.session['usuario_id'], activo=True,
                                         rol__in=ROLES_RECEPCION).first()
        if usuario is None:
            request.session.flush()
            return redirect('login')
        request.usuario = usuario
        return vista(request, *args, **kwargs)
    return envoltura
