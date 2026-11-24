from sqlalchemy import text
import pandas as pd
from colorama import Fore, Style, init
from datetime import datetime
from dateutil.relativedelta import relativedelta
import bisect
import re
import time

from system_and_notification import db_schema as dbs, db_connection_configuration as dbcc


def check_source_db(engine):
    """Sprawdza, czy można nawiązać połączenie z nową bazą danych."""
    try:
        with engine.connect() as conn:
            print(Fore.GREEN + "Połączenie z główną bazą danych zostało pomyślnie nawiązane." + Style.RESET_ALL)
        return True
    except Exception as e:
        print(Fore.RED + f"Wystąpił błąd podczas łączenia z główną bazą danych: {str(e)}" + Style.RESET_ALL)
        return False


def get_settings(config_filename="config_createTableGem.py"):
    """Wczytuje konfigurację z dedykowanego pliku w katalogu cfg."""
    from pathlib import Path
    import runpy
    import sys
    from colorama import Fore, Style

    current_dir = Path(__file__).resolve().parent
    path = current_dir / "cfg" / config_filename

    if not path.exists():
        print(Fore.RED + f"❌ BŁĄD: Plik {config_filename} nie istnieje w {path}" + Style.RESET_ALL)
        sys.exit(1)

    return runpy.run_path(str(path))


def all_tickers(engine, cfg):
    """Pobiera listę tickerów z nowej bazy lub konfiguracji i zapisuje je bezpośrednio w obiekcie cfg sesji."""
    current_tickers = set(cfg.get('TICKERS', []))
    timeframe = cfg.get('TIMEFRAME', '1d')
    all_tickers_flag = cfg.get('ALL_TICKERS', 0)
    czy_dodac_gotowke = cfg.get('CZY_DODAC_GOTOWKE', 0)
    cash_ticker_name = 'CASH'

    if all_tickers_flag == 1:
        try:
            query = text("SELECT DISTINCT ticker FROM v_prices_adjusted WHERE timeframe = :timeframe")
            df = pd.read_sql(query, engine, params={"timeframe": timeframe})
            current_tickers = set(df['ticker'].tolist())
            print(Fore.GREEN + f"Załadowano tickery z bazy: {list(current_tickers)}" + Style.RESET_ALL)
        except Exception as e:
            print(Fore.RED + f"Błąd podczas pobierania tickerów z bazy: {e}")
            return False

    if czy_dodac_gotowke == 1:
        current_tickers.add(cash_ticker_name)
        print(Fore.YELLOW + f"Dodano wirtualny ticker: {cash_ticker_name}" + Style.RESET_ALL)

    # Zapisujemy zmodyfikowaną listę bezpośrednio do izolowanego słownika konfiguracji
    cfg['TICKERS'] = list(current_tickers)
    return True


def recreate_target_db(engine, cfg, tableName):
    """Usuwa starą tabelę docelową i tworzy ją od nowa, korzystając ze zunifikowanej bazy i izolowanej konfiguracji."""
    tickers = cfg.get('TICKERS', [])
    cash_ticker_name = cfg.get('CASH_TICKER_NAME', 'CASH')

    try:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {tableName}"))

            conn.execute(text(f"""
                CREATE TABLE {tableName} (
                    id SERIAL PRIMARY KEY,
                    "dateStart" TIMESTAMP NOT NULL,
                    "dateStop" TIMESTAMP NOT NULL,
                    "dateOfAnalyze" TIMESTAMP NOT NULL,
                    best_performing_ticker_name TEXT,
                    best_performing_ticker_difference NUMERIC,
                    "isBestPerformerOverSMA200" BOOLEAN
                )
            """))

            for ticker in tickers:
                column_name = cash_ticker_name if ticker == cash_ticker_name else ticker.split('.')[0]
                conn.execute(text(f'ALTER TABLE {tableName} ADD COLUMN "{column_name}" NUMERIC'))

        print(Fore.GREEN + F"Pomyślnie utworzono nową tabelę {tableName} w bazie danych." + Style.RESET_ALL)
        return True
    except Exception as e:
        print(Fore.RED + f"Błąd podczas odtwarzania tabeli docelowej: {str(e)}" + Style.RESET_ALL)
        return False


