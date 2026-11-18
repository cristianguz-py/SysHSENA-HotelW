
#!/usr/bin/env bash
set -o errexit

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install gunicorn==22.0.0

python -m pip show gunicorn
python -c "import gunicorn; print('Gunicorn instalado:', gunicorn.__version__)"

python manage.py collectstatic --no-input
python manage.py migrate
