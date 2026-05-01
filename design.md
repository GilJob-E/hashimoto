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

## 3. 주제 전환 트리거: 시스템 주도 PRUNE

**결정**: 주제 전환은 LLM의 출력(`stack_action == "PRUNE"`)에 의존하지 않는다. Python 시스템이 depth 조건을 독립적으로 체크하여 LLM이 PRUNE을 누락하더라도 강제 전환한다.

**구현 위치**: `engine/state.py` → `should_prune(threshold)`, `engine/hashimoto.py` / `engine/low_latency.py` step 5

**판정 로직 (변경 금지)**
```python
llm_pruned   = analysis.state_commands.stack_action.upper() == "PRUNE"
system_prune = not llm_pruned and self.state.should_prune(
    effective_depth_threshold(engine_input.multimodal.anxiety)
)
if llm_pruned or system_prune:
    → 주제 전환 처리
```

**침범 금지 사항**
- `system_prune` 체크를 삭제하거나 LLM 조건 뒤로 이동시키지 않는다.
- `not llm_pruned` 가드를 유지한다. `apply_state_commands`가 LLM PRUNE을 처리하면 `depth_counter`가 0으로 리셋되므로 `should_prune()`은 자동으로 False를 반환한다. 이 순서에 의존하는 설계다.
- `should_prune()`의 시그니처에서 `threshold` 파라미터를 제거하지 않는다.

---

## 4. 불안도 기반 동적 depth 임계값

**결정**: 지원자의 불안도(anxiety)가 높을수록 `depth_counter` 임계값을 낮춰 주제를 더 빨리 전환한다.

**구현 위치**: `engine/state.py` → `effective_depth_threshold(anxiety)`

**매핑 (조정 가능, 삭제 불가)**
| anxiety | threshold |
|---|---|
| 0 – 50 | 5 (PRUNE_DEPTH_THRESHOLD) |
| 75 | 4 |
| 87.5 | 3 |
| 100 | 2 (MIN_PRUNE_DEPTH_THRESHOLD) |

**침범 금지 사항**
- `effective_depth_threshold()`를 우회하여 `PRUNE_DEPTH_THRESHOLD` 상수를 `should_prune()`에 직접 전달하지 않는다.
- `anxiety` 값은 반드시 현재 턴의 `engine_input.multimodal.anxiety`에서 읽는다. 캐싱하거나 이전 턴 값을 재사용하지 않는다.
- `PRUNE_DEPTH_THRESHOLD = 5`, `MIN_PRUNE_DEPTH_THRESHOLD = 2`는 `engine/state.py`에서 중앙 관리한다. 다른 파일에서 하드코딩하지 않는다.

---

## 5. 저지연 모드: 비동기 파이프라인

**결정**: `LowLatencyHashimotoEngine`은 STT 도착 즉시 반환하고(`submit_turn`), Hashimoto 분석은 백그라운드에서 실행한다. Sapien은 `latest_strategy`를 블로킹 없이 읽어 질문을 생성한다.

**구현 위치**: `engine/low_latency.py`, `llm/async_client.py`

**아키텍처 불변 조건**
- 내부 큐는 `asyncio.Queue` + **단일 워커 태스크**로만 소비한다. 복수의 워커를 추가하면 `EngineState`에 경쟁 조건이 발생한다.
- `_latest_strategy`는 워커 태스크만 쓴다. 외부에서 직접 갱신하지 않는다.
- 워커 실패 시 기존 `latest_strategy`를 유지한다. Sapien이 마지막으로 유효한 전략을 계속 사용할 수 있어야 한다.

**침범 금지 사항**
- `submit_turn()`을 블로킹으로 전환하지 않는다.
- `AsyncAnthropic` 클라이언트(`llm/async_client.py`)를 동기 `Anthropic` 클라이언트로 교체하지 않는다.
- 워커를 `threading.Thread`나 `concurrent.futures`로 교체하지 않는다. asyncio 단일 스레드 모델을 유지해야 상태 보호가 성립한다.

---

## 6. LLM 호출: 턴당 단일 호출

**결정**: 매 턴마다 LLM을 **1회만** 호출하며, 슬롯 평가 + 가추 + 상태 명령 + 전략 전달을 하나의 응답에서 받는다.

**구현 위치**: `llm/client.py`, `llm/async_client.py`, `llm/prompts.py`