def get_ticker_with_least_data(engine, cfg):
    """Znajduje ticker z najmniejszą liczbą rekordów przy użyciu parametrów z cfg."""
    tickers = cfg.get('TICKERS', [])
    cash_ticker_name = cfg.get('CASH_TICKER_NAME', 'CASH')
    timeframe = cfg.get('TIMEFRAME', '1d')
    days_to_skip = cfg.get('DAYS_TO_SKIP', 1)

    ticker_counts = {}
    actual_tickers = [t for t in tickers if t != cash_ticker_name]

    with engine.connect() as conn:
        for ticker in actual_tickers:
            result = conn.execute(text("""
                SELECT date FROM v_prices_adjusted 
                WHERE ticker = :ticker AND timeframe = :timeframe
                ORDER BY date ASC
                LIMIT 1 OFFSET :offset
            """), {"ticker": ticker, "timeframe": timeframe, "offset": days_to_skip}).fetchone()

            if result:
                first_date = result[0] if isinstance(result[0], datetime) else datetime.strptime(str(result[0]),
                                                                                                 '%Y-%m-%d %H:%M:%S')

                count = conn.execute(text("""
                    SELECT COUNT(date) FROM v_prices_adjusted 
                    WHERE ticker = :ticker AND timeframe = :timeframe AND date >= :first_date
                """), {"ticker": ticker, "timeframe": timeframe, "first_date": first_date}).fetchone()[0]

                ticker_counts[ticker] = count

    if not ticker_counts:
        raise ValueError("Nie znaleziono danych historycznych dla żadnego z podanych tickerów.")

    min_ticker = min(ticker_counts.items(), key=lambda x: x[1])[0]
    print(
        Fore.BLUE + f"Ticker z najmniejszą ilością danych: {min_ticker} ({ticker_counts[min_ticker]} rekordów)" + Style.RESET_ALL)
    return min_ticker


def get_filtered_dates_for_ticker(engine, cfg):
    """
    Tworzy bezwzględną oś czasu opartą na części wspólnej (intersection) wszystkich dni sesyjnych
    dla wszystkich analizowanych aktywów (z wykluczeniem gotówki).
    """

    timeframe = cfg.get('TIMEFRAME', '1d')
    days_to_skip = cfg.get('DAYS_TO_SKIP', 1)
    cash_ticker = cfg.get('CASH_TICKER_NAME', 'CASH')

    # Bierzemy tylko realne rynkowe tickery, ignorujemy wirtualną gotówkę
    actual_tickers = [t for t in cfg.get('TICKERS', []) if t != cash_ticker]
    ticker_count = len(actual_tickers)

    if ticker_count == 0:
        raise ValueError("KRYTYCZNY BŁĄD: Brak rzeczywistych tickerów do wygenerowania kalendarza.")

    with engine.connect() as conn:
        # 1. Znalezienie absolutnego, najpóźniejszego dnia startu dla całego portfela (najmłodszy ticker dyktuje start)
        min_date_result = conn.execute(text("""
            SELECT MAX(min_date) FROM (
                SELECT ticker, MIN(date) as min_date 
                FROM v_prices_adjusted 
                WHERE timeframe = :timeframe AND ticker IN :tickers
                GROUP BY ticker
            ) as subquery
        """), {"timeframe": timeframe, "tickers": tuple(actual_tickers)}).fetchone()

        if not min_date_result or not min_date_result[0]:
            raise ValueError("Brak danych początkowych dla zdefiniowanych tickerów.")

        min_portfolio_date = min_date_result[0]
        if isinstance(min_portfolio_date, str):
            min_portfolio_date = datetime.strptime(min_portfolio_date, '%Y-%m-%d %H:%M:%S')

        # Omijamy dni startowe szumu (days_to_skip)
        from datetime import timedelta
        safe_start_date = min_portfolio_date + timedelta(days=days_to_skip)

        # 2. Pobranie CZĘŚCI WSPÓLNEJ dat sesyjnych (gdzie wszystkie tickery mają ceny)
        all_dates_result = conn.execute(text("""
            SELECT date 
            FROM v_prices_adjusted 
            WHERE timeframe = :timeframe 
              AND ticker IN :tickers 
              AND date >= :safe_start_date
            GROUP BY date
            HAVING COUNT(DISTINCT ticker) = :ticker_count
            ORDER BY date ASC
        """), {
            "timeframe": timeframe,
            "tickers": tuple(actual_tickers),
            "safe_start_date": safe_start_date,
            "ticker_count": ticker_count
        }).fetchall()

        if not all_dates_result:
            raise ValueError("Brak wspólnych dni sesyjnych dla podanego portfela po zastosowaniu filtrów.")

        all_dates = [row[0] if isinstance(row[0], datetime) else datetime.strptime(str(row[0]), '%Y-%m-%d %H:%M:%S') for
                     row in all_dates_result]

        # 3. Logika okna czasowego (Cutoff)
        date_start_unit, date_start_number, date_stop_unit, date_stop_number = decode_periods(cfg)

        if date_start_unit == 'quarters':
            date_start_unit = 'months'
            date_start_number *= 3
        if date_start_unit == 'years':
            date_start_unit = 'months'
            date_start_number *= 12

        if date_stop_unit == 'quarters':
            date_stop_unit = 'months'
            date_stop_number *= 3
        if date_stop_unit == 'years':
            date_stop_unit = 'months'
            date_stop_number *= 12

        params_start = {date_start_unit: date_start_number}
        params_stop = {date_stop_unit: date_stop_number}

        cutoff_date = all_dates[0] + relativedelta(**params_start) + relativedelta(**params_stop)

        # Daty docelowe, dla których mamy wystarczającą historię w obu kierunkach
        filtered_dates = [date for date in all_dates if date >= cutoff_date]

        return filtered_dates, all_dates


