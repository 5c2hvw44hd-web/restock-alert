#!/usr/bin/env python3
"""
재입고 알림 봇 (카카오톡 '나에게 보내기')

GitHub Actions에서 5분마다 실행돼요.
1) 상품 페이지를 읽어서 '품절'인지 '구매 가능'인지 판정하고
2) 품절 → 구매 가능으로 바뀌면 카카오톡 '나와의 채팅'으로 알림을 보내요.

상품 주소, 감시할 옵션, 알림 횟수 같은 설정은
.github/workflows/restock.yml 의 env 부분에서 바꾸면 돼요.
"""

from __future__ import annotations

import html as htmllib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup, NavigableString, Tag


# ─────────────────────────────────────────────
# 설정 (워크플로 파일의 env 에서 읽어요)
# ─────────────────────────────────────────────
def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(_env(name, str(default))))
    except ValueError:
        return default


# 기본 설정이에요. 평소에는 GitHub [Actions] > '알림 설정 바꾸기'에서 바꾸면 되고,
# 거기서 바꾼 값은 state/settings.json 에 저장돼서 이 기본값보다 우선해요.
DEFAULT_SETTINGS = {
    "product_url": "https://moftkorea.com/product/detail.html?product_no=279&cate_no=134&display_group=1",
    "product_name": "[프리오더] MOFT 모듈러 케이스 아이폰 듀오 맥세이프",
    "watch_options": "",  # 비우면 상품 전체. 예: "미스티코브" / "미스티코브,네이비블루"
    "alert_count": 3,  # 구매 가능해졌을 때 보낼 카톡 횟수 (약 5분 간격)
    "paused": False,  # True면 알림 일시정지
}

# 실제 값은 실행할 때 apply_settings()가 채워요
PRODUCT_URL = DEFAULT_SETTINGS["product_url"]
PRODUCT_NAME = DEFAULT_SETTINGS["product_name"]
WATCH_OPTIONS = ""
ALERT_COUNT = 3
PAUSED = False

REDIRECT_URI = _env("KAKAO_REDIRECT_URI", "https://example.com")
REST_KEY = _env("KAKAO_REST_API_KEY")
CLIENT_SECRET = _env("KAKAO_CLIENT_SECRET")
TOKEN_PASSWORD = _env("TOKEN_PASSWORD")  # 비워두면 클라이언트 시크릿으로 암호화해요
AUTH_INPUT = _env("KAKAO_AUTH_URL")
SEND_TEST = _env("SEND_TEST").lower() == "true"
SETTINGS_MODE = _env("SETTINGS_MODE").lower() == "true"
MANUAL_RUN = _env("GITHUB_EVENT_NAME") == "workflow_dispatch"
IN_ACTIONS = _env("GITHUB_ACTIONS") == "true"

FAIL_WARN_MINUTES = 60  # '판정 불가'가 이만큼 이어지면 점검 알림을 보내요

STATE_DIR = "state"
STATE_FILE = os.path.join(STATE_DIR, "status.json")
TOKEN_FILE = os.path.join(STATE_DIR, "kakao_token.enc")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")

KST = timezone(timedelta(hours=9))

LABEL = {
    "available": "🟢 구매 가능",
    "soldout": "🔴 품절",
    "unknown": "⚪ 판정 불가",
    None: "기록 없음",
}


def now() -> datetime:
    return datetime.now(KST)


# ─────────────────────────────────────────────
# 결과 자료형
# ─────────────────────────────────────────────
@dataclass
class Option:
    name: str
    available: bool


@dataclass
class Result:
    status: str  # "available" | "soldout" | "unknown"
    title: str = ""
    reasons: list = field(default_factory=list)
    options: list = field(default_factory=list)
    option_source: str = ""  # "stock"(재고 데이터) | "select"(이름만) | ""
    debug: list = field(default_factory=list)


# ─────────────────────────────────────────────
# 1. 상품 페이지 불러오기
# ─────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


class FetchError(Exception):
    pass


