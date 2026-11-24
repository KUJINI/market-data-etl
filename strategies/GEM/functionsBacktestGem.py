def get_price_for_ticker_on_date(ticker, target_date, engine):
    """Pobiera cenę zamknięcia z centralnego widoku Postgresa."""
    from sqlalchemy import text
    from colorama import Fore, Style

    if ticker.upper() in ['CASHE', 'CASH']:
        return 1.0

    try:
        with engine.connect() as conn:
            query = text("""
                SELECT adj_close 
                FROM v_prices_adjusted 
                WHERE DATE(date) = DATE(:target_date) 
                  AND ticker = :ticker 
                  AND timeframe = '1d'
            """)
            result = conn.execute(query, {'target_date': target_date, 'ticker': ticker}).fetchone()

            return float(result[0]) if result else None
    except Exception as e:
        print(Fore.RED + f"Błąd pobierania ceny dla {ticker} ({target_date}): {e}" + Style.RESET_ALL)
        return None


def get_ticker_for_date(target_date_str, engine):
    """
    Pobiera gotowy sygnał decyzyjny (zwycięski ticker) oraz weryfikację warunku SMA
    z tabeli wyników GEM. Wybiera najświeższy sygnał z okresu kończącego się w lub przed podaną datą.
    """
    from sqlalchemy import text
    from colorama import Fore, Style

    try:
        with engine.connect() as conn:
            # Pamiętaj o cudzysłowach przy "dateStop", jeśli w Postgresie zachowałeś wielkość liter (CamelCase)
            query = text("""
                SELECT best_performing_ticker_name, "isBestPerformerOverSMA200" 
                FROM gem_backtest_price_changes 
                WHERE "dateOfAnalyze" = :target_date 
                ORDER BY "dateStop" DESC 
                LIMIT 1
            """)
            result = conn.execute(query, {'target_date': target_date_str}).fetchone()

            if result and result[0]:
                # Wyciągamy zmienne i jawnie wymuszamy ich docelowy typ, by uniknąć niespodzianek
                ticker = str(result[0])
                is_over_sma = bool(result[1]) if result[1] is not None else False
                return ticker, is_over_sma
            else:
                # Fallback, jeśli backtest zaczął się przed wygenerowaniem pierwszego sygnału
                return None, None

    except Exception as e:
        print(Fore.RED + f"Błąd pobierania sygnału GEM dla daty {target_date_str}: {e}" + Style.RESET_ALL)
        return None, None


