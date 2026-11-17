"""Game state tracking and the deterministic coaching rules.

Input is CS2's Game State Integration (GSI) payload. Only the local player's own
data is used: when you are dead, GSI's `player` block shows whoever you spectate,
and that is ignored (`provider.steamid` must match `player.steamid`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# CS2 competitive economy (MR12).
LOSS_BONUS = [1400, 1900, 2400, 2900, 3400]
WIN_REWARD = 3250
MAX_MONEY = 16000
ROUNDS_PER_HALF = 12
WINS_NEEDED = 13
# shortcut: one "full buy" figure per side (rifle + armor/helmet + utility, + kit on CT);
# upgrade to per-loadout prices if the advice feels too coarse.
FULL_BUY = {"CT": 4900, "T": 4300}
BOMB_TIMER = 40.0
DEFUSE_TIME = {True: 5.0, False: 10.0}


@dataclass
class Advice:
    key: str  # stable id, so the overlay replaces instead of stacking
    level: str  # call | warn | info
    text: str


@dataclass
class RoundRecord:
    number: int
    side: str
    won: bool
    kills: int
    survived: bool
    money_at_start: int
    equip_value: int
    bomb: str | None


def loss_bonus(consecutive_losses: int) -> int:
    return LOSS_BONUS[min(consecutive_losses, len(LOSS_BONUS) - 1)]


@dataclass
class GameState:
    payload: dict = field(default_factory=dict)
    history: list[RoundRecord] = field(default_factory=list)
    bomb_planted_at: float | None = None
    _phase: str | None = None
    _bomb: str | None = None
    _alive_snapshot: dict = field(default_factory=dict)
    _money_at_start: int = 0

    # -- accessors ---------------------------------------------------------
    @property
    def own(self) -> dict | None:
        p, prov = self.payload.get("player") or {}, self.payload.get("provider") or {}
        if p and prov and p.get("steamid") == prov.get("steamid"):
            return p
        return None

    @property
    def side(self) -> str | None:
        own = self.own or self._alive_snapshot
        return own.get("team") if own else None

    @property
    def round_number(self) -> int:
        """1-based number of the round being played (GSI map.round counts rounds already finished)."""
        m = self.payload.get("map") or {}
        n = int(m.get("round", 0))
        return n if (self.payload.get("round") or {}).get("phase") == "over" else n + 1

    def score(self, side: str) -> int:
        team = (self.payload.get("map") or {}).get(f"team_{side.lower()}") or {}
        return int(team.get("score", 0))

    def consecutive_losses(self, side: str) -> int:
        team = (self.payload.get("map") or {}).get(f"team_{side.lower()}") or {}
        return int(team.get("consecutive_round_losses", 0))

    # -- updates -------------------------------------------------------------
    def update(self, payload: dict, now: float) -> list[str]:
        """Merge a GSI payload; return the transitions it caused."""
        self.payload = payload
        events: list[str] = []
        own = self.own
        rnd = payload.get("round") or {}
        phase, bomb = rnd.get("phase"), rnd.get("bomb")

        hp = lambda p: int((p.get("state") or {}).get("health", 0))  # noqa: E731
        if own and hp(own) > 0:
            self._alive_snapshot = own
        elif phase == "live" and self._alive_snapshot and hp(self._alive_snapshot) > 0:
            # Dead: either our own block with 0 HP, or GSI already switched to a spectated player.
            events.append("died")
            snap = own or self._alive_snapshot
            self._alive_snapshot = {**snap, "state": {**(snap.get("state") or {}), "health": 0}}

        if phase != self._phase:
            if phase == "freezetime":
                self.bomb_planted_at = None
                self._bomb = None
                self._money_at_start = int(((own or {}).get("state") or {}).get("money", 0))
                if own:
                    self._alive_snapshot = own
            if phase == "over" and self._phase in ("live", "freezetime"):
                self._record_round(rnd)
            if phase:
                events.append(phase)
            self._phase = phase

        if bomb != self._bomb:
            if bomb == "planted":
                self.bomb_planted_at = now
            if bomb:
                events.append(f"bomb_{bomb}")
            self._bomb = bomb
        return events

    def _record_round(self, rnd: dict) -> None:
        snap = self._alive_snapshot or {}
        side = snap.get("team")
        if not side:
            return
        st = snap.get("state") or {}
        self.history.append(RoundRecord(
            number=int((self.payload.get("map") or {}).get("round", 0)),
            side=side,
            won=rnd.get("win_team") == side,
            kills=int(st.get("round_kills", 0)),
            survived=st.get("health", 0) > 0,
            money_at_start=self._money_at_start,
            equip_value=int(st.get("equip_value", 0)),
            bomb=self._bomb,
        ))


# -- rules ------------------------------------------------------------------------

def economy_call(gs: GameState) -> Advice | None:
    own = gs.own
    if not own or not own.get("team"):
        return None
    side, n = own["team"], gs.round_number
    money = int((own.get("state") or {}).get("money", 0))
    full = FULL_BUY[side]
    if n in (1, ROUNDS_PER_HALF + 1):
        return Advice("economy", "call", "Pistol round: armor or utility + upgraded pistol, not both unless you can afford it.")
    enemy = "T" if side == "CT" else "CT"
    last_of_half = n in (ROUNDS_PER_HALF, 2 * ROUNDS_PER_HALF)
    enemy_match_point = gs.score(enemy) == WINS_NEEDED - 1
    if money >= full:
        return Advice("economy", "call", f"Full buy (${money}).")
    if last_of_half or enemy_match_point:
        why = "last round of the half" if last_of_half else "enemy match point"
        return Advice("economy", "call", f"Force buy: {why}, money is worth nothing after it.")
    next_if_lose = min(MAX_MONEY, money + loss_bonus(gs.consecutive_losses(side)))
    if next_if_lose >= full:
        return Advice("economy", "call", f"Save (${money}): even on a loss you have ${next_if_lose} next round for a full buy. Follow your team's call.")
    return Advice("economy", "call", f"Eco (${money}): ${next_if_lose} next round on a loss is still short of a ${full} full buy. Buy together with your team or not at all.")


def live_advice(gs: GameState, now: float) -> list[Advice]:
    out: list[Advice] = []
    own = gs.own
    st = (own or {}).get("state") or {}
    # The countdown itself ticks client-side in the overlay; this card holds the rule.
    if gs.bomb_planted_at is not None and (gs.payload.get("round") or {}).get("bomb") == "planted":
        left = max(0.0, BOMB_TIMER - (now - gs.bomb_planted_at))
        if gs.side == "CT":
            kit = bool(st.get("defusekit"))
            need = DEFUSE_TIME[kit]
            msg = f"Defuse takes {need:.0f}s ({'kit' if kit else 'no kit'}): you must be on the bomb before the clock shows {need:.0f}s."
            out.append(Advice("bomb", "warn" if left - need < 5 else "info", msg))
        else:
            out.append(Advice("bomb", "info", "Bomb down: play time and hold the defuse from angles, don't peek."))
    if own and 0 < st.get("health", 0) < 30:
        out.append(Advice("health", "warn", f"{st['health']} HP: play for info and trades, no dry peeks."))
    for w in (own or {}).get("weapons", {}).values():
        if w.get("state") == "active" and w.get("ammo_clip_max"):
            if w.get("ammo_clip", 0) <= w["ammo_clip_max"] // 4 and w.get("ammo_reserve", 0) > 0:
                out.append(Advice("ammo", "info", f"{w['ammo_clip']}/{w['ammo_clip_max']} in the mag: reload behind cover."))
    return out


def summary(gs: GameState, last: int = 6) -> dict:
    """Compact facts for the AI coach."""
    own = gs.own or gs._alive_snapshot or {}
    side = own.get("team")
    m = gs.payload.get("map") or {}
    return {
        "map": m.get("name"),
        "round": gs.round_number,
        "side": side,
        "score": {"CT": gs.score("CT"), "T": gs.score("T")},
        "money": (own.get("state") or {}).get("money"),
        "match_stats": own.get("match_stats"),
        "recent_rounds": [asdict(r) for r in gs.history[-last:]],
    }
