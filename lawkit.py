#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lawkit — 법제처 국가법령정보 Open API(DRF) 위에 얹은 얇은 도구.

korean-law MCP와 함께 쓰는 보완 도구다. 세무 검토에서 자주 막히던 것을 채운다.
  1) 부칙(적용례·경과조치) 원문 조회                → law_addenda
  2) 조문 본문 키워드 검색(법령 전문을 훑어서)         → law_grep
  3) 조문 원문·특정 시행일 판 인출, JSON 우선          → law_article / law_versions
  4) 조문 안의 참조 조문·위임 문구 따라가기            → law_refs
  5) 국세청 예규를 제목+본문으로 검색                → nts_search
  6) 예규 요약표(사실관계·질의요지·요지 앞부분)·본문 묶음 파일 → nts_pack
     예규 본문(요지·회신·질의내용 전문)은 법제처 API에 없어 taxlaw.nts.go.kr에서 받는다.

설계 메모(2026-09 실측)
- 법제처 XML 엔드포인트가 통째로 죽은 날이 있었다(2026-09-16·17). JSON(type=JSON)은 살아 있었다. JSON만 쓴다.
- lawSearch의 `law`는 1건이면 dict, 여러 건이면 list. 둘 다 받는다.
- JO 파라미터는 6자리 "조번호4+가지번호2". 제103조의30 → 010330.
- 조문·검색은 target=eflaw에 연혁에서 선택한 MST·시행일을 함께 보낸다. target=law의 공포판 본문과 구분한다. 부칙은 관련 개정 공포판을 target=law로 조회한다.
- 조문·부칙 내용은 문자열/dict/list가 섞여 오므로 재귀로 편다. JO 조회에도 장·절 제목 단위(조문여부='전문')가 끼어 온다.
- 인증키(OC)는 요청 URL에만 넣고, 응답·오류·캐시 어디에도 남기지 않는다.

의존성: 표준 라이브러리만. Windows(PYTHONUTF8=1)·WSL 동일.
실행:  python lawkit.py --mcp                       (Claude Code MCP 서버, stdio)
       python lawkit.py article 법인세법 제73조 [--efyd 20260101] [--refresh]
       python lawkit.py addenda "법인세법 시행규칙" --require 제43조 --grep 적용례 경과조치 --last 6
       python lawkit.py grep 부가가치세법 등록 공제하지 --all [--efyd 20250101]
       python lawkit.py versions 법인세법
       python lawkit.py refs "법인세법 시행령" 제89조
       python lawkit.py nts 가지급금 [--scope both|title|body] [--display 20] [--page 1]
       python lawkit.py pack 200000000000013380 010000000000025173 --out triage.md [--full]
       python lawkit.py --selftest                   (오프라인 단위검사 → 네트워크 preflight → 실 API 6건)
