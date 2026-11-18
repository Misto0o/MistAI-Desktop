# ============================================================
# PLANNER AND ROUTER MODULE
# ============================================================
#
# The two steps before the agent: the conversation router (chat or task,
# one small model call that fails closed to the agent) and the planner
# (simple goals run directly; others become a small DAG of phases).
#
# SECTION INDEX
#   1. PHASE STATES AND PLANNING LIMITS
#   2. PLAN NODE
#   3. PLAN GRAPH
#   4. PLANNER AND ROUTER MODEL CALL
#   5. PLANNING PROMPT AND SIMPLE-GOAL CUES
#   6. BROWSER TASK GOALS
#   7. QUESTION GOALS
#   8. TRIVIALLY SIMPLE GOALS
#   9. MAKING A PLAN
#   10. MICRO-STEP PATTERNS
#   11. MICRO-STEP PLAN DETECTION
#   12. DEPENDENCY CYCLE CHECK
#   13. PLAN RUNNER
#   14. CONVERSATION ROUTER
#   15. ROUTER PROMPT
#   16. BUILDING THE ROUTER PROMPT
#   17. SINGLE-DELIVERABLE ANSWER
#   18. ROUTE REASON LABELS
#   19. PARSING THE ROUTE
#   20. ROUTER MODEL CHOICE
#   21. CHAT OR TASK DECISION
# ============================================================

"""
core/planner.py — dependency-graph planner + runner (design doc #3),
replacing the linear phase list in AgentLoop._plan_and_run.

The LLM decomposes a goal into a small DAG of phase nodes:

        create_folder
             |
        create_files
        /    |     \\
     html   css    js
        \\    |     /
      final_verification

Execution is still SEQUENTIAL -- there is one keyboard, one mouse, one
screen, so "parallel desktop automation" would be a lie. The graph matters
anyway, for three honest reasons:
- a failed node only blocks its DESCENDANTS; independent branches keep
  running (css failing doesn't kill js)
- progress is per-node and persisted after every transition, so goals are
  crash-safe and resumable ("Mist, resume PortfolioSite")
- each phase's context is scoped to its actual ANCESTORS' results instead
  of "everything that happened so far"

The LLM still makes every in-phase decision (design doc #10); this module
only supplies structure, persistence, and bookkeeping.
"""

import json
import re
import time
from datetime import datetime

import requests

from . import planner, live_state as events
from .config import (
    describe_api_error,
    confirmation_answer,
    log_text,
    single_shot_enabled,
    verbose_logs,
    DEFAULT_MODEL,
    chat_timeout,
)


# ============================================================
# PHASE STATES AND PLANNING LIMITS
# ============================================================

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"

MAX_NODES = 8
PLANNING_TIMEOUT = 30


# ── Plan data model ──────────────────────────────────────────────────────────


# ============================================================
# PLAN NODE
# ============================================================

class PlanNode:
    def __init__(self, node_id, desc, depends_on=None, status=PENDING, result=""):
        self.id = node_id
        self.desc = desc
        self.depends_on = list(depends_on or [])
        self.status = status
        self.result = result

    def to_dict(self):
        return {
            "id": self.id,
            "desc": self.desc,
            "depends_on": self.depends_on,
            "status": self.status,
            "result": self.result,
        }

    @staticmethod
    def from_dict(d):
        return PlanNode(
            d.get("id", ""),
            d.get("desc", ""),
            d.get("depends_on", []),
            d.get("status", PENDING),
            d.get("result", ""),
        )


# ============================================================
# PLAN GRAPH
# ============================================================

