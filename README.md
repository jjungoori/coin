# Coin investment agents — 진화형 가상매매

빗썸 공개 시세로 독립 지갑·전략을 실행하고, 생존 평가·전략 진화·격리 실험을 수행하는 가상매매 시스템입니다. 라이브 실행의 총괄은 실행 사용자로 로그인된 Codex CLI를 통해 시장과 성과를 보고 새 환경 생성, 에이전트 배치, 전략 조건식 변경을 판단합니다. Python 3.11 이상과 Codex CLI를 사용하며 별도 OpenAI API 키는 필요 없습니다. 실주문·출금 기능은 없습니다.

## 바로 시작

거래 에이전트는 지갑을 가진 규칙 기반(`rules`) 또는 생성형 AI 기반(`hybrid`) 개체입니다. 총괄 에이전트는 지갑 없이 거래 에이전트의 전략과 개체군을 관리합니다. 격리 환경 안에서 거래 에이전트가 진화하며, 총괄 판단에 따라 주요 환경 외에 부속 환경을 만들어 전략을 테스트합니다.

프로젝트 디렉터리에서 실행합니다. 데모는 합성 시세로 기능을 빠르게 확인하고 라이브는 빗썸 시세로 가상매매합니다.

```powershell
# 네트워크 없이 60개 완료 봉 시뮬레이션; 재실행하면 이어서 실행
python -m coin.agents demo --steps 60
python -m coin.agents --db data/demo-agents.sqlite3 status
python -m coin.agents --db data/demo-agents.sqlite3 events --limit 30

# 현재 빗썸 시세를 사용하는 지속 가상매매 (Ctrl+C로 중단)
codex login status
# 로그인되지 않았다면 이 실행 환경에서 codex login 실행
python -m coin.agents init
python -m coin.agents run
# 필요할 때 뉴스·시장 심리 수집도 활성화
python -m coin.agents run --external
```

데모는 `data/demo-agents.sqlite3`, 라이브는 `data/agents.sqlite3`에 저장됩니다. 재시작해도 지갑·세대·거래 기록이 유지됩니다. `agents.toml`에서 자본·생존 기준·진화 주기를 조절하며 설정을 바꾼 실험에는 새로운 `--db`를 지정하세요. 기본 5분봉 기준 새로운 완료 봉이 들어올 때 한 번 처리하며 30초마다 수집합니다. 초기 거래 에이전트 3개의 자본은 각각 가상 10만 원입니다.

```powershell
python -m coin.agents --db data/experiment-02.sqlite3 --config agents.toml run --steps 2
python -m coin.agents status
python -m unittest discover -s tests -v
```

[에이전트·진화·총괄 운영 안내](docs/ecosystem.md)에서 기본 생존 문턱, 부모 선택, 자본 배정, 기록과 시뮬레이션 한계를 확인할 수 있습니다.

총괄은 첫 처리 틱과 이후 `ai.every_ticks` 주기마다 생성형 판단을 요청합니다. `agents.toml`의 `[ai]`에서 CLI 명령·모델·주기·시간 제한을 설정합니다. 빈 `model`은 CLI 기본 모델을 사용합니다. 실행 프로세스는 터미널의 사용자 환경과 `CODEX_HOME`을 상속하며, 기존 ChatGPT 로그인을 사용합니다. 인증 파일을 읽거나 복제하지 않고 API 키 환경 변수는 자식 호출에서 제외합니다.

```powershell
# 합성 시세에서도 실제 생성형 총괄을 검증
python -m coin.agents --db data/ai-demo.sqlite3 --ai codex demo --steps 2
python -m coin.agents --db data/ai-demo.sqlite3 events --limit 30
# 규칙 총괄로 실행; 일반 demo는 기본적으로 이 오프라인 모드
python -m coin.agents --ai rules run
```

AI는 상황에 따라 아무 행동도 하지 않을 수 있습니다. `supervisor_ai_plan`에 판단과 행동 계획, `supervisor_ai_action`에 실행, `supervisor_ai_rejected`에 자본/상태 제약으로 거절된 행동, `supervisor_ai_error`에 연결 실패가 남습니다. 실패 시 해당 회차의 총괄 행동을 건너뛰고 기존 전략 실행은 유지합니다. AI는 새 매수·매도 조건식을 작성할 수 있으며, 적용된 규칙은 다음 틱부터 실행됩니다. 계좌 생존/손절·익절·입력 품질·자본 회계는 실행기가 보장합니다. 격리 환경은 별도 금고와 지갑을 가진 논리적 가상매매 환경입니다.

