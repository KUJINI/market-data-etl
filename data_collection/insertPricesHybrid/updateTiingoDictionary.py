import requests
import zipfile
import io
import pandas as pd
from sqlalchemy import text
from colorama import Fore, Style, init

from system_and_notification import db_schema as dbs, db_connection_configuration as dbcc


def fetch_and_update_dictionary(engine):
    url = "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"

    try:
        response = requests.get(url, timeout=60)
        if response.status_code != 200:
            print(Fore.RED + f"❌ Błąd pobierania pliku: HTTP {response.status_code}" + Style.RESET_ALL)
            dbs.log_universal(engine, "Błąd pobierania pliku: HTTP", "Failed", error_traceback=str(response.status_code))
            return False

        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            csv_filename = [name for name in z.namelist() if name.endswith('.csv')][0]
            with z.open(csv_filename) as f:
                df = pd.read_csv(f)

        if df.empty or 'ticker' not in df.columns:
            print(Fore.RED + "❌ Pobrany plik CSV jest pusty lub uszkodzony." + Style.RESET_ALL)
            dbs.log_universal(engine, "Pobrany plik CSV jest pusty lub uszkodzony", "Failed")
            return False

        tickers_series = df['ticker'].dropna().str.upper().unique()
        dict_df = pd.DataFrame({'ticker': tickers_series})

        print(Fore.CYAN + f"📊 Przetworzono {len(dict_df)} unikalnych tickerów z Tiingo." + Style.RESET_ALL)

        with engine.begin() as conn:
            print(Fore.YELLOW + "🔥 Kompletne czyszczenie słownika (Reset ID)..." + Style.RESET_ALL)
            conn.execute(text("TRUNCATE TABLE prices_tiingo_dictionary RESTART IDENTITY"))

            print(Fore.YELLOW + "💾 Zapisywanie nowej listy do bazy danych..." + Style.RESET_ALL)
            dict_df.to_sql('prices_tiingo_dictionary', con=conn, if_exists='append', index=False, method='multi', chunksize=5000)

        print(Fore.GREEN + "🎯 Słownik Tiingo zaktualizowany pomyślnie!" + Style.RESET_ALL)
        dbs.log_universal(engine, "Słownik Tiingo zaktualizowany pomyślnie", "Success")
        return True

    except Exception as e:
        error_msg = str(e)[:50]
        print(Fore.RED + f"❌ Krytyczny błąd podczas aktualizacji słownika: {error_msg}" + Style.RESET_ALL)
        dbs.log_universal(engine, "Krytyczny błąd podczas aktualizacji słownika", "Failed", error_traceback=error_msg)
        return False


def main():
    init(autoreset=True)
    print(Fore.GREEN + "🚀 Uruchamianie aktualizatora słownika Tiingo..." + Style.RESET_ALL)
    engine = dbcc.get_engine()
    try:
        dbs.init_db_schema(engine)
        fetch_and_update_dictionary(engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