class Plan:
    def __init__(self, nodes):
        self.order = [n.id for n in nodes]
        self.nodes = {n.id: n for n in nodes}

    # -- Queries -------------------------------------------------------------

    def ready_nodes(self):
        """Pending nodes whose dependencies are ALL done."""
        out = []
        for nid in self.order:
            n = self.nodes[nid]
            if n.status == PENDING and all(
                d in self.nodes and self.nodes[d].status == DONE
                for d in n.depends_on
            ):
                out.append(n)
        return out

    def ancestors(self, node_id):
        """Transitive dependency closure, in plan order."""
        seen, stack = set(), list(self.nodes[node_id].depends_on)
        while stack:
            d = stack.pop()
            if d in seen or d not in self.nodes:
                continue
            seen.add(d)
            stack.extend(self.nodes[d].depends_on)
        return [self.nodes[nid] for nid in self.order if nid in seen]

    def count(self, status):
        return sum(1 for n in self.nodes.values() if n.status == status)

    def progress(self):
        return f"{self.count(DONE)}/{len(self.nodes)}"

    # -- Mutations -----------------------------------------------------------

    def mark(self, node_id, status, result=None):
        n = self.nodes.get(node_id)
        if not n:
            return
        n.status = status
        if result is not None:
            n.result = (result or "")[:300]

    def block_descendants(self, failed_id):
        """Everything downstream of a failed node becomes blocked; anything
        independent stays runnable. This is the DAG's actual payoff."""
        blocked = []
        changed = True
        while changed:
            changed = False
            for n in self.nodes.values():
                if n.status != PENDING:
                    continue
                if any(
                    self.nodes.get(d) and self.nodes[d].status in (FAILED, BLOCKED)
                    for d in n.depends_on
                ):
                    n.status = BLOCKED
                    blocked.append(n.id)
                    changed = True
        return blocked

    # -- Serialization (for TaskStore) ---------------------------------------

    def to_dict(self):
        return {"nodes": [self.nodes[nid].to_dict() for nid in self.order]}

    @staticmethod
    def from_dict(d):
        return Plan([PlanNode.from_dict(nd) for nd in (d or {}).get("nodes", [])])


# ── Planning (LLM call + validation) ─────────────────────────────────────────


# ============================================================
# PLANNER AND ROUTER MODEL CALL
# ============================================================

def _llm(api, prompt, model, timeout=PLANNING_TIMEOUT, label="Planner", fallback=True):
    headers = api.api_manager.get_headers()
    urls = tuple(
        dict.fromkeys(
            u
            for u in (
                api.api_manager.get_api_url(),
                api.api_manager.get_fallback_url() if fallback else None,
            )
            if u
        )
    )
    for url in urls:
        try:
            r = requests.post(
                url,
                json={"message": prompt, "model": model},
                headers=headers,
                timeout=timeout,
            )
            if r.ok:
                return r.json().get("response", "")
            # Logged only: planning and routing fail over to their safe paths
            # (one phase / the agent), which then report the same error.
            print(f"[{label}] {describe_api_error(r, model)}")
        except Exception as e:
            # The URL and message only under verbose logs (the message can quote the URL).
            print(f"[{label}] LLM call failed: "
                  f"{(f'via {url}: {e}') if verbose_logs() else type(e).__name__}")
    return ""


# ============================================================
# PLANNING PROMPT AND SIMPLE-GOAL CUES
# ============================================================

_PLAN_PROMPT = """You are decomposing a desktop-automation GOAL into PHASES.

GOAL: "{goal}"

WORKSPACE CONTEXT (ground truth; existing files must be preserved and migrated, not recreated as a blank template):
{workspace}

A phase is a DELIVERABLE -- something that exists in the world when it is done (a file written, an app configured, a form submitted), NEVER a UI micro-step. The agent running each phase finds, focuses, clicks and verifies on its own; splitting those across phases makes it repeat actions (on a toggle button, that UNDOES them).

Reply ONLY with JSON, no other text:
{{"task_name": "ShortName", "nodes": [{{"id": "snake_case_id", "desc": "self-contained phase instruction", "depends_on": ["ids of phases whose OUTPUT this phase needs"]}}]}}

Rules:
- DEFAULT TO ONE NODE; use 2-{max_nodes} only for genuinely separate deliverables (e.g. three different files, or "set up the project" + "deploy it").
- NEVER create nodes like "locate the button", "verify the button is enabled", "move the cursor", "click it", "check if X exists", "confirm it worked", "open the editor": those are steps INSIDE a phase. BAD (4 nodes): locate_button -> verify_button -> move_to_button -> click_button. GOOD (1 node): subscribe_to_channel: "Subscribe to the channel on the open video". No verification-only nodes: each phase verifies its own work.
- Don't split steps that use the same tool and can't fail independently: creating a folder and a file inside it is ONE phase.
- depends_on means "cannot start until that phase's OUTPUT exists"; do NOT chain independent phases (style.css does not depend on script.js -- both depend only on the folder).
- Each desc names its OWN deliverable in one sentence and must be executable given just its dependencies' results.
- Plan ONLY what remains. Things the goal or WORKSPACE says are already done are context, not phases.
- Deliver only what was asked, and NEVER specify an output format the user didn't ask for: no "return true/false", "output a list", "write a report", "save to a file". A QUESTION's deliverable is the spoken answer, so its desc just says what to find out. When the goal DOES ask for an output ("...then write the names in Notepad"), that output is a deliverable and must have a phase."""