인증키: 환경변수 LAW_OC. 없으면 ~/.claude.json 의 mcpServers.korean-law.env.LAW_OC 를 읽는다.
"""
import json, os, re, sys, time, sqlite3, hashlib, urllib.request, urllib.parse, argparse, pathlib, threading
from concurrent.futures import ThreadPoolExecutor

from datetime import datetime

API = "https://www.law.go.kr/DRF/"
CACHE_DIR = pathlib.Path(os.environ.get("LAWKIT_CACHE", pathlib.Path.home() / ".cache" / "lawkit"))
TTL_SEARCH, TTL_LAW = 6 * 3600, 7 * 24 * 3600
# 판 목록(lawSearch law/eflaw)은 하루 단위로 바뀐다. MST로 지정한 본문은 그 판 고유의 문언이라 바뀌지 않는다.
TTL_VERSIONS, TTL_FIXED_BODY = TTL_SEARCH, 3650 * 24 * 3600
WORKERS = int(os.environ.get('LAWKIT_WORKERS', '6'))  # 동시 요청 수(법제처·국세청 부하를 고려한 상한)
_HTTP_SLOTS = threading.BoundedSemaphore(WORKERS)  # 바깥·안쪽 병렬이 겹쳐도 law.go.kr 동시 요청은 WORKERS개까지
SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
PAGE = 100  # lawSearch display 최대치


class ConfigError(Exception):
    pass


def _oc():
    oc = os.environ.get("LAW_OC")
    if oc:
        return oc
    p = pathlib.Path.home() / ".claude.json"
    try:
        oc = json.load(open(p, encoding="utf-8"))["mcpServers"]["korean-law"]["env"]["LAW_OC"]
        if oc:
            return oc
    except Exception:
        pass
    raise ConfigError("LAW_OC 환경변수가 필요합니다(법제처 Open API 인증키). 설정 후 다시 실행.")


# ---------- HTTP + cache ----------
def _db():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(CACHE_DIR / "cache.sqlite", timeout=30)  # 병렬 조회 중 쓰기 잠금 대기
    con.execute("CREATE TABLE IF NOT EXISTS c (k TEXT PRIMARY KEY, t REAL, v TEXT)")
    return con


def _public_url(path, params):
    """OC를 뺀 요청 URL. 응답의 source·오류문에는 이것만 쓴다."""
    return API + path + "?" + urllib.parse.urlencode(dict(params, type="JSON"))


def _get(path, params, ttl, refresh=False, valid=None):
    """JSON 응답과 공개용 URL. 캐시 키는 OC를 제외한 URL의 해시(같은 키로 여러 인증키가 캐시를 공유한다).
    valid(data)가 거짓인 응답(오류 JSON 등)은 캐시에 저장하지 않고, 캐시에 남은 것도 쓰지 않는다."""
    public = _public_url(path, params)
    key = hashlib.sha1(public.encode()).hexdigest()
    con = _db()
    if not refresh:
        row = con.execute("SELECT t, v FROM c WHERE k=?", (key,)).fetchone()
        if row and time.time() - row[0] < ttl:
            cached = json.loads(row[1])
            if valid is None or valid(cached):
                return cached, public
    url = API + path + "?" + urllib.parse.urlencode(dict(params, OC=_oc(), type="JSON"))
    last = None
    for _ in range(3):
        try:
            with _HTTP_SLOTS:
                raw = urllib.request.urlopen(url, timeout=45).read().decode("utf-8", "ignore")
            if raw.lstrip().startswith("<"):
                raise RuntimeError("API가 HTML/XML을 반환(JSON 아님)")
            data = json.loads(raw)
            if valid is None or valid(data):
                con.execute("REPLACE INTO c VALUES (?,?,?)", (key, time.time(), raw))
                con.commit()
            return data, public
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise RuntimeError(f"법제처 API 호출 실패({type(last).__name__}: {str(last)[:120]}) — {public}")


# ---------- helpers ----------
def _aslist(x):
    return [] if x is None else (x if isinstance(x, list) else [x])


def _flat(x, out):
    if isinstance(x, str):
        out.append(x)
    elif isinstance(x, list):
        for i in x:
            _flat(i, out)
    elif isinstance(x, dict):
        for k in ("조문내용", "항내용", "호내용", "목내용"):
            if k in x:
                _flat(x[k], out)
        for k in ("항", "호", "목"):
            if k in x:
                _flat(x[k], out)


def _text(x):
    out = []
    _flat(x, out)
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def _jo(s):
    """'제103조의30' / '103의30' / '103조의30' / '010330' → '010330'. 그 외 형식은 거부."""
    s = re.sub(r"\s+", "", str(s))
    if re.fullmatch(r"\d{6}", s):
        n, b = int(s[:4]), int(s[4:])
    else:
        m = re.fullmatch(r"제?(\d{1,4})조?(?:의(\d{1,2}))?", s)
        if not m:
            raise ValueError(f"조문 번호 형식이 아님: {s!r} (예: 제73조, 제103조의30, 010330)")
        n, b = int(m.group(1)), int(m.group(2) or 0)
    if not (1 <= n <= 9999):
        raise ValueError(f"조 번호 범위 밖: {n}")
    return f"{n:04d}{b:02d}"


def _jo_label(n, b):
    n, b = int(n or 0), int(b or 0)
    return f"제{n}조" + (f"의{b}" if b else "")


def _link(name, jo=None):
    u = "https://www.law.go.kr/법령/" + urllib.parse.quote(name)
    if jo:
        u += "/" + urllib.parse.quote(jo)
    return u


def _efyd(s):
    s = str(s).replace("-", "").replace(".", "").strip()
    if not re.fullmatch(r"(19|20)\d{6}", s):
        raise ValueError(f"시행일은 YYYYMMDD 형식이어야 함: {s!r}")
    return s


# ---------- search / resolve ----------
_SEARCH_MEMO, _MEMO_LOCK, _KEY_LOCKS = {}, threading.Lock(), {}


def _is_law_search(d):
    return isinstance(d, dict) and isinstance(d.get("LawSearch"), dict)


def _has_basic_info(d):
    law = d.get("법령") if isinstance(d, dict) else None
    return isinstance(law, dict) and isinstance(law.get("기본정보"), dict)


def _search_all(name, target, refresh=False):
    """정확 일치하는 법령의 판 목록 전부(페이지네이션). 없으면 후보와 함께 LookupError.
    첫 쪽의 totalCnt로 나머지 쪽을 병렬로 받고, 같은 프로세스 안에서는 결과를 다시 쓴다."""
    key = (name, target)
    with _MEMO_LOCK:
        key_lock = _KEY_LOCKS.setdefault(key, threading.Lock())
    with key_lock:  # 같은 법령을 동시에 찾는 작업은 한 번만 받고 나머지는 기다렸다 쓴다
        if not refresh:
            with _MEMO_LOCK:
                hit = _SEARCH_MEMO.get(key)
            if hit and time.time() - hit[0] < TTL_VERSIONS:
                return list(hit[1])
        return _search_all_fetch(name, target, key, refresh)


def _search_all_fetch(name, target, key, refresh):
    fetch = lambda page: _aslist(_get("lawSearch.do", dict(target=target, query=name, display=PAGE, page=page), TTL_VERSIONS, refresh, valid=_is_law_search)[0]
                                 .get("LawSearch", {}).get("law"))
    first, _ = _get("lawSearch.do", dict(target=target, query=name, display=PAGE, page=1), TTL_VERSIONS, refresh, valid=_is_law_search)
    pages = [_aslist(first.get("LawSearch", {}).get("law"))]
    try:
        total = int(first.get("LawSearch", {}).get("totalCnt") or 0)
    except (TypeError, ValueError):
        total = 0
    if total > PAGE:
        last = min(30, -(-total // PAGE))  # 3,000판 이상은 없다고 본다
        with ThreadPoolExecutor(WORKERS) as pool:
            pages += list(pool.map(fetch, range(2, last + 1)))
    elif len(pages[0]) == PAGE:  # totalCnt가 없으면 예전처럼 차례로
        page = 2
        while page <= 30:
            laws = fetch(page); pages.append(laws)
            if len(laws) < PAGE:
                break
            page += 1
    rows, cands = [], []
    for laws in pages:
        for l in laws:
            if l.get("법령명한글") == name:
                rows.append(l)
            elif l.get("법령명한글") and l["법령명한글"] not in cands:
                cands.append(l["법령명한글"])
    if rows:
        with _MEMO_LOCK:
            _SEARCH_MEMO[key] = (time.time(), list(rows))
    if not rows:
        raise LookupError(f"'{name}' 정확 일치 없음. 법제처 표기와 같아야 한다. 후보: {cands[:10]}")
    return rows


def find_law(name, refresh=False):
    """현행 법령 → dict(mst, efYd, pubDate, pubNo, name)."""
    l = _search_all(name, "law", refresh)[0]
    return dict(mst=l["법령일련번호"], efYd=l["시행일자"], pubDate=l["공포일자"], pubNo=l["공포번호"], name=name)


def law_versions(name, refresh=False):
    """시행일 기준 연혁(eflaw). 시행일 내림차순, 같은 시행일은 공포일·MST 내림차순."""
    rows = _search_all(name, "eflaw", refresh)
    rows.sort(key=lambda l: (l.get("시행일자", ""), l.get("공포일자", ""), l.get("법령일련번호", "")), reverse=True)
    return dict(name=name, count=len(rows), versions=[dict(mst=l["법령일련번호"], efYd=l["시행일자"], pubDate=l["공포일자"], pubNo=l["공포번호"], kind=l.get("제개정구분명")) for l in rows],
                source=_public_url("lawSearch.do", dict(target="eflaw", query=name, display=PAGE)))


def resolve_mst(name, efYd=None, refresh=False):
    """조회할 판을 정한다. efYd가 있으면 그 날짜에 시행 중이던 판(시행일 ≤ efYd 중 최신)."""
    if not efYd:
        info = find_law(name, refresh)
        note = "현행(법제처 현행 표시판)"
        # 법제처 '현행' 표시는 시행일이 가장 늦은 판에 붙는다. 그 판보다 늦게 공포됐지만 먼저 시행된 개정(예: 2025.12.23 공포·2026.1.1 시행)의
        # 문구가 그 판 본문에 안 들어 있을 수 있다(2026-09-17 실측: 부가가치세법 §60 3%→4%). 시행 중인 판 중 공포일이 가장 늦은 판이 다르면 알린다.
        try:
            today = time.strftime("%Y%m%d")
            inforce = [v for v in law_versions(name, refresh)["versions"] if v["efYd"] <= today]
            if inforce:
                latest_pub = max(inforce, key=lambda v: (v["pubDate"], v["mst"]))
                if latest_pub["mst"] != info["mst"]:
                    note += (f" — 주의: 시행 중인 판 중 공포일이 가장 늦은 판은 MST {latest_pub['mst']}(공포 {latest_pub['pubDate']}, 시행 {latest_pub['efYd']})로 다르다. "
                             f"이 판의 개정 문구가 현행 표시판에 없을 수 있으니 결론 조문은 efYd={latest_pub['efYd']}로 한 번 더 열어 대조할 것")
        except Exception:
            pass
        return info["mst"], info["efYd"], note
    efYd = _efyd(efYd)
    vers = [v for v in law_versions(name, refresh)["versions"] if v["efYd"] <= efYd]
    if not vers:
        raise LookupError(f"{name}: {efYd} 이전에 시행된 판이 연혁에 없음")
    same = [v for v in vers if v["efYd"] == vers[0]["efYd"]]
    note = f"시행 {vers[0]['efYd']} 판(MST {vers[0]['mst']})"
    if len(same) > 1:
        note += f" — 같은 시행일 판 {len(same)}개 중 공포일·MST가 가장 늦은 것을 골랐다: " + ", ".join(v["mst"] for v in same)
    return vers[0]["mst"], vers[0]["efYd"], note


def _units(mst, refresh=False):
    d, url = _get("lawService.do", dict(target="law", MST=mst), TTL_LAW, refresh, valid=_has_basic_info)
    law = d.get("법령", {})
    units = _aslist(law.get("조문", {}).get("조문단위"))
    return law, [u for u in units if u.get("조문여부") == "조문"] or units, url


# ---------- core ----------
def _select_effective(name, efYd=None, refresh=False):
    """Select the requested day's effective edition; omission means today.

    This dated-body path differs from resolve_mst's current-display default.
    The request date is not the API's edition effective-date parameter.
    """
    requested = _efyd(efYd) if efYd is not None else time.strftime("%Y%m%d")
    datetime.strptime(requested, "%Y%m%d")
    rows = [v for v in law_versions(name, refresh)["versions"] if _efyd(v["efYd"]) <= requested]
    rows.sort(key=lambda v: (v["efYd"], v["pubDate"], int(v["mst"])), reverse=True)
    if not rows:
        raise LookupError(f"{name}: {requested} 이전 시행판 미확보; 현행판으로 대체하지 않음")
    selected = dict(rows[0], requestedDate=requested)
    note = f"시행 {selected['efYd']} 판(MST {selected['mst']}); 요청 기준일 {requested}; 시행일 본문 조회; 부칙 적용은 별도 확인"
    same = [v for v in rows if v["efYd"] == selected["efYd"]]
    if len(same) > 1:
        note += f"; 같은 시행일 {len(same)}판 중 공포일·MST 최신 선택"
    return selected, note

def _effective_body(name, info, jo6=None, refresh=False):
    params = dict(target="eflaw", MST=info["mst"], efYd=info["efYd"])
    if jo6 is not None:
        params['JO'] = jo6
    data, url = _get("lawService.do", params, TTL_FIXED_BODY, refresh, valid=_has_basic_info)
    law = data.get("법령") if isinstance(data, dict) else None
    basic = law.get("기본정보") if isinstance(law, dict) else None
    if not isinstance(basic, dict):
        raise LookupError("시행일 본문 기본정보 미확보; 공포판·현행판으로 대체하지 않음")
    expected = {"법령명_한글": name, "시행일자": info["efYd"],
                "공포일자": info["pubDate"], "공포번호": info["pubNo"]}
    if any(str(basic.get(key, "")) != str(value) for key, value in expected.items()):
        raise LookupError("시행일 본문의 법령명·시행일·공포 정보가 선택 연혁과 불일치; 대체하지 않음")
    container = law.get("조문", {})
    units = _aslist(container.get("조문단위")) if isinstance(container, dict) else []
    articles = []
    for unit in units:
        if not isinstance(unit, dict) or unit.get('조문여부') not in ('조문', '전문'):
            raise LookupError('조문 단위 형식 또는 조문여부 미확인; 일부 단위를 버리고 검색하지 않음')
        if unit['조문여부'] == '조문':
            articles.append(unit)
        elif not re.match(r'^제\d+(?:편|장|절|관)(?:의[1-9]\d*)?\s', _text(unit)):
            raise LookupError('장·절 등 제목으로 확인되지 않은 비조문 단위; 조용히 제외하지 않음')
    units = articles
    expected_number = (int(jo6[:4]), int(jo6[4:])) if jo6 is not None else None
    for unit in units:
        try:
            number = (int(unit.get("조문번호")), int(unit.get("조문가지번호") or 0))
        except (ValueError, TypeError):
            raise LookupError("시행일 본문 조문 식별자 미확보") from None
        if number[0] < 1 or number[1] < 0:
            raise LookupError("시행일 본문 조문 식별자 범위 오류")
        if expected_number is not None and number != expected_number:
            raise LookupError("요청과 다른 조문 반환; 해당 조문으로 대체하지 않음")
    if not units or any(not _text(unit) for unit in units):
        raise LookupError("시행일 본문 조문 목록 또는 본문 미확보")
    if jo6 is not None and len(units) != 1:
        raise LookupError("요청 조문 단일 본문 미확보; 적용 여부는 부칙과 별도 확인")
    return law, units, url

def _image_links(unit):
    """Preserve image URLs without implying image retrieval or visual inspection."""
    strings = []
    def visit(value):
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            for child in value.values(): visit(child)
        elif isinstance(value, list):
            for child in value: visit(child)
    visit(unit)
    return sorted(set(url for text in strings for url in re.findall(
        r'<img\b[^>]*\bsrc=[\"\x27](https?://[^\"\x27<>]+)', text, re.I)))


def law_article(name, jo, efYd=None, refresh=False):
    """Effective-date article body; absent efYd means today's effective edition."""
    jo6 = _jo(jo)
    info, note = _select_effective(name, efYd, refresh)
    law, units, url = _effective_body(name, info, jo6, refresh)
    basic = law.get("기본정보", {})
    parts = [dict(label=_jo_label(u.get("조문번호"), u.get("조문가지번호")), title=u.get("조문제목"),
                  efYd=u.get("조문시행일자"), text=_text(u), image_links=_image_links(u),
                  image_visual_check_performed=False) for u in units]
    return dict(name=name, jo=parts[0]["label"], version=note, mst=info["mst"], efYd=info["efYd"],
                requestedDate=info["requestedDate"], pubDate=basic["공포일자"], pubNo=basic["공포번호"],
                retrieval_view="effective_date_body", source_metadata_matched=True,
                legal_application_verified=False, history_coverage_verified=False,
                articles=parts, link=_link(name, parts[0]["label"]), source=url)


