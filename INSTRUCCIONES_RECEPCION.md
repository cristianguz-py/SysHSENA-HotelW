# Módulo de recepción (RECEPCIONISTA) — instrucciones de integración

Contenido del ZIP: solo archivos NUEVOS y MODIFICADOS, con las mismas rutas de tu repositorio.

## Modificados (4)
- marketweb/models.py   → Pago.registrado_por, Pago.referencia, modelo OperacionRecepcion (solo adiciones)
- marketweb/urls.py     → ruta /recepcion/ y despacho por rol de /panel/, /huespedes/, /disponibilidad/
- marketweb/admin.py    → OperacionRecepcion de solo lectura
- marketweb/views.py    → /checkout/<id>/: el RECEPCIONISTA solo cierra con saldo cero (flujo seguro);
                          el administrador conserva el comportamiento anterior

## Nuevos (31)
- marketweb/migrations/0007_recepcion_pago_trazabilidad_y_operaciones.py  (solo ADD COLUMN y CREATE TABLE)
- marketweb/recepcion/  (permisos, operaciones, forms, vistas, urls, despacho)
- marketweb/templates/recepcion/  (21 plantillas)
- marketweb/public/css/recepcion.css
- marketweb/test_recepcion.py

## 1. Aplicar
1. Copia tu proyecto a una rama nueva (ej. `git checkout -b feature/recepcion`). No hagas commit todavía.
2. Descomprime este ZIP en la raíz del repositorio y acepta sobrescribir los 4 archivos modificados.
3. Revisa los cambios: `git diff` (solo deben cambiar models.py, urls.py, admin.py, views.py) y `git status` (archivos nuevos).

## 2. Probar localmente (SQLite en memoria, NO toca TiDB)
Linux/macOS:
    export DATABASE_URL="sqlite:///:memory:"
    python manage.py test marketweb Wbar --settings=marketweb.test_settings
Windows (PowerShell):
    $env:DATABASE_URL="sqlite:///:memory:"
    python manage.py test marketweb Wbar --settings=marketweb.test_settings
Resultado esperado: "Ran 157 tests ... OK (skipped=1)" (68 anteriores + 89 de recepción).
Comprobaciones extra:
    python manage.py check --settings=marketweb.test_settings
    python manage.py makemigrations --check --dry-run --settings=marketweb.test_settings   # "No changes detected"

## 3. Probar a mano con una BD local de desarrollo (nunca la de TiDB)
    export DATABASE_URL="sqlite:///db_local.sqlite3"   # o un MySQL local
    python manage.py migrate
    python manage.py runserver
Crea un usuario con rol RECEPCIONISTA (admin de Django o shell), inicia sesión: debe aterrizar en /recepcion/.
Recorrido sugerido: registrar huésped → disponibilidad → reservar → check-in → cargar servicio → abono → check-out.

## 4. Despliegue (SOLO cuando tú lo decidas)
- Antes: respaldo de la base de datos TiDB.
- build.sh ejecuta `migrate` automáticamente en Render: la migración 0007 se aplicará en ese deploy
  (agrega 2 columnas a `pago` y crea la tabla `operacion_recepcion`; no modifica datos existentes).
- Este paquete NO hizo commit, push, deploy ni migraciones contra TiDB.

## Limitaciones conocidas
- Probado solo con SQLite; no se probó en MySQL/TiDB (los bloqueos select_for_update no se pueden verificar en SQLite).
- Diseño no revisado en navegador real: verificar visualmente escritorio y móvil.
- Las operaciones hechas por rutas antiguas (/crear-reserva/, /checkin/) no quedan en el historial nuevo.
- Catálogo de servicios: solo Minibar y Lavandería (Alimentación, Transporte y Excursiones quedan para otra versión).
- La URL antigua habitacion/<id>/habilitar/ apunta a una vista equivocada (error previo; no se modificó).
