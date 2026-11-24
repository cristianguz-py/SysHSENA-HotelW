"""Formularios de recepción (HTML normal, validación en el servidor). Ningún formulario tiene campos de
rol, estado, precio ni totales: lo que el navegador envíe de más se ignora."""
import re
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from ..forms import ReservaForm, normalizar_nombre_personal, validar_nombre_personal
from ..models import Huesped, Pago, Servicio
from ..panel_forms import FechaInput

_DOC = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\-]{3,29}$')
_TEL = re.compile(r'^\+?[0-9][0-9 ()\-]{5,19}$')


class HuespedAltaForm(forms.ModelForm):
    class Meta:
        model = Huesped
        fields = ['tipo_documento', 'numero_documento', 'nombres', 'apellidos', 'telefono',
                  'correo', 'nacionalidad', 'es_extranjero']
        labels = {'tipo_documento': 'Tipo de documento', 'numero_documento': 'Número de documento',
                  'nombres': 'Nombres', 'apellidos': 'Apellidos', 'telefono': 'Teléfono',
                  'correo': 'Correo electrónico', 'nacionalidad': 'Nacionalidad',
                  'es_extranjero': 'Es extranjero (exento de IVA)'}

    def clean_numero_documento(self):
        doc = (self.cleaned_data['numero_documento'] or '').strip()
        if not _DOC.match(doc):
            raise ValidationError("Usa de 4 a 30 letras, números o guiones, sin espacios.")
        if Huesped.objects.filter(numero_documento=doc).exists():
            raise ValidationError("Ya existe un huésped registrado con este documento.")
        return doc

    def _nombre(self, campo, etiqueta):
        valor = normalizar_nombre_personal(self.cleaned_data[campo])
        if not valor:
            raise ValidationError(f"{etiqueta}: es obligatorio.")
        validar_nombre_personal(valor, etiqueta)
        return valor

    def clean_nombres(self):
        return self._nombre('nombres', 'Nombres')

    def clean_apellidos(self):
        return self._nombre('apellidos', 'Apellidos')

    def clean_telefono(self):
        tel = (self.cleaned_data['telefono'] or '').strip()
        if not _TEL.match(tel):
            raise ValidationError("Teléfono no válido (solo números, +, espacios, guiones o paréntesis).")
        return tel

    def clean_nacionalidad(self):
        return (self.cleaned_data.get('nacionalidad') or '').strip() or 'Colombiana'

    def datos(self):
        return {k: self.cleaned_data[k] for k in self.Meta.fields}


class ReservaRecepcionForm(ReservaForm):
    """Mismo formulario y mismas reglas de ReservaForm; solo cambia la presentación (fechas con selector,
    tipo y habitación fijados por la elección hecha en Disponibilidad)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for nombre in ('fecha_entrada', 'fecha_salida'):
            self.fields[nombre].widget = FechaInput()
        self.fields['tipo_habitacion'].widget = forms.HiddenInput()
        self.fields['habitacion'].widget = forms.HiddenInput()
        self.fields['es_extranjero'].label = 'Es extranjero (exento de IVA)'
        etiquetas = {
            'tipo_documento': 'Tipo de documento', 'numero_documento': 'Número de documento',
            'nombres': 'Nombres', 'apellidos': 'Apellidos', 'telefono': 'Teléfono',
            'correo': 'Correo electrónico', 'nacionalidad': 'Nacionalidad',
            'fecha_entrada': 'Fecha de entrada', 'fecha_salida': 'Fecha de salida',
            'num_huespedes': 'Número de huéspedes', 'canal_reserva': 'Canal de la reserva'}
        for k, v in etiquetas.items():
            self.fields[k].label = v


class PagoForm(forms.Form):
    reserva = forms.IntegerField(min_value=1, label="Número de la reserva (sin «RW-»)",
                                 error_messages={'required': 'Indica la reserva.', 'invalid': 'Número de reserva no válido.'})
    monto = forms.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'), label="Valor (COP)",
                               error_messages={'required': 'Indica el valor.', 'invalid': 'Valor no válido.',
                                               'min_value': 'El valor debe ser mayor que cero.'},
                               help_text="Solo números; decimales con punto. Sin separadores de miles.")
    metodo_pago = forms.ChoiceField(choices=Pago.METODOS, label="Método de pago")
    referencia = forms.CharField(max_length=60, required=False, label="Referencia o comprobante",
                                 help_text="Opcional: n.º de transferencia, voucher del datáfono, etc.")


class ServicioForm(forms.Form):
    reserva = forms.IntegerField(min_value=1, label="Número de la reserva (sin «RW-»)",
                                 error_messages={'required': 'Indica la reserva.', 'invalid': 'Número de reserva no válido.'})
    servicio = forms.ModelChoiceField(queryset=Servicio.objects.none(), label="Servicio")
    cantidad = forms.IntegerField(min_value=1, max_value=99, initial=1, label="Cantidad")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['servicio'].queryset = Servicio.objects.order_by('categoria', 'nombre')
        self.fields['servicio'].label_from_instance = lambda s: f"{s.nombre} — ${int(s.precio):,}".replace(',', '.')


class CheckinForm(forms.Form):
    observaciones = forms.CharField(max_length=200, required=False, label="Observaciones (opcional)",
                                    widget=forms.Textarea(attrs={'rows': 3, 'maxlength': 200}))


class CheckoutForm(forms.Form):
    monto = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False,
                               label="Pago final (COP)", help_text="Déjalo vacío si el saldo ya es cero.")
    metodo_pago = forms.ChoiceField(choices=[('', '—')] + list(Pago.METODOS), required=False, label="Método de pago")
    referencia = forms.CharField(max_length=60, required=False, label="Referencia o comprobante")

    def clean(self):
        datos = super().clean()
        if datos.get('monto') and not datos.get('metodo_pago'):
            self.add_error('metodo_pago', 'Selecciona el método del pago final.')
        return datos
