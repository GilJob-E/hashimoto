# Hashimoto Engine — Design Constraints

이 문서는 프로젝트에 적용된 설계 결정과 변경 불가 원칙을 기록합니다.
기능 추가·리팩토링·버그 수정 시 아래 항목을 침범하지 않도록 확인하세요.

---

## 1. 초기화: 자기소개서 기반 주제 추출

**결정**: `root_topic`을 호출자가 직접 지정하는 방식과, 자기소개서를 주입하면 LLM이 자동으로 `topic_count`개의 주제를 추출하는 방식 두 가지를 모두 지원한다.

**구현 위치**: `llm/topic_extractor.py` → `extract_topics(resume_text, count)`  
**진입점**: `HashimotoEngine.from_resume(resume_text, topic_count)` / `LowLatencyHashimotoEngine.from_resume(...)`

**침범 금지 사항**
- `from_resume()`는 엔진 초기화 시 **1회만** LLM을 호출한다. 면접 턴 루프 안에서 호출되어서는 안 된다.
- 추출된 첫 번째 주제가 `root_topic`이 되고, 나머지는 `topic_queue`에 순서대로 적재된다.
- `topic_count` 파라미터를 제거하거나 하드코딩하지 않는다.

---

## 2. 다중 주제 관리: topic_queue

**결정**: 엔진은 내부에 `_topic_queue: List[str]`를 유지하며, PRUNE 발생 시 큐 선두에서 다음 주제를 꺼내 자동으로 전환한다.

**구현 위치**: `engine/hashimoto.py`, `engine/low_latency.py`

**침범 금지 사항**
- 주제 전환 시 반드시 `EngineState`를 새 인스턴스로 교체하고(`self.state = EngineState(next_topic)`), LLM 히스토리를 초기화한다(`self._llm.reset_history()`).
- 큐가 비어있을 때 PRUNE이 발생하면 현재 주제에서 `state.prune()`만 호출하고 히스토리를 초기화한다. 오류를 발생시키지 않는다.
- `topic_changed` 플래그를 `SapienStrategyPackage`의 `current_context`에 항상 포함한다. Sapien이 전환 여부를 알아야 하기 때문이다.

---

## 3. PRUNE의 두 단계: within-topic depth prune vs root topic 전환

**결정**: PRUNE은 두 가지 다른 동작으로 분리된다.

| 단계 | 트리거 | 동작 | root_topic 변경 |
|---|---|---|---|
| **within-topic depth prune** | 시스템: `depth_counter >= threshold` AND 스택에 더 낮은 depth 슬롯 존재 | `state.depth_prune()` — 깊은 슬롯 제거, 낮은 depth 슬롯으로 복귀 | **유지** |
| **root topic 전환** | LLM: `stack_action == "PRUNE"` OR 시스템: `topic_question_counter >= TOPIC_QUESTION_THRESHOLD` | `EngineState` 교체 또는 `state.prune()` | **변경** |

**구현 위치**: `engine/state.py` → `should_depth_prune()`, `depth_prune()`, `should_switch_topic()`, `engine/hashimoto.py` / `engine/low_latency.py` step 5

**판정 로직 (변경 금지)** — 임계값은 세션 config에서 읽는다(§12)
```python
llm_pruned = analysis.state_commands.stack_action.upper() == "PRUNE"
depth_threshold = effective_depth_threshold(
    engine_input.multimodal.anxiety,
    self.config.prune_depth_threshold,
    self.config.min_prune_depth_threshold,
    self.config.anxiety_adjust,
)

topic_switch_needed = llm_pruned or self.state.should_switch_topic(
    self.config.topic_question_threshold
)

depth_prune_needed = (
    not topic_switch_needed
    and self.state.should_depth_prune(depth_threshold)
)

if depth_prune_needed:
    → state.depth_prune(), transition_hint="direction_change"
elif topic_switch_needed:
    → EngineState(next_topic, self.config) 교체  (큐가 비면 state.prune() + _session_exhausted=True)
```

