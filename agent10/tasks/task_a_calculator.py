"""
Task A — Calculator arithmetic via deterministic hotkeys (Layer 2a).

Goal: Compute (47 * 53) + (101 * 7) on the macOS Calculator and read
the display value.

Why this task: it satisfies the assignment's "≥1 task completes with
zero vision calls" constraint AND it's the cleanest demonstration of
the Layer 2a / Layer 1 (AX text read) path. No LLM in the action
loop. Cheapest possible run.

Calculator's button hotkeys are documented in macOS Calculator: numbers
type directly, `*` is multiply, `+` is add, `=` evaluates. The display
value lives in an AXStaticText field; we read it without a click.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from agent10 import computer_skill
from agent10.layers.executor import TurnRecord
from agent10.layers.planner import Subgoal


def _safe_arith_eval(expr: str) -> float | int:
    """Evaluate a parenthesised arithmetic expression containing only
    digits, +, -, *, /, parentheses, and decimal points.

    Uses Python's `ast` module: parse the expression into an AST and walk
    it, allowing ONLY the BinOp / UnaryOp / Constant nodes for the four
    operators. Anything else (Name, Call, Attribute, ...) raises.

    Safer than `eval()` because the AST walk is the gate — even if the
    regex pre-filter ever lets something through, the AST walker rejects
    every node that isn't strictly arithmetic.
    """
    import ast as _ast
    tree = _ast.parse(expr, mode="eval")
    allowed_ops = {_ast.Add, _ast.Sub, _ast.Mult, _ast.Div, _ast.USub,
                   _ast.UAdd, _ast.FloorDiv, _ast.Mod, _ast.Pow}

    def _walk(node):
        if isinstance(node, _ast.Expression):
            return _walk(node.body)
        if isinstance(node, _ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError(f"non-numeric constant: {node.value!r}")
        if isinstance(node, _ast.BinOp) and type(node.op) in allowed_ops:
            left, right = _walk(node.left), _walk(node.right)
            op_map = {
                _ast.Add: lambda a, b: a + b,
                _ast.Sub: lambda a, b: a - b,
                _ast.Mult: lambda a, b: a * b,
                _ast.Div: lambda a, b: a / b,
                _ast.FloorDiv: lambda a, b: a // b,
                _ast.Mod: lambda a, b: a % b,
                _ast.Pow: lambda a, b: a ** b,
            }
            return op_map[type(node.op)](left, right)
        if isinstance(node, _ast.UnaryOp) and type(node.op) in allowed_ops:
            value = _walk(node.operand)
            return -value if isinstance(node.op, _ast.USub) else +value
        raise ValueError(f"disallowed AST node: {type(node).__name__}")

    return _walk(tree)


def _evaluate_to_flat(expr: str) -> str:
    """Reduce a parenthesised expression so the Calculator can evaluate
    it strictly left-to-right.

    macOS Calculator basic mode does NOT respect operator precedence —
    "47*53+101*7" evaluates as ((47*53)+101)*7 = 17962, not 3198. So we
    reduce only the PARENTHESISED sub-expressions ahead of time, leaving
    the top-level + and - operators for Calculator to perform.

    Examples:
        "(47*53)+(101*7)"       → "2491+707"      (Calculator adds these)
        "(2+3)*5"               → "5*5"           (Calculator multiplies)
        "47*53"                 → "47*53"         (no parens — pass through)
        "(47*53)"               → "2491"          (whole thing reduces)

    The safe AST evaluator we use rejects anything that isn't pure
    arithmetic — see _safe_arith_eval.
    """
    expr = (
        expr.replace("×", "*").replace("÷", "/").replace("−", "-")
        .replace(" ", "").strip()
    )
    if not re.fullmatch(r"[0-9+\-*/().]+", expr):
        return expr
    # Recursively replace each (...) with its evaluated value.
    out = expr
    safety = 20
    while "(" in out and safety > 0:
        # Find the deepest single (...) group.
        m = re.search(r"\(([^()]+)\)", out)
        if not m:
            break
        try:
            value = _safe_arith_eval(m.group(1))
        except Exception:
            return expr
        # Preserve int formatting when result is whole.
        value_str = (str(int(value))
                     if isinstance(value, float) and value.is_integer()
                     else str(value))
        out = out[:m.start()] + value_str + out[m.end():]
        safety -= 1
    return out


def _hotkey_plan_for(subgoal: Subgoal) -> list[dict]:
    """Layer 2a hook: drive macOS Calculator deterministically.

    cua-driver's press_key only accepts letters / digits / control keys —
    no symbols. macOS Calculator's AX tree exposes every button as an
    AXButton with a stable label (Multiply / Add / Equals / 0..9 etc.),
    so we click by element_index instead of typing operators.

    Numbers go through press_key (cheap, no scan needed between digits).
    Operators (+, -, *, /, =) go through click(element_index) — which
    requires a scan first to prime the cache. That single scan is the
    cost of using the semantic path.

    For expressions with parentheses or precedence (47*53)+(101*7),
    Calculator basic-mode evaluates strictly LEFT-TO-RIGHT, so we
    pre-flatten using Python's evaluator: 47*53+101*7 → 3198 directly.
    But to actually demonstrate the calculator running, we flatten only
    the parentheses: 2491 + 707 = becomes "2491", "Add", "707", "Equals".
    """
    intent_lc = (subgoal.intent or "").lower()
    if not any(k in intent_lc for k in ("compute", "calculate", "arithmetic")):
        return []
    m = re.search(r"[\d\(\)\+\-\*/\.\s×÷−]{3,}", subgoal.intent)
    if not m:
        return []
    expr = m.group(0).strip()
    flat = _evaluate_to_flat(expr)
    # If flatten returned a single number, just type it (degenerate case
    # — we still demonstrate Calculator).
    steps: list[dict] = []
    # All Clear: press 'escape' (Calculator's keyboard binding for AC).
    steps.append({"kind": "press_key", "key": "escape"})

    # If the expression had no operators or fully reduced to a single
    # number, just type the digits.
    if re.fullmatch(r"-?\d+", flat):
        for ch in flat:
            if ch == "-":
                # Sign change — click the Change Sign button later.
                continue
            steps.append({"kind": "press_key", "key": ch})
        steps.append({"kind": "press_key", "key": "="})
        return steps

    # The flatten left an expression with operators. Walk it left to right
    # and emit (digits) + (operator click) + ... + Equals.
    # Tokenise the flat expression.
    tokens = re.findall(r"\d+|[+\-*/]", flat)
    for tok in tokens:
        if tok.isdigit():
            for ch in tok:
                steps.append({"kind": "press_key", "key": ch})
        else:
            # Operator: emit a special placeholder that the runner
            # converts to a click(element_index) after scanning.
            label = {"+": "Add", "-": "Subtract",
                     "*": "Multiply", "/": "Divide"}[tok]
            steps.append({"kind": "click_button", "button_label": label})
    steps.append({"kind": "click_button", "button_label": "Equals"})
    return steps


_DISPLAY_RE = re.compile(r"AXStaticText[^\n]*?=\s*['\"]([\d,\.\-−]+)['\"]")


def _extract_display(subgoal: Subgoal, state: dict) -> dict[str, Any]:
    """Layer 1: read the display value.

    Two paths:
      1. AX tree: older macOS Calculator exposes the result as an
         AXStaticText. Cheap; works on macOS <= 15.
      2. Clipboard: macOS 26's redesigned Calculator renders the result
         in a custom-drawn region NOT in the AX tree. Workaround:
         press Cmd+C inside Calculator (Edit → Copy), read `pbpaste`.

    We try (1) first; if no number found, fall back to (2). Both paths
    are zero-LLM. The clipboard path needs a Calculator with focus.
    """
    md = state.get("tree_markdown") or ""
    matches = _DISPLAY_RE.findall(md)
    if matches:
        matches.sort(key=lambda s: len(s), reverse=True)
        raw = matches[0].replace(",", "").replace("−", "-")
        try:
            val: Any = int(raw) if "." not in raw else float(raw)
        except ValueError:
            val = raw
        return {"display_value": val, "display_raw": matches[0],
                "via": "ax_static_text"}

    # Fallback path: Cmd+C → pbpaste. This requires the daemon to know
    # the Calculator pid, so we pull it from the state's `pid` field.
    pid = state.get("pid")
    if pid is None:
        return {"error": "display not found in AX tree and no pid for "
                          "clipboard fallback"}
    import subprocess
    from agent10 import driver
    try:
        driver.hotkey(int(pid), ["command", "c"])
        import time as _time
        _time.sleep(0.3)
        clip = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not clip:
            return {"error": "clipboard empty after Cmd+C"}
        raw = clip.replace(",", "").replace("−", "-")
        try:
            val = int(raw) if "." not in raw else float(raw)
        except ValueError:
            val = clip
        return {"display_value": val, "display_raw": clip,
                "via": "clipboard"}
    except Exception as e:
        return {"error": f"clipboard fallback failed: {e}"}


HOOKS = computer_skill.TaskHooks(
    hotkey_plan=_hotkey_plan_for,
    extract=_extract_display,
)


# Convenience entry point so the CLI doesn't have to know about hooks.
def run(expression: str = "(47*53)+(101*7)") -> computer_skill.RunResult:
    goal = (
        f"Open the macOS Calculator app, compute {expression}, "
        "and read the value from the display."
    )
    return computer_skill.run(
        goal,
        label="taskA-calculator",
        task_hooks=HOOKS,
        need_screen_recording=False,
    )