# Goals this short and simple never need decomposition; skipping the planning
# call also saves a round-trip on the most common commands.
_SIMPLE_VERBS = (
    "open",
    "close",
    "click",
    "type",
    "press",
    "mute",
    "unmute",
    "play",
    "pause",
    "search",
    "switch",
    "minimize",
    "maximize",
    "scroll",
    "lock",
    "screenshot",
    "subscribe",
    "like",
    "navigate",
    "go to",
    "what",
    "who",
    "when",
    "where",
    "how much",
    "tell me",
    "read",
    "check",
    "import",
)

_BROWSER_TASK_CUES = re.compile(
    r"\b(go to|navigate to|open)\b.{0,80}\b(and|then)\b.{0,80}"
    r"\b(tell|read|find|get|check|extract|say|report|click|search|fill|type)\b",
    re.I,
)


# ============================================================
# BROWSER TASK GOALS
# ============================================================

def _is_browser_task_goal(goal):
    """'Navigate somewhere AND read/click/tell me something' is one
    continuous browser interaction -- browser_navigate then browser_get_text
    (or browser_click) happen inside a single short sequence. Splitting it
    into separate phases loses that continuity: the second phase has to
    re-derive context from scratch and, in practice, falls back to a WORSE
    tool (read_screen_text/OCR) instead of trusting the page is already
    loaded and just reading it directly."""
    return bool(_BROWSER_TASK_CUES.search(goal or ""))


# ============================================================
# QUESTION GOALS
# ============================================================

# Questions are answered inside one agent phase; planning them only adds a
# model round-trip (and invites invented deliverables like "return true/false").
_QUESTION_RX = re.compile(
    r"^(is|are|what|whats|which|how many|does|do|can|where)\b", re.I
)


def _is_question_goal(goal):
    """'is Edge open?', 'how many files are in Downloads' -- a single question
    with no "then"/"and" joining further steps."""
    g = goal.strip().lower()
    return bool(_QUESTION_RX.match(g)) and not re.search(r"\b(then|and)\b", g)


# ============================================================
# TRIVIALLY SIMPLE GOALS
# ============================================================

def _is_trivially_simple(goal):
    """One short instruction with no conjunction joining separate deliverables."""
    g = goal.strip().lower()
    if len(g.split()) > 14:
        return False
    if re.search(r"\b(then|after that|and then)\b", g):
        return False
    # "and" joining two verbs implies two deliverables; "and" inside a noun
    # phrase ("milk and eggs") does not.
    if (
        re.search(r"\band\b", g)
        and len(
            re.findall(
                r"\b(open|write|create|make|build|send|install|delete|set up|deploy)\b",
                g,
            )
        )
        > 1
    ):
        return False
    return g.startswith(_SIMPLE_VERBS)


# ============================================================
# MAKING A PLAN
# ============================================================