지갑 거래 에이전트에는 `rules`(지정 규칙만 실행)와 `hybrid`(규칙에 경량 생성형 매매 판단 추가)가 있습니다. 초기 구성은 규칙형 2개·혼합형 1개입니다. `[workers]`의 `initial_modes` 목록을 반복해 초기 개체와 새 출생의 유형을 정하며 총괄도 개체별 `decision_mode`를 선택할 수 있습니다. `status`에서 각 지갑의 유형을 확인하고 아래 명령으로 활성 거래 개체의 유형을 바꿀 수 있습니다.

```powershell
python -m coin.agents mode main:trader:000001 hybrid
python -m coin.agents mode main:trader:000001 rules
```

혼합형은 `gpt-6-luna`, `low` 추론으로 최대 2개 지갑을 한 번에 검토합니다. 지갑마다 최소 6틱 간격이며 입력은 최근 봉·지표·자기 지갑·규칙 판단만 포함합니다. 모델은 신호를 확정하거나 변경할 수 있지만 추가 매수 금지·보유 없는 매도 금지·최대 투자 비중·손절/익절·품질/심리 제약은 유지됩니다. 호출 실패·예산 소진·호출 간격 사이에는 지정 규칙을 사용합니다. `--ai rules`와 일반 `demo`에서는 혼합형도 모델을 호출하지 않습니다.

토큰 예산은 총괄과 거래 판단을 별도로 관리합니다. 기본 거래 예산은 KST 하루 48회·150,000토큰, 총괄은 24회·300,000토큰입니다. 호출 전 거래 12,000토큰/총괄 16,000토큰의 여유를 요구하고, `--json`의 실제 입력+출력 토큰으로 정산합니다. 사용량을 못 받은 실패·중단 호출에는 예약량을 유지합니다. 재시작과 틱 롤백에도 호출 원장이 남으며 같은 프레임의 요청을 반복하지 않습니다. `status.ai_usage`에서 누적 사용량과 남은 예산을 확인합니다. CLI 출력·추론 토큰에는 강제 상한을 지정하지 않으므로 **한 회차가 예약량을 초과할 수 있는 소프트 예산**입니다. 입력 문자 제한은 CLI 내부 지시문과 스키마의 토큰을 포함하지 않습니다.

구형 CLI가 최신 모델을 지원하지 않으면 프로젝트에만 런타임을 설치할 수 있습니다. `command="codex"`는 아래 위치의 런타임이 있을 때 우선 사용하고, 없으면 PATH의 CLI를 사용합니다. 기존 로그인은 그대로 상속합니다.

```powershell
npm install --prefix data/codex-runtime --no-save @openai/codex@0.162.1
```

`[ai]`와 `[workers]`의 모델·주기·예산은 같은 DB에서 조정해 재시작할 수 있습니다. 기존 지갑의 유형과 누적 사용량은 유지되며 `initial_modes` 변경은 이후 출생부터 적용합니다. 가상 자본·생존·진화 설정은 기존처럼 고정합니다. 거래 판단은 짧은 전용 지시문으로 코딩 지시문을 대체하고 도구와 스킬 목록을 줄여 호출 자체의 입력도 절약합니다. 예산은 저장소별로 관리됩니다.

[검증 기록](docs/verification.md)에서 규칙 실행과 생성형 판단 연결의 검증 범위를 확인할 수 있습니다.

차트 지표와 외부 정보도 에이전트 입력으로 사용할 수 있습니다. 설정과 확장 방법은 [분석 환경 안내](docs/analysis.md)를 참고하세요.

```powershell
# 완료된 5분봉의 기술적 지표
python -m coin analyze KRW-BTC --unit 5 --count 200
# 차트 + 외부 뉴스/시장 심리 수집, 분석 근거 저장
python -m coin context KRW-BTC --external --save
```

## 수동 가상매매와 시세 조회

Python 3.11 이상. 외부 런타임 패키지와 API 키 없이 실행할 수 있습니다.

```powershell
python -m coin markets
python -m coin ticker KRW-BTC
python -m coin orderbook KRW-BTC
python -m coin candles KRW-BTC --unit 5 --count 100

python -m coin create-wallet agent-alpha --cash 1000000
python -m coin create-wallet agent-beta --cash 1000000
python -m coin buy agent-alpha KRW-BTC 10000
python -m coin wallet agent-alpha
python -m coin history agent-alpha
python -m coin report agent-alpha
# 매도 수량은 wallet에서 확인한 보유 수량 이내로 지정
python -m coin sell agent-alpha KRW-BTC 0.0001
```

매수 인자는 **수수료를 포함한 최대 원화 예산**, 매도 인자는 **코인 수량**입니다. 모든 매매는 가상이며 CLI는 GET 요청만 사용합니다. `data/paper.sqlite3`에 지갑, 포지션, 거래 당시 호가가 저장되어 재실행해도 유지됩니다. 지갑을 다시 생성하면 오류가 발생하며 잔고가 초기화되지 않습니다.