def fill_dates_in_target_db(engine, cfg, tableName):
    """Wypełnia docelową tabelę poprawnie wyrównując daty start/stop do faktycznych dni sesyjnych."""
    try:
        min_ticker = get_ticker_with_least_data(engine, cfg)

        # Odbieramy obie listy z poprawionej funkcji
        dates_to_insert, all_dates = get_filtered_dates_for_ticker(engine, cfg)
        all_dates_sorted = sorted(all_dates)  # Zabezpieczenie sortowania dla algorytmu wyszukiwania

        date_start_unit, date_start_number, date_stop_unit, date_stop_number = decode_periods(cfg)

        if date_start_unit == 'quarters':
            date_start_unit = 'months'
            date_start_number *= 3
        if date_start_unit == 'years':
            date_start_unit = 'months'
            date_start_number *= 12

        if date_stop_unit == 'quarters':
            date_stop_unit = 'months'
            date_stop_number *= 3
        if date_stop_unit == 'years':
            date_stop_unit = 'months'
            date_stop_number *= 12

        with engine.begin() as conn:
            for date in dates_to_insert:
                date_of_analyze = date
                params_start = {date_start_unit: date_start_number}
                params_stop = {date_stop_unit: date_stop_number}

                # Naiwna matematyka
                naive_date_stop = date_of_analyze - relativedelta(**params_stop)
                naive_date_start = naive_date_stop - relativedelta(**params_start)

                # KLUCZOWA ZMIANA: Szukanie najbliższej wcześniejszej rzeczywistej daty sesyjnej (snapping)
                idx_stop = bisect.bisect_right(all_dates_sorted, naive_date_stop) - 1
                actual_date_stop = all_dates_sorted[idx_stop] if idx_stop >= 0 else naive_date_stop

                idx_start = bisect.bisect_right(all_dates_sorted, naive_date_start) - 1
                actual_date_start = all_dates_sorted[idx_start] if idx_start >= 0 else naive_date_start

                conn.execute(text(f"""
                    INSERT INTO {tableName} ("dateStart", "dateStop", "dateOfAnalyze", best_performing_ticker_name, best_performing_ticker_difference) 
                    VALUES (:date_start, :date_stop, :date_of_analyze, '', NULL)
                """), {
                    'date_start': actual_date_start,
                    'date_stop': actual_date_stop,
                    'date_of_analyze': date_of_analyze
                })

        print(
            Fore.GREEN + f"Pomyślnie wygenerowano osie czasu i dociągnięto je do rzeczywistych sesji. Zapisano {len(dates_to_insert)} rekordów." + Style.RESET_ALL)
        return True
    except Exception as e:
        print(Fore.RED + f"Błąd podczas generowania i uzupełniania osi czasu: {str(e)}" + Style.RESET_ALL)
        return False


