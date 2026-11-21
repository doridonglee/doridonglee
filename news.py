"""키워드별 기사를 모아 news/ 폴더에 월별로 쌓고, 기사 본문을 읽어 요약을 붙입니다.

기사 모으기
  - 네이버 뉴스 검색 API 키가 있으면 네이버에서 모읍니다 (권장: 링크가 네이버 뉴스라 본문을 안정적으로 읽음)
  - 키가 없으면 Google 뉴스 RSS에서 모읍니다

요약
  - ANTHROPIC_API_KEY가 있으면 AI가 본문을 읽고 요약합니다
  - 없으면 기사 첫머리 1~2문장을 요약으로 씁니다

키 설정: 저장소 Settings → Secrets and variables → Actions → New repository secret
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, ANTHROPIC_API_KEY
외부 패키지 없이 파이썬 기본 기능만 씁니다.
"""
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_KEYWORDS = ["삼성전자", "삼성전자 노동조합"]
NEWS_DIR = "news"
KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

MAX_SUMMARIES_PER_RUN = 40      # 한 번 실행할 때 요약할 최대 기사 수
SUMMARY_TIME_BUDGET = 6 * 60     # 요약에 쓰는 최대 시간(초). 넘으면 남은 건 다음 실행으로 미룸
SUMMARY_LOOKBACK_DAYS = 3       # 이 기간 안의 기사만 요약 (오래된 기사는 건너뜀)
AI_MODEL = "claude-haiku-5-5"

NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "").strip()
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# 언론사 주소 → 이름 (네이버 API는 언론사 이름을 주지 않아서 주소로 찾음)
PRESS = {
    "yna.co.kr": "연합뉴스", "yonhapnewstv.co.kr": "연합뉴스TV", "news1.kr": "뉴스1", "newsis.com": "뉴시스",
    "hankyung.com": "한국경제", "mk.co.kr": "매일경제", "sedaily.com": "서울경제", "mt.co.kr": "머니투데이",
    "edaily.co.kr": "이데일리", "etnews.com": "전자신문", "dt.co.kr": "디지털타임스", "fnnews.com": "파이낸셜뉴스",
    "asiae.co.kr": "아시아경제", "heraldcorp.com": "헤럴드경제", "biz.chosun.com": "조선비즈", "chosun.com": "조선일보",
    "joongang.co.kr": "중앙일보", "donga.com": "동아일보", "hani.co.kr": "한겨레", "khan.co.kr": "경향신문",
    "hankookilbo.com": "한국일보", "kmib.co.kr": "국민일보", "seoul.co.kr": "서울신문", "segye.com": "세계일보",
    "munhwa.com": "문화일보", "nocutnews.co.kr": "노컷뉴스", "ohmynews.com": "오마이뉴스", "pressian.com": "프레시안",
    "ytn.co.kr": "YTN", "mbn.co.kr": "MBN", "kbs.co.kr": "KBS", "imbc.com": "MBC", "sbs.co.kr": "SBS",
    "jtbc.co.kr": "JTBC", "ichannela.com": "채널A", "tvchosun.com": "TV조선", "zdnet.co.kr": "지디넷코리아",
    "bloter.net": "블로터", "ddaily.co.kr": "디지털데일리", "thebell.co.kr": "더벨", "newspim.com": "뉴스핌",
    "inews24.com": "아이뉴스24", "ajunews.com": "아주경제", "bizwatch.co.kr": "비즈워치", "businesspost.co.kr": "비즈니스포스트",
    "etoday.co.kr": "이투데이", "wowtv.co.kr": "한국경제TV", "sentv.co.kr": "서울경제TV", "dailian.co.kr": "데일리안",
    "newdaily.co.kr": "뉴데일리", "labortoday.co.kr": "매일노동뉴스", "kukinews.com": "쿠키뉴스", "ebn.co.kr": "EBN",
}


# ─────────────────────────── 공통 ───────────────────────────
def http_get(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        charset = r.headers.get_content_charset()
        final = r.geturl()
    if not charset:
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:3000])
        charset = m.group(1).decode() if m else "utf-8"
    try:
        return raw.decode(charset, errors="replace"), final
    except LookupError:
        return raw.decode("utf-8", errors="replace"), final


