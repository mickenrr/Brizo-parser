"""brizo.py — Playwright automation for sad1.brizo.ru CRM.

Discovered selectors (from live DOM exploration):
  Login    : https://brizo.ru/cabinet/login
  Cabinet  : https://sad1.brizo.ru/cabinet/
  Deals    : https://sad1.brizo.ru/cabinet/deals
  Deal URL : #deal/{id}/main  (hash-based SPA routing)
  Columns  : .dnd__column  (innerText starts with column name)
  Cards    : a.kanban-card-deal
  Title    : .kanban-card-deal__title
  Resp.    : img.avatar-image[alt="Name"]
  Counters : button.counter  (innerText = count after SVG)
  Stage    : .status-picker__pick (labels for radio buttons)
  Fields   : .base-input__label-text + .base-input__field (input)
"""

import os
import re
import logging
import time
import requests as _rlib
from playwright.sync_api import Page, sync_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [brizo] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BRIZO_URL   = os.getenv("BRIZO_URL", "https://sad1.brizo.ru").rstrip("/")
LOGIN_URL   = f"{BRIZO_URL}/cabinet/login"
MY_EMAIL    = os.getenv("BRIZO_EMAIL", "")
MY_PASSWORD = os.getenv("BRIZO_PASSWORD", "")

RESPONSIBLE  = "Серафим Саровский"
MY_NAME      = ""    # filled after login() via _ensure_user_info()
MY_USER_ID   = 0    # filled after login() via _ensure_user_info()
BASE_COLUMN  = "База"
BASE_STATUS_ID = 157659            # Kanban stage ID for «База»
SKIP_PREFIXES = ("Входящий звонок", "Исходящий звонок")

# Legal entity prefixes — only deals whose title starts with one of these are processed
_LEGAL_PREFIXES = (
    # Сокращённые формы
    "ООО", "АО", "ПАО", "ЗАО", "ОАО", "НАО", "ГУП", "МУП", "ФГУП", "КГУП",
    "НКО", "АНО", "СПК", "КФХ", "СХПК", "НПО", "НПЦ", "ПК", "ПТПК",
    # Полные формы (как Brizo записывает из ЕГРЮЛ)
    "ОБЩЕСТВО", "АКЦИОНЕРНОЕ", "ПУБЛИЧНОЕ", "НЕПУБЛИЧНОЕ",
    "ЗАКРЫТОЕ", "ОТКРЫТОЕ", "ГОСУДАРСТВЕННОЕ", "МУНИЦИПАЛЬНОЕ", "ФЕДЕРАЛЬНОЕ",
    "НЕКОММЕРЧЕСКОЕ", "АВТОНОМНАЯ", "ПРОИЗВОДСТВЕННЫЙ",
)

TIMEOUT = 90_000
LONG    = 240_000
POLL_INTERVAL = 2    # seconds between kanban load checks
POLL_MAX      = 40   # total seconds to wait for kanban

# Module-level state for direct REST API calls
_api_state: dict = {"bearer": "", "user_id": 0, "user_name": ""}

# Cache: field_id (int) → lowercase field name.  Populated on first API use.
_field_map_cache: dict[int, str] = {}

# Brizo main auth endpoint (different from BRIZO_URL subdomain)
_BRIZO_AUTH_URL = "https://brizo.ru/api/auth"

# Field IDs for the deal model (from /api/fields?model_id=1)
FIELD_REJECTION_REASON = 483532   # 'Причина отказа', type=4 (dropdown)

# Option IDs for FIELD_REJECTION_REASON (from /api/fields)
REJECTION_REASON_OPTION_IDS: dict[str, int] = {
    "нет кд":                           28621,
    "нет сайта":                        30500,
    "интересно, но ярд":                30505,
    "интересные, но ярд":               30505,
    "маленькая выручка/мало налогов":   28641,
    "мало налогов":                     28641,
    "не интересно":                     28399,
    "нецелевой":                        28411,
    "нецелевой продукт":                28411,
    "Нецелевой продукт":                28411,
    "не целевой продукт":               28411,
    "интересные, но нет кд":            28829,
    "дистрибьютор":                     28644,
    "дорого":                           28396,
    "дубль":                            30324,
}

# Status IDs from /api/funnels (funnel "САД", id=21480)
STATUS_IDS: dict[str, int] = {
    "База":                       157659,
    "Парсю":                      168873,
    "Пропарсено":                 160321,
    "В работе":                   158678,
    "Первый зум назначен":        157660,
    "Сбор материалов":            157661,
    "Анализ":                     157662,
    "Назначена преза оффера":     157663,
    "Предварительно заинтересован": 157673,
    "Подписание договора":        160635,
    "Долгий ящик":                157677,
    "Выиграно":                   157664,
    "Проиграно":                  157665,
}


def _capture_bearer(page: Page) -> None:
    """Register a request listener that captures the Bearer token from API calls."""
    def _handler(req):
        if "sad1.brizo.ru/api" in req.url:
            auth = req.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                _api_state["bearer"] = auth
    page.on("request", _handler)