def law_articles(requests, refresh=False):
    """One dispatch for explicit dated articles; preserve each individual result.

    Requests run in parallel (LAWKIT_WORKERS). This neither reduces HTTP requests nor
    establishes legal applicability, reading, or completeness of a history range.
    """
    if not isinstance(requests, list) or not 1 <= len(requests) <= 30:
        raise ValueError('requests must contain 1..30 items; split larger lists explicitly')
    if not isinstance(refresh, bool):
        raise ValueError('refresh must be boolean')
    checked = []
    for index, request in enumerate(requests):
        if not isinstance(request, dict) or set(request) != {'name', 'jo', 'efYd'}:
            raise ValueError(f'requests[{index}] requires exactly name, jo, efYd')
        if any(not isinstance(request[k], str) or not request[k].strip() for k in request):
            raise ValueError(f'requests[{index}] fields must be nonempty strings')
        if not re.fullmatch(r'[0-9]{8}', request['efYd']):
            raise ValueError(f'requests[{index}].efYd must be YYYYMMDD')
        datetime.strptime(request['efYd'], '%Y%m%d')
        _jo(request['jo'])
        checked.append(dict(request))
    def one(pair):
        index, request = pair
        item = dict(index=index, request=request)
        try:
            item.update(status='ok', result=law_article(**request, refresh=refresh))
        except Exception as error:
            item.update(status='error', error=dict(type=type(error).__name__, message=str(error)))
        return item
    with ThreadPoolExecutor(WORKERS) as pool:  # 순서는 요청 순서 그대로
        items = list(pool.map(one, enumerate(checked)))
    succeeded = sum(item['status'] == 'ok' for item in items)
    return dict(items=items, requested_count=len(items), succeeded_count=succeeded,
                failed_count=len(items)-succeeded, all_requests_succeeded=succeeded == len(items),
                legal_application_verified=False, history_coverage_verified=False)


