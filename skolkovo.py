"""skolkovo.py — проверка компании на признаки Сколково/IT-льгот через OpenRouter."""

import json
import logging
import os

import requests

log = logging.getLogger(__name__)

OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "meta-llama/llama-3.1-8b-instruct:free"

_PROMPT_TEMPLATE = """Ты эксперт по налоговым льготам. Определи есть ли у компании '{name}' признаки для получения льгот по статье 284.1 НК РФ (IT-компании) или резидентства Сколково.

Данные компании: {data}

Критерии (достаточно одного):
1. Есть патенты на изобретения/полезные модели
2. Получали инновационные гранты (Сколково, РФРИТ, Фонд Бортника и др.)
3. Собственное ПО зарегистрировано в реестре Минцифры
4. Уникальная технология или рецептура собственной разработки
5. Собственный R&D отдел с конструкторской документацией

Отвечай ТОЛЬКО в формате JSON:
{{"eligible": true/false, "reason": "краткое объяснение на русском (1-2 предложения)"}}

Если данных недостаточно — отвечай {{"eligible": false, "reason": "Недостаточно данных для анализа"}}"""


def check_skolkovo(company_name: str, company_data: dict) -> tuple[bool, str]:
    """Проверить компанию на признаки Сколково/IT-льгот через OpenRouter LLM.

    Returns (eligible, reason).
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        log.debug("OPENROUTER_API_KEY не задан — пропускаем Сколково-проверку")
        return (False, "OPENROUTER_API_KEY не задан")

    prompt = _PROMPT_TEMPLATE.format(
        name=company_name,
        data=json.dumps(company_data, ensure_ascii=False, default=str),
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
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()

        # Извлекаем JSON (модель иногда оборачивает в ```json ... ```)
        if "```" in raw:
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        result   = json.loads(raw)
        eligible = bool(result.get("eligible", False))
        reason   = str(result.get("reason", ""))
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
