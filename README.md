# Hashimoto Engine

지능형 면접 시스템의 **논리 분석 엔진**입니다. 지원자의 발화를 턴마다 분석하여 논리적 공백을 추적하고, 후속 질문 전략을 생성해 Sapien 엔진에 전달합니다.

---

## 핵심 개념

| 개념 | 설명 |
|---|---|
| **Slot** | 아직 검증되지 않은 명제 또는 질문 단위. 모든 응답 경로(충족·불충분·거부)에서 스택에서 제거됨 |
| **Priority_Stack** | 활성 슬롯의 LIFO 스택. 인덱스 0이 최상단(가장 최신 슬롯) |
| **Depth_Counter** | 현재 주제 내 연속 질문 횟수. 임계값(5) 도달 시 Pruning 발동 |
| **Topic_Question_Counter** | 현재 root_topic에 대한 누적 질문 수. 임계값(10) 도달 시 강제 topic 전환 |
| **Abduction(가추법)** | 지원자 답변의 논리적 공백으로부터 원인을 역추론하여 연속 슬롯 생성 |
| **Multi-Slot Scanning** | 스택 상위 N개(기본 3개) 슬롯을 동시에 평가 |
| **Fulfillment Score** | 슬롯 충족도 (0.0–1.0). 0.7 이상이면 VERIFIED(충족됨 아카이브 이동) |
| **response_type** | LLM이 분류한 응답 유형: `ANSWERED`(답변 시도) / `INSUFFICIENT`(핵심 미충족) / `REFUSED`(거부·모름) |
| **Pruning** | Depth_Counter가 임계값에 도달하면 스택을 초기화하고 다음 주제로 전환. within-topic prune과 root topic 전환 두 단계로 분리 |
| **Archive** | 해소된 슬롯의 영구 저장소. `verified_archive`(충족됨) / `refused_archive`(거부됨). topic 전환 후에도 소멸하지 않음 |

---

## 엔진 모드

Hashimoto는 두 가지 모드로 사용할 수 있습니다.

| | 표준 모드 (`HashimotoEngine`) | 저지연 모드 (`LowLatencyHashimotoEngine`) |
|---|---|---|
| 처리 방식 | 동기, 블로킹 | 비동기, non-blocking |
| Sapien 대기 | 분석 완료까지 대기 | 대기 없음, 즉시 반환 |
| 상태 보호 | 단일 스레드 | 단일 워커 큐 (락 없음, 경쟁 없음) |
| 주요 메서드 | `process_turn()` | `submit_turn()` + `latest_strategy` |
| 적합한 상황 | 테스트, 배치 처리 | 실시간 음성 면접 |

---

## 초기화 방법

### 주제 직접 지정

```python
engine = HashimotoEngine(root_topic="React 상태관리 경험")
```

### 자기소개서에서 자동 추출

자기소개서 텍스트를 주입하면 LLM이 지정한 개수만큼 면접 주제를 추출하고, 추출된 순서대로 주제를 소화합니다. PRUNE 신호가 발생하면 자동으로 다음 주제로 전환됩니다.

```python
# 동기 (표준 모드)
engine = HashimotoEngine.from_resume(resume_text, topic_count=3)

# 비동기 (저지연 모드)
engine = LowLatencyHashimotoEngine.from_resume(resume_text, topic_count=3)
```

추출 결과 예시:

```
자기소개서 입력
      │
      ▼ LLM 1회 호출 (topic_extractor.py)
["React 상태관리 아키텍처 경험", "팀 리딩 경험", "장애 대응 경험"]
      │
      ▼
HashimotoEngine(
    root_topic  = "React 상태관리 아키텍처 경험",  ← 현재 주제
    topic_queue = ["팀 리딩 경험", "장애 대응 경험"] ← 대기 주제
)
```

---

## 처리 흐름

### 표준 모드 (턴당 1회, 동기)

