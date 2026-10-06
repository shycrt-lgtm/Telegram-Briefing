# 에너지 시장 일일 브리핑 텔레그램 봇

매일(주말·공휴일 포함) 08:30 KST에 SMP·발전소 가동현황(TMS)·시장지표·주요뉴스·바로가기 링크를 텔레그램으로 발송한다.

## 구성

| 파일 | 역할 |
|---|---|
| `briefing.py` | 데이터 수집·요약·텔레그램 발송 |
| `config.json` | 데이터 주소, 표시 사업장, 바로가기 링크, 발송 시각, 휴일 제외 여부 |
| `.github/workflows/briefing.yml` | 매일 07:50 KST 시작 → 08:30 KST까지 대기 후 발송 |

데이터 출처: Energy-Index(`indicators.json`, `news.json`), KPX-Daily-Report(`smp_live.json`, `data_recent.json`의 공휴일표), TMS(`data` 브랜치의 `latest.json`, `reports/report_날짜.csv`)

## 설치 순서

1. 텔레그램에서 `@BotFather` → `/newbot` → 봇 이름 지정 → **토큰** 발급
2. 생성한 봇에게 아무 메시지나 1회 전송(그룹으로 받으려면 봇을 그룹에 초대한 뒤 메시지 1회 전송)
3. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 접속 → 결과의 `chat` → `id` 확인(그룹은 음수)
4. 새 저장소(예: `Telegram-Briefing`)를 만들어 본 폴더의 파일을 그대로 업로드(`.github/workflows` 경로 유지)
5. 저장소 Settings → Secrets and variables → Actions에 등록
   - `TELEGRAM_BOT_TOKEN`: 토큰
   - `TELEGRAM_CHAT_ID`: chat id(여러 명은 쉼표로 구분)
6. Actions → Telegram Briefing → Run workflow
   - 1차: `dry_run` 체크(기본값) → 로그에서 문구 확인
   - 2차: `dry_run` 해제 → 실제 텔레그램 수신 확인

토큰은 채팅·코드에 직접 적지 말고 Secrets에만 보관한다.

## 연동 변경 사항 (Energy-Index `news.yml`)

뉴스 수집 시각을 07:30, 15:30 KST로 변경(cron은 UTC 기준).

```yaml
schedule:
  - cron: '30 22 * * *'   # 매일 07:30 KST
  - cron: '30 6 * * *'    # 매일 15:30 KST
```

## 운영 메모

- 08:30 발송 기준 표시 내용
  - SMP: 전일·당일 모두 24시간 확정값(당일은 전날 공표분). 평균은 거래량 가중평균(사이트와 동일)
  - TMS: 전일 가동시간은 03:10 생성 리포트 기준. 현재 상태는 가장 최근 수신 슬롯 기준이며 시각을 함께 표기(통상 1~1.5시간 지연). 3시간 이상 지연 시 `※수집 지연` 표시
  - 시장지표: 항목별 기준일을 괄호 안에 표기. 갱신이 늦어진 항목은 기준일로 확인 가능
- 주말·공휴일: 기본값은 매일 발송(`skip_off_days: false`). 휴일 제외가 필요하면 `config.json`에서 `true`로 변경하면 사이트 공휴일표(`data_recent.json`)와 `holidays` 패키지(워크플로에 `pip install holidays` 단계 추가)로 판정. 임시공휴일은 `extra_holidays`(예: `"2027-01-02"`)로 추가
- 주말·공휴일에는 국제유가·배출권·REC 등 시장지표가 갱신되지 않아 직전 거래일 값이 표시됨(항목별 기준일로 구분)
- 자체 도메인으로 바꾸면 `config.json`의 `links`만 수정
- 일부 데이터 수신 실패 시 해당 항목만 "자료 수신 실패"로 표시하고 나머지는 정상 발송. Actions 로그에 경고 기록
- 시험 실행: `python briefing.py --dry-run --no-wait` (발송 없음)
