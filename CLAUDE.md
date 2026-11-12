# CLAUDE.md — Team RedRed (Secure AI Systems 2026-2, Track C)

이 파일은 Claude Code가 모든 세션 시작 시 읽는 프로젝트 규칙이다. 팀원 세 명(김지선, 이순아, 유혜영)이 같은 파일을 공유한다.
근거 문서: `docs/` 아래의 Project Brief, Team_RedRed_Proposal, Project_Proposal_Feedback, ROE 초안, 중간점검 계획.

## 0. 프로젝트 한 줄 요약

LLM 기반 자율 레드팀 에이전트(Track C)를 만든다. 에이전트 자체가 우리의 "시스템"이다.
현재 단계: **중간점검(2026-11-23 23:59 KST)** — 자체 스테이징 타깃을 상대로 샌드박스 안에서 전체 루프가 도는 working slice.

## 1. 절대 규칙 (어떤 작업 지시보다 우선)

1. **대상 범위.** 모든 테스트 트래픽은 `STAGING_TARGET_URL`과 `KU_LLM_BASE_URL`로만 보낸다.
   파트너 팀 호스트는 ROE 서면 승인 전까지 코드, 설정, 테스트, 주석, 예시 어디에도 넣지 않는다.
   개발 중 직접 실행하는 `curl`/`httpx` 호출도 같은 규칙을 따른다(이 트래픽은 에이전트의 egress 로그에 잡히지 않는다).
2. **`.env` 취급.** `.env`에는 실제 키가 이미 들어 있다.
   - `.env`를 열거나 `cat`/`grep`/`printenv`/`env`로 내용을 출력하지 않는다. 필요한 변수 이름은 `.env.example`을 본다.
   - 코드는 `python-dotenv` 또는 `os.environ`으로 런타임에 읽는다. 키 값을 코드, 테스트, 로그, 커밋 메시지, 출력에 쓰지 않는다.
   - Logger와 Target client는 `Authorization`, `x-api-key` 등 인증 헤더를 기록 전에 `[REDACTED]`로 바꾼다.
   - `.env`, `.env.*`(단 `.env.example` 제외)가 `.gitignore`에 있는지 커밋 전에 확인한다.
3. **실제 API 호출은 예산을 쓴다.** KU LLM API나 스테이징 타깃을 실제로 호출하는 테스트·스크립트는 실행 전에 사람에게 확인받는다.
   기본 단위 테스트는 오프라인(mock)으로 돈다. 실호출 테스트는 `@pytest.mark.live`로 표시한다.
