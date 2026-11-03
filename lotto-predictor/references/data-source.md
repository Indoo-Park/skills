# 데이터 출처와 장애 대응

## 엔드포인트

```
GET https://www.dhlottery.co.kr/lt645/selectPstLt645InfoNew.do?srchDir=center&srchLtEpsd={회차}
```

- 동행복권 추첨결과 페이지(https://www.dhlottery.co.kr/lt645/result)가 내부적으로 호출하는 주소다. 2026-11 기준으로 확인했다.
- 요청한 회차 기준 +4 ~ −5, 최대 10회차를 최신순으로 돌려준다. 예: 1200 → 1204..1195, 5 → 10..1.
- 아직 추첨되지 않은 회차를 요청하면 `data.list` 가 빈 배열이다. 그래서 최신 회차는 날짜로 추정한 값 +1 부터 한 회차씩 내려가며 찾는다.
- 예전 API(`common.do?method=getLottoNumber&drwNo=`)는 2026-11 현재 JSON 대신 HTML 을 돌려주므로 쓰지 않는다.

응답에서 쓰는 필드:

| 필드 | 의미 |
|---|---|
| `ltEpsd` | 회차 |
| `ltRflYmd` | 추첨일 (YYYYMMDD) |
| `tm1WnNo` … `tm6WnNo` | 당첨번호 6개 |
| `bnsWnNo` | 보너스 번호 |
| `rnk1WnNope`, `rnk1WnAmt` | 1등 당첨자 수, 1인당 당첨금 |

## 캐시 형식

```json
{"source": "...", "fetched_at": "2026-11-03T10:00:00+09:00", "latest_draw": 1248,
 "draws": [{"draw": 1, "date": "2002-12-07", "numbers": [10,23,29,33,37,40], "bonus": 16,
            "first_prize_winners": 0, "first_prize_amount": 0}, ...]}
```

## 장애 대응

| 증상 | 대응 |
|---|---|
| `fetch_draws.py` 가 `ok: false` + 네트워크 오류 | 잠시 후 한 번만 재시도한다. 그래도 실패하면 `--offline` 으로 캐시 상태를 보고, 캐시가 있으면 "제N회까지의 데이터"라고 밝히고 분석한다. |
| 응답이 JSON 이 아님 / 필드 누락 | 사이트 개편 가능성이 크다. 브라우저 도구가 있으면 추첨결과 페이지를 열어 네트워크 요청에서 새 엔드포인트를 찾고, `scripts/fetch_draws.py` 의 `API_URL`·`normalize()` 를 고친다. 이 문서도 함께 갱신한다. |
| `missing` 에 회차가 남음 | 다시 실행하면 빠진 회차만 받는다. 계속 남으면 그 회차를 제외한 채 분석하고 그 사실을 알린다. |
| 토요일 밤인데 최신 회차가 안 보임 | 추첨 직후엔 반영이 늦을 수 있다. 직전 회차까지로 분석하고 그 사실을 알린다. |

어떤 경우에도 당첨번호를 기억이나 추측으로 채우지 않는다.
