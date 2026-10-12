"""Persistent evolutionary paper-trading ecosystem with a generative supervisor.

One frame is one atomic transition: fills, fitness, deaths, treasury transfers,
births, and observations commit together. No real exchange order adapter exists.
"""
import hashlib
import json
import math
import random
import sqlite3
import tomllib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .analysis import utc_time
from .bithumb import validate_market
from .paper import PaperBroker, number
from .strategies import breed, decide, default_genomes, validate_genome
from .generative import CodexCLI, policy_decision, validate_plan
from .generative import combine_review, compact_context, protected_decision, validate_reviews
from .ai_budget import AIBudget


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class InvalidFrameError(ValueError):
    """Transient unusable market observation; no lifecycle state was committed."""


def load_config(path="agents.toml"):
    defaults = {
        "system": {"markets": ["KRW-BTC", "KRW-ETH"], "unit": 5, "poll_seconds": 30,
                   "external": False, "research_config": "research.toml", "research_every_ticks": 12,
                   "seed": 7, "fee_rate": "0.0004", "min_notional": "5000"},
        "population": {"initial_population": 3, "target_population": 3, "max_population": 6,
                       "agent_cash": "100000", "main_budget": "1000000"},
        "survival": {"max_drawdown": 0.15, "min_equity_ratio": 0.75, "grace_ticks": 48,
                     "evaluation_every": 24, "min_return": -0.02, "min_excess_return": -0.06,
                     "inactivity_ticks": 96, "min_trades": 1},
        "evolution": {"every_ticks": 24, "mutation_rate": 0.25, "max_births_per_tick": 2, "min_parent_age": 12},
        "supervisor": {"every_ticks": 12, "spawn_every_ticks": 24,
                       "environment_every_ticks": 48, "experiment_reserve": "1000000",
                       "sandbox_budget": "400000", "max_environments": 3},
        "ai": {"backend": "rules", "command": "codex", "model": "", "every_ticks": 12,
               "timeout_seconds": 180, "reasoning_effort": "medium", "max_input_chars": 60000,
               "max_daily_calls": 24, "daily_token_budget": 300000, "reserve_tokens_per_call": 16000},
        "workers": {"initial_modes": ["rules"], "model": "gpt-6-luna", "reasoning_effort": "low",
                    "every_ticks": 6, "max_agents_per_call": 2, "timeout_seconds": 90,
                    "max_input_chars": 6000, "max_daily_calls": 48,
                    "daily_token_budget": 150000, "reserve_tokens_per_call": 12000}}
    supplied = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    # Legacy supervisor cash no longer allocates a wallet or affects funding.
    supplied.get("supervisor", {}).pop("cash", None)
    if set(supplied) - set(defaults):
        raise ValueError("Unknown agents config section")
    for section, values in supplied.items():
        if set(values) - set(defaults[section]):
            raise ValueError(f"Unknown {section} config setting")
        defaults[section].update(values)
    config = defaults
    ai = config["ai"]
    if ai["backend"] not in ("rules", "codex"):
        raise ValueError("AI backend must be rules or codex")
    for key in ("command", "model"):
        if not isinstance(ai[key], str) or (key == "command" and not ai[key].strip()):
            raise ValueError(f"Invalid AI {key}")
    for key in ("every_ticks", "timeout_seconds"):
        if type(ai[key]) is not int or ai[key] < 1:
            raise ValueError(f"AI {key} must be a positive integer")
    workers = config["workers"]
    if not isinstance(workers["initial_modes"], list) or not workers["initial_modes"] or any(
            mode not in ("rules", "hybrid") for mode in workers["initial_modes"]):
        raise ValueError("Worker initial_modes must list rules/hybrid modes")
    if not isinstance(workers["model"], str) or not workers["model"].strip():
        raise ValueError("Worker model must be explicitly specified")
    for settings in (ai, workers):
        if settings["reasoning_effort"] not in ("low", "medium", "high"):
            raise ValueError("Invalid AI reasoning effort")
        for key in ("max_input_chars", "max_daily_calls", "daily_token_budget", "reserve_tokens_per_call",
                    "every_ticks", "timeout_seconds"):
            if type(settings[key]) is not int or settings[key] < 1:
                raise ValueError(f"Invalid AI budget setting: {key}")
    if type(workers["max_agents_per_call"]) is not int or not 1 <= workers["max_agents_per_call"] <= 16:
        raise ValueError("Worker batch size must be 1..16")
    system, pop, survival, evo, boss = (config[k] for k in ("system", "population", "survival", "evolution", "supervisor"))
    if not isinstance(system["markets"], list) or not system["markets"] or len(set(system["markets"])) != len(system["markets"]):
        raise ValueError("Markets must be a nonempty unique list")
    for market in system["markets"]:
        if not validate_market(market).startswith("KRW-"):
            raise ValueError("Only KRW markets are supported")
    if system["unit"] not in (1, 3, 5, 10, 15, 30, 60, 240):
        raise ValueError("Invalid candle unit")
    for section, keys in ((system, ("research_every_ticks",)), (pop, ("initial_population", "target_population", "max_population")),
                          (survival, ("grace_ticks", "evaluation_every", "inactivity_ticks", "min_trades")),
                          (evo, ("every_ticks", "max_births_per_tick", "min_parent_age")),
                          (boss, ("every_ticks", "spawn_every_ticks", "environment_every_ticks", "max_environments"))):
        for key in keys:
            if type(section[key]) is not int or section[key] < 1:
                raise ValueError(f"{key} must be a positive integer")
    if not pop["initial_population"] <= pop["target_population"] <= pop["max_population"]:
        raise ValueError("Population sizes must satisfy initial <= target <= max")
    number(system["poll_seconds"])
    if type(system["seed"]) is not int or type(system["external"]) is not bool:
        raise ValueError("Invalid seed or external setting")
    for section, keys in ((system, ("fee_rate", "min_notional")), (pop, ("agent_cash", "main_budget")),
                          (boss, ("experiment_reserve", "sandbox_budget"))):
        for key in keys:
            section[key] = str(number(section[key], positive=key != "fee_rate"))
    if Decimal(system["fee_rate"]) >= 1:
        raise ValueError("Fee rate must be below 1")
    for section, key, low, high in ((survival, "max_drawdown", 0, 1), (survival, "min_equity_ratio", 0, 1),
                                   (survival, "min_return", -1, 1), (survival, "min_excess_return", -2, 2),
                                   (evo, "mutation_rate", 0, 1)):
        value = float(section[key])
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Invalid {key}")
        section[key] = value
    if survival["max_drawdown"] == 0 or survival["min_equity_ratio"] == 0:
        raise ValueError("Risk survival limits must be positive")
    if Decimal(pop["main_budget"]) < Decimal(pop["agent_cash"]) * pop["initial_population"]:
        raise ValueError("Main budget cannot fund the initial trading population")
    if Decimal(boss["sandbox_budget"]) < Decimal(pop["agent_cash"]):
        raise ValueError("Sandbox budget must fund at least one agent")
    return config


