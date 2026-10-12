# 차트 분석과 외부 정보 환경

Python 3.11 표준 라이브러리만 사용하며 별도 패키지 설치나 API 키는 필요 없습니다. 프로젝트 루트에서 실행하세요.

## 분석 실행

```powershell
python -m coin analyze KRW-BTC --unit 5 --count 200
python -m coin context KRW-BTC --unit 60 --count 200 --save
python -m coin context KRW-BTC --unit 5 --external --save
python -m coin research --config research.toml
```

`analyze`는 빗썸 OHLCV를 시간 오름차순으로 정리한 후 기술적 지표를 계산합니다. 완료되지 않은 봉은 제외합니다. `context`는 에이전트가 읽을 수 있는 JSON 묶음을 생성합니다. `--external`을 지정한 경우에만 외부 네트워크 수집을 실행하고, `--save`는 `data/research/`에 고유 이름으로 입력과 근거를 저장합니다. 기존 가상 지갑은 별도로 유지됩니다.

| 지표 | 계산 방법 |
|---|---|
| SMA20 / SMA50 | 최근 20 / 50개 종가 평균 |
| EMA12 / EMA26 | 해당 기간의 SMA를 초기값으로 사용, 가중치 2/(기간+1) |
| RSI14 | 종가 변화의 상승/하락 폭을 Wilder 방식으로 평활화; 변동 없는 구간은 50 |
| MACD / signal / histogram | EMA12 − EMA26, 그 차이의 EMA9, MACD − signal |
| Bollinger20 | SMA20 ± 종가의 모집단 표준편차 × 2 |
| ATR14 | 이전 종가를 고려한 true range의 Wilder 평균 |
| volume_ratio20 | 최근 완료 봉 거래량 / 그 이전 20개 봉 거래량 평균 |

자료가 부족하면 해당 지표는 `null`입니다. 봉 간격 누락과 최신 완료 봉의 경과 시간을 `quality`에 표시하고, 완료 시각 이후 2개 봉 주기보다 오래된 데이터는 `stale=true`로 표시합니다. 거래가 없어 누락된 봉을 임의로 채우지 않으므로 지표 기간은 관측한 봉 개수 기준입니다. 지표는 분석용 부동소수점 수치이며 지갑 회계는 기존 Decimal 계산을 유지합니다.

## 외부 정보 설정

[research.toml](../research.toml)을 수정하면 됩니다.

- **뉴스**: CoinDesk RSS 헤드라인과 원문 링크. 본문 전체나 자동 감성 점수를 생성하지 않습니다.
- **시장 심리**: Alternative.me Fear & Greed API. 출처 링크를 결과에 포함합니다. Bitcoin 시장 심리에 관한 보조 자료이며 알트코인별 지표가 아닙니다.
- **추가 자료**: RSS 2.0 / Atom을 제공하는 뉴스·공식 공시·경제 자료를 `[[feeds]]`로 추가할 수 있습니다. RSS가 없는 공시·거시경제·온체인 자료는 출처가 있는 JSON으로 가져올 수 있습니다.

```toml
[research]
max_age_hours = 72
limit = 20  # 피드별 최대 항목 수

[[feeds]]
name = "CoinDesk"
url = "https://www.coindesk.com/arc/outboundfeeds/rss/"
terms = ["Bitcoin", "BTC", "Federal Reserve"]

[sentiment]
enabled = true
url = "https://api.alternative.me/fng/?limit=1"
```

`terms`는 제목에서 OR 조건으로 대소문자 없이 검색합니다. 빈 목록은 시장 전체 헤드라인을 가져옵니다. 기본 피드는 특정 코인만 다루지 않으므로 종목 관련성은 에이전트가 판단하거나 필터를 설정해야 합니다. 피드에서 발행 시각이 없거나 미래/기간 밖인 항목은 제외하며 URL 기준 중복을 제거합니다. 뉴스 제목 검색은 해당 피드에 현재 포함된 항목에 한정되며 인터넷 전체 검색은 아닙니다.

