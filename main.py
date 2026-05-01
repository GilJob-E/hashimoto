"""Interactive demo: 자기소개서 기반 Hashimoto 면접 루프."""

import json

from engine.hashimoto import HashimotoEngine
from models.io import EngineInput, MultimodalVector


def read_resume() -> str:
    """여러 줄 자기소개서 입력. 빈 줄 두 번 연속 입력 시 종료."""
    print("자기소개서를 입력하세요 (입력 완료 시 빈 줄을 두 번 입력):")
    lines = []
    blank_count = 0
    while True:
        line = input()
        if line == "":
            blank_count += 1
            if blank_count >= 2:
                break
            lines.append(line)
        else:
            blank_count = 0
            lines.append(line)
    return "\n".join(lines).strip()


def main() -> None:
    # ── 초기화 ────────────────────────────────────────────────────────────────
    resume_text = read_resume()
    if not resume_text:
        print("자기소개서가 비어있습니다. 종료합니다.")
        return

    try:
        topic_count = int(input("\n추출할 면접 주제 개수 (기본 3): ") or "3")
    except ValueError:
        topic_count = 3

    print("\n[주제 추출 중...]\n")
    engine = HashimotoEngine.from_resume(resume_text, topic_count=topic_count)

    all_topics = [engine.current_topic] + engine.remaining_topics
    print("=== 추출된 면접 주제 ===")
    for i, t in enumerate(all_topics, 1):
        marker = "▶" if i == 1 else " "
        print(f"  {marker} {i}. {t}")
    print()

    # ── 면접 루프 ─────────────────────────────────────────────────────────────
    turn = 1
    while True:
        print(f"--- Turn {turn} | 현재 주제: {engine.current_topic} ---")
        stt_text = input("지원자 발화 (STT) ['exit'으로 종료]: ").strip()
        if stt_text.lower() in ("exit", "quit", "종료"):
            print("세션 종료.")
            break

        try:
            anxiety = float(input("불안도 (0-100, 기본 50): ") or "50")
            confidence = float(input("자신감 (0-100, 기본 50): ") or "50")
        except ValueError:
            anxiety, confidence = 50.0, 50.0

        engine_input = EngineInput(
            stt_text=stt_text,
            multimodal=MultimodalVector(anxiety=anxiety, confidence=confidence),
            turn_number=turn,
        )

        print("\n[분석 중...]\n")
        pkg = engine.process_turn(engine_input)

        if pkg.interaction_strategy.get("current_context", {}).get("topic_changed"):
            print(f"*** 주제 전환 → {engine.current_topic} ***")
            remaining = engine.remaining_topics
            if remaining:
                print(f"    (남은 주제: {', '.join(remaining)})")
            else:
                print("    (마지막 주제입니다)")
            print()

        print("=== Sapien Strategy Package ===")
        print(json.dumps(pkg.interaction_strategy, ensure_ascii=False, indent=2))
        print()
        print(engine.state.summary())
        print()

        turn += 1


if __name__ == "__main__":
    main()
