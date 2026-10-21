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

_SYSTEM_PROMPT = 'You are an analyst determining if Russian companies qualify for Skolkovo innovation center. Respond ONLY with valid JSON: {"eligible": true/false, "reason": "one sentence in Russian"}'

_USER_PROMPT_TEMPLATE = """Определи, подходит ли компания под Сколково.

Компания: {name}
Сайт: {website}
Текст с сайта: {site_text}
Данные с datanewton.ru (патенты и гранты): {datanewton_text}

Критерии «подходит» (достаточно одного):
— Патент на изобретение или полезную модель
— Грант на инновации/НИОКР (Сколково, РФФИ, Фонд Бортника, Минпромторг)
— Собственное ПО в реестре Минцифры
— Уникальная собственная технология/рецептура с КД и НИОКР
— Собственное нестандартное оборудование серийного производства с конструкторской документацией

НЕ подходят:
— Перепродажа чужого ПО или оборудования
— Заказная разработка без своего продукта
— Системный интегратор
— Контрактное производство по чертежам заказчика
— Сервисная компания (бухгалтерия, юристы, логистика)

Примеры подходящих компаний:
✅ НПО ГЕОМАШ — разрабатывает и производит скважинную телеметрическую аппаратуру для каротажа, собственные приборы с серийными номерами
✅ БАЗИС-ЦЕНТР — разработчик САПР «БАЗИС» для проектирования мебели, собственная IT-система с мобильным приложением в RuStore
✅ АМПЛИТЕК — разрабатывает ПЦР-наборы и реагенты для диагностики in vitro, собственное производство медизделий
✅ ЭНТЕЛС — разработчик SCADA ЭНТЕК и приборов учёта «НАРТИС», ПО внесено в реестр отечественного ПО Минцифры
✅ НЕОКОР — производитель биопротезов клапанов сердца (ТиАра, ЮниЛайн), 50 лет собственных разработок с хирургами
✅ С-ИНФО — разработчик BIM-ПО S•INFO для транспортной инфраструктуры, несколько версий (Desktop/Web/Mobile), свидетельства на ПО
✅ СОНДА — производство ковшей из износостойких сталей с запатентованной системой Britva®
✅ МИТИГАТОР КЛАУД — облачная DDoS-защита MITIGATOR, собственная разработка, лицензии ФСТЭК и ФСБ
✅ АКТЕК — разработка и производство метрологического оборудования, собственный конструкторский отдел и отдел разработки ПО
✅ СИСТЕМЫ ПОДДЕРЖАНИЯ СТЕРИЛЬНОСТИ — производство УФ-рециркуляторов и аппаратов физиотерапии, узкоспециализированный собственный продукт

Примеры НЕ подходящих компаний:
❌ ВЕДА ПЛЮС — региональный дистрибьютор КонсультантПлюс, перепродаёт чужое ПО
❌ ВБК ЦЕНТР — системный интегратор, поставляет инфраструктуру Lenovo/Yadro/Qnap
❌ КЛЕВЕРТЕК — заказная разработка мобильных приложений для банков, своего продукта нет
❌ АЛЬФА — контрактная металлообработка по чертежам заказчика, конструкторское бюро обслуживает заказы а не разрабатывает собственные изделия
❌ АКВА-СИТИ — продажа и обслуживание аквапарков, торгово-сервисная компания

Если текст сайта пустой — делай вывод только по названию компании.
Ответь JSON: {{"eligible": true/false, "reason": "ДВЕ части через точку: 1) чем занимается компания (одна фраза, конкретно: что производит/продаёт/делает); 2) почему подходит или не подходит под Сколково (одна фраза с конкретной причиной). Пример eligible=false: 'Строительно-монтажные услуги в электроэнергетике. Не подходит — сервисная компания без собственных разработок.' Пример eligible=true: 'Производит ПЦР-наборы и реагенты для диагностики in vitro. Подходит — собственная разработка медизделий с производством.'"}}"""


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
    inn: str | None = None,
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
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 3000,
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