def _ensure_bearer() -> str:
    """Return the current Bearer token. If not captured from browser yet,
    fetch it directly via POST /api/auth so REST calls work before login()."""
    if _api_state.get("bearer"):
        return _api_state["bearer"]
    try:
        r = _rlib.post(
            _BRIZO_AUTH_URL,
            json={"email": MY_EMAIL, "password": MY_PASSWORD},
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            token = data.get("token", "")
            if token:
                _api_state["bearer"] = f"Bearer {token}"
                _api_state["user_id"] = data.get("id", 0)
                _api_state["user_name"] = data.get("name", "")
                log.info("Bearer token obtained via REST auth (user: %s id=%s)",
                         _api_state["user_name"], _api_state["user_id"])
    except Exception as e:
        log.warning("REST auth failed: %s", e)
    return _api_state.get("bearer", "")


def _get_api_session() -> _rlib.Session | None:
    bearer = _ensure_bearer()
    if not bearer:
        return None
    s = _rlib.Session()
    s.headers.update({
        "Authorization": bearer,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": f"{BRIZO_URL}/cabinet/deals",
    })
    return s


def _ensure_user_info() -> None:
    """Populate user_id/user_name in _api_state if not yet set.
    Called separately from _ensure_bearer() because when auth goes through
    Playwright the bearer is captured from browser requests but user_id is never
    parsed — _ensure_bearer() skips its POST /api/auth on the early-return path."""
    if _api_state.get("user_id"):
        return
    try:
        r = _rlib.post(
            _BRIZO_AUTH_URL,
            json={"email": MY_EMAIL, "password": MY_PASSWORD},
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            _api_state["user_id"] = data.get("id", 0)
            _api_state["user_name"] = data.get("name", "")
            if not _api_state.get("bearer") and data.get("token"):
                _api_state["bearer"] = f"Bearer {data['token']}"
            log.info("User info fetched: %s (id=%s)",
                     _api_state["user_name"], _api_state["user_id"])
    except Exception as e:
        log.warning("Could not fetch user info from /api/auth: %s", e)


def _get_me() -> tuple[int, str]:
    """Return (user_id, user_name) of the authenticated user.
    Falls back to hardcoded constants only if API call fails."""
    _ensure_user_info()
    uid = _api_state.get("user_id") or MY_USER_ID
    name = _api_state.get("user_name") or MY_NAME
    return (uid, name)


def _fetch_field_map() -> dict[int, str]:
    """Return cached field_id → lowercase_name mapping from /api/fields?model_id=1."""
    global _field_map_cache
    if _field_map_cache:
        return _field_map_cache
    sess = _get_api_session()
    if not sess:
        return {}
    try:
        r = sess.get(f"{BRIZO_URL}/api/fields?model_id=1", timeout=10)
        if r.status_code == 200:
            for f in r.json().get("data", []):
                _field_map_cache[f["id"]] = f["name"].strip().lower()
            log.info("Field map loaded: %d fields", len(_field_map_cache))
    except Exception as e:
        log.warning("Could not load field map: %s", e)
    return _field_map_cache


# Direct field-ID → canonical key mapping for critical deal fields.
# Avoids ambiguity when multiple fields share the same name (e.g. two fields called "ИНН").
_CRITICAL_FIELD_IDS: dict[int, str] = {
    486303: "инн",           # ИНН компании (text)
    483103: "чекко",         # Чекко URL
    483104: "сайт",          # Сайт
    504161: "инн генерального",  # ИНН генерального директора
    494999: "налог на прибыль",
    486302: "выручка",
    504162: "чистая прибыль",
}


def _get_lead_details_via_api(lead_id: str) -> dict:
    """Fetch deal fields via REST API (GET /api/deals/{id}).
    Returns dict with lowercase field names as keys.
    Critical fields (INN, website, Checko) are read by explicit field ID
    so name collisions can never overwrite them.
    """
    sess = _get_api_session()
    if not sess:
        return {}
    field_map = _fetch_field_map()
    try:
        r = sess.get(f"{BRIZO_URL}/api/deals/{lead_id}", timeout=10)
        if r.status_code != 200:
            log.warning("GET /api/deals/%s → %s", lead_id, r.status_code)
            return {}
        deal = r.json()
        result: dict = {}
        result["name"] = deal.get("name", "")
        for f in deal.get("fields", []):
            fid = f["id"]
            val = f.get("value")
            if val is None or val == "":
                continue
            # Critical fields: use explicit ID mapping (highest priority)
            canonical = _CRITICAL_FIELD_IDS.get(fid)
            if canonical:
                result[canonical] = str(val)
                continue
            # Generic fields: use name from field map
            name = field_map.get(fid, "")
            if name and name not in result:  # don't overwrite critical fields
                result[name] = str(val)
        return result
    except Exception as e:
        log.warning("API deal fetch failed for %s: %s", lead_id, e)
        return {}


def _get_or_create_contact_tag(inn: str) -> int | None:
    """Return ID of existing tag named `inn` (model_id=2) or create it.
    Contacts use model_id=2; model_id=3 is a different entity type."""
    sess = _get_api_session()
    if not sess:
        log.warning("No API session — cannot create INN tag")
        return None
    r = sess.get(f"{BRIZO_URL}/api/tags?model_id=2", timeout=10)
    if r.status_code == 200:
        for t in r.json().get("data", []):
            if t.get("name") == inn:
                log.info("Reusing existing tag '%s' id=%s", inn, t["id"])
                return t["id"]
    r2 = sess.post(f"{BRIZO_URL}/api/tags",
                   json={"name": inn, "color_id": 1, "model_id": 2}, timeout=10)
    if r2.status_code in (200, 201):
        tag_id = r2.json().get("id")
        log.info("Created new tag '%s' id=%s", inn, tag_id)
        return tag_id
    log.warning("Failed to create tag '%s': %s %s", inn, r2.status_code, r2.text[:100])
    return None


def _read_current_tag_ids(tags_raw) -> list[int]:
    """Parse tags field from GET /api/contragents/{id} — may be int list or dict list."""
    result = []
    for t in (tags_raw or []):
        if isinstance(t, int):
            result.append(t)
        elif isinstance(t, dict) and t.get("id"):
            result.append(int(t["id"]))
    return result


def _assign_tag_to_contact(contact_id: str | int, tag_id: int) -> bool:
    """Assign tag to contragent via PUT /api/contragents/{id}/tags with {"ids": [...]}."""
    sess = _get_api_session()
    if not sess:
        return False
    # Get current tag IDs so we don't wipe existing tags
    current: list[int] = []
    rg = sess.get(f"{BRIZO_URL}/api/contragents/{contact_id}", timeout=10)
    if rg.status_code == 200:
        current = _read_current_tag_ids(rg.json().get("tags"))
    if tag_id in current:
        log.info("Tag %s already on contragent %s — skip", tag_id, contact_id)
        return True
    all_ids = list(set(current + [tag_id]))
    r = sess.put(f"{BRIZO_URL}/api/contragents/{contact_id}/tags",
                 json={"ids": all_ids}, timeout=10)
    if r.status_code == 200:
        log.info("Tag %s assigned to contragent %s ✓", tag_id, contact_id)
        return True
    log.warning("Tag assign failed: %s %s", r.status_code, r.text[:100])
    return False


def _verify_lead_untouched(lead_id: str) -> tuple[bool, str]:
    """
    API-level guard: verify the deal is still safe to process before touching it.

    Checks (all must pass):
      1. Deal is still in «База» column (status_id == BASE_STATUS_ID)
      2. Responsible is still RESPONSIBLE (Серафим Саровский)
      3. Deal has no comments
      4. Deal has only 1 participant

    Returns (True, "") if all checks pass, (False, reason) otherwise.
    """
    sess = _get_api_session()
    if not sess:
        return (False, "нет API-сессии")

    try:
        # ── 1: still in «База» column ─────────────────────────────────────
        r = sess.get(f"{BRIZO_URL}/api/deals/{lead_id}", timeout=10)
        if r.status_code != 200:
            return (False, f"GET deal → {r.status_code}")
        deal = r.json()

        status_id = deal.get("status_id")
        if status_id != BASE_STATUS_ID:
            return (False, f"сделка уже не в База (status_id={status_id})")

        # ── 2: no comments ────────────────────────────────────────────────
        rc = sess.get(f"{BRIZO_URL}/api/deals/{lead_id}/comments", timeout=10)
        if rc.status_code == 200:
            comments = rc.json()
            items = comments.get("data") if isinstance(comments, dict) else comments
            if isinstance(items, list) and len(items) > 0:
                return (False, f"есть {len(items)} комментари(й/ев)")

        # ── 3: exactly 1 participant = RESPONSIBLE ────────────────────────
        rm = sess.get(f"{BRIZO_URL}/api/deals/{lead_id}/members", timeout=10)
        if rm.status_code == 200:
            members_data = rm.json()
            members_list = members_data.get("data") if isinstance(members_data, dict) else members_data
            if not isinstance(members_list, list):
                members_list = []
            if len(members_list) != 1:
                return (False, f"участников: {len(members_list)} (ожидался 1)")
            resp_name = members_list[0].get("name", "")
            if resp_name != RESPONSIBLE:
                return (False, f"ответственный: «{resp_name}»")

    except Exception as e:
        return (False, f"ошибка проверки: {e}")

    return (True, "")


def _post_deal_comment(lead_id: str, text: str) -> bool:
    """Post a comment to the deal via REST API (reliable alternative to UI approach)."""
    sess = _get_api_session()
    if not sess:
        return False
    r = sess.post(f"{BRIZO_URL}/api/deals/{lead_id}/comments",
                  json={"message": text}, timeout=10)
    if r.status_code in (200, 201):
        log.info("Deal %s: API comment posted ✓", lead_id)
        return True
    log.warning("Deal %s: API comment failed %s %s", lead_id, r.status_code, r.text[:80])
    return False


def add_phones_to_contact(contact_id: str | int, phones: list[str]) -> bool:
    """
    Add phone numbers to an existing Brizo contact via REST API.
    Phones must be normalized strings, e.g. '+71234567890'.
    Returns True if at least one phone was added successfully.
    """
    if not phones or not contact_id:
        return False
    sess = _get_api_session()
    if not sess:
        return False

    # Fetch current phones to avoid duplicates
    rg = sess.get(f"{BRIZO_URL}/api/contragents/{contact_id}", timeout=10)
    existing_phones: set[str] = set()
    if rg.status_code == 200:
        for ph in (rg.json().get("phones") or []):
            v = (ph.get("value") or ph.get("phone") or "").strip()
            if v:
                existing_phones.add(re.sub(r'\D', '', v))

    to_add = [p for p in phones
              if re.sub(r'\D', '', p) not in existing_phones]
    if not to_add:
        log.info("Contact %s: all phones already present — skip", contact_id)
        return True

    # Build updated phones list — Brizo expects [{"phone": "..."}]
    current_list = [
        {"phone": ph.get("phone") or ph.get("value")}
        for ph in (rg.json().get("phones") or [])
        if ph.get("phone") or ph.get("value")
    ] if rg.status_code == 200 else []

    new_list = current_list + [{"phone": p} for p in to_add]

    rp = sess.put(
        f"{BRIZO_URL}/api/contragents/{contact_id}",
        json={"phones": new_list},
        timeout=10,
    )
    if rp.status_code in (200, 201, 204):
        log.info("Contact %s: added phones %s ✓", contact_id, to_add)
        return True
    log.warning("Contact %s: phones PUT failed %s %s",
                contact_id, rp.status_code, rp.text[:200])
    return False


def _post_contact_comment(contact_id: str | int, inn: str) -> bool:
    """Post INN digits as a comment to the contact (skip if already present)."""
    sess = _get_api_session()
    if not sess:
        return False
    inn = inn.strip()
    if not inn:
        return False
    # Dedup: skip if this exact INN is already the text of an existing comment
    rc = sess.get(f"{BRIZO_URL}/api/contragents/{contact_id}/comments",
                  params={"order_by": "desc", "limit": 50}, timeout=10)
    if rc.status_code == 200:
        for c in rc.json().get("data", []):
            if (c.get("message") or "").strip() == inn:
                log.info("INN comment already on contact %s — skip", contact_id)
                return True
    r = sess.post(f"{BRIZO_URL}/api/contragents/{contact_id}/comments",
                  json={"message": inn}, timeout=10)
    if r.status_code in (200, 201):
        log.info("INN comment %s posted to contact %s ✓", inn, contact_id)
        return True
    log.warning("Comment post failed for contact %s: %s %s",
                contact_id, r.status_code, r.text[:100])
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 1. Login
# ─────────────────────────────────────────────────────────────────────────────

def login(page: Page, email: str = MY_EMAIL, password: str = MY_PASSWORD) -> None:
    """Log in to Brizo CRM and wait for the kanban board to load."""
    log.info("Signing in as %s", email)
    _capture_bearer(page)   # start intercepting Bearer token from API requests
    # domcontentloaded avoids waiting for slow 3rd-party scripts and anti-bot hooks
    page.goto(LOGIN_URL, wait_until="commit", timeout=LONG)
    page.wait_for_selector('input[type="email"]', timeout=LONG)

    page.locator('input[type="email"]').fill(email)
    page.locator('input[type="password"]').fill(password)

    # Wait for submit button to be enabled, log its text, then click
    page.wait_for_selector('button[type="submit"]', state='visible', timeout=10_000)
    page.wait_for_selector('button[type="submit"]:not([disabled])', timeout=10_000)
    btn = page.locator('button[type="submit"]')
    try:
        btn_text = btn.text_content(timeout=2_000) or ""
        log.info("Submit button found: «%s»", btn_text.strip())
    except Exception:
        log.info("Submit button found (text unavailable)")
    btn.click()

    # Give the browser a moment then log the URL to confirm something happened
    import time as _time
    _time.sleep(2)
    log.info("URL 2s after click: %s", page.url)
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    log.info("URL after networkidle: %s", page.url)

    log.info("Waiting for post-login navigation...")
    # Detect auth error quickly (wrong credentials) before hitting the full timeout
    try:
        page.wait_for_url(lambda url: "/cabinet/" in url and "login" not in url, timeout=60_000)
    except PlaywrightTimeout:
        # Check if there's a visible error message on the login page
        error_sel = ".error, .alert, [class*='error'], [class*='Error'], [class*='alert']"
        err_el = page.locator(error_sel).first
        try:
            err_text = err_el.text_content(timeout=1_000) or ""
        except Exception:
            err_text = ""
        # Ignore field labels like "Email *" or "Пароль:" — they're not real errors
        import re as _re
        is_field_label = bool(_re.search(r'[\*\:]\s*$', err_text.strip()))
        if err_text.strip() and not is_field_label:
            raise RuntimeError(f"Ошибка входа в Brizo: «{err_text.strip()[:120]}»")
        try:
            page.screenshot(path="/tmp/brizo_login_fail.png")
        except Exception:
            pass
        raise TimeoutError(
            f"Войти в Brizo не удалось за 60с. Проверьте email и пароль. (URL: {page.url})"
        )

    log.info("Logged in at %s", page.url)
    # After login the SPA redirects /cabinet/ → /cabinet/deals which destroys
    # the JS execution context mid-query. Navigate explicitly to /cabinet/deals
    # and wait for domcontentloaded so the context is stable before any DOM call.
    if "/cabinet/deals" not in page.url:
        page.goto(f"{BRIZO_URL}/cabinet/deals", timeout=TIMEOUT, wait_until="domcontentloaded")
    time.sleep(1.5)
    _wait_for_kanban(page)

    # Fetch user ID and name from the API now that we have a valid session
    _ensure_user_info()
    global MY_USER_ID, MY_NAME
    MY_USER_ID = _api_state.get("user_id") or MY_USER_ID
    MY_NAME    = _api_state.get("user_name") or MY_NAME
    log.info("Login complete — user: %s (id=%s)", MY_NAME, MY_USER_ID)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Get new leads from "База" column
# ─────────────────────────────────────────────────────────────────────────────

def _get_base_leads_via_api(max_leads: int = 9999) -> list[dict] | None:
    """
    Fetch qualifying deals from «База» via REST API — no browser scrolling needed.
    Returns list or None if API is unavailable.
    """
    sess = _get_api_session()
    if not sess:
        return None

    results: list[dict] = []
    page_num = 1
    per_page = 200
    MAX_PAGES = 30

    while len(results) < max_leads and page_num <= MAX_PAGES:
        log.info("[api_leads] стр. %d/%d (найдено: %d)...", page_num, MAX_PAGES, len(results))
        try:
            r = sess.get(
                f"{BRIZO_URL}/api/funnels/21480/deals/table",
                params={
                    "filter[status_id][]": BASE_STATUS_ID,
                    "limit": per_page,
                    "page": page_num,
                },
                timeout=30,
            )
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            log.warning("[api_leads] Запрос к API не удался (стр. %d): %s", page_num, e)
            if results:
                log.info("[api_leads] Возвращаем %d найденных лидов несмотря на ошибку", len(results))
                break
            return None

        if page_num == 1:
            meta = body.get("meta") or {}
            overal = meta.get("overal_count", "?")
            statuses = meta.get("count_by_statuses") or {}
            base_info = statuses.get(str(BASE_STATUS_ID)) or {}
            base_count = base_info.get("deals_count") or base_info.get("count") or "?"
            log.info("[api_leads] HTTP %s  overal=%s  база=%s",
                     r.status_code, overal, base_count)
            items_sample = (body.get("data") or [])[:1]
            if items_sample:
                first = items_sample[0]
                log.info("[api_leads] item keys: %s", list(first.keys()))
                log.info("[api_leads] item sample: id=%s name=%s status.name=%r responsible.name=%r members_count=%s",
                         first.get("id"), first.get("name", "")[:30],
                         first.get("status.name"), first.get("responsible.name"),
                         len(first.get("members") or []))

        items = body.get("data") or body.get("items") or body.get("deals") or []
        if not items:
            log.info("[api_leads] Пустой ответ на стр.%d", page_num)
            break

        for item in items:
            # Skip deals not in «База» — confirmed: API returns "База" string for that stage
            if item.get("status.name") not in (None, "База"):
                continue

            name = (item.get("name") or item.get("title") or "").strip()
            if not name:
                continue

            first_word = name.split()[0]
            if first_word not in _LEGAL_PREFIXES:
                continue

            if any(name.startswith(p) for p in SKIP_PREFIXES):
                continue

            # Responsible check: table API has flat "responsible.name" key
            resp_name = item.get("responsible.name") or ""
            members = item.get("members") or []
            if not resp_name:
                resp_name = next(
                    (m.get("name", "") for m in members
                     if m.get("is_responsible") or m.get("is_owner")),
                    "",
                )
            if RESPONSIBLE not in resp_name:
                continue

            if len(members) > 1:
                continue

            deal_id = str(item.get("id") or "")
            if deal_id and deal_id not in {r["id"] for r in results}:
                results.append({"id": deal_id, "name": name, "inn": ""})
                log.info("Qualified (API): id=%s  name=%s", deal_id, name[:50])
                if len(results) >= max_leads:
                    break

        total = body.get("meta", {}).get("overal_count") or 0
        if not items or page_num * per_page >= total:
            break
        page_num += 1

    log.info("[api_leads] Итого найдено: %d лидов (просканировано стр: %d)", len(results), page_num)
    return results


def get_new_leads(page: Page, max_leads: int = 9999) -> list[dict]:
    """
    Return qualifying deals from the "База" kanban column.
    Tries REST API first (no browser needed), falls back to browser scrolling.
    Each dict: {id, name, inn}.
    """
    # Try REST API first — avoids browser scrolling that crashes in Docker/low-memory
    api_leads = _get_base_leads_via_api(max_leads)
    if api_leads is not None and len(api_leads) > 0:
        return api_leads

    if api_leads is not None:
        log.info("API вернул 0 лидов (лиды за пределами %d стр.) — используем браузерный скролл", 30)
    else:
        log.info("API не доступен — используем браузерный скролл")
    log.info("Loading deals board")
    if "/cabinet/deals" not in page.url:
        page.goto(f"{BRIZO_URL}/cabinet/deals", timeout=TIMEOUT, wait_until="domcontentloaded")
    _wait_for_kanban(page)

    seen_ids: set[str] = set()
    leads:    list[dict] = []

    SCROLL_STEP  = 700   # pixels per scroll tick
    MAX_EMPTY    = 6     # consecutive scrolls with no new leads → stop

    _js_collect = f"""() => {{
        const RESPONSIBLE    = "{RESPONSIBLE}";
        const SKIP           = {list(SKIP_PREFIXES)};
        const LEGAL_PREFIXES = {list(_LEGAL_PREFIXES)};

        // Find the «База» column
        let bazaCol = null;
        for (const col of document.querySelectorAll('.dnd__column')) {{
            if (col.innerText.trim().startsWith('{BASE_COLUMN}')) {{
                bazaCol = col; break;
            }}
        }}
        if (!bazaCol) return [];

        const results = [];
        for (const card of bazaCol.querySelectorAll('a.kanban-card-deal')) {{
            const titleEl = card.querySelector('.kanban-card-deal__title');
            const title   = titleEl ? titleEl.innerText.trim() : '';

            // Skip service call cards
            if (SKIP.some(p => title.startsWith(p))) continue;

            // Only legal entities (ООО, АО, ПАО, …)
            const firstWord = title.split(' ')[0];
            if (!LEGAL_PREFIXES.includes(firstWord)) continue;

            // Responsible must be RESPONSIBLE
            const respImg = card.querySelector('img.avatar-image[alt="' + RESPONSIBLE + '"]');
            if (!respImg) continue;

            // No other participants — only 1 avatar
            if (card.querySelectorAll('img.avatar-image').length > 1) continue;

            // All activity counters must be 0 (no comments, tasks etc.)
            let hasActivity = false;
            for (const c of card.querySelectorAll('button.counter')) {{
                const t = c.innerText.trim();
                if (t && t !== '0') {{ hasActivity = true; break; }}
            }}
            if (hasActivity) continue;

            const href = card.getAttribute('href') || '';
            const m    = href.match(/deal\\/(\\d+)/);
            if (!m) continue;

            results.push({{ id: m[1], name: title }});
        }}
        return results;
    }}"""

    _js_scroll = f"""(step) => {{
        // Find the scrollable container inside the База column
        function findScroll(el) {{
            const s = window.getComputedStyle(el);
            if ((s.overflowY === 'auto' || s.overflowY === 'scroll') &&
                    el.scrollHeight > el.clientHeight + 10) {{
                return el;
            }}
            for (const child of el.children) {{
                const found = findScroll(child);
                if (found) return found;
            }}
            return null;
        }}
        for (const col of document.querySelectorAll('.dnd__column')) {{
            if (col.innerText.trim().startsWith('{BASE_COLUMN}')) {{
                const sc = findScroll(col) || col;
                const before = sc.scrollTop;
                sc.scrollTop += step;
                return {{ scrolled: sc.scrollTop - before, scrollTop: sc.scrollTop }};
            }}
        }}
        return null;
    }}"""

    no_scroll_count = 0   # сколько раз подряд колонка не прокрутилась (достигли дна)
    MAX_NO_SCROLL   = 3   # стоп только если 3 раза подряд прокрутка == 0px
    log.info("Scrolling «%s» column to collect all qualifying leads…", BASE_COLUMN)

    while no_scroll_count < MAX_NO_SCROLL and len(leads) < max_leads:
        batch = page.evaluate(_js_collect)
        for item in batch:
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                leads.append({"id": item["id"], "name": item["name"], "inn": ""})
                log.info("Qualified: id=%s  name=%s", item["id"], item["name"][:50])
                if len(leads) >= max_leads:
                    break

        scroll_result = page.evaluate(_js_scroll, SCROLL_STEP)
        scrolled = (scroll_result or {}).get("scrolled", 0)
        log.debug("Scroll: moved %dpx, qualified so far: %d", scrolled, len(leads))

        if scrolled == 0:
            no_scroll_count += 1
        else:
            no_scroll_count = 0

        time.sleep(0.8)   # wait for virtual DOM to re-render new cards

    log.info("Qualifying leads found: %d", len(leads))

    # Диагностика: если ничего не нашли — покажем первые карточки и причины отказа
    if len(leads) == 0:
        debug_js = f"""() => {{
            const RESPONSIBLE    = "{RESPONSIBLE}";
            const SKIP           = {list(SKIP_PREFIXES)};
            const LEGAL_PREFIXES = {list(_LEGAL_PREFIXES)};
            let bazaCol = null;
            for (const col of document.querySelectorAll('.dnd__column')) {{
                if (col.innerText.trim().startsWith('{BASE_COLUMN}')) {{
                    bazaCol = col; break;
                }}
            }}
            if (!bazaCol) return [{{"error": "База column not found"}}];
            const cards = Array.from(bazaCol.querySelectorAll('a.kanban-card-deal'));
            const result = [];
            for (const card of cards.slice(0, 8)) {{
                const titleEl = card.querySelector('.kanban-card-deal__title');
                const title   = titleEl ? titleEl.innerText.trim() : '(no title)';
                const firstWord = title.split(' ')[0];
                const respImg = card.querySelector('img.avatar-image[alt="' + RESPONSIBLE + '"]');
                const avatars = card.querySelectorAll('img.avatar-image').length;
                const counters = Array.from(card.querySelectorAll('button.counter')).map(c => c.innerText.trim());
                result.push({{
                    title: title.substring(0, 50),
                    skip: SKIP.some(p => title.startsWith(p)),
                    legal: LEGAL_PREFIXES.includes(firstWord),
                    hasResp: !!respImg,
                    avatars: avatars,
                    counters: counters,
                }});
            }}
            return result;
        }}"""
        try:
            debug_info = page.evaluate(debug_js)
            log.info("[DEBUG] Первые карточки в «База»: %s", debug_info)
        except Exception as e:
            log.info("[DEBUG] Ошибка диагностики: %s", e)

    return leads


# ─────────────────────────────────────────────────────────────────────────────
# 3. Get lead details
# ─────────────────────────────────────────────────────────────────────────────

def get_lead_details(page: Page, lead_id: str) -> dict:
    """
    Fetch deal custom fields via REST API (fast, no popup needed).
    Falls back to DOM scraping if API returns nothing.
    Returns dict with keys: id, name, inn, inn_director, + any other fields.
    """
    log.info("Loading deal %s", lead_id)
    data: dict = {"id": lead_id}

    # Primary: REST API (reliable, no DOM rendering required)
    api_data = _get_lead_details_via_api(lead_id)
    if api_data:
        data.update(api_data)
        log.info("Deal %s — %d fields (via API)", lead_id, len(data))
    else:
        # Fallback: DOM scraping (slower, depends on popup rendering)
        log.warning("Deal %s: API returned nothing — falling back to DOM", lead_id)
        _open_deal(page, lead_id)
        title_el = page.query_selector(".editable-area.deal__name")
        if title_el:
            data["name"] = title_el.input_value() or title_el.inner_text().strip()
        data.update(_extract_all_fields(page))
        log.info("Deal %s — %d fields (via DOM)", lead_id, len(data))

    # Canonical keys for main.py / checko.py compatibility
    for src, dst in [
        ("инн",                   "inn"),
        ("фио контактное лицо",   "contact_name"),
        ("инн генерального",      "inn_director"),
        ("чекко",                 "checko_url_field"),
        ("сайт",                  "website_field"),
        ("корпоративная почта",   "email_field"),
        ("чистая прибыль",        "net_profit"),
    ]:
        if src in data and dst not in data:
            data[dst] = data[src]

    data.setdefault("inn", data.get("инн", ""))
    return data


# ─────────────────────────────────────────────────────────────────────────────
# 4. Assign current user as responsible
# ─────────────────────────────────────────────────────────────────────────────

def assign_to_me(page: Page, lead_id: str) -> None:
    """
    Set Ника Серафимова as responsible via Brizo REST API.

    Flow:
      1. POST /api/deals/{id}/members {"ids": [MY_USER_ID]}  — add as participant
      2. PATCH /api/deals/{id}/responsible {"id": MY_USER_ID} — set as responsible

    Requires Bearer token from _api_state (captured from live browser requests on login).
    """
    user_id, user_name = _get_me()
    log.info("Assigning %s (id=%s) as responsible for deal %s", user_name, user_id, lead_id)

    sess = _get_api_session()
    if not sess:
        log.warning("Deal %s: no API session yet — responsible not set", lead_id)
        return

    # Step 1: add as participant (422 if already a member — that's fine)
    r1 = sess.post(
        f"{BRIZO_URL}/api/deals/{lead_id}/members",
        json={"ids": [user_id]},
        timeout=10,
    )
    log.info("Deal %s: POST /members → %s", lead_id, r1.status_code)

    # Step 2: set as responsible
    r2 = sess.patch(
        f"{BRIZO_URL}/api/deals/{lead_id}/responsible",
        json={"id": user_id},
        timeout=10,
    )
    if r2.status_code == 200:
        log.info("Deal %s: responsible set to %s ✓", lead_id, user_name)
    else:
        log.warning(
            "Deal %s: PATCH /responsible → %s: %s",
            lead_id, r2.status_code, r2.text[:120],
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Move to stage
# ─────────────────────────────────────────────────────────────────────────────

def move_to_stage(page: Page, lead_id: str, stage_name: str) -> None:
    """Move deal to stage_name — REST API first, UI fallback."""
    log.info("Moving deal %s → '%s'", lead_id, stage_name)

    status_id = STATUS_IDS.get(stage_name)
    if status_id:
        sess = _get_api_session()
        if sess:
            r = sess.patch(f"{BRIZO_URL}/api/deals/{lead_id}/status",
                           json={"status_id": status_id}, timeout=10)
            log.info("PATCH /status → %s", r.status_code)
            # Verify
            rv = sess.get(f"{BRIZO_URL}/api/deals/{lead_id}", timeout=10)
            if rv.status_code == 200:
                actual = rv.json().get("status_id")
                if actual == status_id:
                    log.info("Deal %s → '%s' confirmed via API ✓", lead_id, stage_name)
                    return
                log.warning("Deal %s: status_id=%s after PATCH (expected %s) — trying UI", lead_id, actual, status_id)

    # UI fallback — open deal and click radio
    _open_deal(page, lead_id)
    result = page.evaluate(
        """(stageName) => {
            const picks = document.querySelectorAll('.status-picker__pick');
            for (const pick of picks) {
                if (pick.innerText.trim() === stageName) {
                    let el = pick.parentElement;
                    while (el && el !== document.body) {
                        const radio = el.querySelector('input[type="radio"].status-picker__radio');
                        if (radio) {
                            radio.click();
                            radio.dispatchEvent(new Event('change', {bubbles: true}));
                            return true;
                        }
                        el = el.parentElement;
                    }
                    pick.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                    return true;
                }
            }
            return false;
        }""",
        stage_name,
    )
    if result:
        time.sleep(1)
        log.info("Deal %s moved to '%s' (UI)", lead_id, stage_name)
    else:
        log.error("Stage '%s' not found for deal %s", stage_name, lead_id)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Reject lead (move to Проиграно via REST API + reason as comment)
# ─────────────────────────────────────────────────────────────────────────────

_MODAL_SEL = ".required-fields-modal, .modal.required-fields-modal"

# Map reason strings → Brizo dropdown option text (for UI fallback)
_REASON_DROPDOWN_MAP = {
    "нет кд":                           "Нет КД",
    "нет сайта":                        "Нет сайта",
    "интересные, но ярд":               "Интересно, но ярд",
    "интересно, но ярд":                "Интересно, но ярд",
    "маленькая выручка/мало налогов":   "Маленькая выручка/мало налогов",
    "мало налогов":                     "Маленькая выручка/мало налогов",
    "нецелевой":                        "Не целевой продукт",
    "дистрибьютор":                     "Дистрибьютор",
}


def reject_lead(page: Page, lead_id: str, reason: str) -> None:
    """Move deal to 'Проиграно' and set rejection reason via REST API.

    1. PATCH /api/deals/{id}/status {"status_id": 157665}
    2. PATCH /api/deals/{id}/fields [{"id": 483532, "value": <option_id>}]
    3. POST comment for audit trail
    Falls back to UI modal only if REST fails.
    """
    log.info("Отклоняем сделку %s: '%s'", lead_id, reason)
    proig_id = STATUS_IDS["Проиграно"]  # 157665

    sess = _get_api_session()
    api_ok = False
    if sess:
        # Step 1: set Причина отказа FIRST (required before moving to Проиграно)
        option_id = REJECTION_REASON_OPTION_IDS.get(reason.lower())
        if option_id:
            rf = sess.patch(f"{BRIZO_URL}/api/deals/{lead_id}/fields",
                            json=[{"id": FIELD_REJECTION_REASON, "value": option_id}],
                            timeout=10)
            log.info("PATCH /fields (Причина отказа=%s) → %s", option_id, rf.status_code)
        else:
            log.warning("No option_id for reason '%s'", reason)

        # Step 2: change stage to Проиграно (now that field is set)
        r = sess.patch(f"{BRIZO_URL}/api/deals/{lead_id}/status",
                       json={"status_id": proig_id}, timeout=10)
        log.info("PATCH /status → %s", r.status_code)

        # Verify
        rv = sess.get(f"{BRIZO_URL}/api/deals/{lead_id}", timeout=10)
        if rv.status_code == 200 and rv.json().get("status_id") == proig_id:
            log.info("Deal %s → Проиграно confirmed ✓", lead_id)
            api_ok = True
        else:
            actual = rv.json().get("status_id") if rv.status_code == 200 else "?"
            log.warning("Deal %s: status_id=%s (expected %s)", lead_id, actual, proig_id)

    if api_ok:
        return

    # UI fallback (if REST failed)
    log.info("Trying UI fallback for deal %s", lead_id)
    _open_deal(page, lead_id)

    modal_visible = False
    try:
        modal_visible = page.locator(_MODAL_SEL).first.is_visible()
    except Exception:
        pass

    if not modal_visible:
        result = page.evaluate("""() => {
            const picks = document.querySelectorAll('.status-picker__pick');
            for (const pick of picks) {
                if (pick.innerText.trim() === 'Проиграно') {
                    let el = pick.parentElement;
                    while (el && el !== document.body) {
                        const radio = el.querySelector('input[type="radio"].status-picker__radio');
                        if (radio) {
                            radio.click();
                            radio.dispatchEvent(new Event('change', {bubbles: true}));
                            return true;
                        }
                        el = el.parentElement;
                    }
                    pick.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    return true;
                }
            }
            return false;
        }""")
        if not result:
            log.error("UI fallback: Проиграно radio not found for deal %s", lead_id)
            return
        log.info("UI: clicked Проиграно radio for deal %s", lead_id)
        try:
            page.wait_for_selector(_MODAL_SEL, state="visible", timeout=8_000)
        except PlaywrightTimeout:
            log.warning("UI: no required-fields modal for deal %s", lead_id)
            return

    modal_loc = page.locator(_MODAL_SEL).first
    try:
        toggle_loc = modal_loc.locator(".base-select__toggle-btn, .base-input__right-btn").first
        toggle_loc.wait_for(state="visible", timeout=5_000)
        toggle_loc.click()
        time.sleep(2)
        options_info = page.evaluate("""() => {
            for (const list of document.querySelectorAll('.base-dropdown__list')) {
                if (!list.offsetParent) continue;
                const items = list.querySelectorAll('.base-dropdown__list-item-text');
                if (items.length > 0) {
                    return Array.from(items).map(el => {
                        const r = el.getBoundingClientRect();
                        return {text: el.innerText.trim(), x: r.left + r.width/2, y: r.top + r.height/2};
                    });
                }
            }
            return [];
        }""")
        if options_info:
            reason_lower = reason.lower()
            dropdown_text = _REASON_DROPDOWN_MAP.get(reason_lower)
            target = (
                next((o for o in options_info if dropdown_text and dropdown_text.lower() in o["text"].lower()), None)
                or next((o for o in options_info if o["text"].lower() in reason_lower or reason_lower in o["text"].lower()), None)
                or options_info[0]
            )
            page.mouse.click(target["x"], target["y"])
            log.info("UI: selected '%s' for deal %s", target["text"], lead_id)
            time.sleep(1)
    except Exception as e:
        log.warning("UI: could not set reason for deal %s: %s", lead_id, e)

    try:
        apply_btn = modal_loc.locator("button.base-button_primary, button.base-button_blue").first
        apply_btn.wait_for(state="visible", timeout=5_000)
        apply_btn.click(timeout=5_000)
        log.info("UI: clicked Применить for deal %s", lead_id)
        page.wait_for_selector(_MODAL_SEL, state="hidden", timeout=12_000)
        log.info("UI: modal closed for deal %s", lead_id)
    except Exception as e:
        log.warning("UI: Применить/close error for deal %s: %s", lead_id, e)


# ─────────────────────────────────────────────────────────────────────────────
# Legacy wrappers kept for any direct callers
# ─────────────────────────────────────────────────────────────────────────────

def set_rejection_reason(page: Page, lead_id: str, reason: str) -> None:
    """Deprecated: use reject_lead() which handles both the stage change and the modal."""
    reject_lead(page, lead_id, reason)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Add comment
# ─────────────────────────────────────────────────────────────────────────────

def add_comment(page: Page, lead_id: str, text: str) -> None:
    """Post a text comment on the deal. Tries REST API first, falls back to UI."""
    log.info("Adding comment to deal %s: %s", lead_id, text[:60])
    if _post_deal_comment(lead_id, text):
        return  # API succeeded — no UI automation needed
    _open_deal(page, lead_id)

    # Click the "Комментарии" tab — abort if not found to avoid writing to wrong field
    tab_coords = page.evaluate("""() => {
        for (const el of document.querySelectorAll('a, button, li, div')) {
            const t = el.innerText?.trim() || '';
            if (t === 'Комментарии' || t === 'Комментарий') {
                if (!el.offsetParent) continue;
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.top > 0 && r.top < 200) {
                    return {x: r.left + r.width/2, y: r.top + r.height/2};
                }
            }
        }
        return null;
    }""")
    if not tab_coords:
        log.error("Deal %s: 'Комментарии' tab not found — aborting to avoid writing to deal title", lead_id)
        return

    page.mouse.click(tab_coords["x"], tab_coords["y"])
    time.sleep(1.5)

    # Find comment input — scoped to y > 300 to avoid the deal title area at the top.
    # Brizo uses div[contenteditable="true"] for the comment box (not a plain textarea),
    # so we check both, but only accept elements well below the tab headers.
    inp_info = page.evaluate("""() => {
        const selectors = [
            '.comment-input__field',
            'textarea.comment__textarea',
            'textarea',
            'div[contenteditable="true"]',
        ];
        for (const sel of selectors) {
            for (const el of document.querySelectorAll(sel)) {
                if (!el.offsetParent) continue;
                const r = el.getBoundingClientRect();
                if (r.width > 100 && r.height > 10 && r.top > 300) {
                    return {
                        x: r.left + r.width / 2,
                        y: r.top + r.height / 2,
                        sel: sel,
                    };
                }
            }
        }
        return null;
    }""")

    if not inp_info:
        log.error("Deal %s: comment input not found after tab click (nothing at y>300)", lead_id)
        return

    log.info("Deal %s: comment input found via '%s' at y=%.0f", lead_id, inp_info["sel"], inp_info["y"])

    try:
        page.mouse.click(inp_info["x"], inp_info["y"])
        time.sleep(0.3)
        # keyboard.type works for both <textarea> and div[contenteditable]
        page.keyboard.type(text)
        time.sleep(0.3)
        # Ctrl+Enter submits the comment without touching any deal save button
        page.keyboard.press("Control+Enter")
        time.sleep(1)
        log.info("Deal %s: comment posted ✓", lead_id)
    except Exception as e:
        log.error("Deal %s: could not post comment: %s", lead_id, e)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Fill Checko field
# ─────────────────────────────────────────────────────────────────────────────

_FIELD_CHECKO = 483103   # 'Чекко', type=5 (URL)


def fill_contact_details(lead_id: str, website: str = "", email: str = "") -> None:
    """Fill Сайт and/or Корпоративная почта fields via REST API for an existing deal."""
    fields = []
    if website:
        fields.append({"id": _FIELD_SITE,       "value": website})
    if email:
        fields.append({"id": _FIELD_CORP_EMAIL,  "value": email})
    if not fields:
        return
    sess = _get_api_session()
    if not sess:
        log.warning("fill_contact_details: no API session")
        return
    r = sess.patch(f"{BRIZO_URL}/api/deals/{lead_id}/fields", json=fields, timeout=10)
    if r.status_code in (200, 201):
        log.info("Сайт/почта записаны в Brizo для сделки %s ✓", lead_id)
    else:
        log.warning("fill_contact_details failed %s — %s", r.status_code, r.text[:200])


def fill_checko_field(page: Page, lead_id: str, url: str) -> None:
    """Fill the 'Чекко' URL field via REST API (no browser popup needed)."""
    log.info("Filling Чекко field for deal %s: %s", lead_id, url)
    sess = _get_api_session()
    if sess:
        r = sess.patch(f"{BRIZO_URL}/api/deals/{lead_id}/fields",
                       json=[{"id": _FIELD_CHECKO, "value": url}], timeout=10)
        if r.status_code in (200, 201):
            log.info("Чекко field set via REST ✓")
            return
        log.warning("REST fill_checko failed %s — falling back to UI", r.status_code)

    # UI fallback
    _open_deal(page, lead_id)
    inp = _find_field_input(page, "Чекко")
    if inp:
        inp.click()
        time.sleep(0.3)
        inp.fill(url)
        page.keyboard.press("Tab")
        time.sleep(0.5)
        log.info("Чекко field filled via UI")
    else:
        log.error("Чекко field not found for deal %s", lead_id)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Add contact
# ─────────────────────────────────────────────────────────────────────────────

def add_lpr_contacts(
    page: Page, lead_id: str, contacts: list[dict]
) -> list[dict]:
    """
    Add each LPR as a separate contact in the deal via the 'Контакты' rich-select.

    Returns list of {"contact_id": str, "inn": str, "name": str} for each
    successfully created contact (contact_id may be "" if capture failed).

    Flow per contact:
      1. Click 'Контакты' field → dropdown opens with existing contacts list
      2. Click '+ Создать контакт' footer button → contact creation form appears
      3. Find 'Имя' / first-name input in the form, fill it with the full name
      4. Click 'Сохранить' / 'Создать' in the form
    """
    created: list[dict] = []
    if not contacts:
        log.info("Deal %s: no LPR contacts to add", lead_id)
        return

    log.info("Deal %s: adding %d LPR contacts: %s",
             lead_id, len(contacts), [c["name"] for c in contacts])
    _open_deal(page, lead_id, force=True)  # force re-open after stage change
    time.sleep(3.5)  # wait for popup to fully render

    # ── Find 'Контакты' input and scroll into view ────────────────────────────
    # Retry up to 6 times with increasing delay — popup sometimes renders slowly.
    field_coords = None
    for _attempt in range(6):
        field_coords = page.evaluate("""() => {
            for (const lbl of document.querySelectorAll('.base-input__label-text')) {
                if (lbl.innerText.trim().toLowerCase() !== 'контакты') continue;
                lbl.scrollIntoView({behavior: 'instant', block: 'center'});
                const container = lbl.closest('.base-input');
                if (!container) return null;
                const r = container.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) return null;
                return {x: Math.round(r.left + r.width / 2),
                        y: Math.round(r.top  + r.height / 2)};
            }
            return null;
        }""")
        if field_coords and field_coords.get("x", 0) > 10:
            break
        log.debug("Deal %s: Контакты not found (attempt %d/6), waiting...", lead_id, _attempt + 1)
        time.sleep(1.5)

    if not field_coords or field_coords.get("x", 0) <= 10:
        # Last resort: reload the deal and try once more
        log.warning("Deal %s: Контакты not found after 6 attempts — reloading", lead_id)
        _open_deal(page, lead_id, force=True)
        time.sleep(5.0)
        field_coords = page.evaluate("""() => {
            for (const lbl of document.querySelectorAll('.base-input__label-text')) {
                if (lbl.innerText.trim().toLowerCase() !== 'контакты') continue;
                lbl.scrollIntoView({behavior: 'instant', block: 'center'});
                const container = lbl.closest('.base-input');
                if (!container) return null;
                const r = container.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) return null;
                return {x: Math.round(r.left + r.width / 2),
                        y: Math.round(r.top  + r.height / 2)};
            }
            return null;
        }""")
        if not field_coords or field_coords.get("x", 0) <= 10:
            log.error("Deal %s: 'Контакты' field not found — contacts skipped", lead_id)
            return

    time.sleep(0.6)
    log.info("Deal %s: Контакты at x=%d y=%d", lead_id,
             field_coords["x"], field_coords["y"])

    # ── Intercept POST /api/contragents to capture created contact IDs ────────
    # Each new contact creation triggers a POST; we read the returned ID
    # and immediately post the INN as a comment right after the form is saved.
    _cap: dict = {"last_id": None}

    def _on_contragent_response(resp):
        url = resp.url
        if ("/api/contragents" in url
                and resp.request.method == "POST"
                and "/comments" not in url
                and "/tags" not in url):
            try:
                body = resp.json()
                cid = (str(body.get("id") or "")
                       or str((body.get("data") or {}).get("id") or ""))
                if cid:
                    _cap["last_id"] = cid
            except Exception:
                pass

    page.on("response", _on_contragent_response)

    # ── Add each contact ───────────────────────────────────────────────────────
    for contact in contacts:
        name = contact["name"]
        inn_val = contact.get("inn", "").strip()
        _cap["last_id"] = None  # reset before each new contact
        log.info("Deal %s: adding contact '%s'", lead_id, name)

        # 1. Open the Contacts dropdown by JS-clicking the <input> inside the field.
        #    Scroll into view first (may have scrolled away after previous contact save).
        #    Clicking the container center would open an existing chip's detail view.
        page.evaluate("""() => {
            for (const lbl of document.querySelectorAll('.base-input__label-text')) {
                if (lbl.innerText.trim().toLowerCase() !== 'контакты') continue;
                lbl.scrollIntoView({behavior: 'instant', block: 'center'});
                const inp = lbl.closest('.base-input')?.querySelector('input');
                if (inp) { inp.click(); inp.focus(); }
                return;
            }
        }""")
        time.sleep(1.0)

        # 2. Find '+ Создать контакт' button in the dropdown footer
        create_btn = page.evaluate("""() => {
            // Primary: dedicated create button
            for (const el of document.querySelectorAll(
                    '.base-select__create-btn, [class*="create-btn"]')) {
                const r = el.getBoundingClientRect();
                if (r.width > 5 && r.height > 5)
                    return {x: Math.round(r.left + r.width/2),
                            y: Math.round(r.top  + r.height/2),
                            cls: (el.className||'').slice(0,60)};
            }
            // Fallback: any visible element with text 'Создать контакт'
            for (const el of document.querySelectorAll('*')) {
                const cls = (el.className||'').toString();
                if (cls.includes('context-menu')) continue;
                const txt = (el.innerText||'').trim();
                if (txt !== 'Создать контакт') continue;
                const r = el.getBoundingClientRect();
                if (r.width > 5 && r.height > 5)
                    return {x: Math.round(r.left + r.width/2),
                            y: Math.round(r.top  + r.height/2),
                            cls: cls.slice(0,60)};
            }
            return null;
        }""")

        if not create_btn:
            # Retry: re-open dropdown and try again
            log.warning("Deal %s: '+ Создать контакт' not found — retrying", lead_id)
            page.keyboard.press("Escape")
            time.sleep(1.0)
            page.evaluate("""() => {
                for (const lbl of document.querySelectorAll('.base-input__label-text')) {
                    if (lbl.innerText.trim().toLowerCase() !== 'контакты') continue;
                    lbl.scrollIntoView({behavior: 'instant', block: 'center'});
                    const inp = lbl.closest('.base-input')?.querySelector('input');
                    if (inp) { inp.click(); inp.focus(); }
                    return;
                }
            }""")
            time.sleep(1.5)
            create_btn = page.evaluate("""() => {
                for (const el of document.querySelectorAll(
                        '.base-select__create-btn, [class*="create-btn"]')) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 5 && r.height > 5)
                        return {x: Math.round(r.left + r.width/2),
                                y: Math.round(r.top  + r.height/2)};
                }
                for (const el of document.querySelectorAll('*')) {
                    if ((el.className||'').toString().includes('context-menu')) continue;
                    if ((el.innerText||'').trim() === 'Создать контакт') {
                        const r = el.getBoundingClientRect();
                        if (r.width > 5 && r.height > 5)
                            return {x: Math.round(r.left + r.width/2),
                                    y: Math.round(r.top  + r.height/2)};
                    }
                }
                return null;
            }""")
        if not create_btn:
            log.error("Deal %s: '+ Создать контакт' not found after retry — skipping '%s'",
                      lead_id, name)
            page.keyboard.press("Escape")
            continue

        log.info("Deal %s: clicking '+ Создать контакт' at x=%d y=%d",
                 lead_id, create_btn["x"], create_btn["y"])
        page.mouse.click(create_btn["x"], create_btn["y"])
        time.sleep(1.5)  # wait for form; Имя и фамилия field auto-focuses

        # 3. Build display name (abbreviated role suffix goes into the name field only).
        # Должность field is intentionally left empty — it's an autocomplete that only
        # accepts values from Brizo's own list; free-form text causes the form to be
        # discarded on save. The abbreviated role in the name is sufficient.
        raw_role  = contact.get("role", "")
        share_pct = contact.get("share_pct", "")
        if "генеральный директор" in raw_role.lower():
            display_name = f"{name} гендир"
        elif "учредитель" in raw_role.lower():
            if share_pct:
                display_name = f"{name} учред {share_pct}%"
            else:
                display_name = f"{name} учред"
        else:
            display_name = name

        # Fill 'Имя и фамилия' with the display name (includes abbreviated role suffix).
        try:
            name_loc = page.locator("textarea.contact__name").first
            name_loc.wait_for(state="visible", timeout=5_000)
            name_loc.fill(display_name)
            time.sleep(0.3)
            log.info("Deal %s: name '%s' filled", lead_id, display_name)
        except Exception as e:
            log.warning("Deal %s: name fill failed (%s) — skipping '%s'",
                        lead_id, e, name)
            page.keyboard.press("Escape")
            continue

        # Save: click to the LEFT of the Контакты field.
        # Dropdowns in Brizo open rightward; clicking to the left of the field
        # is guaranteed to land OUTSIDE the dropdown and triggers Vue's save.
        left_x = max(30, field_coords["x"] - 250)
        safe_y  = max(200, field_coords["y"] - 250)
        page.mouse.click(left_x, safe_y)
        time.sleep(0.5)

        # Wait up to 6 s for the form to disappear (DOM signal that save completed).
        try:
            page.locator("textarea.contact__name").first.wait_for(
                state="hidden", timeout=6_000
            )
        except Exception:
            page.mouse.click(max(30, left_x - 100), max(100, safe_y - 100))
            time.sleep(0.3)
            try:
                page.locator("textarea.contact__name").first.wait_for(
                    state="hidden", timeout=4_000
                )
            except Exception:
                pass

        time.sleep(0.8)  # let the backend link the contact to the deal

        # Post INN comment using the contragent ID captured from the POST response
        if inn_val:
            if _cap["last_id"]:
                _post_contact_comment(_cap["last_id"], inn_val)
                log.info("Deal %s: INN %s → contact %s ✓", lead_id, inn_val, _cap["last_id"])
            else:
                log.warning("Deal %s: '%s' — contragent ID not captured, INN skipped",
                            lead_id, name)

        created.append({
            "contact_id": _cap["last_id"] or "",
            "inn":        inn_val,
            "name":       name,
        })
        log.info("Deal %s: contact '%s' submitted ✓", lead_id, name)

    page.remove_listener("response", _on_contragent_response)
    log.info("Deal %s: LPR contacts done ✓", lead_id)
    return created


def add_contact(page: Page, lead_id: str, name: str, inn: str, role: str) -> None:
    """
    Write contact to the deal:
      "ФИО контактное лицо" ← "name role"
      "Метки"               ← inn  (appended as a tag)
    If multiple contacts are needed, subsequent calls overwrite the name field
    and append to Метки.
    """
    full_name = f"{name} {role}".strip()
    log.info("Adding contact to deal %s: %s (INN %s)", lead_id, full_name, inn)
    _open_deal(page, lead_id)

    # Write name
    name_inp = _find_field_input(page, "ФИО контактное лицо")
    if name_inp:
        name_inp.click()
        time.sleep(0.2)
        name_inp.fill(full_name)
        page.keyboard.press("Tab")
        time.sleep(0.5)
    else:
        log.warning("'ФИО контактное лицо' field not found for deal %s", lead_id)

    # Write INN to Метки (tags input — type + Enter to add tag)
    if inn:
        tags_inp = _find_field_input(page, "Метки")
        if tags_inp:
            tags_inp.click()
            time.sleep(0.3)
            tags_inp.type(inn)
            page.keyboard.press("Enter")
            time.sleep(0.5)
        else:
            log.warning("'Метки' field not found for deal %s", lead_id)

    log.info("Contact '%s' added to deal %s", full_name, lead_id)


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _wait_for_kanban(page: Page) -> None:
    """Poll until at least one kanban card is present."""
    # Wait for any in-flight navigation to settle before touching the DOM.
    # SPA frameworks (Vue.js) often do a secondary redirect after login which
    # destroys the JS execution context mid-query_selector.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass

    deadline = time.time() + POLL_MAX
    while time.time() < deadline:
        try:
            if page.query_selector("a.kanban-card-deal"):
                return
        except Exception as exc:
            # "Execution context was destroyed" — navigation still in progress
            log.debug("_wait_for_kanban: navigation mid-query, retrying (%s)", exc)
            time.sleep(1)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
        time.sleep(POLL_INTERVAL)
    log.error("Kanban did not load in %ds", POLL_MAX)


def _open_deal(page: Page, lead_id: str, force: bool = False) -> None:
    """Navigate to deal main tab. Uses hash-based SPA routing."""
    target_hash = f"deal/{lead_id}/main"
    if force or target_hash not in page.url:
        # Go via the deals board first when switching between deals.
        # Direct hash-to-hash navigation leaves the previous popup in the DOM
        # during the Vue.js transition, causing stale-popup overlap bugs.
        def _navigate() -> None:
            if "deal/" in page.url:
                page.goto(f"{BRIZO_URL}/cabinet/deals", timeout=TIMEOUT, wait_until="domcontentloaded")
                time.sleep(1.5)
            page.goto(f"{BRIZO_URL}/cabinet/deals#{target_hash}", timeout=TIMEOUT)
            time.sleep(3)  # SPA needs time to render the popup

        try:
            _navigate()
        except PlaywrightTimeout:
            log.warning("  Навигация к сделке #%s зависла — retry через 5 сек", lead_id)
            time.sleep(5)
            _navigate()  # raises PlaywrightTimeout again if retry also fails

    # Dismiss any modal overlay that might block clicks
    try:
        overlay = page.query_selector(".bg-overlay, .brizo-popup-overlay")
        if overlay and overlay.is_visible():
            page.keyboard.press("Escape")
            time.sleep(0.5)
    except Exception:
        pass


def _find_field_input(page: Page, label_text: str):
    """Find the input/textarea associated with a .base-input__label-text."""
    try:
        result = page.evaluate(
            """(label) => {
                const labels = document.querySelectorAll('.base-input__label-text');
                for (const lbl of labels) {
                    if (lbl.innerText.trim().toLowerCase().includes(label.toLowerCase())) {
                        const container = lbl.closest('.base-input');
                        if (container) {
                            const inp = container.querySelector(
                                'input.base-input__field, textarea.base-input__field'
                            );
                            if (inp) return true;
                        }
                    }
                }
                return false;
            }""",
            label_text,
        )
        if not result:
            return None
        # Return Playwright locator for interaction
        return page.locator(
            f'.base-input:has(.base-input__label-text:text-is("{label_text}")) '
            f'.base-input__field'
        ).first
    except Exception:
        return None


def _extract_all_fields(page: Page) -> dict:
    """Extract all label→value pairs from .base-input elements in the deal."""
    try:
        pairs = page.evaluate("""
            () => {
                const result = {};
                const containers = document.querySelectorAll('.base-input');
                for (const c of containers) {
                    const lbl = c.querySelector('.base-input__label-text');
                    const inp = c.querySelector('input.base-input__field, textarea.base-input__field');
                    if (lbl && inp && inp.value) {
                        result[lbl.innerText.trim().toLowerCase()] = inp.value;
                    }
                }
                return result;
            }
        """)
        return pairs or {}
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Lead Finder helpers (create new deals, dedup check)
# ─────────────────────────────────────────────────────────────────────────────

SERAFIM_USER_ID = 46897   # Серафим Саровский — bot account, picks up new leads

# Field IDs used when creating lead-finder deals
_FIELD_INN        = 486303   # ИНН компании
_FIELD_CHECKO     = 483103   # Чекко URL
_FIELD_SITE       = 483104   # Сайт
_FIELD_SOURCE     = 483204   # Источник сделки (dropdown)
_FIELD_ADDRESS    = 494995   # Адрес (регион)
_FIELD_FIO        = 485768   # ФИО контактное лицо
_FIELD_INN_DIR    = 504161   # ИНН генерального директора
_FIELD_CORP_EMAIL = 485095   # Корпоративная почта

# Option ID for «Источник сделки» = "робот Ника" (id 30581 from /api/fields?model_id=1)
# Ставится на все сделки, создаваемые ботом (и из Checko, и из Dadata) —
# это не "пришло через Checko", а "создано автоматически роботом".
_SOURCE_ROBOT_NIKA = 30581


def is_inn_in_brizo(inn: str) -> bool:
    """
    Return True if ANY deal in Brizo already has this INN (field 486303).
    Used by lead_finder.py to skip companies already in the pipeline.
    Unlike check_duplicate_by_inn(), total==1 also means "duplicate" because
    this is called BEFORE creating a new deal.
    """
    if not inn:
        return False
    sess = _get_api_session()
    if not sess:
        log.warning("[dup] No API session — skipping INN check")
        return False
    try:
        r = sess.get(
            f"{BRIZO_URL}/api/funnels/21480/deals/table",
            params={"fields[486303]": inn, "limit": 1},
            timeout=15,
        )
        r.raise_for_status()
        total = r.json().get("meta", {}).get("overal_count", 0)
        log.info("[inn] ИНН %s: %d сделок в Brizo", inn, total)
        return total > 0
    except Exception as e:
        log.warning("[inn] INN check failed: %s", e)
        return False


def create_deal(
    name: str,
    inn: str = "",
    website: str = "",
    checko_url: str = "",
    region: str = "",
    director_name: str = "",
    director_inn: str = "",
    emails: list[str] | None = None,
    responsible_id: int | None = None,
) -> str | None:
    """
    Create a new deal in Brizo «База».
    responsible_id: Brizo user ID to assign as responsible (default: SERAFIM_USER_ID).

    Fills all fields that the main parser fills on qualified deals:
      486303  ИНН
      483103  Чекко URL
      483104  Сайт
      483204  Источник сделки = "чекко" (option 28409)
      494995  Адрес (регион)
      485095  Корпоративная почта
      485768  ФИО контактное лицо (директор)
      504161  ИНН генерального

    Returns the new deal ID (str) or None on failure.
    """
    sess = _get_api_session()
    if not sess:
        log.warning("create_deal: no API session")
        return None

    # Step 1 — create the deal shell
    r = sess.post(
        f"{BRIZO_URL}/api/deals",
        json={
            "name": name,
            "status_id": BASE_STATUS_ID,
            "responsible_id": responsible_id if responsible_id is not None else SERAFIM_USER_ID,
        },
        timeout=15,
    )
    if r.status_code not in (200, 201):
        log.warning("create_deal: POST /api/deals → %s: %s", r.status_code, r.text[:120])
        return None

    deal_id = str(r.json().get("id", ""))
    if not deal_id:
        log.warning("create_deal: no ID in response: %s", r.text[:120])
        return None

    log.info("create_deal: создана сделка #%s (%s) ✓", deal_id, name)

    # Step 2 — fill all available fields
    fields: list[dict] = [
        # Источник сделки = "робот Ника" — на всех сделках, создаваемых ботом,
        # независимо от того, откуда пришли данные (Checko или Dadata).
        {"id": _FIELD_SOURCE, "value": _SOURCE_ROBOT_NIKA},
    ]
    if checko_url:
        fields.append({"id": _FIELD_CHECKO, "value": checko_url})
    if inn:
        fields.append({"id": _FIELD_INN,    "value": inn})
    if website:
        fields.append({"id": _FIELD_SITE,   "value": website})
    if region:
        fields.append({"id": _FIELD_ADDRESS, "value": region})
    if director_name:
        fields.append({"id": _FIELD_FIO,    "value": director_name})
    if director_inn:
        fields.append({"id": _FIELD_INN_DIR, "value": director_inn})
    if emails:
        fields.append({"id": _FIELD_CORP_EMAIL, "value": ", ".join(emails)})

    rf = sess.patch(
        f"{BRIZO_URL}/api/deals/{deal_id}/fields",
        json=fields,
        timeout=15,
    )
    if rf.status_code in (200, 201):
        log.info("create_deal: поля заполнены (%d шт.) ✓", len(fields))
    else:
        log.warning("create_deal: PATCH /fields → %s: %s", rf.status_code, rf.text[:120])

    return deal_id


def create_contact_rest(
    deal_id: str,
    display_name: str,
    inn: str = "",
) -> str | None:
    """
    Create a contact via REST API and link it to the deal.

    display_name должен уже содержать роль в суффиксе:
      "Иванов Иван Иванович гендир"
      "Петров Пётр Петрович учред 45%"

    Шаги:
      1. POST /api/contragents {"name": display_name} → получаем contact_id
      2. POST /api/deals/{deal_id}/contragents {"ids": [contact_id]} → привязываем к сделке
      3. Если есть ИНН — постим его комментарием на контакт (как делает Playwright-версия)

    Возвращает строковый contact_id или None при ошибке.
    """
    sess = _get_api_session()
    if not sess:
        log.warning("create_contact_rest: no API session")
        return None

    # 1 — создаём контакт
    r = sess.post(
        f"{BRIZO_URL}/api/contragents",
        json={"name": display_name},
        timeout=15,
    )
    if r.status_code not in (200, 201):
        log.warning("create_contact_rest: POST /api/contragents → %s: %s",
                    r.status_code, r.text[:120])
        return None

    contact_id = str(r.json().get("id", ""))
    if not contact_id:
        log.warning("create_contact_rest: no id in response: %s", r.text[:120])
        return None

    # 2 — привязываем к сделке
    r2 = sess.post(
        f"{BRIZO_URL}/api/deals/{deal_id}/contragents",
        json={"ids": [int(contact_id)]},
        timeout=15,
    )
    if r2.status_code not in (200, 201):
        log.warning("create_contact_rest: link to deal %s → %s: %s",
                    deal_id, r2.status_code, r2.text[:120])
        # Не возвращаем None — контакт создан, просто не привязан

    # 3 — ИНН как комментарий на контакте
    if inn:
        _post_contact_comment(contact_id, inn)

    log.info("create_contact_rest: контакт '%s' id=%s → сделка %s ✓",
             display_name, contact_id, deal_id)
    return contact_id


# ─────────────────────────────────────────────────────────────────────────────
# Duplicate check
# ─────────────────────────────────────────────────────────────────────────────

def check_duplicate_by_inn(inn: str, current_deal_id: str) -> bool:
    """
    Check if any OTHER deal in Brizo already has the same INN (field 486303).
    Uses the REST API — no browser needed. Returns True if a duplicate exists.
    total==1 means only the current deal; total>1 means at least one other deal
    shares the same INN.
    """
    if not inn:
        return False

    sess = _get_api_session()
    if not sess:
        log.warning("[dup] No API session — skipping INN duplicate check")
        return False

    try:
        r = sess.get(
            f"{BRIZO_URL}/api/funnels/21480/deals/table",
            params={"fields[486303]": inn, "limit": 5},
            timeout=15,
        )
        r.raise_for_status()
        total = r.json().get("meta", {}).get("overal_count", 0)
        log.info("[dup] INN %s: %d deal(s) found in Brizo", inn, total)
        return total > 1

    except Exception as e:
        log.warning("[dup] INN duplicate check failed: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point — backward compat with main.py
# ─────────────────────────────────────────────────────────────────────────────

def get_leads() -> list[dict]:
    """
    Full browser session: log in and return qualifying leads from "База".
    Used by main.py as: leads = get_leads()
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page(viewport={"width": 1920, "height": 1080})
        try:
            login(page)
            return get_new_leads(page)
        except Exception as exc:
            log.error("get_leads() failed: %s", exc, exc_info=True)
            raise
        finally:
            browser.close()