def log_transaction(engine, rebalance_day, ticker, buy_date, sell_date, buy_price, sell_price, shares, portfolio_value):
    """Zapisuje zamkniętą pozycję do tabeli gem_change_logs w Postgresie."""
    from sqlalchemy import text
    from colorama import Fore, Style

    pnl_pct = 0.0
    if buy_price and buy_price > 0 and sell_price:
        pnl_pct = ((sell_price - buy_price) / buy_price) * 100

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO gem_change_logs 
                (dzien_rebalansu, ticker, data_zakupu, data_sprzedazy, cena_zakupu, cena_sprzedazy, ilosc_akcji, zysk_strata_procent, wartosc_portfela_po_operacji)
                VALUES 
                (:dzien_rebalansu, :ticker, :data_zakupu, :data_sprzedazy, :cena_zakupu, :cena_sprzedazy, :ilosc_akcji, :zysk_strata_procent, :wartosc_portfela)
            """), {
                'dzien_rebalansu': rebalance_day,
                'ticker': ticker,
                'data_zakupu': buy_date,
                'data_sprzedazy': sell_date,
                'cena_zakupu': buy_price,
                'cena_sprzedazy': sell_price,
                'ilosc_akcji': shares,
                'zysk_strata_procent': pnl_pct,
                'wartosc_portfela': portfolio_value
            })
    except Exception as e:
        print(Fore.RED + f"Błąd zapisu logu transakcji dla {ticker}: {e}" + Style.RESET_ALL)


def calculate_and_save_statistics(engine, rebalance_day, start_portfolio_value, final_portfolio_value, years_passed):
    """Oblicza pełne statystyki strategii i zapisuje je do bazy."""
    from sqlalchemy import text
    import pandas as pd
    import numpy as np
    from colorama import Fore, Style

    results = {
        'zysk_strata': final_portfolio_value - start_portfolio_value,
        'roe': ((
                            final_portfolio_value - start_portfolio_value) / start_portfolio_value) * 100 if start_portfolio_value else 0,
        'cagr': (((final_portfolio_value / start_portfolio_value) ** (
                    1 / years_passed)) - 1) * 100 if years_passed > 0 and start_portfolio_value else 0,
        'maximum_dropdown': 0.0,
        'dropdown_duration': 0,
        'sortino': 0.0,
        'sharpe': 0.0,
        'liczba_tranzakcji': 0,
        'sredni_czas_dlugosci_pozycji': 0.0,
        'win_ratio': 0.0,
        'market_exposure': 0.0
    }

    try:
        df_daily = pd.read_sql(
            text(
                "SELECT data, wartosc_portfela, ticker FROM gem_daily_portfolio_value WHERE dzien_rebalansu = :r_day ORDER BY data ASC"),
            engine, params={'r_day': rebalance_day}
        )

        if not df_daily.empty:
            df_daily['data'] = pd.to_datetime(df_daily['data'])

            # --- Drawdown i jego długość ---
            df_daily['cummax'] = df_daily['wartosc_portfela'].cummax()
            df_daily['drawdown_percent'] = ((df_daily['cummax'] - df_daily['wartosc_portfela']) / df_daily['cummax']) * 100
            results['maximum_dropdown'] = df_daily['drawdown_percent'].max()
            df_daily['data'] = pd.to_datetime(df_daily['data'])
            df_daily['is_peak'] = df_daily['wartosc_portfela'] >= df_daily['cummax']
            df_daily['peak_date'] = df_daily['data'].where(df_daily['is_peak']).ffill()
            df_daily['drawdown_days'] = (df_daily['data'] - df_daily['peak_date']).dt.days
            results['dropdown_duration'] = df_daily['drawdown_days'].max()

            # Ekspozycja na rynek
            active_days = df_daily['ticker'].apply(
                lambda t: t is not None and str(t).upper() not in ['CASHE', 'CASH']).sum()
            results['market_exposure'] = (active_days / len(df_daily)) * 100

            # --- Sharpe / Sortino ---
            risk_free_rate = 0.0
            trading_days_per_year = 252
            df_daily = df_daily.copy()
            df_daily['data'] = pd.to_datetime(df_daily['data'])
            df_daily = df_daily.sort_values('data')

            # 1. Zabezpieczenie osi czasu: Wyrzucamy weekendy
            # UWAGA: Używaj tego tylko do tradycyjnych rynków (akcje, forex). Dla krypto usuń to!
            df_trading_days = df_daily[df_daily['data'].dt.dayofweek < 5].copy()

            daily_returns = df_trading_days['wartosc_portfela'].pct_change()
            daily_returns = daily_returns.replace([np.inf, -np.inf], np.nan).dropna()

            if len(daily_returns) > 1:
                mean_return = daily_returns.mean()
                std_dev = daily_returns.std()  # Pandas używa ddof=1, co jest standardem dla próbki

                # PRAWIDŁOWA matematyka finansowa dla stóp procentowych
                daily_rf = (1 + risk_free_rate) ** (1 / trading_days_per_year) - 1

                # --- WSKAŹNIK SHARPE'A (Standard Złoty) ---
                if std_dev > 0:
                    results['sharpe'] = ((mean_return - daily_rf) / std_dev) * np.sqrt(trading_days_per_year)
                else:
                    results['sharpe'] = 0.0

                # --- WSKAŹNIK SORTINO ---
                downside_array = np.minimum(0, daily_returns - daily_rf)

                # Pandas mean() na array użyje N, co zgadza się ze standardem Sortino (prawdziwa populacja strat)
                downside_std = np.sqrt((downside_array ** 2).mean())

                if downside_std > 0:
                    results['sortino'] = ((mean_return - daily_rf) / downside_std) * np.sqrt(trading_days_per_year)
                else:
                    results['sortino'] = 999.0 if (mean_return - daily_rf) > 0 else 0.0

            else:
                results['sharpe'] = 0.0
                results['sortino'] = 0.0

        results['calmar'] = results['cagr'] / results['maximum_dropdown']

        df_logs = pd.read_sql(
            text(
                "SELECT data_zakupu, data_sprzedazy, zysk_strata_procent FROM gem_change_logs WHERE dzien_rebalansu = :r_day"),
            engine, params={'r_day': rebalance_day}
        )

        if not df_logs.empty:
            df_logs['data_zakupu'] = pd.to_datetime(df_logs['data_zakupu'])
            df_logs['data_sprzedazy'] = pd.to_datetime(df_logs['data_sprzedazy'])

            results['liczba_tranzakcji'] = len(df_logs)
            df_logs['duration'] = (df_logs['data_sprzedazy'] - df_logs['data_zakupu']).dt.days
            results['sredni_czas_dlugosci_pozycji'] = df_logs['duration'].mean()

            winning_trades = (df_logs['zysk_strata_procent'] > 0).sum()
            if results['liczba_tranzakcji'] > 0:
                results['win_ratio'] = (winning_trades / results['liczba_tranzakcji']) * 100

        def clean_val(val):
            if pd.isna(val) or val is None:
                return None
            if isinstance(val, (np.floating, float)):
                if np.isinf(val) or np.isnan(val):
                    return None
                return float(val)
            if isinstance(val, (np.integer, int)):
                return int(val)
            return val

        clean_params = {
            'dzien': int(rebalance_day),
            'zysk': clean_val(results['zysk_strata']),
            'roe': clean_val(results['roe']),
            'cagr': clean_val(results['cagr']),
            'mdd': clean_val(results['maximum_dropdown']),
            'dd_dur': clean_val(results['dropdown_duration']),
            'sortino': clean_val(results['sortino']),
            'sharpe': clean_val(results['sharpe']),
            'lt': clean_val(results['liczba_tranzakcji']),
            'scdp': clean_val(results['sredni_czas_dlugosci_pozycji']),
            'wr': clean_val(results['win_ratio']),
            'me': clean_val(results['market_exposure']),
            'calmar': clean_val(results['calmar'])
        }

        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO gem_zestawienie_dni_rebalancu 
                (dzien_miesiaca, zysk_strata, roe, cagr, maximum_dropdown, dropdown_duration, calmar, sortino, sharpe, liczba_tranzakcji, sredni_czas_dlugosci_pozycji, win_ratio, market_exposure)
                VALUES 
                (:dzien, :zysk, :roe, :cagr, :mdd, :dd_dur, :calmar, :sortino, :sharpe, :lt, :scdp, :wr, :me)
            """), clean_params)

        print(Fore.CYAN + f"Zapisano statystyki dla dnia rebalansu: {rebalance_day}" + Style.RESET_ALL)

    except Exception as e:
        print(Fore.RED + f"Błąd obliczania statystyk: {e}" + Style.RESET_ALL)


