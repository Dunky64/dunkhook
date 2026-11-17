"""The overlay page: open http://127.0.0.1:3000 in a browser on a second screen."""

OVERLAY_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CS2 Coach</title>
<style>
:root{--bg:#0e1116;--card:#171b22;--fg:#e6e8eb;--dim:#8b939e;--call:#4cc38a;--warn:#f0a43a;--ai:#7aa7ff;--line:#262c36}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,sans-serif;padding:16px}
header{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:12px}
#score{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums}#meta{color:var(--dim)}
#conn{margin-left:auto;font-size:13px;color:var(--dim)}
.card{background:var(--card);border-left:4px solid var(--dim);border-radius:6px;padding:10px 12px;margin:8px 0}
.call{border-color:var(--call)}.warn{border-color:var(--warn)}.ai{border-color:var(--ai)}
.label{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.why{color:var(--dim);font-size:14px;margin-top:4px}
#bombclock{font-size:40px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--warn)}
</style></head><body>
<header><div id="score">-</div><div id="meta">waiting for CS2...</div><div id="conn">connecting</div></header>
<div id="bombclock"></div>
<div id="cards"></div>
<script>
const order=["economy","ai","bomb","health","ammo","died"], labels={economy:"Buy",ai:"Coach",bomb:"Bomb",health:"Health",ammo:"Ammo",died:"Status"};
const cards={}; let plantedAt=null, timer=null;
function render(){const root=document.getElementById("cards");root.innerHTML="";
 for(const k of order){const m=cards[k];if(!m)continue;const d=document.createElement("div");
  d.className="card "+(k==="ai"?"ai":m.level||"");
  d.innerHTML='<div class="label"></div><div class="text"></div>'+(m.because?'<div class="why"></div>':"");
  d.querySelector(".label").textContent=labels[k]+(k==="ai"&&m.round?" - round "+m.round:"");
  d.querySelector(".text").textContent=m.text; if(m.because)d.querySelector(".why").textContent=m.because;
  root.appendChild(d);}}
function tick(){const el=document.getElementById("bombclock");
 if(plantedAt===null){el.textContent="";return}
 const left=Math.max(0,40-(Date.now()-plantedAt)/1000);el.textContent="C4 "+left.toFixed(1)+"s";}
const es=new EventSource("/events");
es.onopen=()=>document.getElementById("conn").textContent="live";
es.onerror=()=>document.getElementById("conn").textContent="reconnecting";
es.onmessage=e=>{const m=JSON.parse(e.data);
 if(m.key==="header"){document.getElementById("score").textContent="CT "+m.score.CT+" : "+m.score.T+" T";
  document.getElementById("meta").textContent=[m.map,"round "+m.round,m.side?"you: "+m.side:"",m.phase].filter(Boolean).join(" | ");return}
 if(m.key==="bomb_clock"){if(m.clear){plantedAt=null}else{plantedAt=Date.now()-m.planted_ago*1000;
  if(!timer)timer=setInterval(tick,100)}tick();return}
 if(m.clear)delete cards[m.key];else cards[m.key]=m; render();};
</script></body></html>
"""
