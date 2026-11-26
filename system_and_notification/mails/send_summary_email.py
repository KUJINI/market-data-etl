import smtplib
import os
import sys
from pathlib import Path
from datetime import date, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from sqlalchemy import text
from system_and_notification import db_connection_configuration as dbcc
from dotenv import load_dotenv

load_dotenv()

# --- KONFIGURACJA MAILOWA ---
SENDER_EMAIL = os.getenv("EMAIL_USER")
SENDER_PASSWORD = os.getenv("EMAIL_PASS")


def load_email_config():
    import runpy
    from pathlib import Path

    config_path = Path(__file__).resolve().parent / "config_email.py"

    default_config = {
        "subject_prefix": "Raport aktualizacji inwestycji",
        "receivers": []
    }

    if not config_path.exists():
        return default_config

    try:
        cfg = runpy.run_path(str(config_path))
        return {
            "subject_prefix": cfg.get("SUBJECT_PREFIX", default_config["subject_prefix"]),
            "receivers": cfg.get("RECEIVERS", default_config["receivers"])
        }
    except Exception as e:
        print(f"BŁĄD: Nie można odczytać pliku konfiguracyjnego config_email.py: {e}")
        return default_config


def fetch_summary_data():
    engine = dbcc.get_engine()

    today_date = date.today()
    # Ostatni dzień sesyjny przed dzisiaj (w poniedziałek/weekend -> piątek)
    target_date = get_last_trading_day(today_date)

    summary = {
        "prices_updated": 0,
        "updated_tickers": [],
        "ticker_diagnostics": [],
        "gem_ticker": "Brak danych",
        "gem_diff": "Brak danych",
        "gem_date": "Brak",
        "logs": [],
        "db_error": None,
        "target_date_str": target_date.strftime("%d-%m-%Y"),
        "today_date_str": today_date.strftime("%d-%m-%Y")
    }

    try:
        with engine.connect() as conn:
            # 1. Jakie unikalne tickery (dla ostatniej sesji) wraz ze źródłem (YFINANCE/TIINGO)
            q_prices = text("SELECT DISTINCT ticker, source FROM v_prices_adjusted WHERE DATE(date) = :target_date")
            tickers_res = conn.execute(q_prices, {"target_date": target_date}).fetchall()

            tickers_list = sorted([f"{row[0]} ({row[1] if row[1] else 'Nieznane'})" for row in tickers_res])
            summary["updated_tickers"] = tickers_list
            summary["prices_updated"] = len(tickers_list)

            # 2. Szczegółowa diagnostyka z ostatniego dzisiejszego przebiegu skryptu pobierającego
            try:
                q_diag = text("""
                    SELECT ticker, source, timeframe, db_before, api_min, api_max, db_after, rows_saved, status, details
                    FROM ticker_download_logs
                    WHERE run_id = (
                        SELECT run_id
                        FROM ticker_download_logs
                        WHERE DATE(execution_date) = :today_date
                        ORDER BY execution_date DESC
                        LIMIT 1
                    )
                    ORDER BY 
                        CASE status
                            WHEN 'ERROR' THEN 1
                            WHEN 'EMPTY' THEN 2
                            WHEN 'FILTERED_OUT' THEN 3
                            WHEN 'STALE' THEN 4
                            ELSE 5
                        END,
                        ticker ASC
                """)
                diag_res = conn.execute(q_diag, {"today_date": today_date}).fetchall()
                summary["ticker_diagnostics"] = [dict(row._mapping) for row in diag_res]
            except Exception:
                summary["ticker_diagnostics"] = []

            # 3. Wyniki GEM (Najnowszy dostępny wpis)
            q_gem = text("""
                SELECT best_performing_ticker_name, best_performing_ticker_difference, DATE("dateOfAnalyze") 
                FROM gem_price_changes 
                ORDER BY "dateOfAnalyze" DESC LIMIT 1
            """)
            gem_res = conn.execute(q_gem).fetchone()
            if gem_res:
                summary["gem_ticker"] = gem_res[0]
                summary["gem_diff"] = round(float(gem_res[1]), 4) if gem_res[1] is not None else "Brak"
                summary["gem_date"] = gem_res[2].strftime("%d-%m-%Y") if gem_res[2] else "Brak"

            # 4. Alerty / Logi (z DZISIAJ)
            q_logs = text("""
                SELECT execution_date, file, function, mode, additional_info, status, duration_ms
                FROM logs_functions
                WHERE DATE(execution_date) = :today_date
                ORDER BY execution_date DESC
            """)
            logs_res = conn.execute(q_logs, {"today_date": today_date}).fetchall()
            summary["logs"] = [dict(row._mapping) for row in logs_res]

    except Exception as e:
        summary["db_error"] = str(e)
    finally:
        engine.dispose()

    return summary