def law_addenda(name, keywords=None, require=None, last=None, limit=20, max_chars=4000, refresh=False, mst=None, efYd=None):
    """부칙 목록. require = 전부 들어 있어야 하는 단어(AND), keywords = 하나라도 들어 있으면(OR). 둘 다 주면 require AND keywords."""
    if mst is not None and efYd is not None:
        raise ValueError("mst와 efYd는 함께 지정할 수 없음")
    for label, value in [('limit', limit), ('max_chars', max_chars), ('last', last)]:
        if (value is None and label != 'last') or (value is not None and (type(value) is not int or value < 1)):
            raise ValueError(label + "은 양의 정수여야 함")
    requested_date = None
    if mst is not None:
        if isinstance(mst, bool) or not re.fullmatch(r'[1-9][0-9]{0,11}', str(mst)):
            raise ValueError("mst는 양의 정수 판 번호여야 함")
        matches = [v for v in law_versions(name, refresh)['versions'] if str(v['mst']) == str(mst)]
        if not matches:
            raise LookupError("명시한 MST의 연혁 공포정보 미확보; 다른 판으로 대체하지 않음")
        if len({(v.get('pubDate'), v.get('pubNo')) for v in matches}) != 1:
            raise LookupError("같은 MST의 연혁 공포정보 불일치")
        info = dict(matches[0], efYd=None)
        selection = 'explicit_publication'
    elif efYd is not None:
        info, _ = _select_effective(name, efYd, refresh)
        requested_date = info['requestedDate']
        selection = 'requested_effective_date'
    else:
        info = find_law(name, refresh)
        selection = 'current_display_publication'
    d, url = _get("lawService.do", dict(target="law", MST=info["mst"]), TTL_LAW, refresh)
    law = d.get('법령') if isinstance(d, dict) else None
    basic = law.get('기본정보') if isinstance(law, dict) else None
    expected = {'법령명_한글': name, '공포일자': info.get('pubDate'), '공포번호': info.get('pubNo')}
    if (not isinstance(basic, dict) or any(not value for value in expected.values())
            or any(str(basic.get(key, '')) != str(value) for key, value in expected.items())):
        raise LookupError("부칙 원응답의 법령명·공포정보가 선택판과 불일치 또는 미확보")
    container = law.get('부칙')
    if not isinstance(container, dict) or '부칙단위' not in container:
        raise LookupError("부칙 응답 구조 미확보; 부칙 없음으로 처리하지 않음")
    raw_units = container['부칙단위']
    if not isinstance(raw_units, (dict, list)):
        raise LookupError("부칙단위 응답 형식 미확인")
    bu = _aslist(raw_units)
    if any(not isinstance(b, dict) or not _text(b.get('부칙내용')) for b in bu):
        raise LookupError("부칙 본문 누락 또는 빈 본문; 부칙 없음으로 처리하지 않음")
    rows = [dict(pubNo=b.get("부칙공포번호"), pubDate=b.get("부칙공포일자"), text=_text(b.get("부칙내용"))) for b in bu]
    rows.sort(key=lambda r: (r["pubDate"] or ""), reverse=True)
    total = len(rows)
    if last:
        rows = rows[: int(last)]
    after_last = len(rows)
    req = [k for k in (require or []) if k]
    kws = [k for k in (keywords or []) if k]
    if req:
        rows = [r for r in rows if all(k in r["text"] for k in req)]
    if kws:
        rows = [r for r in rows if any(k in r["text"] for k in kws)]
    matched_total = len(rows)
    for r in rows:
        hits = []
        for k in req + kws:
            for m in re.finditer(re.escape(k), r["text"]):
                a = max(0, m.start() - 120)
                hits.append(r["text"][a:m.end() + 220])
                if len(hits) >= 6:
                    break
        r["hits"] = hits
        r['original_text_length'] = len(r['text'])
        r['text_truncated'] = len(r['text']) > max_chars
        if r['text_truncated']:
            r["text"] = r["text"][:max_chars] + f" …[{len(r['text']) - max_chars}자 생략 — max_chars를 늘려 다시 조회]"
    truncated = len(rows) > int(limit)
    rows = rows[: int(limit)]
    note = ""
    if not rows:
        note = "매칭되는 부칙이 없다. '부칙 없음'이 아니라 '부칙 미확인'으로 적을 것 — require/keywords를 바꿔 다시 훑거나 조건 없이 전체를 읽어라."
    elif req and not kws:
        note = "require만 적용됨. 적용례·경과조치를 찾는 목적이면 keywords=['적용례','경과조치']를 함께 주어라."
    if truncated:
        note += f" 결과 {limit}건으로 잘림(limit 조정)."
    return dict(name=name, mst=info["mst"], efYd=basic.get('시행일자'), selected_efYd=info['efYd'],
                requestedDate=requested_date, selection=selection,
                pubDate=basic['공포일자'], pubNo=basic['공포번호'], retrieval_view='publication_addenda',
                source_metadata_matched=True, metadata_checked=list(expected), mst_identity_verified=False,
                applicable_text_verified=False, history_coverage_verified=False, legal_application_verified=False,
                total=total, count=len(rows), addenda=rows, note=note.strip(), link=_link(name), source=url,
                coverage=dict(response_structure='explicit_empty' if total == 0 else 'present',
                    omitted_by_last=total-after_last, excluded_by_keywords=after_last-matched_total,
                    omitted_by_limit=max(0, matched_total-len(rows)),
                    truncated_body_count=sum(r['text_truncated'] for r in rows),
                    full_stored_addenda_returned=total == len(rows) and not any(r['text_truncated'] for r in rows)),
                limitations=['공포판 부칙 조회이며 법적 적용 시점 검증이 아니다.',
                    'MST는 요청 URL과 연혁으로 연결했으며 원응답의 독립 MST 식별값으로 확인한 것은 아니다.',
                    'selected_efYd와 공포판 기본 시행일 efYd는 다를 수 있다.'])


def law_grep(name, keywords, mode="any", efYd=None, limit=40, refresh=False):
    """법령 전문을 조문 단위로 키워드 검색. efYd가 있으면 그 시행일 판을 훑는다."""
    if type(limit) is not int or limit < 1:
        raise ValueError('limit은 양의 정수여야 함')
    if mode not in ('any', 'all'):
        raise ValueError('mode는 any 또는 all이어야 함')
    kws = [k for k in keywords if k]
    if not kws:
        raise ValueError("keywords가 비어 있음")
    info, note = _select_effective(name, efYd, refresh)
    law, units, url = _effective_body(name, info, refresh=refresh)
    out = []
    for u in units:
        t = _text(u)
        ok = all(k in t for k in kws) if mode == "all" else any(k in t for k in kws)
        if not ok:
            continue
        snippets = []
        for k in kws:
            m = re.search(re.escape(k), t)
            if m:
                a = max(0, m.start() - 90)
                snippets.append(t[a:m.end() + 160])
        label = _jo_label(u.get("조문번호"), u.get("조문가지번호"))
        out.append(dict(jo=label, title=u.get("조문제목"), efYd=u.get("조문시행일자"), snippets=snippets[:3], link=_link(name, label)))
    returned = out[:limit]
    return dict(name=name, version=note, mst=info['mst'], efYd=info['efYd'], requestedDate=info['requestedDate'],
                pubDate=info['pubDate'], pubNo=info['pubNo'], retrieval_view='effective_date_body',
                source_metadata_matched=True, history_coverage_verified=False, legal_application_verified=False,
                keywords=kws, mode=mode, scanned=len(units), total_articles=len(units),
                matched=len(returned), matched_total=len(out), truncated=len(out)>limit, articles=returned, source=url)


