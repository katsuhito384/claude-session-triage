#!/usr/bin/env python3
"""session-triage の補助: Code タブの外（ターミナルの claude）で動いている CLI セッションを列挙し、
最後のやり取りを JSON で出す。手順は同じフォルダの cli_sessions.md。

読むだけ。名前の変更・メッセージ送信・プロセスの停止・transcript の書き換えはしない。
標準ライブラリだけで動く（Python 3.8 以上）。

使い方:
  python3 cli_sessions.py [--max-idle-hours 72] [--state <state.json>] [--turns 6] [--max-chars 1500]
                          [--exclude <sessionId> ...] [--reread <sessionId> ...] [--all]

出力（stdout・UTF-8 の JSON）:
  {generated_at, max_idle_hours, sessions: [...], skipped_idle, skipped_not_cli, skipped_unreadable, warnings?, error?}
  skipped_idle: 長く動いていない idle を見送った数 / skipped_not_cli: 対象外（interactive でない・entrypoint が不明）
  skipped_unreadable: pid json が無い・読めない・プロセスが終了していた数 / warnings: 1件ごとの想定外（記録は残す）
  sessions[]: sessionId, pid, tty, name, nameSource, cwd, agentStatus (busy|waiting|idle), isRunning, waitingFor,
              lastActivityAt, lastActivityMs, idleHours, resume, transcript, tailExhausted, unchanged,
              trailingToolUse, turns[]
  turns[]:    role (user|assistant|summary|auto), at, text

しくみ:
  1. `claude agents --json`（Claude Code 本体のコマンド・TTY 不要）で生きているセッションを列挙する
  2. kind が interactive で、~/.claude/sessions/<pid>.json の entrypoint が "cli" のものだけ残す
     （"claude-desktop" は Code タブ側の手順が扱う）
  3. agentStatus が idle で max_idle_hours より長く動いていないものは見送る（件数だけ数える）
     Windows では resume は `claude --resume <sessionId>` だけ（cwd で実行する）
  4. ~/.claude/projects/<cwd の英数字以外を - に置き換えた名前>/<sessionId>.jsonl の末尾から
     本人と Claude の最後のやり取りを取り出す。role は user / assistant のほか、
     summary（コンテキスト圧縮の要約）と auto（フックや通知が注入した本文）。どちらも本人の発言ではない
  5. --state を渡すと、前回と lastActivityMs・isRunning・agentStatus が同じものは unchanged: true にして
     やり取りを省く。--reread で指定した sessionId だけは読み直す。--all で省略をやめる
"""
import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
SESSIONS_DIR = os.path.join(HOME, ".claude", "sessions")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
TAIL_BYTES = 512 * 1024  # transcript は大きいので末尾だけ読む。足りなければ倍にして読み直す
TAIL_MAX = 8 * 1024 * 1024
SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
SUMMARY_PREFIX = "This session is being continued from a previous conversation"
HUMAN_PROMPT_SOURCES = ("typed", "queued", "suggestion_accepted", "sdk")
# origin の無い古い行で、注入された本文と見なす先頭タグ
INJECTED_PREFIXES = (
    "<task-notification", "<system-reminder", "<local-command-caveat", "<local-command-stdout",
    "<command-name", "<command-message", "<bash-input", "<bash-stdout", "<bash-stderr",
    "<cross-session-message", "<side-sessions-event", "<ci-monitor-event",
)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def list_agents():
    cmd = shutil.which("claude") or "claude"  # Windows では claude.cmd を拾う
    try:
        r = subprocess.run(
            [cmd, "agents", "--json"],
            stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "claude agents --json を実行できない: %s" % e
    if r.returncode != 0:
        return None, "claude agents --json が失敗 (exit %d): %s" % (r.returncode, (r.stderr or "").strip()[:200])
    try:
        data = json.loads(r.stdout)
    except ValueError as e:
        return None, "claude agents --json の出力を JSON として読めない: %s" % e
    if not isinstance(data, list):
        return None, "claude agents --json の出力が配列でない"
    return data, None


def pid_alive(pid):
    if os.name == "nt":
        return True  # Windows の os.kill(pid, 0) はプロセスを終了させるので使わない
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def ttys_of(pids):
    """pid → tty 名（POSIX のみ。分からなければ空）"""
    if os.name == "nt" or not pids:
        return {}
    try:
        r = subprocess.run(
            ["ps", "-o", "pid=,tty=", "-p", ",".join(str(p) for p in pids)],
            stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            out[int(parts[0])] = parts[1]
    return out


def resume_command(cwd, sid):
    if os.name == "nt":
        return "claude --resume %s" % sid  # cwd で実行する。シェルごとの書式に踏み込まない
    return "cd %s && claude --resume %s" % (shlex.quote(cwd), sid)


def transcript_path(cwd, session_id):
    d = re.sub(r"[^A-Za-z0-9]", "-", cwd or "")
    p = os.path.join(PROJECTS_DIR, d, session_id + ".jsonl")
    if os.path.exists(p):
        return p
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", session_id + ".jsonl"))
    return hits[0] if hits else None


def blocks_of(entry):
    m = entry.get("message")
    if not isinstance(m, dict):
        return []
    c = m.get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    if isinstance(c, list):
        return [b for b in c if isinstance(b, dict)]
    return []


def text_of(blocks):
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text" and b.get("text"))


def role_of(entry, text):
    """user / assistant に加えて、本人の発言ではないものに札を付ける。
    summary: コンテキスト圧縮の要約。auto: フックや通知が注入した本文（task-notification など）"""
    if entry.get("type") == "assistant":
        return "assistant"
    if entry.get("isCompactSummary"):
        return "summary"
    origin = entry.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind is not None:  # origin があればそれを最優先（本体の分類と同じ）
        return "user" if kind == "human" else "auto"
    source = entry.get("promptSource")
    if source in HUMAN_PROMPT_SOURCES:
        return "user"
    if source == "system":
        return "auto"
    # origin の無い古い行: 既知の注入タグで始まるときだけ auto
    if text.startswith(INJECTED_PREFIXES):
        return "auto"
    if text.startswith(SUMMARY_PREFIX):
        return "summary"
    return "user"


def clip(text, max_chars):
    """長い発言は先頭を少し残して末尾を優先する（質問や次の一手は末尾にあることが多い）"""
    if len(text) <= max_chars:
        return text
    head = min(300, max_chars // 4)
    return text[:head] + " …(中略)… " + text[-(max_chars - head):]


def describe_tool_use(blocks):
    names = []
    for b in blocks:
        if b.get("type") != "tool_use":
            continue
        name = b.get("name") or "?"
        if name == "AskUserQuestion":
            inp = b.get("input")
            qs = inp.get("questions") if isinstance(inp, dict) else None
            qs = qs if isinstance(qs, list) else []
            qtext = " / ".join(q.get("question", "") for q in qs if isinstance(q, dict))
            name += ": " + qtext[:300] if qtext else ""
        names.append(name)
    return ", ".join(names)


def read_tail(path, nbytes):
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > nbytes:
            fh.seek(size - nbytes)
            fh.readline()  # 途中から始まる行を捨てる
        return fh.read().decode("utf-8", errors="replace"), size > nbytes


def last_turns(path, n, max_chars):
    """末尾の n 件のやり取りと、最後の発言のあとにツール呼び出しだけで止まっているか"""
    nbytes = TAIL_BYTES
    while True:
        data, truncated = read_tail(path, nbytes)
        turns = []
        trailing = None
        for line in data.splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict) or o.get("type") not in ("user", "assistant"):
                continue
            if o.get("isSidechain") or o.get("isMeta"):
                continue
            blocks = blocks_of(o)
            t = text_of(blocks).strip()
            if not t:
                if o["type"] == "assistant":
                    desc = describe_tool_use(blocks)
                    if desc:
                        trailing = desc  # 返事（tool_result）が来たら下で消える
                elif any(b.get("type") == "tool_result" for b in blocks):
                    trailing = None
                continue
            turns.append({"role": role_of(o, t), "at": o.get("timestamp"), "text": clip(t, max_chars)})
            trailing = None
        if len(turns) >= n or not truncated or nbytes >= TAIL_MAX:
            exhausted = not turns and truncated and nbytes >= TAIL_MAX
            return turns[-n:], trailing, exhausted
        nbytes *= 2


def load_state(path):
    if not path:
        return {}
    sp = os.path.expanduser(path)
    if not os.path.exists(sp):
        return {}
    try:
        with open(sp, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def same_as_prev(prev, rec):
    if not isinstance(prev, dict):
        return False
    if "lastActivityMs" in prev:
        if prev.get("lastActivityMs") != rec["lastActivityMs"]:
            return False
    elif prev.get("lastActivityAt") != rec["lastActivityAt"]:
        return False
    if bool(prev.get("isRunning")) != rec["isRunning"]:
        return False
    if "agentStatus" in prev and prev.get("agentStatus") != rec["agentStatus"]:
        return False
    return True


def positive_int(v):
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("1 以上")
    return n


def collect(a, out):
    agents, err = list_agents()
    if err:
        out["error"] = err
        return 1
    state = {} if a.all else load_state(a.state)
    reread = set(a.reread or [])
    exclude = set(a.exclude or [])
    now_ms = int(time.time() * 1000)
    ttys = ttys_of([ag.get("pid") for ag in agents if isinstance(ag, dict) and isinstance(ag.get("pid"), int)])

    for ag in agents:
        try:
            if not isinstance(ag, dict) or ag.get("kind") != "interactive":
                out["skipped_not_cli"] += 1
                continue
            pid = ag.get("pid")
            sid = ag.get("sessionId")
            if not isinstance(pid, int) or not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
                out["skipped_not_cli"] += 1
                continue  # sessionId はパスに使うので UUID の形だけ通す
            if sid in exclude:
                continue
            try:
                with open(os.path.join(SESSIONS_DIR, "%d.json" % pid), encoding="utf-8") as fh:
                    info = json.load(fh)
            except (OSError, ValueError):
                out["skipped_unreadable"] += 1
                continue
            if not isinstance(info, dict):
                out["skipped_unreadable"] += 1
                continue
            if info.get("entrypoint") != "cli":
                if info.get("entrypoint") != "claude-desktop":
                    out["skipped_not_cli"] += 1  # 不明なものは数えるだけ
                continue
            if not pid_alive(pid):
                out["skipped_unreadable"] += 1
                continue

            raw_status = ag.get("status") or info.get("status") or "idle"
            agent_status = raw_status if raw_status in ("idle", "waiting") else "busy"
            times = [info.get("updatedAt"), info.get("statusUpdatedAt"), info.get("startedAt"), ag.get("startedAt")]
            last_ms = max([t for t in times if isinstance(t, (int, float))] or [0])
            idle_h = (now_ms - last_ms) / 3600000.0
            if agent_status == "idle" and idle_h > a.max_idle_hours:
                out["skipped_idle"] += 1
                continue

            cwd = ag.get("cwd") or info.get("cwd") or ""
            rec = {
                "sessionId": sid,
                "pid": pid,
                "tty": ttys.get(pid),
                "name": ag.get("name") or info.get("name") or "",
                "nameSource": info.get("nameSource"),
                "cwd": cwd,
                "agentStatus": agent_status,
                "isRunning": agent_status == "busy",
                "waitingFor": ag.get("waitingFor") or info.get("waitingFor"),
                "lastActivityAt": iso(last_ms),
                "lastActivityMs": int(last_ms),
                "idleHours": round(idle_h, 1),
                "resume": resume_command(cwd, sid),
                "transcript": None,
                "tailExhausted": False,
                "unchanged": False,
                "trailingToolUse": None,
                "turns": [],
            }
            if sid not in reread and same_as_prev(state.get(sid), rec):
                rec["unchanged"] = True
            tp = transcript_path(cwd, sid)
            rec["transcript"] = tp
            if tp and not rec["isRunning"] and not rec["unchanged"]:
                try:
                    rec["turns"], rec["trailingToolUse"], rec["tailExhausted"] = last_turns(tp, a.turns, a.max_chars)
                except Exception as e:  # transcript が読めなくても記録は残す（waiting の判定は生きる）
                    out.setdefault("warnings", []).append("%s: transcript %s: %s" % (sid, type(e).__name__, e))
            out["sessions"].append(rec)
        except Exception as e:  # 1件の想定外で全体を落とさない
            out["skipped_unreadable"] += 1
            out.setdefault("warnings", []).append("%s: %s" % (type(e).__name__, e))

    out["sessions"].sort(key=lambda r: r["lastActivityMs"], reverse=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-idle-hours", type=float, default=72)
    ap.add_argument("--state", default=None, help="前回の state.json。同じものはやり取りを省く")
    ap.add_argument("--turns", type=positive_int, default=6, help="取り出すやり取りの件数（1以上）")
    ap.add_argument("--max-chars", type=positive_int, default=1500, help="1件あたりの文字数の上限")
    ap.add_argument("--exclude", action="append", metavar="SESSION_ID", help="載せない sessionId（Code タブへ移したものなど）")
    ap.add_argument("--reread", action="append", metavar="SESSION_ID", help="前回と同じでも読み直す sessionId")
    ap.add_argument("--all", action="store_true", help="--state があっても全部のやり取りを出す")
    a = ap.parse_args()

    out = {
        "generated_at": iso(int(time.time() * 1000)),
        "max_idle_hours": a.max_idle_hours,
        "sessions": [],
        "skipped_idle": 0,
        "skipped_not_cli": 0,
        "skipped_unreadable": 0,
    }
    try:
        code = collect(a, out)
    except Exception as e:  # 想定外でも JSON は返す
        out["error"] = "%s: %s" % (type(e).__name__, e)
        code = 1
    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