```
지원자 발화 (STT) + 멀티모달 신호
        │
        ▼
1. 활성 슬롯 컨텍스트 구성 (스택 상위 3개)
   └─ 각 슬롯: proposition, score, criteria, evidence
      연속 슬롯이면 Derived From + 부모 evidence 포함
        │
1b. 이미 해소된 명제 요약 구성 (재질문 방지)
   └─ verified_archive + refused_archive → [이미 해소된 명제] 블록
        │
        ▼
2. LLM 단일 호출 — 히스토리 없음, 슬롯 컨텍스트가 크로스-턴 메모리
   (gemini-3.1-flash-lite + structured output, thinking off)
   ├─ slot_evaluations  : 슬롯 평가 + response_type(ANSWERED/INSUFFICIENT/REFUSED)
   ├─ abduction         : 논리 공백 역추론 → 연속 슬롯 후보 + parent_slot_id
   ├─ state_commands    : PUSH / NONE / PRUNE
   └─ strategy          : Sapien에 전달할 질문 전략
        │
        ▼
3. 평가 반영 → Slot.evidence / fulfillment_score / response_type 갱신
   Python 교정: ANSWERED + score < 0.7 → INSUFFICIENT로 재분류
        │
        ▼
3b. 슬롯 해소 라우팅 (process_slot_resolutions)
   ├─ ANSWERED (score ≥ 0.7) → 스택 제거 → verified_archive
   └─ REFUSED              → 스택 제거 → refused_archive
   ※ INSUFFICIENT는 다음 단계에서 처리
        │
        ▼
4. Abductive Branching + State Transition
   PUSH: INSUFFICIENT 부모 스택 제거 + 부모 evidence 상속 → 연속 슬롯 push
   PRUNE: 스택 초기화 + 다음 주제로 이동
        │
        ▼
5. within-topic depth prune / root topic 전환 판정
        │
        ▼
6. SapienStrategyPackage 생성 및 반환
```

### 저지연 모드 (비동기 병렬)

Hashimoto의 분석이 Sapien의 질문 생성을 블로킹하지 않습니다. strategy는 항상 직전 턴까지의 최신 분석 결과입니다.

```
Turn N:
  STT 도착
  ├─ Sapien: latest_strategy[N-1] + STT → 즉시 질문 생성 (블로킹 없음)
  └─ Hashimoto: STT 분석 시작 (백그라운드)

Sapien이 질문하는 동안:
  └─ Hashimoto: 분석 완료 → latest_strategy[N] 갱신

Turn N+1:
  STT 도착
  ├─ Sapien: latest_strategy[N] + STT → 즉시 질문 생성
  └─ Hashimoto: 새 STT 분석 시작 (백그라운드)
  ...
```

내부적으로 `asyncio.Queue` + 단일 워커를 사용하므로 `EngineState` 접근이 항상 직렬화되며, 별도의 락 없이도 경쟁 조건이 발생하지 않습니다.

---

## 프로젝트 구조

```
hashimoto/
├── main.py                        # CLI 데모 진입점 (자기소개서 기반)
├── requirements.txt
│
├── models/
│   ├── slot.py                    # Slot, SlotStatus, VerificationCriterion
│   └── io.py                      # 입출력 모델 전체
│
├── engine/
│   ├── state.py                   # EngineState: 스택·맵·카운터 관리
│   ├── hashimoto.py               # HashimotoEngine: 동기 오케스트레이터
│   └── low_latency.py             # LowLatencyHashimotoEngine: 비동기 오케스트레이터
│
└── llm/
    ├── prompts.py                 # 시스템 프롬프트 + 턴 메시지 빌더
    ├── client.py                  # 동기 Gemini SDK 래퍼 (structured output)
    ├── async_client.py            # 비동기 Gemini SDK 래퍼 (structured output)
    └── topic_extractor.py         # 자기소개서 → root_topic 추출
```

### 파일별 역할

#### `models/slot.py`

```python
class Slot(BaseModel):
    slot_id: str                   # snake_case 고유 ID
    proposition_statement: str     # 검증할 명제 (의문문 형태)
    verification_criteria: List    # 평가 기준 목록 (criterion + weight)
    expected_answer_depth: str     # Surface | Conceptual | Technical_Deep_Dive | Experiential
    status: SlotStatus             # PENDING | VERIFIED
    fulfillment_score: float       # 0.0–1.0
    evidence: Optional[str]        # LLM이 찾은 근거, 연속 슬롯이면 부모 근거 포함 ← 크로스-턴 메모리
    depth_at_creation: int         # 생성 시점의 Depth_Counter
    parent_slot_id: Optional[str]  # INSUFFICIENT로 제거된 부모 슬롯 ID (연속 슬롯에만 설정)
    resolution_type: Optional[str] # 아카이브 기록용: "VERIFIED" | "REFUSED"
```

