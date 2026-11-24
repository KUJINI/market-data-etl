import time
import pandas as pd
from datetime import datetime, timedelta
import requests
import yfinance as yf
from sqlalchemy import text
from colorama import Fore, Style, init
import sys
import runpy
from pathlib import Path
import warnings
import pandas_market_calendars as mcal
import os
from dotenv import load_dotenv

from system_and_notification import db_schema as dbs, db_connection_configuration as dbcc


# Wyciszamy irytujące ostrzeżenia z Pandas/YFinance
warnings.filterwarnings('ignore')

# Globalny licznik dla limitów Tiingo
TIINGO_REQUEST_COUNT = 0

load_dotenv()


def get_settings(mode_selection):
    current_dir = Path(__file__).resolve().parent
    file_name = "config_automatic.py" if mode_selection == 1 else "config_editable.py"
    path = current_dir / "cfg" / file_name

    if not path.exists():
        raise FileNotFoundError(f"❌ BŁĄD: Plik {file_name} nie istnieje w /cfg/")
    return runpy.run_path(str(path))


def check_routing(ticker, timeframe):
    """Decyduje o API na podstawie nazwy tickera oraz timeframe'u."""
    # Tiingo EOD nie obsługuje intraday. Jeśli jest 'm' lub 'h' w interwale -> wymuś YFinance
    if 'm' in timeframe or 'h' in timeframe:
        return "YFINANCE"

    # Twarde reguły tekstowe dla tickera
    if '.' in ticker or '=' in ticker or '^' in ticker:
        return "YFINANCE"

    engine = dbcc.get_engine()
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM prices_tiingo_dictionary WHERE ticker = :t"), {"t": ticker.upper()}).scalar()
    engine.dispose()

    if exists:
        return "TIINGO"
    return "YFINANCE"


def enforce_tiingo_rate_limit():
    engine = dbcc.get_engine()
    now = datetime.now()

    # KROK 1: Weryfikacja limitu ubezpieczona przed błędem bazy
    try:
        with engine.begin() as conn:
            row = conn.execute(text("""
                SELECT request_count, window_start 
                FROM api_rate_limits 
                WHERE service_name = 'TIINGO'
            """)).fetchone()

            if not row:
                conn.execute(text("""
                    INSERT INTO api_rate_limits (service_name, request_count, window_start) 
                    VALUES ('TIINGO', 1, :now)
                """), {"now": now})
                return

            count = row.request_count
            window_start = row.window_start
            time_elapsed = (now - window_start).total_seconds()

            if time_elapsed >= 3600:
                conn.execute(text("""
                    UPDATE api_rate_limits 
                    SET request_count = 1, window_start = :now 
                    WHERE service_name = 'TIINGO'
                """), {"now": now})
                return

            if count >= 49:
                sleep_time = 3605 - time_elapsed
            else:
                conn.execute(text("""
                    UPDATE api_rate_limits 
                    SET request_count = request_count + 1 
                    WHERE service_name = 'TIINGO'
                """))
                return

    except Exception as e:
        print(Fore.RED + f"❌ Krytyczny błąd DB przy sprawdzaniu limitu Tiingo: {e}" + Style.RESET_ALL)
        raise

    # KROK 2: Oczekiwanie
    if sleep_time > 0:
        print(Fore.MAGENTA + f"⏳ Limit Tiingo na wyczerpaniu. Usypiam skrypt na {int(sleep_time)} sekund..." + Style.RESET_ALL)
        time.sleep(sleep_time)

        # KROK 3: Aktualizacja po wybudzeniu
        try:
            with engine.begin() as conn:
                conn.execute(text("""
                    UPDATE api_rate_limits
                    SET request_count = 1, window_start = :new_now
                    WHERE service_name = 'TIINGO'
                """), {"new_now": datetime.now()})
        except Exception as e:
            print(Fore.RED + f"❌ Błąd DB podczas resetowania limitu po przebudzeniu: {e}" + Style.RESET_ALL)
            raise


