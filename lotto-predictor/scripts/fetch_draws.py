#!/usr/bin/env python3
"""동행복권 로또6/45 과거 당첨번호 수집기 (표준 라이브러리만 사용).

동행복권 추첨결과 페이지(https://www.dhlottery.co.kr/lt645/result)가 내부적으로 호출하는
JSON 엔드포인트를 사용한다. 한 번 호출하면 요청 회차 기준 +4 ~ -5, 총 10회차가 내려온다.

이미 받은 회차는 캐시(JSON)에 저장해 두고, 빠진 회차만 새로 받는다.

사용 예:
    python fetch_draws.py                      # 기본 캐시 위치에 전체 이력 동기화
    python fetch_draws.py --cache draws.json   # 캐시 위치 지정
    python fetch_draws.py --offline            # 네트워크 없이 캐시 상태만 출력
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API_URL = "https://www.dhlottery.co.kr/lt645/selectPstLt645InfoNew.do?srchDir=center&srchLtEpsd={ep}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (lotto-predictor skill; personal statistics)",
    "Referer": "https://www.dhlottery.co.kr/lt645/result",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
}
KST = timezone(timedelta(hours=9))
FIRST_DRAW_DATE = date(2002, 12, 7)  # 1회 추첨일 (토요일)
DEFAULT_CACHE = Path.home() / ".cache" / "lotto-predictor" / "draws.json"
REQUEST_DELAY_SEC = 0.3  # 사이트에 부담을 주지 않도록 요청 간 간격


def fetch_block(ep: int, retries: int = 3) -> list[dict]:
    """ep 회차를 중심으로 최대 10개 회차를 받아 정규화해서 돌려준다."""
    url = API_URL.format(ep=ep)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8")
            payload = json.loads(body)
            items = (payload.get("data") or {}).get("list") or []
            return [normalize(x) for x in items]
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"회차 {ep} 조회 실패: {last_err}")


def normalize(x: dict) -> dict:
    nums = sorted(int(x[f"tm{i}WnNo"]) for i in range(1, 7))
    ymd = str(x["ltRflYmd"])
    return {
        "draw": int(x["ltEpsd"]),
        "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
        "numbers": nums,
        "bonus": int(x["bnsWnNo"]),
        "first_prize_winners": x.get("rnk1WnNope"),
        "first_prize_amount": x.get("rnk1WnAmt"),
    }


def estimate_latest(now: datetime | None = None) -> int:
    """날짜로 추정한 최신 회차. 토요일 20:35(KST) 추첨 후 반영까지 여유를 두고 21:00 기준."""
    now = now or datetime.now(KST)
    first = datetime.combine(FIRST_DRAW_DATE, datetime.min.time(), KST).replace(hour=21)
    return max(1, (now - first).days // 7 + 1)


def find_latest() -> int:
    """실제 API에 질의해 공개된 최신 회차 번호를 찾는다.

    아직 추첨되지 않은 회차를 요청하면 빈 목록이 오므로, 추정치+1 부터 한 회차씩 내려간다.
    """
    ep = estimate_latest() + 1
    for _ in range(20):
        block = fetch_block(ep)
        if block:
            return max(d["draw"] for d in block)
        ep -= 1
        time.sleep(REQUEST_DELAY_SEC)
    raise RuntimeError("최신 회차를 찾지 못했습니다. 동행복권 API 구조가 바뀌었을 수 있습니다.")


def load_cache(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {d["draw"]: d for d in data.get("draws", [])}


def save_cache(path: Path, draws: dict[int, dict], latest: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "source": "https://www.dhlottery.co.kr/lt645/result",
        "fetched_at": datetime.now(KST).isoformat(timespec="seconds"),
        "latest_draw": latest,
        "draws": [draws[k] for k in sorted(draws)],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def sync(path: Path) -> dict:
    draws = load_cache(path)
    latest = find_latest()
    missing = sorted(set(range(1, latest + 1)) - set(draws))
    # 한 요청이 [b, b+9] 범위를 덮도록 블록 단위로 묶는다 (center 기준 b+5 요청 → b+9..b)
    blocks = sorted({((m - 1) // 10) * 10 + 1 for m in missing})
    for i, b in enumerate(blocks, 1):
        for d in fetch_block(b + 5):
            if d["draw"] <= latest:
                draws[d["draw"]] = d
        if len(blocks) > 3 and (i % 25 == 0 or i == len(blocks)):
            print(f"  수집 중... {i}/{len(blocks)} 블록", file=sys.stderr)
        time.sleep(REQUEST_DELAY_SEC)
    still_missing = sorted(set(range(1, latest + 1)) - set(draws))
    save_cache(path, draws, latest)
    return {"latest": latest, "count": len(draws), "newly_fetched": len(missing) - len(still_missing),
            "missing": still_missing}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="동행복권 로또6/45 당첨번호 수집")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help=f"캐시 JSON 경로 (기본: {DEFAULT_CACHE})")
    ap.add_argument("--offline", action="store_true", help="네트워크 없이 캐시 상태만 출력")
    args = ap.parse_args()

    if args.offline:
        draws = load_cache(args.cache)
        if not draws:
            print(json.dumps({"ok": False, "error": "캐시가 비어 있습니다."}, ensure_ascii=False))
            return 1
        latest = max(draws)
        print(json.dumps({"ok": True, "mode": "offline", "cache": str(args.cache), "latest": latest,
                          "latest_date": draws[latest]["date"], "count": len(draws)}, ensure_ascii=False))
        return 0

    try:
        result = sync(args.cache)
    except Exception as e:  # 네트워크 실패 시 캐시라도 있으면 그 사실을 알린다
        cached = load_cache(args.cache)
        print(json.dumps({"ok": False, "error": str(e), "cache": str(args.cache),
                          "cached_latest": max(cached) if cached else None,
                          "cached_count": len(cached)}, ensure_ascii=False))
        return 1
    draws = load_cache(args.cache)
    print(json.dumps({"ok": True, "cache": str(args.cache), **result,
                      "latest_date": draws[result["latest"]]["date"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
