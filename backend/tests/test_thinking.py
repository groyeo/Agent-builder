"""思考强度（off / low / medium / high）的测试。

三件事必须锁住：
1. 四档输入（含别名）收敛正确，乱填不会把请求打挂；
2. 不同 provider 族翻译成各自字段，且 off 一定真的关掉；
3. 运行时改档位能落盘、能重建适配器（否则改了等于没改）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.llm import thinking
from app.core.llm.factory import _build_extra_body, effective_thinking, get_llm
from app.engine import llm_config
from app.main import app

client = TestClient(app)


def test_normalize_accepts_aliases_and_falls_back_to_off() -> None:
    assert thinking.normalize("OFF") == "off"
    assert thinking.normalize("none") == "off"
    assert thinking.normalize("minimal") == "low"
    assert thinking.normalize("mid") == "medium"
    assert thinking.normalize("max") == "high"
    # 乱填时宁可省钱，也不要抛错把整条流水线打断
    assert thinking.normalize("ultra") == "off"
    assert thinking.normalize(None) == "off"


def test_modelscope_uses_chat_template_kwargs() -> None:
    off = thinking.build_body("modelscope", "off")
    assert off == {"chat_template_kwargs": {"enable_thinking": False}}

    high = thinking.build_body("modelscope", "high")
    assert high["chat_template_kwargs"]["enable_thinking"] is True
    assert high["chat_template_kwargs"]["thinking_budget"] == thinking.BUDGETS["high"]

    low = thinking.build_body("qwen", "low")
    assert low["chat_template_kwargs"]["thinking_budget"] < high["chat_template_kwargs"]["thinking_budget"]


def test_deepseek_uses_thinking_block() -> None:
    assert thinking.build_body("deepseek", "off") == {"thinking": {"type": "disabled"}}
    medium = thinking.build_body("deepseek", "medium")
    assert medium["thinking"] == {"type": "enabled"}
    assert medium["reasoning_effort"] == "medium"


def test_unknown_provider_still_produces_something_sane() -> None:
    assert thinking.build_body("some-gateway", "high")["reasoning_effort"] == "high"
    assert thinking.build_body("some-gateway", "off")["thinking"] == {"type": "disabled"}


def test_effective_level_prefers_explicit_setting(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_thinking", "")
    monkeypatch.setattr(settings, "llm_disable_thinking", True)
    assert effective_thinking() == "off"

    monkeypatch.setattr(settings, "llm_disable_thinking", False)
    assert effective_thinking() == "medium"

    # 显式档位优先于旧布尔开关
    monkeypatch.setattr(settings, "llm_thinking", "high")
    monkeypatch.setattr(settings, "llm_disable_thinking", True)
    assert effective_thinking() == "high"


def test_extra_body_override_wins(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "modelscope")
    monkeypatch.setattr(settings, "llm_thinking", "off")
    monkeypatch.setattr(settings, "llm_extra_body", {"chat_template_kwargs": {"enable_thinking": True}})
    assert _build_extra_body() == {"chat_template_kwargs": {"enable_thinking": True}}


def test_get_settings_endpoint() -> None:
    resp = client.get("/api/settings/llm")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["thinking"] in thinking.LEVELS
    assert [o["value"] for o in data["options"]] == list(thinking.LEVELS)
    assert "request_body" in data


def test_update_thinking_persists_and_rebuilds(monkeypatch, isolated_data_dir) -> None:
    monkeypatch.setattr(settings, "llm_thinking", "")
    before = get_llm()

    resp = client.put("/api/settings/llm", json={"thinking": "high"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["thinking"] == "high"
    assert data["thinking_label"] == "高"
    # 适配器必须被重建，否则旧 extra_body 还在用旧档位
    assert get_llm() is not before

    saved = (isolated_data_dir / "llm_settings.json").read_text(encoding="utf-8")
    assert '"high"' in saved

    # 重启后 load() 能把档位读回来
    monkeypatch.setattr(settings, "llm_thinking", "")
    llm_config.load()
    assert settings.llm_thinking == "high"


def test_update_rejects_unknown_level(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_thinking", "")
    resp = client.put("/api/settings/llm", json={"thinking": "extreme"})
    assert resp.status_code == 422
    assert "未知的思考强度" in resp.json()["detail"]