def decode_periods(cfg):
    """Dekoduje okresy analizy i pomijania bezpośrednio ze słownika konfiguracji."""
    period_to_analyse = cfg.get('PERIOD_TO_ANALYSE', '12msc')
    skip_period = cfg.get('SKIP_PERIOD', '1msc')

    def parse_period(p_str):
        match = re.match(r'(\d+)([a-zA-Z]+)', p_str)
        if not match:
            raise ValueError(f"Niepoprawny format okresu: {p_str}")
        val, unit = match.groups()
        val = int(val)

        if unit in ['msc', 'mo']: return 'months', val
        if unit in ['d']: return 'days', val
        if unit in ['w', 'wk']: return 'weeks', val
        if unit in ['q']: return 'quarters', val
        if unit in ['y']: return 'years', val
        raise ValueError(f"Nieznana jednostka czasu: {unit}")

    start_unit, start_num = parse_period(period_to_analyse)
    stop_unit, stop_num = parse_period(skip_period)
    return start_unit, start_num, stop_unit, stop_num


def fill_percentage_difference(engine, cfg, tableName):
    """Oblicza procentowe różnice cen dla tickerów. Zoptymalizowane pod kątem bazy scentralizowanej."""
    timeframe = cfg.get('TIMEFRAME', '1d')
    tickers = cfg.get('TICKERS', [])
    cash_ticker = cfg.get('CASH_TICKER_NAME', 'CASH')
    czy_dodac_gotowke = cfg.get('CZY_DODAC_GOTOWKE', 0)

    try:
        # Pobieramy ramy czasowe z nowo utworzonej tabeli docelowej
        with engine.connect() as conn:
            df_target = pd.read_sql(text(f'SELECT id, "dateStart", "dateStop" FROM {tableName}'), conn)

        if df_target.empty:
            print(Fore.YELLOW + "Tabela docelowa jest pusta. Brak dat do obliczeń." + Style.RESET_ALL)
            return True

        # Wyciągamy rzeczywiste tickery giełdowe
        actual_tickers = [t for t in tickers if t != cash_ticker]

        # Pobieramy ceny zbiorczo z tabeli 'prices', zamiast strzelać zapytaniami w pętli (stary błąd wydajnościowy)
        with engine.connect() as conn:
            query = text("""
                SELECT date, ticker, adj_close FROM v_prices_adjusted 
                WHERE timeframe = :timeframe AND ticker IN :tickers
            """)
            df_prices = pd.read_sql(query, conn, params={"timeframe": timeframe, "tickers": tuple(actual_tickers)})

        df_prices['date'] = pd.to_datetime(df_prices['date'])
        df_target['dateStart'] = pd.to_datetime(df_target['dateStart'])
        df_target['dateStop'] = pd.to_datetime(df_target['dateStop'])

        # Mapowanie cen w pamięci podręcznej typu {ticker: {data: cena}}
        price_map = {}
        for ticker in actual_tickers:
            df_t = df_prices[df_prices['ticker'] == ticker]
            price_map[ticker] = dict(zip(df_t['date'], df_t['adj_close']))

        updates = []
        for _, row in df_target.iterrows():
            row_id = row['id']
            d_start = row['dateStart']
            d_stop = row['dateStop']

            update_data = {'b_id': row_id}

            for ticker in tickers:
                col_name = cash_ticker if ticker == cash_ticker else ticker.split('.')[0]

                if ticker == cash_ticker and czy_dodac_gotowke == 1:
                    update_data[col_name] = 0.0  # Zmiana bazy dla gotówki odniesienia wynosi 0%
                    continue

                t_prices = price_map.get(ticker, {})
                p_start = t_prices.get(d_start)
                p_stop = t_prices.get(d_stop)

                if p_start is not None and p_stop is not None and p_start != 0:
                    update_data[col_name] = ((p_stop - p_start) / p_start) * 100
                else:
                    update_data[col_name] = None

            updates.append(update_data)

        # Transakcyjny zapis zbiorczy (Bulk Update) z pancerną izolacją zmiennych
        if updates:
            with engine.begin() as conn:
                for up in updates:
                    set_clauses = []
                    safe_params = {'b_id': up['b_id']}

                    for i, (col_name, val) in enumerate(up.items()):
                        if col_name == 'b_id':
                            continue

                        # Generujemy sterylną zmienną (val_0, val_1...) dla SQLAlchemy
                        safe_param_name = f"val_{i}"
                        set_clauses.append(f'"{col_name}" = :{safe_param_name}')
                        safe_params[safe_param_name] = val

                    if set_clauses:
                        set_sql = ", ".join(set_clauses)
                        conn.execute(text(f'UPDATE {tableName} SET {set_sql} WHERE id = :b_id'), safe_params)

        print(Fore.GREEN + "Obliczenia różnic procentowych zakończone pomyślnie." + Style.RESET_ALL)
        return True
    except Exception as e:
        print(Fore.RED + f"Błąd podczas kalkulacji różnic cen: {str(e)}" + Style.RESET_ALL)
        return False


