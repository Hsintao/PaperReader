"""Provider-specific fields the translation calls add to each request."""

from app.services.llm_client import _extra_body


def test_deepseek_endpoints_disable_thinking():
    expected = {"thinking": {"type": "disabled"}}
    assert _extra_body("https://api.deepseek.com") == expected
    assert _extra_body("https://api.deepseek.com/v1") == expected


def test_other_openai_compatible_endpoints_get_no_extra_fields():
    # Unknown top-level fields are rejected by strict providers, so the
    # DeepSeek-only parameter must not leak into their requests.
    assert _extra_body("https://api.openai.com/v1") == {}
    assert _extra_body("https://open.bigmodel.cn/api/paas/v4") == {}
    assert _extra_body("http://127.0.0.1:8000/v1") == {}
