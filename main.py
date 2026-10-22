"""main.py — Lead qualification pipeline for Brizo CRM."""

import argparse
import json as _json
import logging
import os
import random
import sys
import threading
import time
from collections import defaultdict

import requests


def _emit(event_type: str, **kwargs) -> None:
    """Print a structured pipeline event line for the web UI."""
    print(f"PIPELINE_EVENT:{_json.dumps({'type': event_type, **kwargs})}", flush=True)

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from brizo import (
    login,
    get_new_leads,
    get_lead_details,
    assign_to_me,
    move_to_stage,
    reject_lead,
    fill_checko_field,
    fill_contact_details,
    add_comment,
    add_contact,
    add_lpr_contacts,
    check_duplicate_by_inn,
    _verify_lead_untouched,
)
import checko as checko_module
from checko import get_company_data, get_lpr_contacts, get_timezone_comment
from qualifier import qualify_company
from skolkovo import (
    check_skolkovo,
    set_pw_page as set_skolkovo_pw_page,
    AllModelsExhausted,
    FREE_MODELS,
    save_preferred_model,
)

load_dotenv()


class CaptchaDetected(Exception):
    """Raised when Checko returns 429 / blocks the request (captcha required)."""


class _StopPipeline(BaseException):
    """Raised to abort the entire pipeline run (e.g. user declined to continue without Skolkovo)."""


# Set to True once the user confirms continuing without Skolkovo analysis.
_skolkovo_disabled: bool = False


def _read_tty(prompt_text: str) -> str:
    """Read one line from the terminal, bypassing stdin (safe even with watcher thread on stdin)."""
    try:
        with open("/dev/tty", "r") as tty:
            sys.stdout.write(prompt_text)
            sys.stdout.flush()
            return tty.readline().strip().lower()
    except (OSError, IOError):
        return input(prompt_text).strip().lower()


_YES = {"да", "д", "yes", "y"}


def _prompt_all_models_failed() -> None:
    """Show interactive confirmation when all OpenRouter models are unavailable.
    Sets _skolkovo_disabled=True if user confirms, or raises _StopPipeline."""
    global _skolkovo_disabled
    border = "═" * 54
    print(f"""
{border}
⚠️  ВНИМАНИЕ: модуль анализа деятельности недоступен
{border}
Все модели OpenRouter вернули ошибку. Парсер не может
анализировать, подходит компания или нет.

Можно продолжить в режиме БЕЗ анализа:
  ✓ назначить ответственного
  ✓ проверить финансы и квалифицировать по налогам
  ✓ добавить контакты ЛПР
  ✗ комментарий о деятельности — не будет
""")
    if _read_tty("Продолжить без анализа деятельности? (да/нет): ") not in _YES:
        raise _StopPipeline("Прогон отменён. Замените модель в конфиге и запустите снова.")
    if _read_tty("Вы уверены? Лиды останутся без оценки по типу бизнеса. (да/нет): ") not in _YES:
        raise _StopPipeline("Прогон отменён. Замените модель в конфиге и запустите снова.")
    _skolkovo_disabled = True
    log.warning("Сколково-модуль отключён по решению пользователя — продолжаем без анализа")


# Shared pause flag: set = running, clear = paused (captcha).
# app.py signals resume by writing "RESUME\n" to main.py's stdin.
pause_event = threading.Event()


def _stdin_resume_watcher() -> None:
    """Daemon thread: watches stdin for RESUME command from app.py."""
    try:
        for line in sys.stdin:
            if line.strip() == "RESUME":
                pause_event.set()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Stage / reason names
# ─────────────────────────────────────────────────────────────────────────────

STAGE_LOST    = "Проиграно"
STAGE_PARSING = "Парсю"
REASON_NO_WEBSITE  = "Нет КД"
REASON_TOO_BIG     = "Интересные, но ярд"
REASON_LOW_TAX     = "Маленькая выручка/мало налогов"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_FREE_EMAIL_DOMAINS: frozenset[str] = frozenset({
    "mail.ru", "yandex.ru", "ya.ru", "gmail.com", "inbox.ru",
    "list.ru", "bk.ru", "rambler.ru", "outlook.com", "hotmail.com",
    "icloud.com", "yahoo.com", "protonmail.com", "mailbox.org",
    "internet.ru", "ro.ru", "mail.com", "ukr.net",
})


