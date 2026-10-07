"""Reading the `handd` camera stream.

`handd` is a separate Swift process (see ../../../handd/) that owns the camera and Vision. It is a
separate process on purpose: Vision runs best from Swift on the neural engine, and a camera crash or a
permission prompt then cannot take the control daemon down with it.

This module is thin by design — the contract is in protocol.py, and everything here does is turn
datagrams into `HandFrame`s and notice when they stop arriving.
"""
from __future__ import annotations

from .. import protocol as P

# How long without a datagram before we declare the hand gone. Two dropped frames at 60 fps is noise;
# a sixth of a second is the camera or the process being in trouble, and the arbiter must freeze.
LOST_AFTER = 0.18


class HandStream:
    def __init__(self, port: int = P.PORT_HANDS, lost_after: float = LOST_AFTER):
        self.rx = P.UdpReceiver(port)
        self.lost_after = lost_after
        self.last_frame_at: float = 0.0
        self.last_hand_at: float = 0.0
        self.frames = 0
        self.empty = 0

    def poll(self, t: float) -> tuple[P.HandFrame | None, bool]:
        """Returns (newest frame or None, hand_is_lost).

        Only the newest datagram is kept. A queued 200 ms-old hand position is worse than no position:
        acting on it moves the fader to where the hand USED to be, which feels like the system fighting
        you.
        """
        objs = self.rx.drain(latest_only=True)
        if objs:
            f = P.HandFrame.from_obj(objs[-1])
            self.frames += 1
            self.last_frame_at = t
            if f.hands:
                self.last_hand_at = t
                return f, False
            self.empty += 1
            return f, (t - self.last_hand_at) > self.lost_after if self.last_hand_at else False
        if self.last_hand_at and (t - self.last_hand_at) > self.lost_after:
            return None, True
        return None, False

    @property
    def alive(self) -> bool:
        return self.last_frame_at > 0.0

    def close(self) -> None:
        self.rx.close()
