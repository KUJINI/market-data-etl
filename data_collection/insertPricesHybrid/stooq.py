from system_and_notification import db_connection_configuration as dbcc
import pandas as pd
from sqlalchemy import text
from colorama import Fore, Style, init
import os


def load_local_gold_csv():
    init(autoreset=True)
    ticker = 'XAUUSD'
    timeframe = '1d'
    source = 'STOOQ_LOCAL'
    file_name = 'xauusd.csv'

    print(Fore.CYAN + f"Wczytywanie historii dla {ticker} z pliku {file_name}..." + Style.RESET_ALL)

    if not os.path.exists(file_name):
        print(Fore.RED + f"❌ Nie znaleziono pliku {file_name} w folderze ze skryptem!" + Style.RESET_ALL)
        return

    try:
        # Wczytanie lokalnego pliku CSV
        df = pd.read_csv(file_name)
    except Exception as e:
        print(Fore.RED + f"❌ Błąd wczytywania pliku CSV: {e}" + Style.RESET_ALL)
        return

    # Oczyszczanie i formatowanie daty
    df['Date'] = pd.to_datetime(df['Date'])
    df = df[df['Date'] >= '1998-01-01']

    if df.empty:
        print(Fore.RED + "❌ Brak danych po przefiltrowaniu daty!" + Style.RESET_ALL)
        return

    engine = dbcc.get_engine()
    records_count = len(df)
    print(Fore.YELLOW + f"Zapisuję {records_count} rekordów do bazy..." + Style.RESET_ALL)

    try:
        # Inicjalizacja stelaża
        with engine.begin() as conn:
            exists = conn.execute(text("SELECT 1 FROM prices_adj_factors WHERE ticker = :t"), {"t": ticker}).fetchone()
            if not exists:
                conn.execute(text("""
                    INSERT INTO prices_adj_factors (ticker, start_date, end_date, multiplier)
                    VALUES (:t, '1900-01-01', '2099-12-31', 1.0)
                """), {"t": ticker})

        # Zrzut do prices_raw
        with engine.begin() as conn:
            for _, row in df.iterrows():
                date_val = row['Date']

                try:
                    open_val = float(row['Open'])
                    high_val = float(row['High'])
                    low_val = float(row['Low'])
                    close_val = float(row['Close'])
                    vol_val = 0 if pd.isna(row.get('Volume')) else int(row['Volume'])
                except (ValueError, TypeError):
                    continue

                conn.execute(text("""
                    INSERT INTO prices_raw (ticker, date, open, high, low, close, volume, dividend, split, source, timeframe)
                    VALUES (:t, :dt, :o, :h, :l, :c, :v, :div, :s, :src, :tf)
                    ON CONFLICT (ticker, date, timeframe) DO UPDATE 
                    SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, 
                        close = EXCLUDED.close, volume = EXCLUDED.volume, 
                        dividend = EXCLUDED.dividend, split = EXCLUDED.split,
                        source = EXCLUDED.source
                """), {
                    "t": ticker, "dt": date_val, "o": open_val, "h": high_val,
                    "l": low_val, "c": close_val, "v": vol_val,
                    "div": 0.0, "s": 1.0, "src": source, "tf": timeframe
                })

        min_date = df['Date'].min().strftime('%Y-%m-%d')
        max_date = df['Date'].max().strftime('%Y-%m-%d')
        print(Fore.GREEN + f"✅ Sukces. Zapisano bazę {ticker} od {min_date} do {max_date}." + Style.RESET_ALL)

    except Exception as e:
        print(Fore.RED + f"❌ Krytyczny błąd podczas zapisu do DB: {e}" + Style.RESET_ALL)
    finally:
        engine.dispose()


if __name__ == "__main__":
    load_local_gold_csv()