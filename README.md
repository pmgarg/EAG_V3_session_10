# Session 10 — Computer-Use Agent

A five-layer Computer-Use skill that drops into the Session 9 catalogue and
solves three real macOS desktop tasks. Built on top of `cua-driver` 0.5.3,
with **no paid APIs and no third-party agentic frameworks** — all LLM and
vision calls go through a local Ollama gateway (`gemma4:12b` for
planning/judgment, `qwen2.5vl:7b` for vision).

---

## Assignment requirement coverage

| Requirement | Where it lives | Evidence |
| --- | --- | --- |
| **≥ 1 task uses vision (Layer 3)** | Task C (`tasks/task_c_cursor.py::_force_vision_verify`) | `docs/sample_outputs/taskC-chrome-cdp-vision/turn-00002/` screenshot + verdict `click_xy` |
| **≥ 1 task uses the Electron `page` path** with `electron_debugging_port` | Task C (`tasks/task_c_cursor.py` + `layers/cdp_client.py`) | `plan.app.electron=True, electron_debugging_port=9222`; trajectory shows CDP `navigate` + `execute_javascript` |
| **≥ 1 task completes with zero vision calls** | Task A (`tasks/task_a_calculator.py`) | Trace shows only Layer 2a `press_key` / `click_button` turns; `extracted.via = "clipboard"` |
| **`start_recording` on every run** | `computer_skill.run()` toggles `record=True` by default | Trajectory directories under `trajectories/` per run |
| **Single application runs on Linux / Windows / macOS** | `platform_/__init__.py` picks `_macos.py` / `_linux.py` / `_windows.py` at import | macOS impl exercised live; Linux/Windows shims compile and expose the same interface |
| **Graceful fallback when one method fails** | `layers/recovery.py` cascade (2a → 2b → 3) + AX-tree retry-with-activate in `_resolve_button` | Task A's earlier menu-bar-only AX scan regression now recovers within 3 attempts |
| **Permission probe + interactive grant** | `permissions.py::ensure_permissions` | Calls `tccutil`-equivalent System Settings panes; polls and restarts the daemon when grants flip |
| **Ollama gemma 12b** | `gateway.py` `DEFAULT_PLANNER_MODEL = DEFAULT_JUDGE_MODEL = "gemma4:12b"` | Planner JSON traces in trajectories |
| **No paid APIs, no agentic frameworks** | All LLM calls via `gateway.chat / gateway.vision` against `http://localhost:11434` | `grep -r "openai\|anthropic\|langchain" agent10/` returns nothing |

---

## Architecture (5 layers)

```
                    ┌────────────────────────────────┐
   user goal ──▶    │  Planner   (gemma4:12b)        │
                    └────────────────────────────────┘
                                  │ ordered subgoals
                                  ▼
                    ┌────────────────────────────────┐
                    │  Perception  (AX tree filter)  │
                    └────────────────────────────────┘
                                  │ shortlist
                                  ▼
                    ┌────────────────────────────────┐
                    │  Executor: scan → act → verify │
                    │                                │
                    │  Layer 2a  hotkeys (no LLM)    │ ◀── Task A
                    │  Layer 2b  AX + gemma4 judge   │ ◀── Task B
                    │  Layer 2-electron CDP/page tool│ ◀── Task C
                    │  Layer 3   screenshot + vision │ ◀── Task C verify
                    └────────────────────────────────┘
                                  │ TurnRecord per attempt
                                  ▼
                    ┌────────────────────────────────┐
                    │  Recovery (classify failures)  │
                    │  retry / escalate / blocked    │
                    └────────────────────────────────┘
```

The cascade is **strict**: each layer's failure mode maps to one of
`{retry, escalate, blocked}`. The executor never skips a cheaper layer
just because the next one is more powerful — that's the cost discipline
the assignment asks for.

---

## Three tasks

### A — Calculator (zero vision, zero LLM at runtime)

`python -m agent10.cli a "(47*53)+(101*7)"`

- Parses the arithmetic expression with an **AST-based safe evaluator**
  (`tasks/task_a_calculator.py::_safe_arith_eval`) — replaces unsafe
  `eval()`.
- Emits a deterministic Layer 2a plan: `escape` → digit presses → operator
  button clicks (via AX `element_index` lookup) → `=`.
- Reads the result via **clipboard fallback** (`Cmd+C` → `pbpaste`) since
  macOS 26 Calculator's display is custom-rendered and not in the AX tree.
- One full LLM call (the planner). Zero vision calls. Zero judge calls.

### B — Notes (Layer 2b — AX tree + AppleScript verify)

`python -m agent10.cli b`

- Creates a new note titled `Session 10 demo <YYYY-MM-DD>`, types 3 bullets.
- Verification reads the note **two ways** for redundancy: AX tree grep
  for the title, and AppleScript `body of note` for the bullets (macOS 26
  Notes hides the body from the AX tree).
- Demonstrates the planner → AX tree → judge → AppleScript fallback flow.

### C — Chrome via Chrome DevTools Protocol + Vision verify

`python -m agent10.cli c`

- Pre-launches Chrome with `--remote-debugging-port=9222` (cua-driver's
  own `launch_app` does not pass the CDP flag to Chrome).
