"""The Tools tab: Hindsight and the Price Predictor (see TOOLS_PLAN.md).

Both work the way f1fantasytools.com's Hindsight and Budget Builder do. The
data is read here at build time and baked into the page as JSON; the team
search runs in the browser, so budget, chip and filters change instantly.

F1 Fantasy's price rule, worked out from the 2026 history (2026-10-31): after
each round, an asset's price moves by its average points per $M over the last
3 rounds it took part in (including this one), against its current price:

    avg PPM      < 0.6   0.6-0.9   0.9-1.2   1.2+
    $18.5M+       -0.3     -0.1      +0.1     +0.3
    under         -0.6     -0.2      +0.2     +0.6

Prices stay between $3.0M and $34.0M, and an asset that didn't take part in
the round doesn't move. That reproduces 605 of the 608 changes so far; the
other 3 are odd ones (Lawson twice, Gasly once)."""
import json
import math

PRICE_MIN, PRICE_MAX = 3.0, 34.0
TIER_A_FROM = 18.5
PPM_CUTS = (0.6, 0.9, 1.2)
STEPS_A = (-0.3, -0.1, 0.1, 0.3)
STEPS_B = (-0.6, -0.2, 0.2, 0.6)


def steps_for(price):
    return STEPS_A if price >= TIER_A_FROM else STEPS_B


def clamp_step(price, step):
    return round(min(PRICE_MAX, max(PRICE_MIN, price + step)) - price, 1)


def price_change(price, window_pts):
    """window_pts: points in each of the last 3 rounds the asset took part in,
    this one included. Returns the price change."""
    if not window_pts:
        return 0.0
    ppm = sum(window_pts) / len(window_pts) / price
    band = sum(ppm >= c for c in PPM_CUTS)
    return clamp_step(price, steps_for(price)[band])


def _norm_cdf(x, mean, sd):
    return 0.5 * (1 + math.erf((x - mean) / (sd * math.sqrt(2))))


