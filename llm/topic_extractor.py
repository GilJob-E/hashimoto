import json
import os
import re
import time
from typing import List

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

_MODEL = "gemini-2.5-flash-lite"

_SYSTEM = """
당신은 채용 면접 설계 전문가입니다.
지원자의 자기소개서를 분석하여 면접에서 심층적으로 검증할 주제를 추출합니다.
반드시 JSON 배열만 출력하세요. 코드블록이나 설명 텍스트는 절대 포함하지 마세요.
"""


def extract_topics(resume_text: str, count: int) -> List[str]:
    """자기소개서에서 면접 root_topic을 count개 추출한다."""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    prompt = f"""\
아래 자기소개서를 읽고, 면접에서 심층적으로 검증할 수 있는 핵심 주제를 정확히 {count}개 추출하세요.

조건:
- 자기소개서에 실제로 언급된 경험·기술·프로젝트·역량에 근거해야 합니다.
- 면접관이 후속 질문을 3~5개 이어갈 수 있을 만큼 구체적이어야 합니다.
- 서로 중복되지 않아야 합니다.
- 각 주제는 간결한 명사구 형태로 작성하세요 (예: "React 상태관리 아키텍처 경험").

출력 형식 (순수 JSON 배열):
["주제1", "주제2", ...]

[자기소개서]
{resume_text}
"""

    for attempt in range(6):
        try:
            response = client.models.generate_content(
                model=_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=_SYSTEM),
            )
            break
        except genai_errors.ServerError:
            if attempt == 5:
                raise
            time.sleep(2 ** attempt)

    raw = response.text
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.DOTALL)
    topics: List[str] = json.loads(cleaned)

    if not isinstance(topics, list) or not topics:
        raise ValueError(f"LLM이 유효한 주제 목록을 반환하지 않았습니다: {raw!r}")

    return topics[:count]
