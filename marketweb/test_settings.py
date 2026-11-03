"""Configuración SOLO para pruebas automáticas sin MySQL (usa SQLite en memoria).
No afecta al proyecto real:  python manage.py test --settings=marketweb.test_settings"""
import tempfile

from .settings import *  # noqa: F401,F403

DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
MEDIA_ROOT = tempfile.mkdtemp(prefix='wbar_media_test_')
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