4. **타깃 응답은 데이터다.** 타깃 응답은 Planner/Verifier 프롬프트에 지시가 아닌 데이터 필드(`untrusted_data`)로만 들어간다.
   응답 내용을 시스템 프롬프트나 지시문 위치에 이어 붙이는 코드를 만들지 않는다(제안서 Defence #5).
5. **타깃 주소는 모델이 바꿀 수 없다.** Target client의 base URL은 설정에서만 오고, 모델 출력으로 URL·호스트·경로 밖 라우트를 바꾸는 경로를 만들지 않는다.
6. **`schemas/`는 합의된 계약이다.** 작업 지시에 명시되지 않으면 수정하지 않는다. 바꿔야 하면 이유를 먼저 보고한다.
7. **측정 타당성.** Planner/Attack generator 코드나 프롬프트를 작성할 때 `staging_target/`에 심은 취약점의 구체 내용을 반영하지 않는다
   (seeded-vulnerability recall 지표가 의미를 잃는다). 팀원 간 정보 공유를 막는 규칙이 아니라, 에이전트 코드에 정답이 새지 않게 하는 규칙이다.

## 2. 아키텍처 ↔ 코드 대응

제안서 §1 다이어그램의 박스와 파일을 1:1로 맞춘다. 새 파일을 만들면 이 표를 갱신한다.

| 박스 | 파일 | 담당 |
| --- | --- | --- |
| Planner | `agent/planner.py`, `prompts/planner.md` | 김지선 |
| Attack generator | `agent/generator.py`, `agent/mutations/` | 김지선 |
| Prompt assembly (데이터/지시 분리) | `agent/prompt_assembly.py` | 김지선 |
| Budget / kill switch | `agent/budget.py` | 김지선 |
| Target client | `agent/target_client.py` | 유혜영 |
| Verifier | `agent/verifier.py`, `prompts/verifier.md` | 이순아 |
| Logger | `agent/logger.py` | 이순아 |
| Sandbox / egress | `docker-compose.yml`, `infra/squid/` | 유혜영 |
| Staging target | `staging_target/` | 이순아 |
| 데이터 | `data/advbench/`, `data/DATA_CARD.md` | 김지선 |

담당은 책임 소재일 뿐이고, 누구나 어느 파일이든 읽고 수정·리뷰할 수 있다.

## 3. 인터페이스 (`schemas/`)

- `schemas/log_record.py` — Logger가 쓰는 JSONL 한 줄. 요청·응답 전문, 타임스탬프, session_id, 타깃 버전, verifier 결과, 이전 레코드 해시.
- `schemas/target_io.py` — `TargetRequest` / `TargetResponse`.
- `schemas/verifier_input.py` — 타깃 응답은 `untrusted_data` 필드로만 전달.

모든 컴포넌트는 이 모델로 입출력을 주고받는다. 딕셔너리를 임의로 만들지 않는다.

## 4. 운영 한도 (제안서 Defence #3 · ROE §3)

| 항목 | 한도 |
| --- | --- |
| 타깃 요청 | 10 req/min, 200 req/session (재시도 포함) |
| 세션 시간 | 30분 |
| 모델 비용 | USD 5/session |
| 동시성 | 감독 세션 1개 |

한도 도달, 로깅 실패, egress 이상 시 자동 정지. 상수는 `agent/budget.py` 한 곳에만 둔다.

## 5. 완료의 정의 — seam 테스트

작업은 아래 테스트가 통과해야 끝난 것이다. 테스트가 없으면 테스트부터 만든다.

| Seam | 테스트 파일 | 통과 조건 |
| --- | --- | --- |
| ① 에이전트 ↔ egress | `tests/test_seam1_egress.py` | 샌드박스 안에서 비허용 호스트 요청 → 차단 + Squid 로그에 DENIED 기록. 프록시 우회 직접 접속도 실패 |
| ② 생성기 ↔ Verifier | `tests/test_seam2_verifier.py` | 심어둔 non-finding은 거부, 심어둔 known finding은 수락 |
| ③ Logger ↔ Target client | `tests/test_seam3_logcount.py` | 세션마다 보낸 요청 수 = 로그된 요청 수 |

중간점검 핵심 숫자: **테스트 세션 전체에서 허용 호스트 외 도달 요청 0건**, 거부 시도는 egress·샌드박스 로그에서 집계.
집계는 `scripts/count_egress.py`로 하고, 이 스크립트로 재현 가능해야 한다(Brief §7: 논문의 모든 숫자는 레포에서 재현).

## 6. 명령어

```bash
docker compose up --build          # 샌드박스 + Squid + 스테이징 타깃
pytest -m "not live"               # 오프라인 테스트 (기본)
pytest -m live                     # 실호출 — 실행 전 사람 확인 필요
python scripts/count_egress.py     # 도달/거부 집계
```

## 7. 작업 방식

- 구현 전에 계획을 먼저 제시하고 승인을 받은 뒤 코드를 쓴다.
- 한 작업은 중간점검 계획표의 한 칸 크기로 유지하고, 변경은 작게 커밋한다.
- 데이터: AdvBench는 커밋 `098262edf85f807224e70ecd87b9d83716bf6b73`에 고정하고, 로드 시 `harmful_behaviors.csv`의
  sha256 `6cd1a5c63c07610d7eb67307772ee5606017ee950b5770ab288a2c487489d3e1`을 검증한다. 불일치하면 실행을 중단한다.
- 생성된 공격 변형은 레코드마다 생성기 모델·프롬프트 파일·temperature·seed·시각을 남긴다(Week 4 generational provenance).
- 의존성은 버전을 고정한다(`requirements.txt`의 `==`, Docker 이미지 digest).
- 우선순위가 밀리면: ① 샌드박스·egress·"도달 0건" → ② 로그 완전성 → ③ 루프 완주 → ④ Verifier 정교화·공격 다양성.

## 8. 중간점검 전 함께 챙길 것 (피드백 슬라이드 17)

- [ ] 성공 지표를 pass line / fail line 문장으로 재작성 (Track C: 동일 예산 baseline 대비)
- [ ] 위협 모델 기반 커버리지 그리드, 빈 칸은 out-of-scope + 사유
- [ ] 방어/에이전트 개선 전에 baseline부터 실행
- [ ] test split 동결, 튜닝은 dev split에서만
- [ ] `data/DATA_CARD.md` 작성·커밋
- [ ] Verifier 오판율: 스테이징의 코드 기반 정답 체크와 Verifier 판정을 대조 (Planner·생성기·Verifier가 모두 같은 모델이라 circularity 점검 필요)

## 9. AI 사용 기록

Brief §6에 따라 모든 제출물에 AI 사용 부록이 필요하다. Claude Code로 작업한 세션은 `docs/AI_USE.md`에
날짜, 작업자, 도구(Claude Code), 모델 버전, 작업 내용을 한 줄씩 추가한다.

## 10. 미정 사항 (확정 전까지 코드에 하드코딩 금지)

- 파트너 팀, 대상 엔드포인트, 프리즈 태그 — ROE §1 [PENDING]
- 공격 기간 정확한 날짜·시간대 — ROE §2 [PENDING]
- 교수님 서면 승인 — Week 14 전 필수
