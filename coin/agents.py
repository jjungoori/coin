"""Persistent paper-agent command line: python -m coin.agents."""
import argparse
from collections import Counter
from copy import deepcopy
import json
import sys
import time

from .ecosystem import EvolutionSystem, InvalidFrameError
from .feeds import DemoFeed, LiveFeed


def emit(value):
    print(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")), flush=True)


def sleep_interval(seconds):
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(remaining, 60))


def compact_tick(result):
    """Keep streaming output small; complete event details remain in SQLite."""
    result = dict(result)
    events = result.pop("events", [])
    if events:
        counts = Counter(event["kind"] for event in events)
        result["events"] = dict(sorted(counts.items()))
        result.update(trades=counts["trade"], births=counts["agent_born"],
                      deaths=counts["agent_died"], sandboxes=counts["supervisor_environment"])
    return result


def resume_demo(system, seed):
    """Record replay parameters and advance the deterministic feed to saved tick."""
    status = system.status()
    if status["source"] not in (None, "demo"):
        raise ValueError("Live and demo sources must use separate databases")
    system.db.execute("CREATE TABLE IF NOT EXISTS runner_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    saved = system.db.execute("SELECT value FROM runner_settings WHERE key='demo_seed'").fetchone()
    if saved is None:
        if status["tick"]:
            raise ValueError("Existing demo lacks saved replay seed; choose a new --db path")
        system.db.execute("INSERT INTO runner_settings VALUES ('demo_seed', ?)", (str(seed),))
    elif int(saved[0]) != seed:
        raise ValueError("Demo seed differs from saved run; use original --seed or a new --db path")
    feed = DemoFeed(system.config, seed)
    for _ in range(status["tick"]):
        feed.next_frame()
    return feed


def parser():
    result = argparse.ArgumentParser(description="Independent evolutionary paper trading agents")
    result.add_argument("--db", default=None, help="SQLite store (demo defaults to a separate demo store)")
    result.add_argument("--config", default="agents.toml")
    result.add_argument("--ai", choices=("codex", "rules"), default=None,
                        help="Supervisor backend; demo defaults to offline rules, run uses configured backend")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("status")
    mode = commands.add_parser("mode", help="Change an active wallet agent's decision type")
    mode.add_argument("agent")
    mode.add_argument("decision_mode", choices=("rules", "hybrid"))
    events = commands.add_parser("events")
    events.add_argument("--limit", type=int, default=30)
    run = commands.add_parser("run")
    run.add_argument("--steps", type=int, default=0, help="0: continuously; otherwise bounded attempts")
    run.add_argument("--interval", type=float)
    run.add_argument("--external", action=argparse.BooleanOptionalAction, default=None,
                     help="Override external evidence collection for this run; saved strategy config stays fixed")
    demo = commands.add_parser("demo")
    demo.add_argument("--steps", type=int, default=60)
    demo.add_argument("--seed", type=int, default=7)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if getattr(args, "steps", 0) < 0 or getattr(args, "limit", 1) < 1:
        parser().error("steps must be nonnegative and limit must be positive")
    if getattr(args, "interval", None) is not None and args.interval <= 0:
        parser().error("interval must be positive")
    db = args.db or ("data/demo-agents.sqlite3" if args.command == "demo" else "data/agents.sqlite3")
    system = None
    try:
        backend = args.ai or ("rules" if args.command == "demo" else None)
        system = EvolutionSystem(db=db, config_path=args.config, ai_backend=backend)
        if args.command == "init":
            emit(system.initialize())
        elif args.command == "status":
            emit(system.status())
        elif args.command == "mode":
            emit(system.set_decision_mode(args.agent, args.decision_mode))
        elif args.command == "events":
            emit(system.events(limit=args.limit))
        else:
            system.initialize()
            expected_source = "demo" if args.command == "demo" else "live"
            if system.status()["source"] not in (None, expected_source):
                raise ValueError("Live and demo sources must use separate databases")
            if args.command == "demo":
                feed, interval = resume_demo(system, args.seed), 0
            else:
                feed_config = deepcopy(system.config)
                if args.external is not None:
                    feed_config["system"]["external"] = args.external
                feed = LiveFeed(feed_config)
                interval = args.interval if args.interval is not None else system.config["system"].get("poll_seconds", 30)
            attempt = 0
            while args.steps == 0 or attempt < args.steps:
                attempt += 1
                try:
                    frame = feed.next_frame()
                except Exception as exc:
                    if args.command == "demo":
                        raise
                    error = f"{type(exc).__name__}: {exc}"
                    system.record_source_error(error)
                    emit({"status": "source_error", "attempt": attempt, "error": error})
                else:
                    try:
                        result = system.tick(frame)
                    except InvalidFrameError as exc:
                        if args.command == "demo":
                            raise
                        error = f"InvalidFrameError: {exc}"
                        system.record_source_error(error)
                        emit({"status": "source_error", "attempt": attempt, "error": error})
                    else:
                        emit(compact_tick(result))
                if interval and (args.steps == 0 or attempt < args.steps):
                    sleep_interval(interval)
        return 0
    except KeyboardInterrupt:
        emit({"status": "stopped", "reason": "keyboard_interrupt"})
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if system is not None:
            system.close()


if __name__ == "__main__":
    raise SystemExit(main())
