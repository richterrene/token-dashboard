#!/usr/bin/env python3
#
# Copyright (C) 2026 richterrene
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Token & cost dashboard for Claude Code and Codex — single-file, zero deps.

Cross-platform (macOS / Linux / Windows). Pure Python standard library: nothing
to pip-install. Reads local session logs already on your machine (no network
except an optional live FX-rate lookup), prices tokens per model, and writes a
self-contained dashboard.html you can double-click.

Share this ONE file with colleagues. They just run:

    python3 token_dashboard.py            # check env, build + open dashboard
    python3 token_dashboard.py --no-open  # build only
    python3 token_dashboard.py --check    # environment preflight only, then exit
    python3 token_dashboard.py --install  # build + auto-refresh every 10 min
    python3 token_dashboard.py --uninstall

Auto-refresh backend per OS:
  macOS    launchd LaunchAgent
  Linux    systemd --user timer (falls back to a crontab entry)
  Windows  Scheduled Task (schtasks)

Data sources:
  Claude Code: <home>/.claude/projects/**/*.jsonl   (assistant msg.usage per record)
  Codex:       <home>/.codex/sessions/**/*.jsonl     (event_msg/token_count last_token_usage)

The tool ABORTS if Python is too old or if NEITHER tool's logs are found.
PRICES below are editable estimates (USD per 1M tokens). Confirm against your
own billing — cache-read volume dominates Claude totals, so its rate matters most.
"""
import argparse
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import date, datetime, timedelta

MIN_PYTHON = (3, 7)

# ---------------------------------------------------------------------------
# Pricing — USD per 1,000,000 tokens. Matched by model-name prefix (longest first).
#
# These are Anthropic's published list prices, which are also the exact rates
# Claude Code's own `/cost` command uses (same usage fields x the same table),
# so the Claude figures here match `/cost`. If you access Claude via AWS Bedrock,
# on-demand per-token rates equal these list prices too. Either way this is an
# ESTIMATE derived from token counts, not a provider invoice. Base (<=200K
# context) rates below; long-context tiers can run higher.
#
# Codex (GPT-5.x) is priced separately. EDIT THESE to match the rates you pay.
# ---------------------------------------------------------------------------
PRICES = {
    # Claude (Anthropic list prices == Claude Code /cost)
    "claude-opus":    {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet":  {"input": 3.0,  "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku":   {"input": 1.0,  "output": 5.0,  "cache_read": 0.10, "cache_write": 1.25},
    # Codex (OpenAI GPT-5.x) — estimates, adjust to your plan
    "gpt-5":          {"input": 1.25, "output": 10.0, "cache_read": 0.125, "cache_write": 0.0},
}
DEFAULT_PRICE = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}

FALLBACK_USD_EUR = 0.8598  # used if the live FX lookup fails

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude", "projects")
CODEX_DIR = os.path.join(HOME, ".codex", "sessions")
CLAUDE_GLOB = os.path.join(CLAUDE_DIR, "**", "*.jsonl")
CODEX_GLOB = os.path.join(CODEX_DIR, "**", "*.jsonl")
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.abspath(__file__)
OUT_HTML = os.path.join(HERE, "dashboard.html")

# Scheduler identifiers / paths (per-OS; only the relevant one is used).
JOB_LABEL = "io.github.tokendashboard"           # macOS launchd / Linux unit id
JOB_NAME = "TokenDashboard"                       # human-facing Windows task name
JOB_DISPLAY_NAME = "AI Token Dashboard"           # macOS Login Items display name
PLIST_PATH = os.path.join(HOME, "Library", "LaunchAgents", JOB_LABEL + ".plist")
# macOS shows the Login Item under the name of the launched executable's
# enclosing .app bundle, so we run Python through this tiny wrapper bundle.
APP_BUNDLE = os.path.join(HOME, "Library", "Application Support",
                          JOB_LABEL, JOB_DISPLAY_NAME + ".app")
SYSTEMD_DIR = os.path.join(HOME, ".config", "systemd", "user")
SYSTEMD_SERVICE = os.path.join(SYSTEMD_DIR, JOB_LABEL + ".service")
SYSTEMD_TIMER = os.path.join(SYSTEMD_DIR, JOB_LABEL + ".timer")


def _count_jsonl(directory):
    if not os.path.isdir(directory):
        return None  # tool not installed at all
    return len(glob.glob(os.path.join(directory, "**", "*.jsonl"), recursive=True))


def check_environment(require_data=True):
    """Preflight: verify Python version and that usable data sources exist.

    Returns a dict of findings. Exits the process (non-zero) on any fatal
    problem so the install / build never proceeds on a broken setup.
    """
    problems = []

    if sys.version_info < MIN_PYTHON:
        problems.append(
            "Python %d.%d+ required, but this is %s. Install a newer Python "
            "and re-run with it." % (MIN_PYTHON[0], MIN_PYTHON[1],
                                     platform.python_version()))

    claude_n = _count_jsonl(CLAUDE_DIR)
    codex_n = _count_jsonl(CODEX_DIR)

    found = []
    if claude_n is None:
        print("  - Claude Code: not found (%s missing)" % CLAUDE_DIR)
    elif claude_n == 0:
        print("  - Claude Code: installed, but no session logs yet (%s)" % CLAUDE_DIR)
    else:
        print("  - Claude Code: %d session files (%s)" % (claude_n, CLAUDE_DIR))
        found.append("claude")

    if codex_n is None:
        print("  - Codex: not found (%s missing)" % CODEX_DIR)
    elif codex_n == 0:
        print("  - Codex: installed, but no session logs yet (%s)" % CODEX_DIR)
    else:
        print("  - Codex: %d session files (%s)" % (codex_n, CODEX_DIR))
        found.append("codex")

    if require_data and not found:
        problems.append(
            "No usage data found for either Claude Code or Codex. Nothing to "
            "report. Use one of these tools first (or check your home directory: "
            "%s)." % HOME)

    # Browser is best-effort, not fatal — warn only.
    try:
        webbrowser.get()
        browser_ok = True
    except webbrowser.Error:
        browser_ok = False
        print("  - Note: no default browser detected; open dashboard.html manually.")

    if problems:
        sys.stderr.write("\nEnvironment check FAILED:\n")
        for p in problems:
            sys.stderr.write("  ! " + p + "\n")
        sys.exit(2)

    return {"claude": claude_n, "codex": codex_n,
            "found": found, "browser": browser_ok}


def price_for(model):
    if not model:
        return DEFAULT_PRICE
    for prefix in sorted(PRICES, key=len, reverse=True):
        if model.startswith(prefix):
            return PRICES[prefix]
    return DEFAULT_PRICE


def fetch_usd_eur():
    try:
        with urllib.request.urlopen("https://open.er-api.com/v6/latest/USD", timeout=8) as r:
            d = json.load(r)
        rate = float(d["rates"]["EUR"])
        return rate, d.get("time_last_update_utc", "live"), "open.er-api.com"
    except Exception:
        return FALLBACK_USD_EUR, "fallback constant", "offline"


_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def parse_day(s):
    """Return the YYYY-MM-DD prefix of an ISO8601 timestamp, or None.

    Avoids datetime.fromisoformat() entirely: across Python 3.7-3.10 it rejects
    trailing 'Z' and some offset forms, which would silently drop records. The
    logs are UTC ISO8601, so the leading date is all we need for daily buckets.
    """
    if not isinstance(s, str):
        return None
    m = _DATE_RE.match(s)
    return m.group(0) if m else None


def blank():
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0, "cost_usd": 0.0}


def add(bucket, day, model, inp, out, cr=0, cw=0):
    p = price_for(model)
    cost = (inp * p["input"] + out * p["output"]
            + cr * p["cache_read"] + cw * p["cache_write"]) / 1_000_000
    b = bucket[day]
    b["input"] += inp
    b["output"] += out
    b["cache_read"] += cr
    b["cache_write"] += cw
    b["total"] += inp + out + cr + cw
    b["cost_usd"] += cost
    return cost


def collect_claude(by_day, by_model):
    seen = set()
    files = glob.glob(CLAUDE_GLOB, recursive=True)
    for path in files:
        try:
            fh = open(path, "r", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                # Dedup on the API response identity, not the transcript node:
                # Claude Code copies messages into new session files on resume,
                # compaction, and branching, each time minting a fresh "uuid".
                # message.id (with requestId, when present) is stable across
                # those copies; keying on uuid would never dedup and ~doubles
                # the totals.
                key = msg.get("id") or d.get("requestId") or d.get("uuid")
                req = d.get("requestId")
                if key is not None:
                    dedup = (key, req)
                    if dedup in seen:
                        continue
                    seen.add(dedup)
                day = parse_day(d.get("timestamp"))
                if not day:
                    continue
                model = msg.get("model") or "claude-unknown"
                inp = usage.get("input_tokens", 0) or 0
                out = usage.get("output_tokens", 0) or 0
                cr = usage.get("cache_read_input_tokens", 0) or 0
                cw = usage.get("cache_creation_input_tokens", 0) or 0
                cost = add(by_day, day, model, inp, out, cr, cw)
                bm = by_model[model]
                bm["total"] += inp + out + cr + cw
                bm["cost_usd"] += cost
    return len(files)


def collect_codex(by_day, by_model):
    files = glob.glob(CODEX_GLOB, recursive=True)
    for path in files:
        try:
            fh = open(path, "r", errors="replace")
        except OSError:
            continue
        model = "gpt-unknown"
        with fh:
            for line in fh:
                if "model" not in line and "token_count" not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "turn_context":
                    m = d.get("model") or (d.get("payload", {}) or {}).get("model")
                    if m:
                        model = m
                    continue
                payload = d.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info") or {}
                lu = info.get("last_token_usage")  # per-turn delta; sum == running total
                if not isinstance(lu, dict):
                    continue
                day = parse_day(d.get("timestamp"))
                if not day:
                    continue
                inp = lu.get("input_tokens", 0) or 0
                cached = lu.get("cached_input_tokens", 0) or 0
                out = lu.get("output_tokens", 0) or 0
                # Codex input_tokens already includes cached_input_tokens.
                cost = add(by_day, day, model, inp - cached, out, cr=cached, cw=0)
                bm = by_model[model]
                bm["total"] += inp + out
                bm["cost_usd"] += cost
    return len(files)


def rollups(by_day):
    days = sorted(by_day.keys())
    fields = ("input", "output", "cache_read", "cache_write", "total", "cost_usd")
    daily = [{"date": d, **by_day[d]} for d in days]

    weekly = defaultdict(blank)
    monthly = defaultdict(blank)
    for d in days:
        dt = date.fromisoformat(d)
        wk = (dt - timedelta(days=dt.weekday())).isoformat()
        mo = d[:7]  # YYYY-MM
        for k in fields:
            weekly[wk][k] += by_day[d][k]
            monthly[mo][k] += by_day[d][k]
    weekly_list = [{"week": w, **weekly[w]} for w in sorted(weekly)]
    monthly_list = [{"month": m, **monthly[m]} for m in sorted(monthly)]

    all_time = blank()
    for d in days:
        for k in fields:
            all_time[k] += by_day[d][k]
    return daily, weekly_list, monthly_list, all_time


def build_data():
    usd_eur, fx_asof, fx_src = fetch_usd_eur()
    c_day, c_model = defaultdict(blank), defaultdict(blank)
    x_day, x_model = defaultdict(blank), defaultdict(blank)
    n_claude = collect_claude(c_day, c_model)
    n_codex = collect_codex(x_day, x_model)
    cd, cw, cm, ca = rollups(c_day)
    xd, xw, xm, xa = rollups(x_day)
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "fx": {"usd_eur": usd_eur, "asof": fx_asof, "source": fx_src},
        "prices": PRICES,
        "sources": {"claude_files": n_claude, "codex_files": n_codex},
        "claude": {"daily": cd, "weekly": cw, "monthly": cm, "all_time": ca,
                   "by_model": {m: c_model[m] for m in sorted(c_model)}},
        "codex": {"daily": xd, "weekly": xw, "monthly": xm, "all_time": xa,
                  "by_model": {m: x_model[m] for m in sorted(x_model)}},
    }


# ---------------------------------------------------------------------------
# HTML template — data is inlined into __DATA__ so the file is self-contained.
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Token &amp; Cost Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#0f1115;--panel:#181b22;--border:#262b36;--text:#e6e9ef;--muted:#8b93a3;--claude:#d97706;--codex:#10a37f;}
  *{box-sizing:border-box;} body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:24px;}
  h1{font-size:20px;margin:0 0 4px;} .meta{color:var(--muted);font-size:12px;margin-bottom:20px;}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin-bottom:24px;}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;}
  .card .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em;}
  .card .big{font-size:24px;font-weight:600;margin:6px 0 2px;} .card .sub{color:var(--muted);font-size:12px;}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle;}
  .claude{color:var(--claude);} .bg-claude{background:var(--claude);}
  .codex{color:var(--codex);} .bg-codex{background:var(--codex);}
  .panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:20px;}
  .panel h2{font-size:14px;margin:0 0 12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em;}
  .toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap;}
  .toolbar button{background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:5px 12px;cursor:pointer;font-size:13px;}
  .toolbar button.active{border-color:var(--text);background:#222732;}
  .chart-wrap{position:relative;height:340px;}
  table{width:100%;border-collapse:collapse;font-size:13px;} th,td{text-align:right;padding:6px 10px;border-bottom:1px solid var(--border);}
  th:first-child,td:first-child{text-align:left;} th{color:var(--muted);font-weight:600;}
  td.num{font-variant-numeric:tabular-nums;}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px;} @media(max-width:800px){.grid2{grid-template-columns:1fr;}}
  .note{color:var(--muted);font-size:11px;margin-top:8px;}
  .footer{margin-top:8px;padding-top:16px;border-top:1px solid var(--border);
    display:flex;flex-direction:column;gap:6px;}
  .footer .foot-row{color:var(--muted);font-size:12px;}
  .footer #coverage{display:flex;gap:8px;align-items:baseline;}
  .footer #coverage b{color:var(--text);font-weight:600;white-space:nowrap;}
</style>
</head>
<body>
<h1>Token &amp; Cost Dashboard</h1>
<div id="app"></div>
<footer class="footer">
  <div class="foot-row" id="coverage"></div>
  <div class="foot-row meta" id="meta"></div>
</footer>
<script>const DATA = __DATA__;</script>
<script>
const fmt=n=>Math.round(n).toLocaleString("en-US");
const compact=n=>{n=Math.round(n);if(n>=1e9)return(n/1e9).toFixed(2)+"B";if(n>=1e6)return(n/1e6).toFixed(2)+"M";if(n>=1e3)return(n/1e3).toFixed(1)+"K";return String(n);};
const FX=DATA.fx.usd_eur;
const usd=n=>"$"+n.toLocaleString("en-US",{maximumFractionDigits:2,minimumFractionDigits:2});
const eur=n=>"€"+(n*FX).toLocaleString("en-US",{maximumFractionDigits:2,minimumFractionDigits:2});
const money=n=>usd(n)+" · "+eur(n);
const sumLastN=(a,n,f)=>a.slice(-n).reduce((s,d)=>s+(d[f]||0),0);
const last=(a,f)=>a.length?(a[a.length-1][f]||0):0;

function run(){
  const c=DATA.claude,x=DATA.codex;
  const gen=new Date(DATA.generated_at);
  const covers=a=>a.length?a[0].date:"—";
  document.getElementById("meta").textContent=
    `Generated ${gen.toLocaleString()} · ${DATA.sources.claude_files} Claude + ${DATA.sources.codex_files} Codex session files · `+
    `FX USD→EUR ${FX} (${DATA.fx.source}, ${DATA.fx.asof})`;
  document.getElementById("coverage").innerHTML=
    `<b>Data coverage</b>`+
    `<span><span class="dot bg-claude"></span>Claude since ${covers(c.daily)} · `+
    `<span class="dot bg-codex"></span>Codex since ${covers(x.daily)}. `+
    `Earlier history isn't recorded — the source logs are pruned over time, `+
    `so absent days mean &ldquo;no record&rdquo;, not necessarily &ldquo;no usage&rdquo;.</span>`;
  const grandTok=c.all_time.total+x.all_time.total;
  const grandUsd=c.all_time.cost_usd+x.all_time.cost_usd;

  document.getElementById("app").innerHTML=`
    <div class="cards">
      <div class="card"><div class="label">All-time cost</div>
        <div class="big">${money(grandUsd)}</div>
        <div class="sub">${compact(grandTok)} tokens total</div></div>
      <div class="card"><div class="label"><span class="dot bg-claude"></span>Claude Code</div>
        <div class="big claude">${money(c.all_time.cost_usd)}</div>
        <div class="sub">${compact(c.all_time.total)} tok · today ${money(last(c.daily,"cost_usd"))}</div></div>
      <div class="card"><div class="label"><span class="dot bg-codex"></span>Codex</div>
        <div class="big codex">${money(x.all_time.cost_usd)}</div>
        <div class="sub">${compact(x.all_time.total)} tok · today ${money(last(x.daily,"cost_usd"))}</div></div>
      <div class="card"><div class="label">Last 7 days</div>
        <div class="big">${money(sumLastN(c.daily,7,"cost_usd")+sumLastN(x.daily,7,"cost_usd"))}</div>
        <div class="sub">${compact(sumLastN(c.daily,7,"total")+sumLastN(x.daily,7,"total"))} tokens</div></div>
    </div>

    <div class="panel">
      <h2>Over time</h2>
      <div class="toolbar">
        <span>Bucket:</span>
        <button data-g="daily">Daily</button>
        <button data-g="weekly">Weekly</button>
        <button data-g="monthly">Monthly</button>
        <span style="margin-left:16px">Measure:</span>
        <button data-m="cost_usd" class="active mbtn">Cost</button>
        <button data-m="total" class="mbtn">Tokens</button>
      </div>
      <div class="chart-wrap"><canvas id="trend"></canvas></div>
    </div>

    <div class="grid2">
      <div class="panel"><h2>Cost by model</h2>
        <div class="chart-wrap" style="height:280px"><canvas id="bymodel"></canvas></div>
        <div class="note">Claude priced at <b>Anthropic list rates</b> (= Claude Code <code>/cost</code>); Codex priced separately. Estimate from token counts, not a provider invoice. Rates editable in token_dashboard.py.</div>
      </div>
      <div class="panel"><h2>Last 14 days</h2>
        <table id="recent"><thead><tr><th>Date</th><th>Claude</th><th>Codex</th><th>Total $</th><th>€</th></tr></thead><tbody></tbody></table>
      </div>
    </div>`;

  // Default to a coarser bucket once daily history grows, so the trend chart
  // stays legible as data accumulates over months/years.
  const nDays=new Set([...c.daily.map(r=>r.date),...x.daily.map(r=>r.date)]).size;
  let curG=nDays>365?"monthly":nDays>60?"weekly":"daily",curM="cost_usd";
  let trend;
  const keyName=g=>g==="daily"?"date":g==="weekly"?"week":"month";
  function keys(g){const s=new Set();c[g].forEach(r=>s.add(r[keyName(g)]));x[g].forEach(r=>s.add(r[keyName(g)]));return[...s].sort();}
  function series(arr,ks,kf,m){const map=Object.fromEntries(arr.map(r=>[r[kf],r[m]]));return ks.map(k=>map[k]||0);}
  function drawTrend(){
    const kf=keyName(curG),ks=keys(curG);
    if(trend)trend.destroy();
    trend=new Chart(document.getElementById("trend"),{type:"bar",
      data:{labels:ks,datasets:[
        {label:"Claude Code",data:series(c[curG],ks,kf,curM),backgroundColor:"#d97706"},
        {label:"Codex",data:series(x[curG],ks,kf,curM),backgroundColor:"#10a37f"}]},
      options:{responsive:true,maintainAspectRatio:false,
        scales:{x:{stacked:true,grid:{color:"#262b36"},ticks:{color:"#8b93a3",autoSkip:true,maxTicksLimit:24,maxRotation:0}},
                y:{stacked:true,grid:{color:"#262b36"},ticks:{color:"#8b93a3",
                   callback:v=>curM==="cost_usd"?usd(v):compact(v)}}},
        plugins:{legend:{labels:{color:"#e6e9ef"}},
          tooltip:{callbacks:{label:ct=>`${ct.dataset.label}: `+(curM==="cost_usd"?money(ct.parsed.y):fmt(ct.parsed.y)+" tok")}}}}});
  }
  document.querySelector(`[data-g="${curG}"]`).classList.add("active");
  drawTrend();
  document.querySelectorAll("[data-g]").forEach(b=>b.onclick=()=>{document.querySelectorAll("[data-g]").forEach(x=>x.classList.remove("active"));b.classList.add("active");curG=b.dataset.g;drawTrend();});
  document.querySelectorAll("[data-m]").forEach(b=>b.onclick=()=>{document.querySelectorAll("[data-m]").forEach(x=>x.classList.remove("active"));b.classList.add("active");curM=b.dataset.m;drawTrend();});

  const models=[],costs=[],colors=[];
  const cPal=["#d97706","#f59e0b","#b45309","#92400e","#78350f"];
  const xPal=["#10a37f","#34d399","#059669","#065f46"];
  Object.entries(c.by_model).forEach(([m,v],i)=>{if(v.cost_usd>0){models.push(m);costs.push(v.cost_usd);colors.push(cPal[i%cPal.length]);}});
  Object.entries(x.by_model).forEach(([m,v],i)=>{if(v.cost_usd>0){models.push(m);costs.push(v.cost_usd);colors.push(xPal[i%xPal.length]);}});
  new Chart(document.getElementById("bymodel"),{type:"doughnut",
    data:{labels:models,datasets:[{data:costs,backgroundColor:colors,borderColor:"#181b22",borderWidth:2}]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{position:"right",labels:{color:"#e6e9ef",font:{size:11}}},
        tooltip:{callbacks:{label:ct=>`${ct.label}: ${money(ct.parsed)}`}}}}});

  const cM=Object.fromEntries(c.daily.map(r=>[r.date,r.cost_usd]));
  const xM=Object.fromEntries(x.daily.map(r=>[r.date,r.cost_usd]));
  const days=[...new Set([...Object.keys(cM),...Object.keys(xM)])].sort().slice(-14).reverse();
  document.querySelector("#recent tbody").innerHTML=days.map(d=>{
    const cc=cM[d]||0,xx=xM[d]||0,t=cc+xx;
    return `<tr><td>${d}</td><td class="num claude">${usd(cc)}</td><td class="num codex">${usd(xx)}</td><td class="num">${usd(t)}</td><td class="num">${eur(t)}</td></tr>`;}).join("");
}
run();
</script>
</body>
</html>
"""