def read_tools_data(conn, season, race_name_by_round):
    """Everything the Tools tab needs, as plain JSON-able data."""
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(players)")}
    has_tla = "tla" in pcols and "active" in pcols
    players = {}
    for row in conn.execute(
        "SELECT id, name, type" + (", tla, active" if has_tla else ", NULL, 1") + " FROM players"
    ):
        pid, name, ptype, tla, active = row
        if ptype not in ("driver", "constructor"):
            continue
        code = tla or (name or pid).replace(".", "").split()[-1][:3].upper()
        players[pid] = {"code": code, "name": name, "type": ptype, "active": active != 0}

    try:
        played = {(r, pid): bool(p) for r, pid, p in conn.execute(
            "SELECT round, player_id, played FROM player_gameday_stats WHERE season=?", (season,))}
    except Exception:
        played = {}

    res = {}   # (round, pid) -> (points, no-negative points, price)
    for r, pid, pts, nn, val in conn.execute(
        "SELECT round, player_id, gameday_points, no_negative_points, value "
        "FROM player_results WHERE season=?", (season,)
    ):
        res[(r, pid)] = (pts, nn, val)

    def took_part(r, pid):
        if (r, pid) in played:
            return played[(r, pid)]
        return res.get((r, pid), (None,))[0] not in (None, 0)  # old rounds, before the stats feed

    managers = {mid: name for mid, name in conn.execute("SELECT id, name FROM managers")}
    chips = {}
    for mid, chip, rnd in conn.execute(
        "SELECT manager_id, chip_name, round_taken FROM chips_used WHERE season=?", (season,)
    ):
        chips[(mid, rnd)] = chip

    # ── Hindsight: every round with league results ──────────────────────────
    rounds = [r for (r,) in conn.execute(
        "SELECT DISTINCT round FROM race_results WHERE season=? ORDER BY round", (season,))]
    final = {r: bool(f) for r, f in conn.execute(
        "SELECT round, MAX(is_final) FROM race_results WHERE season=? GROUP BY round", (season,))}
    team_value = {(r, mid): v for r, mid, v in conn.execute(
        "SELECT round, manager_id, team_value FROM race_results WHERE season=?", (season,))}
    hindsight = []
    for r in rounds:
        assets = []
        for pid, p in players.items():
            pts, nn, price = res.get((r, pid), (None, None, None))
            if pts is None or price is None or not took_part(r, pid):
                continue
            nxt = res.get((r + 1, pid), (None, None, None))[2]
            assets.append({
                "id": pid, "c": p["code"], "t": "C" if p["type"] == "constructor" else "D",
                "p": round(price, 1), "x": pts, "n": nn if nn is not None else max(0.0, pts),
                "d": round(nxt - price, 1) if nxt is not None else None,
            })
        teams = []
        for mid, pts, cap, mega in conn.execute(
            "SELECT manager_id, points, captain_player_id, mega_captain_player_id FROM race_results "
            "WHERE season=? AND round=?", (season, r)
        ):
            picks = [pid for (pid,) in conn.execute(
                "SELECT player_id FROM team_picks WHERE season=? AND round=? AND manager_id=? "
                "ORDER BY position", (season, r, mid))]
            budget = 100.0 if r == 1 else team_value.get((r - 1, mid))
            teams.append({"m": managers.get(mid, mid), "pts": pts, "picks": picks,
                          "cap": cap, "mega": mega, "chip": chips.get((mid, r)),
                          "budget": round(budget, 1) if budget else None})
        teams.sort(key=lambda t: t["m"])
        hindsight.append({"r": r, "name": race_name_by_round.get(r, f"Round {r}"),
                          "final": final.get(r, False), "assets": assets, "teams": teams})

    # ── Price predictor: the newest round F1 has published prices for ───────
    priced = [r for (r,) in conn.execute(
        "SELECT DISTINCT round FROM player_results WHERE season=? AND value IS NOT NULL "
        "ORDER BY round", (season,))]
    predictor = None
    if priced:
        T = priced[-1]
        live = T in rounds
        rows = []
        for pid, p in players.items():
            price = res.get((T, pid), (None, None, None))[2]
            if price is None or not p["active"]:
                continue
            prev = [(r, res.get((r, pid), (None,))[0] if took_part(r, pid) else None)
                    for r in (T - 2, T - 1) if r >= 1]
            counted = [x for _, x in prev if x is not None]
            n = len(counted) + 1
            needs = [round(c * n * price - sum(counted), 1) for c in PPM_CUTS]
            steps = [clamp_step(price, s) for s in steps_for(price)]
            # Odds from recent form: a normal curve over the last 8 rounds the
            # asset took part in. A placeholder until the simulation (plan step B).
            hist = [res[(r, pid)][0] for r in range(T - 1, 0, -1)
                    if took_part(r, pid) and res.get((r, pid), (None,))[0] is not None][:8]
            mean = sum(hist) / len(hist) if hist else 0.0
            sd = (sum((h - mean) ** 2 for h in hist) / (len(hist) - 1)) ** 0.5 if len(hist) > 2 else 15.0
            sd = max(sd, 6.0)
            cdf = [_norm_cdf(x - 0.5, mean, sd) for x in needs]
            probs = [cdf[0], cdf[1] - cdf[0], cdf[2] - cdf[1], 1 - cdf[2]]
            rows.append({
                "c": p["code"], "t": "C" if p["type"] == "constructor" else "D",
                "p": round(price, 1), "prev": prev, "needs": needs, "steps": steps,
                "probs": [round(x, 3) for x in probs], "mean": round(mean, 1),
                "xd": round(sum(pr * s for pr, s in zip(probs, steps)), 2),
            })
        rows.sort(key=lambda a: -a["p"])
        predictor = {"r": T, "name": race_name_by_round.get(T, f"Round {T}"), "live": live,
                     "prevRounds": [r for r in (T - 2, T - 1) if r >= 1], "assets": rows}

    return {"hindsight": hindsight, "predictor": predictor}


def _json(v):
    return json.dumps(v, separators=(",", ":")).replace("</", "<\\/")