def download_tiingo(ticker, start_date, api_key, timeframe, return_meta=False):
    enforce_tiingo_rate_limit()
    meta = {
        "api_min": None,
        "api_max": None,
        "error": None,
        "detail": ""
    }

    headers = {'Content-Type': 'application/json', 'Authorization': f'Token {api_key}'}
    url = f"https://api.tiingo.com/tiingo/daily/{ticker}/prices"

    # Wymuszenie jawnego endDate z dniem zapasu rozwiązuje problem stref czasowych o 4:00 rano
    end_date = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

    params = {
        'startDate': start_date,
        'endDate': end_date
    }

    # Używamy resampleFreq wyłącznie, gdy pytamy o interwały wyższe niż 1d.
    # Używanie resamplera dla 1d na API Tiingo opóźnia publikację aktualnej świeczki.
    if timeframe != '1d':
        freq_map = {'1wk': 'weekly', '1mo': 'monthly'}
        resample_freq = freq_map.get(timeframe)
        if resample_freq:
            params['resampleFreq'] = resample_freq

    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 200:
        data = response.json()
        if not data:
            meta["detail"] = f"HTTP 200, pusta odpowiedź JSON (od {start_date} do {end_date})"
            return (pd.DataFrame(), meta) if return_meta else pd.DataFrame()

        df = pd.DataFrame(data)
        df = df.rename(columns={
            'date': 'date', 'open': 'open', 'high': 'high',
            'low': 'low', 'close': 'close', 'volume': 'volume',
            'divCash': 'dividend', 'splitFactor': 'split'
        })
        df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)

        meta["api_min"] = df['date'].min()
        meta["api_max"] = df['date'].max()
        meta["detail"] = f"HTTP 200, wierszy z API: {len(df)}"
        return (df, meta) if return_meta else df
    else:
        err_msg = f"HTTP {response.status_code}: {response.text[:180]}"
        print(
            Fore.RED + f"⚠️ Błąd API Tiingo dla {ticker} (Kod {response.status_code}): {response.text}" + Style.RESET_ALL)
        meta["error"] = err_msg
        return (pd.DataFrame(), meta) if return_meta else pd.DataFrame()


def download_yfinance(ticker, start_date, timeframe, return_meta=False):
    from colorama import Fore, Style

    meta = {
        "api_min": None,
        "api_max": None,
        "error": None,
        "detail": ""
    }

    try:
        # Przechwytujemy datę 1900-01-01 i zamieniamy na bezpieczne 'max'
        if start_date == '1900-01-01':
            df = yf.download(
                ticker,
                period='max',
                interval=timeframe,
                auto_adjust=False,
                actions=True,
                progress=False
            )
        else:
            df = yf.download(
                ticker,
                start=start_date,
                interval=timeframe,
                auto_adjust=False,
                actions=True,
                progress=False
            )

        if df.empty:
            meta["detail"] = f"Pusty DataFrame z yfinance (start={start_date}, tf={timeframe})"
            return (pd.DataFrame(), meta) if return_meta else pd.DataFrame()

        df = df.reset_index()

        # Oczyszczanie z ewentualnego MultiIndexu
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)

        # Standaryzacja kolumny daty
        date_col = 'Date' if 'Date' in df.columns else 'Datetime'
        df = df.rename(columns={
            date_col: 'date', 'Open': 'open', 'High': 'high',
            'Low': 'low', 'Close': 'close', 'Volume': 'volume',
            'Dividends': 'dividend', 'Stock Splits': 'split'
        })

        # Skoro usunęliśmy actions = True, musimy ręcznie zdefiniować brakujące kolumny
        if 'dividend' not in df.columns:
            df['dividend'] = 0.0

        if 'split' not in df.columns:
            df['split'] = 1.0
        else:
            df['split'] = df['split'].replace(0.0, 1.0)

        # Usunięcie strefy czasowej
        if df['date'].dt.tz is not None:
            df['date'] = df['date'].dt.tz_localize(None)

        meta["api_min"] = df['date'].min()
        meta["api_max"] = df['date'].max()
        meta["detail"] = f"Wierszy z API: {len(df)}"
        return (df, meta) if return_meta else df
    except Exception as e:
        print(Fore.RED + f"Błąd YFinance [{ticker}]: {e}" + Style.RESET_ALL)
        meta["error"] = f"Wyjątek YFinance: {str(e)[:180]}"
        return (pd.DataFrame(), meta) if return_meta else pd.DataFrame()


