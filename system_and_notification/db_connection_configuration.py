import os
import platform
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()

DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST_DEV = os.getenv("DB_HOST_DEV")
DB_HOST_PROD = os.getenv("DB_HOST_PROD")
PROD_HOSTNAME = os.getenv("PROD_HOSTNAME")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

hostname = platform.node()


def get_engine():
    host = DB_HOST_PROD if hostname == PROD_HOSTNAME else DB_HOST_DEV
    engine = create_engine(
        f'postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{host}:{DB_PORT}/{DB_NAME}'
    )
    return engine