**침범 금지 사항**
- `topic_switch_needed`를 `depth_prune_needed`보다 먼저 평가한다. 두 조건이 동시에 충족되면 topic 전환이 우선한다.
- `depth_prune_needed`에 `not topic_switch_needed` 가드를 유지한다.
- `depth_prune()`은 `topic_question_counter`를 리셋하지 않는다. 같은 root_topic 내 누적 질문 수는 보존해야 한다.
- `should_depth_prune()`의 `threshold` 파라미터를 제거하지 않는다.
- `should_switch_topic()`의 `threshold` 파라미터를 제거하지 않는다.

---

## 4. Root Topic 전환 트리거: topic_question_counter

**결정**: 단일 root_topic에 대한 누적 질문 횟수(`topic_question_counter`)가 `TOPIC_QUESTION_THRESHOLD`에 도달하면 시스템이 강제로 topic 전환을 트리거한다. 이를 통해 하나의 주제에서 지나치게 긴 문답이 이어지는 것을 방지한다.

**구현 위치**: `engine/state.py` → `EngineState.topic_question_counter`, `should_switch_topic(threshold)`

**동작 규칙**
- `topic_question_counter`는 `apply_state_commands()` 호출 시마다 무조건 +1 증가한다.
- `depth_prune()` 호출 시 **리셋되지 않는다**. root_topic이 동일한 한 누적을 유지한다.
- `EngineState`가 새 인스턴스로 교체되는 시점(root topic 전환)에만 0으로 리셋된다.
- `TOPIC_QUESTION_THRESHOLD = 10` (기본값, `engine/state.py` 상단 상수로 중앙 관리).

**침범 금지 사항**
- `topic_question_counter` 증가를 PRUNE 여부와 무관하게 항상 수행한다. 조건 분기 안으로 넣지 않는다.
- `TOPIC_QUESTION_THRESHOLD`를 다른 파일에서 하드코딩하지 않는다.

---

## 5. 불안도 기반 동적 depth 임계값

**결정**: 지원자의 불안도(anxiety)가 높을수록 `depth_counter` 임계값을 낮춰 주제를 더 빨리 전환한다.

**구현 위치**: `engine/state.py` → `effective_depth_threshold(anxiety)`

**매핑 (기본값 기준; `HashimotoConfig`로 세션별 조정 가능 — §12, 삭제 불가)**
| anxiety | threshold |
|---|---|
| 0 – 50 | 5 (prune_depth_threshold 기본값) |
| 75 | 4 |
| 87.5 | 3 |
| 100 | 2 (min_prune_depth_threshold 기본값) |

`anxiety_adjust=False`이면 불안도와 무관하게 항상 base를 사용한다.

**침범 금지 사항**
- `effective_depth_threshold()`를 우회하여 `PRUNE_DEPTH_THRESHOLD` 상수를 `should_prune()`에 직접 전달하지 않는다.
- `anxiety` 값은 반드시 현재 턴의 `engine_input.multimodal.anxiety`에서 읽는다. 캐싱하거나 이전 턴 값을 재사용하지 않는다.
- `PRUNE_DEPTH_THRESHOLD = 5`, `MIN_PRUNE_DEPTH_THRESHOLD = 2`는 `engine/state.py`에서 중앙 관리한다. 다른 파일에서 하드코딩하지 않는다.

---

## 6. 저지연 모드: 비동기 파이프라인

**결정**: `LowLatencyHashimotoEngine`은 STT 도착 즉시 반환하고(`submit_turn`), Hashimoto 분석은 백그라운드에서 실행한다. Sapien은 `latest_strategy`를 블로킹 없이 읽어 질문을 생성한다.

**구현 위치**: `engine/low_latency.py`, `llm/async_client.py`

