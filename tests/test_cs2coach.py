import json
import threading
import time
import urllib.request

import pytest

from dunkhook.core.llm import ScriptedLLM
from dunkhook.cs2coach import Coach, GameState, economy_call, install_cfg, live_advice, loss_bonus, make_server

ME, MATE = "76561190000000001", "76561190000000002"


def payload(phase="live", rnd=4, money=3000, team="CT", hp=100, steamid=ME, bomb=None, win_team=None,
            ct=(2, 0), t=(2, 2), kit=False, kills=0, clip=30, token="tok"):
    return {
        "provider": {"steamid": ME, "name": "Counter-Strike 2"},
        "map": {"name": "de_mirage", "phase": "live", "round": rnd,
                "team_ct": {"score": ct[0], "consecutive_round_losses": ct[1]},
                "team_t": {"score": t[0], "consecutive_round_losses": t[1]}},
        "round": {k: v for k, v in {"phase": phase, "bomb": bomb, "win_team": win_team}.items() if v},
        "player": {"steamid": steamid, "team": team,
                   "state": {"health": hp, "money": money, "defusekit": kit, "round_kills": kills, "equip_value": 4700},
                   "match_stats": {"kills": 5, "deaths": 4},
                   "weapons": {"weapon_1": {"name": "weapon_m4a1", "state": "active", "ammo_clip": clip,
                                            "ammo_clip_max": 30, "ammo_reserve": 60}}},
        "auth": {"token": token},
    }


def state_of(p):
    gs = GameState()
    gs.update(p, 0.0)
    return gs


def test_loss_bonus_caps():
    assert [loss_bonus(n) for n in range(7)] == [1400, 1900, 2400, 2900, 3400, 3400, 3400]


@pytest.mark.parametrize("kw, starts", [
    ({"rnd": 0, "phase": "freezetime"}, "Pistol round"),
    ({"rnd": 12, "phase": "freezetime"}, "Pistol round"),             # round 13 = second-half pistol
    ({"money": 5200}, "Full buy"),
    ({"money": 3000, "ct": (2, 2)}, "Save"),                          # 3000 + 2400 >= 4900
    ({"money": 1500, "ct": (2, 0)}, "Eco"),                           # 1500 + 1400 < 4900
    ({"money": 1500, "rnd": 11}, "Force buy: last round of the half"),
    ({"money": 1500, "t": (12, 0)}, "Force buy: enemy match point"),
])
def test_economy_calls(kw, starts):
    assert economy_call(state_of(payload(**kw))).text.startswith(starts)


def test_spectated_player_is_ignored_and_death_detected():
    gs = GameState()
    assert gs.update(payload(phase="freezetime"), 0) == ["freezetime"]
    gs.update(payload(), 1)
    events = gs.update(payload(steamid=MATE, money=9999), 2)  # we died; GSI now shows a teammate
    assert events == ["died"]
    assert gs.own is None and economy_call(gs) is None
    assert gs.side == "CT"  # still known from our last own snapshot


def test_death_detected_from_own_zero_hp_once():
    gs = GameState()
    gs.update(payload(phase="freezetime"), 0)
    assert gs.update(payload(hp=0), 1) == ["died", "live"]
    assert gs.update(payload(hp=0), 2) == []


def test_round_is_recorded_from_own_snapshot():
    gs = GameState()
    gs.update(payload(phase="freezetime", money=4000), 0)
    gs.update(payload(kills=2), 1)
    gs.update(payload(steamid=MATE), 2)  # died, spectating
    gs.update(payload(phase="over", rnd=5, win_team="T", steamid=MATE), 3)
    (r,) = gs.history
    assert (r.side, r.won, r.kills, r.survived, r.money_at_start) == ("CT", False, 2, False, 4000)


def test_bomb_low_hp_and_ammo_advice():
    gs = GameState()
    gs.update(payload(phase="freezetime"), 0)
    assert gs.update(payload(bomb="planted", hp=20, clip=5), 10.0) == ["live", "bomb_planted"]
    adv = {a.key: a for a in live_advice(gs, 11.0)}
    assert "10s (no kit)" in adv["bomb"].text and adv["bomb"].level == "info"
    assert adv["health"].level == "warn" and "5/30" in adv["ammo"].text
    gs.update(payload(bomb="planted", kit=True), 10.0)
    late = {a.key: a for a in live_advice(gs, 10.0 + 32)}  # 8s left, kit needs 5s
    assert "5s (kit)" in late["bomb"].text and late["bomb"].level == "warn"


def test_install_cfg_has_only_own_player_blocks(tmp_path):
    path, token = install_cfg(tmp_path, 3123)
    text = path.read_text()
    assert f'"token" "{token}"' in text and "127.0.0.1:3123" in text
    assert "allplayers" not in text and '"bomb"' not in text and "grenades" not in text


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def test_server_end_to_end_with_ai_tip():
    tip_called = threading.Event()

    def handler(system, prompt, schema):
        facts = json.loads(prompt)
        assert facts["recent_rounds"][0]["kills"] == 0 and facts["side"] == "CT"
        tip_called.set()
        return {"tip": "Play off a teammate this round.", "because": "Died first without a kill last round."}

    coach = Coach("tok", llm=ScriptedLLM(json_handler=handler), log=lambda *_: None)
    server = make_server(coach, 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        assert _post(base + "/", payload(token="wrong")) == 403
        stream = urllib.request.urlopen(base + "/events", timeout=5)
        for p in (payload(phase="freezetime", money=4000), payload(), payload(hp=0),
                  payload(phase="over", rnd=5, win_team="T", hp=0),
                  payload(phase="freezetime", rnd=5, money=3000, ct=(2, 1))):
            assert _post(base + "/", p) == 200
        assert tip_called.wait(5)
        seen, deadline = {}, time.time() + 5
        while "ai" not in seen and time.time() < deadline:
            line = stream.readline().decode().strip()
            if line.startswith("data: "):
                msg = json.loads(line[6:])
                seen[msg["key"]] = msg
        assert seen["ai"]["text"] == "Play off a teammate this round."
        assert seen["economy"]["text"].startswith("Save") or seen["economy"]["text"].startswith("Eco")
        assert seen["header"]["score"] == {"CT": 2, "T": 2}
        page = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert "EventSource" in page
    finally:
        server.shutdown()


def test_no_ai_and_one_tip_per_round():
    calls = []
    llm = ScriptedLLM(json_handler=lambda *a: calls.append(1) or {"tip": "x", "because": "y"})
    coach = Coach("tok", llm=llm, log=lambda *_: None)
    coach.handle(payload(phase="freezetime"))
    coach.handle(payload(phase="over", rnd=5, win_team="CT"))
    coach.handle(payload(phase="freezetime", rnd=5))
    coach.handle(payload(phase="live", rnd=5))
    coach.handle(payload(phase="freezetime", rnd=5))  # same round again (e.g. reconnect)
    time.sleep(0.2)
    assert len(calls) == 1
    assert Coach("tok", llm=None).handle(payload()) is True
