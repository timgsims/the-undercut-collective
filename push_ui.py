"""The 🔔 Notifications panel (1 Nov 2026): turn push notifications on or off on
this device, say which manager you are, and pick which ones you want. The
server side is push-service/ (container undercut-push on Sentinel) and
notify.py, which decides what to send. iPhones only allow push in the app
added to the home screen (iOS 16.4+), never in a Safari tab.
"""
import json

PUSH_API = "https://f1push.simsey.co.nz"

# (type, label, what it is) in the order shown. Keep the types in step with
# push-service/src/server.js TYPES and notify.py. The two that give away
# results (SPOILERS) start switched off (Tim, 1 Nov 2026); the rest start on.
KINDS = [
    ("lock", "Team lock reminder", "2 hours before teams lock, or 7:30pm the night before if it's in the night"),
    ("sim", "New simulation", "When a new simulation is out: very early, then after each practice"),
    ("penalty", "Grid penalties", "When a driver gets a grid penalty, back-of-grid or pit lane start for the next race"),
    ("inactive", "Driver not racing", "If a driver in your team won't be in the next race"),
    ("quali", "Qualifying points", "Your points and place after qualifying (a spoiler if you watch later)"),
    ("final", "Final points", "Your score and league place once a round is final (also a spoiler)"),
    ("prices", "New prices", "When the next round's prices are out, with your team's value"),
    ("chips", "Chips left", "With 4, 2 and 1 races to go, the chips you haven't used"),
]

SPOILERS = {"quali", "final"}

BELL_BUTTON = ('<button class="nb-bell" onclick="ucNotifyOpen()" aria-label="Notifications" '
               'title="Notifications">🔔</button>')


def panel_html(managers):
    return (NOTIFY_HTML
            .replace("__PUSH_API__", PUSH_API)
            .replace("__MANAGERS__", json.dumps([m["name"] for m in managers]))
            .replace("__KINDS__", json.dumps([{"t": t, "l": l, "d": d, "on": t not in SPOILERS}
                                              for t, l, d in KINDS])))