def law_refs(name, jo, efYd=None, refresh=False):
    art = law_article(name, jo, efYd, refresh)
    text = " ".join(a["text"] for a in art["articles"])
    refs, seen = [], set()
    for m in re.finditer(r"(?:「([^」]+)」\s*)?제(\d+)조(?:의(\d+))?(?:제(\d+)항)?", text):
        law_name = m.group(1) or name
        label = _jo_label(m.group(2), m.group(3))
        key = (law_name, label)
        if key in seen or label == art["jo"]:
            continue
        seen.add(key)
        item = dict(law=law_name, jo=label, para=m.group(4))
        if law_name == name:
            try:
                a = law_article(name, label, efYd, refresh)
                item["title"] = a["articles"][0]["title"]
                item["head"] = a["articles"][0]["text"][:200]
            except Exception as e:
                item["error"] = str(e)[:120]
        refs.append(item)
    delegations = re.findall(r"(대통령령|재정경제부령|기획재정부령|행정안전부령)으로 정하는[^.。,]{0,40}", text)
    return dict(name=name, jo=art["jo"], version=art["version"], refs=refs, delegations=delegations[:20], link=art["link"])


NTS_AND_MAX = 300  # 본문 AND 계산 때 단어 하나의 본문 검색 건수 상한(=API 3쪽). 넘는 단어는 AND에서 빼고 note에 남긴다(속도)


def _nts_terms(query):
    """'가지급금 인정이자' → ['가지급금','인정이자'], 따옴표 구절은 한 덩어리로."""
    return [m.group(1) or m.group(2) for m in re.finditer(r'"([^"]+)"|(\S+)', query.strip())]


def _nts_page(query, sv, display, page, sort, refresh):
    data, src = _get("lawSearch.do", dict(target="ntsCgmExpc", query=query, search=sv, display=display, page=page, sort=sort), TTL_SEARCH, refresh,
                     valid=lambda d: isinstance(d, dict) and isinstance(d.get("CgmExpc"), dict))
    root = data.get("CgmExpc", {})
    return int(root.get("totalCnt") or 0), _aslist(root.get("cgmExpc")), src


def _nts_item(it):
    link = it.get("법령해석상세링크") or ""
    m = re.search(r"ntstDcmId=([0-9]+)", link)
    return dict(no=(it.get("안건번호") or "").strip(), title=(it.get("안건명") or "").strip(), date=it.get("해석일자", ""),
                ntstDcmId=m.group(1) if m else None, link=link)


