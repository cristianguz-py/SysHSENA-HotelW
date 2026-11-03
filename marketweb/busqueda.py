"""Formulario de búsqueda de disponibilidad (página de inicio pública).

Solo valida y entrega datos limpios: NO consulta disponibilidad real, NO crea
reservas y NO bloquea habitaciones. Todas las reglas se aplican en el servidor;
los atributos HTML (required, min) son solo ayuda para el navegador.
"""
from datetime import timedelta

from django import forms
from django.utils import timezone

from .models import TipoHabitacion

CLAVE_SESION = 'busqueda_hotel'


class BusquedaDisponibilidadForm(forms.Form):
    llegada = forms.DateField(
        label='Fecha de llegada',
        input_formats=['%Y-%m-%d'],
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'reserva__input'}, format='%Y-%m-%d'),
        error_messages={
            'required': 'Indica la fecha de llegada.',
            'invalid': 'La fecha de llegada no es válida.',
        },
    )
    salida = forms.DateField(
        label='Fecha de salida',
        input_formats=['%Y-%m-%d'],
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'reserva__input'}, format='%Y-%m-%d'),
        error_messages={
            'required': 'Indica la fecha de salida.',
            'invalid': 'La fecha de salida no es válida.',
        },
    )
    tipo = forms.ModelChoiceField(
        label='Tipo de habitación',
        queryset=TipoHabitacion.objects.none(),
        empty_label='Selecciona un tipo',
        widget=forms.Select(attrs={'class': 'reserva__input reserva__select'}),
        error_messages={
            'required': 'Selecciona un tipo de habitación.',
            'invalid_choice': 'Selecciona un tipo de habitación válido.',
        },
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        hoy = timezone.localdate()
        self.hoy = hoy
        self.fields['llegada'].widget.attrs['min'] = hoy.isoformat()
        # Mínimo del navegador para la salida: mañana. La regla real (salida > llegada) la aplica clean().
        self.fields['salida'].widget.attrs['min'] = (hoy + timedelta(days=1)).isoformat()
        # Solo tipos reales con al menos una habitación activa, de menor a mayor precio.
        self.fields['tipo'].queryset = (
            TipoHabitacion.objects.filter(habitaciones__activa=True).distinct().order_by('precio_noche', 'id')
        )
        self.fields['tipo'].label_from_instance = lambda tipo: tipo.nombre

    def clean(self):
        datos = super().clean()
        llegada, salida = datos.get('llegada'), datos.get('salida')
        if llegada and llegada < self.hoy:
            self.add_error('llegada', 'La fecha de llegada no puede ser anterior a hoy.')
        elif llegada and salida and salida <= llegada:
            self.add_error('salida', 'La fecha de salida debe ser posterior a la fecha de llegada.')
        return datos

    def full_clean(self):
        super().full_clean()
        # Accesibilidad: marca los campos con error para lectores de pantalla (sin JavaScript).
        for nombre in self.errors:
            if nombre in self.fields:
                self.fields[nombre].widget.attrs.update(
                    {'aria-invalid': 'true', 'aria-describedby': f'error-{nombre}'}
                )

    def para_sesion(self):
        """Dict serializable (solo fechas ISO e id; nada sensible) para request.session."""
        d = self.cleaned_data
        return {
            'llegada': d['llegada'].isoformat(),
            'salida': d['salida'].isoformat(),
            'tipo_id': d['tipo'].pk,
        }