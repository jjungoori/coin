"""Agent input bundle: mandatory chart features, opt-in external evidence."""
import json
from datetime import datetime, timezone
from pathlib import Path

from .analysis import analyze
from .bithumb import BithumbPublicClient, validate_market
from .research import collect, import_evidence


def build_context(market, unit=5, count=200, external=False, config="research.toml", evidence=None, client=None):
    validate_market(market)
    client = client or BithumbPublicClient()
    candles = client.candles(market, unit, count)
    now = datetime.now(timezone.utc)
    chart = analyze(candles, market, unit, now)
    research = collect(config, now) if external else {"items": [], "providers": [], "status": "not_requested"}
    if evidence:
        research["items"].extend(import_evidence(evidence, now))
    return {"schema_version": 1, "market": market, "created_at": now.isoformat(),
            "chart": chart, "external": research,
            "purpose": "Strategy input for paper trading; does not generate or execute orders"}


def save_context(bundle, directory="data/research"):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{bundle['market']}-{stamp}.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path.resolve())