def get_settings():
    """Wczytuje konfigurację z dedykowanego pliku w katalogu cfg."""
    from pathlib import Path
    import runpy
    import sys
    from colorama import Fore, Style

    current_dir = Path(__file__).resolve().parent
    path = current_dir / "cfg" / "config_executeBacktestGem.py"

    if not path.exists():
        print(Fore.RED + f"❌ BŁĄD: Plik config_executeBacktestGem.py nie istnieje w {path}" + Style.RESET_ALL)
        sys.exit(1)

    return runpy.run_path(str(path))


def kalkulator_kolejnej_daty(baza_data, wartosc, jednostka, domyslny_dzien, engine):
    """
    Kalkulator chroniący przed dryfem czasu.
    Najpierw wylicza teoretyczną datę wg harmonogramu, a następnie
    koryguje ją w przód do najbliższego dnia z fizycznie dostępnym sygnałem w bazie.
    """
    import calendar
    from datetime import timedelta, datetime
    from sqlalchemy import text
    from colorama import Fore, Style

    # --- ETAP 1: MATEMATYKA KALENDARZOWA (TEORETYCZNY CEL) ---
    if jednostka == 'mo':
        m = baza_data.month - 1 + wartosc
        rok = baza_data.year + m // 12
        miesiac = m % 12 + 1
        _, ostatni_dzien = calendar.monthrange(rok, miesiac)
        dzien = min(domyslny_dzien, ostatni_dzien)
        teoretyczna_data = baza_data.replace(year=rok, month=miesiac, day=dzien)
    elif jednostka == 'w':
        teoretyczna_data = baza_data + timedelta(weeks=wartosc)
    elif jednostka == 'd':
        teoretyczna_data = baza_data + timedelta(days=wartosc)
    else:
        teoretyczna_data = baza_data

    # --- ETAP 2: ZDERZENIE Z RZECZYWISTOŚCIĄ (WERYFIKACJA W BAZIE) ---
    try:
        with engine.connect() as conn:
            # Szukamy najwcześniejszej daty w gem_backtest_price_changes,
            # która jest równa lub późniejsza niż nasz cel.
            query = text("""
                SELECT MIN("dateOfAnalyze") 
                FROM gem_backtest_price_changes 
                WHERE "dateOfAnalyze" >= :td
            """)
            result = conn.execute(query, {'td': teoretyczna_data.strftime('%Y-%m-%d')}).fetchone()

            if result and result[0]:
                # Upewniamy się, że zwracamy obiekt datetime.date
                if isinstance(result[0], str):
                    return datetime.strptime(result[0], '%Y-%m-%d').date()
                elif isinstance(result[0], datetime):
                    return result[0].date()
                return result[0]
            else:
                # Jeśli baza zwróciła None (np. to już koniec dostępnych danych backtestu),
                # zwracamy teoretyczną datę. Pętla while w głównym programie i tak zaraz się zatrzyma.
                return teoretyczna_data
    except Exception as e:
        print(Fore.RED + f"Błąd w kalkulatorze dat (szukanie najbliższej sesji): {e}" + Style.RESET_ALL)
        return teoretyczna_data