def write_html(data):
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(data))
    tmp = OUT_HTML + ".tmp"
    with open(tmp, "w") as f:
        f.write(html)
    os.replace(tmp, OUT_HTML)


REFRESH_SECONDS = 600
RUN_ARGS = [SCRIPT, "--no-open"]


# ---- macOS: launchd --------------------------------------------------------
_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>
{args}
  </array>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{err}</string>
</dict></plist>
"""


_APP_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>{name}</string>
  <key>CFBundleDisplayName</key><string>{name}</string>
  <key>CFBundleIdentifier</key><string>{label}</string>
  <key>CFBundleExecutable</key><string>{name}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict></plist>
"""


def _build_app_bundle():
    """Create a minimal .app wrapper so macOS shows a friendly Login Item name."""
    # Rebuild from scratch so a stale launcher (e.g. an old "run") is removed.
    shutil.rmtree(APP_BUNDLE, ignore_errors=True)
    macos_dir = os.path.join(APP_BUNDLE, "Contents", "MacOS")
    os.makedirs(macos_dir, exist_ok=True)
    with open(os.path.join(APP_BUNDLE, "Contents", "Info.plist"), "w") as f:
        f.write(_APP_INFO_PLIST.format(name=JOB_DISPLAY_NAME, label=JOB_LABEL))
    # The Login Item is labeled after the running executable's name, so name
    # the launcher after the bundle rather than a generic "run".
    run = os.path.join(macos_dir, JOB_DISPLAY_NAME)
    cmd = " ".join('"%s"' % a for a in [sys.executable] + RUN_ARGS)
    with open(run, "w") as f:
        f.write("#!/bin/sh\nexec %s\n" % cmd)
    os.chmod(run, 0o755)
    # Unsigned bundles get attributed to the raw executable name ("run") in
    # Login Items; an ad-hoc signature makes macOS use CFBundleName instead.
    if _have("codesign"):
        subprocess.run(["codesign", "--force", "--deep", "-s", "-", APP_BUNDLE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
    return run


def _install_launchd():
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    exe = _build_app_bundle()
    args = "\n".join("    <string>%s</string>" % a for a in [exe])
    with open(PLIST_PATH, "w") as f:
        f.write(_PLIST.format(label=JOB_LABEL, args=args, interval=REFRESH_SECONDS,
                              log=os.path.join(HERE, "agent.log"),
                              err=os.path.join(HERE, "agent.err.log")))
    subprocess.run(["launchctl", "unload", PLIST_PATH],
                   stderr=subprocess.DEVNULL, check=False)
    r = subprocess.run(["launchctl", "load", PLIST_PATH], check=False)
    if r.returncode == 0:
        print("Installed launchd agent → %s (every %d min)"
              % (PLIST_PATH, REFRESH_SECONDS // 60))
    else:
        sys.exit("launchctl load failed (rc=%d)." % r.returncode)


def _uninstall_launchd():
    removed = False
    if os.path.exists(PLIST_PATH):
        subprocess.run(["launchctl", "unload", PLIST_PATH],
                       stderr=subprocess.DEVNULL, check=False)
        os.remove(PLIST_PATH)
        print("Removed launchd agent %s" % PLIST_PATH)
        removed = True
    if os.path.isdir(APP_BUNDLE):
        shutil.rmtree(APP_BUNDLE, ignore_errors=True)
    return removed


# ---- Linux: systemd --user timer, fallback to crontab ----------------------
_SYSTEMD_SERVICE = """[Unit]
Description=Token & cost dashboard refresh

[Service]
Type=oneshot
ExecStart={python} {script} --no-open
"""

_SYSTEMD_TIMER = """[Unit]
Description=Refresh token dashboard every {min} min

[Timer]
OnBootSec=1min
OnUnitActiveSec={sec}s
Persistent=true

[Install]
WantedBy=timers.target
"""


def _have(cmd):
    return shutil.which(cmd) is not None


def _systemd_user_available():
    if not _have("systemctl"):
        return False
    r = subprocess.run(["systemctl", "--user", "show-environment"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
    return r.returncode == 0


def _cron_line():
    redirect = ">> %s 2>> %s" % (os.path.join(HERE, "agent.log"),
                                 os.path.join(HERE, "agent.err.log"))
    cmd = "%s %s --no-open %s" % (sys.executable, SCRIPT, redirect)
    return "*/%d * * * * %s # %s" % (REFRESH_SECONDS // 60, cmd, JOB_LABEL)


def _read_crontab():
    r = subprocess.run(["crontab", "-l"], stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, check=False)
    return r.stdout.decode() if r.returncode == 0 else ""


def _write_crontab(text):
    p = subprocess.run(["crontab", "-"], input=text.encode(), check=False)
    return p.returncode == 0


def _install_cron():
    if not _have("crontab"):
        sys.exit("Neither systemd --user nor crontab is available; cannot "
                 "schedule. Run `--no-open` periodically yourself.")
    existing = "\n".join(l for l in _read_crontab().splitlines()
                         if JOB_LABEL not in l)
    new = (existing + "\n" if existing.strip() else "") + _cron_line() + "\n"
    if _write_crontab(new):
        print("Installed crontab entry (every %d min)." % (REFRESH_SECONDS // 60))
    else:
        sys.exit("Failed to write crontab.")


def _install_systemd():
    os.makedirs(SYSTEMD_DIR, exist_ok=True)
    with open(SYSTEMD_SERVICE, "w") as f:
        f.write(_SYSTEMD_SERVICE.format(python=sys.executable, script=SCRIPT))
    with open(SYSTEMD_TIMER, "w") as f:
        f.write(_SYSTEMD_TIMER.format(min=REFRESH_SECONDS // 60, sec=REFRESH_SECONDS))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    r = subprocess.run(["systemctl", "--user", "enable", "--now",
                        JOB_LABEL + ".timer"], check=False)
    if r.returncode == 0:
        print("Installed systemd --user timer (every %d min)."
              % (REFRESH_SECONDS // 60))
    else:
        sys.exit("systemctl --user enable failed (rc=%d)." % r.returncode)


def _install_linux():
    if _systemd_user_available():
        _install_systemd()
    else:
        print("systemd --user not available; using crontab instead.")
        _install_cron()


def _uninstall_linux():
    removed = False
    if _have("systemctl") and os.path.exists(SYSTEMD_TIMER):
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        JOB_LABEL + ".timer"], stderr=subprocess.DEVNULL, check=False)
        for p in (SYSTEMD_TIMER, SYSTEMD_SERVICE):
            if os.path.exists(p):
                os.remove(p)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        print("Removed systemd --user timer.")
        removed = True
    if _have("crontab"):
        ct = _read_crontab()
        if JOB_LABEL in ct:
            kept = "\n".join(l for l in ct.splitlines() if JOB_LABEL not in l)
            _write_crontab(kept + ("\n" if kept.strip() else ""))
            print("Removed crontab entry.")
            removed = True
    return removed


# ---- Windows: schtasks -----------------------------------------------------
def _install_windows():
    cmd = '"%s" "%s" --no-open' % (sys.executable, SCRIPT)
    r = subprocess.run(["schtasks", "/Create", "/TN", JOB_NAME, "/SC", "MINUTE",
                        "/MO", str(REFRESH_SECONDS // 60), "/TR", cmd, "/F"],
                       check=False)
    if r.returncode == 0:
        print("Installed Scheduled Task '%s' (every %d min)."
              % (JOB_NAME, REFRESH_SECONDS // 60))
    else:
        sys.exit("schtasks /Create failed (rc=%d)." % r.returncode)


def _uninstall_windows():
    r = subprocess.run(["schtasks", "/Delete", "/TN", JOB_NAME, "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
    if r.returncode == 0:
        print("Removed Scheduled Task '%s'." % JOB_NAME)
        return True
    return False


def install_agent():
    s = sys.platform
    if s == "darwin":
        _install_launchd()
    elif s.startswith("linux"):
        _install_linux()
    elif s.startswith("win"):
        _install_windows()
    else:
        sys.exit("Auto-refresh not supported on platform '%s'. Run with "
                 "--no-open on a schedule yourself." % s)


def uninstall_agent():
    s = sys.platform
    if s == "darwin":
        removed = _uninstall_launchd()
    elif s.startswith("linux"):
        removed = _uninstall_linux()
    elif s.startswith("win"):
        removed = _uninstall_windows()
    else:
        removed = False
    if not removed:
        print("No auto-refresh job was installed.")


def open_in_browser(path):
    try:
        if webbrowser.open("file://" + os.path.abspath(path)):
            return
    except webbrowser.Error:
        pass
    print("Open this file in your browser: %s" % path)


def main():
    ap = argparse.ArgumentParser(
        description="Token & cost dashboard for Claude Code and Codex "
                    "(cross-platform, zero dependencies).")
    ap.add_argument("--no-open", action="store_true",
                    help="build without opening the browser")
    ap.add_argument("--check", action="store_true",
                    help="run the environment preflight only, then exit")
    ap.add_argument("--install", action="store_true",
                    help="build, then schedule auto-refresh every 10 min (launchd/systemd/schtasks)")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove the scheduled auto-refresh job")
    args = ap.parse_args()

    if args.uninstall:
        uninstall_agent()
        return

    print("Environment check (Python %s on %s):"
          % (platform.python_version(), platform.system()))
    # --uninstall already returned; for install/build we require real data.
    check_environment(require_data=True)
    print("Environment OK.\n")

    if args.check:
        return

    data = build_data()
    write_html(data)
    ct = data["claude"]["all_time"]
    xt = data["codex"]["all_time"]
    print("Wrote %s" % OUT_HTML)
    print("  Claude: {:,} tok  ${:,.2f}  ({} files)".format(
        ct["total"], ct["cost_usd"], data["sources"]["claude_files"]))
    print("  Codex:  {:,} tok  ${:,.2f}  ({} files)".format(
        xt["total"], xt["cost_usd"], data["sources"]["codex_files"]))
    print("  FX USD->EUR {} ({})".format(data["fx"]["usd_eur"], data["fx"]["source"]))

    if args.install:
        install_agent()
    elif not args.no_open:
        open_in_browser(OUT_HTML)


if __name__ == "__main__":
    main()
