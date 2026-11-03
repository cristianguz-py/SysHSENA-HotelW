"""
Formulario de creación de reservas.

Este formulario SOLO interpreta y sanea lo que llega por POST (tipos, formatos,
obligatoriedad, longitudes). Las reglas del hotel (fechas, capacidad,
disponibilidad, estado de la habitación, solapamientos, precio) viven en
Reserva.errores_de_reserva / services.crear_reserva, que son la autoridad.

Nota de seguridad: el formulario NO tiene campos de precio, total ni estado.
Si el navegador los envía, se ignoran; el servidor calcula el valor desde la BD.
"""
import re
import unicodedata
from types import SimpleNamespace

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .models import Huesped, Reserva, TipoHabitacion, Usuario

FORMATO_FECHA = ['%Y-%m-%d']


class ReservaForm(forms.Form):
    # --- Huésped ---
    tipo_documento = forms.ChoiceField(
        choices=Huesped.TIPOS_DOC,
        error_messages={'required': 'Selecciona el tipo de documento.',
                        'invalid_choice': 'El tipo de documento no es válido.'},
    )
    numero_documento = forms.CharField(
        max_length=30, strip=True,
        error_messages={'required': 'El número de documento es obligatorio.'},
    )
    nombres = forms.CharField(max_length=80, strip=True,
                            error_messages={'required': 'Los nombres son obligatorios.'})
    apellidos = forms.CharField(max_length=80, strip=True,
                            error_messages={'required': 'Los apellidos son obligatorios.'})
    telefono = forms.CharField(max_length=20, strip=True,
                            error_messages={'required': 'El teléfono es obligatorio.'})
    correo = forms.EmailField(
        max_length=254,
        error_messages={'required': 'El correo es obligatorio.',
                        'invalid': 'Ingresa un correo electrónico válido.'},
    )
    nacionalidad = forms.CharField(max_length=50, strip=True, required=False)
    es_extranjero = forms.BooleanField(required=False)

    # --- Estancia ---
    fecha_entrada = forms.DateField(
        input_formats=FORMATO_FECHA,
        error_messages={'required': 'La fecha de entrada es obligatoria.',
                        'invalid': 'La fecha de entrada no es válida.'},
    )
    fecha_salida = forms.DateField(
        input_formats=FORMATO_FECHA,
        error_messages={'required': 'La fecha de salida es obligatoria.',
                        'invalid': 'La fecha de salida no es válida.'},
    )
    tipo_habitacion = forms.ModelChoiceField(
        queryset=TipoHabitacion.objects.all(),
        error_messages={'required': 'Selecciona un tipo de habitación.',
                        'invalid_choice': 'El tipo de habitación no es válido.'},
    )
    habitacion = forms.IntegerField(
        min_value=1,
        error_messages={'required': 'Selecciona una habitación disponible.',
                        'invalid': 'La habitación seleccionada no es válida.',
                        'min_value': 'La habitación seleccionada no es válida.'},
    )
    num_huespedes = forms.IntegerField(
        min_value=1,
        error_messages={'required': 'Indica el número de huéspedes.',
                        'invalid': 'El número de huéspedes no es válido.',
                        'min_value': 'Debe haber al menos un huésped.'},
    )
    canal_reserva = forms.ChoiceField(
        choices=Reserva.CANALES,
        error_messages={'required': 'Selecciona el canal de origen.',
                        'invalid_choice': 'El canal de origen no es válido.'},
    )

    def clean_nacionalidad(self):
        return self.cleaned_data.get('nacionalidad') or 'Colombiana'

    def datos_huesped(self):
        """Diccionario listo para services.crear_reserva (solo campos del modelo Huesped)."""
        cd = self.cleaned_data
        return {
            'tipo_documento': cd['tipo_documento'],
            'numero_documento': cd['numero_documento'],
            'nombres': cd['nombres'],
            'apellidos': cd['apellidos'],
            'telefono': cd['telefono'],
            'correo': cd['correo'],
            'nacionalidad': cd['nacionalidad'],
            'es_extranjero': cd['es_extranjero'],
        }


# ======================================================================
# REGISTRO DE HUÉSPEDES (cuenta de acceso → tabla `usuario`, rol CLIENTE)
# ======================================================================
# Nombres personales: sin regex rígida. Se acepta cualquier letra Unicode (con
# sus marcas combinantes: tildes, diéresis, etc.), espacios, guion y apóstrofe.
# Se rechazan números, símbolos, emojis y caracteres de control/formato
# (p. ej. los invisibles o de dirección de texto). No se cambian mayúsculas:
# «de la Cruz» o «McDonald» se guardan tal como se escribieron.
_PUNTUACION_NOMBRE = {"'", '\u2019', '-'}


def normalizar_nombre_personal(valor):
    """NFC + espacios colapsados. No toca mayúsculas/minúsculas."""
    return ' '.join(unicodedata.normalize('NFC', valor or '').split())


