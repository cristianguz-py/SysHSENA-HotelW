from django import forms

from marketweb.models import Reserva

from . import services
from .models import CategoriaProducto, Pedido, Producto


class _Estilo:
    """Añade las clases CSS del panel a los widgets (solo HTML/CSS, sin JavaScript)."""
    def _estilizar(self):
        for campo in self.fields.values():
            w = campo.widget
            if isinstance(w, forms.CheckboxInput):
                continue
            w.attrs.setdefault('class', 'wb-input')


class CategoriaForm(_Estilo, forms.ModelForm):
    class Meta:
        model = CategoriaProducto
        fields = ['nombre', 'descripcion', 'orden', 'activa']

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._estilizar()


class ProductoForm(_Estilo, forms.ModelForm):
    class Meta:
        model = Producto
        fields = ['categoria', 'nombre', 'descripcion', 'presentacion', 'informacion', 'precio', 'imagen',
                  'contiene_alcohol', 'activo', 'disponible', 'stock']
        widgets = {'descripcion': forms.Textarea(attrs={'rows': 3}), 'informacion': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields['categoria'].queryset = CategoriaProducto.objects.order_by('orden', 'nombre')
        self._estilizar()

    def clean_precio(self):
        precio = self.cleaned_data['precio']
        if precio <= 0 or precio > 9_999_999:
            raise forms.ValidationError("Ingresa un precio mayor que 0 y menor que 10.000.000.")
        return precio


class ConfirmarPedidoForm(_Estilo, forms.Form):
    reserva = forms.ModelChoiceField(queryset=Reserva.objects.none(), label="Habitación de entrega",
                                     empty_label=None)
    declaracion_mayoria_edad = forms.BooleanField(
        required=False, label="Declaro que soy mayor de edad. Entiendo que el personal verificará mi documento al entregar.")
    notas = forms.CharField(required=False, max_length=300, label="Notas para el pedido (opcional)",
                            widget=forms.TextInput(attrs={'placeholder': 'Ej.: sin hielo'}))
    token = forms.CharField(widget=forms.HiddenInput, max_length=40)

    def __init__(self, *args, usuario, **kwargs):
        super().__init__(*args, **kwargs)
        # Las ÚNICAS opciones válidas son las reservas del propio usuario con estadía activa.
        self.fields['reserva'].queryset = services.reservas_habilitadas(usuario)
        self.fields['reserva'].label_from_instance = (
            lambda r: f"Habitación {r.habitacion.numero} · {r.habitacion.tipo.nombre} (reserva #{r.pk})")
        self._estilizar()


class EntregaForm(_Estilo, forms.Form):
    codigo = forms.CharField(max_length=6, min_length=6, label="Código de entrega del huésped")
    edad_verificada = forms.BooleanField(required=False, label="Verifiqué con documento original que el huésped es mayor de edad")
    documento = forms.ChoiceField(required=False, label="Documento verificado",
                                  choices=[('', '—')] + Pedido.DOCUMENTOS)
    forma_cobro = forms.ChoiceField(label="Cobro", choices=[
        (Pedido.PAGO_COBRADO, 'Cobrado ahora en la habitación'),
        (Pedido.PAGO_CARGO_CUENTA, 'Cargar a la cuenta de la habitación (se paga en el check-out)')])
    metodo_pago = forms.ChoiceField(required=False, label="Medio de pago", choices=[
        ('', '—'), ('EFECTIVO', 'Efectivo'), ('DATAFONO', 'Datáfono del hotel'), ('TRANSFERENCIA', 'Otro medio autorizado por el hotel')])
    referencia = forms.CharField(required=False, max_length=40, label="N.º de comprobante (opcional)")

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._estilizar()


class CancelarForm(_Estilo, forms.Form):
    motivo = forms.CharField(max_length=200, required=False, label="Motivo")

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._estilizar()