**아키텍처 불변 조건**
- 내부 큐는 `asyncio.Queue` + **단일 워커 태스크**로만 소비한다. 복수의 워커를 추가하면 `EngineState`에 경쟁 조건이 발생한다.
- `_latest_strategy`는 워커 태스크만 쓴다. 외부에서 직접 갱신하지 않는다.
- 워커 실패 시 기존 `latest_strategy`를 유지한다. Sapien이 마지막으로 유효한 전략을 계속 사용할 수 있어야 한다.

**침범 금지 사항**
- `submit_turn()`을 블로킹으로 전환하지 않는다.
- 비동기 Gemini 클라이언트(`AsyncHashimotoLLMClient`, `client.aio.models`)를 동기 클라이언트로 교체하지 않는다.
- 워커를 `threading.Thread`나 `concurrent.futures`로 교체하지 않는다. asyncio 단일 스레드 모델을 유지해야 상태 보호가 성립한다.

---

## 7. LLM 호출: 턴당 단일 호출

**결정**: 매 턴마다 LLM을 **1회만** 호출하며, 슬롯 평가 + 가추 + 상태 명령 + 전략 전달을 하나의 응답에서 받는다.

**구현 위치**: `llm/client.py`, `llm/async_client.py`, `llm/prompts.py`

**침범 금지 사항**
- 슬롯 평가와 가추를 별도 LLM 호출로 분리하지 않는다.
- 시스템 프롬프트(`SYSTEM_PROMPT`)에 동적 값(턴 번호, 슬롯 내용, 멀티모달 신호 등)을 삽입하지 않는다. 동적 값은 모두 `build_turn_message()`의 유저 메시지에만 포함한다. **시스템 프롬프트는 불변이어야 한다.**
- `system_instruction`은 `__init__` 시점에 `GenerateContentConfig`에 고정한다. 매 호출마다 재생성하지 않는다.

---

## 8. LLM 모델 및 파라미터 (저지연 최적화)

**결정**: 분석 호출은 `gemini-3.1-flash-lite` + `thinking_budget=0` + structured output(`response_schema`)을 사용한다. 이는 면접 분석(분류·명제 추출)이 긴 추론 체인을 요하지 않는다는 판단 하에 실시간 면접의 latency를 우선한 결정이며, 벤치마크로 검증되었다(gemini-2.5-flash 대비 약 1.6배 단축, 가추 품질 유지).

**구현 위치**: `llm/client.py`, `llm/async_client.py` 상단 상수 및 config; `llm/topic_extractor.py`

```python
_MODEL = "gemini-3.1-flash-lite"

# HashimotoLLMClient / AsyncHashimotoLLMClient
self._config = types.GenerateContentConfig(
    system_instruction=build_system_prompt(),
    response_mime_type="application/json",
    response_schema=LLMAnalysisOutput,
    thinking_config=types.ThinkingConfig(thinking_budget=0),
)

# topic_extractor — gemini-2.5-flash-lite, thinking 불필요
types.GenerateContentConfig(system_instruction=_SYSTEM)
```

**불변 조건**
- structured output을 유지한다: `response_mime_type="application/json"` + `response_schema=LLMAnalysisOutput`. JSON 파싱 견고성을 확보하고 시스템 프롬프트에서 스키마 서술을 제거하기 위함이다. 수동 정규식 파싱(`_parse_output`)으로 되돌리지 않는다.
- `response_schema`에 들어가는 Pydantic 모델은 **닫힌 스키마**여야 한다. 개방형 `Dict[str, Any]`(additionalProperties)는 Gemini API가 전송 단계에서 거부한다. 그래서 `verification_criteria`는 `CriterionInput`으로 닫혀 있어야 한다.
- 분석 모델은 세션별 `HashimotoConfig.analysis_model`로 오버라이드한다(§12). 클라이언트는 `model` 인자를 받아 `_MODEL`을 기본값으로 쓴다. 모델명을 다른 곳에 추가 하드코딩하지 않는다.
- 환경 변수 키는 `GEMINI_API_KEY`를 사용한다.

