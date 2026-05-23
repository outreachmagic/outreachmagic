#!/usr/bin/env python3
"""
Outreach Magic — Simple Pipeline Web UI
Single-file web server. No frameworks, no npm, no build step.
Just Python stdlib serving the pipeline dashboard at http://localhost:3100.
"""

import http.server
import json
import os
import sys
import sqlite3
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
DB_PATH = Path(HERMES_HOME) / "outreach_magic.db"

_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))


def _pipeline_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("om_pipeline", _PIPELINE_DIR / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def query_leads(
    stage=None,
    limit=50,
    sentiment=None,
    auto_reply=None,
    lead_status=None,
    sort="updated_at",
    order="desc",
):
    om = _pipeline_module()
    ar = None
    if auto_reply is not None:
        ar = auto_reply in (True, 1, "1", "true", "yes")
    return om.get_pipeline(
        stage_filter=stage,
        limit=limit,
        sentiment=sentiment,
        auto_reply=ar,
        lead_status=lead_status,
        sort=sort,
        order=order,
    )


def query_stats():
    conn = sqlite3.connect(str(DB_PATH))
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    stages = conn.execute("SELECT stage, COUNT(*) as count FROM leads GROUP BY stage").fetchall()
    stage_counts = {r["stage"]: r["count"] for r in stages}
    active = sum(v for k, v in stage_counts.items() if k not in ("won", "lost"))
    recent = conn.execute("SELECT COUNT(*) FROM events WHERE created_at > datetime('now', '-7 days')").fetchone()[0]
    conn.close()
    return {"total_leads": total, "total_events": events, "active_pipeline": active,
            "won": stage_counts.get("won", 0), "lost": stage_counts.get("lost", 0),
            "events_7d": recent, "stages": stage_counts}


PIPELINE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Outreach Magic \u2014 Pipeline</title>
<style>
  :root { --bg: #0d1117; --surface: #161b22; --border: #30363d; --text: #c9d1d9;
    --text-dim: #8b949e; --green: #3fb950; --blue: #58a6ff; --yellow: #d2991d;
    --red: #da3633; --purple: #a371f7; --orange: #db6d28; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5; padding: 24px; }
  header { display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
  h1 { font-size: 1.5rem; font-weight: 600; }
  h1 span { color: var(--purple); }
  .stats-bar { display: flex; gap: 24px; margin-bottom: 24px; flex-wrap: wrap; }
  .stat { background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 20px; }
  .stat-value { font-size: 1.5rem; font-weight: 700; }
  .stat-label { font-size: 0.75rem; color: var(--text-dim); text-transform: uppercase; }
  .stat-value.green { color: var(--green); } .stat-value.red { color: var(--red); } .stat-value.blue { color: var(--blue); }
  .stage-filters { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
  .stage-filter { background: var(--surface); border: 1px solid var(--border); color: var(--text);
    padding: 6px 14px; border-radius: 20px; cursor: pointer; font-size: 0.8rem; transition: all 0.15s; }
  .stage-filter:hover, .stage-filter.active { background: var(--purple); border-color: var(--purple); color: white; }
  table { width: 100%; border-collapse: collapse; background: var(--surface);
    border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  th { text-align: left; padding: 12px 16px; font-size: 0.75rem; text-transform: uppercase;
    color: var(--text-dim); border-bottom: 1px solid var(--border); background: var(--surface); }
  td { padding: 10px 16px; border-bottom: 1px solid var(--border); font-size: 0.875rem; }
  tr:hover { background: rgba(88, 166, 255, 0.05); }
  .stage-badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.75rem; font-weight: 500; }
  .stage-prospecting { background: rgba(139,148,158,0.15); color: var(--text-dim); }
  .stage-contacted { background: rgba(88,166,255,0.15); color: var(--blue); }
  .stage-replied { background: rgba(163,113,247,0.15); color: var(--purple); }
  .stage-interested { background: rgba(210,153,29,0.15); color: var(--yellow); }
  .stage-proposal { background: rgba(219,109,40,0.15); color: var(--orange); }
  .stage-won { background: rgba(63,185,80,0.15); color: var(--green); }
  .stage-lost { background: rgba(218,54,51,0.15); color: var(--red); }
  .empty-state { text-align: center; padding: 60px 20px; color: var(--text-dim); }
  .empty-state h2 { font-size: 1.2rem; margin-bottom: 8px; color: var(--text); }
  .empty-state code { background: var(--surface); border: 1px solid var(--border);
    padding: 2px 8px; border-radius: 4px; font-size: 0.85rem; }
</style>
</head>
<body>
<header><h1>Outreach Magic <span>Pipeline</span></h1><span class="last-updated" id="last-updated"></span></header>
<div class="stats-bar" id="stats"></div>
<div class="stage-filters">
  <button class="stage-filter active" onclick="filterStage('')">All</button>
  <button class="stage-filter" onclick="filterStage('prospecting')">Prospecting</button>
  <button class="stage-filter" onclick="filterStage('contacted')">Contacted</button>
  <button class="stage-filter" onclick="filterStage('replied')">Replied</button>
  <button class="stage-filter" onclick="filterStage('interested')">Interested</button>
  <button class="stage-filter" onclick="filterStage('proposal')">Proposal</button>
  <button class="stage-filter" onclick="filterStage('won')">Won</button>
  <button class="stage-filter" onclick="filterStage('lost')">Lost</button>
</div>
<div id="table-container"><table id="pipeline-table"></table></div>
<script>
let currentStage='';
function sc(s){const m={prospecting:'stage-prospecting',contacted:'stage-contacted',replied:'stage-replied',interested:'stage-interested',proposal:'stage-proposal',won:'stage-won',lost:'stage-lost'};return m[s]||'stage-prospecting';}
function ta(ts){if(!ts)return'';const d=(Date.now()-new Date(ts+'Z').getTime())/1000;if(d<60)return'just now';if(d<3600)return Math.floor(d/60)+'m ago';if(d<86400)return Math.floor(d/3600)+'h ago';return Math.floor(d/86400)+'d ago';}
async function load(){
  const u=currentStage?'/api/leads?stage='+currentStage:'/api/leads';
  const[rs,rl]=await Promise.all([fetch('/api/stats'),fetch(u)]);
  const s=await rs.json(),l=await rl.json();
  document.getElementById('last-updated').textContent='Updated '+ta(new Date().toISOString());
  document.getElementById('stats').innerHTML=`<div class="stat"><div class="stat-value blue">${s.active_pipeline}</div><div class="stat-label">Active Pipeline</div></div><div class="stat"><div class="stat-value green">${s.won}</div><div class="stat-label">Won</div></div><div class="stat"><div class="stat-value red">${s.lost}</div><div class="stat-label">Lost</div></div><div class="stat"><div class="stat-value">${s.total_leads}</div><div class="stat-label">Total Leads</div></div><div class="stat"><div class="stat-value">${s.events_7d}</div><div class="stat-label">Events (7d)</div></div>`;
  if(!l.length){document.getElementById('pipeline-table').innerHTML=`<div class="empty-state"><h2>No leads yet</h2><p>Your Hermes agent will populate this automatically as it does outreach.</p></div>`;return;}
  document.getElementById('pipeline-table').innerHTML=`<thead><tr><th>Lead</th><th>Company</th><th>Stage</th><th>Last Activity</th><th>Events</th><th>Next Action</th></tr></thead><tbody>${l.map(ld=>`<tr><td>${e(ld.name)}${ld.title?'<br><span style="font-size:0.75rem;color:var(--text-dim)">'+e(ld.title)+'</span>':''}</td><td>${e(ld.company||'')}</td><td><span class="stage-badge ${sc(ld.stage)}">${ld.stage}</span></td><td style="font-size:0.8rem;color:var(--text-dim)">${ta(ld.last_event_at)}</td><td style="text-align:center">${ld.event_count}</td><td style="font-size:0.8rem;color:var(--text-dim)">${e(ld.next_action||'')}</td></tr>`).join('')}</tbody>`;}
function e(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function filterStage(st){currentStage=st;document.querySelectorAll('.stage-filter').forEach(b=>b.classList.remove('active'));event.target.classList.add('active');load();}
load();setInterval(load,30000);
</script>
</body>
</html>"""


class PipelineHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path == "/api/stats":
            self._json(query_stats())
        elif path == "/api/leads":
            stage = params.get("stage", [None])[0]
            sentiment = params.get("sentiment", [None])[0]
            auto_reply_raw = params.get("auto_reply", [None])[0]
            lead_status = params.get("lead_status", [None])[0]
            sort = params.get("sort", ["updated_at"])[0]
            order = params.get("order", ["desc"])[0]
            limit = int(params.get("limit", ["50"])[0])
            auto_reply = None
            if auto_reply_raw is not None:
                auto_reply = auto_reply_raw.lower() in ("1", "true", "yes")
            self._json(
                query_leads(
                    stage=stage,
                    limit=limit,
                    sentiment=sentiment,
                    auto_reply=auto_reply,
                    lead_status=lead_status,
                    sort=sort,
                    order=order,
                )
            )
        else:
            self._html(PIPELINE_HTML)

    def _json(self, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    port = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[1] == "--port" else 3100
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        print("Run: python pipeline.py init"); sys.exit(1)
    server = http.server.HTTPServer(("0.0.0.0", port), PipelineHandler)
    print(f"Outreach Magic Pipeline => http://localhost:{port}")
    print(f"Database: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()