def clean_up_null_rows(engine, cfg, tableName):
    """Usuwa wiersze, które nie posiadają kompletnych danych obliczeniowych dla wszystkich tickerów."""
    tickers = cfg.get('TICKERS', [])
    cash_ticker = cfg.get('CASH_TICKER_NAME', 'CASH')

    try:
        conditions = []
        for ticker in tickers:
            col_name = cash_ticker if ticker == cash_ticker else ticker.split('.')[0]
            conditions.append(f'"{col_name}" IS NULL')

        if not conditions:
            return True

        where_clause = " OR ".join(conditions)

        with engine.begin() as conn:
            result = conn.execute(text(f"DELETE FROM {tableName} WHERE {where_clause}"))
            print(
                Fore.GREEN + f"Oczyszczono bazę. Usunięto {result.rowcount} wierszy z brakami danych (NULL)." + Style.RESET_ALL)
        return True
    except Exception as e:
        print(Fore.RED + f"Błąd czyszczenia tabeli z wartości NULL: {str(e)}" + Style.RESET_ALL)
        return False


def update_best_performing_ticker_name(engine, cfg, tableName):
    """Weryfikuje wyniki w danym okresie i zapisuje dominujący ticker."""
    tickers = cfg.get('TICKERS', [])
    cash_ticker = cfg.get('CASH_TICKER_NAME', 'CASH')

    try:
        with engine.connect() as conn:
            df = pd.read_sql(text(f"SELECT * FROM {tableName}"), conn)

        if df.empty:
            print(Fore.YELLOW + "Brak rekordów do wyznaczenia liderów rynkowych." + Style.RESET_ALL)
            return True

        ticker_cols = [cash_ticker if t == cash_ticker else t.split('.')[0] for t in tickers]
        updates = []

        for _, row in df.iterrows():
            row_id = row['id']
            row_values = {col: row[col] for col in ticker_cols if row[col] is not None}

            if not row_values:
                continue

            best_ticker = max(row_values, key=row_values.get)
            best_value = row_values[best_ticker]

            updates.append({
                'b_id': row_id,
                'best_ticker': best_ticker,
                'best_value': float(best_value),
                'tableName': tableName
            })

        if updates:
            with engine.begin() as conn:
                stmt = text(f"""
                    UPDATE {tableName}
                    SET best_performing_ticker_name = :best_ticker,
                        best_performing_ticker_difference = :best_value
                    WHERE id = :b_id
                """)
                conn.execute(stmt, updates)

        print(Fore.GREEN + "Zaktualizowano kolumny liderów (best_performing_ticker_name)." + Style.RESET_ALL)
        return True
    except Exception as e:
        print(Fore.RED + f"Błąd zapisu liderów zestawienia: {str(e)}" + Style.RESET_ALL)
        return False


def verify_tickers_availability(engine, cfg):
    """
    Sprawdza na samym starcie, czy zdefiniowane w configu tickery posiadają
    chociaż jeden rekord w widoku źródłowym v_prices_adjusted.
    Jeśli któregoś brakuje, zrywa proces rzucając wyjątek.
    """
    from sqlalchemy import text
    from colorama import Fore, Style

    tickers = cfg.get('TICKERS', [])
    if not tickers:
        print(Fore.RED + "❌ BŁĄD KRYTYCZNY: Lista TICKERS w konfiguracji jest pusta!" + Style.RESET_ALL)
        raise ValueError("Pusta lista tickerów w pliku konfiguracyjnym.")

    missing_tickers = []

    try:
        with engine.connect() as conn:
            for ticker in tickers:
                # Najszybsze możliwe sprawdzenie obecności za pomocą LIMIT 1
                query = text('SELECT 1 FROM v_prices_adjusted WHERE ticker = :t LIMIT 1')
                result = conn.execute(query, {"t": ticker}).fetchone()

                if not result:
                    missing_tickers.append(ticker)
    except Exception as e:
        print(Fore.RED + f"❌ BŁĄD: Problem z połączeniem podczas wstępnej weryfikacji tickerów: {e}" + Style.RESET_ALL)
        raise e

    if missing_tickers:
        print(
            Fore.RED + f"\n[KRYTYCZNY BŁĄD] Następujące tickery nie posiadają ŻADNYCH danych w widoku v_prices_adjusted: {missing_tickers}" + Style.RESET_ALL)
        print(Fore.RED + "Przerwanie operacji. Struktura bazy danych nie została naruszona." + Style.RESET_ALL)
        raise ValueError(f"Brak danych w bazie źródłowej dla tickerów: {missing_tickers}")

    print(
        Fore.GREEN + "✅ Weryfikacja danych źródłowych: Wszystkie zdefiniowane tickery istnieją w bazie." + Style.RESET_ALL)
    return True