**변경 이력 / 재변경 시 규칙**
- 초기 설계는 `gemini-2.5-pro` + `thinking_budget=-1`(품질 우선)이었으나, 실시간 면접의 latency 요구에 따라 저지연 구성으로 전환했다(사용자 승인 완료).
- thinking을 재활성화하거나 상위 모델로 되돌릴 경우, 반드시 `test_console.py`의 latency 측정으로 회귀를 확인하고 재승인을 받는다.

---

## 9. 출력 스키마: SapienStrategyPackage

**결정**: `interaction_strategy.current_context`에는 반드시 `topic`, `depth_level`, `topic_changed`, `transition_hint`, `multimodal_feedback_requirement`, `resolved_history` 여섯 필드가 포함된다.

**구현 위치**: `models/io.py` → `SapienStrategyPackage.from_analysis()`

**침범 금지 사항**
- `topic_changed` 필드를 제거하거나 다른 위치로 이동하지 않는다. Sapien이 root topic 전환 감지에 사용한다.
- `transition_hint` 필드를 제거하지 않는다. `"direction_change"` 값은 Sapien이 "잘 알겠습니다" / "자, 그러면" 등의 추임새를 삽입하는 신호다. `None`이면 추임새 없음.
- `multimodal_feedback_requirement`의 자동 생성 로직(anxiety > 70, confidence < 30)을 삭제하지 않는다.
- `resolved_history` 필드를 제거하지 않는다. 질문 LLM의 재질문 방지에 사용한다(§13).
- `from_analysis()`의 `topic_changed` 파라미터 기본값은 `False`를 유지한다.
- `from_analysis()`의 `transition_hint` 파라미터 기본값은 `None`을 유지한다.

---

## 10. 슬롯 해소: 3경로 모두 현재 슬롯을 스택에서 제거

**결정**: 모든 응답 유형은 반드시 현재 슬롯을 스택에서 제거하는 경로로 귀결된다. 슬롯의 스택 잔류가 무한히 이어지지 않도록 각 경로는 명확히 정의된다.

**구현 위치**: `engine/state.py` → `apply_evaluations()`, `process_slot_resolutions()`, `apply_state_commands()`; `engine/hashimoto.py` / `engine/low_latency.py` → `_verified_archive`, `_refused_archive`

**3경로 정의**

| response_type | 조건 | Python 동작 | 아카이브 |
|---|---|---|---|
| `ANSWERED` | score ≥ 0.7 | 스택 제거 | `_verified_archive` |
| `REFUSED` | 거부·모름 | 스택 제거 | `_refused_archive` |
| `INSUFFICIENT` | score < 0.7, 답변 시도 있음 | PUSH 시 부모 스택 제거 + 연속 슬롯 생성 | 없음 |

**Python 교정 (변경 금지)**
LLM이 `ANSWERED`로 분류했더라도 `fulfillment_score < FULFILLMENT_THRESHOLD`이면 Python이 `INSUFFICIENT`로 교정한다. `apply_evaluations()` 내에서 수행되며 LLM 분류보다 Python 교정이 우선한다.

**호출 순서 (변경 금지)**
```
apply_evaluations()          ← response_type 기록 + Python 교정
    ↓
process_slot_resolutions()   ← ANSWERED/REFUSED 스택 제거 + 아카이브 반환
    ↓
apply_state_commands()       ← INSUFFICIENT 부모 스택 제거 + 연속 슬롯 PUSH
```

`process_slot_resolutions()`를 `apply_state_commands()` 이전에 호출한다. INSUFFICIENT 부모는 PUSH 시점까지 스택에 잔류해야 하므로 `process_slot_resolutions()`에서는 제거하지 않는다.