def rebuild_adj_factors(conn, ticker):
    # Czyścimy starą historię mnożników dla tego waloru
    conn.execute(text("DELETE FROM prices_adj_factors WHERE ticker = :t"), {"t": ticker})

    # Inicjalizujemy stelaż bazowy
    conn.execute(text("""
        INSERT INTO prices_adj_factors (ticker, start_date, end_date, multiplier)
        VALUES (:t, '1900-01-01', '2099-12-31', 1.0)
    """), {"t": ticker})

    # Pobieramy chronologicznie wszystkie odnotowane zdarzenia dla waloru
    actions = conn.execute(text("""
        SELECT date, dividend, split
        FROM prices_raw
        WHERE ticker = :t AND timeframe = '1d' AND (dividend > 0 OR split != 1.0)
        ORDER BY date ASC
    """), {"t": ticker}).fetchall()

    for action in actions:
        ex_date = action.date
        div = float(action.dividend)
        split = float(action.split)

        adj_ratio = 1.0
        if split != 1.0 and split > 0:
            adj_ratio *= (1.0 / split)

        if div > 0:
            close_prev = conn.execute(text("""
                SELECT close FROM prices_raw
                WHERE ticker = :t AND date < :dt AND timeframe = '1d'
                ORDER BY date DESC LIMIT 1
            """), {"t": ticker, "dt": ex_date}).scalar()

            if close_prev and float(close_prev) > 0:
                adj_ratio *= ((float(close_prev) - div) / float(close_prev))

        if adj_ratio == 1.0:
            continue

        # Wsteczna aktualizacja wyliczonych wcześniej okresów
        conn.execute(text("""
            UPDATE prices_adj_factors
            SET multiplier = multiplier * :adj
            WHERE ticker = :t AND end_date < :ex_date
        """), {"adj": adj_ratio, "t": ticker, "ex_date": ex_date})

        # Przecięcie otwartego okresu
        open_period = conn.execute(text("""
            SELECT start_date, multiplier FROM prices_adj_factors
            WHERE ticker = :t AND end_date = '2099-12-31'
        """), {"t": ticker}).fetchone()

        if open_period:
            close_date = ex_date - timedelta(days=1)
            conn.execute(text("""
                UPDATE prices_adj_factors
                SET end_date = :close_date, multiplier = multiplier * :adj
                WHERE ticker = :t AND end_date = '2099-12-31'
            """), {"close_date": close_date, "adj": adj_ratio, "t": ticker})

            conn.execute(text("""
                INSERT INTO prices_adj_factors (ticker, start_date, end_date, multiplier)
                VALUES (:t, :start, '2099-12-31', :orig_mult)
            """), {"t": ticker, "start": ex_date, "orig_mult": float(open_period.multiplier)})


