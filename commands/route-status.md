---
description: Show recent auto-router decisions and a rolling distribution of model routings. Reads from ~/.claude/cache/router/audit.jsonl.
---

# /route-status

Print a summary of recent auto-router activity.

## What to show

1. **Last 20 decisions** (most recent first):

   ```bash
   tail -n 20 ~/.claude/cache/router/audit.jsonl | tac
   ```

   Render each as a compact line:
   `<ts>  <band>  <model>/<effort>  conf=<0.xx>  <reason — truncated>`

2. **Distribution over the last 7 days**:

   ```bash
   python3 - <<'PY'
   import json, os, time, collections
   from datetime import datetime, timezone, timedelta
   path = os.path.expanduser("~/.claude/cache/router/audit.jsonl")
   cutoff = datetime.now(timezone.utc) - timedelta(days=7)
   by_model = collections.Counter()
   by_band = collections.Counter()
   total = 0
   with open(path) as f:
       for line in f:
           try: r = json.loads(line)
           except: continue
           ts = datetime.fromisoformat(r["ts"].replace("Z","+00:00"))
           if ts < cutoff: continue
           d = r.get("decision") or {}
           by_model[d.get("model","?")] += 1
           by_band[d.get("band","?")] += 1
           total += 1
   print(f"Decisions in last 7d: {total}")
   if total:
       print("By model:")
       for m,c in by_model.most_common():
           print(f"  {m:<8} {c/total*100:>5.1f}%  ({c})")
       print("By band:")
       for b,c in by_band.most_common():
           print(f"  {b:<6} {c/total*100:>5.1f}%  ({c})")
   PY
   ```

3. **Cache size**:

   ```bash
   ls ~/.claude/cache/router/*.json 2>/dev/null | wc -l
   ```

   Report as: `<N> cached classifications`.

4. If `audit.jsonl` is missing or empty, print:
   `No auto-router activity yet. Send a prompt to see decisions.`
