"""테스트 공용: 데모 로그인(/login) 후 세션 토큰으로 /query를 호출한다."""


def login(client, user_id: str) -> dict:
    """등록 사용자로 로그인해 Authorization 헤더를 돌려준다."""
    r = client.post("/login", json={"user_id": user_id})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}


def post_query(client, body: dict):
    """기존 테스트 형식 {question, user_id, role?, thread_id?, approval?}을 로그인 세션 요청으로 바꿔 보낸다.
    role은 서버가 등록 정보로 정하므로 보내지 않고, 테스트가 적은 role이 등록 역할과 같은지만 확인한다."""
    from src import api
    b = dict(body)
    uid, role = b.pop("user_id"), b.pop("role", None)
    if role is not None:
        assert api.registered_user(uid)["role"] == role, (uid, role)
    return client.post("/query", json=b, headers=login(client, uid))
