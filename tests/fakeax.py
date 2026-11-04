"""A fake of the Accessibility channel for tests that must never touch a real UI.

Elements are plain objects with role/title/desc/value/attrs/children. `FakeAX` mirrors the subset of
logic_mcp.channels.ax that ax_logic, dialogs and the dispatchers call, and behaves like Logic in the two
ways readback tests depend on: pressing a checkbox flips its value, and AXIncrement/AXDecrement on a
Volume slider moves its dB readout by 0.1 dB. Nothing else happens by itself -- a test that wants Logic
to "not respond" simply builds an element whose press does nothing.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from logic_mcp import target
from logic_mcp.channels import ax as real_ax
from logic_mcp.channels import keys as real_keys
from logic_mcp.channels.midi import McuState


class El:
    def __init__(self, role, desc=None, title=None, value=None, children=None, attrs=None, ident=None, flips=None):
        self.role, self.desc, self.title, self.value = role, desc, title, value
        self.children = list(children or [])
        self.attrs = dict(attrs or {})
        self.ident = ident
        self.pressed = 0
        self.flips = (role == "AXCheckBox") if flips is None else flips
        self.inert = False                 # True: Logic ignores presses and value writes on this control

    def __repr__(self):
        return f"El({self.role}, desc={self.desc!r}, title={self.title!r}, value={self.value!r})"


class FakeAX:
    A_CHILDREN, A_ROLE, A_SUBROLE, A_TITLE, A_DESC, A_VALUE, A_ID, A_WINDOWS, A_MENUBAR = (
        real_ax.A_CHILDREN, real_ax.A_ROLE, real_ax.A_SUBROLE, real_ax.A_TITLE, real_ax.A_DESC, real_ax.A_VALUE,
        real_ax.A_ID, real_ax.A_WINDOWS, real_ax.A_MENUBAR)

    def __init__(self, root, trusted: bool = True):
        self.root, self._trusted, self.log = root, trusted, []

    def trusted(self, prompt: bool = False): return self._trusted
    def app(self, pid): return self.root

    def attr(self, el, name):
        if el is None:
            return None
        table = {"AXChildren": list(el.children), "AXRole": el.role, "AXTitle": el.title, "AXDescription": el.desc,
                 "AXValue": el.value, "AXIdentifier": el.ident,
                 "AXWindows": [c for c in el.children if c.role == "AXWindow"]}
        return table[name] if name in table else el.attrs.get(name)

    def attr_names(self, el): return []
    def actions(self, el): return []
    def children(self, el): return list(el.children) if el is not None else []
    def role(self, el): return el.role if el is not None else None
    def title(self, el): return el.title if el is not None else None
    def desc(self, el): return el.desc if el is not None else None
    def value(self, el): return el.value if el is not None else None
    def ident(self, el): return el.ident if el is not None else None

    def info(self, el):
        return {"role": el.role, "subrole": el.attrs.get("AXSubrole"), "title": el.title, "desc": el.desc,
                "id": el.ident, "value": None if el.value is None else str(el.value)}

    def walk(self, el, max_depth=30, _depth=0):
        yield _depth, el
        if _depth >= max_depth:
            return
        for c in el.children:
            yield from self.walk(c, max_depth, _depth + 1)

    def find_all(self, root, pred, max_depth=30, limit=200):
        out = []
        for _, el in self.walk(root, max_depth):
            if pred(el):
                out.append(el)
                if len(out) >= limit:
                    break
        return out

    def find(self, root, role_=None, title_=None, desc_=None, id_=None, contains=None, max_depth=30):
        if root is None:
            return None

        def pred(el):
            if role_ and el.role != role_:
                return False
            if title_ is not None and el.title != title_:
                return False
            if desc_ is not None and el.desc != desc_:
                return False
            if id_ is not None and el.ident != id_:
                return False
            if contains:
                hay = " ".join(x for x in (el.title, el.desc, el.ident) if x)
                if contains.lower() not in hay.lower():
                    return False
            return True
        hits = self.find_all(root, pred, max_depth, limit=1)
        return hits[0] if hits else None

    def press(self, el):
        el.pressed += 1
        self.log.append(("press", el))
        if el.flips and not el.inert:
            el.value = 0 if el.value else 1
        return True

    def perform(self, el, action):
        self.log.append((action, el))
        if el.desc == "Volume" and action in ("AXIncrement", "AXDecrement") and not el.inert:
            from logic_mcp.channels.ax_logic import parse_db
            cur = parse_db(el.attrs.get("AXValueDescription"))
            if cur == float("-inf"):
                cur = -60.0 if action == "AXIncrement" else cur     # first step up from the floor
            elif cur is not None:
                cur += 0.1 if action == "AXIncrement" else -0.1
            el.attrs["AXValueDescription"] = "-\u221e dB" if cur == float("-inf") else f"{cur:+.1f} dB"
        return True

    def set_value(self, el, v):
        self.log.append(("set", el, v))
        if not el.inert:
            el.value = v
        return True

    def windows(self, app_el):
        return [w for w in app_el.children if w.role == "AXWindow"]

    def dump(self, *a, **k): return []
    def press_menu(self, *a): return False


# ---------------------------------------------------------------- Logic-shaped trees
def track_item(index: int, name: str, mute=0, solo=0, arm=0, monitor=0, volume_text="+0.0 dB", selected=0) -> El:
    return El("AXLayoutItem", desc=f"Track {index} “{name}”", children=[
        El("AXRadioButton", desc="Has Focus", value=selected),
        El("AXCheckBox", desc="Mute", value=mute),
        El("AXCheckBox", desc="Solo", value=solo),
        El("AXCheckBox", desc="Record Enable", value=arm),
        El("AXCheckBox", desc="Input Monitoring", value=monitor),
        El("AXSlider", desc="Volume", value=173, attrs={"AXValueDescription": volume_text}),
    ])


def make_project(names, doc="file:///tmp/Fixture%20Song.logicx", title="Fixture Song - Tracks", volumes=None):
    """(root, win): an application element with one '<title> - Tracks' window holding a control bar and
    one track header per name. `names` may repeat -- that is the point of several tests."""
    items = [track_item(i + 1, n, volume_text=(volumes or {}).get(n, "+0.0 dB")) for i, n in enumerate(names)]
    header = El("AXGroup", desc="Tracks header", children=items)
    bar = El("AXGroup", desc="Control Bar", children=[
        El("AXCheckBox", title="Play", value=0), El("AXCheckBox", title="Record", value=0),
        El("AXCheckBox", title="Cycle", value=0), El("AXCheckBox", title="Count In", value=0),
        El("AXSlider", desc="Tempo", value=120.0, flips=False),
    ])
    win = El("AXWindow", title=title, attrs={"AXDocument": doc, "AXSubrole": "AXStandardWindow"},
             children=[bar, El("AXGroup", desc="Tracks", children=[header])])
    root = El("AXApplication", children=[win])
    return root, win


def logic_app(pid: int | None = 4242) -> target.LogicApp:
    return target.LogicApp("creator_studio", "com.apple.mobilelogic", "Logic Pro Creator Studio",
                           "/Applications/Logic Pro Creator Studio.app", "12.3", pid)


def wire(monkeypatch, names=("Vocal", "Guitar"), **project_kw) -> SimpleNamespace:
    """Point every module that talks to Logic at the fake tree and a Mock MIDI bus.

    After this: no AX call reaches macOS, no MIDI port opens, no osascript runs, no sleep waits. The
    real `_win`, `_mcu_live`, `transport_state`, dispatchers and resolver all run against the fake."""
    from logic_mcp import doctor, local, server
    from logic_mcp.channels import ax_logic, dialogs

    root, win = make_project(list(names), **project_kw)
    fax = FakeAX(root)
    local._recent.clear()                      # the thrash guard's memory is per process; tests want a fresh one
    app = logic_app()
    for mod in (ax_logic, dialogs, server, local, doctor):
        monkeypatch.setattr(mod, "ax", fax)
    tgt = Mock()
    tgt.find.return_value = app
    for mod in (server, local, doctor):
        monkeypatch.setattr(mod, "target", tgt)
    monkeypatch.setattr(server, "_pid", lambda: app.pid)
    monkeypatch.setattr(server, "_app", lambda: app)
    midi = Mock()
    midi.mcu = McuState()
    midi.strip_for.return_value = None
    midi.list_ports.return_value = {"outputs": [], "inputs": []}
    for mod in (server, doctor):
        monkeypatch.setattr(mod, "bus", lambda: midi)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(doctor, "session_state", lambda: {"locked": False, "display_asleep": False, "ok": True})
    monkeypatch.setattr(doctor, "applescript", Mock(frontmost=Mock(return_value="Finder")))
    monkeypatch.setattr(server, "applescript", Mock())
    keys = Mock(); keys.LOGIC_KEYS = dict(real_keys.LOGIC_KEYS); keys.press.return_value = True
    monkeypatch.setattr(server, "keys", keys)
    return SimpleNamespace(root=root, win=win, ax=fax, midi=midi, app=app, target=tgt)