NOTIFY_HTML = r"""
<style>
.nb-bell{flex:none;font-size:16px;line-height:1;background:#1a1a1a;border:0.5px solid #2a2a2a;border-radius:20px;padding:5px 9px;cursor:pointer;font-family:inherit}
.nb-wrap{position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.6);display:none;align-items:flex-end;justify-content:center}
.nb-wrap.on{display:flex}
.nb-sheet{background:#151515;border:0.5px solid #2a2a2a;border-radius:16px 16px 0 0;width:100%;max-width:560px;max-height:88vh;overflow-y:auto;padding:16px 16px calc(20px + env(safe-area-inset-bottom));box-sizing:border-box;color:#e8e8e6}
@media (min-width:700px){.nb-wrap{align-items:center}.nb-sheet{border-radius:16px}}
.nb-head{display:flex;align-items:center;margin-bottom:8px}
.nb-head h2{font-size:18px;margin:0;font-weight:700;flex:1}
.nb-x{background:none;border:0;color:#888;font-size:22px;cursor:pointer;padding:2px 6px;font-family:inherit}
.nb-note{font-size:12px;color:#888;line-height:1.5;margin:6px 0 12px}
.nb-row{display:flex;align-items:center;gap:12px;padding:11px 0;border-top:0.5px solid #262626}
.nb-row .nb-t{flex:1;font-size:14px}
.nb-row .nb-t small{display:block;font-size:11px;color:#777;margin-top:2px;line-height:1.35}
.nb-sw{flex:none;width:46px;height:28px;border-radius:14px;background:#333;border:0;position:relative;cursor:pointer;transition:background .15s}
.nb-sw::after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#ddd;transition:left .15s}
.nb-sw.on{background:#2f9e44}.nb-sw.on::after{left:21px;background:#fff}
.nb-sw:disabled{opacity:.4}
.nb-sel{width:100%;box-sizing:border-box;font-family:inherit;font-size:16px;padding:9px 30px 9px 10px;border-radius:8px;border:0.5px solid #333;background-color:#1a1a1a;color:#e8e8e6;-webkit-appearance:none;appearance:none;
  background-image:linear-gradient(45deg,transparent 50%,#888 50%),linear-gradient(135deg,#888 50%,transparent 50%);background-position:calc(100% - 16px) 50%,calc(100% - 11px) 50%;background-size:5px 5px;background-repeat:no-repeat}
.nb-lbl{font-size:10px;color:#777;text-transform:uppercase;letter-spacing:.04em;margin:12px 0 4px}
.nb-btn{width:100%;margin-top:14px;font-family:inherit;font-size:14px;padding:10px;border-radius:8px;border:0.5px solid #333;background:#1f1f1f;color:#ddd;cursor:pointer}
.nb-msg{font-size:12px;color:#aaa;min-height:16px;margin-top:8px;text-align:center}
</style>
<div class="nb-wrap" id="nbWrap" onclick="if(event.target===this)ucNotifyClose()">
  <div class="nb-sheet" role="dialog" aria-label="Notifications">
    <div class="nb-head"><h2>Notifications</h2><button class="nb-x" onclick="ucNotifyClose()" aria-label="Close">×</button></div>
    <div id="nbBody"></div>
  </div>
</div>
<script>
(function(){
const API='__PUSH_API__', MANAGERS=__MANAGERS__, KINDS=__KINDS__;
const $=id=>document.getElementById(id);
const isIos=()=>/iPhone|iPad|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
const standalone=()=>window.matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
let st={on:false,manager:'',prefs:{},endpoint:null,why:null,busy:false,msg:''};

function device(){const ua=navigator.userAgent;
  return /iPhone/.test(ua)?'iPhone':/iPad/.test(ua)||isIos()?'iPad':/Android/.test(ua)?'Android phone':/Mac/.test(ua)?'Mac':/Windows/.test(ua)?'Windows PC':'computer';}
async function call(path,body){
  const r=await fetch(API+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  const j=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(j.error||('error '+r.status));
  return j;}
const keyBytes=b=>Uint8Array.from(atob(b.replace(/-/g,'+').replace(/_/g,'/')+'='.repeat((4-b.length%4)%4)),c=>c.charCodeAt(0));
// A kind not chosen yet takes its default (the spoiler ones start off).
const isOn=k=>k.t in st.prefs?st.prefs[k.t]!==false:k.on;
function prefsOut(){const p={};KINDS.forEach(k=>p[k.t]=isOn(k));return p;}

async function refresh(){
  st.why=null;
  if(!('serviceWorker' in navigator)||!('PushManager' in window)||location.protocol!=='https:'){
    st.why=isIos()&&!standalone()?'home':'unsupported';render();return;}
  try{
    const reg=await navigator.serviceWorker.ready;
    const sub=await reg.pushManager.getSubscription();
    st.endpoint=sub?sub.endpoint:null;
    if(sub){const s=await call('/api/status',{endpoint:sub.endpoint});
      st.on=!!s.on; if(s.on){st.manager=s.manager||'';st.prefs=s.prefs||{};}
      else{ // the service forgot this phone (e.g. it was reset): sign it up again
        await call('/api/subscribe',{subscription:sub.toJSON(),manager:st.manager,prefs:prefsOut(),device:device()});st.on=true;}
    } else st.on=false;
  }catch(e){st.msg="Couldn't reach the notifications service. Try again in a minute.";}
  render();}

async function toggle(){
  if(st.busy)return; st.busy=true; st.msg='';
  try{
    const reg=await navigator.serviceWorker.ready;
    if(st.on){
      const sub=await reg.pushManager.getSubscription();
      if(sub){await call('/api/unsubscribe',{endpoint:sub.endpoint}).catch(()=>{});await sub.unsubscribe();}
      st.on=false;st.msg='Notifications are off on this device.';
    }else{
      // must come straight after the tap, or iOS won't ask
      const perm=await Notification.requestPermission();
      if(perm!=='granted'){st.msg=perm==='denied'?'Notifications are blocked for this app. Allow them in your phone\'s Settings, then try again.':'Not turned on.';}
      else{
        const {key}=await call('/api/key');
        const sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:keyBytes(key)});
        await call('/api/subscribe',{subscription:sub.toJSON(),manager:st.manager,prefs:prefsOut(),device:device()});
        st.on=true;st.endpoint=sub.endpoint;st.msg=st.manager?'On. Send a test to check.':'On. Pick who you are for messages about your own team.';
      }
    }
  }catch(e){st.msg="Couldn't change notifications: "+e.message;}
  st.busy=false;render();}

async function save(){
  if(!st.on||!st.endpoint)return;
  try{await call('/api/prefs',{endpoint:st.endpoint,manager:st.manager,prefs:prefsOut()});st.msg='Saved.';}
  catch(e){st.msg="Couldn't save: "+e.message;}
  render();}

async function test(){
  st.msg='Sending…';render();
  try{await call('/api/test',{endpoint:st.endpoint});st.msg='Sent. It should arrive in a few seconds.';}
  catch(e){st.msg=e.message;}
  render();}

function render(){
  const b=$('nbBody'); if(!b)return;
  if(st.why==='home'){b.innerHTML=`<div class="nb-note">On an iPhone, notifications only work in the app on your home screen.<br><br>In Safari, tap <b>Share</b> then <b>Add to Home Screen</b>, open The Undercut Collective from your home screen, and come back to this 🔔.</div>`;return;}
  if(st.why){b.innerHTML=`<div class="nb-note">This browser can't take notifications. Try Chrome or Edge, or on an iPhone the app added to your home screen.</div>`;return;}
  b.innerHTML=`<div class="nb-note">Reminders to set your team and news about your points. Nothing is sent between 10pm and 7:30am NZ time.</div>
    <div class="nb-row"><div class="nb-t"><b>Notifications on this device</b></div><button class="nb-sw ${st.on?'on':''}" id="nbMain" ${st.busy?'disabled':''} aria-pressed="${st.on}"></button></div>
    <div style="${st.on?'':'opacity:.45;pointer-events:none'}">
      <div class="nb-lbl">Who are you?</div>
      <select class="nb-sel" id="nbMgr"><option value="">Not saying (general messages only)</option>${MANAGERS.map(m=>`<option ${m===st.manager?'selected':''}>${m}</option>`).join('')}</select>
      <div class="nb-lbl" style="margin-top:16px">Send me</div>
      ${KINDS.map(k=>`<div class="nb-row"><div class="nb-t">${k.l}<small>${k.d}</small></div><button class="nb-sw ${isOn(k)?'on':''}" data-k="${k.t}" aria-pressed="${isOn(k)}"></button></div>`).join('')}
      <button class="nb-btn" id="nbTest">Send a test</button>
    </div>
    <div class="nb-msg">${st.msg||''}</div>`;
  $('nbMain').onclick=toggle;
  $('nbMgr').onchange=e=>{st.manager=e.target.value;save();};
  b.querySelectorAll('[data-k]').forEach(x=>x.onclick=()=>{const k=KINDS.find(y=>y.t===x.dataset.k);st.prefs[k.t]=!isOn(k);save();});
  $('nbTest').onclick=test;}

window.ucNotifyOpen=()=>{st.msg='';$('nbWrap').classList.add('on');render();refresh();};
window.ucNotifyClose=()=>$('nbWrap').classList.remove('on');
})();
</script>
"""