def panel_tools(data):
    tools = data.get("tools") or {}
    if not tools.get("hindsight") and not tools.get("predictor"):
        return '<div class="card" style="padding:16px;color:#888">No data yet.</div>'
    return (TOOLS_HTML
            .replace("__TOOLS_DATA__", _json(tools))
            .replace("__MANAGER_COLOURS__", _json({m["name"]: m["color"] for m in data["managers"]})))


TOOLS_HTML = r"""
<style>
.tl-sub{display:flex;gap:6px;margin:4px 0 12px}
.tl-sub button{flex:1;font-size:13px;padding:8px 10px;border-radius:8px;border:0.5px solid #333;background:#1a1a1a;color:#aaa;cursor:pointer}
.tl-sub button.on{background:#2a2a2a;color:#eee;border-color:#555}
.tl-ctl{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}
.tl-ctl label{font-size:10px;color:#777;text-transform:uppercase;letter-spacing:.04em;display:block;margin-bottom:3px}
#panel-tools button{font-family:inherit}
.tl-ctl select,.tl-ctl input{width:100%;box-sizing:border-box;font-family:inherit;font-size:16px;padding:7px 28px 7px 9px;border-radius:8px;border:0.5px solid #333;background-color:#1a1a1a;color:#e8e8e6;-webkit-appearance:none;appearance:none}
.tl-ctl input{padding-right:9px}
.tl-ctl select{background-image:linear-gradient(45deg,transparent 50%,#888 50%),linear-gradient(135deg,#888 50%,transparent 50%);background-position:calc(100% - 15px) 50%,calc(100% - 10px) 50%;background-size:5px 5px;background-repeat:no-repeat}
.tl-note{font-size:11px;color:#777;line-height:1.45;margin:6px 0 10px}
.tl-team{display:grid;grid-template-columns:22px 1fr 46px 40px;gap:6px;align-items:center;padding:8px 0;border-bottom:0.5px solid #2a2a2a}
.tl-rank{font-size:11px;color:#666}
.tl-codes{display:flex;flex-wrap:wrap;gap:3px}
.tl-code{font-size:11px;font-weight:600;padding:2px 5px;border-radius:5px;background:#252525;color:#ddd;white-space:nowrap}
.tl-code.c{background:#1e2a36;color:#9cc8f0}
.tl-code.x2{background:#1a3010;color:#88dd44}
.tl-code.x3{background:#3a2a00;color:#ffaa44}
.tl-num{text-align:right;font-size:13px;font-variant-numeric:tabular-nums}
.tl-num small{display:block;font-size:10px;color:#777}
.tl-assets{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 8px}
.tl-assets button{font-size:11px;font-weight:600;padding:4px 6px;border-radius:6px;border:0.5px solid #333;background:#1a1a1a;color:#aaa;cursor:pointer}
.tl-assets button.inc{background:#1a3010;color:#88dd44;border-color:#2f5a1a}
.tl-assets button.exc{background:#3a1414;color:#ff7a7a;border-color:#5a2020;text-decoration:line-through}
.tl-cmp{font-size:13px;line-height:1.5;padding:10px 0}
.tl-more{width:100%;margin:10px 0 4px;font-size:13px;padding:8px;border-radius:8px;border:0.5px solid #333;background:#1a1a1a;color:#aaa;cursor:pointer}
.tl-pp{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.tl-pp th{font-size:9px;color:#666;font-weight:500;text-align:right;padding:4px 2px;text-transform:uppercase}
.tl-pp th:first-child,.tl-pp td:first-child{text-align:left}
.tl-pp td{font-size:12px;text-align:right;padding:6px 2px;border-top:0.5px solid #2a2a2a;vertical-align:top}
.tl-pp td small{display:block;font-size:9px;color:#666;font-weight:400}
.tl-pp .lk{font-weight:600}
.tl-up{color:#88dd44}.tl-dn{color:#ff7a7a}
</style>
<div class="tl-sub">
  <button id="tlBtnH" class="on" onclick="tlShow('h')">Hindsight</button>
  <button id="tlBtnP" onclick="tlShow('p')">Price predictor</button>
</div>
<div id="tlH">
  <div class="tl-note">The best teams you could have picked for a past round, from the points every driver and constructor actually scored. Pick a manager to use their budget and compare with what they had.</div>
  <div class="tl-ctl">
    <div><label>Round</label><select id="tlRound"></select></div>
    <div><label>Manager</label><select id="tlMgr"></select></div>
    <div><label>Max budget ($M)</label><input id="tlBudget" type="number" step="0.1" inputmode="decimal"></div>
    <div><label>Chip</label><select id="tlChip">
      <option value="">None</option><option value="X3">Extra DRS (3x)</option>
      <option value="NN">No Negative</option><option value="LL">Limitless</option></select></div>
  </div>
  <div class="tl-note" style="margin-bottom:2px">Tap a driver or constructor to require it, again to leave it out, again to reset.</div>
  <div class="tl-assets" id="tlAssets"></div>
  <div id="tlCmp" class="tl-cmp"></div>
  <div class="card" style="padding:4px 12px" id="tlTeams"></div>
  <button class="tl-more" id="tlMore">Show more teams</button>
</div>
<div id="tlP" style="display:none"></div>
<script>
(function(){
const T=__TOOLS_DATA__, COL=__MANAGER_COLOURS__;
const $=id=>document.getElementById(id);
window.tlShow=function(w){
  $('tlH').style.display=w==='h'?'':'none'; $('tlP').style.display=w==='p'?'':'none';
  $('tlBtnH').classList.toggle('on',w==='h'); $('tlBtnP').classList.toggle('on',w==='p');
};
const fmt=(v,d=1)=>v==null?'—':(+v).toFixed(d);
const sgn=v=>v==null?'—':(v>0?'+':'')+(+v).toFixed(1);

// ── Team search: every 5-driver + 2-constructor team under budget. DRS (2x)
// goes on the best driver; Extra DRS puts 3x on the best and 2x on the next.
function combos(arr,k){const out=[];const n=arr.length;const idx=[...Array(k).keys()];
  if(n<k)return out;
  while(true){out.push(idx.map(i=>arr[i]));let i=k-1;while(i>=0&&idx[i]===n-k+i)i--;if(i<0)break;idx[i]++;for(let j=i+1;j<k;j++)idx[j]=idx[j-1]+1;}
  return out;}
function bestTeams(assets,o){
  const val=a=>o.chip==='NN'?a.n:a.x;
  const ok=a=>!o.exc.has(a.id);
  const D=assets.filter(a=>a.t==='D'&&ok(a)), C=assets.filter(a=>a.t==='C'&&ok(a));
  const incD=D.filter(a=>o.inc.has(a.id)), incC=C.filter(a=>o.inc.has(a.id));
  const budget=o.chip==='LL'?1e9:o.budget;
  const ds=combos(D,5).filter(s=>incD.every(a=>s.includes(a))).map(s=>{
    const v=s.map(val).sort((a,b)=>b-a);
    const base=v.reduce((a,b)=>a+b,0);
    const pts=o.chip==='X3'?base+2*v[0]+v[1]:base+v[0];
    return {s,cost:s.reduce((a,b)=>a+b.p,0),pts,d:s.reduce((a,b)=>a+(b.d||0),0)};
  }).sort((a,b)=>b.pts-a.pts);
  const cs=combos(C,2).filter(s=>incC.every(a=>s.includes(a)));
  const top=[];let floor=-1e9;const N=o.n;
  for(const c of cs){const cc=c[0].p+c[1].p, cp=val(c[0])+val(c[1]), cd=(c[0].d||0)+(c[1].d||0);
    const left=budget-cc+1e-6;
    for(const d of ds){ if(d.pts+cp<=floor&&top.length>=N)break;
      if(d.cost>left)continue;
      const t={c,d:d.s,pts:d.pts+cp,cost:cc+d.cost,dp:cd+d.d};
      top.push(t);top.sort((a,b)=>b.pts-a.pts||a.cost-b.cost);
      if(top.length>N)top.pop();
      if(top.length>=N)floor=top[top.length-1].pts;}}
  return top;}
function boosts(drivers,chip){const v=drivers.map(a=>[chip==='NN'?a.n:a.x,a.id]).sort((a,b)=>b[0]-a[0]);
  return chip==='X3'?{[v[0][1]]:'x3',[v[1][1]]:'x2'}:{[v[0][1]]:'x2'};}
function teamRow(t,i,chip){
  const b=boosts(t.d,chip);
  const codes=t.c.map(a=>`<span class="tl-code c">${a.c}</span>`).join('')+
    t.d.slice().sort((a,b)=>(chip==='NN'?b.n-a.n:b.x-a.x)).map(a=>`<span class="tl-code ${b[a.id]||''}">${a.c}${b[a.id]?' '+b[a.id]:''}</span>`).join('');
  return `<div class="tl-team"><div class="tl-rank">${i+1}</div><div class="tl-codes">${codes}</div>
    <div class="tl-num">${Math.round(t.pts)}<small>pts</small></div><div class="tl-num">${fmt(t.cost)}<small>${sgn(Math.round(t.dp*10)/10)}</small></div></div>`;}

// ── Hindsight ──────────────────────────────────────────────────────────────
const H=T.hindsight||[];
const st={inc:new Set(),exc:new Set(),n:10};
const rSel=$('tlRound'), mSel=$('tlMgr'), bIn=$('tlBudget'), cSel=$('tlChip');
H.slice().reverse().forEach(h=>rSel.add(new Option(`R${h.r} ${h.name}${h.final?'':' (live)'}`,h.r)));
function round(){return H.find(h=>h.r==rSel.value);}
function fillMgr(){const h=round();const keep=mSel.value;mSel.innerHTML='';
  mSel.add(new Option('— none —',''));(h?h.teams:[]).forEach(t=>mSel.add(new Option(t.m,t.m)));
  if([...mSel.options].some(o=>o.value===keep))mSel.value=keep;}
function mgr(){const h=round();return h&&h.teams.find(t=>t.m===mSel.value);}
function setBudget(){const m=mgr();bIn.value=(m&&m.budget?m.budget:100).toFixed(1);}
function assetsBar(){const h=round();if(!h){$('tlAssets').innerHTML='';return;}
  const sorted=h.assets.slice().sort((a,b)=>(a.t>b.t?1:a.t<b.t?-1:b.p-a.p));
  $('tlAssets').innerHTML=sorted.map(a=>`<button data-id="${a.id}" class="${st.inc.has(a.id)?'inc':st.exc.has(a.id)?'exc':''}">${a.c} <span style="font-weight:400;color:#888">${a.x}</span></button>`).join('');
  $('tlAssets').querySelectorAll('button').forEach(b=>b.onclick=()=>{const id=b.dataset.id;
    if(st.inc.has(id)){st.inc.delete(id);st.exc.add(id);}else if(st.exc.has(id)){st.exc.delete(id);}else st.inc.add(id);
    assetsBar();runH();});}
function runH(){
  const h=round(); if(!h){$('tlTeams').innerHTML='<div class="tl-note">No rounds yet.</div>';return;}
  const o={budget:parseFloat(bIn.value)||100,chip:cSel.value,inc:st.inc,exc:st.exc,n:st.n};
  const top=bestTeams(h.assets,o);
  $('tlTeams').innerHTML=top.length?`<div class="tl-team" style="padding:4px 0"><div></div><div class="tl-rank">TEAM</div><div class="tl-num"><small>POINTS</small></div><div class="tl-num"><small>COST / Δ$</small></div></div>`+top.map((t,i)=>teamRow(t,i,o.chip)).join('')
    :'<div class="tl-note">No team fits that budget and those filters.</div>';
  $('tlMore').style.display=top.length>=st.n&&st.n<50?'':'none';
  const m=mgr();
  if(m&&top.length){const best=top[0].pts;const col=COL[m.m]||'#ccc';
    const byId=Object.fromEntries(h.assets.map(a=>[a.id,a]));
    const mine=m.picks.map(id=>byId[id]).filter(Boolean);
    const cost=mine.reduce((a,b)=>a+b.p,0);
    $('tlCmp').innerHTML=`<span style="color:${col};font-weight:600">${m.m}</span> scored <b>${Math.round(m.pts)}</b>${m.chip?` with ${m.chip}`:''}, team cost ${fmt(cost)}. Best possible within ${fmt(o.budget)}: <b>${Math.round(best)}</b> (${Math.round(best-m.pts)} more).<div class="tl-codes" style="margin-top:6px">${mine.map(a=>`<span class="tl-code ${a.t==='C'?'c':''} ${a.id===m.mega?'x3':a.id===m.cap?'x2':''}">${a.c} ${a.x}</span>`).join('')}</div>`;
  } else $('tlCmp').innerHTML='';
}
rSel.onchange=()=>{st.inc.clear();st.exc.clear();st.n=10;fillMgr();setBudget();assetsBar();runH();};
mSel.onchange=()=>{setBudget();runH();};
bIn.oninput=runH; cSel.onchange=runH;
$('tlMore').onclick=()=>{st.n=Math.min(50,st.n+10);runH();};
fillMgr();setBudget();assetsBar();runH();

// ── Price predictor ────────────────────────────────────────────────────────
const P=T.predictor;
function ppTable(rows,tier,label){
  if(!rows.length)return '';
  // The tier's steps (an asset at the $3M floor or $34M cap moves less).
  const hd=rows[0].p>=18.5?[-0.3,-0.1,0.1,0.3]:[-0.6,-0.2,0.2,0.6];
  const pr=P.prevRounds;
  return `<div class="section-label">${label}</div><table class="tl-pp"><tr><th>${tier}</th><th>$</th>${pr.map(r=>`<th>R${r}</th>`).join('')}
    ${hd.map(s=>`<th>${sgn(s)}</th>`).join('')}<th>xΔ$</th></tr>`+
  rows.map(a=>{const cells=a.probs.map((p,i)=>{const need=i===0?`&lt;${Math.ceil(a.needs[0])}`:`${Math.ceil(a.needs[i-1])}+`;
      const pct=Math.round(p*100);const lk=pct===Math.max(...a.probs.map(x=>Math.round(x*100)));
      return `<td class="${lk?'lk':''}">${pct}%<small>${need}</small></td>`;}).join('');
    return `<tr><td class="lk">${a.c}</td><td>${fmt(a.p)}</td>${a.prev.map(([r,x])=>`<td>${x==null?'—':Math.round(x)}</td>`).join('')}${cells}<td class="${a.xd>0.04?'tl-up':a.xd<-0.04?'tl-dn':''}">${a.xd>0?'+':''}${a.xd.toFixed(2)}</td></tr>`;}).join('')+'</table>';}
if(P){
  const A=P.assets, grp=(t,hi)=>A.filter(a=>a.t===t&&(hi?a.p>=18.5:a.p<18.5));
  $('tlP').innerHTML=`<div class="tl-note">Price changes after <b>R${P.r} ${P.name}</b>${P.live?' (weekend in progress)':''}. Each percentage is the chance of that price move; underneath is the points it takes. F1 moves a price by its average points per $M over the last 3 races it took part in (this one included): under 0.6, 0.6 to 0.9, 0.9 to 1.2, or 1.2 and up. Prices stay between $3M and $34M.
  <br>The odds come from each asset's last 8 races, so they lag behind form changes. Odds from a proper race simulation are planned.</div>`+
  ppTable(grp('D',true),'Driver','Drivers $18.5M+')+ppTable(grp('D',false),'Driver','Drivers under $18.5M')+
  ppTable(grp('C',true),'Team','Constructors $18.5M+')+ppTable(grp('C',false),'Team','Constructors under $18.5M');
} else $('tlP').innerHTML='<div class="tl-note">No prices published yet.</div>';
})();
</script>
"""
