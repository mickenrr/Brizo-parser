"""skolkovo.py — проверка компании на признаки Сколково/IT-льгот через OpenRouter."""

import json
import logging
import os
import re

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ─── Provider infrastructure ──────────────────────────────────────────────────

_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skolkovo_model.txt")

_OR_URL   = "https://openrouter.ai/api/v1/chat/completions"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GEM_URL  = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"


class ProviderUnavailable(Exception):
    """Signal from a provider call: skip to the next one."""


class AllModelsExhausted(Exception):
    """Raised when every provider is unavailable."""


def _openai_compat(url: str, api_key: str, model: str,
                   system: str, user: str,
                   extra_headers: dict | None = None) -> str:
    """Generic OpenAI-compatible chat-completions call. Returns response text."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    resp = requests.post(
        url,
        headers=headers,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "max_tokens": 500,
        },
        timeout=30,
    )
    if resp.status_code in (404, 429, 503):
        raise ProviderUnavailable(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    # reasoning-модели кладут ответ в поле reasoning вместо content
    return msg.get("content") or msg.get("reasoning") or ""


def _call_openrouter(model: str, system: str, user: str) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise ProviderUnavailable("OPENROUTER_API_KEY не задан")
    return _openai_compat(
        _OR_URL, api_key, model, system, user,
        extra_headers={
            "HTTP-Referer": "https://github.com/mickenrr/Brizo-parser",
            "X-Title": "BrizoParser",
        },
    )


def _call_groq(system: str, user: str) -> str:
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise ProviderUnavailable("GROQ_API_KEY не задан")
    return _openai_compat(_GROQ_URL, api_key, "llama-3.1-8b-instant", system, user)


def _call_gemini(system: str, user: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ProviderUnavailable("GEMINI_API_KEY не задан")
    resp = requests.post(
        f"{_GEM_URL}?key={api_key}",
        headers={"Content-Type": "application/json"},
        json={
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
        },
        timeout=30,
    )
    if resp.status_code in (404, 429, 503):
        raise ProviderUnavailable(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


# Each entry: (display_name, api_key_env_var, call_fn(system, user) -> str)
PROVIDERS: list[tuple[str, str, object]] = [
    ("OpenRouter / gemini-2.0-flash-exp:free",   "OPENROUTER_API_KEY",
     lambda s, u: _call_openrouter("google/gemini-2.0-flash-exp:free", s, u)),
    ("OpenRouter / llama-3.3-70b-instruct:free", "OPENROUTER_API_KEY",
     lambda s, u: _call_openrouter("meta-llama/llama-3.3-70b-instruct:free", s, u)),
    ("Groq / llama-3.1-8b-instant",              "GROQ_API_KEY",
     lambda s, u: _call_groq(s, u)),
    ("Google Gemini / gemini-1.5-flash",         "GEMINI_API_KEY",
     lambda s, u: _call_gemini(s, u)),
    ("OpenRouter / gpt-4o-mini [платный]",       "OPENROUTER_API_KEY",
     lambda s, u: _call_openrouter("openai/gpt-4o-mini", s, u)),
]

# Keep for backward compatibility with main.py
FREE_MODELS = [name for name, _, __ in PROVIDERS]


def get_current_preferred() -> str | None:
    try:
        if os.path.exists(_CONFIG_FILE):
            return open(_CONFIG_FILE, encoding="utf-8").read().strip() or None
    except Exception:
        pass
    return None


def save_preferred_provider(name: str) -> None:
    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(name)
    except Exception as exc:
        log.warning("Не удалось сохранить выбор провайдера: %s", exc)


# kept for old callers
save_preferred_model = save_preferred_provider


def get_providers_order() -> list[tuple[str, str, object]]:
    """Return PROVIDERS with the saved preferred provider moved to the front."""
    preferred = get_current_preferred()
    if preferred:
        found = next((p for p in PROVIDERS if p[0] == preferred), None)
        if found:
            return [found] + [p for p in PROVIDERS if p[0] != preferred]
    return list(PROVIDERS)


def call_ai(system: str, user: str) -> str:
    """Try providers in order. Returns raw response text or raises AllModelsExhausted."""
    providers = get_providers_order()
    for idx, (name, _key_env, fn) in enumerate(providers):
        try:
            text = fn(system, user)
            log.info("  Сколково: провайдер [%s] — OK", name)
            return text
        except ProviderUnavailable as exc:
            next_name = providers[idx + 1][0] if idx + 1 < len(providers) else None
            if next_name:
                log.warning("⚠️ Провайдер %s недоступен (%s), переключаемся на %s",
                            name, exc, next_name)
            else:
                log.warning("⚠️ Провайдер %s недоступен (%s), резервных нет", name, exc)
    raise AllModelsExhausted("Все провайдеры недоступны")

# Playwright page injected from main.py (avoids nesting two sync_playwright contexts).
# If not injected, _fetch_site_text falls back to requests.get.
_site_pw_page = None


def set_pw_page(page) -> None:
    global _site_pw_page
    _site_pw_page = page


def _get_pw_page():
    return _site_pw_page

_SYSTEM_PROMPT = """Ты — эксперт по инновационным компаниям. Анализируешь, подходит ли российская компания для резидентства в Сколково.