`evidence`는 단순 메타데이터가 아닙니다. LLM 클라이언트가 대화 히스토리를 유지하지 않기 때문에, `to_context_str()`이 이 필드를 다음 턴의 프롬프트에 포함시켜 "직전 턴에서 무엇이 확인됐는가"를 LLM에 전달합니다. 연속 슬롯의 경우 부모 슬롯의 `proposition`과 `evidence`가 초기값으로 주입되어 LLM이 부모에서 무엇이 불충분했는지 파악할 수 있습니다. Slot Map이 Hashimoto의 유일한 크로스-턴 메모리입니다.

#### `models/io.py`

| 모델 | 방향 | 설명 |
|---|---|---|
| `EngineInput` | 입력 | STT 텍스트 + 멀티모달 벡터(불안도/자신감) + 턴 번호 |
| `HashimotoConfig` | 입력(세션) | 세션 단위 튜닝 파라미터. 엔진 생성 시 주입 (미지정 시 기본값) |
| `LLMAnalysisOutput` | LLM → Engine | slot_evaluations + abduction + state_commands + strategy. `response_schema`로 구조 강제 |
| `CriterionInput` | LLM → Engine | verification_criteria 항목(criterion+weight). Gemini structured output용 **닫힌 스키마** |
| `SapienStrategyPackage` | 출력 | Sapien 엔진에 전달하는 최종 전략 패키지 |

#### `engine/state.py`

세 가지 핵심 상태를 관리합니다.

- `global_slot_map`: `slot_id → Slot` 딕셔너리
- `priority_stack`: LIFO 슬롯 ID 목록 (인덱스 0 = 최상단)
- `depth_counter`: 현재 주제 연속 질문 횟수 (branch depth)
- `topic_question_counter`: 현재 root_topic 내 누적 질문 수

위 상수들은 `HashimotoConfig`의 **기본값**이며, 세션별 튜닝은 엔진 생성 시 `config`로 주입합니다 (미지정 시 기본값 사용).

```python
from models.io import HashimotoConfig

config = HashimotoConfig(
    prune_depth_threshold=5,        # within-topic depth prune 발동 깊이
    min_prune_depth_threshold=2,    # 불안도 최고 시 최소 임계값
    topic_question_threshold=10,    # root topic 강제 전환 누적 질문 수
    fulfillment_threshold=0.7,      # 이 이상이면 VERIFIED
    active_window=3,                # Multi-Slot Scanning 윈도우 크기
    anxiety_adjust=True,            # 불안도에 따른 depth 임계값 자동 하향
    analysis_model=None,            # 분석 LLM 모델 오버라이드 (None=기본)
)
```

#### `engine/hashimoto.py`

동기 오케스트레이터. `process_turn(EngineInput) → SapienStrategyPackage`.

```python
# 단일 주제
engine = HashimotoEngine(root_topic="...")

# 주제 목록 직접 지정
engine = HashimotoEngine.from_topics(["주제1", "주제2"], config=config)

# 자기소개서 기반 다중 주제
engine = HashimotoEngine.from_resume(resume_text, topic_count=3, config=config)
print(engine.current_topic)     # 현재 주제
print(engine.remaining_topics)  # 대기 중인 주제 목록
print(engine.session_complete)  # 모든 주제 소진 시 True (세션 종료 신호)
```

#### `engine/low_latency.py`

비동기 오케스트레이터. `submit_turn()`은 non-blocking이며, `latest_strategy`를 통해 최신 전략을 즉시 읽습니다.

```python
engine = LowLatencyHashimotoEngine.from_resume(resume_text, topic_count=3)
engine.is_busy          # 큐에 미처리 항목 여부
await engine.flush()    # 큐 전체 처리 완료 대기 (테스트·종료 시)
```

strategy 갱신 시 push 방식 알림이 필요하면 콜백을 등록합니다.

```python
engine = LowLatencyHashimotoEngine.from_resume(
    resume_text,
    topic_count=3,
    on_strategy_ready=lambda pkg: print("새 전략:", pkg.interaction_strategy["logic_goal"]),
)
```

