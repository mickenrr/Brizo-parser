import atexit
import os
import re
import socket as _socket_module
import time
import random
import requests
import socks as _socks_lib
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# Глобальный таймаут на уровне сокета — защита от вечных зависаний.
# Timeout в requests не всегда срабатывает через SOCKS5-монкипатч.
# setdefaulttimeout применяется ко всем новым сокетам, включая socks.socksocket.
_socket_module.setdefaulttimeout(30)

BASE_URL = os.getenv("CHECKO_URL", "https://checko.ru").rstrip("/")

HEADERS = {
    # Основные заголовки браузера
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    # НЕ указываем Accept-Encoding вручную — requests сам добавляет
    # "gzip, deflate" и умеет их распаковывать. Если прописать "br" (brotli),
    # сервер начнёт отвечать brotli, который requests не поддерживает без
    # дополнительного пакета — контент приходит нечитаемым бинарником.
    # Sec-Fetch заголовки — Chrome 87+ всегда их шлёт при навигации.
    # Их отсутствие — явный признак бота для современных WAF/rate-limiter.
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    # Client Hints — часть fingerprint Chrome 90+
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    # Upgrade-Insecure-Requests — стандарт для browser navigation
    "Upgrade-Insecure-Requests": "1",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ---------------------------------------------------------------------------
# Proxy rotation
# ---------------------------------------------------------------------------
# Поддерживаемые форматы в proxies.txt (одна строка = один прокси):
#   ip:port:username:password         — формат webshare.io
#   http://username:password@ip:port  — стандартный URL
#   http://ip:port                    — без авторизации
#
# Также читается переменная PROXY_URL из .env (один прокси).
#
# Логика ротации:
#   — пока запросы успешны → используем ОДИН прокси
#   — получили 429 или сетевую ошибку → переключаемся на следующий

_PROXIES: list[dict] = []
_proxy_idx: int = 0
_PROXY_BLOCKED_UNTIL: dict[int, float] = {}   # index → time.time() когда разблокируется
_PROXY_COOLDOWN_SEC = 8 * 60                   # 8 минут кулдаун после 429
# CHECKO_FORCE_PROXY=1 → всегда через прокси (нужно для Railway где прямой IP заблокирован)
_use_proxy_mode: bool = os.getenv("CHECKO_FORCE_PROXY", "").strip() in ("1", "true", "yes")

# Флаг «видели 429 в последнем _get()» — для вызывающего кода, которому
# нужно среагировать на rate-limit своей собственной паузой (см. lead_generator.py).
# Не влияет на поведение _get() — только фиксирует факт для внешнего наблюдателя.
_last_429: bool = False

# Флаг «Checko заблокировал запрос» — True когда _get() вернул None из-за 429,
# а не просто потому что компания не найдена. Сбрасывается в get_company_data().
_request_blocked: bool = False
_network_error: bool = False


def consume_429() -> bool:
    """Вернуть True если с прошлого вызова был замечен 429, и сбросить флаг."""
    global _last_429
    seen, _last_429 = _last_429, False
    return seen


def was_request_blocked() -> bool:
    """Вернуть True если последний get_company_data() завершился блокировкой (429 или капча)."""
    return _request_blocked


def was_network_error() -> bool:
    """Вернуть True если последний get_company_data() упал из-за таймаута/обрыва соединения."""
    return _network_error


def _load_proxies() -> None:
    """Load proxy list from proxies.txt or PROXY_URL env var."""
    global _proxy_idx
    _PROXIES.clear()
    _proxy_idx = 0

    def _parse_line(line: str) -> dict | None:
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        if line.startswith("socks5") or line.startswith("socks4"):
            url = line
        elif line.startswith("http"):
            url = line
        else:
            parts = line.split(":")
            if len(parts) == 4:
                ip, port, user, pw = parts
                # proxy6.net использует SOCKS5 на порту 8000
                url = f"socks5://{user}:{pw}@{ip}:{port}"
            elif len(parts) == 2:
                url = f"http://{line}"
            else:
                return None
        return {"http": url, "https": url}

    # 1. PROXY_URL (single proxy from .env)
    single = os.getenv("PROXY_URL", "").strip()
    if single:
        p = _parse_line(single)
        if p:
            _PROXIES.append(p)

    # 2. proxies.txt (one proxy per line, webshare.io format)
    proxy_file = os.path.join(os.path.dirname(__file__), "proxies.txt")
    try:
        with open(proxy_file) as f:
            for line in f:
                p = _parse_line(line)
                if p and p not in _PROXIES:
                    _PROXIES.append(p)
    except FileNotFoundError:
        pass

    if _PROXIES:
        # Стартуем со случайного прокси — воркеры не будут толпиться на одном
        _proxy_idx = random.randint(0, len(_PROXIES) - 1)
        print(f"[checko] прокси: {len(_PROXIES)} шт. — ротация при 429 (старт с #{_proxy_idx + 1})")
    else:
        print("[checko] прокси не настроены — прямое подключение")


def rotate_proxy() -> None:
    """
    Помечаем текущий прокси как заблокированный на _PROXY_COOLDOWN_SEC секунд,
    затем переключаемся на первый свободный.
    """
    global _proxy_idx
    if not _PROXIES:
        return
    # Блокируем текущий
    _PROXY_BLOCKED_UNTIL[_proxy_idx] = time.time() + _PROXY_COOLDOWN_SEC
    # Ищем первый незаблокированный
    now = time.time()
    for offset in range(1, len(_PROXIES) + 1):
        candidate = (_proxy_idx + offset) % len(_PROXIES)
        if _PROXY_BLOCKED_UNTIL.get(candidate, 0) <= now:
            _proxy_idx = candidate
            print(f"[checko] → прокси #{_proxy_idx + 1}/{len(_PROXIES)}")
            return
    # Все заблокированы — ждём ближайшего разблокирования
    earliest_idx = min(_PROXY_BLOCKED_UNTIL, key=_PROXY_BLOCKED_UNTIL.get)
    wait = max(0, _PROXY_BLOCKED_UNTIL[earliest_idx] - now)
    print(f"[checko] все прокси в кулдауне, ждём {wait:.0f} сек...")
    time.sleep(wait + 1)
    _PROXY_BLOCKED_UNTIL[earliest_idx] = 0
    _proxy_idx = earliest_idx
    print(f"[checko] → прокси #{_proxy_idx + 1}/{len(_PROXIES)} (разблокирован)")


def _proxy() -> dict | None:
    """Return current proxy dict, or None if not configured."""
    if not _PROXIES:
        return None
    # Пропускаем заблокированные (на случай если cooldown не истёк)
    now = time.time()
    if _PROXY_BLOCKED_UNTIL.get(_proxy_idx, 0) > now:
        # Текущий ещё заблокирован — ищем свободный
        for offset in range(1, len(_PROXIES) + 1):
            candidate = (_proxy_idx + offset) % len(_PROXIES)
            if _PROXY_BLOCKED_UNTIL.get(candidate, 0) <= now:
                return _PROXIES[candidate]
    return _PROXIES[_proxy_idx]


# Оригинальный socket.socket для восстановления после монкипатча
_orig_socket = _socket_module.socket


def _is_socks5(proxy: dict | None) -> bool:
    """True если прокси — SOCKS5 (proxy6.net и аналоги)."""
    if not proxy:
        return False
    url = proxy.get("https") or proxy.get("http", "")
    return url.startswith("socks5") or url.startswith("socks4")


def _apply_proxy() -> None:
    """
    Применить текущий прокси к глобальному socket.socket.

    Для SOCKS5: монкипатчим socket.socket → socks.socksocket.
    Для HTTP: ничего не меняем (requests сам использует proxies= kwarg).
    """
    p = _proxy()
    if not p or not _is_socks5(p):
        return
    from urllib.parse import urlparse
    url = p.get("https") or p.get("http", "")
    parsed = urlparse(url)
    _socks_lib.set_default_proxy(
        _socks_lib.SOCKS5,
        parsed.hostname,
        parsed.port,
        username=parsed.username,
        password=parsed.password,
    )
    _socket_module.socket = _socks_lib.socksocket


def _remove_proxy() -> None:
    """Снять монкипатч — восстановить оригинальный socket.socket."""
    _socks_lib.set_default_proxy()
    _socket_module.socket = _orig_socket


_load_proxies()


def get_pw_proxy() -> dict | None:
    """
    Возвращает прокси-конфиг для Playwright browser context.

    Playwright не использует monkey-patch socket — прокси нужно передавать
    явно через параметр proxy={'server': '...'} при создании контекста.
    Возвращает None если прокси не настроены.
    """
    p = _proxy()
    if not p:
        return None
    url = p.get("https") or p.get("http") or ""
    if not url:
        return None
    return {"server": url}


# ---------------------------------------------------------------------------
# Minimal wrapper: позволяет вернуть контент Playwright как requests.Response
# ---------------------------------------------------------------------------

class _PlaywrightResponse:
    """Замена requests.Response — содержит HTML/JSON из браузерного запроса."""
    def __init__(self, text: str, url: str = "", status_code: int = 200):
        self.text = text
        self.url  = url
        self.status_code = status_code

    def json(self):
        import json as _json
        return _json.loads(self.text)


# ---------------------------------------------------------------------------
# Playwright page for search.
# The caller (main.py) must inject an already-running page via set_pw_page()
# to avoid nesting two sync_playwright() contexts in the same thread.
# ---------------------------------------------------------------------------

_external_pw_page = None   # injected from main.py
_pw_instance      = None   # fallback: own playwright instance
_pw_browser       = None
_pw_page          = None


def set_pw_page(page) -> None:
    """Provide an existing Playwright page so checko.py doesn't start its own."""
    global _external_pw_page
    _external_pw_page = page


def _get_pw_page():
    if _external_pw_page is not None:
        return _external_pw_page
    # Fallback: start own playwright (only works when NOT inside another sync_playwright context)
    global _pw_instance, _pw_browser, _pw_page
    from playwright.sync_api import sync_playwright
    if _pw_page is None:
        _pw_instance = sync_playwright().start()
        _pw_browser  = _pw_instance.chromium.launch(headless=True)
        _pw_page     = _pw_browser.new_page(viewport={"width": 1280, "height": 900})
        atexit.register(_cleanup_pw)
    return _pw_page


def _cleanup_pw():
    global _pw_instance, _pw_browser, _pw_page
    try:
        if _pw_browser:
            _pw_browser.close()
        if _pw_instance:
            _pw_instance.stop()
    except Exception:
        pass
    _pw_page = _pw_browser = _pw_instance = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Текстовые маркеры Яндекс SmartCaptcha на страницах Checko
_CAPTCHA_MARKERS = (
    "smartcaptcha",
    "я не робот",
    "подтвердите, что вы человек",
    "yandex smartcaptcha",
)


def _is_captcha_html(html: str) -> bool:
    """Вернуть True если HTML содержит признаки Яндекс SmartCaptcha."""
    lower = html.lower()
    return any(m in lower for m in _CAPTCHA_MARKERS)


def _pause() -> None:
    time.sleep(random.uniform(1.5, 2.5))


def _parse_amount_rus(text: str) -> int | None:
    """Parse Russian money amounts: '202,2 млн' → 202_200_000, '19 568 777' → 19_568_777."""
    if not text:
        return None
    text = text.replace("\xa0", " ").replace(",", ".")
    mlrd = re.search(r"(-?[\d]+(?:\.[\d]+)?)\s*млрд", text)
    if mlrd:
        val = float(mlrd.group(1))
        return int(val * 1_000_000_000)
    mln = re.search(r"(-?[\d]+(?:\.[\d]+)?)\s*млн", text)
    if mln:
        val = float(mln.group(1))
        return int(val * 1_000_000)
    tys = re.search(r"(-?[\d]+(?:\.[\d]+)?)\s*тыс", text)
    if tys:
        val = float(tys.group(1))
        return int(val * 1_000)
    # Plain number with spaces as thousands separator (e.g. "19 568 777 руб")
    rub = re.search(r"(-?[\d][\d\s]*)\s*руб", text)
    if rub:
        digits = re.sub(r"[^\d]", "", rub.group(1))
        return int(digits) if digits else None
    return None


def _get(url: str, retries: int = 3, referer: str | None = None) -> requests.Response | None:
    """
    GET запрос к Checko.

    Стратегия:
    1. curl UA (без прокси) — Checko не банит простые клиенты.
    2. Chrome UA (SESSION) с прокси-ротацией — если первый вариант не работает.
    """
    # ── Попытка 1: curl UA, прямой запрос (работает даже при банах на Chrome UA) ──
    _saw_429 = False
    _had_exc = False
    try:
        resp = _CURL_SESSION.get(url, timeout=20, allow_redirects=True)
        if resp.status_code == 200:
            return resp
        if resp.status_code == 429:
            global _last_429
            _last_429 = True
            _saw_429 = True
            print(f"[checko] curl UA 429 на {url} — пробуем SESSION")
        # Для других кодов ошибок тоже пробуем через SESSION
    except Exception as e:
        print(f"[checko] curl GET {url} failed: {e}")

    # ── Попытка 2: SESSION с имитацией браузера и ротацией прокси ──
    if referer is None:
        if "/company/" in url and "/select" not in url:
            referer = BASE_URL + "/company/select"
        else:
            referer = BASE_URL + "/"

    global _use_proxy_mode, _request_blocked, _network_error
    headers = {"Referer": referer, "Sec-Fetch-Site": "same-origin"}
    for attempt in range(1, retries + 1):
        try:
            # Первая попытка — прямое соединение без прокси.
            # Прокси подключается только если получили 429.
            use_proxy = _use_proxy_mode and _PROXIES
            p = _proxy() if use_proxy else None
            use_socks5 = _is_socks5(p)
            if use_socks5:
                _apply_proxy()
                req_proxies = None
            else:
                req_proxies = p

            try:
                resp = SESSION.get(url, timeout=20, headers=headers, proxies=req_proxies)
            finally:
                if use_socks5:
                    _remove_proxy()

            if resp.status_code == 429:
                _last_429 = True
                _saw_429 = True
                _use_proxy_mode = True  # теперь переключаемся на прокси
                print(f"[checko] 429 на {url} (попытка {attempt}/{retries}) — включаем прокси")
                rotate_proxy()
                if attempt < retries:
                    time.sleep(random.uniform(2, 4))
                    continue
                break
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            print(f"[checko] GET {url} attempt {attempt}/{retries} failed: {e}")
            _had_exc = True
            if _use_proxy_mode:
                rotate_proxy()
            if attempt < retries:
                time.sleep(3 * attempt)

    # ── Попытка 3: браузерный запрос (cookies + сессия Checko, без навигации) ──
    try:
        page = _get_pw_page()
        print(f"[checko] browser request для {url}")
        br = page.request.get(url, headers={"Referer": BASE_URL + "/"}, timeout=25_000)
        if br.ok:
            html = br.text()
            if _is_captcha_html(html):
                print(f"[checko] ⚠️ КАПЧА в ответе browser request для {url}")
                _request_blocked = True
                return None
            return _PlaywrightResponse(html, url)
        print(f"[checko] browser request → {br.status}")
        if br.status == 429:
            _saw_429 = True
    except Exception as e:
        print(f"[checko] browser request failed для {url}: {e}")

    if _saw_429:
        _request_blocked = True
    elif _had_exc:
        _network_error = True
    return None


# ---------------------------------------------------------------------------
# Step 1 — resolve INN → company URL
# ---------------------------------------------------------------------------

# Отдельная сессия для поиска — использует curl User-Agent чтобы не попасть
# под rate-limit Checko (сайт блокирует Chrome/Firefox UA агрессивнее).
_CURL_SESSION = requests.Session()
_CURL_SESSION.headers.update({
    "User-Agent": "curl/7.88.1",
    "Accept": "*/*",
})


def _resolve_url(inn: str) -> str | None:
    """
    Resolve INN → Checko company URL using Playwright browser only (no curl).

    Strategy:
    1. Navigate to /?q={INN} — wait networkidle — check redirect / find link
    2. Navigate to /search?query={INN} — same logic (fallback)
    """
    _SKIP = ("/select", "/updates", "/edit", "/contacts", "/details", "/finances", "/activity")

    page = _get_pw_page()

    def _company_url_from_page() -> str | None:
        """
        After navigation: if Checko redirected to a company page → return it.
        Otherwise find first /company/ link in search results via JS.
        """
        current = page.url or ""
        if "/company/" in current and "?q=" not in current and "/search" not in current:
            clean = current.split("?")[0].rstrip("/")
            print(f"[checko] редирект на страницу компании: {clean}")
            return clean
        try:
            links = page.evaluate("""() => {
                return Array.from(document.querySelectorAll('a[href*="/company/"]'))
                    .filter(a => {
                        const h = a.href || '';
                        return !h.includes('/select') && !h.includes('/updates')
                            && !h.includes('/edit') && !h.includes('/contacts')
                            && !h.includes('/details') && !h.includes('/finances')
                            && !h.includes('/activity');
                    })
                    .map(a => a.href);
            }""")
            if links:
                print(f"[checko] найдена ссылка на компанию: {links[0]}")
                return links[0]
        except Exception as e:
            print(f"[checko] JS eval error: {e}")
        print(f"[checko] url={current[:80]}  ссылок=0")
        return None

    def _goto_and_find(target_url: str) -> str | None:
        global _request_blocked, _network_error
        try:
            print(f"[checko] browser goto: {target_url}")
            page.goto(target_url, timeout=30_000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            # Проверяем: не показывает ли Checko капчу Яндекс SmartCaptcha
            try:
                html = page.content()
                if _is_captcha_html(html):
                    print(f"[checko] ⚠️ КАПЧА обнаружена на {page.url}")
                    _request_blocked = True
                    return None
            except Exception:
                pass
            return _company_url_from_page()
        except Exception as e:
            print(f"[checko] goto failed ({target_url}): {e}")
            _network_error = True
            return None

    print(f"[checko] ищем по ИНН {inn}")

    # ── 1. Основной: /?q={INN} ───────────────────────────────────────────────
    found = _goto_and_find(f"{BASE_URL}/?q={inn}")
    if _request_blocked:
        return None  # Капча — не пробуем второй URL
    if found:
        print(f"[checko] INN {inn} → {found}")
        return found

    # ── 2. Запасной: /search?query={INN} ─────────────────────────────────────
    found = _goto_and_find(f"{BASE_URL}/search?query={inn}")
    if found:
        print(f"[checko] INN {inn} → {found}")
        return found

    print(f"[checko] could not resolve URL for INN {inn}")
    return None


# ---------------------------------------------------------------------------
# Step 2 — parse static company page
# ---------------------------------------------------------------------------

def _parse_company_page(text: str, company_url: str) -> dict:
    """
    Extract structured company data from the plain text of a checko.ru page.
    """
    data: dict = {"checko_url": company_url}

    # ── Company INN (10 digits, not to be confused with director INN = 12 digits) ──
    inn_m = re.search(r'ИНН\s+(\d{10})(?!\d)', text)
    data["inn"] = inn_m.group(1) if inn_m else ""

    # ── Revenue ───────────────────────────────────────────────────────────────
    # "Выручка 202,2 млн руб." / "Выручка = выросла до 1,2 млрд руб." / "Выручка снизилась до..."
    rev_m = re.search(
        r"Выручка[\s=]*(?:снизилась до\s+|выросла до\s+)?(-?[\d,\.]+\s*(?:млрд|млн|тыс|руб))",
        text,
    )
    data["revenue"] = _parse_amount_rus(rev_m.group(1)) if rev_m else None

    # ── Taxes ─────────────────────────────────────────────────────────────────
    # "Уплачены налоги на сумму 15,2 млн руб."
    tax_m = re.search(r"[Уу]плачены налоги на сумму\s+([\d,\.\s]+(?:млн|тыс|руб))", text)
    data["taxes_total"] = _parse_amount_rus(tax_m.group(1)) if tax_m else None

    # If that didn't work, try "Итого NNN руб." inside taxes section
    if data["taxes_total"] is None:
        itogo_m = re.search(r"Итого\s+([\d\s]+)\s*руб", text)
        if itogo_m:
            digits = re.sub(r"\D", "", itogo_m.group(1))
            data["taxes_total"] = int(digits) if digits else None

    # ── Income tax (last 3 years) ─────────────────────────────────────────────
    # Strategy 1: multiple "Налог на прибыль ... amount" occurrences (one per year section)
    income_tax_amounts: list[int] = []
    for m in re.finditer(r"[Нн]алог на прибыль[^0-9]*?([\d][\d\s,\.]+(?:руб|млн|тыс|млрд))", text):
        amount = _parse_amount_rus(m.group(1))
        if amount is not None:
            income_tax_amounts.append(amount)
        if len(income_tax_amounts) >= 3:
            break

    # Strategy 2: all amounts on one row after the first "Налог на прибыль" mention
    if len(income_tax_amounts) <= 1:
        first = re.search(r"[Нн]алог на прибыль", text)
        if first:
            window = text[first.start(): first.start() + 400]
            candidates: list[int] = []
            for m in re.finditer(r"(-?[\d][\d\s]*(?:[.,]\d+)?)\s*(млрд|млн|тыс)", window):
                a = _parse_amount_rus(m.group(0))
                if a is not None:
                    candidates.append(a)
                if len(candidates) >= 3:
                    break
            if not candidates:
                for m in re.finditer(r"(\d[\d\s]{5,})\s*руб", window):
                    digits = re.sub(r"\D", "", m.group(1))
                    if digits:
                        candidates.append(int(digits))
                    if len(candidates) >= 3:
                        break
            if len(candidates) > len(income_tax_amounts):
                income_tax_amounts = candidates

    data["income_tax_amounts"] = income_tax_amounts
    data["income_tax_exists"]  = any(a > 0 for a in income_tax_amounts)
    data["income_tax_amount"]  = income_tax_amounts[0] if income_tax_amounts else None
    data["income_tax_max_3y"]  = max(income_tax_amounts) if income_tax_amounts else None

    # ── Director ──────────────────────────────────────────────────────────────
    # Name words must start uppercase and have ≥1 lowercase (prevents matching
    # abbreviations like "Р." or all-caps tokens like "ИНН")
    _NAME_WORD = r"[А-ЯЁ][а-яё]+"
    # 2–3 word Russian names (Фамилия Имя [Отчество]); cap at {1,2} to avoid
    # capturing navigation words like "Все", "Лицензии" after the name.
    _NAME_PAT  = rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{1,2}}"
    dir_titles = [
        "Генеральный директор",
        "Директор",
        "Гендиректор",
        r"Руководитель(?:\s+(?:Генеральный директор|Директор|Гендиректор))?",
    ]
    dir_m = None
    for title in dir_titles:
        dir_m = re.search(rf"(?:{title})\s+({_NAME_PAT})", text)
        if dir_m:
            break

    if dir_m:
        data["director_name"] = dir_m.group(1).strip()
        after = text[dir_m.end(): dir_m.end() + 120]
        inn_m = re.search(r"ИНН\s+(\d{12})", after)
        data["director_inn"] = inn_m.group(1) if inn_m else ""
    else:
        data["director_name"] = ""
        data["director_inn"]  = ""

    # ── Founders (physical persons only) ──────────────────────────────────────
    # Words that appear in Checko table headers / financial labels but not in names
    _NON_NAME_WORDS = {
        "Стоимость", "Дата", "Доля", "Учредитель", "Руководитель",
        "Директор", "Основание", "Сумма", "Итого", "Процент",
    }
    founders: list[dict] = []
    seen_f: set[str] = set()

    def _add_founder(name: str, inn: str, share_pct: str) -> None:
        name = name.strip()
        if not name or name in seen_f:
            return
        if re.search(r"\b(ООО|ЗАО|ПАО|АО|ИП)\b", name):
            return
        if any(w in _NON_NAME_WORDS for w in name.split()):
            return
        seen_f.add(name)
        founders.append({"name": name, "inn": inn, "share_pct": share_pct})

    # Format 1 — numbered table rows: "1. Иванов Иван Иванович ИНН 123456789012 ... 50%"
    # Checko shows this in the founders table; INN is always present here.
    for m in re.finditer(rf"\d+\.\s+({_NAME_PAT})\s+ИНН\s+(\d{{12}})", text):
        after_f = text[m.end(): m.end() + 100]
        share_m = re.search(r"([\d]+(?:[.,]\d+)?)\s*%", after_f)
        _add_founder(
            m.group(1),
            m.group(2),
            share_m.group(1).replace(",", ".") if share_m else "",
        )

    # Format 2 — simple mention: "Учредитель Иванов Иван Иванович"
    # INN may appear immediately after the name or not at all.
    # Only adds names not already captured by Format 1 (which has guaranteed INN).
    for m in re.finditer(rf"[Уу]чредитель\s+({_NAME_PAT})", text):
        name = m.group(1).strip()
        if name in seen_f:
            continue
        after_f = text[m.end(): m.end() + 200]
        inn_m2 = re.search(r"ИНН\s+(\d{12})", after_f)
        inn_f = inn_m2.group(1) if inn_m2 else ""
        share_m = re.search(r"([\d]+(?:[.,]\d+)?)\s*%", after_f[:80])
        _add_founder(
            name,
            inn_f,
            share_m.group(1).replace(",", ".") if share_m else "",
        )

    data["founders"] = founders

    # ── Legal address ─────────────────────────────────────────────────────────
    # "Юридический адрес … 665776, Иркутская область, г. Братск …"
    # Strategy: find "Юридический адрес", then scan up to 500 chars ahead for
    # the 6-digit postal code (addresses always start with one).
    legal_address = ""
    section_m = re.search(r"[Юю]ридический адрес", text)
    if section_m:
        window = text[section_m.start(): section_m.start() + 500]
        postal_m = re.search(r"(\d{6},\s*[А-ЯЁа-яё][^•\n]{10,250})", window)
        if postal_m:
            legal_address = postal_m.group(1).strip()
    data["legal_address"] = legal_address

    # ── Website ───────────────────────────────────────────────────────────────
    # Parse from the original HTML for <a> tags (text already stripped)
    # We reconstruct from text pattern
    _SKIP_DOMAINS = (
        "checko.ru", "chrome.google.com", "google.com",
        "vk.com", "facebook.com", "instagram.com", "t.me",
        "youtube.com", "yandex.ru", "gosuslugi.ru", "egrul.nalog.ru",
        "kad.arbitr.ru",
    )
    website_m = re.search(r"(https?://[^\s\"'<>\)]{5,})", text)
    website = ""
    if website_m:
        candidate = website_m.group(1).rstrip(".,;)")
        if not any(d in candidate for d in _SKIP_DOMAINS):
            website = candidate
    data["website"] = website

    # ── ОКВЭД codes ──────────────────────────────────────────────────────────
    # Format: XX.XX or XX.XX.X  (never XX.XX.YYYY — that's a date)
    seen_codes: set[str] = set()
    okved_codes: list[str] = []
    for m in re.finditer(r'\b(\d{2}\.\d{2}(?:\.\d{1,2})?)\b', text):
        code = m.group(1)
        # Skip if this is actually part of a date (DD.MM.YYYY)
        after = text[m.end(): m.end() + 5]
        if re.match(r'\.\d{4}', after):
            continue
        if code not in seen_codes:
            seen_codes.add(code)
            okved_codes.append(code)
        if len(okved_codes) >= 20:
            break
    data["okved_codes"] = okved_codes

    return data


_SKIP_DOMAINS = (
    "checko.ru", "chrome.google.com", "google.com",
    "vk.com", "facebook.com", "instagram.com", "t.me",
    "youtube.com", "yandex.ru", "gosuslugi.ru", "egrul.nalog.ru",
    "kad.arbitr.ru", "nalog.ru", "trudvsem.ru", "fedresurs.ru",
    "arbitr.ru", "rnp.fas.gov.ru", "zakupki.gov.ru", "rosreestr.ru",
    "pfr.gov.ru", "fss.ru", "rosstat.gov.ru", "economy.gov.ru",
    "sbis.ru", "rusprofile.ru", "spark-interfax.ru", "list-org.com",
    "rospotrebnadzor.ru", "fssp.gov.ru", "rkn.gov.ru",
    "fips.ru",
)


def _extract_url_from_href(href: str) -> str:
    """Resolve direct and redirect-style hrefs to a company website URL."""
    if href.startswith("http"):
        return href
    # Checko sometimes wraps external links: /ext?url=https://... or /link?url=...
    if "url=" in href:
        from urllib.parse import urlparse, parse_qs
        try:
            qs = parse_qs(urlparse(href).query)
            candidate = (qs.get("url") or qs.get("href") or [""])[0]
            if candidate.startswith("http"):
                return candidate
        except Exception:
            pass
    return ""


def _find_website_in_html(html: str) -> str:
    """Extract company website from raw HTML."""
    soup = BeautifulSoup(html, "html.parser")

    # Try contacts section first (more precise)
    contacts_section = None
    for tag in soup.find_all(["section", "div", "article", "h2", "h3"]):
        txt = (tag.get_text(strip=True) or "").lower()
        if "контакт" in txt and len(txt) < 200:
            contacts_section = tag
            break

    # Search inside contacts section, then fall back to whole page
    scopes = [contacts_section, soup] if contacts_section else [soup]
    for scope in scopes:
        for a in scope.find_all("a", href=True):
            url = _extract_url_from_href(a["href"])
            if url and not any(d in url for d in _SKIP_DOMAINS):
                return url
    return ""


def _find_emails_in_html(html: str) -> list[str]:
    """
    Extract corporate email addresses from Checko HTML.

    Picks up <a href="mailto:..."> links. Deduplicates and filters out
    clearly personal / generic addresses (like noreply, admin@ that are
    infrastructure emails rather than company contacts).
    """
    _SKIP_PREFIXES = ("noreply", "no-reply", "postmaster", "webmaster", "mailer-daemon")
    _SKIP_DOMAINS  = ("checko.ru",)

    emails: list[str] = []
    seen:   set[str]  = set()

    for m in re.finditer(r'href=["\']mailto:([^"\'?\s]+)["\']', html, re.IGNORECASE):
        addr = m.group(1).strip().lower()
        if "@" not in addr:
            continue
        local, domain = addr.rsplit("@", 1)
        if any(domain.endswith(d) for d in _SKIP_DOMAINS):
            continue
        if any(local.startswith(p) for p in _SKIP_PREFIXES):
            continue
        if addr not in seen:
            seen.add(addr)
            emails.append(addr)

    return emails


# ---------------------------------------------------------------------------
# Income tax from detailed taxes page
# ---------------------------------------------------------------------------

def _get_income_tax_from_taxes_page(company_url: str) -> tuple[list[int], int | None]:
    """
    Fetch checko.ru/company/{OGRN}/taxes/data and extract two values:
      - «Налог на прибыль»: last 3 calendar years, most-recent-first (list[int])
      - «Итого»: total taxes for the last year (int | None)

    Returns ([], None) if the page is unavailable or table not found.

    URL format: Checko's /taxes/data works only via OGRN, not slug.
      slug URL:  /company/npo-svyazproekt-5087746032589
      taxes URL: /company/5087746032589/taxes/data   ← OGRN only

    Why: the main company page aggregates/rounds tax data and can show
    incorrect amounts. The /taxes/data page (ФНС open data) is authoritative.
    """
    global _request_blocked, _network_error
    # Extract OGRN (13–15 digits at end of slug, e.g. "slug-name-1027739714606")
    ogrn_m = re.search(r"-(\d{13,15})$", company_url.rstrip("/"))
    if not ogrn_m:
        # URL might already be OGRN-only (e.g. /company/1047796514105)
        ogrn_m = re.search(r"/(\d{13,15})$", company_url.rstrip("/"))
    if not ogrn_m:
        return [], []
    ogrn = ogrn_m.group(1)
    taxes_url = f"{BASE_URL}/company/{ogrn}/taxes/data"

    # Стратегия 1: curl UA (не триггерит rate-limit, быстрый таймаут)
    # Стратегия 2: SESSION с browser UA + прокси (резервная)
    tax_headers = {"Referer": company_url, "Sec-Fetch-Site": "same-origin"}
    time.sleep(random.uniform(0.8, 1.2))

    def _taxes_get():
        p = _proxy()
        if _is_socks5(p):
            _apply_proxy()
            try:
                return SESSION.get(taxes_url, timeout=15, headers=tax_headers)
            finally:
                _remove_proxy()
        else:
            return SESSION.get(taxes_url, timeout=15, headers=tax_headers, proxies=p)

    resp = None

    # ── Попытка 1: curl UA (обходит rate-limit, как для поиска и основных страниц) ──
    try:
        resp = _CURL_SESSION.get(taxes_url, timeout=12,
                                 headers={"Referer": company_url}, allow_redirects=True)
        if resp.status_code not in (200, 404):
            print(f"[checko] taxes/data curl UA → {resp.status_code}, пробуем SESSION")
            resp = None
    except requests.RequestException as e:
        print(f"[checko] taxes/data curl UA failed: {e}, пробуем SESSION")
        resp = None

    # ── Попытка 2: browser SESSION (резервная) ──────────────────────────────────
    if resp is None:
        try:
            resp = _taxes_get()
        except requests.RequestException as e:
            print(f"[checko] taxes/data request failed: {e}")
            _network_error = True
            return [], []

    if resp is not None and resp.status_code == 429:
        print(f"[checko] taxes/data 429 — пробуем браузерный запрос")
        resp = None

    # ── Попытка 3: браузерный запрос (cookies сессии Checko) ───────────────────
    if resp is None or resp.status_code == 429:
        try:
            page = _get_pw_page()
            print(f"[checko] taxes/data browser request")
            br = page.request.get(taxes_url, headers={"Referer": company_url}, timeout=15_000)
            resp = _PlaywrightResponse(br.text(), taxes_url, br.status)
        except Exception as e:
            print(f"[checko] taxes/data browser request failed: {e}")
            _network_error = True
            return [], []

    if resp.status_code == 200 and _is_captcha_html(resp.text):
        print(f"[checko] ⚠️ КАПЧА на /taxes/data: {taxes_url}")
        _request_blocked = True
        return [], []

    if resp.status_code != 200:
        # 404 = no tax page (госструктуры, иностранные юрлица и т.п.) — норма
        if resp.status_code != 404:
            print(f"[checko] taxes/data → {resp.status_code}")
        return [], []

    soup = BeautifulSoup(resp.text, "html.parser")

    income_tax_amounts: list[int] = []
    taxes_total_amounts: list[int] = []  # Итого за последние 3 года, новый год первым

    def _parse_last3(year_values: list[str]) -> list[int]:
        """Берёт последние 3 столбца (= последние 3 года) и разворачивает: новый год первым."""
        last_3 = year_values[-3:] if len(year_values) >= 3 else year_values
        result = []
        for v in reversed(last_3):
            cleaned = re.sub(r"[^\d]", "", v)
            result.append(int(cleaned) if cleaned else 0)
        return result

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        # First cell of header must contain "Налоги" to identify the right table
        header_cells = rows[0].find_all(["th", "td"])
        if not header_cells or "Налоги" not in header_cells[0].get_text(strip=True):
            continue

        # Scan ALL rows — we need both «Налог на прибыль» and «Итого»
        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            row_name = cells[0].get_text(strip=True)
            year_values = [c.get_text(strip=True) for c in cells[1:]]

            if "Налог на прибыль" in row_name:
                income_tax_amounts = _parse_last3(year_values)

            elif row_name in ("Итого", "ИТОГО") or row_name.startswith("Итого ") or row_name.startswith("ИТОГО "):
                # Те же 3 года что и для налога на прибыль — не только последний год,
                # чтобы не ошибиться на неполном текущем году (например 2025 = 0 за 9 месяцев)
                taxes_total_amounts = _parse_last3(year_values)

        # Found the right table — stop looking
        if income_tax_amounts or taxes_total_amounts:
            break

    return income_tax_amounts, taxes_total_amounts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_company_data_from_url(url: str, inn: str = "", fetch_taxes: bool = True) -> dict:
    """Fetch company data directly from a known Checko URL (skips INN search).

    Args:
        url: Checko company page URL (slug format).
        inn: Optional INN override (if already known).
        fetch_taxes: If True (default), also fetch /taxes/data for authoritative
            income tax breakdown. Pass False to get basic data only (e.g. to
            extract INN quickly before the duplicate check — saves one HTTP request
            for every company that turns out to be a duplicate).
    """
    global _request_blocked, _network_error
    _request_blocked = False
    _network_error = False
    url = url.split("#")[0]  # strip fragment anchors like #taxes
    print(f"[checko] fetching direct URL {url}")
    resp = _get(url)
    if resp is None:
        return {}
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    data = _parse_company_page(text, url)
    data["website"] = _find_website_in_html(resp.text) or data["website"]
    data["emails"]  = _find_emails_in_html(resp.text)

    # If main page has no website/emails — try the /contacts subpage
    if not data["website"] and not data["emails"]:
        print(f"[checko] Сайт/почта не найдены на главной — пробуем /contacts")
        site, emails = _fetch_contacts_section(url)
        if site:
            data["website"] = site
        if emails:
            data["emails"] = emails

    print(f"[checko] Итог: сайт={data['website']!r}  почты={data['emails']}")

    # Use provided INN only if given; otherwise keep what _parse_company_page extracted
    if inn:
        data["inn"] = inn
    # Extract company name from <title>: "ООО РОМАШКА — Чекко"
    title_el = soup.find("title")
    if title_el:
        title_text = title_el.get_text(strip=True)
        for sep in [" — ", " - ", " | "]:
            if sep in title_text:
                title_text = title_text.split(sep)[0].strip()
                break
        if title_text:
            data["name"] = title_text

    if fetch_taxes:
        enrich_with_taxes(data, url)

    return data


def enrich_with_taxes(data: dict, url: str) -> None:
    """Fetch /taxes/data and update data dict in-place with authoritative tax figures.

    Основная страница компании показывает агрегированные/округлённые суммы,
    которые не всегда совпадают с реальными. Страница /taxes/data содержит
    официальные данные ФНС с разбивкой по годам.

    Call this after the duplicate / OKVED check so we avoid the extra HTTP request
    for companies that will be skipped anyway.
    """
    tax_amounts, taxes_totals = _get_income_tax_from_taxes_page(url)
    if tax_amounts:
        data["income_tax_amounts"] = tax_amounts
        data["income_tax_amount"]  = tax_amounts[0]
        data["income_tax_max_3y"]  = max(tax_amounts)
        data["income_tax_exists"]  = any(a > 0 for a in tax_amounts)
        print(f"[checko] taxes/data: налог на прибыль (3 года) = {[f'{a:,}' for a in tax_amounts]}")
    else:
        # Страница /taxes/data недоступна — НЕ используем данные главной страницы,
        # так как они могут показывать неправильные цифры (общие налоги вместо налога на прибыль).
        # Безопаснее считать налог неизвестным → компания не пройдёт квалификацию.
        data["income_tax_amounts"] = []
        data["income_tax_amount"]  = None
        data["income_tax_max_3y"]  = None
        data["income_tax_exists"]  = False
        print("[checko] taxes/data: не удалось получить — данные налога на прибыль сброшены (компания не квалифицируется)")

    # Итого (общая сумма налогов) за последние 3 года — для Критерия 2
    if taxes_totals:
        data["taxes_total_amounts"]  = taxes_totals
        data["taxes_total_from_data"] = taxes_totals[0]   # последний год (для обратной совместимости)
        print(f"[checko] taxes/data: итого (3 года) = {[f'{t:,}' for t in taxes_totals]}")


def _fetch_contacts_section(company_url: str) -> tuple[str, list[str]]:
    """
    Fetch the /contacts subpage of a Checko company page and extract website + emails.
    Returns (website, emails_list). Uses the browser session (cookies intact).
    """
    contacts_url = company_url.rstrip("/") + "/contacts"
    print(f"[checko] Контакты: запрашиваем {contacts_url}")
    try:
        page = _get_pw_page()
        br = page.request.get(contacts_url, headers={"Referer": company_url}, timeout=20_000)
        if not br.ok:
            print(f"[checko] Контакты: HTTP {br.status} — пропускаем")
            return "", []
        html = br.text()
    except Exception as e:
        print(f"[checko] Контакты: ошибка запроса — {e}")
        return "", []

    website = _find_website_in_html(html)
    emails  = _find_emails_in_html(html)
    print(f"[checko] Контакты: сайт={website!r}  почты={emails}")
    return website, emails


def get_company_data(inn: str) -> dict:
    """
    Fetch and return enriched company data from checko.ru for the given INN.

    Keys in returned dict:
        checko_url, revenue, taxes_total, income_tax_exists, income_tax_amount,
        director_name, director_inn, founders (list), website, emails, inn
    """
    global _request_blocked, _network_error
    _request_blocked = False  # сбросить перед каждым поиском
    _network_error = False

    inn = inn.strip()
    if not inn:
        return {}

    print(f"[checko] resolving INN {inn} ...")
    company_url = _resolve_url(inn)
    _pause()

    if company_url is None:
        print(f"[checko] could not resolve URL for INN {inn}")
        return {}

    print(f"[checko] fetching {company_url}")
    resp = _get(company_url)
    _pause()

    if resp is None:
        return {}

    text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
    data = _parse_company_page(text, company_url)

    data["website"] = _find_website_in_html(resp.text) or data["website"]
    data["emails"]  = _find_emails_in_html(resp.text)
    data["inn"]     = inn

    # If main page has no website/emails — try the /contacts subpage
    if not data["website"] and not data["emails"]:
        print(f"[checko] Сайт/почта не найдены на главной — пробуем /contacts")
        site, emails = _fetch_contacts_section(company_url)
        if site:
            data["website"] = site
        if emails:
            data["emails"] = emails

    print(f"[checko] Итог: сайт={data['website']!r}  почты={data['emails']}")

    # Авторитетные данные о налогах из /taxes/data (ФНС)
    # Перезаписывает значения из _parse_company_page, которые ненадёжны
    enrich_with_taxes(data, company_url)

    return data


def get_lpr_contacts(company: dict) -> list[dict]:
    """
    Return all decision-makers from company data as a deduplicated contact list.
    Each item: {"name": str, "role": str, "inn": str, "share_pct": str}
    share_pct is non-empty only for founders (e.g. "50" → display as "50%").
    """
    contacts: list[dict] = []
    seen_names: set[str] = set()

    def _add(name: str, role: str, inn: str, share_pct: str = "") -> None:
        name = name.strip()
        if not name or name in seen_names:
            return
        seen_names.add(name)
        contacts.append({"name": name, "role": role, "inn": inn, "share_pct": share_pct})

    if company.get("director_name"):
        _add(company["director_name"], "Генеральный директор",
             company.get("director_inn", ""))
    for f in company.get("founders", []):
        if not f.get("name"):
            continue
        share_str = f.get("share_pct", "")
        # Берём всех учредителей независимо от доли
        _add(f["name"], "Учредитель", f.get("inn", ""), share_str)

    return contacts


def get_timezone_comment(address: str) -> str:
    """
    Return deal comment indicating hours difference from Moscow based on legal address.
    Moscow timezone (UTC+3) → "мск"
    Ahead of Moscow → "+Xч"
    Behind Moscow → "-Xч"
    """
    a = address.lower()

    # MSK-1 (UTC+2): Калининград
    if "калининград" in a:
        return "-1ч"

    # MSK+1 (UTC+4): Самара, Удмуртия, Ульяновск, Саратов (с 2022 г.)
    if any(x in a for x in ["самарск", "самара", "удмурт", "ульяновск", "саратов"]):
        return "+1ч"

    # MSK+2 (UTC+5): Урал
    if any(x in a for x in [
        "екатеринбург", "свердловск", "тюмен", "курган",
        "челябинск", "оренбург", "пермск", "башкорт",
        "ханты-мансийск", "хмао", "ямало", "янао",
    ]):
        return "+2ч"

    # MSK+3 (UTC+6): Омск
    if "омск" in a:
        return "+3ч"

    # MSK+4 (UTC+7): Новосибирск, Красноярск, Кемерово, Томск, Алтай, Хакасия, Тыва
    if any(x in a for x in [
        "новосибирск", "красноярск", "кемеров", "томск",
        "алтайск", "республика алтай", "алтай", "хакас", "тыва", "тув",
    ]):
        return "+4ч"

    # MSK+5 (UTC+8): Иркутск, Бурятия, Забайкальский
    if any(x in a for x in ["иркутск", "бурят", "забайкальск"]):
        return "+5ч"

    # MSK+6 (UTC+9): Якутия, Амурская
    if any(x in a for x in ["якутск", "якут", "амурск", "амурская"]):
        return "+6ч"

    # MSK+7 (UTC+10): Владивосток, Приморский, Хабаровский, Еврейская АО
    if any(x in a for x in ["владивосток", "приморск", "хабаровск", "еврейская"]):
        return "+7ч"

    # MSK+8 (UTC+11): Магадан, Сахалин
    if any(x in a for x in ["магадан", "сахалин"]):
        return "+8ч"

    # MSK+9 (UTC+12): Камчатка, Чукотка
    if any(x in a for x in ["камчатк", "камчатск", "чукот"]):
        return "+9ч"

    # Default — Moscow timezone
    return "мск"


def enrich_leads(leads: list[dict]) -> list[dict]:
    """Add checko.ru data to each lead that has an INN field."""
    enriched = []
    for lead in leads:
        inn = lead.get("inn", "").strip()
        if inn:
            print(f"[checko] enriching INN {inn} ...")
            company_data = get_company_data(inn)
            lead = {**lead, **company_data}
        enriched.append(lead)
    return enriched
