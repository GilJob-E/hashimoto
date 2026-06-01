1. 개요 (Overview)
본 시스템은 지능형 면접 시스템을 위해 실제 질문을 생성하고 사용자와 물리적인 상호작용을 수행하는 Sapien 엔진(Interaction)이 면접관으로서의 대화전략을 갖출 수 있도록 사용자의 답변을 분석하여 논리적 공백을 추적한 후 sapien에게 전략프롬프트를 넘겨주는 Hashimoto 엔진(Logic)임.
2. 핵심 용어 정의 (Glossary)
Slot (슬롯): 대화 맥락에서 아직 검증되지 않았거나 정보가 불충분하여 추가적으로 확인이 필요한 명제 및 질문. (예: "사용자가 원리는 설명했으나 실제 코드로 구현한 방식이 모호함")
Logic Stack (LIFO): 면접 시작 시 설정된 주제를 바탕으로, 응답에 따라 실시간 생성된 Slot을 Push하는 구조. 최신 Slot을 상단에 두어 대화 맥락을 유지하며, 대화의 비효율을 방지하기 위해 Hashimoto 엔진은 최신 $n$개의 Slot을 동시에 고려함.
Abduction (가추법): 결과(사용자의 답변)를 보고 원인(누락된 논리나 배경)을 역으로 추론하여 새로운 슬롯(명제)을 생성하는 방식.
Pruning (가지치기): 특정 깊이(Depth) 도달 또는 주제 전환 없이 문답이 일정 횟수 이상 이어질 경우, 대화의 피로도를 줄이기 위해 일정 단계의 노드를 제거하고 새로운 주제로 전환하는 기법.


3. 시스템 아키텍처 및 데이터 흐름
논리적 판단을 담당하는 Hashimoto와 실제 인터랙션을 담당하는 Sapien으로 이원화됨.
Hashimoto Engine: 대화 맥락 관리 및 질문의 전략/방향성/슬롯 도출.
Sapien Engine: Hashimoto가 제공한 질문 방향성을 프롬프트로 입력받아, 실제 사용자에게 던질 질문을 생성하고 물리적인 상호작용(음성, 동작 등)을 수행함.
3.1 Input
STT Context: 사용자의 발화 텍스트 전사본.
Multimodal Vector: 수치화된 지표 (Anxiety, Confidence 등, $0 \sim 100$).
3.2 Hashimoto Engine (Internal Logic & State)
엔진은 내부적으로 다음 상태 변수를 관리하며 전체 대화를 제어할 수 있음.

변수명
타입
설명
Global_Slot_Map
Dict
생성된 슬롯들의 ID와 각 슬롯의 충족 여부(상태)를 기록하는 맵.
Priority_Stack
List
현재 탐색 및 검증 중인 슬롯들의 우선순위 스택 (LIFO).
Depth_Counter
Int
현재 주제(Root Topic) 내에서 파생되어 이어진 연속 질문의 횟수.

Topic_Question_Counter
Int
현재 주제(Root Topic)에 대한 누적 질문 수. 임계값 도달 시 주제 전환을 강제함. (구현에서 추가된 상태 변수)



4. 핵심 프로세스 (Processing Loop)
Multi-Slot Scanning: 사용자의 답변이 입력되면, Priority_Stack 내의 모든 활성화된 슬롯에 대해 현재 응답이 정보를 충족했는지 판별하여 Global_Slot_Map을 업데이트함.
Stack Compaction:Global_Slot_Map에서 충족(Verified)된 것으로 판단된 슬롯들을 스택에서 즉시 제거(Pop)하여 중복 질문을 방지함.
Abductive Branching: 스택 최상단 슬롯이 미충족 상태일 경우, 가추법을 통해 해당 공백을 메우기 위한 새로운 슬롯(검증 명제)을 생성하여 스택에 Push함.
State Transition:Depth_Counter가 설정된 임계치에 도달하거나 스택이 비었을 경우, Pruning을 통해 상위 노드를 정리하고 주제를 강제 전환함.
Strategy Delivery: 결정된 질문 방향성과 분석된 슬롯 상태를 Sapien 엔진에 전달하여 실제 상호작용을 유도함.


5. Hashimoto 엔진의 내부 판단 메커니즘 (Internal Reasoning)
Hashimoto 엔진은 사용자의 응답으로부터 다음을 내부적으로 수행해야 함.
슬롯 충족 판단: 현재 답변이 스택에 쌓인 명제들을 해소했는가? (Evidence Extraction)
신규 슬롯 생성: 미충족된 정보를 확인하기 위해 어떤 명제를 새로 세워야 하는가? (Proposition Generation)
전략 수립: 사피엔이 질문할 때 참고할 수 있도록 '왜 이 질문이 필요한지'에 대한 논리적 근거(Reasoning)와 목표(Goal)를 생성.
6.내부 데이터 예시(단순 예시이며 구현과정에서 논리적 허점이나 보완해야하는 부분이 있다면 변형된 형태를 이용할 수 있음)