def _decode(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if text.count("�") > 20:  # UTF-8이 아니면 EUC-KR로 다시 시도
        try:
            return raw.decode("cp949")
        except UnicodeDecodeError:
            pass
    return text


def fetch_page(url: str) -> str:
    sep = "&" if "?" in url else "?"
    target = f"{url}{sep}_t={int(time.time())}"  # 캐시된 옛 페이지를 피하려고 붙여요
    last_error = "알 수 없는 오류"
    for attempt in range(1, 4):
        try:
            resp = requests.get(target, headers=HEADERS, timeout=20)
            if resp.status_code == 200 and resp.content:
                return _decode(resp.content)
            last_error = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_error = type(exc).__name__
        if attempt < 3:
            time.sleep(4)
    raise FetchError(last_error)


# ─────────────────────────────────────────────
# 2. 품절/구매 가능 판정 (카페24 쇼핑몰 기준)
# ─────────────────────────────────────────────
SOLDOUT_RE = re.compile(
    r"^(sold\s*-?\s*out|품절|일시\s*품절|품절\s*상품|현재\s*품절|구매\s*불가)$", re.I
)
BUY_RE = re.compile(
    r"^((바로\s*)?(구매|주문)(\s*하기)?|buy(\s*it)?(\s*now)?"
    r"|예약\s*(주문|구매)(\s*하기)?|pre-?\s*order(\s*now)?)$",
    re.I,
)
CART_RE = re.compile(r"^(장바구니(\s*담기)?|add\s*to\s*cart|cart)$", re.I)

# 관련상품·추가구성상품·리뷰 같은 '다른 상품' 영역은 판정에서 빼요
EXCLUDE_PARTS = (
    "addproduct", "relation", "recent", "listmain", "listnormal", "listrecommend",
    "listnew", "listitem", "prdlist", "review", "board", "qna", "additional",
    "basketpackage",
)
HIDDEN_CLASSES = {"displaynone", "display-none", "d-none"}
SKIP_PARENTS = {"script", "style", "noscript", "template", "option", "select", "textarea", "title"}
TOP_TAGS = {"body", "html", "[document]"}


def _describe(el: Tag) -> str:
    s = el.name or "?"
    if el.get("id"):
        s += f"#{el.get('id')}"
    classes = el.get("class") or []
    if classes:
        s += "." + ".".join(classes)
    return s


def _hidden_by(el: Tag) -> str | None:
    """요소(또는 부모)가 숨김 처리돼 있으면 숨긴 요소 설명을, 아니면 None."""
    node = el
    while isinstance(node, Tag) and node.name not in TOP_TAGS:
        classes = [c.lower() for c in (node.get("class") or [])]
        style = (node.get("style") or "").replace(" ", "").lower()
        if (
            any(c in HIDDEN_CLASSES for c in classes)
            or "display:none" in style
            or "visibility:hidden" in style
            or node.has_attr("hidden")
        ):
            return _describe(node)
        node = node.parent
    return None


def _excluded(el: Tag) -> bool:
    node = el
    while isinstance(node, Tag) and node.name not in TOP_TAGS:
        marker = (" ".join(node.get("class") or []) + " " + (node.get("id") or "")).lower()
        if any(part in marker for part in EXCLUDE_PARTS):
            return True
        node = node.parent
    return False


def _clean(text: str) -> str:
    text = htmllib.unescape(re.sub(r"<[^>]+>", "", str(text)))
    return " ".join(text.split())


def _find_scopes(soup: BeautifulSoup) -> list:
    """상품 상세·구매 버튼 영역을 찾아요. 못 찾으면 페이지 전체."""
    scopes = [
        el
        for el in soup.select(".xans-product-action, .xans-product-detail")
        if not _excluded(el)
    ]
    return scopes or [soup.body or soup]


def _scan(scopes: list) -> dict:
    found = {"soldout": [], "buy": [], "cart": []}
    seen = set()

    def add(kind, el, text):
        if id(el) in seen:
            return
        seen.add(id(el))
        found[kind].append((el, text))

    for scope in scopes:
        # 글자로 판단: 'SOLD OUT', '품절', '바로 구매하기', '장바구니 담기' 등
        for node in scope.find_all(string=True):
            if type(node) is not NavigableString:  # 주석·스크립트 제외
                continue
            parent = node.parent
            if parent is None or parent.name in SKIP_PARENTS:
                continue
            text = " ".join(str(node).split())
            if not text or len(text) > 30:
                continue
            if SOLDOUT_RE.match(text):
                kind = "soldout"
            elif BUY_RE.match(text):
                kind = "buy"
            elif CART_RE.match(text):
                kind = "cart"
            else:
                continue
            if not _excluded(parent):
                add(kind, parent, text)
        # 품절 아이콘 이미지
        for img in scope.find_all("img"):
            alt = (img.get("alt") or "").strip()
            src = (img.get("src") or "").lower()
            if (SOLDOUT_RE.match(alt) or "soldout" in src) and not _excluded(img):
                add("soldout", img, alt or "품절 아이콘")
        # 카페24 구매 버튼 id
        for el in scope.select("#btnBuy, #btnReserve"):
            if not _excluded(el):
                add("buy", el, " ".join(el.get_text(" ", strip=True).split()) or el.get("id"))
    return found


def _truthy(value) -> bool:
    return value is True or str(value).strip().upper() in ("T", "TRUE", "Y", "1")


def _to_int(value):
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _js_string_to_text(s: str) -> str:
    """자바스크립트 문자열 리터럴 내용을 실제 문자열로 바꿔요."""
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "'":
                out.append("'")
            elif nxt in '"\\/bfnrtu':
                out.append("\\" + nxt)
            else:
                out.append(nxt)
            i += 2
            continue
        out.append({'"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(c, c))
        i += 1
    return json.loads('"' + "".join(out) + '"')


def _stock_option_available(d: dict) -> bool:
    if "is_selling" in d and not _truthy(d["is_selling"]):
        return False
    if "is_display" in d and not _truthy(d["is_display"]):
        return False
    stock = _to_int(d.get("stock_number"))
    if _truthy(d.get("use_stock", False)) and _truthy(d.get("use_soldout", "F")):
        if stock is not None and stock <= 0:
            return False
    return True


def _read_stock_data(page_html: str):
    for m in re.finditer(r"option_stock_data\s*=(?!=)\s*", page_html):
        rest = page_html[m.end():]
        try:
            if rest[:1] in ("'", '"'):
                q = rest[0]
                sm = re.match(q + r"((?:\\.|[^" + q + r"\\])*)" + q, rest, re.S)
                data = json.loads(_js_string_to_text(sm.group(1))) if sm else None
            elif rest[:1] == "{":
                data, _ = json.JSONDecoder().raw_decode(rest)
            else:
                data = None
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict) and data:
            return data
    return None


def parse_option_stock(page_html: str) -> list:
    """카페24가 페이지에 넣어두는 옵션별 재고 데이터(option_stock_data)를 읽어요."""
    data = _read_stock_data(page_html)
    if not data:
        return []
    options = []
    for key, d in data.items():
        if not isinstance(d, dict):
            continue
        original = d.get("option_value_orginal") or d.get("option_value_original")
        if isinstance(original, list) and original:
            name = " / ".join(_clean(x) for x in original)
        else:
            name = _clean(d.get("option_value") or d.get("option_name") or key)
        options.append(Option(name or key, _stock_option_available(d)))
    return options


def parse_select_options(soup: BeautifulSoup) -> list:
    """재고 데이터가 없을 때 옵션 '이름'만 참고용으로 읽어요 (판정에는 안 써요)."""
    options = []
    for sel in soup.select("select[id^=product_option_id]"):
        if _excluded(sel):
            continue
        for opt in sel.find_all("option"):
            value = (opt.get("value") or "").strip()
            text = " ".join(opt.get_text(" ", strip=True).split())
            if value in ("", "*", "**") or not text or text.startswith("-"):
                continue
            sold = bool(re.search(r"품절|sold\s*-?\s*out", text, re.I)) or opt.has_attr("disabled")
            options.append(Option(text, not sold))
    return options


def _watch_groups(spec: str) -> list:
    """'미스티코브, 17 Pro+네이비블루' → [['미스티코브'], ['17pro', '네이비블루']]"""
    groups = []
    for item in spec.split(","):
        keys = [re.sub(r"\s+", "", k).lower() for k in item.split("+")]
        keys = [k for k in keys if k]
        if keys:
            groups.append(keys)
    return groups


WATCH_GROUPS = _watch_groups(WATCH_OPTIONS)


def option_matches(name: str, groups: list | None = None) -> bool:
    groups = WATCH_GROUPS if groups is None else groups
    flat = re.sub(r"\s+", "", name).lower()
    return any(all(k in flat for k in g) for g in groups)


def _detect_title(soup: BeautifulSoup) -> str:
    for sel in (".xans-product-detail .headingArea h2", ".headingArea h2", ".xans-product-detail h2"):
        el = soup.select_one(sel)
        if el and _clean(el.get_text(" ", strip=True)):
            return _clean(el.get_text(" ", strip=True))
    for attrs in ({"property": "og:title"}, {"name": "title"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            return _clean(meta["content"])
    if soup.title and soup.title.string:
        return _clean(soup.title.string)
    return ""


def _product_no(url: str) -> str | None:
    for pattern in (r"product_no=(\d+)", r"/(\d+)/category/", r"/product/[^/?#]+/(\d+)"):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def analyze(page_html: str, product_url: str = "") -> Result:
    soup = BeautifulSoup(page_html, "html.parser")
    res = Result("unknown")
    res.title = (PRODUCT_NAME or _detect_title(soup) or "상품")[:80]

    # 상품 페이지가 맞는지 확인 (상품이 내려가 메인으로 이동한 경우 등)
    pno = _product_no(product_url or PRODUCT_URL)
    has_detail = soup.select_one(".xans-product-detail, .xans-product-action") is not None
    if pno and not has_detail and not re.search(
        rf"(product_no.{{0,40}}?\b{pno}\b|iProductNo\D{{0,10}}{pno}\b)", page_html, re.S
    ):
        res.reasons.append(f"상품 번호({pno})가 페이지에 없어요. 상품이 내려갔거나 주소가 바뀌었을 수 있어요.")
        return res

    found = _scan(_find_scopes(soup))
    vis = {k: [(el, t) for el, t in v if not _hidden_by(el)] for k, v in found.items()}
    names = {"soldout": "품절 표시", "buy": "구매 버튼", "cart": "장바구니 버튼"}
    for kind, items in found.items():
        for el, text in items:
            hb = _hidden_by(el)
            res.debug.append(
                f"[{names[kind]}] <{_describe(el)}> '{text}' → " + (f"숨김 ({hb})" if hb else "보임")
            )

    stock_opts = parse_option_stock(page_html)
    if stock_opts:
        res.options, res.option_source = stock_opts, "stock"
    else:
        res.options = parse_select_options(soup)
        res.option_source = "select" if res.options else ""

    sold_v, buy_v, cart_v = bool(vis["soldout"]), bool(vis["buy"]), bool(vis["cart"])
    if sold_v:
        res.reasons.append("'SOLD OUT/품절' 표시가 화면에 보여요")
    elif found["soldout"]:
        res.reasons.append("'SOLD OUT/품절' 표시는 숨겨져 있어요")
    if buy_v:
        res.reasons.append("구매 버튼이 화면에 보여요")
    elif found["buy"]:
        res.reasons.append("구매 버튼이 숨겨져 있어요")

    base = None
    if sold_v and not buy_v:
        base = "soldout"
    elif buy_v and not sold_v:
        base = "available"
    elif not sold_v and not buy_v and not found["buy"] and cart_v:
        base = "available"
        res.reasons.append("장바구니 버튼만 보여서 구매 가능으로 판단했어요")

    if res.option_source == "stock":
        any_ok = any(o.available for o in res.options)
        if base is None:
            base = "available" if any_ok else "soldout"
            res.reasons.append("버튼만으로는 애매해서 옵션 재고 데이터로 판단했어요")
        elif base == "available" and not any_ok:
            res.reasons.append("참고: 옵션 재고 데이터는 모두 0이지만 버튼 기준으로 판단했어요")
        elif base == "soldout" and any_ok:
            res.reasons.append("참고: 옵션 재고 데이터엔 남은 옵션이 있지만 버튼 기준으로 판단했어요")

    if base is None:
        res.reasons.append("구매 버튼과 품절 표시를 확실하게 구분하지 못했어요")
        return res

    if base == "available" and WATCH_GROUPS:
        if res.option_source == "stock":
            matched = [o for o in res.options if option_matches(o.name)]
            if matched:
                ok = [o for o in matched if o.available]
                res.reasons.append(f"감시 옵션 {len(matched)}개 중 구매 가능 {len(ok)}개")
                base = "available" if ok else "soldout"
            else:
                res.reasons.append("⚠️ WATCH_OPTIONS와 이름이 맞는 옵션이 없어서 상품 전체 기준으로 판단했어요")
        else:
            res.reasons.append("⚠️ 옵션 재고 데이터가 없어서 상품 전체 기준으로 판단했어요")

    res.status = base
    return res


def check_product() -> Result:
    try:
        page = fetch_page(PRODUCT_URL)
    except FetchError as exc:
        return Result("unknown", title=PRODUCT_NAME or "상품",
                      reasons=[f"상품 페이지를 불러오지 못했어요 ({exc})"])
    return analyze(page, PRODUCT_URL)


# ─────────────────────────────────────────────
# 3. 카카오톡 '나에게 보내기'
# ─────────────────────────────────────────────
KAUTH_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAPI_MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
FORM_CT = "application/x-www-form-urlencoded;charset=utf-8"

KAKAO_HINTS = {
    "KOE320": "로그인 코드가 만료됐거나 이미 사용됐어요. 카카오 로그인부터 다시 해서 새 주소로 실행해 주세요.",
    "KOE303": "리다이렉트 URI가 달라요. 카카오 콘솔에 등록한 주소와 KAKAO_REDIRECT_URI가 똑같아야 해요.",
    "KOE006": "리다이렉트 URI가 등록되지 않았어요. 카카오 콘솔에 https://example.com 을 등록해 주세요.",
    "KOE010": "클라이언트 시크릿이 맞지 않아요. GitHub Secrets의 KAKAO_CLIENT_SECRET 값을 확인해 주세요.",
    "KOE101": "REST API 키가 맞지 않아요. GitHub Secrets의 KAKAO_REST_API_KEY 값을 확인해 주세요.",
    "KOE322": "카카오 연결이 만료됐어요. 가이드의 '카카오 처음 연결'을 다시 해 주세요.",
    "-401": "카카오 연결이 유효하지 않아요. 가이드의 '카카오 처음 연결'을 다시 해 주세요.",
    "-402": "'카카오톡 메시지 전송' 동의가 없어요. 동의항목을 설정한 뒤 카카오 로그인부터 다시 해 주세요.",
}


class KakaoError(Exception):
    pass


class KakaoNotConnected(KakaoError):
    pass


def _mask(value: str) -> None:
    """GitHub 로그에 토큰이 찍히지 않도록 가려요."""
    if value and IN_ACTIONS:
        print(f"::add-mask::{value}")


def _json(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _kakao_error(resp: requests.Response, what: str) -> KakaoError:
    j = _json(resp)
    code = str(j.get("error_code") or j.get("code") or resp.status_code)
    desc = j.get("error_description") or j.get("msg") or j.get("error") or resp.text[:150]
    hint = KAKAO_HINTS.get(code)
    return KakaoError(f"{what} 실패 [{code}] {desc}" + (f"\n   👉 {hint}" if hint else ""))


def _require_keys() -> None:
    if not REST_KEY:
        raise KakaoError("GitHub Secrets에 KAKAO_REST_API_KEY가 없어요.")
    if not (CLIENT_SECRET or TOKEN_PASSWORD):
        raise KakaoError("GitHub Secrets에 KAKAO_CLIENT_SECRET이 없어요.")


def _openssl(extra: list, data: bytes) -> bytes:
    password = TOKEN_PASSWORD or CLIENT_SECRET
    if not password:
        raise KakaoError("GitHub Secrets에 KAKAO_CLIENT_SECRET이 없어요.")
    env = dict(os.environ, RESTOCK_PW=password)
    cmd = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256",
           "-a", "-A", *extra, "-pass", "env:RESTOCK_PW"]
    proc = subprocess.run(cmd, input=data, capture_output=True, env=env)
    if proc.returncode != 0:
        raise KakaoError(
            "저장된 카카오 연결 정보를 열지 못했어요. 클라이언트 시크릿을 바꿨다면 "
            "가이드의 '카카오 처음 연결'을 다시 해 주세요."
        )
    return proc.stdout


def save_refresh_token(token: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    encrypted = _openssl(["-salt"], token.encode()).decode().strip()
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(encrypted + "\n")


def load_refresh_token() -> str | None:
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, encoding="utf-8") as f:
        encrypted = f.read().strip()
    if not encrypted:
        return None
    token = _openssl(["-d"], (encrypted + "\n").encode()).decode().strip()
    _mask(token)
    return token


_access_token: str | None = None


def get_access_token() -> str:
    """저장된 리프레시 토큰으로 6시간짜리 액세스 토큰을 새로 받아요."""
    global _access_token
    if _access_token:
        return _access_token
    refresh = load_refresh_token()
    if not refresh:
        raise KakaoNotConnected("아직 카카오 연결 전이에요 (가이드의 '카카오 처음 연결' 단계).")
    _require_keys()
    data = {"grant_type": "refresh_token", "client_id": REST_KEY, "refresh_token": refresh}
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET
    resp = requests.post(KAUTH_TOKEN_URL, data=data, headers={"Content-Type": FORM_CT}, timeout=15)
    if resp.status_code != 200:
        raise _kakao_error(resp, "카카오 토큰 갱신")
    j = _json(resp)
    token = j.get("access_token")
    if not token:
        raise _kakao_error(resp, "카카오 토큰 갱신")
    _mask(token)
    if j.get("refresh_token"):  # 만료 1개월 전부터 새 리프레시 토큰이 와요 → 바로 저장
        _mask(j["refresh_token"])
        save_refresh_token(j["refresh_token"])
        print("🔁 카카오 연결 정보가 새로 발급돼서 저장했어요.")
    _access_token = token
    return token


def _clip(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def kakao_send(text: str, link_url: str) -> None:
    token = get_access_token()
    template = {
        "object_type": "text",
        "text": _clip(text),
        "link": {"web_url": link_url, "mobile_web_url": link_url},
        "button_title": "상품 페이지 열기",
    }
    resp = requests.post(
        KAPI_MEMO_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": FORM_CT},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
        timeout=15,
    )
    if resp.status_code != 200:
        raise _kakao_error(resp, "카카오톡 보내기")
    if _json(resp).get("result_code", 0) != 0:
        raise _kakao_error(resp, "카카오톡 보내기")


def extract_auth_code(text: str) -> str:
    text = text.strip()
    m = re.search(r"[?&#]error=([^&#\s]+)", text)
    if m:
        raise KakaoError(f"카카오 로그인이 취소됐거나 실패했어요 ({unquote(m.group(1))}). 처음부터 다시 해 주세요.")
    m = re.search(r"[?&#]code=([^&#\s]+)", text)
    if m:
        return unquote(m.group(1))
    if re.fullmatch(r"[A-Za-z0-9_\-]{20,}", text):
        return text
    raise KakaoError("붙여넣은 주소에서 code= 부분을 찾지 못했어요. 로그인 후 주소창의 '전체 주소'를 복사해 주세요.")


def kakao_setup(user_input: str) -> None:
    """처음 1번: 로그인 후 받은 주소의 code로 토큰을 받아 암호화해서 저장해요."""
    global _access_token
    _require_keys()
    code = extract_auth_code(user_input)
    data = {
        "grant_type": "authorization_code",
        "client_id": REST_KEY,
        "redirect_uri": REDIRECT_URI,
        "code": code,
    }
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET
    resp = requests.post(KAUTH_TOKEN_URL, data=data, headers={"Content-Type": FORM_CT}, timeout=15)
    if resp.status_code != 200:
        raise _kakao_error(resp, "카카오 첫 연결")
    j = _json(resp)
    if not j.get("access_token") or not j.get("refresh_token"):
        raise _kakao_error(resp, "카카오 첫 연결")
    _mask(j["access_token"])
    _mask(j["refresh_token"])
    if "talk_message" not in (j.get("scope") or "talk_message"):
        print("⚠️ '카카오톡 메시지 전송' 동의가 빠진 것 같아요. 카톡이 안 오면 동의항목을 확인해 주세요.")
    save_refresh_token(j["refresh_token"])
    _access_token = j["access_token"]
    print("✅ 카카오 연결 완료! 연결 정보는 암호화해서 저장했어요.")


# ─────────────────────────────────────────────
# 4. 상태 저장 & 알림 결정
# ─────────────────────────────────────────────
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        print("⚠️ 상태 파일을 읽지 못해서 새로 시작해요.")
        return {}


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _minutes_since(iso: str) -> int:
    try:
        return int((now() - datetime.fromisoformat(iso)).total_seconds() // 60)
    except (TypeError, ValueError):
        return 0


def _available_names(res: Result) -> list:
    if res.option_source != "stock":
        return []
    return [o.name for o in res.options if o.available and (not WATCH_GROUPS or option_matches(o.name))]


def build_alert_text(res: Result, sent: int) -> str:
    if sent == 0:
        head = "🔔 재입고 알림! 지금 구매할 수 있어요"
    else:
        head = f"⏰ 아직 구매할 수 있어요 ({sent + 1}/{ALERT_COUNT})"
    lines = [head, res.title, f"{now():%m/%d %H:%M} 확인"]
    names = _available_names(res)
    if names:
        lines.append("가능 옵션: " + ", ".join(names))
    return _clip("\n".join(lines))


def handle_status(res: Result, state: dict) -> int:
    stamp = now().isoformat(timespec="minutes")
    prev = state.get("status")

    if res.status == "unknown":
        if not state.get("fail_since"):
            state["fail_since"] = stamp
        minutes = _minutes_since(state["fail_since"])
        print(f"⚪ 판정 불가 상태가 {minutes}분째예요. (지난 상태 유지: {LABEL.get(prev, prev)})")
        if minutes >= FAIL_WARN_MINUTES and not state.get("fail_warned"):
            reason = res.reasons[-1] if res.reasons else "원인 불명"
            text = (
                f"⚠️ 재입고 알림 점검 필요\n{res.title}\n{minutes}분째 상품 상태를 확인하지 못하고 있어요.\n"
                f"사유: {reason}\nGitHub Actions 실행 기록을 확인해 주세요."
            )
            try:
                kakao_send(text, PRODUCT_URL)
                state["fail_warned"] = True
                print("📨 점검 필요 카톡을 보냈어요.")
            except KakaoNotConnected as exc:
                print(f"ℹ️ {exc}")
            except KakaoError as exc:
                print(f"❌ 점검 카톡 전송 실패: {exc}")
                return 1
        return 0

    state.pop("fail_since", None)
    state.pop("fail_warned", None)

    if res.status != prev:
        print(f"🔄 상태가 바뀌었어요: {LABEL.get(prev, prev)} → {LABEL[res.status]}")
        state["status"] = res.status
        state["since"] = stamp
        state["alerts_sent"] = 0

    if res.status != "available":
        print("💤 구매 가능해지면 알려드릴게요.")
        return 0

    sent = int(state.get("alerts_sent") or 0)
    if sent >= ALERT_COUNT:
        print(f"✅ 이미 알림을 {sent}번 보냈어요. 다시 품절됐다가 풀리면 또 알려드려요.")
        return 0
    try:
        kakao_send(build_alert_text(res, sent), PRODUCT_URL)
        state["alerts_sent"] = sent + 1
        print(f"📨 재입고 카톡을 보냈어요! ({sent + 1}/{ALERT_COUNT})")
        return 0
    except KakaoNotConnected:
        state["alerts_sent"] = sent + 1
        print("❌ 구매 가능해졌는데 카카오 연결이 안 돼 있어서 카톡을 못 보냈어요!")
        return 1
    except KakaoError as exc:
        print(f"❌ 구매 가능해졌는데 카톡 전송에 실패했어요: {exc}")
        return 1  # 보낸 횟수를 올리지 않아서 다음 실행 때 다시 시도해요


def daily_token_check(state: dict) -> int:
    """하루 1번 카카오 연결이 살아 있는지 확인해요 (문제 있으면 GitHub이 메일로 알려줘요)."""
    if not os.path.exists(TOKEN_FILE):
        print("ℹ️ 아직 카카오 연결 전이에요. 가이드의 '카카오 처음 연결'을 진행해 주세요.")
        return 0
    today = f"{now():%Y-%m-%d}"
    if state.get("token_checked_on") == today and not MANUAL_RUN:
        return 0
    state["token_checked_on"] = today
    try:
        get_access_token()
        state.pop("token_error", None)
        print("🔑 카카오 연결 정상")
        return 0
    except KakaoError as exc:
        state["token_error"] = str(exc).splitlines()[0][:200]
        print(f"❌ 카카오 연결 확인 실패: {exc}")
        return 1


def send_status_message(res: Result, setup: bool) -> int:
    head = "✅ 재입고 알림 연결 완료!" if setup else "🔔 테스트 메시지예요"
    tail = "지금 구매할 수 있어요!" if res.status == "available" else "구매 가능해지면 카톡으로 알려드릴게요."
    text = f"{head}\n{res.title}\n현재 상태: {LABEL[res.status]}\n{tail}"
    try:
        kakao_send(text, PRODUCT_URL)
        print("📨 확인용 카톡을 보냈어요. '나와의 채팅'을 확인해 보세요.")
        return 0
    except KakaoError as exc:
        print(f"❌ 확인용 카톡 전송 실패: {exc}")
        return 1


# ─────────────────────────────────────────────
# 5. 설정 (GitHub [Actions] > '알림 설정 바꾸기'에서 바꿔요)
# ─────────────────────────────────────────────
class SettingsError(Exception):
    pass


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    # 예전 방식(워크플로 파일의 env)으로 준 값도 받아줘요
    env_url = _env("PRODUCT_URL")
    if env_url and env_url != s["product_url"]:
        s["product_url"], s["product_name"] = env_url, ""
    if _env("PRODUCT_NAME"):
        s["product_name"] = _env("PRODUCT_NAME")
    if _env("WATCH_OPTIONS"):
        s["watch_options"] = _env("WATCH_OPTIONS")
    if _env("ALERT_COUNT"):
        s["alert_count"] = _int_env("ALERT_COUNT", 3)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            s.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        print("⚠️ 설정 파일을 읽지 못해서 기본 설정을 써요.")
    return s


def apply_settings(s: dict) -> None:
    global PRODUCT_URL, PRODUCT_NAME, WATCH_OPTIONS, WATCH_GROUPS, ALERT_COUNT, PAUSED
    PRODUCT_URL = str(s.get("product_url") or DEFAULT_SETTINGS["product_url"]).strip()
    PRODUCT_NAME = str(s.get("product_name") or "").strip()
    WATCH_OPTIONS = str(s.get("watch_options") or "").strip()
    WATCH_GROUPS = _watch_groups(WATCH_OPTIONS)
    try:
        ALERT_COUNT = max(1, int(s.get("alert_count") or 3))
    except (TypeError, ValueError):
        ALERT_COUNT = 3
    PAUSED = s.get("paused") is True or str(s.get("paused")).strip().lower() == "true"


def save_settings(s: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        data = {k: s.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def settings_line() -> str:
    alert = "꺼짐(일시정지)" if PAUSED else "켜짐"
    return f"감시 옵션: {WATCH_OPTIONS or '전체'} · 카톡 {ALERT_COUNT}번 · 알림 {alert}"


def change_settings(s: dict, state: dict) -> list:
    """'알림 설정 바꾸기'에 입력한 값을 반영하고, 바뀐 항목 목록을 돌려줘요."""
    # 1) 입력값부터 모두 확인 (하나라도 잘못되면 아무것도 바꾸지 않아요)
    text, choice = _env("SET_WATCH_TEXT"), _env("SET_WATCH")
    if text:
        new_watch = "" if re.sub(r"\s+", "", text) in ("전체", "모두", "-") else text
    elif choice and choice != "그대로":
        new_watch = "" if choice.startswith("전체") else choice
    else:
        new_watch = None

    count, new_count = _env("SET_ALERT_COUNT"), None
    if count and count != "그대로":
        try:
            new_count = max(1, int(count))
        except ValueError:
            raise SettingsError(f"카톡 횟수 '{count}'를 숫자로 읽지 못했어요.")

    onoff = _env("SET_ALERTS")
    url = _env("SET_PRODUCT_URL")
    if url and not re.match(r"^https?://[^\s/]+\.[^\s/]+", url):
        raise SettingsError("상품 주소는 https:// 로 시작하는 전체 주소로 넣어 주세요.")

    # 2) 반영
    changes, reset = [], False
    if new_watch is not None and new_watch != (s.get("watch_options") or ""):
        s["watch_options"] = new_watch
        changes.append(f"감시 옵션 → {new_watch or '전체'}")
        reset = True
    if new_count is not None and new_count != int(s.get("alert_count") or 3):
        s["alert_count"] = new_count
        changes.append(f"카톡 횟수 → {new_count}번")
    if onoff.startswith("끄기") and not s.get("paused"):
        s["paused"] = True
        changes.append("알림 → 꺼짐(일시정지)")
    elif onoff.startswith("켜기") and s.get("paused"):
        s["paused"] = False
        changes.append("알림 → 다시 켜짐")
        reset = True
    if url and url != s.get("product_url"):
        s["product_url"], s["product_name"] = url, ""
        changes.append("상품 → 새 주소")
        reset = True
        host = re.sub(r"^https?://", "", url).split("/")[0].lower()
        if host.removeprefix("www.") != "moftkorea.com":
            print(f"ℹ️ 다른 사이트예요. 카카오 콘솔 [앱] > [제품 링크 관리] > [웹 도메인]에 "
                  f"https://{host} 를 추가해야 카톡의 '상품 페이지 열기' 버튼이 열려요.")

    if reset:  # 무엇을 감시하는지가 바뀌면 처음부터 다시 판단해요
        for key in ("status", "since", "fail_since", "fail_warned"):
            state.pop(key, None)
        state["alerts_sent"] = 0
    return changes


def send_settings_message(res: Result, changes: list) -> int:
    head = "⚙️ 알림 설정을 바꿨어요" if changes else "⚙️ 지금 알림 설정이에요"
    lines = [
        head,
        _clip(res.title, 40),
        f"감시 옵션: {WATCH_OPTIONS or '전체'}",
        f"카톡 {ALERT_COUNT}번 · 알림 {'꺼짐(일시정지)' if PAUSED else '켜짐'}",
        f"현재 상태: {LABEL[res.status]}",
    ]
    try:
        kakao_send("\n".join(lines), PRODUCT_URL)
        print("📨 설정 확인 카톡을 보냈어요.")
        return 0
    except KakaoNotConnected as exc:
        print(f"ℹ️ {exc} 설정은 저장했어요.")
        return 0
    except KakaoError as exc:
        print(f"❌ 설정 확인 카톡 전송 실패: {exc}")
        return 1


# ─────────────────────────────────────────────
# 6. 실행 결과 보여주기 (로그 + 실행 요약 화면)
# ─────────────────────────────────────────────
def _write_summary(md_lines: list) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(md_lines) + "\n")


def report(res: Result, state: dict) -> None:
    prev = state.get("status")
    lines = [
        "=" * 50,
        f"🕐 {now():%Y-%m-%d %H:%M} (한국시간)",
        f"🛍  {res.title}",
        f"⚙️  {settings_line()}",
        f"📦 판정: {LABEL[res.status]}",
        "🔎 근거:",
        *[f"   - {r}" for r in (res.reasons or ["(없음)"])],
    ]
    if res.options:
        src = "재고 데이터" if res.option_source == "stock" else "이름만 참고, 재고 아님"
        lines.append(f"🎨 옵션 {len(res.options)}개 ({src}):")
        for o in res.options:
            mark = ("✅" if o.available else "❌") if res.option_source == "stock" else "•"
            watch = " 👀" if WATCH_GROUPS and option_matches(o.name) else ""
            lines.append(f"   {mark} {o.name}{watch}")
    lines.append(f"📝 지난 상태: {LABEL.get(prev, prev)}")
    if MANUAL_RUN and res.debug:
        lines.append("🧩 찾은 요소 (문제 생기면 이 부분을 보여주세요):")
        lines += [f"   {d}" for d in res.debug[:20]]
    lines.append("=" * 50)
    print("\n".join(lines))

    md = [
        f"## {LABEL[res.status]}",
        f"**{res.title}**  ",
        f"확인 시각: {now():%Y-%m-%d %H:%M} (한국시간) · 지난 상태: {LABEL.get(prev, prev)}  ",
        f"설정: {settings_line()}",
        "",
        "**판정 근거**",
        *[f"- {r}" for r in (res.reasons or ["(없음)"])],
    ]
    if res.options:
        md += ["", "| 옵션 | 상태 |", "|---|---|"]
        for o in res.options:
            if res.option_source == "stock":
                st = "✅ 구매 가능" if o.available else "❌ 품절"
            else:
                st = "(재고 정보 없음)"
            watch = " 👀" if WATCH_GROUPS and option_matches(o.name) else ""
            md.append(f"| {o.name.replace('|', '/')}{watch} | {st} |")
    if res.debug:
        md += ["", "<details><summary>찾은 요소 (문제 해결용)</summary>", "", "```"]
        md += res.debug[:30]
        md += ["```", "</details>"]
    _write_summary(md)


# ─────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────
def _dump(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def main() -> int:
    settings = load_settings()
    apply_settings(settings)
    state = load_state()
    before_state, before_settings = _dump(state), _dump(settings)
    exit_code = 0
    changes = []
    try:
        if SETTINGS_MODE:
            try:
                changes = change_settings(settings, state)
            except SettingsError as exc:
                print(f"❌ {exc} (설정은 바꾸지 않았어요)")
                return 1
            apply_settings(settings)
            print("⚙️ " + (", ".join(changes) if changes else "바뀐 설정 없음"))

        if AUTH_INPUT:
            try:
                kakao_setup(AUTH_INPUT)
            except KakaoError as exc:
                print(f"❌ {exc}")
                return 1
            except requests.RequestException as exc:
                print(f"❌ 카카오 서버에 연결하지 못했어요 ({type(exc).__name__}). 잠시 뒤 다시 해 주세요.")
                return 1

        if PAUSED and not (SETTINGS_MODE or AUTH_INPUT or SEND_TEST):
            print("⏸ 알림이 꺼져 있어서 상품 확인을 건너뛰어요. ([Actions] > '알림 설정 바꾸기'에서 켤 수 있어요)")
            _write_summary(["## ⏸ 알림 꺼짐 (일시정지)", f"설정: {settings_line()}"])
            try:
                return daily_token_check(state)  # 쉬는 동안에도 카카오 연결은 살려둬요
            except requests.RequestException:
                return 1

        res = check_product()
        report(res, state)
        try:
            if not PAUSED:
                exit_code |= handle_status(res, state)
            exit_code |= daily_token_check(state)
            if SETTINGS_MODE:
                exit_code |= send_settings_message(res, changes)
            elif AUTH_INPUT or SEND_TEST:
                exit_code |= send_status_message(res, setup=bool(AUTH_INPUT))
        except requests.RequestException as exc:
            print(f"❌ 카카오 서버와 통신하지 못했어요 ({type(exc).__name__}). 다음 실행 때 다시 시도해요.")
            exit_code = 1
    finally:
        if _dump(state) != before_state:
            save_state(state)
            print("💾 상태를 저장했어요.")
        if _dump(settings) != before_settings or (SETTINGS_MODE and changes and not os.path.exists(SETTINGS_FILE)):
            save_settings(settings)
            print("💾 설정을 저장했어요.")
    return exit_code


apply_settings(load_settings())

if __name__ == "__main__":
    sys.exit(main())