**아카이브 범위 (변경 금지)**
- `_verified_archive`, `_refused_archive`는 `EngineState`가 아닌 엔진(`HashimotoEngine` / `LowLatencyHashimotoEngine`) 인스턴스에 위치한다.
- topic switch 후에도 아카이브는 소멸하지 않고 누적된다. 이전 주제에서 동일한 슬롯이 재생성되는 것을 방지하기 위한 참조로 활용된다.
- PRUNE으로 강제 제거된 슬롯(아직 평가되지 않은 PENDING 슬롯)은 어느 아카이브에도 포함되지 않고 소멸한다.

**연속 슬롯 evidence 상속 (변경 금지)**
`apply_state_commands()`의 PUSH 처리 시 부모 슬롯의 `proposition_statement`와 `evidence`를 연속 슬롯의 초기 `evidence`에 주입한다. 형식:
```
[부모 명제] {parent.proposition_statement} | [기존 응답 근거] {parent.evidence}
```
연속 슬롯의 `parent_slot_id`는 부모 슬롯 ID로 설정한다. 부모 슬롯 객체는 `global_slot_map`에 보존(계보 참조용)되고 `priority_stack`에서만 제거된다.

**침범 금지 사항**
- `process_slot_resolutions()`에서 INSUFFICIENT 슬롯을 제거하지 않는다. PUSH 흐름에서 처리한다.
- 아카이브를 `EngineState` 안으로 이동하지 않는다. topic switch 시 `EngineState`가 교체되므로 아카이브 데이터가 소멸된다.
- PUSH 시 `parent_slot_id`를 `apply_state_commands()`에 전달한다. LLM이 명시한 `parent_slot_id` 우선, 없으면 스택 최상단을 부모로 사용한다.

---

## 11. LLM 호출 무상태성(Stateless): Slot Map이 대화 메모리

**결정**: LLM 클라이언트는 대화 히스토리(`_history`)를 누적하지 않는다. 매 턴 단일 user 메시지만 전송하며, 크로스-턴 맥락은 `active_slots_context`(슬롯의 충족도·증거·상태)가 전담한다.

**구현 위치**: `llm/client.py`, `llm/async_client.py`, `models/slot.py` → `to_context_str()`

**설계 근거**

일반적인 대화 맥락 보존은 Sapien이 담당한다. Hashimoto가 턴 간에 알아야 할 것은 "어떤 명제가 얼마나 채워졌는가"뿐이며, 이는 `Slot.evidence`와 `Slot.fulfillment_score`에 이미 기록된다. 전체 발화 이력을 넘기는 것은 불필요한 토큰 낭비이자 주제 간 오염 위험이다.

**맥락 전달 흐름**

```
turn N:   analyze_turn() → LLM이 evidence="A를 언급, B는 미서술"을 반환
           → Slot.evidence 갱신

turn N+1: to_context_str()가 Evidence So Far: A를 언급, B는 미서술 포함
           → 새 LLM이 직전 턴의 판단 결과를 인식한 채로 분석

[연속 슬롯의 경우]
turn N+1: to_context_str()가 Derived From + Evidence(부모 명제 + 기존 근거) 포함
           → LLM이 부모 슬롯에서 무엇이 불충분했는지 파악한 채로 분석
```

**침범 금지 사항**
- `HashimotoLLMClient` / `AsyncHashimotoLLMClient`에 `_history` 리스트를 재추가하지 않는다.
- `messages` 파라미터에 이전 턴의 user/assistant 쌍을 포함시키지 않는다. `[{"role": "user", "content": user_message}]` 형태를 유지한다.
- `Slot.to_context_str()`에서 `evidence` 필드 출력을 제거하지 않는다. 이것이 히스토리 없이 크로스-턴 맥락을 전달하는 유일한 수단이다.
- 연속 슬롯 생성 시 부모 evidence 주입을 생략하지 않는다. 주입하지 않으면 LLM이 동일한 질문을 재생성할 수 있다.
- `reset_history()`는 API 호환성을 위해 유지하되 no-op으로 남긴다. PRUNE 후 engine이 이를 호출하는 것은 설계 의도다(Section 2 참조).
- `fulfillment_score` 임계값은 프롬프트(`SYSTEM_PROMPT`)와 코드(`HashimotoConfig.fulfillment_threshold`, 기본 0.7) 양쪽에서 반드시 동일한 값을 사용한다. 어느 한쪽만 변경하지 않는다.