def nts_search(query, scope="both", display=20, page=1, sort="ddes", refresh=False):
    """국세청 예규(법령해석) 검색. 법제처 lawSearch target=ntsCgmExpc의 검색범위 변수 search(1=안건명, 2=본문).
    2026-10-02 실측: 띄어쓰기는 제목 검색에서 AND, 본문 검색에서 OR로 동작한다('가지급금 인정이자' 제목 70 / 본문 861).
    그래서 본문은 단어(따옴표 구절은 한 덩어리)마다 따로 받아 안건번호로 교집합(AND)을 낸다. 단어 하나가 NTS_AND_MAX건을
    넘으면 교집합을 내지 않고 note로 알린다. 결과의 ntstDcmId는 nts_pack으로 본문을 받을 때 쓴다."""
    if scope not in ("both", "title", "body"):
        raise ValueError("scope는 both|title|body")
    display = max(1, min(int(display), PAGE))
    terms = _nts_terms(query)
    if not terms:
        raise ValueError("검색어가 비었다")
    totals, notes, sources, merged = {}, [], [], {}

    def add(it, label):
        k = it["no"] or it["link"]
        merged.setdefault(k, dict(it, matched=[]))
        if label not in merged[k]["matched"]:
            merged[k]["matched"].append(label)

    if scope in ("both", "title"):
        n, items, src = _nts_page(" ".join(terms), 1, display, page, sort, refresh)
        totals["title"] = n; sources.append(src)
        for it in items:
            add(_nts_item(it), "title")
    if scope in ("both", "body"):
        if len(terms) == 1:
            n, items, src = _nts_page(terms[0], 2, display, page, sort, refresh)
            totals["body"] = n; sources.append(src)
            for it in items:
                add(_nts_item(it), "body")
        else:
            per = {}
            with ThreadPoolExecutor(WORKERS) as pool:
                counted = list(pool.map(lambda t: _nts_page(t, 2, 1, 1, sort, refresh), terms))
            for t, (n, _, src) in zip(terms, counted):
                per[t] = n; sources.append(src)
            totals["body_per_term"] = per
            too_big = [t for t, n in per.items() if n > NTS_AND_MAX]
            use = [t for t in terms if t not in too_big]
            if too_big:
                notes.append(f"흔한 단어는 AND에서 뺌: {', '.join(f'{t} {per[t]}건' for t in too_big)}. 이 단어 포함 여부는 본문을 열어 확인할 것")
            if not use:
                totals["body"] = None
                notes.append("모든 단어가 너무 흔해 본문 AND 계산 불가. 더 구체적인 단어나 \"구절\"로 다시 검색할 것")
            else:
                totals["body_and_terms"] = use
                def term_rows(t):
                    pages = range(1, -(-per[t] // PAGE) + 1) if per[t] else []
                    rows = {}
                    for _, items, _ in (_nts_page(t, 2, PAGE, pg, sort, refresh) for pg in pages):
                        for it in items:
                            x = _nts_item(it); rows[x["no"] or x["link"]] = x
                    return rows
                with ThreadPoolExecutor(WORKERS) as pool:
                    sets = dict(zip(use, pool.map(term_rows, use)))
                keys = set.intersection(*(set(r) for r in sets.values()))
                totals["body"] = len(keys)
                inter = sorted((sets[use[0]][k] for k in keys), key=lambda x: x["date"], reverse=(sort == "ddes"))
                start = (page - 1) * display
                for x in inter[start:start + display]:
                    add(x, "body")
    items = sorted(merged.values(), key=lambda x: x["date"], reverse=(sort == "ddes"))
    if scope == "both" and isinstance(totals.get("body"), int) and totals["body"] > totals.get("title", 0):
        notes.append(f"본문 검색 {totals['body']}건 중 제목 검색에 걸리는 것은 {totals['title']}건. 제목 검색만 하면 나머지를 놓친다.")
    return dict(query=query, terms=terms, scope=scope, totals=totals, page=page, display=display, items=items, note=" / ".join(notes),
                how_to_read="본문 교집합에 실제 사용한 단어는 totals.body_and_terms와 note를 확인한다. 상한 초과로 제외한 단어까지 AND 검증한 결과가 아니다. 각 건 본문은 nts_pack(ids=[ntstDcmId…])으로 받는다. 목록만으로 인용 금지.",
                source=sources)

# ---------- 국세청 예규 본문(taxlaw.nts.go.kr) : 요약표·본문 묶음 ----------
NTS_CACHE = CACHE_DIR / "nts"
NTS_PACK_MAX = 20  # 한 번에 받는 예규 수 상한(국세청 사이트 대량 호출 금지)


def _nts_strip(h):
    if not h:
        return ""
    h = re.sub(r"<br\s*/?>", "\n", h)
    h = re.sub(r"</(p|div|tr|li)>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    import html as _html
    return re.sub(r"\n{3,}", "\n\n", _html.unescape(h)).strip()


def nts_doc(dcm_id, refresh=False):
    """예규 1건의 본문(요지·회신·질의내용 전문). 응답 원문을 ~/.cache/lawkit/nts/<id>.json 에 보관해 다시 받지 않는다"""
    if not re.fullmatch(r"[0-9]{6,24}", str(dcm_id)):
        raise ValueError(f"ntstDcmId 형식 오류: {dcm_id}")
    NTS_CACHE.mkdir(parents=True, exist_ok=True)
    f = NTS_CACHE / f"{dcm_id}.json"
    if f.exists() and not refresh:
        with f.open(encoding="utf-8") as cached:
            res = json.load(cached)
    else:
        body = urllib.parse.urlencode({"actionId": "ASIQTB002PR01", "paramData": json.dumps({"dcmDVO": {"ntstDcmId": str(dcm_id)}})}).encode()
        req = urllib.request.Request("https://taxlaw.nts.go.kr/action.do", data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        with urllib.request.urlopen(req, timeout=30) as response:
            res = json.load(response)
    if not isinstance(res, dict):
        raise LookupError("예규 본문 응답 형식 오류")
    d = (res.get("data") or {}).get("ASIQTB002PR01") or {}
    v = d.get("dcmDVO")
    if res.get("status") != "SUCCESS" or not v:
        raise LookupError(f"예규 본문 조회 실패({res.get('status')}): ntstDcmId={dcm_id}")
    if not isinstance(v, dict) or str(v.get("ntstDcmId", "")) != str(dcm_id):
        raise LookupError("예규 본문 요청·반환 식별자 불일치; 원문 확인 필요")
    if not f.exists() or refresh:
        with f.open("w", encoding="utf-8") as cached:
            json.dump(res, cached, ensure_ascii=False)
    q = "\n".join(_nts_strip(e.get("dcmFleByte")) for e in (d.get("dcmHwpEditorDVOList") or []) if "<html" in (e.get("dcmFleByte") or ""))
    return dict(id=str(dcm_id), no=v.get("ntstDcmDscmCntn") or "", title=v.get("ntstDcmTtl") or "", date=v.get("ntstDcmRgtDt") or "",
                gist=_nts_strip(v.get("ntstDcmGistCntn")), reply=_nts_strip(v.get("ntstDcmCntn")), query_text=q,
                link=f"https://taxlaw.nts.go.kr/qt/USEQTA002P.do?ntstDcmId={dcm_id}")


def _nts_section(text, heads, stops, n):
    """질의내용 전문에서 '사실관계'·'질의요지' 같은 소제목 아래를 n자까지."""
    for h in heads:
        m = re.search(h, text)
        if m:
            rest = text[m.end():]
            e = min([x.start() for x in (re.search(sp, rest) for sp in stops) if x] or [len(rest)])
            return re.sub(r"\s+", " ", rest[:e]).strip()[:n]
    return ""


def nts_pack(ids, out, full=False, refresh=False):
    """예규 여러 건을 받아 파일 하나로 쓴다. full=False면 요약표(사실관계·질의요지·요지 앞부분, 후보 고르기용),
    full=True면 본문 전체(인용·리뷰어 공유용). 같은 예규는 캐시에서 즉시 나온다. 결과 파일 경로와 건별 성공 여부를 돌려준다."""
    ids = [str(i) for i in dict.fromkeys(ids)]
    if not ids:
        raise ValueError("ids가 비었다")
    if len(ids) > NTS_PACK_MAX:
        raise ValueError(f"한 번에 {NTS_PACK_MAX}건까지(국세청 사이트 대량 호출 금지). 나눠서 부를 것")
    docs, fails = [], []
    for i in ids:
        try:
            docs.append(nts_doc(i, refresh))
        except Exception as e:
            fails.append(dict(id=i, error=f"{type(e).__name__}: {str(e)[:120]}"))
    lines = []
    if full:
        lines.append(f"# 국세청 예규 본문 묶음 ({len(docs)}건)\n")
        for d in docs:
            lines += [f"## {d['no']} ({d['date']}) {d['title']}", f"- 원문: {d['link']}", f"- ntstDcmId: {d['id']}", "", "### 요지", d["gist"], "", "### 회신·결정", d["reply"], "", "### 질의내용 전문", d["query_text"] or "(없음)", "", "---", ""]
    else:
        lines.append(f"# 국세청 예규 요약표 ({len(docs)}건) — 후보 고르기용. 인용은 본문 전체를 읽은 뒤에만\n")
        lines.append("판정 칸: 관련 / 애매(본문을 연다) / 무관(사유). 무관으로 버린 건도 이 파일에 남는다.\n")
        for d in docs:
            fact = _nts_section(d["query_text"], [r"사실\s*관계"], [r"\n\s*\d\.\s", r"질의\s*요지", r"질의\s*내용"], 350)
            ask = _nts_section(d["query_text"], [r"질의\s*요지", r"질의\s*내용"], [r"\n\s*\d\.\s", r"관련\s*법령", r"회신"], 250)
            flat = lambda x, n: re.sub(r"\s+", " ", x or "").strip()[:n]
            if not fact:
                fact = "(소제목 없음, 질의 앞부분) " + flat(d["query_text"], 350) if d["query_text"] else "(질의내용 전문 없음)"
            lines += [f"## {d['no']} ({d['date']})", f"- 제목: {d['title']}", f"- ntstDcmId: {d['id']}",
                      f"- 사실관계: {fact}", f"- 질의요지: {ask or '-'}", f"- 요지: {flat(d['gist'], 250)}", f"- 회신 앞부분: {flat(d['reply'], 250)}", "- 판정: ", ""]
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(out).write_text("\n".join(lines), encoding="utf-8")
    return dict(out=str(out), mode="full" if full else "triage", ok=[d["id"] for d in docs], failed=fails)


# ---------- MCP (stdio JSON-RPC, 외부 라이브러리 없음) ----------
_REFRESH = dict(type="boolean", description="캐시를 무시하고 다시 받는다(개정 직후)")
TOOLS = [
    dict(name="law_articles", description="명시한 (name, jo, efYd) 목록의 조문 전문을 한 번에 조회. 항목마다 기준일 YYYYMMDD 필수, 1~30건(기술 한도, 초과는 나눠 호출). 내부는 기존 law_article 순차 실행이며 원문·메타데이터 그대로 반환. items의 개별 status/error를 확인할 것. 다른 요청 실패가 성공 원문을 지우지 않는다. HTTP 절감·독해·법적 적용·연혁 전수 확인을 뜻하지 않는다.",
         inputSchema=dict(type="object", properties=dict(requests=dict(type="array", minItems=1, maxItems=30,
             items=dict(type="object", properties=dict(name=dict(type="string"), jo=dict(type="string"), efYd=dict(type="string", pattern="^[0-9]{8}$")),
                        required=["name", "jo", "efYd"], additionalProperties=False)), refresh=_REFRESH), required=["requests"])),
    dict(name="law_article", description="법령 조문 시행일 본문(JSON). name=법령명, jo='제73조'|'제103조의30', efYd=조회 기준일 YYYYMMDD. 생략하면 오늘까지 시행된 판을 연혁에서 선택(기존 현행 표시판 선택과 다름). 선택판의 법령명·시행일·공포정보·조문번호를 대조하고 불일치 시 실패. requestedDate는 요청 검토 기준일, efYd는 선택 시행판 날짜. 부칙 적용·중간 연혁 전수·법적 적용 검증은 별도.",
         inputSchema=dict(type="object", properties=dict(name=dict(type="string"), jo=dict(type="string"), efYd=dict(type="string"), refresh=_REFRESH), required=["name", "jo"])),
    dict(name="law_addenda", description="공포판 부칙 원문. mst=관련 개정 공포판(law_versions의 공포번호·공포일로 확인) 또는 efYd=요청 검토 기준일 중 하나만 지정. 현재 조문의 MST가 다른 개정 부칙까지 포함하지는 않음. 둘 다 생략하면 기존 현행 표시 공포판 사용. 법령명·공포정보 대조이며 법적 적용 검증 아님. selected_efYd와 공포판 efYd는 구분. require=AND, keywords=OR, last/limit/max_chars의 누락 범위는 coverage에 표시. 빈 결과는 부칙 미확인으로 적을 것.",
         inputSchema=dict(type="object", properties=dict(name=dict(type="string"), mst=dict(type="string"), efYd=dict(type="string"), require=dict(type="array", items=dict(type="string")), keywords=dict(type="array", items=dict(type="string")), last=dict(type="integer"), limit=dict(type="integer"), max_chars=dict(type="integer"), refresh=_REFRESH), required=["name"])),
    dict(name="law_grep", description="선택 시행판 전문의 조문 키워드 검색. keywords 배열, mode=any|all, efYd=요청 검토 기준일 YYYYMMDD, 생략하면 오늘까지 시행된 판. law_article과 같은 시행일 본문 경로. 전체 조문을 검색한 matched_total과 반환 matched, limit 잘림 truncated를 구분한다. snippets는 발췌이므로 결론 조문은 law_article로 원문 조회. 서식 의무는 법·령·규칙을 각각 확인.",
         inputSchema=dict(type="object", properties=dict(name=dict(type="string"), keywords=dict(type="array", items=dict(type="string")), mode=dict(type="string", enum=["any", "all"]), efYd=dict(type="string"), limit=dict(type="integer"), refresh=_REFRESH), required=["name", "keywords"])),
    dict(name="law_versions", description="법령의 시행일 연혁 전체(eflaw, 미시행 판 포함). law_article/law_grep의 efYd로 특정 판을 열 때 시행일을 여기서 고른다.",
         inputSchema=dict(type="object", properties=dict(name=dict(type="string"), refresh=_REFRESH), required=["name"])),
    dict(name="law_refs", description="조문 안의 참조 조문(같은 법·다른 법)과 위임 문구('대통령령으로 정하는')를 추출. 같은 법 참조는 제목·첫 200자를 붙인다.",
         inputSchema=dict(type="object", properties=dict(name=dict(type="string"), jo=dict(type="string"), efYd=dict(type="string"), refresh=_REFRESH), required=["name", "jo"])),
    dict(name="nts_search", description="국세청 예규를 제목+본문으로 검색하고 단어별 교집합을 낸다. query=검색어, scope=both(기본)|title|body, display≤100, page, sort=ddes(최신순)|dasc. 결과마다 matched(제목/본문)·ntstDcmId. 본문은 nts_pack으로 받아 읽고 인용.",
         inputSchema=dict(type="object", properties=dict(query=dict(type="string"), scope=dict(type="string", enum=["both", "title", "body"]), display=dict(type="integer"), page=dict(type="integer"), sort=dict(type="string", enum=["ddes", "dasc"]), refresh=_REFRESH), required=["query"])),
    dict(name="nts_pack", description="국세청 예규 여러 건(ids=ntstDcmId 배열, 20건까지)을 받아 파일 하나(out=경로)로 쓴다. full=false면 요약표(사실관계·질의요지·요지 앞부분: 후보 고르기용), full=true면 본문 전체(인용·리뷰어 공유용). 한 번 받은 예규는 캐시에서 즉시 나온다.",
         inputSchema=dict(type="object", properties=dict(ids=dict(type="array", items=dict(type="string")), out=dict(type="string"), full=dict(type="boolean"), refresh=_REFRESH), required=["ids", "out"])),
]


def _call(name, a):
    r = bool(a.get("refresh"))
    if name == "law_articles":
        return law_articles(a["requests"], a.get("refresh", False))
    if name == "law_article":
        return law_article(a["name"], a["jo"], a.get("efYd"), r)
    if name == "law_addenda":
        return law_addenda(a["name"], a.get("keywords"), a.get("require"), a.get("last"), a.get("limit", 20), a.get("max_chars", 4000), r, mst=a.get("mst"), efYd=a.get("efYd"))
    if name == "law_grep":
        return law_grep(a["name"], a["keywords"], a.get("mode", "any"), a.get("efYd"), a.get("limit", 40), r)
    if name == "law_versions":
        return law_versions(a["name"], r)
    if name == "law_refs":
        return law_refs(a["name"], a["jo"], a.get("efYd"), r)
    if name == "nts_pack":
        return nts_pack(a["ids"], a["out"], bool(a.get("full")), r)
    if name == "nts_search":
        return nts_search(a["query"], a.get("scope", "both"), a.get("display", 20), a.get("page", 1), a.get("sort", "ddes"), r)
    raise ValueError("unknown tool " + str(name))


def handle_rpc(line):
    """한 줄 JSON-RPC 요청 → 응답 dict 또는 None(알림). 테스트에서 직접 부른다."""
    try:
        req = json.loads(line)
    except Exception:
        return dict(jsonrpc="2.0", id=None, error=dict(code=-32700, message="parse error"))
    if not isinstance(req, dict):
        return dict(jsonrpc="2.0", id=None, error=dict(code=-32600, message="invalid request: object expected"))
    mid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        want = str(params.get("protocolVersion", ""))
        ver = want if want in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return dict(jsonrpc="2.0", id=mid, result=dict(protocolVersion=ver, capabilities=dict(tools={}), serverInfo=dict(name="lawkit", version="0.2.1")))
    if mid is None or (isinstance(method, str) and method.startswith("notifications/")):
        return None
    if method == "tools/list":
        return dict(jsonrpc="2.0", id=mid, result=dict(tools=TOOLS))
    if method == "tools/call":
        try:
            res = _call(params.get("name"), params.get("arguments") or {})
            return dict(jsonrpc="2.0", id=mid, result=dict(content=[dict(type="text", text=json.dumps(res, ensure_ascii=False, indent=1))]))
        except Exception as e:  # ConfigError·LookupError·ValueError·RuntimeError 전부 도구 오류로
            return dict(jsonrpc="2.0", id=mid, result=dict(content=[dict(type="text", text=f"[lawkit 오류] {type(e).__name__}: {e}")], isError=True))
    if method == "ping":
        return dict(jsonrpc="2.0", id=mid, result={})
    return dict(jsonrpc="2.0", id=mid, error=dict(code=-32601, message="method not found: " + str(method)))


def serve_mcp():
    for s in (sys.stdin, sys.stdout):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        resp = handle_rpc(line)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


# ---------- selftest: 오프라인 단위검사 → 네트워크 preflight → 실 API ----------
def _unit_checks():
    checks = [
        ("_jo 정상 경계", lambda: _jo("제1조") == "000100" and _jo("제100조의2") == "010002" and _jo("103의30") == "010330" and _jo("010330") == "010330"),
        ("_jo 비정상 거부", lambda: all(_raises(_jo, s) for s in ["제1조abc", "제1조제2항", "1.5", "제0조", "제12345조"])),
        ("_efyd 검증", lambda: _efyd("2026-01-02") == "20260102" and _raises(_efyd, "0102")),
        ("공개 URL에 인증키 없음", lambda: "OC=" not in _public_url("lawService.do", dict(target="law", MST="1", JO="000100"))),
        ("MCP initialize 미지원 버전 협상", lambda: handle_rpc(json.dumps(dict(jsonrpc="2.0", id=1, method="initialize", params=dict(protocolVersion="2099-01-01"))))["result"]["protocolVersion"] == SUPPORTED_PROTOCOLS[0]),
        ("MCP parse error -32700", lambda: handle_rpc("{bad")["error"]["code"] == -32700),
        ("MCP 배열 요청 -32600", lambda: handle_rpc("[1,2]")["error"]["code"] == -32600),
        ("MCP 알림 무응답", lambda: handle_rpc(json.dumps(dict(jsonrpc="2.0", method="notifications/initialized"))) is None),
        ("MCP unknown tool → isError", lambda: handle_rpc(json.dumps(dict(jsonrpc="2.0", id=2, method="tools/call", params=dict(name="nope", arguments={}))))["result"]["isError"] is True),
    ]
    ok = True
    for label, fn in checks:
        try:
            r = bool(fn())
        except Exception as e:
            r = False
            label += f" ({e})"
        ok &= r
        print(("PASS " if r else "FAIL ") + label)
    return ok


def _raises(fn, *a):
    try:
        fn(*a)
        return False
    except (ValueError, LookupError):
        return True


def _live_checks():
    checks = [
        ("법인세법 제73조에 비영업대금 25%", lambda: "100분의 25" in law_article("법인세법", "제73조")["articles"][0]["text"]),
        ("지방세법 제103조의30 미러링 10%", lambda: "100분의 10" in law_article("지방세법", "제103조의30")["articles"][0]["text"]),
        ("시행규칙 §43 2026.1.2 개정 부칙 = 직제 명칭 정비 (require+keywords)", lambda: any("재정경제부" in r["text"] for r in law_addenda("법인세법 시행규칙", keywords=["직제", "적용례"], require=["제43조"], last=8)["addenda"])),
        ("부가가치세법 본문 grep: 등록 전 매입세액", lambda: any(a["jo"] == "제39조" for a in law_grep("부가가치세법", ["등록", "공제하지"], mode="all")["articles"])),
        ("시행규칙 §43 2025-12-31 시행판은 '기획재정부령'(과거 판 선택)", lambda: "기획재정부령" in law_article("법인세법 시행규칙", "제43조", "20251231")["articles"][0]["text"]),
        ("law_grep efYd도 과거 판을 훑음", lambda: law_grep("법인세법 시행규칙", ["기획재정부령으로 정하는 당좌대출이자율"], efYd="20251231")["matched"] >= 1),
        ("연혁 페이지네이션: 법인세법 50건 초과", lambda: law_versions("법인세법")["count"] > 50),
        ("법인세법 시행령 §89 참조 추적에 제88조", lambda: any(r["jo"] == "제88조" for r in law_refs("법인세법 시행령", "제89조")["refs"])),
        ("JO 조회 시 장·절 단위 제외(법인세법 제71조 제목)", lambda: law_article("법인세법", "제71조")["articles"][0]["title"] == "징수 및 환급"),
        ("국세청 예규 본문 검색이 제목 검색보다 넓음(가지급금)", lambda: (lambda r: r["totals"]["body"] > r["totals"]["title"] > 0 and any("body" in i["matched"] and "title" not in i["matched"] for i in r["items"]) and all(i["ntstDcmId"] for i in r["items"]))(nts_search("가지급금", display=50))),
        ("예규 본문 검색 여러 단어 = AND(외국자회사 익금불산입 ≤ 외국자회사 단독)", lambda: (lambda r: isinstance(r["totals"]["body"], int) and 0 < r["totals"]["body"] <= r["totals"]["body_per_term"]["외국자회사"])(nts_search("외국자회사 익금불산입", scope="body"))),
        ("흔한 단어는 AND에서 빠지고 note에 남음(가지급금 642건)", lambda: "AND에서 뺌" in nts_search("가지급금 인정이자", scope="body")["note"]),
        ("예규 요약표에 사실관계가 뽑힘(서면-2024-법규국조-3783)", lambda: "A법인은 해외소재 지주회사" in (lambda p: (nts_pack(["200000000000013380"], p), open(p, encoding="utf-8").read())[1])(str(CACHE_DIR / "selftest_triage.md"))),
        ("조문 기준일 생략: 오늘까지 시행된 판의 시행일 본문", lambda: (lambda r: r["requestedDate"] == time.strftime("%Y%m%d") and r["efYd"] <= r["requestedDate"] and r["source_metadata_matched"] and "target=eflaw" in r["source"])(law_article("부가가치세법", "제60조"))),
    ]
    ok = True
    for label, fn in checks:
        try:
            r = bool(fn())
            ok &= r
            print(("PASS " if r else "FAIL ") + label)
        except Exception as e:
            ok = False
            print(f"ERR  {label}: {type(e).__name__}: {str(e)[:160]}")
    return ok


def selftest():
    print("[1/3] 오프라인 단위검사")
    ok = _unit_checks()
    print("[2/3] 네트워크 preflight")
    try:
        _oc()
    except ConfigError as e:
        print("SKIP 실 API 검사 — " + str(e))
        return 0 if ok else 1
    try:
        urllib.request.urlopen("https://www.law.go.kr/DRF/", timeout=15)
        print("PASS law.go.kr 연결")
    except Exception as e:
        print(f"SKIP 실 API 검사 — 네트워크 불가({type(e).__name__}). 단위검사 결과만 유효")
        return 0 if ok else 1
    print("[3/3] 실 API 검사(기준선 사건 기반)")
    ok &= _live_checks()
    print("selftest", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mcp", action="store_true"); ap.add_argument("--selftest", action="store_true"); ap.add_argument("--refresh", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("article"); p.add_argument("name"); p.add_argument("jo"); p.add_argument("--efyd")
    p = sub.add_parser("addenda"); p.add_argument("name"); p.add_argument("--grep", nargs="*"); p.add_argument("--require", nargs="*"); p.add_argument("--last", type=int); p.add_argument("--limit", type=int, default=20)
    p.add_argument("--max-chars", type=int, default=4000)
    date_group = p.add_mutually_exclusive_group(); date_group.add_argument("--mst"); date_group.add_argument("--efyd")
    p = sub.add_parser("grep"); p.add_argument("name"); p.add_argument("keywords", nargs="+"); p.add_argument("--all", action="store_true"); p.add_argument("--efyd")
    p = sub.add_parser("versions"); p.add_argument("name")
    p = sub.add_parser("refs"); p.add_argument("name"); p.add_argument("jo"); p.add_argument("--efyd")
    p = sub.add_parser("pack"); p.add_argument("ids", nargs="+"); p.add_argument("--out", required=True); p.add_argument("--full", action="store_true")
    p = sub.add_parser("nts"); p.add_argument("query"); p.add_argument("--scope", default="both", choices=["both", "title", "body"]); p.add_argument("--display", type=int, default=20); p.add_argument("--page", type=int, default=1); p.add_argument("--sort", default="ddes", choices=["ddes", "dasc"])
    a = ap.parse_args()
    if a.mcp:
        return serve_mcp()
    if a.selftest:
        return selftest()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        if a.cmd == "article": r = law_article(a.name, a.jo, a.efyd, a.refresh)
        elif a.cmd == "addenda": r = law_addenda(a.name, a.grep, a.require, a.last, a.limit, a.max_chars, refresh=a.refresh, mst=a.mst, efYd=a.efyd)
        elif a.cmd == "grep": r = law_grep(a.name, a.keywords, "all" if a.all else "any", a.efyd, refresh=a.refresh)
        elif a.cmd == "versions": r = law_versions(a.name, a.refresh)
        elif a.cmd == "refs": r = law_refs(a.name, a.jo, a.efyd, a.refresh)
        elif a.cmd == "pack": r = nts_pack(a.ids, a.out, a.full, a.refresh)
        elif a.cmd == "nts": r = nts_search(a.query, a.scope, a.display, a.page, a.sort, a.refresh)
        else:
            ap.print_help(); return 2
    except (ConfigError, LookupError, ValueError, RuntimeError) as e:
        print(f"[lawkit 오류] {type(e).__name__}: {e}", file=sys.stderr); return 1
    print(json.dumps(r, ensure_ascii=False, indent=1)); return 0


if __name__ == "__main__":
    sys.exit(main())
