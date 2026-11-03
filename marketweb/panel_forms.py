"""Formularios del panel de gestión (HTML normal + validación en el servidor, sin JavaScript)."""
from django import forms
from django.db.models import Max
from django.utils import timezone

from .models import Habitacion, Huesped, Reserva, Servicio, TipoHabitacion


class FechaInput(forms.DateInput):
    input_type = 'date'

    def __init__(self, **kw):
        super().__init__(format='%Y-%m-%d', **kw)


class ReservaEdicionForm(forms.Form):
    habitacion = forms.ModelChoiceField(queryset=Habitacion.objects.none(), label="Habitación")
    fecha_entrada = forms.DateField(widget=FechaInput(), label="Fecha de entrada")
    fecha_salida = forms.DateField(widget=FechaInput(), label="Fecha de salida")
    num_huespedes = forms.IntegerField(min_value=1, max_value=20, label="Cantidad de huéspedes")
    canal_reserva = forms.ChoiceField(choices=Reserva.CANALES, label="Canal de la reserva")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['habitacion'].queryset = (
            Habitacion.objects.filter(activa=True).select_related('tipo').order_by('piso', 'numero'))
        self.fields['habitacion'].label_from_instance = (
            lambda h: f"{h.numero} · {h.tipo.nombre} (máx. {h.tipo.capacidad})")


class HuespedForm(forms.ModelForm):
    """Solo datos de contacto. El documento (identidad) no se edita desde aquí."""
    class Meta:
        model = Huesped
        fields = ['nombres', 'apellidos', 'telefono', 'correo', 'nacionalidad', 'es_extranjero']
        labels = {'nombres': 'Nombres', 'apellidos': 'Apellidos', 'telefono': 'Teléfono',
                  'correo': 'Correo electrónico', 'nacionalidad': 'Nacionalidad',
                  'es_extranjero': 'Es extranjero (exento de IVA)'}


class HabitacionForm(forms.ModelForm):
    ESTADOS_MANUALES = [(v, e) for v, e in Habitacion.ESTADOS if v != Habitacion.OCUPADA]

    class Meta:
        model = Habitacion
        fields = ['numero', 'piso', 'tipo', 'estado', 'activa']
        labels = {'numero': 'Número', 'piso': 'Piso', 'tipo': 'Tipo de habitación',
                  'estado': 'Estado operativo', 'activa': 'Habitación activa'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['estado'].choices = self.ESTADOS_MANUALES
        if self.instance.pk and self.instance.estado == Habitacion.OCUPADA:
            # «Ocupada» solo la fija el check-in; no se cambia a mano.
            del self.fields['estado']
        self.fields['tipo'].queryset = TipoHabitacion.objects.order_by('nombre')

    def clean_numero(self):
        numero = (self.cleaned_data['numero'] or '').strip()
        if not numero:
            raise forms.ValidationError("Indica el número de la habitación.")
        return numero

    def clean_piso(self):
        piso = self.cleaned_data['piso']
        if piso < 0 or piso > 99:
            raise forms.ValidationError("El piso debe estar entre 0 y 99.")
        return piso


class TipoHabitacionForm(forms.ModelForm):
    class Meta:
        model = TipoHabitacion
        fields = ['capacidad', 'precio_noche', 'descripcion']
        labels = {'capacidad': 'Capacidad (huéspedes)', 'precio_noche': 'Precio por habitación y noche (COP)',
                  'descripcion': 'Descripción'}

    def clean_capacidad(self):
        capacidad = self.cleaned_data['capacidad']
        if capacidad < 1 or capacidad > 20:
            raise forms.ValidationError("La capacidad debe estar entre 1 y 20.")
        mayor = (Reserva.objects.filter(habitacion__tipo=self.instance,
                                        estado__in=('PENDIENTE', 'CONFIRMADA', 'CHECKIN'),
                                        fecha_salida__gte=timezone.localdate())
                 .aggregate(m=Max('num_huespedes'))['m'] or 0)
        if capacidad < mayor:
            raise forms.ValidationError(
                f"Hay reservas vigentes con {mayor} huéspedes; la capacidad no puede ser menor.")
        return capacidad

    def clean_precio_noche(self):
        precio = self.cleaned_data['precio_noche']
        if precio <= 0:
            raise forms.ValidationError("El precio debe ser mayor que cero.")
        return precio


class ConsumoForm(forms.Form):
    reserva = forms.ModelChoiceField(queryset=Reserva.objects.none(), label="Reserva con huésped en casa")
    servicio = forms.ModelChoiceField(queryset=Servicio.objects.none(), label="Servicio")
    cantidad = forms.IntegerField(min_value=1, max_value=99, initial=1, label="Cantidad")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['reserva'].queryset = (Reserva.objects.filter(estado='CHECKIN')
                                           .select_related('huesped', 'habitacion').order_by('habitacion__numero'))
        self.fields['reserva'].label_from_instance = (
            lambda r: f"Hab. {r.habitacion.numero} · {r.huesped.nombres} {r.huesped.apellidos}")
        self.fields['servicio'].queryset = Servicio.objects.order_by('categoria', 'nombre')
        self.fields['servicio'].label_from_instance = lambda s: f"{s.nombre} (${int(s.precio):,})".replace(',', '.')