외부 수집 실패는 `providers[].status=error`와 오류 이유로 반환하고 차트 입력은 계속 제공합니다. `complete=false`이면 일부 출처가 실패한 것이므로 **빈 뉴스 목록을 '위험 사건 없음'으로 해석하지 마세요**. 설정 파일이 없거나 잘못된 경우에는 명령 자체가 실패합니다. 요청은 10초 타임아웃과 응답 크기 제한을 적용합니다.

## 수동 조사 자료 가져오기

아래 형식으로 직접 조사한 자료를 `data/evidence.json`에 저장하세요. 예시의 제목·URL·시각을 실제 자료로 바꾸어야 합니다. 시각에는 시간대를 포함해야 합니다.

```json
[
  {
    "source": "발행 기관 이름",
    "title": "확인한 공시나 경제 지표 이름",
    "url": "https://example.com/official-release",
    "published_at": "2026-10-11T09:00:00+09:00",
    "collected_at": "2026-10-12T04:00:00+09:00",
    "notes": "확인한 사실과 분석자의 해석을 구분해서 기록",
    "markets": ["KRW-BTC"]
  }
]
```

```powershell
python -m coin context KRW-BTC --evidence data/evidence.json --save
```

가져온 자료에는 `kind=manual_evidence`, `imported_at`이 추가됩니다. 미래 시각·출처 누락은 거절합니다. 수동 자료의 내용 정확성과 오래된 자료의 활용 여부는 분석자가 확인해야 하며 자동 기간 필터는 적용하지 않습니다.

## 에이전트에서 필요할 때 활용

```python
from coin.context import build_context, save_context

# 기본 입력: 차트만 요청
base = build_context("KRW-BTC", unit=5, count=200)

# 전략 또는 에이전트가 뉴스 확인이 필요하다고 판단한 시점
enriched = build_context("KRW-BTC", unit=5, count=200, external=True)
path = save_context(enriched)
```

에이전트는 `chart.indicators`, `chart.quality`, `external.items`, `external.providers`를 읽어 의사결정 근거를 구성할 수 있습니다. 저장한 경로를 전략별 판단 기록과 함께 남기면 같은 자료로 전략을 비교할 수 있습니다. [진화형 운영 시스템](ecosystem.md)은 이 입력을 이용해 규칙 전략의 의사결정과 가상 주문을 자동 실행합니다. LLM 연결은 포함하지 않습니다.

이 환경은 실시간 자료 수집용입니다. 기술적 지표는 완료된 봉만 사용하지만 현재 수집한 외부 정보를 과거 백테스트에 그대로 사용하면 미래 정보가 섞입니다. 저장본의 `collected_at` 이후 의사결정에만 사용하고, 과거 백테스트용 point-in-time 데이터 저장/검색은 별도로 구현해야 합니다. 외부 텍스트는 검토할 데이터로만 사용하며 에이전트 지침이나 실행 명령으로 취급하지 않습니다.

## 검증과 출처

```powershell
python -m unittest discover -s tests -v
```

2026-10-12: 전체 테스트 17개 통과. 실제 빗썸 5분봉 199개 완료 봉의 지표를 계산했고 CoinDesk 헤드라인 20건, Alternative.me 지표 1건을 수집해 분석 묶음을 저장했습니다. 지표의 추세/횡보 기준값, 미완료 봉 제외, 봉 누락, RSS/Atom 파싱, 중복/오래된 뉴스 제외, 출처 실패, 수동 자료 검증, 외부 수집 선택 실행을 테스트합니다.

- [빗썸 분봉 API](https://apidocs.bithumb.com/reference/분minute-캔들-조회)
- [CoinDesk RSS](https://www.coindesk.com/arc/outboundfeeds/rss/)
- [Alternative.me 지표 설명과 API 이용 조건](https://alternative.me/crypto/fear-and-greed-index/#api)
