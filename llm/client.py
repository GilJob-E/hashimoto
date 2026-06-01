import os
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from dotenv import load_dotenv

from llm.prompts import build_system_prompt, build_turn_message
from models.io import LLMAnalysisOutput

load_dotenv()

_MODEL = "gemini-3.1-flash-lite"


class HashimotoLLMClient:
    """Gemini API 동기 래퍼. Hashimoto 엔진 턴 분석 전용.

    Stateless per-turn: 대화 히스토리 없이 매 턴 단일 메시지만 전송.
    시스템 프롬프트는 상수이므로 __init__ 시점에 config에 고정한다.
    """

    def __init__(self, model: str | None = None) -> None:
        self._model = model or _MODEL
        self._client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self._config = types.GenerateContentConfig(
            system_instruction=build_system_prompt(),
            response_mime_type="application/json",
            response_schema=LLMAnalysisOutput,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )

    def analyze_turn(
        self,
        stt_text: str,
        active_slots_context: str,
        depth_counter: int,
        anxiety: float,
        confidence: float,
        resolved_context: str = "",
    ) -> LLMAnalysisOutput:
        user_message = build_turn_message(
            stt_text=stt_text,
            active_slots_context=active_slots_context,
            depth_counter=depth_counter,
            anxiety=anxiety,
            confidence=confidence,
            resolved_context=resolved_context,
        )

        for attempt in range(6):
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=user_message,
                    config=self._config,
                )
                break
            except genai_errors.ServerError:
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt)

        return LLMAnalysisOutput.model_validate_json(response.text)

    def reset_history(self) -> None:
        pass