Критерии Сколково (достаточно ОДНОГО):
1. Патенты на изобретения/полезные модели/промышленные образцы
2. Гранты от РФФИ, Фонда Бортника, РНФ, Минпромторга
3. Собственное ПО в реестре Минцифры
4. Уникальная технология/рецептура с конструкторской документацией и НИОКР
5. Собственное серийное оборудование с конструкторской документацией

НЕ подходят: торговля, логистика, строительство, сервис без собственных разработок, контрактное производство по чужим чертежам.

Отвечай ТОЛЬКО валидным JSON: {{"eligible": true/false, "reason": "..."}}"""

_USER_PROMPT_TEMPLATE = """Компания: {name}
Сайт: {website}

Текст с сайта:
{site_text}

Патенты и гранты (datanewton.ru):
{datanewton_text}

Задача: напиши reason в ДВУХ частях через точку:
Часть 1 — конкретно чем занимается компания (что производит/продаёт/разрабатывает). Если сайт загружен — используй конкретные продукты/услуги с сайта, не пиши абстрактно.
Часть 2 — почему подходит или не подходит под критерии Сколково. Если есть патенты или гранты из datanewton — обязательно упомяни их.

Примеры хорошего reason:
✅ "Разрабатывает промышленные роботы-манипуляторы серии РМ-500 для автоматизации сварки. Подходит — 3 патента на конструкцию манипулятора подтверждены на datanewton."
✅ "Производит и серийно продаёт собственные чиллеры и холодильные централи. Подходит — выпускает собственный серийный продукт с конструкторской документацией."
❌ "Оптово продаёт крепёжные изделия для отечественных автомобилей. Не подходит — торговая компания, собственных разработок нет."
❌ "Оказывает услуги металлообработки по чертежам заказчиков. Не подходит — контрактное производство без собственных технологий и патентов."
❌ "Сайт недоступен, по названию «РЕНН-К» деятельность не установлена. Не подходит — недостаточно данных для положительного решения."
"""


def _fetch_datanewton(inn: str) -> str:
    """Запросить datanewton.ru и вернуть сводку по упоминаниям патентов и грантов."""
    if not inn:
        return ""
    try:
        r = requests.get(
            f"https://datanewton.ru/company/{inn}",
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True).lower()

        patent_keywords = ["патент", "изобретение", "полезная модель", "свидетельство"]
        grant_keywords  = ["грант", "рффи", "бортника", "сколково", "минпромторг", "рнф"]

        patent_count = sum(text.count(kw) for kw in patent_keywords)
        grant_matches = [kw for kw in grant_keywords if kw in text]

        patent_part = f"Патенты: найдено упоминаний ({patent_count})." if patent_count else "Патенты: не найдено."

        if grant_matches:
            display_map = {
                "грант": "грант", "рффи": "РФФИ", "бортника": "Фонд Бортника",
                "сколково": "Сколково", "минпромторг": "Минпромторг", "рнф": "РНФ",
            }
            labels = [display_map.get(m, m) for m in grant_matches]
            grant_part = f"Гранты: найдено ({len(grant_matches)} — {', '.join(labels)})."
        else:
            grant_part = "Гранты: не найдено."

        return f"{patent_part} {grant_part}"
    except Exception as exc:
        log.debug("  Сколково: не удалось загрузить datanewton.ru/%s — %s", inn, exc)
        return ""


def _fetch_site_text(url: str) -> str:
    """Загрузить сайт через Playwright (JS-сайты, Cloudflare) и вернуть первые 600 слов.
    Если Playwright-страница не инжектирована — fallback на requests.get.
    При любой ошибке — пустая строка."""
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    pw_page = _get_pw_page()
    if pw_page is not None:
        try:
            pw_page.goto(url, timeout=15_000)
            pw_page.wait_for_load_state("domcontentloaded")
            text = pw_page.inner_text("body")
            words = text.split()
            result = " ".join(words[:600])
            log.info("  Сколково: загружено %d слов с сайта %s", len(words[:600]), url)
            return result
        except Exception as exc:
            log.info("  Сколково: сайт недоступен через Playwright (%s): %s", url, type(exc).__name__)
    try:
        r = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"}, verify=False)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))
        words = text.split()
        result = " ".join(words[:600])
        log.info("  Сколково: загружено %d слов с сайта %s", len(words[:600]), url)
        return result
    except Exception as exc:
        log.info("  Сколково: сайт недоступен совсем (%s)", url)
        return ""


def _extract_email_domain(email: str | None) -> str:
    """Извлечь домен из первого email-адреса."""
    if not email:
        return "не указан"
    first = email.replace(";", ",").split(",")[0].strip()
    if "@" in first:
        return first.split("@")[1].strip()
    return "не указан"


def _parse_json_response(raw: str | None) -> dict:
    """Извлечь JSON из ответа модели, устойчиво к обёрткам и мусору."""
    if not raw:
        return {}
    # Убираем блоки ```json ... ```
    if "```" in raw:
        parts = raw.split("```")
        if len(parts) > 1:
            raw = parts[1].lstrip("json").strip()
    # Пробуем прямой json.loads
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Ищем первый {...} через regex
    m = re.search(r'\{.*?\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return {}


def check_skolkovo(
    company_name: str,
    company_data: dict,
    website: str | None = None,
    email: str | None = None,
    inn: str | None = None,
) -> tuple[bool, str]:
    """Проверить компанию на признаки Сколково через мультипровайдерный LLM-модуль.

    Returns (eligible, reason). Raises AllModelsExhausted если все провайдеры упали.
    """
    site_text = _fetch_site_text(website or "")

    datanewton_text = _fetch_datanewton(inn or "")
    if datanewton_text:
        log.info("  Сколково datanewton: %s", datanewton_text)

    prompt = _USER_PROMPT_TEMPLATE.format(
        name=company_name,
        website=website or "не указан",
        site_text=site_text or "не доступен",
        datanewton_text=datanewton_text or "нет данных",
    )

    try:
        raw = call_ai(_SYSTEM_PROMPT, prompt)
    except AllModelsExhausted:
        raise

    log.info("  Сколково RAW: %s", raw[:300])
    try:
        result   = _parse_json_response(raw)
        eligible = bool(result.get("eligible", False))
        reason   = str(result.get("reason", "Ошибка разбора ответа"))
        log.info("  Сколково: eligible=%s  reason=%s", eligible, reason)
        return (eligible, reason)
    except (json.JSONDecodeError, KeyError) as exc:
        log.warning("  Сколково: не удалось разобрать ответ — %s", exc)
        return (False, "Ошибка разбора ответа модели")
    except Exception as exc:
        log.warning("  Сколково: непредвиденная ошибка — %s", exc)
        return (False, f"Ошибка: {exc}")
