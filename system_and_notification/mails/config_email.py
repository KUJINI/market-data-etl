import os
from dotenv import load_dotenv

load_dotenv()

RECEIVERS = [e.strip() for e in os.getenv("EMAIL_RECEIVERS", "").split(",") if e.strip()]

SUBJECT_PREFIX = 'Raport dzienny - Analiza Pokrycia'
