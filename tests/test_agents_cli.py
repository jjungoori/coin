import io
import json
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from coin.agents import main
from coin.ecosystem import EvolutionSystem, InvalidFrameError, load_config
from coin.feeds import DemoFeed


CONFIG = str(Path(__file__).resolve().parents[1] / "agents.toml")


class AgentCLITests(unittest.TestCase):
    def invoke(self, db, *args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(["--db", str(db), "--config", CONFIG, "--ai", "rules", *args])
        return code, [json.loads(line) for line in output.getvalue().splitlines()], error.getvalue()

    def test_init_status_and_demo_resume_without_replaying_saved_frames(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "demo.sqlite3"
            self.assertEqual(self.invoke(db, "init")[0], 0)
            code, output, _ = self.invoke(db, "status")
            self.assertEqual((code, output[0]["tick"]), (0, 0))
            self.assertEqual(self.invoke(db, "demo", "--steps", "3")[0], 0)
            code, output, error = self.invoke(db, "demo", "--steps", "2")
            self.assertEqual(code, 0, error)
            self.assertEqual([row["tick"] for row in output], [4, 5])
            with closing(sqlite3.connect(db)) as connection:
                frames = connection.execute("SELECT payload FROM frames ORDER BY tick").fetchall()
            reference = DemoFeed(load_config(CONFIG), 7)
            self.assertEqual([json.loads(row[0]) for row in frames], [reference.next_frame() for _ in range(5)])
            code, _, error = self.invoke(db, "demo", "--steps", "1", "--seed", "8")
            self.assertEqual(code, 1)
            self.assertIn("seed differs", error)
            self.assertEqual(self.invoke(db, "status")[1][0]["tick"], 5)
            with patch("coin.agents.LiveFeed") as live:
                self.assertEqual(self.invoke(db, "run", "--steps", "1")[0], 1)
                live.assert_not_called()

    def test_live_failure_recorded_then_retry_and_external_override_does_not_mutate_config(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "live.sqlite3"
            frame = DemoFeed(load_config(CONFIG)).next_frame()
            frame["source"] = "live"
            with patch("coin.agents.LiveFeed") as live, patch("coin.agents.sleep_interval"):
                live.return_value.next_frame.side_effect = [RuntimeError("offline"), frame]
                code, output, error = self.invoke(db, "run", "--steps", "2", "--external")
                self.assertEqual(code, 0, error)
                self.assertEqual([row["status"] for row in output], ["source_error", "processed"])
                self.assertTrue(live.call_args.args[0]["system"]["external"])
            with closing(sqlite3.connect(db)) as connection:
                saved = json.loads(connection.execute("SELECT value FROM ecosystem_meta WHERE key='config'").fetchone()[0])
                self.assertFalse(saved["system"]["external"])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM ecosystem_events WHERE kind='source_error'").fetchone()[0], 1)
            self.assertEqual(self.invoke(db, "demo", "--steps", "1")[0], 1)

    def test_tick_engine_failure_returns_nonzero(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "demo.sqlite3"
            with patch("coin.agents.EvolutionSystem.tick", side_effect=ValueError("engine refused")):
                code, _, error = self.invoke(db, "demo", "--steps", "1")
            self.assertEqual(code, 1)
            self.assertIn("engine refused", error)

    def test_live_invalid_frame_logged_and_next_frame_processed(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "live.sqlite3"
            frame = DemoFeed(load_config(CONFIG)).next_frame()
            frame["source"] = "live"
            original_tick = EvolutionSystem.tick
            attempts = [0]
            def flaky_tick(system, observation):
                attempts[0] += 1
                if attempts[0] == 1:
                    raise InvalidFrameError("stale book")
                return original_tick(system, observation)
            with patch("coin.agents.LiveFeed") as live, patch("coin.agents.sleep_interval"), patch("coin.agents.EvolutionSystem.tick", flaky_tick):
                live.return_value.next_frame.return_value = frame
                code, output, error = self.invoke(db, "run", "--steps", "2")
                self.assertEqual(code, 0, error)
                self.assertEqual([row["status"] for row in output], ["source_error", "processed"])
            self.assertEqual(self.invoke(db, "status")[1][0]["tick"], 1)


if __name__ == "__main__":
    unittest.main()
