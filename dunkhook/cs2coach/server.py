"""Local coach server: receives CS2 GSI posts, serves the overlay, pushes advice over SSE.

    CS2 --POST /--> this server --SSE /events--> overlay page (browser, second screen)

Binds to 127.0.0.1 only. GSI posts must carry the token from the generated cfg.
The AI tip runs once per round at the start of freezetime, in a background thread,
so a slow model never delays the rule-based advice.
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..core.llm import LLM, LLMError
from ..core.schema import obj, string
from .overlay import OVERLAY_HTML
from .rules import GameState, economy_call, live_advice, summary

COACH_SYSTEM = """You are a calm Counter-Strike 2 coach talking to one player between rounds, \
during the buy phase. You get the player's own recent rounds (no enemy information exists). Give ONE \
specific, actionable focus for the coming round, grounded in a pattern in the data (e.g. dying \
without kills on repeated rounds, saving too rarely, low impact after full buys). Max 20 words. No \
generic advice like "communicate" unless the data shows a reason. If the data is too thin, say what \
to watch for this round."""

TIP_SCHEMA = obj(tip=string("max 20 words"), because=string("the pattern in the data, max 15 words"))


def gsi_cfg(token: str, port: int) -> str:
    """The cfg CS2 reads from game/csgo/cfg/. Only the local player's own data blocks."""
    return f'''"dunkhook coach"
{{
  "uri" "http://127.0.0.1:{port}/"
  "timeout" "5.0"
  "buffer" "0.1"
  "throttle" "0.25"
  "heartbeat" "10.0"
  "auth"
  {{
    "token" "{token}"
  }}
  "data"
  {{
    "provider" "1"
    "map" "1"
    "round" "1"
    "player_id" "1"
    "player_state" "1"
    "player_weapons" "1"
    "player_match_stats" "1"
  }}
}}
'''


def install_cfg(cfg_dir: str | Path, port: int, token: str | None = None) -> tuple[Path, str]:
    token = token or secrets.token_urlsafe(24)
    path = Path(cfg_dir) / "gamestate_integration_dunkhook.cfg"
    path.write_text(gsi_cfg(token, port))
    return path, token


class Hub:
    """Fan-out of overlay messages to every connected browser."""

    def __init__(self):
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.last: dict[str, dict] = {}  # latest message per key, replayed to new clients

    def publish(self, msg: dict) -> None:
        with self.lock:
            self.last[msg["key"]] = msg
            for q in self.clients:
                q.put(msg)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            for msg in self.last.values():
                q.put(msg)
            self.clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)


class Coach:
    def __init__(self, token: str, llm: LLM | None = None, clock=time.monotonic, log=print):
        self.token = token
        self.llm = llm
        self.clock = clock
        self.log = log
        self.state = GameState()
        self.hub = Hub()
        self.lock = threading.Lock()
        self._tip_round: int | None = None

    def handle(self, payload: dict) -> bool:
        if (payload.get("auth") or {}).get("token") != self.token:
            return False
        with self.lock:
            now = self.clock()
            events = self.state.update(payload, now)
            gs = self.state
            hdr = {"round": gs.round_number, "side": gs.side, "map": (payload.get("map") or {}).get("name"),
                   "score": {"CT": gs.score("CT"), "T": gs.score("T")},
                   "phase": (payload.get("round") or {}).get("phase")}
            self.hub.publish({"key": "header", "level": "info", **hdr})
            if "freezetime" in events:
                for key in ("bomb", "health", "ammo", "died"):
                    self.hub.publish({"key": key, "clear": True})
                eco = economy_call(gs)
                if eco:
                    self.hub.publish(eco.__dict__)
                self._maybe_tip()
            if "bomb_planted" in events:
                self.hub.publish({"key": "bomb_clock", "planted_ago": 0.0, "level": "info"})
            if {"bomb_defused", "bomb_exploded", "over"} & set(events):
                self.hub.publish({"key": "bomb_clock", "clear": True})
            if "died" in events:
                self.hub.publish({"key": "died", "level": "info", "text": "Dead: watch your teammate's view, call what you see."})
            current = {a.key: a for a in live_advice(gs, now)}
            for key in ("bomb", "health", "ammo"):
                self.hub.publish(current[key].__dict__ if key in current else {"key": key, "clear": True})
        return True

    def _maybe_tip(self) -> None:
        if self.llm is None or self._tip_round == self.state.round_number or not self.state.history:
            return
        self._tip_round = self.state.round_number
        facts = summary(self.state)
        threading.Thread(target=self._tip, args=(facts,), daemon=True).start()

    def _tip(self, facts: dict) -> None:
        try:
            out = self.llm.json(COACH_SYSTEM, json.dumps(facts), TIP_SCHEMA)
        except LLMError as exc:
            self.log(f"[coach] AI tip skipped: {exc}")
            return
        except Exception as exc:  # network etc.: the rules keep working
            self.log(f"[coach] AI tip failed: {type(exc).__name__}: {exc}")
            return
        self.hub.publish({"key": "ai", "level": "info", "text": out["tip"], "because": out["because"],
                          "round": facts["round"]})


def make_server(coach: Coach, port: int = 3000) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(200 if coach.handle(payload) else 403)
            self.end_headers()

        def do_GET(self):
            if self.path == "/events":
                return self._events()
            if self.path not in ("/", "/index.html"):
                self.send_response(404)
                self.end_headers()
                return
            body = OVERLAY_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = coach.hub.subscribe()
            try:
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                coach.hub.unsubscribe(q)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
