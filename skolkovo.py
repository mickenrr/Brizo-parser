"""skolkovo.py — проверка компании на признаки Сколково/IT-льгот через OpenRouter."""

import json
import logging
import os
import re

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "minimax/minimax-m3.1-flash:free"

_PROMPT_TEMPLATE = """Ты эксперт по налоговым льготам. Определи есть ли у компании '{name}' признаки для получения льгот по статье 284.1 НК РФ (IT-компании) или резидентства Сколково.

Сайт компании: {website}
Email домен: {email_domain}
Данные компании: {data}
Текст с сайта компании (если есть): {site_text}

На основе названия компании, сайта, домена и текста сайта определи вероятность наличия инновационной деятельности.

Критерии (достаточно одного):
1. Есть патенты на изобретения/полезные модели
2. Получали инновационные гранты (Сколково, РФРИТ, Фонд Бортника и др.)
3. Собственное ПО зарегистрировано в реестре Минцифры
4. Уникальная технология или рецептура собственной разработки
5. Собственный R&D отдел с конструкторской документацией

Отвечай ТОЛЬКО в формате JSON:
{{"eligible": true/false, "reason": "краткое объяснение на русском (1-2 предложения)"}}

Если данных недостаточно — отвечай {{"eligible": false, "reason": "Недостаточно данных для анализа"}}"""


def _fetch_site_text(url: str) -> str:
    """Загрузить сайт и вернуть первые 500 слов из <p> тегов. При любой ошибке — пустая строка."""
    if not url:
        return ""
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        r = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"}, verify=False)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        words = []
        for p in soup.find_all("p"):
            words.extend(p.get_text(" ", strip=True).split())
            if len(words) >= 500:
                break
        return " ".join(words[:500])
    except Exception as exc:
        log.debug("  Сколково: не удалось загрузить сайт %s — %s", url, exc)
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
) -> tuple[bool, str]:
    """Проверить компанию на признаки Сколково/IT-льгот через OpenRouter LLM.

    Returns (eligible, reason).
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        log.debug("OPENROUTER_API_KEY не задан — пропускаем Сколково-проверку")
        return (False, "OPENROUTER_API_KEY не задан")

    site_text = _fetch_site_text(website or "")
    if site_text:
        log.info("  Сколково: загружено %d слов с сайта %s", len(site_text.split()), website)

    prompt = _PROMPT_TEMPLATE.format(
        name=company_name,
        website=website or "не указан",
        email_domain=_extract_email_domain(email),
        data=json.dumps(company_data, ensure_ascii=False, default=str),
        site_text=site_text or "не доступен",
    )

    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": "Отвечай кратко. Только JSON. Без лишних рассуждений."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 1500,
            },
            timeout=30,
        )
        resp.raise_for_status()

        msg     = resp.json()["choices"][0]["message"]
        content = msg.get("content")
        reasoning = msg.get("reasoning") or ""
        log.info("  Сколково RAW content: %s", str(content)[:300])
        if not content:
            log.info("  Сколково RAW reasoning (последние 300): %s", reasoning[-300:])

        # Читаем content, при null — ищем JSON в reasoning (reasoning-модели)
        result = _parse_json_response(content) or _parse_json_response(reasoning)
        eligible = bool(result.get("eligible", False))
        reason   = str(result.get("reason", "Ошибка разбора ответа"))
        log.info("  Сколково: eligible=%s  reason=%s", eligible, reason)
        return (eligible, reason)

    except requests.HTTPError as exc:
        log.warning("  Сколково: HTTP ошибка %s", exc)
        return (False, f"HTTP ошибка: {exc}")
    except (json.JSONDecodeError, KeyError) as exc:
        log.warning("  Сколково: не удалось разобрать ответ — %s", exc)
        return (False, "Ошибка разбора ответа модели")
    except Exception as exc:
        log.warning("  Сколково: непредвиденная ошибка — %s", exc)
        return (False, f"Ошибка: {exc}")
