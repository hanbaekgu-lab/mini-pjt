"""LLM·임베딩 설정 (한 곳에서 관리).

.env의 AWS 자격 증명과 AWS_DEFAULT_REGION을 쓴다. 자격 증명 값은 코드에 두지 않는다.
테스트는 set_llm()으로 가짜 모델을 주입해 AWS 없이 실행한다.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"   # 에이전트·judge 공용, temperature=0
EMBED_MODEL = "amazon.titan-embed-text-v2:0"

_llm = None
_embeddings = None


def region() -> str:
    return os.environ["AWS_DEFAULT_REGION"]  # 리전은 .env에서만 정한다


def get_llm():
    global _llm
    if _llm is None:
        from langchain_aws import ChatBedrockConverse
        _llm = ChatBedrockConverse(model=MODEL, region_name=region(), temperature=0, max_tokens=2048)
    return _llm


def set_llm(llm) -> None:
    global _llm
    _llm = llm


def get_embeddings():
    global _embeddings
    if _embeddings is None:
        from langchain_aws import BedrockEmbeddings
        _embeddings = BedrockEmbeddings(model_id=EMBED_MODEL, region_name=region())
    return _embeddings


def text_of(message) -> str:
    """ChatBedrockConverse는 content를 블록 리스트로 주기도 하므로 텍스트만 모은다."""
    content = getattr(message, "content", message)
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content or ""
