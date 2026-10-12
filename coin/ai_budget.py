"""Durable admission control for CLI inference, including failed/unknown usage."""
from datetime import datetime, timedelta, timezone
import json


class AIBudget:
    def __init__(self, db):
        self.db = db
        db.executescript('''
            CREATE TABLE IF NOT EXISTS ai_calls (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL, day TEXT NOT NULL,
                frame_key TEXT NOT NULL, base_tick INTEGER NOT NULL, status TEXT NOT NULL,
                tokens INTEGER NOT NULL, usage TEXT, response TEXT,
                UNIQUE(scope,frame_key));
            CREATE TABLE IF NOT EXISTS worker_review_clock (
                agent TEXT PRIMARY KEY, last_tick INTEGER NOT NULL);
        ''')

    def day(self):
        return datetime.now(timezone(timedelta(hours=9))).date().isoformat()

    def status(self, scope, settings):
        day = self.day()
        calls, tokens = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(tokens),0) FROM ai_calls WHERE scope=? AND day=?",
            (scope, day)).fetchone()
        unknown = self.db.execute("SELECT COUNT(*) FROM ai_calls WHERE scope=? AND day=? AND usage IS NULL",
                                  (scope, day)).fetchone()[0]
        return {"day": day, "calls": calls, "accounted_tokens": tokens, "unknown_usage_calls": unknown,
                "max_daily_calls": settings["max_daily_calls"], "daily_token_budget": settings["daily_token_budget"],
                "remaining_tokens": max(0, settings["daily_token_budget"] - tokens)}

    def cached(self, scope, frame_key, base_tick):
        old = self.db.execute("SELECT * FROM ai_calls WHERE scope=? AND frame_key=?", (scope, frame_key)).fetchone()
        if old is None:
            return None
        if old["base_tick"] != base_tick:
            return {"base_tick": base_tick, "error": "State advanced since saved AI request"}
        return json.loads(old["response"]) if old["response"] else {
            "base_tick": base_tick, "error": "Prior inference interrupted; request not repeated"}

    def invoke(self, scope, settings, frame_key, base_tick, provider, invoke, agents=()):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cached = self.cached(scope, frame_key, base_tick)
            if cached is not None:
                self.db.execute("COMMIT")
                return cached
            status = self.status(scope, settings)
            reserve = settings["reserve_tokens_per_call"]
            if status["calls"] >= settings["max_daily_calls"] or status["remaining_tokens"] < reserve:
                self.db.execute("COMMIT")
                return {"base_tick": base_tick, "skipped": "daily_call_or_token_budget"}
            cursor = self.db.execute("INSERT INTO ai_calls(scope,day,frame_key,base_tick,status,tokens) VALUES (?,?,?,?,?,?)",
                                     (scope, status["day"], frame_key, base_tick, "pending", reserve))
            call_id = cursor.lastrowid
            for agent in agents:
                self.db.execute("INSERT INTO worker_review_clock VALUES (?,?) ON CONFLICT(agent) DO UPDATE SET last_tick=excluded.last_tick",
                                (agent, base_tick + 1))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        provider.last_usage = None
        try:
            response = {"base_tick": base_tick, "plan": invoke()}
        except Exception as exc:
            response = {"base_tick": base_tick, "error": f"{type(exc).__name__}: {exc}"}
        usage = provider.last_usage
        if not isinstance(usage, dict) or not all(type(usage.get(k)) is int and usage[k] >= 0
                                               for k in ("input_tokens", "output_tokens")):
            usage = None
        tokens = usage["input_tokens"] + usage["output_tokens"] if usage else reserve
        self.db.execute("UPDATE ai_calls SET status=?,tokens=?,usage=?,response=? WHERE id=?",
                        ("error" if "error" in response else "completed", tokens,
                         json.dumps(usage) if usage else None, json.dumps(response, ensure_ascii=False), call_id))
        return response
