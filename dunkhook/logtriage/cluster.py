"""Parse production logs into error events and cluster them by fingerprint.

Recognised inputs (mixed freely in one file):

* Python tracebacks (``Traceback (most recent call last):`` ... ``SomeError: msg``)
* JSON lines with an error/critical level (``message``/``msg`` plus optional
  ``exc_info``/``stack``/``traceback`` holding a Python traceback)
* Plain log lines containing ``ERROR``/``CRITICAL``/``FATAL``
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

FRAME_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)$')
EXC_RE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Fault|Timeout)?)(?::\s?(?P<msg>.*))?$")
LEVEL_RE = re.compile(r"\b(ERROR|CRITICAL|FATAL)\b")
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")

_NORMALISERS = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hash>"),
    (TS_RE, "<ts>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
    (re.compile(r"'[^']*'"), "'<s>'"),
    (re.compile(r'"[^"]*"'), '"<s>"'),
    (re.compile(r"\s+"), " "),
]


def normalise(msg: str) -> str:
    for rx, repl in _NORMALISERS:
        msg = rx.sub(repl, msg)
    return msg.strip()


@dataclass
class Frame:
    file: str
    line: int
    func: str


@dataclass
class ErrorEvent:
    exc_type: str
    message: str
    frames: list[Frame] = field(default_factory=list)
    timestamp: str | None = None
    raw: str = ""
    source: str = ""

    def app_frames(self, repo_markers: list[str]) -> list[Frame]:
        """Frames that belong to the application (not site-packages / stdlib)."""
        def is_app(f: Frame) -> bool:
            if repo_markers:
                return any(m in f.file for m in repo_markers)
            return "site-packages" not in f.file and "/lib/python" not in f.file and not f.file.startswith("<")
        return [f for f in self.frames if is_app(f)]

    def fingerprint(self, repo_markers: list[str]) -> str:
        frames = self.app_frames(repo_markers)[-3:] or self.frames[-1:]
        parts = [self.exc_type] + [f"{Path(f.file).name}:{f.func}" for f in frames]
        if not frames:
            parts.append(normalise(self.message))
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def _parse_traceback(lines: list[str]) -> ErrorEvent | None:
    frames: list[Frame] = []
    exc_type, msg = None, ""
    for line in lines[1:]:
        fm = FRAME_RE.match(line)
        if fm:
            frames.append(Frame(fm["file"], int(fm["line"]), fm["func"].strip()))
            continue
        if line.startswith((" ", "\t")):
            continue
        em = EXC_RE.match(line.strip())
        if em:
            exc_type, msg = em["type"], em["msg"] or ""
    if exc_type is None:
        return None
    return ErrorEvent(exc_type, msg, frames, raw="\n".join(lines))


def parse_lines(lines: list[str], source: str = "") -> list[ErrorEvent]:
    events: list[ErrorEvent] = []
    i = 0
    last_ts: str | None = None
    while i < len(lines):
        line = lines[i]
        ts = TS_RE.search(line)
        if ts:
            last_ts = ts.group(0)
        stripped = line.strip()

        if stripped.startswith("{"):
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                rec = None
            if isinstance(rec, dict):
                level = str(rec.get("level") or rec.get("levelname") or rec.get("severity") or "").upper()
                if level in {"ERROR", "CRITICAL", "FATAL"}:
                    tb = rec.get("exc_info") or rec.get("traceback") or rec.get("stack") or ""
                    ev = _parse_traceback(str(tb).splitlines()) if "Traceback" in str(tb) else None
                    msg = str(rec.get("message") or rec.get("msg") or "")
                    if ev is None:
                        ev = ErrorEvent(str(rec.get("error_type") or rec.get("logger") or "LogError"), msg)
                    ev.timestamp = str(rec.get("timestamp") or rec.get("time") or rec.get("ts") or "") or None
                    ev.raw, ev.source = stripped, source
                    events.append(ev)
                i += 1
                continue

        if "Traceback (most recent call last):" in line:
            block = [line]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                block.append(nxt)
                j += 1
                if nxt and not nxt[0].isspace() and EXC_RE.match(nxt.strip()) and not nxt.startswith("Traceback"):
                    # Chained exceptions continue after a blank line + "During handling..."
                    if j < len(lines) and lines[j].strip() == "" and j + 1 < len(lines) and (
                        lines[j + 1].startswith("During handling") or lines[j + 1].startswith("The above exception")
                    ):
                        continue
                    break
            ev = _parse_traceback(block)
            if ev:
                ev.timestamp, ev.source = last_ts, source
                events.append(ev)
            i = j
            continue

        if LEVEL_RE.search(line) and not (i + 1 < len(lines) and lines[i + 1].startswith("Traceback (most recent call last)")):
            msg = LEVEL_RE.split(line, maxsplit=1)[-1].strip(" :-|")
            events.append(ErrorEvent("LogError", msg, timestamp=last_ts, raw=line, source=source))
        i += 1
    return events


def parse_files(paths: list[str | Path]) -> list[ErrorEvent]:
    out: list[ErrorEvent] = []
    for p in paths:
        out.extend(parse_lines(Path(p).read_text(errors="replace").splitlines(), str(p)))
    return out


@dataclass
class Cluster:
    id: str
    exc_type: str
    events: list[ErrorEvent] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.events)

    @property
    def sample(self) -> ErrorEvent:
        return self.events[0]

    @property
    def first_seen(self) -> str | None:
        ts = sorted(e.timestamp for e in self.events if e.timestamp)
        return ts[0] if ts else None

    @property
    def last_seen(self) -> str | None:
        ts = sorted(e.timestamp for e in self.events if e.timestamp)
        return ts[-1] if ts else None

    def messages(self, limit: int = 5) -> list[str]:
        seen: list[str] = []
        for e in self.events:
            if e.message not in seen:
                seen.append(e.message)
            if len(seen) >= limit:
                break
        return seen

    def summary(self) -> dict:
        frames = self.sample.frames[-3:]
        return {
            "id": self.id,
            "exc_type": self.exc_type,
            "count": self.count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "messages": self.messages(),
            "top_frames": [f"{f.file}:{f.line} in {f.func}" for f in frames],
        }


def _similar(a: str, b: str) -> float:
    ta, tb = set(normalise(a).split()), set(normalise(b).split())
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / max(1, len(ta | tb))


def cluster_events(events: list[ErrorEvent], repo_markers: list[str] | None = None, merge_threshold: float = 0.6) -> list[Cluster]:
    markers = repo_markers or []
    by_fp: dict[str, Cluster] = {}
    for ev in events:
        fp = ev.fingerprint(markers)
        by_fp.setdefault(fp, Cluster(fp, ev.exc_type)).events.append(ev)

    # Second pass: frame-less clusters (plain log lines) with near-identical messages merge.
    clusters = sorted(by_fp.values(), key=lambda c: -c.count)
    merged: list[Cluster] = []
    for c in clusters:
        target = None
        if not c.sample.frames:
            for m in merged:
                if not m.sample.frames and m.exc_type == c.exc_type and _similar(m.sample.message, c.sample.message) >= merge_threshold:
                    target = m
                    break
        if target:
            target.events.extend(c.events)
        else:
            merged.append(c)
    return sorted(merged, key=lambda c: -c.count)


def render_clusters(clusters: list[Cluster]) -> str:
    total = sum(c.count for c in clusters)
    lines = [f"# Error clusters ({total} events, {len(clusters)} clusters)", ""]
    for rank, c in enumerate(clusters, 1):
        s = c.summary()
        lines.append(f"## {rank}. `{c.exc_type}` × {c.count}  (id `{c.id}`)")
        if s["first_seen"]:
            lines.append(f"- seen {s['first_seen']} → {s['last_seen']}")
        for f in s["top_frames"]:
            lines.append(f"- at `{f}`")
        for m in s["messages"]:
            lines.append(f"- message: {m[:200]}")
        lines.append("")
    return "\n".join(lines)
