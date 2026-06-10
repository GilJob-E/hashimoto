"""
채용공고 → 세션 집중 키워드 도출.

엔진 인스턴스 시 1회만 호출된다. 도출된 키워드는 엔진 내부 로직에서 직접
사용되지 않으며, SapienStrategyPackage의 부가 메타정보로만 전달된다.

career-ops의 JD 분석 접근(아키타입·요구역량을 LLM 추론으로 도출)을 참고하되,
면접 세션이 집중할 키워드 목록만 추출하는 단순화된 형태다.
"""

import os
import re
import urllib.request
from typing import List

from google import genai
from google.genai import types
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

_MODEL = "gemini-2.5-flash-lite"

_SYSTEM = (
    "당신은 채용공고를 분석하여 면접에서 집중적으로 검증할 핵심 키워드를 추출하는 "
    "전문가입니다. 공고에 실제로 명시·암시된 직무 역량·기술·경험에 근거해야 하며, "
    "근거 없는 키워드를 지어내지 마세요. 반드시 요구된 JSON만 출력합니다."
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# fetch 본문이 이보다 짧으면 유효한 공고로 보지 않는다 (JS 렌더링/차단 가능성).
_MIN_TEXT_LEN = 100


class _FocusKeywords(BaseModel):
    keywords: List[str]


def fetch_job_posting(url: str, timeout: float = 10.0) -> str:
    """공고 URL을 받아 본문 텍스트를 반환한다 (HTML 태그·스크립트 제거).

    주의: Greenhouse·Lever·LinkedIn 등 JS 렌더링/봇차단 공고는 정적 fetch로
    본문을 충분히 얻지 못할 수 있다. 그 경우 텍스트가 비어 ValueError가 난다.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read().decode(charset, errors="ignore")

    # script/style 블록 제거 후 태그 제거
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)  # HTML 엔티티 단순 제거
    text = re.sub(r"\s+", " ", text).strip()
    return text


def derive_focus_keywords(job_text: str, count: int = 8) -> List[str]:
    """공고 본문 텍스트에서 면접 집중 키워드를 최대 count개 추출한다."""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    prompt = f"""\
아래 채용공고를 분석하여, 이 직무 면접에서 집중적으로 검증할 핵심 키워드를
중요도 순으로 최대 {count}개 추출하세요.

조건:
- 공고에 실제로 언급된 직무 역량·기술·도구·경험에 근거할 것.
- 간결한 명사구 형태 (예: "대용량 트래픽 처리", "분산 트랜잭션").
- 서로 중복되지 않게.

[채용공고 본문]
{job_text[:8000]}
"""
    resp = client.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM,
            response_mime_type="application/json",
            response_schema=_FocusKeywords,
        ),
    )
    keywords = _FocusKeywords.model_validate_json(resp.text).keywords
    # 공백·중복 정리
    seen, out = set(), []
    for k in keywords:
        k = k.strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:count]


def extract_focus_keywords_from_url(url: str, count: int = 8) -> List[str]:
    """공고 URL → 본문 fetch → 집중 키워드 도출. 엔진 팩토리에서 1회 호출."""
    text = fetch_job_posting(url)
    if len(text) < _MIN_TEXT_LEN:
        raise ValueError(
            "공고 본문을 충분히 가져오지 못했습니다 (JS 렌더링/봇차단 가능). "
            f"가져온 길이={len(text)}자."
        )
    return derive_focus_keywords(text, count)