def make_plan(api, goal, model):
    """Returns (Plan, task_name). Falls back to a single-node plan on any
    parsing/validation problem -- planning must never be the thing that
    breaks a task."""
    goal = goal.strip().strip("\"\u201c\u201d'\u2018\u2019")
    # MISTAI_SINGLE_SHOT: the router's "one deliverable" hint, consumed here
    # whatever happens, and trusted only for this exact goal.
    hinted = getattr(api, "_single_shot_goal", None)
    if hinted is not None:
        try:
            api._single_shot_goal = None
        except Exception:
            pass
    if (
        len(goal.split()) < 2
        or _is_browser_task_goal(goal)
        or _is_trivially_simple(goal)
        or _is_question_goal(goal)
    ):
        print("[Planner] Simple goal -- running directly, no decomposition")
        return Plan([PlanNode("main", goal)]), ""
    if (single_shot_enabled() and isinstance(hinted, str)
            and hinted.strip().strip("\"\u201c\u201d'\u2018\u2019") == goal):
        print("[Planner] Router: one deliverable -- running directly, no planning call")
        return Plan([PlanNode("main", goal)]), ""

    try:
        workspace = api.workspace.context_block()
    except Exception:
        workspace = "(workspace unavailable)"
    t0 = time.monotonic()
    raw = _llm(
        api,
        _PLAN_PROMPT.format(goal=goal, max_nodes=MAX_NODES, workspace=workspace),
        model,
    )
    plan_secs = time.monotonic() - t0
    print(f"[Timing] Planning LLM: {plan_secs:.2f}s")
    agent = getattr(api, "agent", None)
    if agent is not None and hasattr(agent, "record_time"):
        agent.record_time("plan", plan_secs)
    name = ""
    nodes = []
    try:
        cleaned = raw.strip()
        for fence in ("```json", "```"):
            if fence in cleaned:
                cleaned = cleaned.split(fence)[1].split("```")[0]
        data = json.loads(cleaned.strip())

        if isinstance(data, dict) and isinstance(data.get("nodes"), list):
            name = str(data.get("task_name", ""))[:48]
            raw_nodes = data["nodes"]
        elif isinstance(data, list):  # legacy: ordered list of strings -> chain
            raw_nodes = [
                {
                    "id": f"phase_{i+1}",
                    "desc": str(s),
                    "depends_on": ([f"phase_{i}"] if i else []),
                }
                for i, s in enumerate(data)
            ]
        else:
            raw_nodes = []

        seen = set()
        aliases = {}
        for nd in raw_nodes[:MAX_NODES]:
            if not isinstance(nd, dict):
                raise ValueError("plan node was not an object")
            raw_id = re.sub(r"[^a-z0-9_]", "", str(nd.get("id", "")).lower())
            nid = raw_id or f"n{len(seen)+1}"
            while nid in seen:
                nid += "x"
            seen.add(nid)
            # IDs in model output often vary only by case or punctuation.
            # Preserve a stable alias so `depends_on: ["SETUP"]` still
            # references an emitted `setup` node. A duplicate raw ID remains
            # ambiguous, so its first occurrence is the only legal alias.
            if raw_id:
                aliases.setdefault(raw_id, nid)
            aliases.setdefault(nid, nid)
            raw_dependencies = nd.get("depends_on", [])
            if raw_dependencies is None:
                raw_dependencies = []
            elif isinstance(raw_dependencies, str):
                raw_dependencies = [raw_dependencies]
            elif not isinstance(raw_dependencies, list):
                raise ValueError("depends_on was not a list")
            nodes.append(
                PlanNode(
                    nid,
                    str(nd.get("desc", "")).strip()[:400],
                    [str(d) for d in raw_dependencies],
                )
            )
        nodes = [n for n in nodes if n.desc]
        ids = {n.id for n in nodes}
        invalid_dependency = False
        for n in nodes:
            resolved = []
            for dependency in n.depends_on:
                key = re.sub(r"[^a-z0-9_]", "", dependency.lower())
                dep_id = aliases.get(key)
                if not dep_id or dep_id not in ids:
                    print(  # dependency names come from the model: content
                        f"[Planner] Unknown dependency "
                        f"{repr(dependency) + ' on ' + repr(n.id) + ' ' if verbose_logs() else ''}"
                        f"-- discarding malformed plan"
                    )
                    invalid_dependency = True
                    break
                resolved.append(dep_id)
            if invalid_dependency:
                break
            n.depends_on = list(dict.fromkeys(resolved))
        if invalid_dependency:
            nodes = []
        elif _has_cycle(nodes):
            print("[Planner] Cycle in plan -- falling back to linear chain")
            for i, n in enumerate(nodes):
                n.depends_on = [nodes[i - 1].id] if i else []
    except Exception as e:
        print(f"[Planner] Plan parse failed ({e if verbose_logs() else type(e).__name__}) "
              "-- single-phase fallback")
        nodes = []

    if not nodes:
        nodes = [PlanNode("main", goal)]
    elif len(nodes) > 1 and _is_micro_step_plan(nodes):
        # The model ignored the rules and split one action into UI steps.
        # Running those as separate phases repeats the action N times, which
        # on a toggle (Subscribe, mute, checkbox) silently undoes it.
        print(
            f"[Planner] Plan looked like UI micro-steps "
            f"({', '.join(n.id for n in nodes)}) -- collapsing to one phase"
        )
        nodes = [PlanNode("main", goal)]
    print(f"[Planner] Plan: {len(nodes)} node(s)" + (f" for {log_text(name)}" if name else ""))
    for n in nodes:
        deps = f" <- {n.depends_on}" if n.depends_on else ""
        # The description is made from the user's goal: content (verbose only).
        print(f"    {n.id}: {log_text(n.desc[:60])}{deps}")
    return Plan(nodes), name


# ============================================================
# MICRO-STEP PATTERNS
# ============================================================

