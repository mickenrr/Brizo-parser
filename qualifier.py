import warnings

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

load_dotenv()

REVENUE_LIMIT    = 1_000_000_000   # 1 млрд руб.
NET_PROFIT_LIMIT = 300_000_000    # 300 млн руб. — порог чистой прибыли для «ярда»
INCOME_TAX_MIN   = 4_000_000      # 4 млн руб. — хотя бы раз за последние 3 года
MODEL          = "claude-haiku-4-5"


def _check_income_tax_rule(amounts: list, rule: dict) -> bool:
    """
    Проверяет выполнение правила налога на прибыль.

    amounts — список сумм налога, упорядоченный от новых к старым (amounts[0] = последний год).
    rule — словарь из settings["income_tax_rule"]:
        mode:       "any"    — хотя бы раз за N лет (дефолт)
                    "last"   — только последний год
                    "all"    — все N лет подряд
                    "k_of_n" — минимум K из N лет
        years:      сколько лет брать (для any/all/k_of_n)
        min_amount: минимальная сумма налога
        k:          минимальное кол-во лет (для k_of_n)
    """
    if not amounts:
        return False
    min_amt = rule.get("min_amount", INCOME_TAX_MIN)
    years   = rule.get("years", 3)
    mode    = rule.get("mode", "any")
    relevant = amounts[:years]

    if mode == "last":
        return relevant[0] >= min_amt
    elif mode == "all":
        return all(a >= min_amt for a in relevant)
    elif mode == "k_of_n":
        k = rule.get("k", 1)
        return sum(1 for a in relevant if a >= min_amt) >= k
    else:  # "any"
        return any(a >= min_amt for a in relevant)


def qualify_company(
    company_data: dict,
    revenue_max: int = REVENUE_LIMIT,
    income_tax_min: int = INCOME_TAX_MIN,   # backward compat (deprecated)
    income_tax_rule: dict | None = None,
    advanced_finance: dict | None = None,
) -> tuple[bool, str]:
    """Check sequential qualification criteria. Returns (passed, reason).

    revenue_max      — компании с выручкой >= этого значения отклоняются
    income_tax_rule  — словарь с правилом проверки налога (из settings.json).
                       Если None, используется income_tax_min в режиме "any / 3 года".
    advanced_finance — расширенные критерии (transport_tax_min и т.п.); 0 = не проверять.
    """

    # 1. Выручка > 1 млрд — отклоняем только если чистая прибыль тоже >= 300 млн.
    # Если прибыль неизвестна (None) — считаем крупной и отклоняем.
    # Если прибыль < 300 млн — компания ещё не «выросла», оставляем.
    revenue    = company_data.get("revenue")
    net_profit = company_data.get("net_profit")
    if revenue is not None and revenue >= revenue_max:
        if net_profit is None or net_profit >= NET_PROFIT_LIMIT:
            return (False, "Интересные, но ярд")

    # 2. Налог на прибыль — по правилу из настроек
    amounts = company_data.get("income_tax_amounts") or []
    if not amounts and company_data.get("income_tax_amount") is not None:
        amounts = [company_data["income_tax_amount"]]

    if income_tax_rule:
        ok = _check_income_tax_rule(amounts, income_tax_rule)
    else:
        # Старая логика: хотя бы раз за 3 года >= income_tax_min
        ok = bool(amounts) and any(a >= income_tax_min for a in amounts[:3])

    if not ok:
        return (False, "Маленькая выручка/мало налогов")

    # 2.5. Общая сумма налогов «Итого» — хотя бы раз за последние 3 года >= 5 млн
    # Используем список за 3 года (не только последний год — он может быть неполным,
    # например текущий год с данными только за 9 месяцев).
    # Если страница /taxes/data недоступна — не отклоняем (нет данных ≠ маленький налог).
    taxes_totals = company_data.get("taxes_total_amounts") or []
    if taxes_totals and not any(t >= INCOME_TAX_MIN for t in taxes_totals):
        return (False, "Маленькая выручка/мало налогов")

    # 3. Расширенные финансовые критерии (если заданы и данные доступны)
    if advanced_finance:
        for field, key in [
            ("transport_tax",  "transport_tax_min"),
            ("property_tax",   "property_tax_min"),
            ("insurance",      "insurance_min"),
            ("vat",            "vat_min"),
        ]:
            threshold = advanced_finance.get(key, 0)
            if threshold > 0:
                val = company_data.get(field)
                if val is not None and val < threshold:
                    return (False, f"Низкий {field}: {val} < {threshold}")

    return (True, "Подходит")


def qualify_leads(leads: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (qualified, rejected) lists with qualification_reason attached."""
    qualified, rejected = [], []
    for lead in leads:
        passed, reason = qualify_company(lead)
        lead["qualification_reason"] = reason
        if passed:
            qualified.append(lead)
        else:
            rejected.append(lead)
    return qualified, rejected
