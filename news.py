"""Google 뉴스 RSS에서 키워드별 기사를 모아 news/ 폴더에 월별로 계속 쌓습니다.
키워드는 config.json의 keywords를 씁니다(관리자 화면에서 수정).
외부 패키지 없이 파이썬 기본 기능만 씁니다."""
import email.utils
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

DEFAULT_KEYWORDS = ["삼성전자", "삼성전자 노동조합"]
NEWS_DIR = "news"
KST = timezone(timedelta(hours=9))


def load_keywords():
    try:
        with open("config.json", encoding="utf-8") as f:
            kws = [k.strip() for k in json.load(f).get("keywords", []) if k.strip()]
            return kws or DEFAULT_KEYWORDS
    except (OSError, ValueError):
        return DEFAULT_KEYWORDS


def fetch(keyword):
    q = urllib.parse.urlencode({"q": f"{keyword} when:2d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    req = urllib.request.Request(
        "https://news.google.com/rss/search?" + q,
        headers={"User-Agent": "Mozilla/5.0 (compatible; union-tracker)"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def norm_title(t):
    return re.sub(r"[\s\W_]+", "", t).lower()


def parse(xml_bytes, keyword):
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = it.find("source")
        source = (src.text or "").strip() if src is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        pub = it.findtext("pubDate")
        try:
            dt = email.utils.parsedate_to_datetime(pub) if pub else datetime.now(timezone.utc)
        except (TypeError, ValueError):
            dt = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if not title or not link:
            continue
        items.append({
            # 같은 언론사의 같은 제목이면 같은 기사로 봄
            "id": hashlib.sha1((norm_title(title) + "|" + source).encode()).hexdigest()[:12],
            "title": title,
            "link": link,
            "source": source,
            "published": dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "keywords": [keyword],
        })
    return items


def month_of(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m")


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    keywords = load_keywords()
    fresh = []
    ok = 0
    for kw in keywords:
        try:
            fresh += parse(fetch(kw), kw)
            ok += 1
        except Exception as e:  # 한 키워드가 실패해도 나머지는 계속
            print(f"[{kw}] 수집 실패: {e}", file=sys.stderr)
    if ok == 0:
        print("모든 키워드 수집에 실패해서 기사 파일을 바꾸지 않습니다.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(NEWS_DIR, exist_ok=True)
    by_month = {}
    for a in fresh:
        by_month.setdefault(month_of(a["published"]), []).append(a)

    added = 0
    for month, items in by_month.items():
        path = os.path.join(NEWS_DIR, f"{month}.json")
        store = {a["id"]: a for a in read_json(path, {"items": []}).get("items", [])}
        for a in items:
            if a["id"] in store:
                kws = store[a["id"]].setdefault("keywords", [])
                if a["keywords"][0] not in kws:
                    kws.append(a["keywords"][0])
            else:
                store[a["id"]] = a
                added += 1
        out = sorted(store.values(), key=lambda a: a["published"], reverse=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"month": month, "items": out}, f, ensure_ascii=False, indent=1)

    months = []
    for name in sorted(os.listdir(NEWS_DIR), reverse=True):
        m = re.fullmatch(r"(\d{4}-\d{2})\.json", name)
        if m:
            n = len(read_json(os.path.join(NEWS_DIR, name), {"items": []}).get("items", []))
            months.append({"month": m.group(1), "count": n})
    index = {
        "fetchedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "keywords": keywords,
        "months": months,
    }
    with open(os.path.join(NEWS_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    print(f"새 기사 {added}건 추가 ({ok}/{len(keywords)} 키워드 성공), 누적 {sum(m['count'] for m in months)}건")


if __name__ == "__main__":
    main()
