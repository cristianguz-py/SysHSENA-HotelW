"""Despacho por rol de las rutas históricas /panel/, /huespedes/ y /disponibilidad/.
El administrador conserva EXACTAMENTE su vista de siempre; el recepcionista pasa a su panel."""
from functools import wraps

from django.shortcuts import redirect

from .permisos import ROL_RECEPCION


def despachar(vista_admin, destino_recepcion):
    @wraps(vista_admin)
    def envoltura(request, *args, **kwargs):
        if request.session.get('usuario_rol') == ROL_RECEPCION:
            return redirect(destino_recepcion)
        return vista_admin(request, *args, **kwargs)
    return envoltura
