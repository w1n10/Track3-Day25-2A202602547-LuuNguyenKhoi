"""Experiments for the report (sections 2, 3 and 8). Writes reports/experiments.json.

    python scripts/run_experiments.py            # all experiments, seed 25
    python scripts/run_experiments.py --seed 26

Every experiment overrides a few settings of configs/gameday.yaml in memory —
the config file itself is never modified.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without pip install

import argparse
import dataclasses
import json
from typing import Any

from gameday_lab.client import ResilientClient
from gameday_lab.factory import build_client
from gameday_lab.sim.config import load_config
from gameday_lab.sim.engine import evaluate_checks, run_scenario

GAMEDAY_KEYS = ["availability", "latency_p95_s", "amplification", "retry_ratio", "cost_per_good_turn",
                "recovery_s", "primary_share_after_restore", "incompatible_routes", "give_up_reasons"]
CACHE_KEYS = ["availability", "faq_availability", "cache_hit_ratio", "stale_served", "wrong_answers",
              "wrong_answers_by_reason", "outdated_answers", "cost_per_good_turn"]

NO_BUDGET = {"retry_budget_ratio": 100.0, "retry_budget_max_tokens": 1.0e9, "retry_budget_min_per_s": 1.0e6}


def _run(cfg: Any, scenario: str, client: str, seed: int, keys: list[str]) -> dict[str, Any]:
    m = run_scenario(cfg, scenario, build_client(client, cfg), seed).metrics()
    out = {k: m.get(k) for k in keys}
    out["pass"] = all(evaluate_checks(m, cfg.scenarios[scenario].checks).values())
    return out


def gameday(seed: int, scenarios: list[str], client_overrides: dict[str, Any],
            breaker_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = load_config("configs/gameday.yaml", "configs/scenarios.yaml")
    client = {**cfg.client, **client_overrides}
    client["breaker"] = {**cfg.client.get("breaker", {}), **(breaker_overrides or {})}
    cfg = dataclasses.replace(cfg, client=client)
    return {s: _run(cfg, s, "resilient", seed, GAMEDAY_KEYS) for s in scenarios}


def cache(seed: int, overrides: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config("configs/gameday.yaml", "configs/scenarios_cache.yaml")
    cfg = dataclasses.replace(cfg, cache={**cfg.cache, **overrides})
    return _run(cfg, "C1_faq_outage", "cached", seed, CACHE_KEYS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=25)
    ap.add_argument("--out", default="reports/experiments.json")
    args = ap.parse_args()
    seed = args.seed
    res: dict[str, Any] = {"seed": seed}

    # Section 2: tuning sweeps for the client block
    res["timeout_sweep"] = {t: gameday(seed, ["G2_overload_storm", "G4_slow_region", "G6_provider_outage"],
                                       {"attempt_timeout_s": t}) for t in (4.0, 6.0, 8.0, 12.0)}
    res["slow_call_sweep"] = {s: gameday(seed, ["G2_overload_storm", "G4_slow_region"], {}, {"slow_call_s": s})
                              for s in (3.0, 5.0, 8.0)}
    res["open_duration_sweep"] = {d: gameday(seed, ["G2_overload_storm", "G6_provider_outage"], {},
                                             {"open_duration_s": d}) for d in (5.0, 10.0, 30.0)}
    res["max_attempts_sweep"] = {n: gameday(seed, ["G3_brownout"], {"max_attempts_per_provider": n})
                                 for n in (1, 2, 3)}

    # Section 3.1: retry budget as the last line of defence (breaker misconfigured, 5 attempts/provider)
    broken = {"min_calls": 1_000_000_000}
    res["exp1_budget"] = {
        "broken_breaker_with_budget": gameday(seed, ["G2_overload_storm", "G3_brownout"],
                                              {"max_attempts_per_provider": 5}, broken),
        "broken_breaker_no_budget": gameday(seed, ["G2_overload_storm", "G3_brownout"],
                                            {"max_attempts_per_provider": 5, **NO_BUDGET}, broken),
        "working_breaker_with_budget": gameday(seed, ["G2_overload_storm", "G3_brownout"],
                                               {"max_attempts_per_provider": 5}),
        "working_breaker_no_budget": gameday(seed, ["G2_overload_storm", "G3_brownout"],
                                             {"max_attempts_per_provider": 5, **NO_BUDGET}),
    }

    # Section 3.2: a 30 s attempt timeout
    res["exp2_timeout30"] = {"8s": gameday(seed, ["G4_slow_region", "G6_provider_outage"], {}),
                             "30s": gameday(seed, ["G4_slow_region", "G6_provider_outage"],
                                            {"attempt_timeout_s": 30.0})}

    # Section 3.3: ladder() without the tool-calling filter
    original = ResilientClient.ladder
    ResilientClient.ladder = lambda self, task: list(self.order)  # type: ignore[method-assign]
    try:
        res["exp3_no_tool_filter"] = gameday(seed, ["G6_provider_outage"], {})
    finally:
        ResilientClient.ladder = original  # type: ignore[method-assign]

    # Section 8: cache experiments on C1
    res["cache_threshold"] = {t: cache(seed, {"similarity_threshold": t}) for t in (0.80, 0.92, 0.97)}
    res["cache_ttl"] = {t: cache(seed, {"ttl_s": t}) for t in (30.0, 60.0, 300.0)}
    res["cache_stale"] = {s: cache(seed, {"max_stale_s": s}) for s in (0.0, 300.0)}
    res["cache_starter"] = cache(seed, {"similarity_threshold": 0.80, "ttl_s": 600.0, "max_stale_s": 0.0,
                                        "max_entries": 500})

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
