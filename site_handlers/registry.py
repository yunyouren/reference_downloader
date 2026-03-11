from __future__ import annotations

from typing import Callable, Literal


HandlerResult = Literal["downloaded", "retry", "continue", "unhandled"]
HtmlHandler = Callable[..., HandlerResult]

HTML_HANDLERS: dict[str, HtmlHandler] = {}


def register(hosts: list[str]) -> Callable[[HtmlHandler], HtmlHandler]:
    def decorator(func: HtmlHandler) -> HtmlHandler:
        for h in hosts:
            HTML_HANDLERS[h.lower()] = func
        return func

    return decorator


def dispatch(host: str) -> HtmlHandler | None:
    return HTML_HANDLERS.get((host or "").lower())
