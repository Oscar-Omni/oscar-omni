#!/usr/bin/env python3
"""Run an Omni AI eval on a PR branch and compare it against a main baseline.

Architecture A (see PR/scoping notes): the PR's YAML is assumed to already live
on an Omni model branch whose name matches the git head branch (created during
the developer's omni-sync session). This script resolves that branch, runs each
configured prompt set against both `main` and the branch, polls both to
completion, then reports per-prompt regressions.

Which prompt sets run is controlled by the PROMPT_SETS list below, not an env
var -- edit that list in your fork to add/remove suites. Everything that's
genuinely per-repo/per-org (credentials, model id) stays in the environment:

  OMNI_BASE_URL    e.g. https://<your-org>.omniapp.co/api     (required)
  OMNI_API_KEY     Organization API key or PAT                (required)
  MODEL_ID         Omni model id to evaluate                  (required)
  BRANCH_NAME      git head branch to evaluate                (required)
  POLL_TIMEOUT     seconds to wait for runs to finish         (default: 900)
  POLL_INTERVAL    seconds between polls                      (default: 10)
  SCORING_GRACE    extra seconds to poll for scores after a
                   run reaches COMPLETE (scoring lands async) (default: 180)
  SUMMARY_PATH     markdown summary output file               (default: omni-eval-summary.md)
  RESULTS_PATH     full JSON results output file               (default: omni-eval-results.json)

Omni allows at most 2 in-progress eval runs org-wide, so prompt sets are run
one at a time (main + branch per set), never all at once.

Exit codes:
  0  eval completed, no regressions
  1  one or more prompts regressed (passed on main, failed on branch) -> gate fails
  2  operational failure (API error, timeout, run cancelled/failed)
  3  no Omni branch found matching BRANCH_NAME  (hard fail per workflow decision)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Prompt sets to evaluate on every run. Add one entry per suite; each runs
# independently against `main` and the branch, and shows up as its own
# section in the PR comment. Label is just for display.
PROMPT_SETS = [
    {"id": "fc7d143b-896f-4787-b355-f5af073198fa", "label": "BasePromptSet"},
]

TERMINAL_RUN_STATES = {"COMPLETE", "CANCELLED"}


class Config:
    def __init__(self):
        # env_or() coalesces empty strings to the default. GitHub Actions injects
        # an empty string (not an absent key) when a `vars.*` reference is unset,
        # which os.environ.get(key, default) would NOT fall back on.
        self.base_url = require_env("OMNI_BASE_URL").rstrip("/")
        self.api_key = require_env("OMNI_API_KEY")
        self.model_id = require_env("MODEL_ID")
        self.branch_name = require_env("BRANCH_NAME")
        self.poll_timeout = int(env_or("POLL_TIMEOUT", "900"))
        self.poll_interval = int(env_or("POLL_INTERVAL", "10"))
        self.scoring_grace = int(env_or("SCORING_GRACE", "180"))
        self.summary_path = os.environ.get("SUMMARY_PATH", "omni-eval-summary.md")
        self.results_path = os.environ.get("RESULTS_PATH", "omni-eval-results.json")


def require_env(name):
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"Missing required environment variable: {name}\n")
        sys.exit(2)
    return val


def env_or(name, default):
    """Like os.environ.get, but treats an empty value as unset. GitHub Actions
    passes `${{ vars.X }}` as an empty string when the variable is undefined."""
    val = os.environ.get(name)
    return val if val else default


def api(cfg, method, path, body=None):
    url = f"{cfg.base_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {cfg.api_key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        sys.stderr.write(f"API {method} {path} -> {e.code}: {detail}\n")
        sys.exit(2)
    except urllib.error.URLError as e:
        sys.stderr.write(f"API {method} {path} failed: {e}\n")
        sys.exit(2)


def resolve_branch_id(cfg):
    """Page through model branches and return the id of the branch whose name
    matches BRANCH_NAME. If several match, prefer the most recently updated."""
    matches = []
    cursor = None
    while True:
        query = {"modelKind": "BRANCH", "pageSize": "50"}
        if cursor:
            query["cursor"] = cursor
        page = api(cfg, "GET", "/v1/models?" + urllib.parse.urlencode(query))
        for rec in page.get("records", []):
            if rec.get("baseModelId") == cfg.model_id and rec.get("name") == cfg.branch_name:
                matches.append(rec)
        info = page.get("pageInfo", {})
        if info.get("hasNextPage") and info.get("nextCursor"):
            cursor = info["nextCursor"]
        else:
            break
    if not matches:
        return None
    matches.sort(key=lambda r: r.get("updatedAt", ""), reverse=True)
    return matches[0]["id"]


def start_run(cfg, prompt_set_id, description, branch_id=None):
    body = {"prompt_set_id": prompt_set_id, "description": description}
    if branch_id:
        body["run_config"] = {"branch_id": branch_id}
    resp = api(cfg, "POST", "/v1/ai/eval/runs", body)
    return resp["run"]["id"]


def result_settled(r):
    """A per-prompt result is done once it has a score or has errored. Scores
    land a few seconds AFTER the run status flips to COMPLETE, so status alone
    is not a safe signal to read scores."""
    return r.get("score") is not None or r.get("error_reason") is not None


def run_fully_scored(run):
    results = run.get("results", [])
    return bool(results) and all(result_settled(r) for r in results)


def poll_both(cfg, main_id, branch_id):
    """Poll two runs concurrently (Omni allows max 2 in-progress runs), and keep
    polling after COMPLETE until every prompt is scored. If scores are still
    missing SCORING_GRACE seconds after a run went terminal, give up on the
    stragglers and report whatever is present rather than hang."""
    deadline = time.time() + cfg.poll_timeout
    runs = {main_id: None, branch_id: None}
    terminal_since = {main_id: None, branch_id: None}

    def settled(rid, now):
        run = runs[rid]
        if run is None or run["status"] not in TERMINAL_RUN_STATES:
            return False
        if run_fully_scored(run):
            return True
        return (now - terminal_since[rid]) >= cfg.scoring_grace

    while True:
        now = time.time()
        for rid in list(runs):
            if not settled(rid, now):
                runs[rid] = api(cfg, "GET", f"/v1/ai/eval/runs/{rid}")["run"]
                if runs[rid]["status"] in TERMINAL_RUN_STATES and terminal_since[rid] is None:
                    terminal_since[rid] = now
        now = time.time()
        if all(settled(rid, now) for rid in runs):
            return runs[main_id], runs[branch_id]
        if now > deadline:
            sys.stderr.write(f"Runs did not finish within {cfg.poll_timeout}s\n")
            sys.exit(2)
        time.sleep(cfg.poll_interval)


def index_by_prompt(run):
    return {r["prompt"]: r for r in run.get("results", [])}


def accuracy(results):
    scored = [r for r in results if r.get("score") is not None]
    if not scored:
        return None, 0, 0
    passed = sum(1 for r in scored if r["score"] == 1)
    return passed / len(scored), passed, len(scored)


def result_cost(r):
    """Total raw LLM cost (USD) for one prompt: model answer + judge scoring.
    Returns None when Omni reported no cost at all."""
    c, s = r.get("cost"), r.get("scoring_cost")
    if c is None and s is None:
        return None
    return (c or 0) + (s or 0)


def run_cost(run):
    return sum(result_cost(r) or 0 for r in run.get("results", []))


def build_comparison(main_run, branch_run):
    main_by = index_by_prompt(main_run)
    branch_by = index_by_prompt(branch_run)
    rows = []
    for prompt in sorted(set(main_by) | set(branch_by)):
        m = main_by.get(prompt, {})
        b = branch_by.get(prompt, {})
        ms, bs = m.get("score"), b.get("score")
        if ms == 1 and bs == 0:
            status = "regressed"
        elif ms == 0 and bs == 1:
            status = "improved"
        elif ms is None or bs is None:
            status = "unscored"
        elif ms == bs:
            status = "unchanged"
        else:
            status = "changed"
        rows.append({
            "prompt": prompt,
            "main_score": ms,
            "branch_score": bs,
            "status": status,
            "branch_error": b.get("error_reason"),
            "branch_job_state": (b.get("agentic_job") or {}).get("state"),
            "branch_conversation_id": (b.get("agentic_job") or {}).get("conversation_id"),
            "branch_cost": b.get("cost"),
            "branch_scoring_cost": b.get("scoring_cost"),
            "main_total_cost": result_cost(m),
            "branch_total_cost": result_cost(b),
            "branch_timing_ms": b.get("timing_ms"),
            "main_query_count": m.get("query_count"),
            "branch_query_count": b.get("query_count"),
        })
    return rows


def score_cell(s):
    if s is None:
        return "—"
    return "✅" if s == 1 else "❌"


def fmt_pct(acc):
    return "n/a" if acc is None else f"{acc * 100:.1f}%"


def render_set_section(set_info, rows, main_run, branch_run):
    m_acc, m_pass, m_n = accuracy(main_run.get("results", []))
    b_acc, b_pass, b_n = accuracy(branch_run.get("results", []))
    delta = None if (m_acc is None or b_acc is None) else (b_acc - m_acc) * 100
    regressions = [r for r in rows if r["status"] == "regressed"]
    improvements = [r for r in rows if r["status"] == "improved"]

    m_cost, b_cost = run_cost(main_run), run_cost(branch_run)
    cost_delta = b_cost - m_cost
    cost_pct = f" ({cost_delta / m_cost * 100:+.0f}%)" if m_cost else ""

    lines = [
        f"### {set_info['label']}",
        "",
        f"Prompt set `{set_info['id']}`",
        "",
        "| | main | branch | Δ |",
        "|---|---|---|---|",
        f"| **Accuracy** | {fmt_pct(m_acc)} ({m_pass}/{m_n}) | "
        f"{fmt_pct(b_acc)} ({b_pass}/{b_n}) | "
        f"{'n/a' if delta is None else f'{delta:+.1f} pts'} |",
        f"| **LLM spend (USD)** | ${m_cost:.4f} | ${b_cost:.4f} | "
        f"{cost_delta:+.4f}{cost_pct} |",
        "",
    ]

    if regressions:
        lines.append(f"#### 🔴 Regressions ({len(regressions)})")
        for r in regressions:
            note = f" — _{r['branch_error']}_" if r.get("branch_error") else ""
            lines.append(f"- {r['prompt']}{note}")
        lines.append("")
    if improvements:
        lines.append(f"#### 🟢 Improvements ({len(improvements)})")
        for r in improvements:
            lines.append(f"- {r['prompt']}")
        lines.append("")

    lines.append("<details><summary>Per-prompt detail</summary>")
    lines.append("")
    lines.append("| Prompt | main | branch | status | main $ | branch $ | Δ $ "
                 "| main q | branch q | branch time | conversation |")
    lines.append("|---|:---:|:---:|---|---:|---:|---:|---:|---:|---:|---|")
    for r in rows:
        prompt = r["prompt"] if len(r["prompt"]) <= 90 else r["prompt"][:87] + "…"
        prompt = prompt.replace("|", "\\|")
        conv = r.get("branch_conversation_id") or ""
        t = r.get("branch_timing_ms")
        t_str = "" if t is None else f"{t / 1000:.1f}s"
        mc, bc = r.get("main_total_cost"), r.get("branch_total_cost")
        mc_str = "" if mc is None else f"${mc:.4f}"
        bc_str = "" if bc is None else f"${bc:.4f}"
        dc_str = f"{bc - mc:+.4f}" if (mc is not None and bc is not None) else ""
        mq = r.get("main_query_count")
        bq = r.get("branch_query_count")
        lines.append(
            f"| {prompt} | {score_cell(r['main_score'])} | {score_cell(r['branch_score'])} "
            f"| {r['status']} | {mc_str} | {bc_str} | {dc_str} "
            f"| {'' if mq is None else mq} | {'' if bq is None else bq} "
            f"| {t_str} | `{conv}` |"
        )
    lines.append("")
    lines.append("_LLM spend is the raw model cost (USD) reported by Omni's API "
                 "(prompt + judge), not the Omni credits your org is billed — "
                 "credits are a separate unit with no published dollar conversion._")
    lines.append("</details>")
    return "\n".join(lines), len(regressions)


def render_markdown(cfg, branch_id, set_results):
    sections = []
    total_regressions = 0
    for sr in set_results:
        section_md, n_regressions = render_set_section(
            sr["set"], sr["rows"], sr["main_run"], sr["branch_run"])
        sections.append(section_md)
        total_regressions += n_regressions

    if total_regressions:
        headline = f"🔴 Omni AI eval: {total_regressions} prompt(s) regressed on this branch"
    else:
        headline = "🟢 Omni AI eval: no regressions detected"

    set_count = len(set_results)
    set_word = "prompt set" if set_count == 1 else "prompt sets"

    lines = [
        "<!-- omni-ai-eval -->",
        f"### {headline}",
        "",
        f"branch `{cfg.branch_name}` (`{branch_id}`) vs `main` · "
        f"{set_count} {set_word}",
        "",
    ]
    lines.append("\n\n".join(sections))
    lines.append("")
    lines.append("_This check **fails on any regression** (a prompt that passed "
                 "on `main` but failed on the branch), in any prompt set. Score is "
                 "the judge's binary pass/fail; a prompt's optional `expectation` "
                 "gives the judge a reference answer to check against. Cost changes "
                 "never gate._")
    return "\n".join(lines)


def write_no_branch_summary(cfg):
    md = "\n".join([
        "<!-- omni-ai-eval -->",
        "### 🔴 Omni AI eval: no matching Omni branch found",
        "",
        f"No Omni model branch named `{cfg.branch_name}` exists under model "
        f"`{cfg.model_id}`, so the PR's changes could not be evaluated.",
        "",
        "This usually means the branch was edited directly on GitHub rather than "
        "through an `omni-sync` session, or the Omni branch name differs from the "
        "git branch. Start an `omni-sync` session for this branch, or rename the "
        "Omni branch to match, then re-run this check.",
    ])
    with open(cfg.summary_path, "w") as f:
        f.write(md)
    print(md)


def main():
    if not PROMPT_SETS:
        sys.stderr.write("PROMPT_SETS is empty; add at least one prompt set to omni_eval.py\n")
        sys.exit(2)

    cfg = Config()

    branch_id = resolve_branch_id(cfg)
    if not branch_id:
        write_no_branch_summary(cfg)
        sys.stderr.write(f"No Omni branch named '{cfg.branch_name}' found.\n")
        sys.exit(3)

    print(f"Resolved branch '{cfg.branch_name}' -> {branch_id}")

    # Omni allows only 2 in-progress eval runs org-wide, so prompt sets are run
    # one at a time (main + branch per set) rather than all at once.
    set_results = []
    for prompt_set in PROMPT_SETS:
        main_id = start_run(cfg, prompt_set["id"], "CI baseline (main)")
        branch_id_run = start_run(
            cfg, prompt_set["id"], f"CI branch eval ({cfg.branch_name})", branch_id=branch_id)
        print(f"[{prompt_set['label']}] started runs: main={main_id} branch={branch_id_run}")

        main_run, branch_run = poll_both(cfg, main_id, branch_id_run)
        print(f"[{prompt_set['label']}] runs finished: "
              f"main={main_run['status']} branch={branch_run['status']}")

        rows = build_comparison(main_run, branch_run)
        set_results.append({
            "set": prompt_set,
            "main_run": main_run,
            "branch_run": branch_run,
            "rows": rows,
        })

    md = render_markdown(cfg, branch_id, set_results)

    with open(cfg.summary_path, "w") as f:
        f.write(md)
    with open(cfg.results_path, "w") as f:
        json.dump({
            "branch_name": cfg.branch_name,
            "branch_id": branch_id,
            "prompt_sets": [
                {
                    "prompt_set_id": sr["set"]["id"],
                    "label": sr["set"]["label"],
                    "main_run": sr["main_run"],
                    "branch_run": sr["branch_run"],
                    "comparison": sr["rows"],
                }
                for sr in set_results
            ],
        }, f, indent=2)

    print(md)

    # Gate: fail the check if any prompt regressed (passed on main, failed on branch),
    # in any prompt set.
    all_regressions = [
        (sr["set"]["label"], r)
        for sr in set_results
        for r in sr["rows"] if r["status"] == "regressed"
    ]
    if all_regressions:
        sys.stderr.write(
            f"{len(all_regressions)} prompt(s) regressed on this branch:\n"
            + "".join(f"  - [{label}] {r['prompt']}\n" for label, r in all_regressions))
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