def http_post(url, data, headers, timeout=45):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_tags(s):
    s = re.sub(r"(?s)<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def norm_title(t):
    return re.sub(r"[\s\W_]+", "", t).lower()


def make_id(title, source):
    return hashlib.sha1((norm_title(title) + "|" + source).encode()).hexdigest()[:12]


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_date(s):
    try:
        dt = email.utils.parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def press_from_url(url):
    host = urllib.parse.urlparse(url).hostname or ""
    host = host.lower().removeprefix("www.").removeprefix("m.")
    for domain, name in PRESS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return ""


def load_keywords():
    try:
        with open("config.json", encoding="utf-8") as f:
            kws = [k.strip() for k in json.load(f).get("keywords", []) if k.strip()]
            return kws or DEFAULT_KEYWORDS
    except (OSError, ValueError):
        return DEFAULT_KEYWORDS


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ─────────────────────────── 기사 모으기 ───────────────────────────
def collect_naver(keyword):
    q = urllib.parse.urlencode({"query": keyword, "display": 100, "sort": "date"})
    text, _ = http_get("https://openapi.naver.com/v1/search/news.json?" + q,
                       headers={"X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET})
    items = []
    for it in json.loads(text).get("items", []):
        title = strip_tags(it.get("title"))
        original = it.get("originallink") or it.get("link") or ""
        link = it.get("link") or original
        if not title or not link:
            continue
        source = press_from_url(original) or press_from_url(link)
        items.append({
            "title": title, "link": link, "original": original, "source": source,
            "published": iso(parse_date(it.get("pubDate"))), "keywords": [keyword],
            "snippet": strip_tags(it.get("description")),
        })
    return items


def collect_google(keyword):
    import xml.etree.ElementTree as ET
    q = urllib.parse.urlencode({"q": f"{keyword} when:2d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    text, _ = http_get("https://news.google.com/rss/search?" + q)
    items = []
    for it in ET.fromstring(text.encode()).iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = it.find("source")
        source = (src.text or "").strip() if src is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        if not title or not link:
            continue
        items.append({
            "title": title, "link": link, "source": source,
            "published": iso(parse_date(it.findtext("pubDate"))), "keywords": [keyword],
        })
    return items


# ─────────────────────────── 본문 읽기 ───────────────────────────
def decode_google_link(url):
    """Google 뉴스 RSS 주소를 실제 기사 주소로 바꿉니다. (실패하면 None)"""
    m = re.search(r"/articles/([^?/]+)", url)
    if not m:
        return None
    gid = m.group(1)
    page, _ = http_get(f"https://news.google.com/rss/articles/{gid}")
    sig = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sig and ts):
        return None
    inner = json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None,
                                         None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                        gid, int(ts.group(1)), sig.group(1)])
    body = urllib.parse.urlencode({"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])}).encode()
    resp = http_post("https://news.google.com/_/DotsSplashUi/data/batchexecute", body,
                     {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    data = json.loads(resp.split("\n\n", 1)[1])
    return json.loads(data[0][2])[1]


def meta(page, *names):
    for n in names:
        m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(n)}["\'][^>]+content=["\']([^"\']*)', page, re.I) \
            or re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(n)}["\']', page, re.I)
        if m and m.group(1).strip():
            return html.unescape(m.group(1).strip())
    return ""


def extract_body(page, url):
    """기사 페이지에서 본문 글자만 뽑습니다."""
    page = re.sub(r"(?is)<(script|style|noscript|iframe|figure|figcaption)\b.*?</\1>", " ", page)
    page = re.sub(r'(?is)<(em|span|div)[^>]+class="[^"]*(img_desc|caption|photo|byline|reporter)[^"]*"[^>]*>.*?</\1>', " ", page)
    body = ""
    # 네이버 뉴스
    m = re.search(r'(?is)<(article|div)[^>]+id="(dic_area|newsct_article|articeBody|articleBodyContents)"[^>]*>(.*?)</\1>', page)
    if m:
        body = m.group(3)
    # 일반 언론사: <article> 또는 본문으로 흔히 쓰는 id/class
    if not body:
        m = re.search(r'(?is)<(div|section)[^>]+(?:id|class)="[^"]*(article[_-]?body|articleBody|news_body|article_txt|article-text|view_con|news_content|article_view|story-news)[^"]*"[^>]*>(.*?)</\1>', page)
        if m:
            body = m.group(3)
    if not body:
        m = re.search(r"(?is)<article\b[^>]*>(.*?)</article>", page)
        if m:
            body = m.group(1)
    if body:
        body = re.sub(r"(?i)<br\s*/?>|</p>", "\n", body)
        text = strip_tags(body.replace("\n", " ⏎ ")).replace("⏎", "\n")
    else:
        paras = [strip_tags(p) for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", page)]
        text = "\n".join(p for p in paras if len(p) > 40)
    text = re.sub(r"\n\s*", "\n", text).strip()
    return text


def clean_body(text):
    """기자 이름, 지역 머리말 같은 군더더기를 지웁니다."""
    text = re.sub(r"^\s*[\[\(【][^\]\)】]{2,40}[\]\)】]\s*", "", text)               # [서울=뉴시스] 같은 머리말
    text = re.sub(r"^\s*\S{2,4}\s?기자\s*=\s*", "", text)                            # 홍길동 기자 =
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "", text)                               # 이메일
    text = re.sub(r"(무단\s*전재|재배포\s*금지|저작권자).*$", "", text, flags=re.S)  # 저작권 문구 이후
    return re.sub(r"[ \t]+", " ", text).strip()


def first_sentences(text, n=2, limit=200):
    sents = re.findall(r".+?(?:다|요|음|함)\.(?=\s|$)|.+?[.!?](?=\s|$)", text.replace("\n", " "))
    out = ""
    for s in sents[:n]:
        s = s.strip()
        if len(out) + len(s) > limit and out:
            break
        out = (out + " " + s).strip()
    return out[:limit].strip()


def brief_from_sentence(sentence):
    """'~했다.' 로 끝나는 첫 문장을 '~했다는 기사'로 바꿉니다."""
    s = sentence.strip()
    m = re.match(r"(.+다)\.$", s)
    if m and len(s) <= 120:
        return m.group(1) + "는 기사"
    return ""


def fetch_article(a):
    """기사 페이지를 읽어 (본문, 실제 주소, 언론사) 를 돌려줍니다."""
    url = a["link"]
    if "news.google.com" in url:
        real = decode_google_link(url)
        if not real:
            raise RuntimeError("Google 뉴스 주소를 풀지 못함")
        url = real
    page, final = http_get(url)
    body = clean_body(extract_body(page, final))
    if len(body) < 60:
        desc = meta(page, "og:description", "description")
        body = clean_body(desc) if len(desc) > len(body) else body
    source = a.get("source") or meta(page, "og:article:author", "article:publisher", "og:site_name")
    if not source or "네이버" in source:   # 네이버 뉴스: 상단 언론사 로고의 이름
        m = re.search(r'media_end_head_top_logo[^>]*>\s*<img[^>]+alt="([^"]+)"', page) or \
            re.search(r'<img[^>]+class="[^"]*media_end_head_top_logo_img[^"]*"[^>]+alt="([^"]+)"', page)
        if m:
            source = m.group(1)
    source = re.sub(r"\s*[|:]\s*네이버.*$", "", source).strip()
    if "네이버" in source:
        source = ""
    return body, final, source or press_from_url(final)


# ─────────────────────────── 요약 ───────────────────────────
AI_PROMPT = """아래 기사를 기업 노무·홍보 담당자가 빠르게 파악할 수 있게 요약하세요.

반드시 JSON 한 개만 출력하세요. 다른 말은 쓰지 마세요.
{{"summary": "핵심 내용 2~3문장. '~함', '~했음'으로 끝나는 개조식. 150자 이내. 숫자, 날짜, 주체를 살릴 것",
  "brief": "보고서 한 줄. '누가 무엇을 했다는 기사' 형태로 끝맺음. 60자 이내"}}

제목: {title}
언론사: {source}
본문:
{body}"""


def ai_summarize(title, source, body):
    payload = {
        "model": AI_MODEL,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": AI_PROMPT.format(title=title, source=source or "미상", body=body[:3500])}],
    }
    resp = http_post("https://api.anthropic.com/v1/messages", json.dumps(payload).encode(), {
        "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json",
    })
    text = "".join(c.get("text", "") for c in json.loads(resp).get("content", []))
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {}
    summary = str(data.get("summary", "")).strip()
    brief = str(data.get("brief", "")).strip()
    if not summary:
        raise RuntimeError("AI 응답에 요약이 없음")
    return summary, brief


def summarize(a):
    body, final, source = fetch_article(a)
    if final and "news.google.com" in a["link"]:
        a["link"] = final                       # 보고서에 실제 기사 주소가 들어가도록
    if source and not a.get("source"):
        a["source"] = source
    if len(body) < 40:
        body = a.get("snippet", "")
    if len(body) < 40:
        raise RuntimeError("본문을 찾지 못함")
    if ANTHROPIC_KEY:
        try:
            a["summary"], a["brief"] = ai_summarize(a["title"], a.get("source", ""), body)
            a["summaryBy"] = "ai"
            return
        except Exception as e:
            print(f"  AI 요약 실패, 첫머리로 대신함: {e}", file=sys.stderr)
    lead = first_sentences(body)
    a["summary"] = lead
    a["brief"] = brief_from_sentence(first_sentences(body, n=1, limit=140))
    a["summaryBy"] = "lead"


# ─────────────────────────── 실행 ───────────────────────────
def month_of(iso_str):
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m")


def main():
    keywords = load_keywords()
    use_naver = bool(NAVER_ID and NAVER_SECRET)
    collector = collect_naver if use_naver else collect_google
    print(f"수집 방식: {'네이버 뉴스 API' if use_naver else 'Google 뉴스 RSS'}, 요약: {'AI' if ANTHROPIC_KEY else '기사 첫머리'}")

    fresh, ok = [], 0
    for kw in keywords:
        try:
            got = collector(kw)
            fresh += got
            ok += 1
            print(f"[{kw}] {len(got)}건")
        except Exception as e:
            print(f"[{kw}] 수집 실패: {e}", file=sys.stderr)
    if ok == 0:
        print("모든 키워드 수집에 실패해서 기사 파일을 바꾸지 않습니다.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(NEWS_DIR, exist_ok=True)
    stores = {}

    def store_for(month):
        if month not in stores:
            path = os.path.join(NEWS_DIR, f"{month}.json")
            stores[month] = {a["id"]: a for a in read_json(path, {"items": []}).get("items", [])}
        return stores[month]

    # 언론사 이름이 비어 있는 네이버 기사는 본문을 읽을 때 채움 → id는 제목 기준으로 먼저 정함
    added = 0
    for a in fresh:
        a["id"] = make_id(a["title"], a.get("source") or urllib.parse.urlparse(a.get("original") or a["link"]).hostname or "")
        store = store_for(month_of(a["published"]))
        if a["id"] in store:
            kws = store[a["id"]].setdefault("keywords", [])
            if a["keywords"][0] not in kws:
                kws.append(a["keywords"][0])
        else:
            store[a["id"]] = a
            added += 1

    # 요약: 최근 기사 중 아직 요약이 없는 것부터
    cutoff = datetime.now(timezone.utc) - timedelta(days=SUMMARY_LOOKBACK_DAYS)
    todo = [a for st in stores.values() for a in st.values()
            if not a.get("summary") and a.get("summaryTries", 0) < 2
            and datetime.fromisoformat(a["published"].replace("Z", "+00:00")) >= cutoff]
    todo.sort(key=lambda a: a["published"], reverse=True)
    done, failed, started = 0, 0, time.time()
    for a in todo[:MAX_SUMMARIES_PER_RUN]:
        if time.time() - started > SUMMARY_TIME_BUDGET:
            print("  요약 시간 상한에 도달해서 나머지는 다음 실행으로 미룹니다")
            break
        try:
            summarize(a)
            done += 1
        except Exception as e:
            a["summaryTries"] = a.get("summaryTries", 0) + 1
            failed += 1
            if failed >= 8 and done == 0:
                print("  연속으로 본문을 읽지 못해 이번 실행의 요약을 멈춥니다 (네이버 키를 넣으면 대부분 해결됩니다)")
                break
            print(f"  요약 건너뜀 ({a.get('source') or '?'}) {a['title'][:30]}: {e}", file=sys.stderr)
        time.sleep(0.3)

    for month, store in stores.items():
        out = sorted(store.values(), key=lambda a: a["published"], reverse=True)
        for a in out:
            a.pop("original", None)
        with open(os.path.join(NEWS_DIR, f"{month}.json"), "w", encoding="utf-8") as f:
            json.dump({"month": month, "items": out}, f, ensure_ascii=False, indent=1)

    months = []
    for name in sorted(os.listdir(NEWS_DIR), reverse=True):
        m = re.fullmatch(r"(\d{4}-\d{2})\.json", name)
        if m:
            n = len(read_json(os.path.join(NEWS_DIR, name), {"items": []}).get("items", []))
            months.append({"month": m.group(1), "count": n})
    index = {
        "fetchedAt": iso(datetime.now(timezone.utc)),
        "keywords": keywords,
        "source": "naver" if use_naver else "google",
        "summarizer": "ai" if ANTHROPIC_KEY else "lead",
        "months": months,
    }
    with open(os.path.join(NEWS_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    print(f"새 기사 {added}건, 요약 {done}/{min(len(todo), MAX_SUMMARIES_PER_RUN)}건, 누적 {sum(m['count'] for m in months)}건")


if __name__ == "__main__":
    main()
