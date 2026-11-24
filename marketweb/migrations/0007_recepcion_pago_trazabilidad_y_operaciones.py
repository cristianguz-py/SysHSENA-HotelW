
# Correccion compatible con TiDB para la migracion 0007

import django.db.models.deletion
from django.db import migrations, models


def agregar_referencia_si_no_existe(apps, schema_editor):
    Pago = apps.get_model('marketweb', 'Pago')
    tabla = Pago._meta.db_table

    with schema_editor.connection.cursor() as cursor:
        columnas = schema_editor.connection.introspection.get_table_description(
            cursor, tabla
        )

    nombres = {columna.name for columna in columnas}

    if 'referencia' not in nombres:
        campo = models.CharField(
            blank=True,
            default='',
            max_length=60,
        )
        campo.set_attributes_from_name('referencia')
        schema_editor.add_field(Pago, campo)


class Migration(migrations.Migration):

    dependencies = [
        ('marketweb', '0006_reserva_usuario'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    agregar_referencia_si_no_existe,
                    migrations.RunPython.noop,
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='pago',
                    name='referencia',
                    field=models.CharField(
                        blank=True,
                        default='',
                        max_length=60,
                    ),
                ),
            ],
        ),

        migrations.AddField(
            model_name='pago',
            name='registrado_por',
            field=models.ForeignKey(
                blank=True,
                null=True,
                db_constraint=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='pagos_registrados',
                to='marketweb.usuario',
            ),
        ),

        migrations.CreateModel(
            name='OperacionRecepcion',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True,
                    primary_key=True,
                    serialize=False,
                    verbose_name='ID',
                )),
                ('tipo', models.CharField(
                    choices=[
                        ('HUESPED_ALTA', 'Huésped registrado'),
                        ('HUESPED_EDICION', 'Huésped actualizado'),
                        ('RESERVA_CREADA', 'Reserva creada'),
                        ('RESERVA_MODIFICADA', 'Reserva modificada'),
                        ('RESERVA_CONFIRMADA', 'Reserva confirmada'),
                        ('RESERVA_CANCELADA', 'Reserva cancelada'),
                        ('CHECKIN', 'Check-in'),
                        ('CHECKOUT', 'Check-out'),
                        ('SERVICIO', 'Servicio adicional'),
                        ('PAGO', 'Pago / abono'),
                        ('HABITACION_HABILITADA', 'Habitación habilitada'),
                    ],
                    max_length=24,
                )),
                ('fecha', models.DateTimeField(auto_now_add=True)),
                ('monto', models.DecimalField(
                    blank=True,
                    decimal_places=2,
                    max_digits=12,
                    null=True,
                )),
                ('descripcion', models.CharField(max_length=300)),
                ('huesped', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='operaciones',
                    to='marketweb.huesped',
                )),
                ('reserva', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='operaciones',
                    to='marketweb.reserva',
                )),
                ('usuario', models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='operaciones_recepcion',
                    to='marketweb.usuario',
                )),
            ],
            options={
                'db_table': 'operacion_recepcion',
                'ordering': ['-fecha', '-id'],
                'indexes': [
                    models.Index(fields=['fecha'], name='oprec_fecha_idx'),
                    models.Index(fields=['tipo'], name='oprec_tipo_idx'),
                ],
            },
        ),
    ]
