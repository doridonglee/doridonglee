"""세 조합 홈페이지에서 조합원 수를 읽어 data.json에 날짜별로 쌓습니다.

1) 먼저 홈페이지 HTML을 바로 받아서 숫자를 찾고 (1~2초)
2) 못 찾으면 GitHub 서버에 깔려 있는 Chrome으로 화면을 그려서 다시 찾습니다.
외부 패키지 없이 파이썬 기본 기능만 씁니다.
"""
import html
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# 조합별 주소와 숫자 찾는 규칙 (원본 scrape.js와 같은 규칙)
SITES = {
    "secu": {
        "url": "https://secunion.co.kr",
        "count": r"([\d,]+)\s*명\s*동행중",
        "date": r"동행중\s*[-·]?\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})",
    },
    "nseu": {
        "url": "https://samsunglabor.co.kr",
        "count": r"현재\s*조합원\s*수\s*([\d,]+)",
        "date": r"(\d{4}년\s*\d{1,2}월\s*\d{1,2}일\s*\d{1,2}시)\s*기준",
    },
    "selu": {
        "url": "https://selunion.co.kr",
        "count": r"조합원\s*([\d,]+)\s*명",
        "date": r"(\d{4}년\s*\d+월\s*\d+일\s*\d+시)",
    },
}

BLOCK_TAGS = r"br|p|div|li|tr|td|th|h[1-6]|section|article|header|footer|ul|ol|table"


def html_to_text(src):
    """브라우저의 innerText와 비슷하게 HTML을 글자로 바꿉니다."""
    src = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>", " ", src)
    src = re.sub(r"(?is)<!--.*?-->", " ", src)
    src = re.sub(rf"(?i)</?(?:{BLOCK_TAGS})\b[^>]*>", "\n", src)
    src = re.sub(r"(?s)<[^>]+>", "", src)
    src = html.unescape(src).replace("\xa0", " ")
    src = re.sub(r"[ \t\r\f\v]+", " ", src)
    return re.sub(r"\n\s*", "\n", src)


def fetch_static(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=20) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, errors="replace")


def find_chrome():
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


def fetch_rendered(url):
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("Chrome을 찾지 못했습니다")
    out = subprocess.run(
        [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
         f"--user-agent={UA}", "--virtual-time-budget=8000", "--dump-dom", url],
        capture_output=True, timeout=60,
    )
    if out.returncode != 0 and not out.stdout:
        raise RuntimeError(out.stderr.decode(errors="replace")[-300:])
    return out.stdout.decode("utf-8", errors="replace")


def parse(text, rule):
    m = re.search(rule["count"], text)
    if not m:
        return None
    count = m.group(1).replace(",", "")
    if not count.isdigit() or int(count) <= 0:
        return None
    d = re.search(rule["date"], text)
    return {"count": count, "date": re.sub(r"\s+", " ", d.group(1)).strip() if d else None}


def scrape(key, rule):
    errors = []
    for how, fetcher in (("HTML", fetch_static), ("Chrome", fetch_rendered)):
        try:
            got = parse(html_to_text(fetcher(rule["url"])), rule)
            if got:
                print(f"[{key}] {how} 방식으로 읽음: {got['count']}명 ({got['date']})")
                return got
            errors.append(f"{how}: 숫자를 찾지 못함")
        except Exception as e:
            errors.append(f"{how}: {e}")
    print(f"[{key}] 실패 - " + " / ".join(errors), file=sys.stderr)
    return {"error": " / ".join(errors)}


def main():
    try:
        with open("data.json", encoding="utf-8") as f:
            history = json.load(f)
    except (OSError, ValueError):
        history = []

    today = datetime.now(KST).strftime("%Y-%m-%d")
    record = next((r for r in history if r.get("date") == today), None)
    if record is None:
        record = {"date": today}
        history.append(record)

    ok = 0
    for key, rule in SITES.items():
        got = scrape(key, rule)
        if "count" in got:
            record[key] = got
            ok += 1
        elif not (isinstance(record.get(key), dict) and "count" in record[key]):
            record[key] = got  # 오늘 값이 아직 없을 때만 실패 기록을 남김 (이미 읽은 값은 지우지 않음)

    history.sort(key=lambda r: r["date"])
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"{today}: {ok}/{len(SITES)}개 조합 수집")
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