---

## 12. 세션 설정: HashimotoConfig

**결정**: 면접 세션 단위 튜닝 파라미터를 `HashimotoConfig`(`models/io.py`)로 엔진 생성 시 주입한다. §4·§5·§8에 등장하는 임계값/모델 상수들은 이제 `HashimotoConfig`의 **기본값**이며, 오케스트레이터가 세션별로 오버라이드한다.

**구현 위치**: `models/io.py` → `HashimotoConfig`; `engine/state.py`, `engine/hashimoto.py`, `engine/low_latency.py`로 주입

**설정 항목**: `prune_depth_threshold`, `min_prune_depth_threshold`, `topic_question_threshold`, `fulfillment_threshold`, `active_window`, `anxiety_adjust`, `analysis_model`.

**침범 금지 사항**
- 모든 필드는 기본값을 가져 `HashimotoConfig()`만으로 기존 동작을 재현한다. 기본값을 바꿀 때는 §4·§5·§8의 표시값과 모듈 상수(`PRUNE_DEPTH_THRESHOLD` 등)도 함께 갱신한다.
- config는 `EngineState`로 전달되며, **주제 전환·reset 시 새 `EngineState`에도 반드시 동일 config를 다시 전달**한다(`EngineState(next_topic, self.config)`). 누락하면 세션 중간에 기본값으로 되돌아간다.
- `effective_depth_threshold(anxiety)`는 하위호환을 위해 모듈 상수를 기본 인자로 갖되, 엔진은 config 값(base/floor/adjust)을 명시 전달한다(§3 코드 참조).
- 모듈 상수는 표시·하위호환·display용으로 유지한다. 제거하지 않는다.
- `fulfillment_threshold`를 기본값(0.7)에서 바꾸면 `SYSTEM_PROMPT`의 "0.7" 서술과 불일치한다. 둘을 함께 조정한다(§11).

**세션 종료 신호 / 팩토리**
- 모든 주제가 소진되어 더 전환할 곳이 없으면 `_session_exhausted=True`가 set되고 `session_complete` 프로퍼티로 노출된다. 오케스트레이터는 이 신호로 면접 종료를 판단한다(api.py `/process_turn` 응답에도 포함).
- `from_topics([...])` 팩토리로 자소서 없이 주제 목록만으로 초기화할 수 있다. config를 함께 받는다.

---

## 13. 재질문 방지: 해소 아카이브를 양 LLM에 주입

**결정**: 이미 검증(verified)되거나 거부(refused)된 명제를 다시 묻지 않도록, 두 아카이브를 요약해 분석 LLM과 질문 LLM **양쪽** 컨텍스트에 주입한다.

**구현 위치**: `engine/state.py` → `resolved_context_str()`, `resolved_history_list()`; `llm/prompts.py` → `build_turn_message`의 `[이미 해소된 명제]` 블록 + `SYSTEM_PROMPT`의 재질문 금지 지침; `models/io.py` → `SapienStrategyPackage.current_context.resolved_history`; 두 엔진의 `process_turn` / `_run_pipeline`.

**주입 시점 (변경 금지)**
- 분석 LLM용 `resolved_context`는 LLM 호출 **전**에 생성한다 → 이전 턴까지의 해소분이 가추(abduction) 단계에서 중복 슬롯 생성을 막는다.
- 질문 LLM용 `resolved_history`는 이번 턴 해소분 적재 **후**에 패키지에 싣는다 → 방금 답한/거부한 슬롯도 즉시 재질문 방지.