**침범 금지 사항**
- 슬롯 평가와 가추를 별도 LLM 호출로 분리하지 않는다.
- 시스템 프롬프트(`SYSTEM_PROMPT`)에 동적 값(턴 번호, 슬롯 내용, 멀티모달 신호 등)을 삽입하지 않는다. 동적 값은 모두 `build_turn_message()`의 유저 메시지에만 포함한다. **시스템 프롬프트는 캐싱 대상(안정적 접두사)이므로 불변이어야 한다.**
- 시스템 프롬프트의 `cache_control: {"type": "ephemeral"}`을 제거하지 않는다.

---

## 7. LLM 모델 및 파라미터

**결정**: `claude-opus-4-7`, `thinking: {"type": "adaptive"}`, `output_config: {"effort": "high"}`를 사용한다.

**구현 위치**: `llm/client.py`, `llm/async_client.py` 상단 상수

**침범 금지 사항**
- 모델을 비용 절감 목적으로 변경하지 않는다. 모델 변경은 명시적 요구사항이 있을 때만 한다.
- `thinking`을 `{"type": "enabled", budget_tokens: N}` 형태로 교체하지 않는다. Opus 4.7에서 지원되지 않는다.
- `effort` 값을 `"low"`나 `"medium"`으로 낮추지 않는다. 가추 추론의 품질이 effort에 민감하다.

---

## 8. 출력 스키마: SapienStrategyPackage

**결정**: `interaction_strategy.current_context`에는 반드시 `topic`, `depth_level`, `topic_changed`, `multimodal_feedback_requirement` 네 필드가 포함된다.

**구현 위치**: `models/io.py` → `SapienStrategyPackage.from_analysis()`

**침범 금지 사항**
- `topic_changed` 필드를 제거하거나 다른 위치로 이동하지 않는다. Sapien이 전환 감지에 사용한다.
- `multimodal_feedback_requirement`의 자동 생성 로직(anxiety > 70, confidence < 30)을 삭제하지 않는다.
- `from_analysis()`의 `topic_changed` 파라미터 기본값은 `False`를 유지한다.

---

## 9. LLM 호출 무상태성(Stateless): Slot Map이 대화 메모리

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
```

**침범 금지 사항**
- `HashimotoLLMClient` / `AsyncHashimotoLLMClient`에 `_history` 리스트를 재추가하지 않는다.
- `messages` 파라미터에 이전 턴의 user/assistant 쌍을 포함시키지 않는다. `[{"role": "user", "content": user_message}]` 형태를 유지한다.
- `Slot.to_context_str()`에서 `evidence` 필드 출력을 제거하지 않는다. 이것이 히스토리 없이 크로스-턴 맥락을 전달하는 유일한 수단이다.
- `reset_history()`는 API 호환성을 위해 유지하되 no-op으로 남긴다. PRUNE 후 engine이 이를 호출하는 것은 설계 의도다(Section 2 참조).
- `fulfillment_score` 임계값은 프롬프트(`SYSTEM_PROMPT`)와 코드(`FULFILLMENT_THRESHOLD = 0.7`) 양쪽에서 반드시 동일한 값을 사용한다. 어느 한쪽만 변경하지 않는다.

---

## 변경 시 체크리스트

새 기능을 추가하거나 기존 코드를 수정할 때 아래를 확인한다.

- [ ] `submit_turn()` / `process_turn()`이 여전히 단일 LLM 호출만 수행하는가?
- [ ] 시스템 프롬프트에 동적 값이 삽입되지 않았는가?
- [ ] 주제 전환이 `llm_pruned OR system_prune` 조건으로 처리되는가?
- [ ] `effective_depth_threshold(anxiety)`를 경유하여 임계값을 계산하는가?
- [ ] `topic_changed` 플래그가 `SapienStrategyPackage`에 포함되어 반환되는가?
- [ ] `LowLatencyHashimotoEngine`의 워커가 단일 태스크인가?
- [ ] `from_resume()`이 턴 루프 외부에서만 호출되는가?
- [ ] `messages` 파라미터가 단일 user 메시지만 포함하는가? (히스토리 누적 금지)
- [ ] `Slot.to_context_str()`이 `evidence` 필드를 포함하여 반환하는가?
- [ ] `SYSTEM_PROMPT`의 fulfillment 임계값과 `FULFILLMENT_THRESHOLD` 상수가 일치하는가?