def update_sma_condition(engine, cfg, tableName):
    """Sprawdza, czy lider znajduje się powyżej swojego SMA (jeśli aktywowane w configu)."""
    czy_uzywac_sma = cfg.get('CZY_UZYWAC_SMA', 0)
    wartosc_sma = cfg.get('WARTOSC_SMA', 200)
    timeframe = cfg.get('TIMEFRAME', '1d')
    cash_ticker = cfg.get('CASH_TICKER_NAME', 'CASH')
    tickers = cfg.get('TICKERS', [])

    try:
        # 1. Prosty przypadek - SMA wyłączone (Wstawiamy wartość True dla wszystkich wpisów)
        if czy_uzywac_sma == 0:
            with engine.begin() as conn:
                conn.execute(text(f'UPDATE {tableName} SET "isBestPerformerOverSMA200" = TRUE'))
            print(
                Fore.GREEN + "Weryfikacja SMA wyłączona (CZY_UZYWAC_SMA=0). Ustawiono wartość TRUE w całej kolumnie." + Style.RESET_ALL)
            return True

        # 2. SMA włączone - zaawansowana kalkulacja używając Pandas dla wydajności
        actual_tickers = [t for t in tickers if t != cash_ticker]

        with engine.connect() as conn:
            df_target = pd.read_sql(text(f'SELECT id, "dateOfAnalyze", best_performing_ticker_name FROM {tableName}'),
                                    conn)

        if df_target.empty:
            return True

        # Pobieramy ceny dla wszystkich rzeczywistych tickerów naraz
        with engine.connect() as conn:
            query = text("""
                SELECT date, ticker, adj_close FROM v_prices_adjusted 
                WHERE timeframe = :timeframe AND ticker IN :tickers
            """)
            df_prices = pd.read_sql(query, conn, params={"timeframe": timeframe, "tickers": tuple(actual_tickers)})

        if df_prices.empty:
            print(Fore.YELLOW + "Brak danych cenowych do obliczenia SMA." + Style.RESET_ALL)
            return True

        # Sortujemy i liczymy wybrane kroczące SMA (rolling window) zgrupowane po tickerach
        df_prices['date'] = pd.to_datetime(df_prices['date'])
        df_prices = df_prices.sort_values(by=['ticker', 'date'])
        df_prices['sma'] = df_prices.groupby('ticker')['adj_close'].transform(
            lambda x: x.rolling(window=wartosc_sma).mean())

        # Słownik dla złożoności czasowej O(1) przy wyszukiwaniu
        price_map = {}
        for ticker in actual_tickers:
            df_t = df_prices[df_prices['ticker'] == ticker]
            price_map[ticker] = {
                row['date']: (row['adj_close'], row['sma'])
                for _, row in df_t.iterrows()
            }

        col_to_ticker = {t.split('.')[0]: t for t in actual_tickers}
        updates = []

        for _, row in df_target.iterrows():
            row_id = row['id']
            date_of_analyze = pd.to_datetime(row['dateOfAnalyze'])
            best_ticker_col = row['best_performing_ticker_name']

            # Zabezpieczenie: jeśli wygrywa gotówka, uznajemy, że warunek (lub brak straty) jest spełniony
            if best_ticker_col == cash_ticker:
                updates.append({'b_id': row_id, 'is_over_sma': True})
                continue

            real_ticker = col_to_ticker.get(best_ticker_col)
            if not real_ticker:
                updates.append({'b_id': row_id, 'is_over_sma': False})
                continue

            # Sprawdzamy cenę i SMA w dniu 'dateOfAnalyze'
            ticker_data = price_map.get(real_ticker, {}).get(date_of_analyze)

            if ticker_data:
                current_price, current_sma = ticker_data
                # Jeśli w tym dniu nie dało się jeszcze wyliczyć SMA (brak 200 dni w przeszłość) to False
                if pd.isna(current_sma):
                    is_over_sma = False
                else:
                    is_over_sma = bool(current_price > current_sma)
            else:
                is_over_sma = False

            updates.append({'b_id': row_id, 'is_over_sma': is_over_sma})

        # Zapisujemy wyliczone wartości masowo
        if updates:
            with engine.begin() as conn:
                stmt = text(f"""
                    UPDATE {tableName}
                    SET "isBestPerformerOverSMA200" = :is_over_sma
                    WHERE id = :b_id
                """)
                conn.execute(stmt, updates)

        print(
            Fore.GREEN + f"Obliczono i zaktualizowano warunek SMA ({wartosc_sma}) dla liderów okresu." + Style.RESET_ALL)
        return True

    except Exception as e:
        print(Fore.RED + f"Błąd podczas wyliczania warunku SMA: {str(e)}" + Style.RESET_ALL)
        return False


