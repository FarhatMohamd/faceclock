"""Production entry point: gunicorn -w 4 -b 0.0.0.0:8000 wsgi:app"""

from faceclock import create_app

app = create_app()