def is_corporate_email(email: str) -> bool:
    """Return True if the email domain is not a free/public mail provider."""
    try:
        domain = email.strip().lower().split("@")[1]
    except IndexError:
        return False
    return domain not in _FREE_EMAIL_DOMAINS


def _check_domain_site(domain: str) -> str | None:
    """
    Try https://{domain} then http://{domain}.
    Return the URL if status 200 and content > 500 chars, else None.
    """
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}"
        try:
            resp = requests.get(url, timeout=5, allow_redirects=True,
                                headers=_SITE_HEADERS, verify=False)
            if resp.status_code == 200 and len(resp.text) > 500:
                return url
        except requests.exceptions.SSLError:
            continue
        except Exception:
            break
    return None


_PARKING_KEYWORDS = [
    # Russian
    "домен продаётся", "домен продается", "купить домен", "припаркован",
    "домен свободен", "домен истёк", "домен истек", "срок регистрации домена",
    "домен на продажу", "приобрести домен",
    # English
    "domain for sale", "buy this domain", "domain is for sale",
    "parked domain", "domain parking", "this domain has expired",
    "hugedomains", "sedo.com", "dan.com", "undeveloped.com",
    "godaddy.com/domains", "namecheap.com",
]

_SITE_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def check_website(url: str) -> bool:
    """Return True if the website appears alive, False if down/parked/expired."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    def _try(u: str, timeout: int = 10) -> bool | None:
        try:
            resp = requests.get(u, timeout=timeout, allow_redirects=True,
                                headers=_SITE_HEADERS, verify=False)
            if resp.status_code >= 500:
                return False
            content = resp.text.lower()
            return not any(kw in content for kw in _PARKING_KEYWORDS)
        except requests.exceptions.SSLError:
            return None  # retry over http
        except Exception:
            return False

    result = _try(url)
    if result is None:
        # SSL failed — try plain http
        result = _try(url.replace("https://", "http://", 1), timeout=8)
    return bool(result)


def _pause(lo: float = 1.0, hi: float = 2.0) -> None:
    delay = random.uniform(lo, hi)
    log.debug("Pause %.1f s", delay)
    time.sleep(delay)


def _fmt(value) -> str:
    """Format a number with spaces as thousands separator."""
    if value is None:
        return "нет данных"
    return f"{int(value):,}".replace(",", " ")


# ─────────────────────────────────────────────────────────────────────────────
# Per-lead processor
# ─────────────────────────────────────────────────────────────────────────────

def process_lead(page, lead: dict, stats: dict) -> tuple[str, str]:
    """
    Логика квалификации:
      1. Нет сайта → Проиграно 'Нет КД'
      2. Нет в Чекко → добавляем комментарий, пропускаем (оставляем в База)
      3. Выручка > 1 млрд → Проиграно 'Интересные, но ярд'
      4. Налог на прибыль < 5 млн → Проиграно 'Маленькая выручка/мало налогов'
      5. Прошли → прикрепляем Чекко URL, добавляем ЛПРов, остаются в «База»
    """
    lead_id = str(lead.get("id", ""))
    name    = lead.get("name", f"Lead {lead_id}")

    log.info("─" * 64)
    log.info("Лид #%s  «%s»", lead_id, name)
    stats["total"] += 1

    # ── 0. API-проверка перед любыми действиями ───────────────────────────
    # Убеждаемся, что сделка всё ещё в Базе, ответственный не изменился,
    # нет комментариев и нет лишних участников — иначе пропускаем без следа.
    ok, reason_skip = _verify_lead_untouched(lead_id)
    if not ok:
        log.info("  Пропускаем #%s — %s", lead_id, reason_skip)
        stats["errors"] += 1
        return ("skipped", reason_skip)

    # ── a. Назначить себя ответственной ──────────────────────────────────
    _pause()
    assign_to_me(page, lead_id)

    # ── b. Получить детали сделки ─────────────────────────────────────────
    _pause()
    details = get_lead_details(page, lead_id)
    inn = (details.get("inn") or lead.get("inn", "")).strip()

    if not inn:
        log.warning("  ИНН не найден — пропускаем")
        add_comment(page, lead_id, "⚠️ ИНН компании не найден в карточке сделки.")
        stats["errors"] += 1
        return ("error", "Нет ИНН")

    log.info("  ИНН компании: %s", inn)

    # ── c. Проверить поле «Сайт» и «Корпоративная почта» ────────────────
    # Если в Brizo есть хотя бы одно из двух — продолжаем как раньше.
    # Если оба пустые — идём на Чекко искать сайт/почту:
    #   - нашли → записываем в Brizo, продолжаем обработку
    #   - не нашли → отклоняем «Нет КД»
    deal_website = details.get("website_field", "").strip()
    deal_email   = details.get("email_field", "").strip()
    existing_checko = details.get("checko_url_field", "").strip()

    _company_prefetch = None   # будет заполнен ниже, чтобы шаг d не дублировал запрос

    if not deal_website and not deal_email:
        log.info("  Нет сайта/почты в Brizo — ищем на Чекко...")
        if existing_checko:
            _company_prefetch = checko_module.get_company_data_from_url(existing_checko, inn)
        else:
            _company_prefetch = get_company_data(inn)

        if not _company_prefetch:
            # Если провал связан с капчей — уходим на паузу, не закрываем лид
            if checko_module.was_request_blocked():
                raise CaptchaDetected(inn)
            log.warning("  Чекко не ответил при поиске сайта — оставляем в База")
            stats["errors"] += 1
            return ("error", "Не найден в Чекко")

        checko_website = (_company_prefetch.get("website") or "").strip()
        checko_emails  = _company_prefetch.get("emails") or []
        # Сохраняем ВСЕ почты через запятую — чтобы шаг c.5 проверил каждую
        checko_email   = ", ".join(e.strip() for e in checko_emails if e.strip())

        if checko_website or checko_email:
            log.info("  Нашли на Чекко: сайт=%s  почта=%s", checko_website, checko_email)
            fill_contact_details(lead_id, checko_website, checko_email)
            deal_website = checko_website
            deal_email   = checko_email
        else:
            log.info("  На Чекко тоже нет сайта/почты → Проиграно 'Нет КД'")
            _pause()
            reject_lead(page, lead_id, REASON_NO_WEBSITE)
            stats["rejected"][REASON_NO_WEBSITE] += 1
            return ("rejected", REASON_NO_WEBSITE)

    # ── c.5. Если сайта нет, но есть почта — проверяем домены всех почт ────
    # Перебираем ВСЕ почты (поле может содержать несколько через запятую).
    # Если хотя бы одна почта корпоративная (домен не в списке бесплатных) —
    # лид продолжает обработку (сайт не проверяем — почта сама по себе КД).
    # Если все почты бесплатные — Нет КД.
    if not deal_website and deal_email:
        all_emails = [e.strip() for e in deal_email.replace(";", ",").split(",")
                      if e.strip() and "@" in e]
        corporate_emails = [e for e in all_emails if is_corporate_email(e)]

        if not corporate_emails:
            log.info("  Все почты бесплатные (%s) → Проиграно 'Нет КД'", deal_email)
            _pause()
            reject_lead(page, lead_id, REASON_NO_WEBSITE)
            stats["rejected"][REASON_NO_WEBSITE] += 1
            return ("rejected", REASON_NO_WEBSITE)

        log.info("  Корпоративная почта найдена: %s — продолжаем обработку",
                 corporate_emails[0])

    # ── d. Получить данные из Чекко ──────────────────────────────────────
    log.info("  [d] Запрашиваем Чекко для ИНН %s", inn)
    if _company_prefetch:
        company = _company_prefetch   # уже загружено в шаге c — не дублируем запрос
    elif existing_checko:
        company = checko_module.get_company_data_from_url(existing_checko, inn)
    else:
        company = get_company_data(inn)

    if not company:
        if checko_module.was_request_blocked():
            raise CaptchaDetected(inn)
        log.warning("  Компания не найдена в Чекко — оставляем в База")
        add_comment(page, lead_id, f"⚠️ Не найдено в Чекко для ИНН {inn}.")
        stats["errors"] += 1
        return ("error", "Не найден в Чекко")

    checko_url = company.get("checko_url", "")
    revenue    = company.get("revenue")
    income_tax = company.get("income_tax_amounts") or []

    # Капча на /taxes/data не даёт исключения, а возвращает данные без налога —
    # без этой проверки лид уйдёт в квалификацию и будет закрыт как «мало налогов».
    if checko_module.was_request_blocked():
        raise CaptchaDetected(inn)

    log.info("  Чекко: выручка=%s  налог на прибыль=%s",
             _fmt(revenue), [_fmt(t) for t in income_tax[:3]])

    # ── e. Прикрепить ссылку Чекко ───────────────────────────────────────
    if checko_url and not existing_checko:
        log.info("  Заполняем поле Чекко: %s", checko_url)
        _pause()
        fill_checko_field(page, lead_id, checko_url)

    # ── f. Финансовая квалификация ────────────────────────────────────────
    passed, reason = qualify_company(company)
    log.info("  Квалификация: %s (причина: %s)", "✓ Подходит" if passed else "✗ Не подходит", reason)

    if not passed:
        _pause()
        reject_lead(page, lead_id, reason)
        stats["rejected"][reason] += 1
        return ("rejected", reason)

    stats["qualified"] += 1

    # ── f.2. Проверка Сколково/IT-льготы ─────────────────────────────────
    if _skolkovo_disabled:
        _pause(0.3, 0.7)
        add_comment(page, lead_id, "⚠️ Анализ деятельности недоступен — проверить вручную")
    else:
        # Если сайт не указан, но есть корпоративная почта — берём домен из неё
        skolkovo_website = deal_website or None
        if not skolkovo_website and deal_email:
            for _em in [e.strip() for e in deal_email.replace(";", ",").split(",") if e.strip() and "@" in e]:
                if is_corporate_email(_em):
                    skolkovo_website = "https://" + _em.split("@")[1].strip()
                    log.info("  Сколково: сайт не указан — используем домен из почты %s → %s", _em, skolkovo_website)
                    break

        try:
            skolkovo_eligible, skolkovo_reason = check_skolkovo(
                name, company, website=skolkovo_website, email=deal_email or None, inn=inn or None
            )
            skolkovo_emoji = "✅" if skolkovo_eligible else "❌"
            log.info("  %s Сколково: %s", skolkovo_emoji, skolkovo_reason)
            _pause(0.3, 0.7)
            add_comment(page, lead_id, f"{skolkovo_emoji} {skolkovo_reason}")
        except AllModelsExhausted:
            _prompt_all_models_failed()   # sets _skolkovo_disabled or raises _StopPipeline
            _pause(0.3, 0.7)
            add_comment(page, lead_id, "⚠️ Анализ деятельности недоступен — проверить вручную")

    # ── f.5. Проверка на дубли по ИНН ────────────────────────────────────
    if check_duplicate_by_inn(inn, lead_id):
        log.info("  Найден дубль по ИНН %s", inn)
        add_comment(page, lead_id, "Есть дубль")

    # ── g. Часовой пояс — комментарий к сделке (только для прошедших) ────
    legal_address = company.get("legal_address", "")
    tz_comment = get_timezone_comment(legal_address)
    log.info("  Адрес: %s → %s", legal_address[:80] if legal_address else "не найден", tz_comment)
    _pause(0.3, 0.7)
    add_comment(page, lead_id, tz_comment)

    # ── h. Перемещаем в «Парсю» ── ОТКЛЮЧЕНО: сделка остаётся в «База» ───
    # move_to_stage(page, lead_id, STAGE_PARSING)

    # ── i. Добавляем ЛПРов из Чекко в контакты сделки ───────────────────
    lpr_contacts = get_lpr_contacts(company)
    log.info("  ЛПРы из Чекко: %s",
             [(c["name"], c["role"]) for c in lpr_contacts] or "не найдены")
    if lpr_contacts:
        _pause(0.5, 1.0)
        add_lpr_contacts(page, lead_id, lpr_contacts)

    return ("qualified", "")


# ─────────────────────────────────────────────────────────────────────────────
# Statistics output
# ─────────────────────────────────────────────────────────────────────────────

def print_stats(stats: dict) -> None:
    total     = stats["total"]
    qualified = stats.get("qualified", 0)
    errors    = stats["errors"]
    rejected  = stats["rejected"]

    no_kd       = rejected.get(REASON_NO_WEBSITE, 0)
    high_rev    = rejected.get(REASON_TOO_BIG, 0)
    low_tax     = rejected.get(REASON_LOW_TAX, 0)
    other_rej   = sum(v for k, v in rejected.items()
                      if k not in (REASON_NO_WEBSITE, REASON_TOO_BIG, REASON_LOW_TAX))

    lines = [
        "",
        "═" * 55,
        "  СТАТИСТИКА",
        "═" * 55,
        f"  Обработано всего              : {total}",
        f"  → обработано (остались в База) : {qualified}",
        f"  ❌ Нет КД → Проиграно          : {no_kd}",
        f"  ❌ Интересные, но ярд → Проигр.: {high_rev}",
        f"  ❌ Мало налогов → Проиграно    : {low_tax}",
    ]
    if other_rej:
        lines.append(f"  ❌ Иные отказы                 : {other_rej}")
    if errors:
        lines.append(f"  ⚠️  Ошибок (пропущено)          : {errors}")
    lines.append("═" * 55)

    output = "\n".join(lines)
    print(output)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _set_model_interactive() -> None:
    """CLI wizard to pick and save a preferred OpenRouter model."""
    print("\nДоступные модели для анализа Сколково:")
    for i, m in enumerate(FREE_MODELS, 1):
        tag = "" if m.endswith(":free") else "  (платная)"
        print(f"  {i}. {m}{tag}")
    while True:
        choice = input("\nВыберите номер модели: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(FREE_MODELS):
            selected = FREE_MODELS[int(choice) - 1]
            save_preferred_model(selected)
            print(f"✓ Модель сохранена: {selected}")
            return
        print("Неверный номер, попробуйте ещё раз.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Brizo lead qualification pipeline"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N leads (useful for testing)",
    )
    parser.add_argument(
        "--set-model",
        action="store_true",
        help="Выбрать модель OpenRouter для анализа Сколково и сохранить в конфиг",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.set_model:
        _set_model_interactive()
        return

    limit = args.limit

    log.info("═" * 64)
    log.info("  СТАРТ ПАЙПЛАЙНА%s", f"  (лимит: {limit})" if limit else "")
    log.info("═" * 64)

    stats: dict = {
        "total": 0,
        "qualified": 0,
        "errors": 0,
        "rejected": defaultdict(int),
    }

    pause_event.set()  # start unpaused
    threading.Thread(target=_stdin_resume_watcher, daemon=True).start()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                # Required in Docker: /dev/shm is 64MB by default, Chrome crashes without this
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--metrics-recording-only",
                "--mute-audio",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=True,
        )
        # Hide webdriver fingerprint
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        # Separate page for fetching company websites in skolkovo.py (JS/Cloudflare sites)
        site_fetch_page = context.new_page()
        set_skolkovo_pw_page(site_fetch_page)
        # Block analytics/tracking scripts that cause load-event hangs in headless mode
        def _block_analytics(route):
            if any(d in route.request.url for d in ["yandex.ru", "mail.ru", "tg-desk.com"]):
                route.abort()
            else:
                route.continue_()
        page.route("**", _block_analytics)

        # Checko gets its own browser context, optionally with a proxy.
        # On cloud servers (Railway) Checko blocks the server IP — proxy bypasses the block.
        def _block_checko_resources(route):
            resource_type = route.request.resource_type
            if resource_type in ("image", "stylesheet", "font", "media"):
                route.abort()
            elif any(d in route.request.url for d in ["yandex.ru", "mail.ru", "tg-desk.com",
                                                        "mc.yandex", "top-fwz1", "counter"]):
                route.abort()
            else:
                route.continue_()

        _checko_ctx_kwargs: dict = {
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1280, "height": 720},
            "ignore_https_errors": True,
        }
        _pw_proxy = checko_module.get_pw_proxy()
        _force_proxy = bool(_pw_proxy) and os.getenv("CHECKO_FORCE_PROXY", "").strip() in ("1", "true", "yes")

        def _make_checko_page(with_proxy: bool) -> object:
            kw = dict(_checko_ctx_kwargs)
            if with_proxy and _pw_proxy:
                kw["proxy"] = _pw_proxy
            elif "proxy" in kw:
                del kw["proxy"]
            ctx = browser.new_context(**kw)
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            pg = ctx.new_page()
            pg.route("**", _block_checko_resources)
            return pg

        # С CHECKO_FORCE_PROXY=1 (Railway) — стартуем сразу с прокси
        # Без него (локально) — прямое соединение, прокси подключается при 429
        if _force_proxy:
            log.info("[0] Checko-браузер: FORCE PROXY режим (Railway)")
        else:
            log.info("[0] Checko-браузер: прямое соединение (прокси подключится при 429)")
        checko_page = _make_checko_page(with_proxy=_force_proxy)
        checko_module.set_pw_page(checko_page)

        # Открываем Checko в браузере ОДИН РАЗ при старте.
        try:
            log.info("[0] Открываем Checko в браузере...")
            checko_page.goto("https://checko.ru/", timeout=30_000, wait_until="domcontentloaded")
            time.sleep(2)
            log.info("[0] Checko открыт")
        except Exception as _ce:
            _ce_str = str(_ce)
            if _pw_proxy and "PROXY" in _ce_str.upper():
                log.warning("[0] Прокси недоступен (%s) — переключаемся на прямое соединение", _ce_str[:80])
                checko_page = _make_checko_page(with_proxy=False)
                checko_module.set_pw_page(checko_page)
                try:
                    checko_page.goto("https://checko.ru/", timeout=30_000, wait_until="domcontentloaded")
                    time.sleep(2)
                    log.info("[0] Checko открыт (без прокси)")
                except Exception as _ce2:
                    log.warning("[0] Не удалось открыть Checko: %s — продолжаем", _ce2)
            else:
                log.warning("[0] Не удалось открыть Checko: %s — продолжаем", _ce)

        try:
            # ── Шаг 3: войти в Brizo ─────────────────────────────────────
            log.info("[1] Авторизация в Brizo")
            login(page)

            # ── Шаг 4: получить список новых лидов ───────────────────────
            log.info("[2] Получаем новые лиды из колонки «База»")
            leads = get_new_leads(page, max_leads=limit if limit is not None else 9999)

            if not leads:
                log.info("Новых лидов не найдено. Завершаем.")
                return

            if limit is not None and len(leads) > limit:
                leads = leads[:limit]

            total_leads = len(leads)
            log.info("Найдено %d лидов", total_leads)
            _emit("total", total=total_leads)

            # ── Шаг 5: обрабатываем каждый лид ───────────────────────────
            log.info("[3] Начинаем обработку")
            i = 0
            while i < len(leads):
                lead = leads[i]
                lead_num = i + 1
                lead_name = lead.get("name", f"Lead {lead.get('id')}")
                _emit("lead_start", num=lead_num, total=total_leads, name=lead_name)
                status, reason = "error", "Ошибка"
                captcha_hit = False
                try:
                    status, reason = process_lead(page, lead, stats)
                    i += 1
                except CaptchaDetected:
                    captcha_hit = True
                    log.warning("  КАПЧА/БЛОКИРОВКА Checko — ставим парсер на паузу")
                    _emit("captcha_detected", lead_name=lead_name)
                    time.sleep(1.5)  # дать SSE-стриму время доставить событие до браузера
                    pause_event.clear()   # блокируем цикл до команды RESUME от app.py
                    pause_event.wait()    # ← ждём нажатия кнопки «Продолжить» в браузере
                    log.info("  Возобновили работу — ждём 30 сек перед повтором лида")
                    time.sleep(30)
                    # Не увеличиваем i — повторяем тот же лид
                except _StopPipeline as exc:
                    log.warning("Прогон остановлен: %s", exc)
                    print(f"\n{exc}")
                    return
                except PlaywrightTimeout as exc:
                    log.warning(
                        "Playwright timeout для лида %s (retry исчерпан): %s",
                        lead.get("id"), exc,
                    )
                    stats["errors"] += 1
                    status, reason = "error", "Timeout"
                    i += 1
                except Exception as exc:
                    log.error(
                        "Неожиданная ошибка для лида %s: %s",
                        lead.get("id"), exc,
                        exc_info=True,
                    )
                    stats["errors"] += 1
                    status, reason = "error", "Ошибка"
                    i += 1
                finally:
                    if not captcha_hit:
                        _emit("lead_done", num=lead_num, status=status, name=lead_name, reason=reason,
                              qualified=stats["qualified"],
                              rejected=sum(stats["rejected"].values()),
                              errors=stats["errors"])
                    _pause(1.5, 2.5)

        except Exception as exc:
            log.critical("Критическая ошибка пайплайна: %s", exc, exc_info=True)
        finally:
            browser.close()

    # ── Шаг 6: вывести статистику ─────────────────────────────────────────
    print_stats(stats)


if __name__ == "__main__":
    main()