#### `llm/topic_extractor.py`

`extract_topics(resume_text, count) → List[str]`

자기소개서를 분석하여 면접 주제를 추출합니다. 엔진 초기화 시 1회만 호출됩니다. 각 주제는 자기소개서에 실제로 언급된 경험·기술·역량에 근거하며, 면접관이 후속 질문을 이어갈 수 있을 만큼 구체적인 명사구 형태로 생성됩니다.

#### `llm/client.py` / `llm/async_client.py`

- 분석 모델 `gemini-3.1-flash-lite`, `thinking_budget=0`(저지연), **structured output**(`response_mime_type="application/json"` + `response_schema=LLMAnalysisOutput`) 사용. 응답은 `model_validate_json()`으로 직접 파싱하며 정규식 후처리가 없습니다.
- 생성자는 `model` 인자를 받아 세션별 오버라이드를 지원합니다(`HashimotoConfig.analysis_model`). 미지정 시 `_MODEL` 기본값.
- 시스템 프롬프트는 `__init__` 시점에 `GenerateContentConfig`에 고정 (`system_instruction`)
- **Stateless 설계**: 대화 히스토리를 누적하지 않습니다. 매 턴마다 단일 `contents` 메시지만 전송합니다. 멀티턴 맥락은 `active_slots_context`(슬롯 충족도·증거)가 대신하며, 일반적인 대화 맥락 보존은 Sapien의 역할입니다.
- `async_client.py`는 `client.aio.models.generate_content()`를 사용하며, `await` 기반으로 동작

#### `llm/prompts.py`

- `SYSTEM_PROMPT`: 역할 정의 + 출력 필드 설명 + 판단 기준 + **재질문 금지 지침**. JSON 구조 자체는 `response_schema`가 강제하므로 스키마 예시는 포함하지 않습니다. **캐싱 대상(안정적 접두사)**이므로 동적 값을 포함하지 않습니다.
- `build_turn_message()`: 매 턴마다 STT, 활성 슬롯, **이미 해소된 명제**(재질문 방지), Depth_Counter, 멀티모달 신호를 주입합니다.

---

## LLM 출력 스키마

LLM은 매 턴 아래 JSON을 반환합니다. 이 구조는 `response_schema=LLMAnalysisOutput`로 API 단에서 강제되며(`verification_criteria` 항목은 `CriterionInput`), 응답은 `model_validate_json()`으로 바로 파싱됩니다. 슬롯의 스택 관리·아카이브 처리는 Python 시스템이 담당하며, LLM은 평가와 명제 생성만 수행합니다.

```json
{
  "slot_evaluations": [
    {
      "slot_id": "string",
      "fulfillment_score": 0.0,
      "evidence": "근거 또는 부재 이유",
      "response_type": "ANSWERED | INSUFFICIENT | REFUSED"
    }
  ],
  "abduction": {
    "observed_gap": "이번 답변에서 발견된 논리적 공백",
    "abductive_hypothesis": "공백 원인에 대한 가추적 가설",
    "parent_slot_id": "이 연속 슬롯이 보완하는 부모 슬롯의 ID",
    "emergent_slot": {
      "slot_id": "new_slot_id",
      "proposition_statement": "검증할 명제",
      "verification_criteria": [{"criterion": "기준", "weight": 0.6}],
      "expected_answer_depth": "Conceptual"
    }
  },
  "state_commands": {
    "stack_action": "PUSH | NONE | PRUNE",
    "depth_increment": 1,
    "slots_to_pop": []
  },
  "strategy": {
    "logic_goal": "이번 질문의 논리적 목표",
    "logical_gap_to_bridge": "메워야 할 논리적 공백",
    "interviewer_intent": "면접관의 숨은 의도",
    "focus_point": "질문의 핵심 포인트",
    "suggested_tone": "Analytical_Pressure | Neutral_Curious | Supportive | Skeptical_Professional"
  }
}
```

| `response_type` 값 | 의미 | Python 동작 |
|---|---|---|
| `ANSWERED` | 답변 시도 있음 (score로 충족도 판단) | score ≥ 0.7이면 `verified_archive` 이동 |
| `INSUFFICIENT` | 시도했으나 핵심 미충족 + `abduction` 필수 | 부모 제거 + 연속 슬롯 PUSH |
| `REFUSED` | "모르겠다" / 답변 의사 없음 | `refused_archive` 이동 |

