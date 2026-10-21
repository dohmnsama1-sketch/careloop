"""MCP Apps (ui://) cards for screen devices such as Echo Show. One template, six views."""
from importlib import resources

VIEWS = ("briefing", "doses", "adherence", "order", "alerts", "report")
_BASE = resources.files("careloop").joinpath("ui/base.html").read_text(encoding="utf-8")


def card_html(view: str) -> str:
    if view not in VIEWS:
        raise ValueError(f"unknown view {view}")
    return _BASE.replace("__VIEW__", view)


def uri(view: str) -> str:
    return f"ui://careloop/{view}.html"