def main(config_filename="config_createTableGem.py"):
    """Główny punkt wejścia skryptu GEM."""
    init(autoreset=True)
    start_time = time.perf_counter()
    engine = dbcc.get_engine()

    try:
        print(Fore.CYAN + "=== INICJACJA PROCESU GENEROWANIA TABELI GEM ===" + Style.RESET_ALL)

        # --- 1. Weryfikacja i inicjalizacja struktury bazy danych ---
        dbs.init_db_schema(engine)

        # --- 2. Ładowanie konfiguracji ---
        try:
            cfg = get_settings(config_filename)
            print(Fore.GREEN + f"Pomyślnie załadowano konfigurację z cfg/{config_filename}.")
        except Exception as e:
            print(Fore.RED + f"Krytyczny błąd konfiguracji: {e}")
            return False

        if config_filename == 'config_createTableGem.py':
            tableName = 'gem_price_changes'
            mode_label = "GEM_MAIN"
        elif config_filename == 'config_backtest_createTableGem.py':
            tableName = 'gem_backtest_price_changes'
            mode_label = "GEM_BACKTEST"
        else:
            return False

        # --- 3. Główny Pipeline ---
        steps = [
            ("Weryfikacja dostępności bazy danych", lambda: check_source_db(engine)),
            ("Weryfikacja obecności danych dla wybranych tickerów", lambda: verify_tickers_availability(engine, cfg)),
            ("Pobieranie listy aktywnych tickerów", lambda: all_tickers(engine, cfg)),
            ("Rekonstrukcja tabeli docelowej w nowej bazie", lambda: recreate_target_db(engine, cfg, tableName)),
            ("Inicjalizacja i mapowanie osi czasu", lambda: fill_dates_in_target_db(engine, cfg, tableName)),
            ("Kalkulacja momentum (różnice procentowe)", lambda: fill_percentage_difference(engine, cfg, tableName)),
            ("Eliminacja niepełnych okresów (czyszczenie NULL)", lambda: clean_up_null_rows(engine, cfg, tableName)),
            ("Ewaluacja i oznaczanie liderów", lambda: update_best_performing_ticker_name(engine, cfg, tableName)),
            ("Weryfikacja warunku SMA dla lidera", lambda: update_sma_condition(engine, cfg, tableName))
        ]

        for description, step_func in steps:
            print(Fore.BLUE + f"\n[URUCHAMIANIE]: {description}...")

            if not step_func():
                print(Fore.RED + f"❌ Pipeline przerwany na kroku: '{description}'" + Style.RESET_ALL)
                dbs.log_universal(engine, f"Przerwano na kroku: {description}", "Failed", mode_label, start_time)
                return False

        # --- 4. Zakończenie sukcesem ---
        dbs.log_universal(engine, f"Zaktualizowano tabelę: {tableName}", "Success", mode_label, start_time)
        print(Fore.GREEN + "\n=======================================================")
        print(Fore.GREEN + "✅ PROCES ZAKOŃCZONY: Tabela strategii GEM została zaktualizowana.")
        print(Fore.GREEN + "=======================================================" + Style.RESET_ALL)
        return True
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()