def filter_and_log_trading_days(df, ticker):
    if df.empty:
        return df

    # Proste mapowanie giełd po sufiksie YFinance
    exchange_code = 'NYSE'  # Domyślnie US
    if ticker.endswith('.WA'):
        exchange_code = 'XWAR'  # GPW
    elif ticker.endswith('.L'):
        exchange_code = 'LSE'  # Londyn
    elif ticker.endswith('.F') or ticker.endswith('.DE'):
        exchange_code = 'XFRA'  # Frankfurt

    try:
        cal = mcal.get_calendar(exchange_code)

        # Ograniczamy sprawdzany zakres do tego, co faktycznie jest w DataFrame
        min_date = df['date'].min().strftime('%Y-%m-%d')
        max_date = df['date'].max().strftime('%Y-%m-%d')

        schedule = cal.schedule(start_date=min_date, end_date=max_date)
        trading_days = set(schedule.index.strftime('%Y-%m-%d').tolist())

        original_count = len(df)

        df['date_str'] = df['date'].dt.strftime('%Y-%m-%d')
        df_filtered = df[df['date_str'].isin(trading_days)].drop(columns=['date_str'])

        deleted_count = original_count - len(df_filtered)

        if deleted_count > 0:
            dbs.log_universal(engine=None, mode="Cleanup", additional_info=f"Ticker: {ticker} | Usunięte dni niehandlowe: {deleted_count}")

        return df_filtered

    except Exception as e:
        dbs.log_universal(
            engine=None,
            mode="Cleanup",
            additional_info=f"Ticker: {ticker} | Giełda: {exchange_code}",
            error_traceback=f"Błąd mcal: {str(e)[:200]}",
            status="Failed"
        )
        # Profilaktycznie usuwamy kolumnę date_str w razie błędu, żeby nie popsuła dalszego INSERTu
        if 'date_str' in df.columns:
            df = df.drop(columns=['date_str'])
        return df


