"""Accessibility channel: read Logic's UI tree, press buttons, set values. Needs Accessibility trust."""
from __future__ import annotations

from typing import Any, Callable, Iterator

import ApplicationServices as AS
import Foundation

A_CHILDREN, A_ROLE, A_SUBROLE, A_TITLE, A_DESC, A_VALUE, A_ID, A_WINDOWS, A_MENUBAR = (
    "AXChildren", "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXIdentifier",
    "AXWindows", "AXMenuBar",
)


def trusted(prompt: bool = False) -> bool:
    if prompt:
        opts = Foundation.NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
        return bool(AS.AXIsProcessTrustedWithOptions(opts))
    return bool(AS.AXIsProcessTrusted())


def app(pid: int):
    return AS.AXUIElementCreateApplication(pid)


def attr(el, name: str) -> Any:
    err, val = AS.AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def attr_names(el) -> list[str]:
    err, names = AS.AXUIElementCopyAttributeNames(el, None)
    return list(names) if err == 0 and names else []


def actions(el) -> list[str]:
    err, names = AS.AXUIElementCopyActionNames(el, None)
    return list(names) if err == 0 and names else []


def children(el) -> list:
    return list(attr(el, A_CHILDREN) or [])


def role(el) -> str | None: return attr(el, A_ROLE)
def title(el) -> str | None: return attr(el, A_TITLE)
def desc(el) -> str | None: return attr(el, A_DESC)
def value(el) -> Any: return attr(el, A_VALUE)
def ident(el) -> str | None: return attr(el, A_ID)


def _s(v: Any) -> str | None:
    if v is None:
        return None
    try:
        return str(v)
    except Exception:
        return repr(v)


def info(el) -> dict:
    return {
        "role": role(el), "subrole": attr(el, A_SUBROLE), "title": _s(title(el)),
        "desc": _s(desc(el)), "id": _s(ident(el)), "value": _s(value(el)),
    }


def walk(el, max_depth: int = 30, _depth: int = 0) -> Iterator[tuple[int, Any]]:
    yield _depth, el
    if _depth >= max_depth:
        return
    for c in children(el):
        yield from walk(c, max_depth, _depth + 1)


def find_all(root, pred: Callable[[Any], bool], max_depth: int = 30, limit: int = 200) -> list:
    out = []
    for _, el in walk(root, max_depth):
        if pred(el):
            out.append(el)
            if len(out) >= limit:
                break
    return out


def find(root, role_: str | None = None, title_: str | None = None, desc_: str | None = None,
         id_: str | None = None, contains: str | None = None, max_depth: int = 30):
    def pred(el) -> bool:
        if role_ and role(el) != role_:
            return False
        if title_ is not None and _s(title(el)) != title_:
            return False
        if desc_ is not None and _s(desc(el)) != desc_:
            return False
        if id_ is not None and _s(ident(el)) != id_:
            return False
        if contains:
            hay = " ".join(x for x in (_s(title(el)), _s(desc(el)), _s(ident(el))) if x)
            if contains.lower() not in hay.lower():
                return False
        return True
    hits = find_all(root, pred, max_depth, limit=1)
    return hits[0] if hits else None


def press(el) -> bool:
    return AS.AXUIElementPerformAction(el, "AXPress") == 0


def perform(el, action: str) -> bool:
    return AS.AXUIElementPerformAction(el, action) == 0


def set_value(el, v: Any) -> bool:
    return AS.AXUIElementSetAttributeValue(el, A_VALUE, v) == 0


def windows(app_el) -> list:
    """Only real AXWindow elements. (When Logic has no project open, AXWindows can hand back the app element itself.)"""
    return [w for w in (attr(app_el, A_WINDOWS) or []) if role(w) == "AXWindow"]


def dump(root, max_depth: int = 6, max_nodes: int = 400, roles: set[str] | None = None) -> list[dict]:
    """Flat, depth-annotated snapshot for exploration. Skip empty structural nodes."""
    out = []
    for d, el in walk(root, max_depth):
        i = info(el)
        if roles and i["role"] not in roles:
            continue
        if not any((i["title"], i["desc"], i["id"], i["value"])) and i["role"] in {"AXGroup", "AXSplitGroup", "AXScrollArea", "AXLayoutArea", None}:
            continue
        out.append({"d": d, **{k: v for k, v in i.items() if v}})
        if len(out) >= max_nodes:
            out.append({"truncated": True})
            break
    return out


def press_menu(app_el, *path: str) -> bool:
    """Navigate the menu bar by titles, e.g. press_menu(app, 'Track', 'New Software Instrument Track')."""
    bar = attr(app_el, A_MENUBAR)
    if bar is None:
        return False
    node = bar
    for i, name in enumerate(path):
        nxt = None
        for c in children(node):
            if _s(title(c)) == name:
                nxt = c
                break
        if nxt is None:
            return False
        kids = children(nxt)
        node = kids[0] if kids and i < len(path) - 1 else nxt
    return press(node)
