"""운영 실행 진입점: Bedrock LLM 에이전트를 연결한 FastAPI 앱.

실행: .venv\\Scripts\\python.exe -m uvicorn src.server:app --port 8000
"""
from src import agent, api

agent.setup()
app = api.app
