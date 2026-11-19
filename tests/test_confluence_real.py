"""Markdown → Confluence 저장 형식 변환기와 실제 Confluence 어댑터(HTTP는 가짜로 대체)."""
import base64
import json

import pytest

from src import confluence, meeting
from src.confluence_convert import md_to_storage

MD = """> 자동 작성 문서

## 진척 현황
| 업무 | 진척률 | 진행 |
|---|---:|---|
| SCR-C02-01 결제 <승인> | **40.00%** | `████░░░░░░` |

## 결정사항
- 목 서버로 개발

## 액션아이템
- [ ] 인증 키 재요청
- [x] 배포 스크립트 정리"""


def test_converter_makes_confluence_storage():
    s = md_to_storage(MD)
    assert '<ac:structured-macro ac:name="info">' in s
    assert "<h2>진척 현황</h2>" in s
    assert "<table><tbody><tr><th>업무</th><th style=\"text-align: right;\">진척률</th><th>진행</th></tr>" in s
    assert "<td>SCR-C02-01 결제 &lt;승인&gt;</td>" in s  # 특수문자 이스케이프
    assert "<strong>40.00%</strong>" in s and "<code>████░░░░░░</code>" in s
    assert "<ul><li>목 서버로 개발</li></ul>" in s
    assert s.count("<ac:task>") == 2 and "<ac:task-status>complete</ac:task-status>" in s


def test_meeting_material_converts_tables():
    s = md_to_storage(meeting.build_meeting_material())
    assert s.count("<table>") >= 3 and "<ac:task-list>" in s and "|---" not in s


def test_converter_colors_week_over_week_markers():
    s = md_to_storage("| 항목 | 증감 |\n|---|---|\n| 진척 | ▲ +1.86%p |\n| 차이 | ▼ -0.36%p |\n| 화면 | ▲ +3 |\n| 기타 | - |")
    assert '<td><span style="color: rgb(0,135,90);">▲ +1.86%p</span></td>' in s  # 증가 초록
    assert '<td><span style="color: rgb(222,53,11);">▼ -0.36%p</span></td>' in s  # 감소 빨강
    assert '<span style="color: rgb(0,135,90);">▲ +3</span>' in s and "<td>-</td>" in s
    assert "<span" not in md_to_storage("- 일정 경과율 -7.63%p, 증감 없음")  # 마커 없는 부호는 그대로


def test_meeting_material_storage_has_colored_deltas():
    s = md_to_storage(meeting.build_meeting_material())
    assert s.count('<span style="color: rgb(0,135,90);">▲ +') >= 10
    assert '<span style="color: rgb(222,53,11);">▼ -0.36%p</span>' in s  # 계획 대비 차이 악화


def test_fake_mode_writes_storage_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_DIR", str(tmp_path))
    page = confluence.create_page("주간회의 자료", MD, page_id="weekly_meeting_test")
    assert (tmp_path / "weekly_meeting_test.md").exists()
    assert "<table>" in (tmp_path / "weekly_meeting_test.storage.xml").read_text(encoding="utf-8")
    assert page["page_id"] == "weekly_meeting_test"


@pytest.fixture
def real_env(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_MODE", "real")
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://mock.example.com/wiki")
    monkeypatch.setenv("CONFLUENCE_SPACE_KEY", "MOCK")
    monkeypatch.setenv("CONFLUENCE_USER", "mock-user@example.com")
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "mock-token")
    monkeypatch.setenv("CONFLUENCE_PARENT_PAGE_ID", "100")


class FakeHTTP:
    """urllib.request.urlopen 대체: 요청을 기록하고 정해진 응답을 돌려준다."""

    def __init__(self, existing=None):
        self.requests, self.existing = [], existing

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.requests.append((req.get_method(), req.full_url, dict(req.header_items()), body))
        if req.get_method() == "GET":
            data = {"results": [self.existing] if self.existing else []}
        else:
            data = {"id": "555", "_links": {"webui": "/spaces/MOCK/pages/555"}}
        return _Resp(data)


class _Resp:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_real_create_posts_storage_with_auth(real_env, monkeypatch):
    http = FakeHTTP()
    monkeypatch.setattr("urllib.request.urlopen", http)
    page = confluence.create_page("주간회의 자료 2026-11-16", MD)
    method, url, headers, body = http.requests[-1]
    assert method == "POST" and url == "https://mock.example.com/wiki/rest/api/content"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"mock-user@example.com:mock-token").decode()
    assert body["space"]["key"] == "MOCK" and body["ancestors"] == [{"id": "100"}]
    assert body["body"]["storage"]["representation"] == "storage" and "<table>" in body["body"]["storage"]["value"]
    assert page["page_id"] == "555" and page["url"].endswith("/spaces/MOCK/pages/555")


def test_real_create_updates_existing_page_version(real_env, monkeypatch):
    http = FakeHTTP(existing={"id": "555", "version": {"number": 3}})
    monkeypatch.setattr("urllib.request.urlopen", http)
    confluence.create_page("주간회의 자료 2026-11-16", MD)
    method, url, _, body = http.requests[-1]
    assert method == "PUT" and url.endswith("/rest/api/content/555") and body["version"]["number"] == 4


def test_real_mode_missing_settings_names_only(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_MODE", "real")
    for k in confluence.REAL_SETTINGS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "secret-value")
    with pytest.raises(RuntimeError) as e:
        confluence.create_page("t", "b")
    assert "CONFLUENCE_BASE_URL" in str(e.value) and "secret-value" not in str(e.value)


def test_converter_renders_rule_before_publish_footer():
    s = md_to_storage(meeting.with_footer("## 액션아이템\n- [ ] 일"))
    assert "<hr />" in s and "<p>---</p>" not in s
    assert s.endswith(f"<p>{meeting.PUBLISH_FOOTER}</p>")