> ⚠️ 아래 JSON은 최초 기획 단계의 개념 예시입니다. 실제 구현 스키마(`LLMAnalysisOutput`, `SapienStrategyPackage`)는 일부 필드명·구조가 다릅니다(예: `response_type`, `slots_to_pop`, `topic_changed`, `transition_hint`, `resolved_history` 등). **권위 있는 현재 스키마는 `README.md`의 "LLM 출력 스키마" 절과 `models/io.py`를 참조하세요.**
LLM은 이하의 형태로 현재 응답에 대해 새로운 slot명제를 생성한다
{
  "gap_analysis": {
    "detected_void": "사용자가 고난도 알고리즘(HDBSCAN, sBERT)을 나열했으나, 데이터의 특성에 따라 왜 해당 알고리즘을 선택했는지에 대한 '의사결정 논리'가 누락됨",
    "abductive_hypothesis": "라이브러리를 단순히 호출해서 사용했을 가능성이 있음. 실제 데이터 분포에 따른 하이퍼파라미터(min_cluster_size 등) 조정 경험이 있는지 확인하여 실무 역량을 검증해야 함"
  },
  "emergent_slot": {
    "slot_id": "logic_rationale_hdbscan_01",
    "type": "DYNAMIC_PROPOSITION",
    "proposition_statement": "사용자가 데이터의 밀도나 노이즈 특성을 고려하여 HDBSCAN의 파라미터를 직접 튜닝했는가?",
    "verification_criteria": [
      {
        "criterion": "HDBSCAN의 하이퍼파라미터(min_samples, min_cluster_size 등) 언급 여부",
        "weight": 0.6
      },
      {
        "criterion": "K-means 등 타 클러스터링 대비 HDBSCAN을 선택한 구체적 이유(노이즈 처리 등) 언급",
        "weight": 0.4
      }
    ],
    "expected_answer_depth": "Technical_Deep_Dive"
  }
}

LLM은 이하의 형태로 slot충족여부를 판단하고 hashimoto engine에서 상태를 업데이트 한다{
  "logic_analysis": {
    "slot_evaluations": [
      {
        "slot_id": "ux_improvement_intent",
        "fulfillment_score": 0.9,
        "evidence": "사용자가 시스템의 허점을 찾기 위해 UX에 집중했다는 동기를 명확히 밝힘"
      },
      {
        "slot_id": "technical_logic_gap",
        "fulfillment_score": 0.3,
        "evidence": "UX를 통한 개선은 언급했으나, 이를 뒷받침하는 기술적 수정 사항은 모호함"
      }
    ],
    "abduction_results": {
      "observed_gap": "사용자가 UX적 허점을 발견했다고 하나, 이를 '실제 서비스 수준'으로 올리기 위해 수행한 구체적인 엔지니어링 조치가 누락됨",
      "new_proposition_slot": {
        "id": "eng_solution_01",
        "statement": "사용자가 발견한 UX 허점을 해결하기 위해 도입한 구체적인 기술적 로직이나 예외 처리 방식이 무엇인가?",
        "priority": "HIGH"
      }
    }
  },
  "state_commands": {
    "stack_action": "PUSH", 
    "pop_count": 1,
    "depth_increment": 1,
    "target_slots_to_pop": ["ux_improvement_intent"]
  },
  "strategy_package": {
    "interviewer_intent": "사용자의 'UX 집중' 발언이 기술적 실무 능력 부족을 감추기 위한 것인지, 아니면 실제 엔지니어링 역량과 결합된 것인지 검증",
    "focus_point": "발견한 허점에 대한 구체적인 '기술적 해결책(How)'",
    "suggested_tone": "Analytical_Pressure"
  }
}

hashimoto는 sapien에 이하의 형태로 전략프롬프트를 주입할수 있음
{
  "interaction_strategy": {
    "logic_goal": "사용자가 제안한 UX 개선안의 기술적 실체 파악",
    "logical_gap_to_bridge": "UX 허점을 찾았다고 했으나, 그것을 해결한 '엔지니어링 로직'이 설명되지 않음",
    "interviewer_persona_guidance": {
      "intent": "논리적 허점을 날카롭게 지적하며 구체적인 구현 방식을 캐물을 것",
      "emotion_direction": "Slightly_Skeptical_but_Professional",
      "key_keywords": ["예외 처리", "구현 로직", "기술적 대응"]
    },
    "current_context": {
      "topic": "Capstone Project - AI Interview Training System",
      "depth_level": 3,
      "multimodal_feedback_requirement": "사용자가 당황할 경우 질문 수위를 조절할 것"
    }
  }
}