# Phase descriptions that are really UI steps, not deliverables. If most of a
# plan looks like this, the decomposition was wrong.
_MICRO_STEP_PATTERNS = (
    r"\block|locat|identif|find\b",
    r"\bverif|confirm|check (if|whether|that)\b",
    r"\bmove (the )?(mouse|cursor)\b",
    r"\bperform|execute (a )?click\b",
    r"\bhover\b",
    r"\bwait for\b",
    r"\bensure .* (exists|is (open|visible|enabled))\b",
    r"\bfocus (the )?(window|element|button)\b",
    r"\bscroll (to|into)\b",
    r"\binitiate (the |a )?call\b",
    r"\bend (the )?call\b",
    r"\bhang ?up\b",
    r"\bdeliver the greeting\b",
    r"\bmake the call\b",
)


# ============================================================
# MICRO-STEP PLAN DETECTION
# ============================================================

def _is_micro_step_plan(nodes):
    micro = 0
    for n in nodes:
        d = n.desc.lower()
        if any(re.search(p, d) for p in _MICRO_STEP_PATTERNS):
            micro += 1
    # A single verification tail on a real multi-deliverable plan is fine;
    # a majority of micro-steps means the whole decomposition is bogus.
    return micro >= max(2, len(nodes) // 2 + 1)


# ============================================================
# DEPENDENCY CYCLE CHECK
# ============================================================

def _has_cycle(nodes):
    ids = {n.id for n in nodes}
    indeg = {n.id: len([d for d in n.depends_on if d in ids]) for n in nodes}
    ready = [nid for nid, d in indeg.items() if d == 0]
    seen = 0
    while ready:
        cur = ready.pop()
        seen += 1
        for n in nodes:
            if cur in n.depends_on:
                indeg[n.id] -= 1
                if indeg[n.id] == 0:
                    ready.append(n.id)
    return seen != len(nodes)


# ── Runner ───────────────────────────────────────────────────────────────────


# ============================================================
# PLAN RUNNER
# ============================================================

class PlanRunner:
    """Drives a Plan through AgentLoop._loop one ready node at a time,
    persisting the TaskRecord after every transition. Single-node plans
    bypass all of this and behave exactly like the old direct path."""

    def __init__(self, api, agent):
        self.api = api
        self.agent = agent

    # -- Entry points --------------------------------------------------------

    def run_goal(self, goal, model, gender, input_mode, on_step):
        plan, name = make_plan(self.api, goal, model)
        if len(plan.nodes) <= 1:
            # Exact parity with the pre-DAG behavior for simple tasks:
            # no record, no overhead.
            return self.agent._loop(goal, model, gender, input_mode, on_step)
        record = self.api.task_store.create(name or goal[:48], goal, plan.to_dict())
        return self._drive(record, plan, model, gender, input_mode, on_step)

    def resume(self, record, model, gender, input_mode, on_step):
        plan = Plan.from_dict(record.plan)
        if not plan.nodes:
            return {"success": False, "error": "Saved task has no plan"}
        # failed/blocked get another chance; a node caught mid-run when we
        # crashed is redone from scratch (phases are written to be re-runnable).
        for n in plan.nodes.values():
            if n.status in (FAILED, BLOCKED, RUNNING):
                n.status = PENDING
        record.status = "running"
        self.api.task_store.save(record)
        remaining = plan.count(PENDING)
        self.api.speak(
            f"Resuming {record.name}. " f"{remaining} of {len(plan.nodes)} phases left."
        )
        return self._drive(record, plan, model, gender, input_mode, on_step)

    # -- Core drive loop -----------------------------------------------------

    def _drive(self, record, plan, model, gender, input_mode, on_step):
        api = self.api
        api.bus.publish(
            events.TASK_STARTED,
            {"task_id": record.task_id, "name": record.name},
            "PlanRunner",
        )

        created_files = []

        def _collect(ev):
            p = ev.data.get("path", "")
            if p and "\\MistAI\\" not in p and "AppData" not in p:
                created_files.append(p)

        unsub = api.bus.subscribe(events.FILE_CREATED, _collect)
        total_steps = 0
        try:
            while True:
                if api._stop_requested.is_set():
                    return self._pause(record, plan, total_steps)

                ready = plan.ready_nodes()
                if not ready:
                    break
                node = ready[0]

                if api.caption_window and api.captions_enabled:
                    api.caption_window.show_status(
                        f"Phase {plan.count(DONE) + 1}/{len(plan.nodes)}: "
                        f"{node.desc[:50]}",
                        4,
                    )

                plan.mark(node.id, RUNNING)
                self._persist(record, plan)
                phase_started = time.time()

                result = self.agent._loop(
                    self._frame(record.goal, plan, node),
                    model,
                    gender,
                    input_mode,
                    on_step,
                    hint_text=node.desc,
                )
                total_steps += result.get("steps", 0)
                err = result.get("error", "")

                if result.get("success"):
                    evidence = api.workspace.phase_evidence(phase_started)
                    summary = (
                        f"Verified phase actions: {evidence}"
                        if evidence
                        else (result.get("speech") or "completed")[:300]
                    )
                    plan.mark(node.id, DONE, summary)
                    record.outputs[node.id] = summary
                    api.workspace.record_task_output(record.task_id, node.id, summary)
                    self._persist(record, plan)
                    api.bus.publish(
                        events.TASK_PHASE_DONE,
                        {
                            "task_id": record.task_id,
                            "node": node.id,
                            "progress": plan.progress(),
                        },
                        "PlanRunner",
                    )
                    continue

                if "Stopped by user" in err:
                    plan.mark(node.id, PENDING)  # didn't finish -- redo on resume
                    return self._pause(record, plan, total_steps)

                plan.mark(node.id, FAILED, err[:300])
                record.last_error = err[:300]
                blocked = plan.block_descendants(node.id)
                self._persist(record, plan)
                print(f"[PlanRunner] '{node.id}' failed; blocked: {blocked or 'none'}")

                if plan.ready_nodes():
                    q = (
                        f"Phase '{node.desc[:50]}' failed "
                        f"({err[:60] or 'unknown error'}). "
                        f"Continue with the independent phases?"
                    )
                    if not self._confirm(q, input_mode):
                        # stop_current_task() wakes a pending text prompt so
                        # _confirm returns False.  That is materially
                        # different from the user declining to continue: a
                        # stop must preserve the task as resumable rather
                        # than falling through to a misleading partial-failed
                        # result (and speaking a completion summary).
                        if api._stop_requested.is_set():
                            return self._pause(record, plan, total_steps)
                        break
                else:
                    break

            return self._finish(record, plan, total_steps)
        finally:
            unsub()
            record.files_created = sorted(set(record.files_created + created_files))[
                :100
            ]
            api.task_store.save(record)

    # -- Helpers -------------------------------------------------------------

    def _frame(self, goal, plan, node):
        """Phase framing scoped to real ancestors -- less noise than the old
        'context from ALL previous phases' dump."""
        remaining = [
            n.id for n in plan.nodes.values() if n.status == PENDING and n.id != node.id
        ]
        lines = [
            f"YOUR ASSIGNED PHASE: {node.desc}",
            "",
            "Do this phase and NOTHING else. Every other phase of this goal is "
            "assigned to a separate run that will execute on its own -- work "
            "you do outside your phase gets DUPLICATED, which overwrites files "
            "and double-toggles buttons (clicking Subscribe twice unsubscribes). "
            "When your phase's deliverable exists, call 'done' immediately; do "
            "not continue into the rest of the goal, and do not add extra "
            "verification steps beyond what your own phase needs.",
            "",
            f"WIDER GOAL (background only -- NOT your assignment): {goal}",
        ]
        try:
            workspace = self.api.workspace.context_block()
        except Exception:
            workspace = "(workspace unavailable)"
        if workspace and workspace != "(nothing recorded yet)":
            lines.extend(
                [
                    "",
                    "[CURRENT WORKSPACE -- preserve these existing files unless the "
                    "user explicitly requested replacement]",
                    workspace,
                ]
            )
        if remaining:
            lines.append(f"Phases still owned by other runs: {', '.join(remaining)}")
        anc = [a for a in plan.ancestors(node.id) if a.status == DONE]
        if anc:
            lines.append("")
            lines.append(
                "[WHAT YOUR PREREQUISITES PRODUCED -- trust this, do not "
                "re-verify or redo it]"
            )
            for a in anc:
                # Truncated hard: full summaries announced the entire goal was
                # finished ("All done! ...also style.css and script.js"), which
                # made downstream phases skip or repeat their own work.
                result = (a.result or "completed").split("\n")[0][:110]
                lines.append(f"- {a.id}: {result}")
        return "\n".join(lines)

    def _confirm(self, question, input_mode):
        api = self.api
        answer, source = api.ask_confirmation(question, input_mode)   # Yes/No buttons too
        if api._stop_requested.is_set():
            return False
        text = " ".join(
            re.sub(r"[^\w\s']", " ", str(answer or "").lower()).split()
        )
        if source == "voice" and text in {
            "stop",
            "mist stop",
            "hey mist stop",
            "cancel",
            "mist cancel",
            "stop it",
            "abort",
            "halt",
        }:
            # The manual voice listener is holding the microphone while the
            # wake worker is unavailable, so it must honor a stop itself.
            api.stop_current_task()
            return False
        verdict = confirmation_answer(text, extra_yes=("continue",))
        print(f"[Confirm] answer={verdict} ({source})")   # never the words
        return verdict == "yes"                        # unclear: don't continue

    def _persist(self, record, plan):
        record.plan = plan.to_dict()
        self.api.task_store.save(record)

    def _pause(self, record, plan, steps):
        record.status = "paused"
        self._persist(record, plan)
        self.api.bus.publish(
            events.TASK_PAUSED,
            {"task_id": record.task_id, "progress": plan.progress()},
            "PlanRunner",
        )
        msg = (
            f"Stopped -- progress saved at {plan.progress()} phases. "
            f"Say 'resume {record.name}' to continue."
        )
        self.api.speak(msg)
        return {
            "success": False,
            "error": msg,
            "steps": steps,
            "speech": msg,
            "paused": True,
        }

    def _finish(self, record, plan, steps):
        total = len(plan.nodes)
        done, failed, blocked = (
            plan.count(DONE),
            plan.count(FAILED),
            plan.count(BLOCKED),
        )
        success = done == total
        record.status = "completed" if success else "failed" if done == 0 else "partial"
        self._persist(record, plan)
        self.api.bus.publish(
            events.TASK_COMPLETED if success else events.TASK_FAILED,
            {"task_id": record.task_id, "progress": plan.progress()},
            "PlanRunner",
        )
        speech = (
            f"Completed all {total} phases of {record.name}."
            if success
            else f"Finished {done} of {total} phases"
            + (f" -- {failed} failed" if failed else "")
            + (f", {blocked} blocked" if blocked else "")
            + f". Say 'resume {record.name}' to retry the rest."
        )
        self.api.speak(speech)
        error = ""
        if not success:
            error = f"{failed} phase(s) failed, {blocked} blocked, {done} completed"
        return {
            "success": success,
            "speech": speech,
            "steps": steps,
            "phases": total,
            "error": error,
        }


# ============================================================
# CONVERSATION ROUTER
# ============================================================

# core/conversation.py — the conversation layer in front of the agent.
#
# One small model call decides whether a message is pure conversation (answer
# it now: no screenshots, vision, planning or tools) or anything else (hand it
# to the agent unchanged). It fails closed: every error, timeout, or malformed
# or doubtful reply means "task", so the agent path behaves exactly as before.

# ============================================================
# ROUTER PROMPT
# ============================================================

_PROMPT = """You are Mist, a desktop assistant inspired by JARVIS: quick, confident, dry and a little playful, natural when spoken aloud, never robotic, and you don't narrate yourself.{address}

CURRENT DATE/TIME (system clock): {now}
{history}
USER MESSAGE: {message}

Decide how to handle the message. Reply ONLY with JSON:
{{"route": "chat" or "task", "reply": "your spoken answer, empty for task"}}

Use "chat" ONLY for pure conversation: small talk, thanks, jokes, opinions, general knowledge, definitions, or the current date/time given above. Keep chat replies to 1-3 sentences unless the user asks for more. The reply is spoken aloud: plain sentences only, no markdown, lists, emoji or stage directions.
Use "task" for everything else: anything about this computer, its screen, files, apps, or the web; live or changing facts (weather, news, prices, scores, whether an app is open); anything that needs an action; a message mixing chat with a request; an indirect request ("it's too loud"); and whenever you are unsure.
In a chat reply, never claim you did, opened, checked, saw or changed anything on the computer."""


# MISTAI_SINGLE_SHOT only: the extra field and its rule (the default prompt is unchanged).
_REPLY_FIELD = '"reply": "your spoken answer, empty for task"}'
_SINGLE_RULE = ('\nFor "task", "single" is true only when the message asks for ONE deliverable '
                "that one run can finish (one answer, one app action, one file); false for "
                "several deliverables and whenever you are unsure.")


# ============================================================
# BUILDING THE ROUTER PROMPT
# ============================================================

def build_prompt(message, now, history="", title="", single=False):
    """The routing prompt. ``now`` is the system clock string, ``history`` the
    agent's recent-conversation block, ``title`` "sir"/"ma'am" or "".
    ``single`` adds the one-deliverable field (MISTAI_SINGLE_SHOT)."""
    text = _PROMPT.format(
        address=f" Occasionally address the user as {title}, not in every reply."
        if title else "",
        now=now,
        history=history,
        message=json.dumps(str(message)),
    )
    if single:
        text = text.replace(_REPLY_FIELD, _REPLY_FIELD[:-1] + ', "single": true or false}', 1)
        text += _SINGLE_RULE
    return text


# ============================================================
# SINGLE-DELIVERABLE ANSWER
# ============================================================

def parse_single(raw):
    """True only for an explicit {"route": "task", "single": true}."""
    try:
        cleaned = (raw or "").strip()
        for fence in ("```json", "```"):
            if fence in cleaned:
                cleaned = cleaned.split(fence)[1].split("```")[0]
        data = json.loads(cleaned.strip())
    except Exception:
        return False
    return isinstance(data, dict) and data.get("route") == "task" and data.get("single") is True


# ============================================================
# ROUTE REASON LABELS
# ============================================================

def route_reason(raw):
    """Why a reply routes to the agent, as a fixed label ("" = the model said
    task or chat): "no reply" (the call failed or timed out), "unparsable",
    or "no chat reply" (route chat with an empty reply)."""
    if not (raw or "").strip():
        return "no reply"
    try:
        cleaned = raw.strip()
        for fence in ("```json", "```"):
            if fence in cleaned:
                cleaned = cleaned.split(fence)[1].split("```")[0]
        data = json.loads(cleaned.strip())
    except Exception:
        return "unparsable"
    if not isinstance(data, dict):
        return "unparsable"
    if data.get("route") == "chat" and parse_route(raw) is None:
        return "no chat reply"
    return ""


# ============================================================
# PARSING THE ROUTE
# ============================================================

def parse_route(raw):
    """The chat reply, or None when the call says task or can't be trusted."""
    try:
        cleaned = (raw or "").strip()
        for fence in ("```json", "```"):
            if fence in cleaned:
                cleaned = cleaned.split(fence)[1].split("```")[0]
        data = json.loads(cleaned.strip())
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("route") != "chat":
        return None
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return None
    return reply.strip()


# ============================================================
# ROUTER MODEL CHOICE
# ============================================================

def _model_for(api, model):
    """The request's model, else the user's choice (api.current_model()),
    else the default -- bare test objects have no current_model."""
    if model:
        return model
    current = getattr(api, "current_model", None)
    return current() if callable(current) else DEFAULT_MODEL


# ============================================================
# CHAT OR TASK DECISION
# ============================================================

def try_chat(api, message, gender="none", model=None):
    """Answer ``message`` as conversation, or return None to hand it to the
    agent. Never raises. ``model`` is the request's model (ask_mistai)."""
    t0 = time.monotonic()
    timeout = chat_timeout()
    reason = ""
    single = single_shot_enabled()
    try:
        api._single_shot_goal = None    # a hint is only ever for this message
    except Exception:
        pass
    try:
        try:
            history = api.agent._recent_conversation_block(message)
        except Exception:
            history = ""
        title = "sir" if gender == "male" else "ma'am" if gender == "female" else ""
        now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
        raw = planner._llm(
            api,
            build_prompt(message, now, history, title, single=single),
            _model_for(api, model),
            timeout=timeout,
            label="Conversation",
            fallback=False,
        )
        reply = parse_route(raw)
        reason = route_reason(raw)
        if single and reply is None:
            one = parse_single(raw)
            print(f"[Conversation] single deliverable: {'yes' if one else 'no'}")
            if one:
                api._single_shot_goal = str(message)
    except Exception as e:
        print(f"[Conversation] Router failed ({e if verbose_logs() else type(e).__name__}) "
              "-- routing to agent")
        reply, reason = None, "error"
    elapsed = time.monotonic() - t0
    if reason == "no reply" and elapsed >= timeout * 0.95:
        reason = "timeout"
    # A fixed label says why a message went to the agent without a model
    # decision (timeouts cost the whole wait before the agent starts).
    print(
        f"[Conversation] route={'chat' if reply else 'task'} "
        f"in {elapsed:.2f}s" + (f" ({reason})" if reason and not reply else "")
    )
    return reply