- Navigates to `google.com`, sets `<textarea name="q">` value, submits the
  form — all via a **direct CDP WebSocket client** in `layers/cdp_client.py`
  because `cua-driver`'s built-in `page` tool falls back to AppleScript and
  times out on this host.
- **Forces vision** on the final "results visible" check: captures the
  Chrome window, asks `qwen2.5vl:7b` "is a vertical list of search results
  visible below the search bar?", expects `done` or `click_xy` verdict.

---

## Running the agent

### Prerequisites

```bash
# 1. Install cua-driver (Rust daemon, JSON over Unix socket)
brew install trycua/tap/cua-driver        # macOS
# OR cargo install --git https://github.com/trycua/cua-driver

# 2. Pull the Ollama models
ollama pull gemma4:12b
ollama pull qwen2.5vl:7b
ollama serve &

# 3. Install Python deps
pip install httpx pyyaml
```

### macOS permissions (TCC)

On first run, `permissions.ensure_permissions()` opens the relevant
System Settings panes and polls until you grant:
- **Accessibility** (for AX tree reads + key injection)
- **Screen Recording** (for Layer 3 vision captures)

The daemon (`com.trycua.driver`) is restarted automatically every 30s
while polling so it picks up new grants without manual intervention.

### Run one task

```bash
cd /path/to/Session_10
python -m agent10.cli a            # Calculator
python -m agent10.cli b            # Notes
python -m agent10.cli c            # Chrome
python -m agent10.cli all          # All three back-to-back
```

Each run writes a trajectory to `trajectories/<timestamp>-<label>/`
suitable for `cua-driver replay_trajectory`.

---

## Sample outputs

Trajectory snapshots from a clean run are checked into
[docs/sample_outputs/](docs/sample_outputs/):

- [taskA-calculator/](docs/sample_outputs/taskA-calculator/) — 12 turns, 1.7s wall clock, extracted `3198`
- [taskB-notes/](docs/sample_outputs/taskB-notes/) — 4 turns, AppleScript body verify
- [taskC-chrome-cdp-vision/](docs/sample_outputs/taskC-chrome-cdp-vision/) — CDP navigate + JS submit + vision verdict

Each directory contains:
- `cursor.jsonl` — cua-driver's per-step recording (mouse path, keypresses)
- `session.json` — run metadata (pid, window_id, plan)
- `turn-NNNNN/` — one folder per action (`pre.png`, `post.png`, `event.json`)

---

## Notable engineering decisions

| Decision | Why |
| --- | --- |
| **Custom CDP WebSocket client** instead of `cua-driver`'s `page` tool | cua-driver 0.5.3's `page` falls back to AppleScript on this host and times out at 15s; the direct CDP path is ~5ms per call |
| **AST-based arithmetic evaluator** instead of `eval()` | Security: the expression comes from user input |
| **Activate-then-rescan retry** in `_resolve_button` | macOS sometimes returns just the menu-bar AX tree when the target app isn't strictly frontmost; we re-activate and rescan up to 3 times |
| **Clipboard read for Calculator result** | macOS 26 Calculator's display element is custom-rendered (not AX-visible) |
| **AppleScript `body of note`** as a verify path for Notes | macOS 26 Notes hides the body from the AX tree |
| **`page.execute_javascript` to drive Google search** | Sidesteps focus, IME, and rate-limit issues that plague the typed-keystroke path |
| **`open -a -g` then `osascript activate`** for app launch | `osascript activate` alone races AppleEvent timeouts (-1728); `open -a -g` is non-blocking and AppleEvent-free |

---

## Layout

```
Session_10/
├── README.md                      ← this file
├── agent10/                       ← the skill
│   ├── agent_config.yaml          ← catalogue entry (Session 9 contract)
│   ├── cli.py                     ← `python -m agent10.cli {a|b|c|all}`
│   ├── computer_skill.py          ← orchestrator (TaskHooks + run())
│   ├── driver.py                  ← cua-driver JSON wrapper
│   ├── gateway.py                 ← Ollama chat + vision
│   ├── permissions.py             ← TCC probe + System Settings opener
│   ├── platform_/                 ← OS dispatch (macOS / Linux / Windows)
│   ├── prompts/                   ← planner.md, judge.md, vision.md
│   ├── layers/                    ← 5-layer cascade
│   │   ├── planner.py
│   │   ├── perception.py
│   │   ├── executor.py
│   │   ├── recovery.py
│   │   ├── vision.py
│   │   └── cdp_client.py
│   └── tasks/                     ← three concrete tasks
│       ├── task_a_calculator.py
│       ├── task_b_notes.py
│       └── task_c_cursor.py
├── docs/
│   └── sample_outputs/            ← committed trajectory snapshots
├── trajectories/                  ← runtime output (gitignored)
└── reference_file/                ← assignment writeup + cua-driver guide
```

---

## Demo

A YouTube walkthrough of the three tasks (with the trajectory replay) is
linked at the top of the submission form. The demo intentionally shows
the **failure-and-recovery** path on Task A (the AX-tree menu-bar-only
case), so the cascade behavior is visible — not just the happy path.