def process_ticker(ticker, api_key, cfg, run_id=None):
    if not run_id:
        run_id = datetime.now().strftime('%Y%m%d_%H%M%S')

    timeframe = cfg.get('TIMEFRAME', '1d')
    engine = dbcc.get_engine()

    route = check_routing(ticker, timeframe)
    route_color = Fore.CYAN if route == "TIINGO" else Fore.YELLOW
    print(f"\n{route_color}Przetwarzam: {ticker} | TF: {timeframe} | (Routing: {route}){Style.RESET_ALL}")

    with engine.begin() as conn:
        exists = conn.execute(text("SELECT 1 FROM prices_adj_factors WHERE ticker = :t"), {"t": ticker}).fetchone()
        if not exists:
            conn.execute(text("""
                INSERT INTO prices_adj_factors (ticker, start_date, end_date, multiplier)
                VALUES (:t, '1900-01-01', '2099-12-31', 1.0)
            """), {"t": ticker})

    with engine.connect() as conn:
        last_dt = conn.execute(text("""
            SELECT MAX(date) FROM prices_raw 
            WHERE ticker = :t AND timeframe = :tf
        """), {"t": ticker, "tf": timeframe}).scalar()

    db_before = pd.to_datetime(last_dt).tz_localize(None) if last_dt is not None else None

    if db_before is not None:
        start_date = (db_before - timedelta(days=1)).strftime('%Y-%m-%d')
    else:
        if timeframe == '1m':
            start_date = (datetime.now() - timedelta(days=6)).strftime('%Y-%m-%d')
        elif 'm' in timeframe:
            start_date = (datetime.now() - timedelta(days=59)).strftime('%Y-%m-%d')
        elif 'h' in timeframe:
            start_date = (datetime.now() - timedelta(days=729)).strftime('%Y-%m-%d')
        else:
            start_date = '1900-01-01'

    if route == "TIINGO":
        df, meta = download_tiingo(ticker, start_date, api_key, timeframe, return_meta=True)
    else:
        df, meta = download_yfinance(ticker, start_date, timeframe, return_meta=True)

    api_min = meta.get("api_min")
    api_max = meta.get("api_max")

    if meta.get("error"):
        log_ticker_diagnostic(
            run_id=run_id, ticker=ticker, source=route, timeframe=timeframe,
            db_before=db_before, api_min=api_min, api_max=api_max, db_after=db_before,
            rows_saved=0, status="ERROR", details=meta["error"]
        )
        engine.dispose()
        return False

    if df.empty:
        print(Fore.LIGHTBLACK_EX + f"Brak nowych danych dla {ticker}." + Style.RESET_ALL)
        log_ticker_diagnostic(
            run_id=run_id, ticker=ticker, source=route, timeframe=timeframe,
            db_before=db_before, api_min=None, api_max=None, db_after=db_before,
            rows_saved=0, status="EMPTY", details=meta.get("detail", "Brak danych z API")
        )
        engine.dispose()
        return False

    ohlc_cols = ['open', 'high', 'low', 'close']
    df[ohlc_cols] = df[ohlc_cols].apply(pd.to_numeric, errors='coerce')
    df = df.dropna(subset=ohlc_cols)
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce').fillna(0)

    if df.empty:
        print(Fore.LIGHTBLACK_EX + f"Brak poprawnych (kompletnych) danych dla {ticker} po oczyszczeniu z NaN." + Style.RESET_ALL)
        dbs.log_universal(engine, f"Ticker: {ticker}", "Error_NaN", "Data_Check")
        log_ticker_diagnostic(
            run_id=run_id, ticker=ticker, source=route, timeframe=timeframe,
            db_before=db_before, api_min=api_min, api_max=api_max, db_after=db_before,
            rows_saved=0, status="FILTERED_OUT", details="Wszystkie wiersze odrzucone przez filtr NaN w OHLC"
        )
        engine.dispose()
        return False

    if route == "YFINANCE" and timeframe == '1d':
        df = filter_and_log_trading_days(df, ticker)

        if df.empty:
            print(Fore.LIGHTBLACK_EX + f"Brak danych dla {ticker} po odfiltrowaniu dni niehandlowych." + Style.RESET_ALL)
            log_ticker_diagnostic(
                run_id=run_id, ticker=ticker, source=route, timeframe=timeframe,
                db_before=db_before, api_min=api_min, api_max=api_max, db_after=db_before,
                rows_saved=0, status="FILTERED_OUT", details="Wszystkie wiersze odrzucone przez kalendarz mcal"
            )
            engine.dispose()
            return False

    min_date = df['date'].min().strftime('%Y-%m-%d %H:%M')
    max_date = df['date'].max().strftime('%Y-%m-%d %H:%M')
    records_count = len(df)

    needs_rebuild = False

    with engine.begin() as conn:
        for _, row in df.iterrows():
            date_val = row['date']
            close_val = float(row['close'])
            div = float(row['dividend'])
            split = float(row['split'])

            try:
                old_state = conn.execute(text("""
                    SELECT dividend, split FROM prices_raw
                    WHERE ticker = :t AND date = :dt AND timeframe = :tf
                """), {"t": ticker, "dt": date_val, "tf": timeframe}).fetchone()

                old_div = float(old_state.dividend) if old_state else 0.0
                old_split = float(old_state.split) if old_state else 1.0

                if div != old_div or split != old_split:
                    needs_rebuild = True

                conn.execute(text("""
                    INSERT INTO prices_raw (ticker, date, open, high, low, close, volume, dividend, split, source, timeframe)
                    VALUES (:t, :dt, :o, :h, :l, :c, :v, :div, :s, :src, :tf)
                    ON CONFLICT (ticker, date, timeframe) DO UPDATE 
                    SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, 
                        close = EXCLUDED.close, volume = EXCLUDED.volume, 
                        dividend = EXCLUDED.dividend, split = EXCLUDED.split
                """), {
                    "t": ticker, "dt": date_val, "o": float(row['open']), "h": float(row['high']),
                    "l": float(row['low']), "c": close_val, "v": int(row['volume']),
                    "div": div, "s": split, "src": route, "tf": timeframe
                })
            except Exception as e:
                print(Fore.RED + f"Błąd zapisu surowego dla {ticker} w dacie {date_val}: {e}" + Style.RESET_ALL)

        if needs_rebuild and timeframe == '1d':
            rebuild_adj_factors(conn, ticker)

    with engine.connect() as conn:
        after_dt = conn.execute(text("""
            SELECT MAX(date) FROM prices_raw 
            WHERE ticker = :t AND timeframe = :tf
        """), {"t": ticker, "tf": timeframe}).scalar()

    db_after = pd.to_datetime(after_dt).tz_localize(None) if after_dt is not None else None
    is_actually_newer = (db_before is None and db_after is not None) or (
        db_before is not None and db_after is not None and db_after > db_before
    )

    if is_actually_newer:
        diag_status = "UPDATED"
        diag_detail = f"Pobrano nową świeczkę (API: {min_date} -> {max_date}, rek: {records_count})"
        print(Fore.CYAN + f"Zaktualizowano przedział: {min_date} -> {max_date}" + Style.RESET_ALL)
        print(Fore.GREEN + f"✅ Zakończono {ticker}." + Style.RESET_ALL)
    else:
        diag_status = "STALE"
        diag_detail = (
            f"API zwróciło tylko istniejące daty (API max: {max_date}, DB przed: "
            f"{db_before.strftime('%Y-%m-%d %H:%M') if db_before else 'Brak'})"
        )
        print(Fore.YELLOW + f"⚠️ {ticker}: API nie zwróciło nowszej daty (API max: {max_date})." + Style.RESET_ALL)

    log_ticker_diagnostic(
        run_id=run_id, ticker=ticker, source=route, timeframe=timeframe,
        db_before=db_before, api_min=api_min, api_max=api_max, db_after=db_after,
        rows_saved=records_count, status=diag_status, details=diag_detail
    )

    engine.dispose()
    return is_actually_newer