> `abduction`은 `response_type == "INSUFFICIENT"`일 때만 제공됩니다. 그 외에는 `null`로 설정됩니다.

---

## 설치 및 실행

### 1. 의존성 설치

```bash
pip install -r requirements.txt
```

### 2. 환경 변수 설정

프로젝트 루트에 `.env` 파일을 생성합니다.

```
GEMINI_API_KEY=AIza...
```

### 3. CLI 데모 실행

```bash
python main.py
```

자기소개서를 붙여넣으면 주제를 자동 추출하고, 이후 턴마다 지원자 발화와 멀티모달 신호를 입력받습니다. 주제 전환 시점에 알림이 표시됩니다.

### 4. 표준 모드 코드 예시

```python
from engine.hashimoto import HashimotoEngine
from models.io import EngineInput, MultimodalVector

engine = HashimotoEngine.from_resume(resume_text, topic_count=3)
print(engine.current_topic)     # 첫 번째 추출 주제

result = engine.process_turn(
    EngineInput(
        stt_text="Redux를 주로 사용했고, Context API도 써봤습니다.",
        multimodal=MultimodalVector(anxiety=30.0, confidence=70.0),
        turn_number=1,
    )
)
print(result.interaction_strategy)
```

### 5. 저지연 모드 코드 예시

```python
import asyncio
from engine.low_latency import LowLatencyHashimotoEngine
from models.io import EngineInput, MultimodalVector

async def run():
    async with LowLatencyHashimotoEngine.from_resume(resume_text, topic_count=3) as engine:
        while True:
            stt = await get_stt()                    # 지원자 발화 수신

            engine.submit_turn(                       # 백그라운드 분석 시작 (즉시 반환)
                EngineInput(stt_text=stt, multimodal=MultimodalVector())
            )

            strategy = engine.latest_strategy        # 직전 턴 분석 결과 즉시 읽기
            question = await sapien.generate(stt, strategy)
            await speak(question)
            # Hashimoto는 이 시간 동안 현재 STT 분석 완료 → latest_strategy 갱신

asyncio.run(run())
```

---

## Sapien 연동 포인트

`SapienStrategyPackage.interaction_strategy` 딕셔너리를 Sapien 엔진에 전달합니다.

```json
{
  "logic_goal": "...",
  "logical_gap_to_bridge": "...",
  "interviewer_persona_guidance": {
    "intent": "...",
    "emotion_direction": "Neutral_Curious",
    "focus_point": "..."
  },
  "current_context": {
    "topic": "React 상태관리 아키텍처 경험",
    "depth_level": 2,
    "topic_changed": false,
    "transition_hint": null,
    "multimodal_feedback_requirement": null,
    "resolved_history": [
      {"proposition": "Redux와 Context API의 선택 기준은?", "status": "검증됨"}
    ]
  }
}
```

| 필드 | 설명 |
|---|---|
| `topic_changed` | `true`이면 root topic 전환 발생. Sapien이 주제 전환 멘트를 생성할 수 있음 |
| `transition_hint` | `"direction_change"`이면 within-topic depth prune 발생. Sapien이 "잘 알겠습니다" / "자, 그러면" 등 추임새 삽입 권장. `null`이면 일반 진행 |
| `multimodal_feedback_requirement` | 불안도 > 70 또는 자신감 < 30일 때 자동으로 채워져 Sapien의 어조 조정을 유도 |
| `resolved_history` | 이미 검증·거부된 명제 목록(`proposition`+`status`). 질문 생성 시 재질문을 피하도록 참고. 빈 배열일 수 있음 |

### 아카이브 접근

면접 종료 후 엔진 인스턴스에서 직접 아카이브를 읽을 수 있습니다.

```python
engine.verified_archive   # 충족 응답으로 해소된 슬롯 목록 (전 주제 포함 누적)
engine.refused_archive    # 거부·모름으로 해소된 슬롯 목록 (전 주제 포함 누적)
```

> 아카이브는 topic 전환 후에도 소멸하지 않습니다. 이전 주제에서 동일한 슬롯이 재생성되어 중복 질문이 발생하는 것을 방지하는 참조 목적으로 활용할 수 있습니다.