def save_backtest_run_to_history(engine):
    """Zapisuje kompletną, płaską strukturę wyników backtestu do bazy z uśrednianiem danych."""
    from sqlalchemy import text
    import pandas as pd
    from colorama import Fore, Style
    import runpy
    from pathlib import Path

    # 1. Pobranie parametrów konfiguracyjnych
    try:
        current_dir = Path(__file__).resolve().parent
        cfg_create = runpy.run_path(str(current_dir / "cfg" / "config_backtest_createTableGem.py"))
        cfg_exec = runpy.run_path(str(current_dir / "cfg" / "config_executeBacktestGem.py"))

        # Wyciągamy surową listę i sprawdzamy warunek dla gotówki
        raw_tickers = cfg_create.get('TICKERS', []).copy()
        czy_dodac_gotowke = cfg_create.get('CZY_DODAC_GOTOWKE', 0)

        if czy_dodac_gotowke == 1 and 'CASHE' not in raw_tickers:
            raw_tickers.append('CASHE')

        # Dopiero teraz konwertujemy czystą listę na stringa dla dalszych potrzeb
        tickers_list = str(raw_tickers)

        skip_per = str(cfg_create.get('SKIP_PERIOD', 'Nie zdefiniowano'))
        doplata = cfg_exec.get('KWOTA_DOPLATY_CO_REBALANCE', 0.0)
        rebalance = str(cfg_exec.get('CO_ILE_REBALANCE', 'Nie zdefiniowano'))
        wartosc_sma = cfg_create.get('WARTOSC_SMA', 0)
        czy_uzywac_sma = str(cfg_create.get('CZY_UZYWAC_SMA', 'Nie zdefiniowano'))
        period_to_analyse = str(cfg_create.get('PERIOD_TO_ANALYSE', 'Nie zdefiniowano'))

        with engine.connect() as conn:
            res = conn.execute(text('SELECT MIN("data_zakupu") FROM gem_change_logs')).fetchone()
            poczatek = str(res[0]) if res and res[0] else "Nieznana"
    except Exception as e:
        print(Fore.RED + f"Błąd odczytu plików konfiguracyjnych: {e}" + Style.RESET_ALL)
        return False

    # 2. Dynamiczne pobranie ram czasowych
    try:
        with engine.connect() as conn:
            res = conn.execute(text('SELECT MAX("dateStop") FROM gem_backtest_price_changes')).fetchone()
            koniec = str(res[0].date()) if res and res[0] else "Nieznana"
    except:
        koniec = "Nieznana"

    # 3. Wyciągnięcie z uśrednieniem (Mean) statystyk z gem_zestawienie_dni_rebalancu
    metrics = {
        'zysk_strata': None, 'roe': None, 'cagr': None, 'maximum_dropdown': None, 'dropdown_duration': None,
        'sortino': None, 'sharpe': None, 'liczba_tranzakcji': None,
        'sredni_czas_dlugosci_pozycji': None, 'win_ratio': None, 'market_exposure': None, 'calmar': None
    }

    try:
        with engine.connect() as conn:
            # Pobieramy całą tabelę, a nie tylko LIMIT 1
            df_stats = pd.read_sql(text("SELECT * FROM gem_zestawienie_dni_rebalancu"), conn)

        if not df_stats.empty:
            col_mapping = {
                'zysk_strata': 'zysk_strata',
                'roe': 'roe',
                'cagr': 'cagr',
                'maximum_dropdown': 'maximum_dropdown',
                'dropdown_duration': 'dropdown_duration',
                'sortino': 'sortino',
                'sharpe': 'sharpe',
                'liczba_tranzakcji': 'liczba_tranzakcji',
                'sredni_czas_dlugosci_pozycji': 'sredni_czas_dlugosci_pozycji',
                'win_ratio': 'win_ratio',
                'market_exposure': 'market_exposure',
                'calmar': 'calmar'
            }

            for db_col, df_col in col_mapping.items():
                if df_col in df_stats.columns:
                    # Obliczamy średnią arytmetyczną z całej kolumny, ignorując z automatu NaN
                    avg_value = df_stats[df_col].mean()
                    if pd.notnull(avg_value):
                        metrics[db_col] = round(float(avg_value), 2)

    except Exception as e:
        print(Fore.RED + f"Błąd podczas wyciągania i uśredniania statystyk: {e}" + Style.RESET_ALL)

    # 5. Zapis do płaskiej bazy
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO gem_backtest_history 
                (poczatek_testu, koniec_testu, kwota_doplat, tickers, co_ile_rebalance, skip_period,
                 zysk_strata, roe, cagr, maximum_dropdown, dropdown_duration, sortino, sharpe, liczba_tranzakcji, 
                 sredni_czas_dlugosci_pozycji, win_ratio, market_exposure, wartosc_sma, czy_uzywac_sma, period_to_analyse, calmar)
                VALUES 
                (:poczatek, :koniec, :doplata, :tickers, :rebalance, :skip_per,
                 :z_s, :roe, :cagr, :mdd, :ddd, :sortino, :sharpe, :lt, :scdp, :wr, :me, :wsma, :cusma, :pta, :cal)
            """), {
                'poczatek': poczatek, 'koniec': koniec, 'doplata': doplata, 'tickers': tickers_list,
                'rebalance': rebalance, 'skip_per': skip_per,
                'z_s': metrics['zysk_strata'], 'roe': metrics['roe'], 'cagr': metrics['cagr'],
                'mdd': metrics['maximum_dropdown'], 'ddd': metrics['dropdown_duration'], 'sortino': metrics['sortino'],
                'sharpe': metrics['sharpe'], 'lt': metrics['liczba_tranzakcji'],
                'scdp': metrics['sredni_czas_dlugosci_pozycji'], 'wr': metrics['win_ratio'],
                'me': metrics['market_exposure'],
                'wsma': wartosc_sma,
                'cusma': czy_uzywac_sma,
                'pta': period_to_analyse,
                'cal': metrics['calmar']
            })
        print(Fore.GREEN + "✅ Wyniki pomyślnie zarchiwizowane w płaskiej tabeli (zastosowano uśrednienie)." + Style.RESET_ALL)
        return True
    except Exception as e:
        print(Fore.RED + f"Błąd zapisu do bazy danych: {e}" + Style.RESET_ALL)
        return False


def load_all_prices_to_ram(engine, start_date, end_date):
    """Pobiera do RAM tylko te ceny, które są potrzebne dla liderów strategii GEM."""
    from sqlalchemy import text
    import pandas as pd
    from colorama import Fore, Style

    print(Fore.CYAN + "Ładowanie historii cen do pamięci RAM (Tylko wymagane tickery)..." + Style.RESET_ALL)

    # KRYTYCZNA OPTYMALIZACJA: Pobieramy tylko tickery, które wygrały ranking
    query = text("""
        SELECT date, ticker, adj_close 
        FROM v_prices_adjusted 
        WHERE timeframe = '1d' 
          AND date >= :start_date 
          AND date <= :end_date
          AND ticker IN (
              SELECT DISTINCT best_performing_ticker_name 
              FROM gem_backtest_price_changes
              WHERE best_performing_ticker_name IS NOT NULL 
                AND best_performing_ticker_name != ''
                AND UPPER(best_performing_ticker_name) NOT IN ('CASH', 'CASHE')
          )
    """)

    try:
        with engine.connect() as conn:
            df = pd.read_sql(query, conn, params={'start_date': start_date, 'end_date': end_date})

        if df.empty:
            print(Fore.YELLOW + "Brak danych cenowych do załadowania dla wybranych dat." + Style.RESET_ALL)
            return {}

        # Tworzymy słownik słowników: price_map['TICKER']['2020-01-01'] = 150.0
        df['date_str'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')

        price_map = {}
        for ticker, group in df.groupby('ticker'):
            price_map[ticker] = dict(zip(group['date_str'], group['adj_close']))

        print(Fore.GREEN + f"Pomyślnie załadowano ceny dla {len(price_map)} aktywnych tickerów." + Style.RESET_ALL)
        return price_map
    except Exception as e:
        print(Fore.RED + f"Błąd ładowania cen do RAM: {e}" + Style.RESET_ALL)
        return {}


def get_price_from_ram(ticker, target_date_str, price_map):
    """Zastępuje odpytywanie bazy danych błyskawicznym słownikiem."""
    if ticker.upper() in ['CASHE', 'CASH']:
        return 1.0

    ticker_data = price_map.get(ticker)
    if not ticker_data:
        return None

    return ticker_data.get(target_date_str)


def update_gem_daily_portfolio_value(portfolio, current_date_str, rebalance_day, price_map, daily_logs):
    """Zoptymalizowana wersja RAM. Zwraca portfel i aktualizuje listę logów."""

    new_asset_value = portfolio.get('asset_value', 0.0)
    current_asset_ticker = portfolio.get('current_asset_ticker')

    if current_asset_ticker and current_asset_ticker.upper() not in ['CASHE', 'CASH']:
        if current_asset_ticker in portfolio.get('assets', {}):
            shares = portfolio['assets'][current_asset_ticker]['shares']
            # UDERZAMY DO RAM, NIE DO BAZY!
            current_price = get_price_from_ram(current_asset_ticker, current_date_str, price_map)

            if current_price is not None:
                new_asset_value = shares * current_price

    elif current_asset_ticker and current_asset_ticker.upper() in ['CASHE', 'CASH']:
        new_asset_value = 0.0

    portfolio['asset_value'] = new_asset_value
    portfolio['total_value'] = portfolio.get('cash', 0.0) + portfolio['asset_value']

    # Zamiast robić powolny INSERT, po prostu dodajemy paczkę danych do listy
    daily_logs.append({
        'data': current_date_str,
        'dzien_rebalansu': rebalance_day,
        'wartosc_portfela': portfolio['total_value'],
        'gotowka': portfolio.get('cash', 0.0),
        'wartosc_aktywow': portfolio['asset_value'],
        'ticker': current_asset_ticker
    })

    return portfolio


def bulk_save_daily_logs(engine, daily_logs):
    """Zapisuje zgromadzoną historię całego backtestu jedną, masową transakcją."""
    from sqlalchemy import text
    from colorama import Fore, Style

    if not daily_logs:
        return

    print(Fore.CYAN + f"Zapisywanie {len(daily_logs)} rekordów wyceny do bazy danych..." + Style.RESET_ALL)

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO gem_daily_portfolio_value 
                (data, dzien_rebalansu, wartosc_portfela, gotowka, wartosc_aktywow, ticker) 
                VALUES (:data, :dzien_rebalansu, :wartosc_portfela, :gotowka, :wartosc_aktywow, :ticker)
                ON CONFLICT (data, dzien_rebalansu) 
                DO UPDATE SET 
                    wartosc_portfela = EXCLUDED.wartosc_portfela,
                    gotowka = EXCLUDED.gotowka,
                    wartosc_aktywow = EXCLUDED.wartosc_aktywow,
                    ticker = EXCLUDED.ticker
            """), daily_logs)

        print(Fore.GREEN + "Pomyślnie zapisano całą krzywą kapitału!" + Style.RESET_ALL)
    except Exception as e:
        print(Fore.RED + f"Błąd masowego zapisu portfela: {e}" + Style.RESET_ALL)