def log_ticker_diagnostic(
    run_id,
    ticker,
    source,
    timeframe,
    db_before,
    api_min,
    api_max,
    db_after,
    rows_saved,
    status,
    details
):
    """
    Zapisuje pełny ślad diagnostyczny dla tickera: stan DB przed, zakres z API, stan DB po oraz powód.
    """
    def _clean_dt(val):
        if val is None or pd.isna(val):
            return None
        return val.to_pydatetime() if hasattr(val, "to_pydatetime") else val

    engine = dbcc.get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO ticker_download_logs (
                    run_id, ticker, source, timeframe,
                    db_before, api_min, api_max, db_after,
                    rows_saved, status, details
                )
                VALUES (
                    :run_id, :ticker, :source, :timeframe,
                    :db_before, :api_min, :api_max, :db_after,
                    :rows_saved, :status, :details
                )
            """), {
                "run_id": run_id,
                "ticker": ticker,
                "source": source,
                "timeframe": timeframe,
                "db_before": _clean_dt(db_before),
                "api_min": _clean_dt(api_min),
                "api_max": _clean_dt(api_max),
                "db_after": _clean_dt(db_after),
                "rows_saved": rows_saved,
                "status": status,
                "details": str(details)[:500]
            })
    except Exception as e:
        print(Fore.RED + f"Nie udało się zapisać diagnostyki tickera {ticker}: {e}" + Style.RESET_ALL)
    finally:
        engine.dispose()


def main(mode_selection=0):
    import os
    import getpass

    init(autoreset=True)
    run_id = datetime.now().strftime('%Y%m%d_%H%M%S')
    engine = dbcc.get_engine()

    dbs.init_db_schema(engine)

    # --- LOGIKA TRYBU 2 (REBUILD) ---
    if mode_selection == 2:
        with engine.begin() as conn:
            tickers_to_rebuild = [row[0] for row in
                                  conn.execute(text("SELECT DISTINCT ticker FROM prices_raw")).fetchall()]

            print("Tickery do pobrania na nowo: ", tickers_to_rebuild)

            if tickers_to_rebuild:
                insert_data = [{"ticker": t} for t in tickers_to_rebuild]
                conn.execute(text("""
                    INSERT INTO price_before_rebuild (nazwaTickera)
                    VALUES (:ticker);
                """), insert_data)

        dbs.clean_table(engine, "prices_raw")
        dbs.clean_table(engine, "prices_adj_factors")
        cfg = get_settings(1)
    else:
        cfg = get_settings(mode_selection)

    engine.dispose()

    try:
        api_key = os.getenv("TIINGO_API_KEY")
        if not api_key:
            raise AttributeError
    except AttributeError:
        print(Fore.RED + "❌ BŁĄD KRYTYCZNY: Brak zmiennej TIINGO_API_KEY w pliku .env!" + Style.RESET_ALL)
        return

    try:
        timeframe = cfg.get('TIMEFRAME', '1d')
        if mode_selection == 2:
            only_update = 0
            tickers_from_cfg = []
        else:
            tickers_from_cfg = cfg['TICKERS']
            only_update = cfg['ONLY_UPDATE_EXIST']
    except KeyError as e:
        print(Fore.RED + f"❌ BŁĄD KRYTYCZNY: Brak klucza {e} w pliku config!" + Style.RESET_ALL)
        return

    if mode_selection == 1:
        mode_label = "AUTOMATIC"
    elif mode_selection == 2:
        mode_label = "REBUILD"
    else:
        mode_label = "EDITABLE"

    env_context = f"run_id={run_id} | user={getpass.getuser()} | cwd={os.getcwd()}"
    print(f"{Fore.YELLOW}🚀 START Hybryda: Config {mode_label} | TF: {timeframe} | {env_context}{Style.RESET_ALL}")

    if mode_selection == 2:
        active_tickers = tickers_to_rebuild
        if not active_tickers:
            print(Fore.RED + "❌ BŁĄD: Baza była pusta przed kasowaniem, brak tickerów do odbudowy!" + Style.RESET_ALL)
            return
    elif only_update == 1:
        check_engine = dbcc.get_engine()
        with check_engine.connect() as conn:
            active_tickers = [row[0] for row in
                              conn.execute(text("SELECT DISTINCT ticker FROM prices_raw WHERE timeframe = :tf"),
                                           {"tf": timeframe}).fetchall()]
        check_engine.dispose()
        if not active_tickers:
            print(Fore.RED + f"❌ BŁĄD: Baza dla interwału {timeframe} jest pusta. Brak tickerów do aktualizacji." + Style.RESET_ALL)
            return
    else:
        active_tickers = tickers_from_cfg
        if not active_tickers:
            print(Fore.RED + "❌ BŁĄD: Lista TICKERS w configu jest pusta!" + Style.RESET_ALL)
            return

    try:
        success_count = 0
        skipped_or_failed = []

        for ticker in active_tickers:
            try:
                is_success = process_ticker(ticker, api_key, cfg, run_id=run_id)
                if is_success:
                    success_count += 1
                else:
                    skipped_or_failed.append(ticker)
            except Exception as e:
                print(Fore.RED + f"Krytyczny błąd wewnątrz process_ticker dla {ticker}: {e}" + Style.RESET_ALL)
                skipped_or_failed.append(ticker)
                dbs.log_universal(engine, f"Ticker: {ticker}", "Failed", mode_label, error_traceback=f"Exception: {str(e)[:200]}")
                log_ticker_diagnostic(
                    run_id=run_id, ticker=ticker, source="UNKNOWN", timeframe=timeframe,
                    db_before=None, api_min=None, api_max=None, db_after=None,
                    rows_saved=0, status="ERROR", details=f"Crash process_ticker: {str(e)[:250]}"
                )

        summary_info = f"Nowe dane: {success_count}/{len(active_tickers)} | {env_context}"
        if skipped_or_failed:
            summary_info += f" | Bez nowych danych/Błąd: {', '.join(skipped_or_failed)}"

        dbs.log_universal(engine, summary_info, "Success", mode_label)
        print(Fore.GREEN + "\n🎉 Aktualizacja hybrydowa zakończona!" + Style.RESET_ALL)
    except Exception as e:
        dbs.log_universal(engine, f"Krytyczny błąd w głównej pętli | {env_context}", "Failed", mode_label, error_traceback="Error: {str(e)[:200]}")
        raise


if __name__ == "__main__":
    selection = 1 if "--auto" in sys.argv else 0
    if len(sys.argv) > 1 and sys.argv[1] in ['0', '1', '2']:
        selection = int(sys.argv[1])

    main(mode_selection=selection)