설정은 하위 명령 앞에 지정합니다.

```powershell
python -m coin --db data/experiment-01.sqlite3 --fee-rate 0.0004 --min-notional 5000 create-wallet agent-gamma
python -m unittest discover -s tests -v
```

`fee-rate=0.0004`(0.04%), 최소 거래금액 5,000원은 **시뮬레이션 가정**이며 실제 계좌 수수료/주문 제약을 확인한 값이 아닙니다. 실험 중에는 같은 설정을 유지하세요. 매 거래에 적용된 수수료는 기록됩니다.

## 구성과 확장

- `coin/bithumb.py`: 마켓 목록, 현재가, 호가, 분봉 조회. HTTPS GET만 제공하며 요청 간격, 타임아웃, 429/5xx/연결 오류에 대한 제한된 재시도를 적용합니다.
- `coin/paper.py`: 에이전트별 원화 잔고/코인 포지션, 매수 원가, 실현 손익, 거래 원장. SQLite 트랜잭션으로 잔고 변경과 거래 기록을 함께 저장하며 Decimal로 금액을 계산합니다.
- `coin/cli.py`: 수동 실험과 API 연결 점검. `report`는 현재가 기준 평가액·손익·수익률·실현 손익을 출력합니다. 평가액은 매도 수수료를 차감하지 않습니다.

전략 에이전트는 아래 인터페이스를 호출하면 됩니다. `agent_id`별 초기 자본을 동일하게 설정하면 성과를 비교하기 쉽습니다.

```python
from coin.bithumb import BithumbPublicClient
from coin.paper import PaperBroker

client = BithumbPublicClient()
broker = PaperBroker()
try:
    # 최초 한 번: broker.create_wallet("strategy-v1", "1000000")
    snapshot = client.orderbook("KRW-BTC")
    trade = broker.execute("strategy-v1", "KRW-BTC", "buy", "10000", snapshot)
finally:
    broker.close()
```

현재는 KRW 현물 시장가 매매만 지원합니다. 매수는 매도호가, 매도는 매수호가를 순서대로 소진하여 호가 차이와 여러 가격대의 체결 비용을 반영합니다. 전체 수량을 채울 잔량이 부족하면 거래를 거절합니다. 30초 이상 오래된 호가, 잔고 부족, 최소 거래금액 미달도 거절합니다. 매수 수량은 소수점 8자리로 내림하고 남은 예산은 지갑에 유지됩니다.

가상 지갑은 독립적인 전략 실험입니다. 여러 에이전트나 반복 주문 사이에서 실제 호가 잔량을 공유/차감하지 않습니다. 네트워크 지연, 주문 대기열, 시장 충격, 부분 체결, 지정가 주문은 모델링하지 않습니다. 따라서 가상 수익률이 실거래 수익률을 보장하지 않습니다. 자동 실행 시스템의 프레임·성과·계보·생사 기록은 별도 에이전트 데이터베이스에 저장됩니다.

## API 근거

공식 REST base URL은 `https://api.bithumb.com`이며 공개 API는 인증이 필요 없습니다. 개인 계좌 API는 JWT 인증이 필요하지만 현재 범위에는 포함하지 않았습니다.

- [API 개요](https://apidocs.bithumb.com/reference/)
- [마켓 목록](https://apidocs.bithumb.com/reference/거래-대상-목록-조회): `/v1/market/all`
- [현재가](https://apidocs.bithumb.com/reference/현재가-조회): `/v1/ticker`
- [호가](https://apidocs.bithumb.com/reference/호가-조회): `/v1/orderbook`
- [분봉](https://apidocs.bithumb.com/reference/분minute-캔들-조회): `/v1/candles/minutes/{unit}`. 최신 순서로 반환하며 `to`는 KST 기준, 지정 시각 제외, 최대 200개입니다.

실제 연결 점검: `python -m coin ticker KRW-BTC`. 전체 연결 및 가상 왕복매매 점검: `python -m coin.smoke` (임시 지갑을 생성하고 종료 시 삭제). 단위 테스트는 네트워크 없이 실행됩니다. 데이터베이스와 향후 인증 키는 Git에 포함하지 않습니다.

자동 시스템은 `coin/ecosystem.py`, 규칙 전략은 `coin/strategies.py`, Codex 연결·생성된 조건식 실행기는 `coin/generative.py`, 라이브·데모 입력은 `coin/feeds.py`, 운영 명령은 `coin/agents.py`에 있습니다. 수동 가상 지갑은 `data/paper.sqlite3`을 사용합니다.