class EvolutionSystem:
    def __init__(self, db="data/agents.sqlite3", config_path="agents.toml", ai_backend=None, planner=None, worker=None):
        self.config = load_config(config_path)
        self.ai_backend = ai_backend or self.config["ai"]["backend"]
        if self.ai_backend not in ("rules", "codex"):
            raise ValueError("AI backend must be rules or codex")
        self.planner = planner or CodexCLI(self.config["ai"])
        self.worker = worker or CodexCLI({**self.config["workers"], "command": self.config["ai"]["command"]})
        self.broker = PaperBroker(db, self.config["system"]["fee_rate"], self.config["system"]["min_notional"])
        self.db = self.broker.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS ecosystem_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS environments (
                id TEXT PRIMARY KEY, budget TEXT NOT NULL, treasury TEXT NOT NULL,
                created_tick INTEGER NOT NULL, purpose TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY REFERENCES wallets(agent), environment TEXT NOT NULL REFERENCES environments(id),
                role TEXT NOT NULL, market TEXT NOT NULL, genome TEXT NOT NULL, parents TEXT NOT NULL,
                generation INTEGER NOT NULL, born_tick INTEGER NOT NULL, age INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active', peak_equity TEXT NOT NULL,
                max_drawdown REAL NOT NULL DEFAULT 0, last_return REAL NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 0, benchmark_price TEXT,
                terminal_equity TEXT, death_reason TEXT, last_bar TEXT);
            CREATE TABLE IF NOT EXISTS frames (
                tick INTEGER PRIMARY KEY, frame_key TEXT UNIQUE NOT NULL, source TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                tick INTEGER NOT NULL REFERENCES frames(tick), agent TEXT NOT NULL REFERENCES agents(id),
                equity TEXT NOT NULL, return_fraction REAL NOT NULL, drawdown REAL NOT NULL,
                benchmark_return REAL NOT NULL, score REAL NOT NULL, PRIMARY KEY(tick, agent));
            CREATE TABLE IF NOT EXISTS decisions (
                tick INTEGER NOT NULL REFERENCES frames(tick), agent TEXT NOT NULL REFERENCES agents(id),
                action TEXT NOT NULL, reason TEXT NOT NULL, trade_id TEXT, outcome TEXT NOT NULL,
                PRIMARY KEY(tick, agent));
            CREATE TABLE IF NOT EXISTS ecosystem_events (
                id INTEGER PRIMARY KEY, tick INTEGER NOT NULL, kind TEXT NOT NULL,
                agent TEXT, environment TEXT, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_policies (
                agent TEXT PRIMARY KEY REFERENCES agents(id), policy TEXT NOT NULL, updated_tick INTEGER NOT NULL);
        ''')
        if "last_bar" not in {r[1] for r in self.db.execute("PRAGMA table_info(agents)")}:
            self.db.execute("ALTER TABLE agents ADD COLUMN last_bar TEXT")
        if "decision_mode" not in {r[1] for r in self.db.execute("PRAGMA table_info(agents)")}:
            self.db.execute("ALTER TABLE agents ADD COLUMN decision_mode TEXT NOT NULL DEFAULT 'rules'")
            modes = self.config["workers"]["initial_modes"]
            for index, row in enumerate(self.db.execute("SELECT id FROM agents WHERE role='trader' AND status='active' ORDER BY born_tick,id").fetchall()):
                mode = modes[index % len(modes)]
                self.db.execute("UPDATE agents SET decision_mode=? WHERE id=?", (mode, row[0]))
                if mode != "rules":
                    self._event(int(self._meta("tick", "0")), "agent_mode_migrated", {"decision_mode": mode}, row[0])
        self.ai_budget = AIBudget(self.db)
        self._archive_supervisor_wallets()

    def _archive_supervisor_wallets(self):
        """Stop legacy supervisor trading while preserving assets and audit history."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            tick = int(self._meta("tick", "0"))
            for agent in self.db.execute("SELECT * FROM agents WHERE role='supervisor'").fetchall():
                cash = Decimal(self.broker.wallet(agent["id"])["cash"])
                treasury = Decimal(self.db.execute("SELECT treasury FROM environments WHERE id=?",
                                                   (agent["environment"],)).fetchone()[0])
                self.db.execute("UPDATE environments SET treasury=? WHERE id=?",
                                (str(treasury + cash), agent["environment"]))
                self.db.execute("UPDATE wallets SET cash='0' WHERE agent=?", (agent["id"],))
                self.broker.set_wallet_status(agent["id"], "dead")
                self.db.execute("UPDATE agents SET role='legacy_supervisor',status='dead',"
                                "death_reason='supervisor_wallet_removed' WHERE id=?", (agent["id"],))
                self._event(tick, "supervisor_wallet_archived", {"recycled_cash": str(cash),
                            "positions": "Frozen historical assets; excluded from trading and breeding"},
                            agent["id"], agent["environment"])
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.broker.close()

    def set_decision_mode(self, agent, mode):
        if mode not in ("rules", "hybrid"):
            raise ValueError("Mode must be rules or hybrid")
        self.initialize()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT role,status FROM agents WHERE id=?", (agent,)).fetchone()
            if row is None or row["role"] != "trader" or row["status"] != "active":
                raise ValueError("Mode change requires an active trader wallet")
            self.db.execute("UPDATE agents SET decision_mode=? WHERE id=?", (mode, agent))
            self._event(int(self._meta("tick", "0")), "agent_mode_changed", {"decision_mode": mode}, agent)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return {"agent": agent, "decision_mode": mode}

    def _meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM ecosystem_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set_meta(self, key, value):
        self.db.execute("INSERT INTO ecosystem_meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def _config_matches(self):
        saved = json.loads(self._meta("config"))
        saved["supervisor"].pop("cash", None)
        # Inference controls can change without resetting wallets or usage ledgers.
        # Financial, survival and evolution settings remain immutable for a store.
        return {k: v for k, v in saved.items() if k not in ("ai", "workers")} == {
            k: v for k, v in self.config.items() if k not in ("ai", "workers")}

    def _event(self, tick, kind, detail, agent=None, environment=None):
        self.db.execute("INSERT INTO ecosystem_events(tick, kind, agent, environment, detail) VALUES (?, ?, ?, ?, ?)",
                        (tick, kind, agent, environment, encoded(detail)))

    def initialize(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self._meta("config")
            if existing is not None:
                if not self._config_matches():
                    raise ValueError("This database has a different saved config; use a separate --db for new settings")
            else:
                self._set_meta("config", encoded(self.config))
                self._set_meta("reserve", self.config["supervisor"]["experiment_reserve"])
                self._set_meta("tick", 0)
                budget = self.config["population"]["main_budget"]
                self.db.execute("INSERT INTO environments VALUES (?, ?, ?, ?, ?)", ("main", budget, budget, 0, "Initial strategy population"))
                genomes = default_genomes()
                for i in range(self.config["population"]["initial_population"]):
                    self._spawn("main", genomes[i % len(genomes)], 0, market=self.config["system"]["markets"][i % len(self.config["system"]["markets"])] )
                self._event(0, "initialized", {"config": self.config})
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return self.status()

    def _spawn(self, environment, genome, tick, parents=(), role="trader", amount=None, market=None, decision_mode=None):
        if role != "trader":
            raise ValueError("Only trading agents can own wallets")
        genome = validate_genome(genome)
        amount = number(amount or self.config["population"]["agent_cash"])
        env = self.db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone()
        if env is None:
            raise ValueError("Unknown environment")
        count = self.db.execute("SELECT COUNT(*) FROM agents WHERE environment=? AND role='trader' AND status='active'", (environment,)).fetchone()[0]
        modes = self.config["workers"]["initial_modes"]
        decision_mode = decision_mode or modes[count % len(modes)]
        if decision_mode not in ("rules", "hybrid"):
            raise ValueError("Invalid wallet agent decision mode")
        if role == "trader" and count >= self.config["population"]["max_population"]:
            return None
        treasury = Decimal(env["treasury"])
        if treasury < amount:
            self._event(tick, "birth_unfunded", {"required": str(amount), "treasury": str(treasury)}, environment=environment)
            return None
        serial = int(self._meta("next_agent_id", "1"))
        agent = f"{environment}:{role}:{serial:06d}"
        self._set_meta("next_agent_id", serial + 1)
        market = market or self.config["system"]["markets"][count % len(self.config["system"]["markets"])]
        generation = 0
        for parent in parents:
            row = self.db.execute("SELECT generation FROM agents WHERE id=?", (parent,)).fetchone()
            if row:
                generation = max(generation, row[0] + 1)
        self.broker.create_wallet(agent, amount)
        self.broker.set_wallet_status(agent, "active")
        self.db.execute("UPDATE environments SET treasury=? WHERE id=?", (str(treasury - amount), environment))
        self.db.execute("INSERT INTO agents(id,environment,role,market,genome,parents,generation,born_tick,peak_equity) VALUES(?,?,?,?,?,?,?,?,?)",
                        (agent, environment, role, market, encoded(genome), encoded(list(parents)), generation, tick, str(amount)))
        self.db.execute("UPDATE agents SET decision_mode=? WHERE id=?", (decision_mode, agent))
        self._event(tick, "agent_born", {"genome": genome, "parents": list(parents), "generation": generation,
                    "capital": str(amount), "role": role, "decision_mode": decision_mode}, agent, environment)
        return agent

    def _check_frame(self, frame):
        timestamp = frame["timestamp_ms"]
        if type(timestamp) is not int or timestamp <= 0:
            raise ValueError("Frame timestamp must be a positive integer")
        now = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
        if frame["source"] not in ("live", "demo"):
            raise ValueError("Invalid source")
        existing_source = self._meta("source")
        if existing_source and existing_source != frame["source"]:
            raise ValueError("Live and demo sources must use separate databases")
        parts = []
        for market in self.config["system"]["markets"]:
            context = frame["contexts"][market]
            chart = context["chart"]
            book = frame["books"][market]
            if context["market"] != market or chart["market"] != market or book["market"] != market or chart["unit_minutes"] != self.config["system"]["unit"]:
                raise ValueError("Frame market/unit mismatch")
            number(chart["latest_close"])
            if not chart["candles"]:
                raise ValueError("Frame has no closed candles")
            latest = utc_time(chart["candles"][-1]["time"])
            if latest.timestamp() + chart["unit_minutes"] * 60 > now.timestamp():
                raise ValueError("Frame contains unfinished/future candle")
            actual_age = now.timestamp() - latest.timestamp() - chart["unit_minutes"] * 60
            if actual_age > chart["unit_minutes"] * 120:
                raise ValueError("Stale chart frame; lifecycle clock will not advance")
            # Frame milliseconds truncate submillisecond feed timestamps.
            if any((utc_time(value) - now).total_seconds() > 0.001 for value in (chart["as_of"], context["created_at"])):
                raise ValueError("Future context timestamp")
            if abs(float(chart["latest_close"]) - float(chart["candles"][-1]["close"])) > abs(float(chart["latest_close"])) * 1e-12:
                raise ValueError("Latest close mismatches signal candle")
            age = timestamp - int(number(book["timestamp"]))
            if age < -5000 or age > 30000:
                raise ValueError("Stale or future orderbook in frame")
            levels = book["orderbook_units"]
            if not levels:
                raise ValueError("Frame has empty orderbook")
            for level in levels:
                for key in ("ask_price", "bid_price"):
                    number(level[key])
                for key in ("ask_size", "bid_size"):
                    number(level[key], positive=False)
            if min(number(u["ask_price"]) for u in levels) < max(number(u["bid_price"]) for u in levels):
                raise ValueError("Crossed orderbook")
            parts.append(f"{market}:{latest.isoformat()}")
        return hashlib.sha256((frame["source"] + "|" + "|".join(parts)).encode()).hexdigest()

    def _equity(self, agent, frame):
        wallet = self.broker.wallet(agent)
        equity = Decimal(wallet["cash"])
        for pos in wallet["positions"]:
            quantity = Decimal(pos["quantity"])
            if quantity:
                price = max(number(u["bid_price"]) for u in frame["books"][pos["market"]]["orderbook_units"])
                equity += quantity * price * (1 - self.broker.fee_rate)
        return equity

    def _observe(self, agent, tick, frame):
        equity = self._equity(agent["id"], frame)
        wallet = self.broker.wallet(agent["id"])
        initial = Decimal(wallet["initial_cash"])
        peak = max(equity, Decimal(agent["peak_equity"]))
        drawdown = float((peak - equity) / peak)
        max_dd = max(drawdown, agent["max_drawdown"])
        ret = float(equity / initial - 1)
        price = number(frame["contexts"][agent["market"]]["chart"]["latest_close"])
        baseline = Decimal(agent["benchmark_price"]) if agent["benchmark_price"] else price
        benchmark = float(price / baseline - 1)
        # Same-age per-bar excess performance allows cross-market cohort comparison.
        score = (ret - benchmark - 0.5 * max_dd) / max(agent["age"], 1)
        self.db.execute("UPDATE agents SET peak_equity=?, max_drawdown=?, last_return=?, score=?, benchmark_price=? WHERE id=?",
                        (str(peak), max_dd, ret, score, str(baseline), agent["id"]))
        self.db.execute("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (tick, agent["id"], str(equity), ret, drawdown, benchmark, score))
        return {"equity": str(equity), "return": ret, "drawdown": drawdown, "max_drawdown": max_dd,
                "benchmark_return": benchmark, "excess_return": ret - benchmark, "score": score}

    def _death_reason(self, agent, metrics):
        rules = self.config["survival"]
        age = agent["age"]
        if metrics["max_drawdown"] >= rules["max_drawdown"]:
            return "maximum_drawdown"
        if metrics["return"] <= rules["min_equity_ratio"] - 1:
            return "capital_floor"
        trades = self.db.execute("SELECT COUNT(*) FROM trades WHERE agent=?", (agent["id"],)).fetchone()[0]
        if age >= rules["inactivity_ticks"] and trades < rules["min_trades"]:
            return "inactivity"
        if age >= rules["grace_ticks"] and age % rules["evaluation_every"] == 0:
            if metrics["return"] < rules["min_return"]:
                return "minimum_return"
            if metrics["excess_return"] < rules["min_excess_return"]:
                return "benchmark_underperformance"
        return None

    def _retire(self, agent, reason, tick, frame):
        fills, unliquidated = [], []
        for position in self.broker.wallet(agent["id"])["positions"]:
            qty = Decimal(position["quantity"])
            if not qty:
                continue
            try:
                fills.append(self.broker.execute(agent["id"], position["market"], "sell", qty,
                    frame["books"][position["market"]], now_ms=frame["timestamp_ms"],
                    order_key=f"death:{tick}:{agent['id']}:{position['market']}"))
            except ValueError as exc:
                unliquidated.append({"position": position, "reason": str(exc)})
        terminal = self._equity(agent["id"], frame)
        wallet = self.broker.wallet(agent["id"])
        cash = Decimal(wallet["cash"])
        treasury = Decimal(self.db.execute("SELECT treasury FROM environments WHERE id=?", (agent["environment"],)).fetchone()[0])
        self.db.execute("UPDATE environments SET treasury=? WHERE id=?", (str(treasury + cash), agent["environment"]))
        self.db.execute("UPDATE wallets SET cash='0' WHERE agent=?", (agent["id"],))
        self.db.execute("UPDATE agents SET status='dead', terminal_equity=?, death_reason=? WHERE id=?", (str(terminal), reason, agent["id"]))
        self.broker.set_wallet_status(agent["id"], "dead")
        self._event(tick, "agent_died", {"reason": reason, "terminal_equity": str(terminal), "recycled_cash": str(cash),
                    "liquidations": fills, "frozen_positions": unliquidated}, agent["id"], agent["environment"])

    def _trade(self, agent, decision, tick, frame):
        action, reason = decision["action"], decision["reason"]
        trade_id, outcome = None, "hold"
        if action != "hold":
            wallet = self.broker.wallet(agent["id"])
            if action == "buy":
                cash = Decimal(wallet["cash"])
                amount = min(cash, self._equity(agent["id"], frame) * number(decision["target_fraction"]))
            else:
                amount = sum((Decimal(p["quantity"]) for p in wallet["positions"] if p["market"] == agent["market"]), Decimal(0))
            try:
                fill = self.broker.execute(agent["id"], agent["market"], action, amount, frame["books"][agent["market"]],
                    now_ms=frame["timestamp_ms"], order_key=f"strategy:{tick}:{agent['id']}")
                trade_id, outcome = fill["id"], "filled"
                self._event(tick, "trade", {"fill": fill, "reason": reason}, agent["id"], agent["environment"])
            except ValueError as exc:
                outcome = "rejected"
                reason += f"; {exc}"
        self.db.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)", (tick, agent["id"], action, reason, trade_id, outcome))

    def _parents(self, environment):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM agents WHERE environment=? AND role='trader' AND age>=? ORDER BY (status='active') DESC, score DESC, born_tick ASC, id LIMIT 2",
            (environment, self.config["evolution"]["min_parent_age"]))]

    def _child(self, environment, tick, rng, mode="evolution"):
        parents = self._parents(environment)
        if not parents:
            genome = default_genomes()[rng.randrange(len(default_genomes()))]
            parent_ids = []
        else:
            genome = breed(json.loads(parents[0]["genome"]), json.loads(parents[-1]["genome"]), rng,
                           self.config["evolution"]["mutation_rate"])
            parent_ids = list(dict.fromkeys(p["id"] for p in parents))
        agent = self._spawn(environment, genome, tick, parents=parent_ids)
        if agent:
            self._event(tick, mode, {"parents": parent_ids, "child_genome": genome}, agent, environment)
        return agent

    def _evolve(self, tick, rng):
        for env in self.db.execute("SELECT id FROM environments ORDER BY created_tick,id").fetchall():
            alive = self.db.execute("SELECT COUNT(*) FROM agents WHERE environment=? AND role='trader' AND status='active'", (env[0],)).fetchone()[0]
            births = min(self.config["evolution"]["max_births_per_tick"], self.config["population"]["target_population"] - alive)
            # At a generation boundary also add a challenger even if the target survived.
            if tick % self.config["evolution"]["every_ticks"] == 0 and alive >= self.config["population"]["target_population"]:
                births = 1
            for _ in range(max(0, births)):
                self._child(env[0], tick, rng)

    def _supervise(self, tick, frame, rng):
        settings = self.config["supervisor"]
        traders = [dict(r) for r in self.db.execute("SELECT id,environment,status,age,last_return,max_drawdown,score FROM agents WHERE role='trader'")]
        if tick % settings["every_ticks"] == 0:
            alive = [r for r in traders if r["status"] == "active"]
            eligible = [r for r in alive if r["age"] >= self.config["evolution"]["min_parent_age"]]
            winner = max(eligible, key=lambda r: r["score"]) if eligible else None
            self._event(tick, "supervisor_observation", {"agents": traders, "best_agent": winner["id"] if winner else None,
                        "active": len(alive), "dead": len(traders) - len(alive),
                        "decisions": [dict(r) for r in self.db.execute("SELECT * FROM decisions WHERE tick=?", (tick,))],
                        "environments": self.db.execute("SELECT COUNT(*) FROM environments").fetchone()[0]})
        if tick % settings["spawn_every_ticks"] == 0:
            # Independent exploration: choose a fresh archetype, rather than crossover.
            environments = [dict(r) for r in self.db.execute("SELECT * FROM environments")]
            environment = max(environments, key=lambda r: Decimal(r["treasury"]))
            active_kinds = {json.loads(r[0])["kind"] for r in self.db.execute(
                "SELECT genome FROM agents WHERE environment=? AND status='active' AND role='trader'", (environment["id"],))}
            candidates = [g for g in default_genomes() if g["kind"] not in active_kinds] or default_genomes()
            genome = dict(candidates[rng.randrange(len(candidates))])
            genome["allocation"] = 0.1  # Smaller independent probe allocation.
            born = self._spawn(environment["id"], genome, tick)
            if born:
                self._event(tick, "supervisor_spawn", {"reason": "Explore missing archetype or conservative probe", "genome": genome}, born, environment["id"])
        if tick % settings["environment_every_ticks"] == 0:
            count = self.db.execute("SELECT COUNT(*) FROM environments").fetchone()[0]
            reserve, budget = Decimal(self._meta("reserve")), Decimal(settings["sandbox_budget"])
            if count < settings["max_environments"] and reserve >= budget:
                env_id = f"sandbox-{count}"
                self._set_meta("reserve", reserve - budget)
                self.db.execute("INSERT INTO environments VALUES (?, ?, ?, ?, ?)",
                    (env_id, str(budget), str(budget), tick, "Supervisor-created isolated conservative experiment"))
                genomes = default_genomes()
                born = []
                for i in range(min(self.config["population"]["initial_population"], int(budget / Decimal(self.config["population"]["agent_cash"])))):
                    genome = {**genomes[i % len(genomes)], "allocation": 0.1, "stop_loss": 0.03}
                    born.append(self._spawn(env_id, genome, tick))
                self._event(tick, "supervisor_environment", {"budget": str(budget), "reserve_remaining": str(reserve-budget),
                            "agents": born, "reason": "Bounded isolated strategy experiment"}, environment=env_id)

    def _prepare_plan(self, frame):
        """Slow inference runs without a SQLite write lock; apply only to the same state."""
        try:
            key = self._check_frame(frame)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise InvalidFrameError(str(exc)) from exc
        if self.db.execute("SELECT 1 FROM frames WHERE frame_key=?", (key,)).fetchone():
            return None
        tick = int(self._meta("tick", "0"))
        last = self.db.execute("SELECT timestamp_ms FROM frames ORDER BY tick DESC LIMIT 1").fetchone()
        if last and frame["timestamp_ms"] <= last[0]:
            raise InvalidFrameError("Frames must advance in time")
        if self.ai_backend != "codex" or (tick != 0 and (tick + 1) % self.config["ai"]["every_ticks"]):
            return None
        snapshot = self.status()
        snapshot.update(next_tick=tick + 1, limits=self.config,
                        market_context={m: compact_context(c) for m, c in frame["contexts"].items()},
                        recent_events=[{k: e[k] for k in ("tick", "kind", "agent", "environment")} for e in self.events(20)],
                        policies={r[0]: json.loads(r[1]) for r in self.db.execute("SELECT agent,policy FROM agent_policies")})
        return self.ai_budget.invoke("supervisor", self.config["ai"], key, tick, self.planner,
                                     lambda: validate_plan(self.planner.plan(snapshot)))

    def _base_decision(self, agent, frame):
        genome = json.loads(agent["genome"])
        context = frame["contexts"][agent["market"]]
        wallet = self.broker.wallet(agent["id"])
        baseline = decide(genome, context, wallet)
        policy = self.db.execute("SELECT policy FROM agent_policies WHERE agent=?", (agent["id"],)).fetchone()
        if policy:
            baseline = policy_decision(json.loads(policy[0]), genome, context, wallet, baseline)
        return baseline

    def _prepare_workers(self, frame):
        if self.ai_backend != "codex":
            return None
        key = self._check_frame(frame)
        if self.db.execute("SELECT 1 FROM frames WHERE frame_key=?", (key,)).fetchone():
            return None
        tick = int(self._meta("tick", "0"))
        settings = self.config["workers"]
        cached = self.ai_budget.cached("workers", key, tick)
        if cached is not None:
            return cached
        candidates = []
        rows = self.db.execute('''SELECT agents.*, COALESCE(worker_review_clock.last_tick,-1000000) AS review_tick
            FROM agents LEFT JOIN worker_review_clock ON agents.id=worker_review_clock.agent
            WHERE role='trader' AND status='active' AND decision_mode='hybrid'
            ORDER BY review_tick, born_tick, id''').fetchall()
        for row in rows:
            agent = dict(row)
            context = frame["contexts"][agent["market"]]
            if agent["last_bar"] == context["chart"]["candles"][-1]["time"] or tick + 1 - agent["review_tick"] < settings["every_ticks"]:
                continue
            baseline = self._base_decision(agent, frame)
            if protected_decision(baseline):
                continue
            candidates.append({"agent": agent["id"], "genome": json.loads(agent["genome"]),
                               "wallet": self.broker.wallet(agent["id"]), "baseline": baseline,
                               "market_context": compact_context(context),
                               "performance": {k: agent[k] for k in ("age", "last_return", "max_drawdown")}})
            if len(candidates) >= settings["max_agents_per_call"]:
                break
        if not candidates:
            return None
        snapshot = {"tick": tick + 1, "candidates": candidates}
        return self.ai_budget.invoke("workers", settings, key, tick, self.worker,
                                     lambda: validate_reviews(self.worker.review(snapshot), candidates),
                                     agents=[c["agent"] for c in candidates])

    def _apply_plan(self, prepared, tick):
        if prepared is None:
            return
        if prepared["base_tick"] != tick - 1:
            self._event(tick, "supervisor_ai_error", {"error": "State advanced during inference; plan discarded"})
            return
        if "skipped" in prepared:
            self._event(tick, "supervisor_ai_skipped", prepared)
            return
        if "error" in prepared:
            self._event(tick, "supervisor_ai_error", {"error": prepared["error"], "behavior": "No supervisor actions"})
            return
        plan = prepared["plan"]
        self._event(tick, "supervisor_ai_plan", plan)
        births = 0
        for index, action in enumerate(plan["actions"]):
            self.db.execute("SAVEPOINT ai_action")
            try:
                kind = action["kind"]
                if kind != "revise_strategy" and births >= self.config["evolution"]["max_births_per_tick"]:
                    raise ValueError("AI birth limit reached")
                market = action["market"]
                if kind != "revise_strategy" and market not in self.config["system"]["markets"]:
                    raise ValueError("Action market is not configured")
                if kind == "create_environment":
                    environment = action["environment"]
                    if not environment or len(environment) > 80 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in environment):
                        raise ValueError("Invalid environment name")
                    settings = self.config["supervisor"]
                    budget = number(action["budget"])
                    reserve = Decimal(self._meta("reserve"))
                    count = self.db.execute("SELECT COUNT(*) FROM environments").fetchone()[0]
                    if count >= settings["max_environments"] or budget > Decimal(settings["sandbox_budget"]) or budget > reserve:
                        raise ValueError("Environment count/budget/reserve limit reached")
                    if budget < Decimal(self.config["population"]["agent_cash"]):
                        raise ValueError("Environment must fund at least one agent")
                    self.db.execute("INSERT INTO environments VALUES (?,?,?,?,?)",
                                    (environment, str(budget), str(budget), tick, action["reason"]))
                    self._set_meta("reserve", reserve - budget)
                elif kind == "spawn_agent":
                    environment = action["environment"]
                else:
                    row = self.db.execute("SELECT * FROM agents WHERE id=? AND role='trader' AND status='active'", (action["agent"],)).fetchone()
                    if row is None:
                        raise ValueError("Strategy target is not active")
                    agent, environment = row["id"], row["environment"]
                    mode = action["decision_mode"] or row["decision_mode"]
                    self.db.execute("UPDATE agents SET genome=? WHERE id=?", (encoded(action["genome"]), agent))
                    self.db.execute("UPDATE agents SET decision_mode=? WHERE id=?", (mode, agent))
                if kind != "revise_strategy":
                    agent = self._spawn(environment, action["genome"], tick, market=market, decision_mode=action["decision_mode"])
                    if agent is None:
                        raise ValueError("Agent unfunded or population limit reached")
                if action["policy"] is None:
                    self.db.execute("DELETE FROM agent_policies WHERE agent=?", (agent,))
                else:
                    self.db.execute("INSERT INTO agent_policies VALUES (?,?,?) ON CONFLICT(agent) DO UPDATE SET policy=excluded.policy,updated_tick=excluded.updated_tick",
                                    (agent, encoded(action["policy"]), tick))
                self._event(tick, "supervisor_ai_action", {"index": index, **action}, agent, environment)
                if kind == "create_environment":
                    self._event(tick, "supervisor_environment", {"budget": str(budget), "agents": [agent],
                                "reason": action["reason"], "provider": "codex"}, environment=environment)
                self.db.execute("RELEASE ai_action")
                if kind != "revise_strategy":
                    births += 1
            except (ValueError, TypeError, sqlite3.IntegrityError) as exc:
                self.db.execute("ROLLBACK TO ai_action")
                self.db.execute("RELEASE ai_action")
                self._event(tick, "supervisor_ai_rejected", {"index": index, "action": action, "error": str(exc)})

    def tick(self, frame):
        if self._meta("config") is None:
            self.initialize()
        if not self._config_matches():
            raise ValueError("This database has a different saved config; use a separate --db for new settings")
        prepared = self._prepare_plan(frame)
        worker_prepared = self._prepare_workers(frame)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if not self._config_matches():
                raise ValueError("This database has a different saved config; use a separate --db for new settings")
            try:
                frame_key = self._check_frame(frame)
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                raise InvalidFrameError(str(exc)) from exc
            duplicate = self.db.execute("SELECT tick FROM frames WHERE frame_key=?", (frame_key,)).fetchone()
            if duplicate:
                self.db.execute("COMMIT")
                return {"status": "duplicate", "tick": duplicate[0], "reason": "Closed candles already processed"}
            last = self.db.execute("SELECT timestamp_ms FROM frames ORDER BY tick DESC LIMIT 1").fetchone()
            if last and frame["timestamp_ms"] <= last[0]:
                raise InvalidFrameError("Frames must advance in time")
            tick = int(self._meta("tick", "0")) + 1
            self._set_meta("source", frame["source"])
            self.db.execute("INSERT INTO frames VALUES (?, ?, ?, ?, ?)", (tick, frame_key, frame["source"], frame["timestamp_ms"], encoded(frame)))
            reviews = {}
            if worker_prepared is not None:
                if worker_prepared["base_tick"] != tick - 1:
                    self._event(tick, "worker_ai_error", {"error": "State advanced during worker inference"})
                elif "plan" in worker_prepared:
                    reviews = {d["agent"]: d for d in worker_prepared["plan"]["decisions"]}
                    self._event(tick, "worker_ai_review", worker_prepared["plan"])
                else:
                    self._event(tick, "worker_ai_error" if "error" in worker_prepared else "worker_ai_skipped", worker_prepared)
            rng = random.Random(self.config["system"]["seed"] + tick)
            agents = [dict(r) for r in self.db.execute("SELECT * FROM agents WHERE role='trader' AND status='active' ORDER BY id")]
            for agent in agents:
                signal_bar = frame["contexts"][agent["market"]]["chart"]["candles"][-1]["time"]
                if agent["last_bar"] == signal_bar:
                    continue
                agent["age"] += 1
                self.db.execute("UPDATE agents SET age=?,last_bar=? WHERE id=?", (agent["age"], signal_bar, agent["id"]))
                # Pre-trade assessment: do not let another entry rescue a failing agent.
                metrics = self._observe(agent, tick, frame)
                reason = self._death_reason(agent, metrics)
                if reason:
                    self._retire(agent, reason, tick, frame)
                    continue
                context = frame["contexts"][agent["market"]]
                genome = json.loads(agent["genome"])
                decision = self._base_decision(agent, frame)
                if agent["decision_mode"] == "hybrid" and agent["id"] in reviews:
                    decision = combine_review(reviews[agent["id"]], decision, genome, context, self.broker.wallet(agent["id"]))
                self._trade(agent, decision, tick, frame)
                # Replace the observation with post-fill liquidation-value performance.
                self.db.execute("DELETE FROM observations WHERE tick=? AND agent=?", (tick, agent["id"]))
                updated = dict(self.db.execute("SELECT * FROM agents WHERE id=?", (agent["id"],)).fetchone())
                metrics = self._observe(updated, tick, frame)
                reason = self._death_reason(updated, metrics)
                if reason:
                    self._retire(updated, reason, tick, frame)
            self._evolve(tick, rng)
            if self.ai_backend == "codex":
                self._apply_plan(prepared, tick)
            else:
                self._supervise(tick, frame, rng)
            self._set_meta("tick", tick)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return {"status": "processed", "tick": tick, "source": frame["source"],
                "active": self.db.execute("SELECT COUNT(*) FROM agents WHERE role='trader' AND status='active'").fetchone()[0],
                "events": [{**dict(row), "detail": json.loads(row["detail"])} for row in self.db.execute(
                    "SELECT * FROM ecosystem_events WHERE tick=? ORDER BY id", (tick,))]}

    def status(self):
        agents, archived_accounts = [], []
        for row in self.db.execute("SELECT * FROM agents ORDER BY born_tick,id"):
            agent = dict(row)
            agent["genome"], agent["parents"] = json.loads(agent["genome"]), json.loads(agent["parents"])
            agent["wallet"] = self.broker.wallet(agent["id"])
            agent["trade_count"] = self.db.execute("SELECT COUNT(*) FROM trades WHERE agent=?", (agent["id"],)).fetchone()[0]
            policy = self.db.execute("SELECT policy FROM agent_policies WHERE agent=?", (agent["id"],)).fetchone()
            agent["policy"] = json.loads(policy[0]) if policy else None
            (agents if agent["role"] == "trader" else archived_accounts).append(agent)
        capital = self.capital_accounting()
        return {"tick": int(self._meta("tick", "0")), "source": self._meta("source"), "ai_backend": self.ai_backend,
                "ai_usage": {scope: self.ai_budget.status(scope, settings) for scope, settings in
                             (("supervisor", self.config["ai"]), ("workers", self.config["workers"]))},
                "experiment_reserve": self._meta("reserve", "0"),
                "capital_accounting": capital,
                "environments": [dict(r) for r in self.db.execute("SELECT * FROM environments ORDER BY created_tick,id")],
                "supervisor": {"id": "supervisor", "role": "supervisor", "backend": self.ai_backend,
                               "responsibilities": ["manage_trading_agents", "improve_strategies", "manage_environments"]},
                "archived_accounts": archived_accounts, "agents": agents}

    def capital_accounting(self):
        """Transfers/births cannot mint capital: cash + cost = initial + realized.

        Unrealized prices are deliberately excluded from this bookkeeping identity.
        Frozen dead-agent assets remain counted. Manual unmanaged wallets excluded.
        """
        if self._meta("config") is None:
            return None
        saved_config = json.loads(self._meta("config"))
        initial = Decimal(saved_config["population"]["main_budget"]) + Decimal(saved_config["supervisor"]["experiment_reserve"])
        current = Decimal(self._meta("reserve", "0"))
        current += sum((Decimal(r[0]) for r in self.db.execute("SELECT treasury FROM environments")), Decimal(0))
        current += sum((Decimal(r[0]) for r in self.db.execute("SELECT cash FROM wallets JOIN agents ON wallets.agent=agents.id")), Decimal(0))
        current += sum((Decimal(r[0]) for r in self.db.execute("SELECT cost FROM positions JOIN agents ON positions.agent=agents.id")), Decimal(0))
        realized = sum((Decimal(r[0]) for r in self.db.execute("SELECT realized_pnl FROM trades JOIN agents ON trades.agent=agents.id")), Decimal(0))
        expected = initial + realized
        return {"initial_virtual_capital": str(initial), "cash_plus_position_cost": str(current),
                "realized_pnl": str(realized), "expected_cash_plus_cost": str(expected),
                "identity_error": str(current - expected)}

    def events(self, limit=30):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("Event limit must be 1..10000")
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in self.db.execute(
            "SELECT * FROM ecosystem_events ORDER BY id DESC LIMIT ?", (limit,))]

    def record_source_error(self, error):
        self._event(int(self._meta("tick", "0")), "source_error", {"error": str(error), "collected_at": datetime.now(timezone.utc).isoformat()})
