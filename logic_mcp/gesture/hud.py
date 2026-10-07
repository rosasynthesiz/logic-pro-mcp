"""A small always-on-top strip showing what the hand currently owns.

You cannot look at your hands and at Logic at the same time, which is the practical problem with every
gesture system. Two channels solve it: you can hear the fader move, and you can glance at one strip of
text that never moves.

Everything shown here comes from `gestured`, which in turn takes its values from Logic's own OSC echo.
That matters: the HUD reports what Logic DID, not what we asked for. A HUD that showed intent would be
confidently wrong exactly when something had gone wrong.

Built on the PyObjC Cocoa bindings the repo already depends on — no new dependency, and an NSPanel can
float above Logic without ever stealing focus, which an ordinary window cannot.
"""
from __future__ import annotations

import argparse
import sys

from . import protocol as P

STATE_COLOURS = {                       # r, g, b — warm amber for live, quiet grey for everything else
    P.IDLE: (0.55, 0.53, 0.50),
    P.ARMED: (0.85, 0.72, 0.35),
    P.ENGAGED: (0.95, 0.72, 0.25),
    P.FROZEN: (0.45, 0.70, 0.58),
}


def run(port: int = P.PORT_HUD, width: float = 340.0, height: float = 64.0,
        corner: str = "top-right", margin: float = 24.0) -> None:
    try:
        import AppKit
        import Foundation
        import objc
    except ImportError:
        print("hud needs pyobjc (already a project dependency); run inside `uv run`", file=sys.stderr)
        raise SystemExit(2)

    rx = P.UdpReceiver(port, timeout=0.0)
    app = AppKit.NSApplication.sharedApplication()
    # Accessory, not regular: no Dock icon, no menu bar, and crucially it never takes focus from Logic.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    screen = AppKit.NSScreen.mainScreen().frame()
    x = screen.size.width - width - margin if "right" in corner else margin
    y = screen.size.height - height - margin if "top" in corner else margin

    panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        Foundation.NSMakeRect(x, y, width, height),
        AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
        AppKit.NSBackingStoreBuffered, False)
    panel.setLevel_(AppKit.NSStatusWindowLevel)
    panel.setOpaque_(False)
    panel.setHasShadow_(True)
    panel.setIgnoresMouseEvents_(True)          # it is a readout, never a control
    panel.setBackgroundColor_(AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
        0.07, 0.065, 0.06, 0.92))
    panel.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
        AppKit.NSWindowCollectionBehaviorStationary |
        AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)

    def label(frame, size, weight, colour):
        f = AppKit.NSTextField.alloc().initWithFrame_(frame)
        f.setBezeled_(False); f.setDrawsBackground_(False); f.setEditable_(False); f.setSelectable_(False)
        f.setFont_(AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight))
        f.setTextColor_(colour)
        return f

    grey = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.62, 0.60, 0.57, 1.0)
    white = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.94, 0.92, 0.88, 1.0)

    l_state = label(Foundation.NSMakeRect(14, height - 26, 120, 16), 11,
                    AppKit.NSFontWeightMedium, grey)
    l_target = label(Foundation.NSMakeRect(14, 12, width - 130, 22), 16,
                     AppKit.NSFontWeightRegular, white)
    l_value = label(Foundation.NSMakeRect(width - 140, 10, 126, 26), 20,
                    AppKit.NSFontWeightMedium, white)
    l_value.setAlignment_(AppKit.NSTextAlignmentRight)
    l_pose = label(Foundation.NSMakeRect(width - 140, height - 26, 126, 16), 11,
                   AppKit.NSFontWeightRegular, grey)
    l_pose.setAlignment_(AppKit.NSTextAlignmentRight)

    dot = AppKit.NSView.alloc().initWithFrame_(Foundation.NSMakeRect(width - 0, 0, 3, height))
    dot.setWantsLayer_(True)

    cv = panel.contentView()
    for v in (l_state, l_target, l_value, l_pose, dot):
        cv.addSubview_(v)
    panel.orderFrontRegardless()

    last = {"t": 0.0}

    class Tick(AppKit.NSObject):
        def fire_(self, _timer):
            objs = rx.drain(latest_only=True)
            if not objs:
                # Nothing from gestured for two seconds means the daemon is gone; say so rather than
                # leaving a stale value on screen that someone might trust.
                if last["t"] and (P.now() - last["t"]) > 2.0:
                    l_state.setStringValue_("no signal")
                    l_target.setStringValue_("gestured is not running")
                    l_value.setStringValue_("")
                    l_pose.setStringValue_("")
                return
            o = objs[-1]
            last["t"] = P.now()
            st = str(o.get("state", P.IDLE))
            r, g, b = STATE_COLOURS.get(st, STATE_COLOURS[P.IDLE])
            l_state.setStringValue_(st.upper())
            l_state.setTextColor_(AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0))
            tgt = o.get("target") or ("—" if st == P.IDLE else "")
            axis = o.get("axis") or ""
            l_target.setStringValue_(f"{tgt}  {axis}".strip())
            l_value.setStringValue_(str(o.get("readout") or ""))
            pose = str(o.get("pose") or P.NO_POSE)
            hands = int(o.get("hands") or 0)
            l_pose.setStringValue_(pose if hands else "no hand")
            dot.layer().setBackgroundColor_(
                AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0).CGColor())

    t = Tick.alloc().init()
    Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1 / 30.0, t, objc.selector(t.fire_, signature=b"v@:@"), None, True)
    print(f"[hud] listening on udp/{port}", file=sys.stderr)
    app.run()


def main() -> None:
    ap = argparse.ArgumentParser(description="gestured HUD — an always-on-top readout")
    ap.add_argument("--port", type=int, default=P.PORT_HUD)
    ap.add_argument("--corner", default="top-right",
                    choices=["top-right", "top-left", "bottom-right", "bottom-left"])
    ap.add_argument("--width", type=float, default=340.0)
    args = ap.parse_args()
    run(port=args.port, width=args.width, corner=args.corner)


if __name__ == "__main__":
    main()
