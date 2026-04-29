---

**Proposal: Upgrade XLI to Support Different Models for Orchestrator vs Workers**

We want to evolve the current XLI codebase to better support a high-performance, cost-efficient multi-agent swarm using Grok models.

### Goal
- Main agent (orchestrator) should use a stronger reasoning model (e.g. `grok-4` or `grok-4-1-fast-reasoning`)
- Worker agents (via `dispatch_subagent`) should use a cheaper, faster model (e.g. `grok-4-1-fast-non-reasoning`)
- Keep the change **minimal**, clean, and fully backward compatible
- Do **not** add temperature or other sampling parameters yet

### New Config Structure (Recommended)

Update the config so it supports this (while keeping old `model` field for backward compatibility):

```json
{
  "model": "grok-4-1-fast-reasoning",           // fallback / legacy
  "orchestrator_model": "grok-4-1-fast-reasoning",
  "worker_model": "grok-4-1-fast-non-reasoning",
  
  "management_api_key": "xai-...",
  "keys": [
    {"api_key": "...", "label": "primary"},
    {"api_key": "...", "label": "w1"},
    ...
  ],
  
  "max_tool_iterations": 20,
  "max_worker_iterations": 12,
  "max_parallel_workers": 6
}
```

### What Needs to Change

1. **`config.py`**
   - Add `orchestrator_model` and `worker_model` fields to `GlobalConfig` class
   - Add logic so that if `orchestrator_model` or `worker_model` are not set, fall back to the old `model` field
   - Update `load()` and template accordingly

2. **`pool.py`**
   - Modify `ClientPool` so it can create clients with different models
   - Possibly add methods like:
     - `get_orchestrator_client()`
     - `acquire_worker_client()`

3. **`agent.py`**
   - In `Agent` class: use `orchestrator_model` when creating chat completions
   - In `WorkerAgent` class: use `worker_model` for its completions
   - Update `TurnStats` or logging to clearly show which model was used

4. **`client.py`**
   - Possibly extend `Clients` class to accept a specific `model` at creation time (instead of always pulling from global config)

5. **Other small updates**
   - Update `cmd_status` in `cli.py` to show both orchestrator and worker models
   - Update any places in `agent.py` or `tools.py` that hardcode model usage (if any)
   - Keep all existing behavior identical when the new fields are not present in config

### Priorities for Implementation

1. **Backward compatibility is mandatory** — existing configs must continue to work exactly as before.
2. Keep changes as localized and clean as possible.
3. Make the model selection logic clear and easy to follow.
4. Add helpful status output so the user can verify which models are being used.
5. Do not add temperature, top_p, or other sampling params in this round.

### Success Criteria
- When I run `xli status`, it clearly shows orchestrator model and worker model.
- In `xli chat`, the main agent uses the orchestrator model.
- When I use `/dispatch_subagent` (or multiple in one turn), the workers use the cheaper worker model.
- Old single-`model` configs still work unchanged.

---