**침범 금지 사항**
- `SYSTEM_PROMPT`의 재질문 금지 지침("의미적으로 중복되는 emergent_slot 생성 금지", "거부된 명제 재추궁 금지")을 제거하지 않는다.
- `resolved_history`를 `SapienStrategyPackage.current_context`에서 제거하지 않는다(§9).
- 토큰 관리를 위해 분석용은 `slot_id+명제+상태`, 질문용은 `명제+상태`만 싣고 evidence 전문은 싣지 않는다.
- 아카이브는 엔진 레벨(`verified_archive`/`refused_archive` 공개 프로퍼티)이며 topic 전환 후에도 누적된다(§10). 따라서 주제 간 재질문도 방어된다.

---

## 변경 시 체크리스트

새 기능을 추가하거나 기존 코드를 수정할 때 아래를 확인한다.

**LLM 호출 불변 조건**
- [ ] `submit_turn()` / `process_turn()`이 여전히 단일 LLM 호출만 수행하는가?
- [ ] 시스템 프롬프트에 동적 값이 삽입되지 않았는가?
- [ ] `messages` 파라미터가 단일 user 메시지만 포함하는가? (히스토리 누적 금지)
- [ ] `SYSTEM_PROMPT`의 fulfillment 임계값과 `FULFILLMENT_THRESHOLD` 상수가 일치하는가?
- [ ] `system_instruction`이 `__init__` 시점의 `GenerateContentConfig`에 고정되어 있는가?
- [ ] 분석 config가 structured output(`response_mime_type` + `response_schema`)과 `thinking_budget=0`을 유지하는가? (모델/thinking 변경 시 latency를 재측정·재승인했는가?)
- [ ] `response_schema`에 쓰이는 모델이 닫힌 스키마인가? (개방형 `Dict[str, Any]` 미사용)

**슬롯 해소 흐름**
- [ ] `apply_evaluations()` → `process_slot_resolutions()` → `apply_state_commands()` 순서가 유지되는가?
- [ ] `apply_evaluations()`에서 ANSWERED + score < threshold를 INSUFFICIENT로 교정하는가?
- [ ] ANSWERED/REFUSED 슬롯이 엔진 레벨(`_verified_archive` / `_refused_archive`)에 적재되는가?
- [ ] PUSH 시 부모 슬롯이 스택에서 제거되고 evidence가 연속 슬롯에 상속되는가?
- [ ] `parent_slot_id`가 `apply_state_commands()`에 전달되는가?
- [ ] `Slot.to_context_str()`이 `evidence`와 `parent_slot_id`(있을 경우) 필드를 포함하여 반환하는가?

**PRUNE / 주제 전환**
- [ ] `topic_switch_needed`가 `depth_prune_needed`보다 먼저 평가되는가?
- [ ] `depth_prune_needed`에 `not topic_switch_needed` 가드가 있는가?
- [ ] `effective_depth_threshold(anxiety)`를 경유하여 임계값을 계산하는가?
- [ ] `topic_question_counter` 증가가 PRUNE 여부와 무관하게 항상 수행되는가?
- [ ] `depth_prune()` 호출 시 `topic_question_counter`가 리셋되지 않는가?
- [ ] `topic_changed`와 `transition_hint` 플래그가 `SapienStrategyPackage`에 포함되어 반환되는가?

**세션 설정 / 재질문 방지**
- [ ] 주제 전환·reset 시 새 `EngineState`에 `self.config`를 다시 전달하는가? (§12)
- [ ] `effective_depth_threshold` / `should_switch_topic`에 config 값을 전달하는가? (모듈 상수 직접 사용 금지)
- [ ] `resolved_context`를 분석 LLM 호출 **전**, `resolved_history`를 해소분 적재 **후** 패키지에 싣는가? (§13)
- [ ] `SYSTEM_PROMPT`의 재질문 금지 지침과 `current_context.resolved_history`가 유지되는가?

**아키텍처**
- [ ] `LowLatencyHashimotoEngine`의 워커가 단일 태스크인가?
- [ ] `from_resume()` / `from_topics()`가 턴 루프 외부에서만 호출되는가?
