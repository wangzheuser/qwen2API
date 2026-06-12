"""Qwen 上游风控页面识别工具。"""

from __future__ import annotations


def is_waf_challenge(text: str | bytes | None, content_type: str | None = None) -> bool:
    """判断响应是否为 Aliyun/Baxia 风控校验页。"""
    if isinstance(text, bytes):
        body = text.decode("utf-8", errors="ignore")
    else:
        body = text or ""

    lower = body.lower()
    content_type_lower = (content_type or "").lower()

    # Aliyun WAF 明确会返回 aliyun_waf_* meta，Baxia 惩罚页会包含滑动验证/验证码关键词。
    if (
        "aliyun_waf" in lower
        or "_____tmd_____" in lower
        or "fail_sys_user_validate" in lower
        or "x5secdata" in lower
        or "滑动验证" in body
    ):
        return True
    if "captcha" in lower and ("aliyun" in lower or "baxia" in lower):
        return True

    # Qwen API 正常应返回 JSON/SSE；保护页通常是 200 text/html。
    if "text/html" in content_type_lower and ("<!doctype" in lower or "<html" in lower):
        return True

    return False


def format_waf_error(text: str | bytes | None = None, status_code: int | None = None) -> str:
    """生成不会误伤账号状态的 WAF 错误信息。"""
    prefix = "upstream_waf: Qwen 返回 Aliyun/Baxia 风控校验页"
    if status_code is not None:
        prefix = f"{prefix} HTTP {status_code}"

    if isinstance(text, bytes):
        body = text.decode("utf-8", errors="ignore")
    else:
        body = text or ""
    marker = "aliyun_waf" if "aliyun_waf" in body.lower() else ""
    return f"{prefix}{' marker=' + marker if marker else ''}"