def validar_nombre_personal(valor, etiqueta):
    if not valor:
        return
    for ch in valor:
        if ch == ' ' or ch in _PUNTUACION_NOMBRE:
            continue
        if unicodedata.category(ch)[0] not in ('L', 'M'):
            raise ValidationError(
                f'{etiqueta}: usa solo letras, espacios, guiones o apóstrofes.', code='caracteres')
    if unicodedata.category(valor[0])[0] != 'L' or unicodedata.category(valor[-1])[0] not in ('L', 'M'):
        raise ValidationError(f'{etiqueta}: debe empezar y terminar con una letra.', code='bordes')


class RegistroHuespedForm(forms.Form):
    """Crea una cuenta de huésped: Usuario con rol CLIENTE. No existe ningún campo
    de rol, estado ni permisos: lo que el navegador envíe de más se ignora."""

    MSG_CORREO_DUPLICADO = 'Ya existe una cuenta con este correo. Inicia sesión o usa otro correo.'

    nombres = forms.CharField(
        max_length=80, strip=True,
        error_messages={'required': 'Escribe tu nombre.',
                        'max_length': 'El nombre no puede superar los 80 caracteres.'},
    )
    apellidos = forms.CharField(
        max_length=80, strip=True,
        error_messages={'required': 'Escribe tus apellidos.',
                        'max_length': 'Los apellidos no pueden superar los 80 caracteres.'},
    )
    correo = forms.EmailField(
        max_length=254,
        error_messages={'required': 'Escribe tu correo electrónico.',
                        'invalid': 'Ingresa un correo electrónico válido.',
                        'max_length': 'El correo no puede superar los 254 caracteres.'},
    )
    # strip=False: los espacios de una contraseña son parte de la contraseña.
    # max_length evita enviar contraseñas enormes para encarecer el hash.
    contrasena = forms.CharField(
        max_length=128, strip=False, widget=forms.PasswordInput(render_value=False),
        error_messages={'required': 'Escribe una contraseña.',
                        'max_length': 'La contraseña no puede superar los 128 caracteres.'},
    )
    confirmar_contrasena = forms.CharField(
        max_length=128, strip=False, widget=forms.PasswordInput(render_value=False),
        error_messages={'required': 'Repite la contraseña.'},
    )

    def clean_nombres(self):
        valor = normalizar_nombre_personal(self.cleaned_data['nombres'])
        validar_nombre_personal(valor, 'Nombre')
        return valor

    def clean_apellidos(self):
        valor = normalizar_nombre_personal(self.cleaned_data['apellidos'])
        validar_nombre_personal(valor, 'Apellidos')
        return valor

    def clean_correo(self):
        # Política de normalización: sin espacios y TODO en minúsculas. La
        # comparación de duplicados es insensible a mayúsculas (también contra
        # cuentas antiguas guardadas con otro formato).
        correo = self.cleaned_data['correo'].strip().lower()
        if Usuario.objects.filter(correo__iexact=correo).exists():
            raise ValidationError(self.MSG_CORREO_DUPLICADO, code='duplicado')
        return correo

    def clean(self):
        cd = super().clean()
        clave, repetida = cd.get('contrasena'), cd.get('confirmar_contrasena')
        if clave:
            # Los validadores de AUTH_PASSWORD_VALIDATORS (longitud, comunes,
            # solo numérica, parecido a datos personales). UserAttributeSimilarity
            # lee username/first_name/last_name/email; se los damos desde el formulario.
            correo = cd.get('correo') or ''
            datos = SimpleNamespace(
                username=correo.split('@')[0], first_name=cd.get('nombres', ''),
                last_name=cd.get('apellidos', ''), email=correo,
            )
            try:
                password_validation.validate_password(clave, datos)
            except ValidationError as e:
                self.add_error('contrasena', e)
        if clave and repetida and clave != repetida:
            self.add_error('confirmar_contrasena', 'Las contraseñas no coinciden.')
        return cd

    def guardar(self):
        """Inserta el usuario en una transacción. Si otra solicitud creó el mismo
        correo entre la validación y el INSERT, la restricción UNIQUE de la base de
        datos lo detiene: se informa como error del campo y devuelve None."""
        cd = self.cleaned_data
        try:
            with transaction.atomic():
                return Usuario.objects.create(
                    nombres=cd['nombres'], apellidos=cd['apellidos'], correo=cd['correo'],
                    contrasena=make_password(cd['contrasena']),
                    rol='CLIENTE', activo=True,
                )
        except IntegrityError:
            self.add_error('correo', self.MSG_CORREO_DUPLICADO)
            return None