def build_html_email(summary):
    target_date_str = summary["target_date_str"]
    today_date_str = summary["today_date_str"]
    gem_date_str = summary["gem_date"]

    if summary["db_error"]:
        return f"<h2>Wystąpił błąd podczas odpytywania bazy danych!</h2><p>{summary['db_error']}</p>"

    def _fmt_dt(val):
        if val is None:
            return "Brak"
        if hasattr(val, "strftime"):
            return val.strftime("%Y-%m-%d %H:%M") if (val.hour or val.minute) else val.strftime("%Y-%m-%d")
        return str(val)

    tickers_display = ", ".join(summary["updated_tickers"]) if summary["updated_tickers"] else "Brak"

    diag_rows_html = ""
    if summary.get("ticker_diagnostics"):
        status_colors = {
            "UPDATED": "#27AE60",
            "STALE": "#E67E22",
            "EMPTY": "#C0392B",
            "FILTERED_OUT": "#C0392B",
            "ERROR": "#C0392B"
        }
        for d in summary["ticker_diagnostics"]:
            st = str(d.get("status", "UNKNOWN"))
            st_color = status_colors.get(st, "#333333")
            diag_rows_html += f"""
                <tr>
                    <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">{d.get('ticker', '')}</td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{d.get('source', '')}</td>
                    <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold; color: {st_color};">{st}</td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{_fmt_dt(d.get('db_before'))}</td>
                    <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold; color: #1F618D;">{_fmt_dt(d.get('api_max'))}</td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{_fmt_dt(d.get('db_after'))}</td>
                    <td style="padding: 8px; border: 1px solid #ddd; font-size: 12px; color: #555;">{d.get('details', '')}</td>
                </tr>
            """
    else:
        diag_rows_html = "<tr><td colspan='7' style='padding: 8px; border: 1px solid #ddd;'>Brak wpisów w ticker_download_logs z dzisiejszego dnia.</td></tr>"

    logs_html = ""
    if summary["logs"]:
        for log in summary["logs"]:
            color = "red" if "error" in str(log.get('status', '')).lower() else "black"
            logs_html += f"""
                <tr style="color: {color};">
                    <td style="padding: 8px; border: 1px solid #ddd;">{log.get('execution_date', '')}</td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{log.get('file', '')}</td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{log.get('function', '')}</td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{log.get('mode', '')}</td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{log.get('additional_info', '')}</td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{log.get('status', '')}</td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{log.get('duration_ms', '')}</td>
                </tr>
            """
    else:
        logs_html = f"<tr><td colspan='6' style='padding: 8px; border: 1px solid #ddd;'>Brak logów z dzisiejszego poranka ({today_date_str}).</td></tr>"

    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333;">
        <h2>Podsumowanie aktualizacji bazy za ostatnią sesję: {target_date_str}</h2>
        <p><i>Raport wygenerowany po porannym przebiegu crona z dnia {today_date_str}</i></p>

        <h3>1. Pobieranie cen (ostatnia sesja: {target_date_str})</h3>
        <p>Liczba unikalnych tickerów w <b>v_prices_adjusted</b>: 
           <span style="font-size: 18px; font-weight: bold; color: #2E86C1;">{summary['prices_updated']}</span>
        </p>
        <p style="font-size: 14px; color: #555;">
           <b>Zaktualizowane tickery:</b> {tickers_display}
        </p>

        <h3>2. Diagnostyka API vs Baza (ostatni przebieg z {today_date_str})</h3>
        <table style="border-collapse: collapse; width: 100%; max-width: 1000px; font-size: 13px;">
            <tr style="background-color: #f2f2f2;">
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Ticker</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Źródło</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Status</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">DB przed</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">API max data</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">DB po</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Szczegóły / Powód</th>
            </tr>
            {diag_rows_html}
        </table>

        <h3>3. Kalkulacja GEM (Najnowszy odczyt z: <span style="color: #E67E22;">{gem_date_str}</span>)</h3>
        <ul>
            <li>Rekomendowany ticker do zakupu: <b style="color: #27AE60;">{summary['gem_ticker']}</b></li>
            <li>Różnica / Momentum: <b>{summary['gem_diff']}</b></li>
        </ul>

        <h3>4. Logi systemowe z wykonania crona (z {today_date_str})</h3>
        <table style="border-collapse: collapse; width: 100%; max-width: 1000px; font-size: 13px;">
            <tr style="background-color: #f2f2f2;">
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Data wykonania</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Plik</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Funkcja</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Tryb</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Dodatkowe informacje</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Status</th>
                <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Czas trwania (ms)</th>
            </tr>
            {logs_html}
        </table>
      </body>
    </html>
    """
    return html


def get_last_trading_day(reference_date=None):
    """
    Zwraca ostatni dzień sesyjny (NYSE) przed reference_date.
    W sobotę, niedzielę i poniedziałek zwraca piątek (lub wcześniejszy dzień przy świętach).
    """
    if reference_date is None:
        reference_date = date.today()

    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar('NYSE')
        start_check = reference_date - timedelta(days=10)
        end_check = reference_date - timedelta(days=1)
        schedule = nyse.schedule(start_date=start_check.strftime('%Y-%m-%d'), end_date=end_check.strftime('%Y-%m-%d'))
        if not schedule.empty:
            return schedule.index[-1].date()
    except Exception:
        pass

    # Fallback na dni tygodnia (0=pon, ..., 5=sob, 6=nd)
    wd = reference_date.weekday()
    if wd == 0:      # poniedziałek -> piątek
        return reference_date - timedelta(days=3)
    elif wd == 6:    # niedziela -> piątek
        return reference_date - timedelta(days=2)
    else:
        return reference_date - timedelta(days=1)


def send_email(log_file_path=None):
    if not SENDER_PASSWORD:
        print("BŁĄD: Brak hasła do e-maila (zmienna środowiskowa EMAIL_PASS).")
        return

    # Pobranie konfiguracji .py
    config = load_email_config()
    subject_prefix = config.get("subject_prefix", "Raport aktualizacji inwestycji")
    receivers_list = config.get("receivers", [])

    summary = fetch_summary_data()
    html_content = build_html_email(summary)
    target_date_str = summary["target_date_str"]

    msg = MIMEMultipart("mixed")
    # Łączenie prefiksu z datą
    msg['Subject'] = f"{subject_prefix} za {target_date_str}"
    msg['From'] = SENDER_EMAIL

    # SMTP wymaga podania listy maili w nagłówku "To" po przecinku
    msg['To'] = ", ".join(receivers_list)

    msg.attach(MIMEText(html_content, "html"))

    # Wybór pliku logu: z argumentu CLI lub najnowszy z katalogu cron_logs/
    log_file = Path(log_file_path) if log_file_path else None
    if not log_file or not log_file.is_file():
        logs_dir = Path(__file__).resolve().parents[2] / "cron_logs"
        if logs_dir.is_dir():
            log_files = sorted(logs_dir.glob("cron_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            log_file = log_files[0] if log_files else None

    if log_file and log_file.is_file():
        raw_bytes = log_file.read_bytes()
        max_raw_bytes = 18 * 1024 * 1024  # 18 MB raw -> ~24 MB po kodowaniu Base64 (limit Gmaila to 25 MB)
        if len(raw_bytes) > max_raw_bytes:
            notice = b"[UCIETO POCZATEK LOGU - PRZEKROCZONO LIMIT ROZMIARU ZALACZNIKA]\n"
            raw_bytes = notice + raw_bytes[-max_raw_bytes:]

        attachment = MIMEApplication(raw_bytes, _subtype="octet-stream")
        attachment.add_header("Content-Disposition", "attachment", filename=log_file.name)
        msg.attach(attachment)
        print(f"Dołączono plik logu: {log_file.name}")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            # Metoda sendmail może przyjąć listę w drugim argumencie
            server.sendmail(SENDER_EMAIL, receivers_list, msg.as_string())
        print(f"E-mail z podsumowaniem za {target_date_str} został wysłany do: {', '.join(receivers_list)}")
    except Exception as e:
        print(f"BŁĄD podczas wysyłania e-maila: {e}")


if __name__ == "__main__":
    send_email(sys.argv[1] if len(sys.argv) > 1 else None)
