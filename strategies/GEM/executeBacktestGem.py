from datetime import timedelta
from colorama import Fore, Style, init
from sqlalchemy import text
import re
import strategies.GEM.functionsBacktestGem as fbg
from system_and_notification import db_schema as dbs, db_connection_configuration as dbcc
import time


def main():
    init(autoreset=True)
    start_time = time.perf_counter()
    engine = dbcc.get_engine()

    # 0. Ładowanie konfiguracji
    cfg = fbg.get_settings()
    WARTOSC_POCZATKOWA = cfg.get('WARTOSC_POCZATKOWA', 1000)
    KWOTA_DOPLATY_CO_REBALANCE = cfg.get('KWOTA_DOPLATY_CO_REBALANCE', 100)
    PROWIZJA_I_PODATKI = cfg.get('PROWIZJA_I_PODATKI', 0.001)
    PODATEK_BELKI = cfg.get('PODATEK_BELKI', 0.19)
    CZY_PRZEPROWADZAC_DLA_KAZDEGO_DNIA_MIESIACA = cfg.get('CZY_PRZEPROWADZAC_DLA_KAZDEGO_DNIA_MIESIACA', 1)
    KTOREGO_DNIA_MIESIACA_POCZATEK = cfg.get('KTOREGO_DNIA_MIESIACA_POCZATEK', 1)

    # Nowe parametry
    CO_ILE_REBALANCE = cfg.get('CO_ILE_REBALANCE', '1mo')
    CZY_OD_PIERWSZEJ_MOZLIWEJ_DATY = cfg.get('CZY_OD_PIERWSZEJ_MOZLIWEJ_DATY', 1)
    POCZATEK_BACKTESTU = cfg.get('POCZATEK_BACKTESTU', '2000-01-01')

    match = re.match(r"(\d+)([a-zA-Z]+)", str(CO_ILE_REBALANCE).strip().lower())
    if match:
        interwal_wartosc = int(match.group(1))
        interwal_jednostka = match.group(2)
        if interwal_jednostka not in ['d', 'w', 'mo']:
            print(Fore.RED + f"BŁĄD: Nieznana jednostka czasu '{interwal_jednostka}'. Wymuszam 'mo'." + Style.RESET_ALL)
            interwal_jednostka = 'mo'
    else:
        print(Fore.RED + "BŁĄD: Zły format CO_ILE_REBALANCE w configu. Wymuszam '1mo'." + Style.RESET_ALL)
        interwal_wartosc = 1
        interwal_jednostka = 'mo'

    # 1. Przygotowanie czystych tabel wynikowych
    dbs.init_db_schema(engine)
    dbs.clean_table(engine, "gem_zestawienie_dni_rebalancu")
    dbs.clean_table(engine, "gem_change_logs")
    dbs.clean_table(engine, "gem_daily_portfolio_value")

    # 2. Pobranie absolutnych ram czasowych z bazy danych
    with engine.connect() as conn:
        result = conn.execute(
            text('SELECT MIN("dateOfAnalyze"), MAX("dateOfAnalyze") FROM gem_backtest_price_changes')).fetchone()
        if not result or not result[0]:
            print(
                Fore.RED + "Brak danych w gem_backtest_price_changes. Przelicz najpierw strategię GEM!" + Style.RESET_ALL)
            return

        db_start_date = result[0].date()
        end_date = result[1].date()

    # 3. Logika wyboru daty początkowej (Ochrona przed błędem ludzkim)
    from datetime import datetime

    if CZY_OD_PIERWSZEJ_MOZLIWEJ_DATY == 1:
        start_date = db_start_date
        print(Fore.CYAN + f"Używam pełnej historii dostępnej w bazie." + Style.RESET_ALL)
    else:
        try:
            user_start_date = datetime.strptime(POCZATEK_BACKTESTU, '%Y-%m-%d').date()
            start_date = max(user_start_date, db_start_date)

            if user_start_date < db_start_date:
                print(
                    Fore.YELLOW + f"OSTRZEŻENIE: Żądana data {user_start_date} jest starsza niż dane w bazie. Startuję od {db_start_date}." + Style.RESET_ALL)

        except ValueError:
            print(
                Fore.RED + f"BŁĄD: Zły format daty POCZATEK_BACKTESTU w configu. Używam domyślnej daty z bazy." + Style.RESET_ALL)
            start_date = db_start_date

    # Obliczenie kalendarzowych lat do wskaźnika CAGR
    years_passed = (end_date - start_date).days / 365.25

    if years_passed <= 0:
        print(
            Fore.RED + "KRYTYCZNY BŁĄD: Data początkowa jest równa lub późniejsza niż data końcowa. Przerywam." + Style.RESET_ALL)
        return

    print(
        Fore.YELLOW + f"Zakres backtestu ustalony na: {start_date} -> {end_date} ({years_passed:.2f} lat)" + Style.RESET_ALL)

    days_to_run = range(1, 32) if CZY_PRZEPROWADZAC_DLA_KAZDEGO_DNIA_MIESIACA == 1 else [KTOREGO_DNIA_MIESIACA_POCZATEK]

    PRICE_MAP = fbg.load_all_prices_to_ram(engine, start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))

    for day in days_to_run:
        print(Fore.WHITE + f"\n{'=' * 40}\nURUCHAMIANIE BACKTESTU DLA DNIA: {day}\n{'=' * 40}")
        daily_records = []

        portfolio = {
            'cash': WARTOSC_POCZATKOWA,
            'asset_value': 0.0,
            'total_value': WARTOSC_POCZATKOWA,
            'current_asset_ticker': None,
            'assets': {},
            'strata_podatkowa': 0.0
        }

        current_date = start_date
        pierwsza_transakcja_wykonana = False
        nastepny_planowany_rebalans = None

        while current_date <= end_date:
            current_date_str = current_date.strftime('%Y-%m-%d')
            assets_dict: dict = portfolio['assets']  # Silne typowanie dla lintera IDE

            # =========================================================
            # STADIUM S: Czekamy na pierwszy rynkowy sygnał
            # =========================================================
            if not pierwsza_transakcja_wykonana:
                target_ticker, is_over_sma = fbg.get_ticker_for_date(current_date_str, engine)

                if target_ticker:
                    # LOGIKA SMA: Jeżeli warunek nie jest spełniony, symulujemy ucieczkę/utrzymanie w gotówce
                    if not is_over_sma:
                        target_ticker = 'CASH'

                    if target_ticker.upper() not in ['CASHE', 'CASH']:
                        buy_price = fbg.get_price_for_ticker_on_date(target_ticker, current_date_str, engine)

                        if not buy_price or buy_price <= 0:
                            raise RuntimeError(
                                f"KRYTYCZNY BŁĄD: Brak ceny dla {target_ticker} w dniu {current_date_str} (Stadium S). Przerywam backtest zgodnie z założeniami.")

                        cash_to_invest = portfolio['cash']
                        fee = cash_to_invest * PROWIZJA_I_PODATKI
                        shares_to_buy = (cash_to_invest - fee) / buy_price

                        # Prawdziwa cena nabycia z wliczoną prowizją (do kosztów uzyskania przychodu)
                        true_buy_price = cash_to_invest / shares_to_buy

                        assets_dict[target_ticker] = {
                            'shares': shares_to_buy,
                            'buy_price': true_buy_price,
                            'buy_date': current_date_str
                        }
                        portfolio['cash'] = 0.0
                        portfolio['current_asset_ticker'] = target_ticker
                        pierwsza_transakcja_wykonana = True

                        print(
                            Fore.MAGENTA + f"[{current_date_str}] 🚀 [LOG] SCENARIUSZ S: Start systemu. Pierwszy zakup -> {target_ticker} (Cena: {buy_price:.2f})" + Style.RESET_ALL)

                        nastepny_planowany_rebalans = fbg.kalkulator_kolejnej_daty(current_date, interwal_wartosc,
                                                                                   interwal_jednostka, day, engine)
                    else:
                        portfolio['current_asset_ticker'] = target_ticker
                        pierwsza_transakcja_wykonana = True

                        print(
                            Fore.MAGENTA + f"[{current_date_str}] 🚀 [LOG] SCENARIUSZ S: Start systemu. Ewakuacja do gotówki." + Style.RESET_ALL)
                        nastepny_planowany_rebalans = fbg.kalkulator_kolejnej_daty(current_date, interwal_wartosc,
                                                                                   interwal_jednostka, day, engine)

            # =========================================================
            # STADIUM A & B: Cykliczny rebalans wg harmonogramu
            # =========================================================
            else:
                if current_date >= nastepny_planowany_rebalans:
                    target_ticker, is_over_sma = fbg.get_ticker_for_date(current_date_str, engine)

                    if target_ticker:
                        # LOGIKA SMA: Jeżeli lider spadł pod średnią, wywołujemy twardą zmianę na gotówkę
                        if not is_over_sma:
                            target_ticker = 'CASH'

                        if KWOTA_DOPLATY_CO_REBALANCE > 0:
                            portfolio['cash'] += KWOTA_DOPLATY_CO_REBALANCE

                        # SCENARIUSZ A: Zmiana aktywa
                        if portfolio['current_asset_ticker'] != target_ticker:

                            # Sprzedaż starego (jeśli to nie była gotówka)
                            if portfolio['current_asset_ticker'] and portfolio['current_asset_ticker'].upper() not in ['CASHE', 'CASH']:
                                old_ticker = portfolio['current_asset_ticker']
                                sell_price = fbg.get_price_for_ticker_on_date(old_ticker, current_date_str, engine)

                                if not sell_price or sell_price <= 0:
                                    raise RuntimeError(
                                        f"KRYTYCZNY BŁĄD: Brak ceny sprzedaży dla {old_ticker} w dniu {current_date_str} (Stadium A). Przerywam backtest.")

                                shares = assets_dict[old_ticker]['shares']
                                avg_buy_price = assets_dict[old_ticker]['buy_price']
                                buy_date = assets_dict[old_ticker]['buy_date']

                                # 1. Wycena brutto i potrącenie prowizji maklerskiej
                                gross_value = shares * sell_price
                                fee_sell = gross_value * PROWIZJA_I_PODATKI
                                netto_ze_sprzedazy = gross_value - fee_sell

                                # 2. Obliczenie kosztu nabycia
                                koszt_bazowy = shares * avg_buy_price

                                # 3. Logika podatkowa z uwzględnieniem tarczy
                                zysk_brutto = netto_ze_sprzedazy - koszt_bazowy
                                podatek = 0.0

                                if zysk_brutto > 0:
                                    do_odliczenia = min(zysk_brutto, portfolio['strata_podatkowa'])
                                    podstawa_opodatkowania = zysk_brutto - do_odliczenia
                                    portfolio['strata_podatkowa'] -= do_odliczenia

                                    podatek = podstawa_opodatkowania * PODATEK_BELKI
                                else:
                                    portfolio['strata_podatkowa'] += abs(zysk_brutto)

                                # 4. Zasilenie gotówki
                                portfolio['cash'] += (netto_ze_sprzedazy - podatek)
                                assets_dict.pop(old_ticker)
                                fbg.log_transaction(engine, day, old_ticker, buy_date, current_date_str, avg_buy_price,
                                                    sell_price, shares, portfolio['cash'])

                                portfolio['current_asset_ticker'] = None
                            else:
                                portfolio['current_asset_ticker'] = None

                            # Kupno nowego (jeśli to nie jest gotówka)
                            if target_ticker.upper() not in ['CASHE', 'CASH']:
                                buy_price = fbg.get_price_for_ticker_on_date(target_ticker, current_date_str, engine)

                                if not buy_price or buy_price <= 0:
                                    raise RuntimeError(
                                        f"KRYTYCZNY BŁĄD: Brak ceny zakupu dla {target_ticker} w dniu {current_date_str} (Stadium A). Przerywam backtest.")

                                cash_to_invest = portfolio['cash']
                                fee = cash_to_invest * PROWIZJA_I_PODATKI
                                shares_to_buy = (cash_to_invest - fee) / buy_price

                                # Prawdziwa cena nabycia do bazy kosztowej
                                true_buy_price = cash_to_invest / shares_to_buy

                                assets_dict[target_ticker] = {
                                    'shares': shares_to_buy,
                                    'buy_price': true_buy_price,
                                    'buy_date': current_date_str
                                }
                                portfolio['cash'] = 0.0
                                portfolio['current_asset_ticker'] = target_ticker
                            else:
                                portfolio['current_asset_ticker'] = target_ticker

                        # SCENARIUSZ B: Utrzymanie pozycji i inwestowanie nadwyżki
                        elif portfolio['current_asset_ticker'] == target_ticker and target_ticker.upper() not in ['CASHE', 'CASH']:
                            if portfolio['cash'] > 0:
                                current_price = fbg.get_price_for_ticker_on_date(target_ticker, current_date_str,
                                                                                 engine)

                                if not current_price or current_price <= 0:
                                    raise RuntimeError(
                                        f"KRYTYCZNY BŁĄD: Brak ceny dokupienia dla {target_ticker} w dniu {current_date_str} (Stadium B). Przerywam backtest.")

                                cash_to_invest = portfolio['cash']
                                fee = cash_to_invest * PROWIZJA_I_PODATKI
                                shares_to_buy = (cash_to_invest - fee) / current_price

                                old_shares = assets_dict[target_ticker]['shares']
                                old_avg_price = assets_dict[target_ticker]['buy_price']
                                new_total_shares = old_shares + shares_to_buy

                                # Prawdziwa średnia ważona kosztów (stary wydatek + nowy wydatek)
                                total_historical_cost = old_shares * old_avg_price
                                total_new_cost = cash_to_invest
                                new_avg_price = (total_historical_cost + total_new_cost) / new_total_shares

                                assets_dict[target_ticker]['shares'] = new_total_shares
                                assets_dict[target_ticker]['buy_price'] = new_avg_price
                                portfolio['cash'] = 0.0

                        # Wyliczanie kolejnego kroku na nowo
                        nastepny_planowany_rebalans = fbg.kalkulator_kolejnej_daty(nastepny_planowany_rebalans,
                                                                                   interwal_wartosc, interwal_jednostka,
                                                                                   day, engine)

            # --- KONIEC DNIA ---
            portfolio = fbg.update_gem_daily_portfolio_value(portfolio, current_date_str, day, PRICE_MAP, daily_records)
            current_date += timedelta(days=1)

        # Zamknięcie otwartych pozycji na koniec backtestu
        assets_dict_final: dict = portfolio['assets']
        if portfolio['current_asset_ticker'] and portfolio['current_asset_ticker'].upper() not in ['CASHE', 'CASH']:
            final_ticker = portfolio['current_asset_ticker']
            sell_price = fbg.get_price_for_ticker_on_date(final_ticker, end_date.strftime('%Y-%m-%d'), engine)

            if sell_price:
                shares = assets_dict_final[final_ticker]['shares']
                avg_buy_price = assets_dict_final[final_ticker]['buy_price']
                buy_date = assets_dict_final[final_ticker]['buy_date']

                gross_value = shares * sell_price
                fee_sell = gross_value * PROWIZJA_I_PODATKI
                netto_ze_sprzedazy = gross_value - fee_sell
                koszt_bazowy = shares * avg_buy_price

                zysk_brutto = netto_ze_sprzedazy - koszt_bazowy
                podatek = 0.0

                if zysk_brutto > 0:
                    do_odliczenia = min(zysk_brutto, portfolio['strata_podatkowa'])
                    podstawa_opodatkowania = zysk_brutto - do_odliczenia
                    portfolio['strata_podatkowa'] -= do_odliczenia
                    podatek = podstawa_opodatkowania * PODATEK_BELKI
                else:
                    portfolio['strata_podatkowa'] += abs(zysk_brutto)

                portfolio['cash'] += (netto_ze_sprzedazy - podatek)
                portfolio['total_value'] = portfolio['cash']

                fbg.log_transaction(engine, day, final_ticker, buy_date, end_date.strftime('%Y-%m-%d'), avg_buy_price,
                                    sell_price, shares, portfolio['total_value'])

        fbg.bulk_save_daily_logs(engine, daily_records)
        fbg.calculate_and_save_statistics(engine, day, WARTOSC_POCZATKOWA, portfolio['total_value'], years_passed)
        dbs.log_universal(engine, "Wykonano backtest", "Success", "", start_time)


if __name__ == "__main__":
    main()