# ======================================================================
# CONFIGURACIÓN DE CUENTA: cambio de contraseña (Usuario propio, no auth.User)
# ======================================================================
class CambiarContrasenaForm(forms.Form):
    """Equivale a PasswordChangeForm para el modelo Usuario: valida la contraseña
    actual, aplica AUTH_PASSWORD_VALIDATORS a la nueva y la guarda con make_password."""

    actual = forms.CharField(
        max_length=128, strip=False, widget=forms.PasswordInput(render_value=False),
        error_messages={'required': 'Escribe tu contraseña actual.'},
    )
    nueva = forms.CharField(
        max_length=128, strip=False, widget=forms.PasswordInput(render_value=False),
        error_messages={'required': 'Escribe la nueva contraseña.',
                        'max_length': 'La contraseña no puede superar los 128 caracteres.'},
    )
    confirmar = forms.CharField(
        max_length=128, strip=False, widget=forms.PasswordInput(render_value=False),
        error_messages={'required': 'Repite la nueva contraseña.'},
    )

    def __init__(self, usuario, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario

    def clean_actual(self):
        actual = self.cleaned_data['actual']
        if not check_password(actual, self.usuario.contrasena or ''):
            raise ValidationError('La contraseña actual no es correcta.', code='incorrecta')
        return actual

    def clean(self):
        cd = super().clean()
        nueva, confirmar = cd.get('nueva'), cd.get('confirmar')
        if nueva:
            datos = SimpleNamespace(
                username=(self.usuario.correo or '').split('@')[0], first_name=self.usuario.nombres,
                last_name=self.usuario.apellidos, email=self.usuario.correo,
            )
            try:
                password_validation.validate_password(nueva, datos)
            except ValidationError as e:
                self.add_error('nueva', e)
            else:
                if cd.get('actual') and nueva == cd['actual']:
                    self.add_error('nueva', 'La nueva contraseña debe ser distinta de la actual.')
        if nueva and confirmar and nueva != confirmar:
            self.add_error('confirmar', 'Las contraseñas no coinciden.')
        return cd

    def guardar(self):
        self.usuario.contrasena = make_password(self.cleaned_data['nueva'])
        self.usuario.save(update_fields=['contrasena'])
        return self.usuario


# ======================================================================
# RESERVA DEL CLIENTE (cuenta web). Fechas y tipo NO vienen de aquí: salen de la
# búsqueda guardada en la sesión y se revalidan en el servidor. Tampoco hay campos
# de precio, estado, habitación ni usuario.
# ======================================================================
class ReservaClienteForm(forms.Form):
    tipo_documento = forms.ChoiceField(
        choices=Huesped.TIPOS_DOC,
        error_messages={'required': 'Selecciona el tipo de documento.',
                        'invalid_choice': 'El tipo de documento no es válido.'},
    )
    numero_documento = forms.CharField(
        max_length=30, strip=True,
        error_messages={'required': 'El número de documento es obligatorio.'},
    )
    telefono = forms.CharField(
        max_length=20, strip=True,
        error_messages={'required': 'El teléfono es obligatorio.'},
    )
    nacionalidad = forms.CharField(max_length=50, strip=True, required=False)
    es_extranjero = forms.BooleanField(required=False)
    num_huespedes = forms.IntegerField(
        min_value=1,
        error_messages={'required': 'Indica el número de huéspedes.',
                        'invalid': 'El número de huéspedes no es válido.',
                        'min_value': 'Debe haber al menos un huésped.'},
    )

    _RE_DOC = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.\-]{3,29}$')
    _RE_TEL = re.compile(r'^\+?[0-9][0-9 ()\-]{6,19}$')

    def __init__(self, *args, tipo=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tipo = tipo

    def clean_numero_documento(self):
        valor = self.cleaned_data['numero_documento']
        if not self._RE_DOC.match(valor):
            raise ValidationError('El documento debe tener de 4 a 30 letras, números, puntos o guiones, sin espacios.')
        return valor

    def clean_telefono(self):
        valor = self.cleaned_data['telefono']
        if not self._RE_TEL.match(valor):
            raise ValidationError('Ingresa un teléfono válido (solo números, espacios, paréntesis, guiones o + inicial).')
        return valor

    def clean_nacionalidad(self):
        valor = normalizar_nombre_personal(self.cleaned_data.get('nacionalidad'))
        if valor:
            validar_nombre_personal(valor, 'Nacionalidad')
        return valor or 'Colombiana'

    def clean_num_huespedes(self):
        n = self.cleaned_data['num_huespedes']
        if self.tipo is not None and n > self.tipo.capacidad:
            raise ValidationError(
                f'{self.tipo.nombre} admite máximo {self.tipo.capacidad} huéspedes y se indicaron {n}.')
        return n

    def datos_huesped(self, usuario):
        """Nombre y correo salen de la cuenta autenticada, no del navegador."""
        cd = self.cleaned_data
        return {
            'tipo_documento': cd['tipo_documento'],
            'numero_documento': cd['numero_documento'],
            'nombres': usuario.nombres,
            'apellidos': usuario.apellidos,
            'telefono': cd['telefono'],
            'correo': usuario.correo,
            'nacionalidad': cd['nacionalidad'],
            'es_extranjero': cd['es_extranjero'],
        }