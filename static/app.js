const $ = id => document.getElementById(id);
const orb = $("orb"), say = $("say"), you = $("you"), statusEl = $("status"), stepsEl = $("steps");
const MCP_URL = "/mcp", CHAINED = ["propose_fix", "open_fix_pr"];
let busy = false, rpcId = 0, sessionId = null, protoVersion = "2025-11-25", pending = null, rawLog = "";

const setState = (s, label) => { orb.dataset.state = s; statusEl.textContent = label || ""; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const logRaw = (label, obj) => { rawLog += `// ${label}\n${JSON.stringify(obj, null, 2)}\n\n`; $("raw").textContent = rawLog; };

/* ---------- real MCP client (JSON-RPC 2.0 over Streamable HTTP) ---------- */
async function rpc(method, params, isNote){
  const body = isNote ? {jsonrpc:"2.0", method, params} : {jsonrpc:"2.0", id:++rpcId, method, params};
  const h = {"Content-Type":"application/json", "Accept":"application/json, text/event-stream"};
  if(sessionId){ h["Mcp-Session-Id"] = sessionId; h["MCP-Protocol-Version"] = protoVersion; }
  const r = await fetch(MCP_URL, {method:"POST", headers:h, body:JSON.stringify(body)});
  const sid = r.headers.get("Mcp-Session-Id"); if(sid) sessionId = sid;
  if(isNote) return null;
  if(r.status===404 && sessionId){ sessionId = null; throw new Error("MCP session expired. Send your command again."); }
  if(!r.ok) throw new Error("MCP server returned HTTP "+r.status);
  const d = await r.json(); if(d.error) throw new Error(d.error.message); return d.result;
}
async function post(path, body){
  const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  if(!r.ok) throw new Error("Server returned HTTP "+r.status+" for "+path); return r.json();
}
async function connect(){
  const r = await rpc("initialize", {protocolVersion:"2025-11-25", capabilities:{}, clientInfo:{name:"opspulse-ambient-ui", version:"1.2.0"}});
  protoVersion = r.protocolVersion;
  await rpc("notifications/initialized", {}, true);
  setMode(true, r); return r;
}
function setMode(ok, info){
  const m = $("mode"); m.className = "mode "+(ok?"live":"demo");
  m.textContent = ok ? `Live · ${info.serverInfo.name} · MCP ${info.protocolVersion}` : "MCP server offline";
}

/* ---------- dynamic trace ---------- */
async function step(title, desc, work){
  const li = document.createElement("li"); li.className = "step run";
  li.innerHTML = '<i class="dot"></i><div><div class="t"></div><div class="d"></div></div><span class="ms"></span>';
  li.querySelector(".t").textContent = title; li.querySelector(".d").textContent = desc;
  stepsEl.appendChild(li); const t0 = performance.now();
  try{
    const out = await work(li.querySelector(".d"));
    li.className = "step done"; li.querySelector(".ms").textContent = Math.round(performance.now()-t0)+" ms"; return out;
  }catch(e){ li.className = "step fail"; throw e; }
}
async function ensureSession(){
  if(sessionId) await step("initialize","Session reused",async()=>{});
  else await step("initialize","Negotiate protocol version",connect);
}
async function callTool(name, args, desc){
  const r = await step("tools/call · "+name, desc, ()=>rpc("tools/call",{name, arguments:args}));
  if(r.isError) throw new Error(r.content?.[0]?.text || "Tool failed");
  logRaw(name, r.structuredContent); return r.structuredContent;
}

/* ---------- metrics ---------- */
function setMetric(id, text, cls){ const m=$(id); m.querySelector("b").textContent=text; m.className="m "+(cls||""); }
function updateMetrics(tool, o, ms){
  if(tool==="fetch_repo_health") setMetric("m-pr", o.open_prs, o.open_prs>5?"warn":"ok");
  if(tool==="run_security_audit"){ const n=o.findings.length; setMetric("m-vuln", n, n?"bad":"ok"); }
  if(tool==="triage_failed_ci") setMetric("m-ci", "failed", "bad");
  setMetric("m-lat", ms+" ms", ms<4000?"ok":"warn");
}

/* ---------- speech ---------- */
function speak(text){
  return new Promise(res=>{
    if(!$("voice").checked || !("speechSynthesis" in window)) return res();
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text); u.rate = 1.02;
    u.onend = u.onerror = res; speechSynthesis.speak(u);
  });
}
async function typeOut(text){
  say.className="say"; say.textContent="";
  for(const w of text.split(" ")){ say.textContent += (say.textContent?" ":"")+w; await sleep(45); }
}
async function reply(query, tool, result){
  setState("thinking","Writing the reply…");
  const sp = await step("bedrock.converse","Turn JSON into a spoken reply", async d=>{
    const s = await post("/alexa/speak",{query, tool, result}); d.textContent = "Reply by "+s.engine; return s; });
  setState("speaking","Alexa+ is speaking");
  await Promise.all([typeOut(sp.speech), speak(sp.speech)]);
}

/* ---------- incident card ---------- */
function showIncident(inc, prop){
  const live = inc.source === "github-actions";
  $("incBadge").textContent = live ? "LIVE CI FAILURE" : "SAMPLE INCIDENT";
  $("incBadge").className = "badge " + (live ? "live" : "sample");
  $("incTitle").textContent = `${inc.workflow||"CI"} · ${inc.failed_job||"job"} → ${inc.failed_step||"step"}`;
  $("incSummary").textContent = prop.summary;
  const pre = $("incDiff"); pre.textContent = "";
  (prop.diff||"").split("\n").filter(Boolean).forEach(line=>{
    const s = document.createElement("span"); s.textContent = line+"\n";
    s.className = line.startsWith("+")&&!line.startsWith("+++") ? "add" : line.startsWith("-")&&!line.startsWith("---") ? "del" : "ctx";
    pre.appendChild(s);
  });
  pre.hidden = !prop.diff;
  $("incConf").textContent = "Confidence "+Math.round(prop.confidence*100)+"%";
  $("incRisk").textContent = "Risk: "+prop.risk;
  $("incResult").textContent = "";
  $("confirmBtn").hidden = !prop.diff; $("confirmBtn").disabled = false;
  $("incident").hidden = false;
}
function dismiss(){ pending = null; $("incident").hidden = true; }

/* ---------- flows ---------- */
async function handleCommand(text, t0){
  stepsEl.innerHTML = ""; rawLog = "";
  setState("thinking","Connecting to MCP server…");
  await ensureSession();
  const tools = await step("tools/list","Discover available tools", async()=>(await rpc("tools/list",{})).tools);
  setState("thinking","Choosing a tool…");
  const route = await step("intent routing","Pick the tool that fits your words", async d=>{
    const r = await post("/alexa/route",{query:text, tools:tools.filter(t=>!CHAINED.includes(t.name))});
    d.textContent = "Picked by "+r.router; return r; });
  const meta = tools.find(t=>t.name===route.tool);
  setState("thinking","Running "+route.tool+"…");
  const data = await callTool(route.tool, route.arguments, meta ? meta.description : "");
  if(route.tool === "triage_failed_ci"){
    setState("thinking","Drafting a fix…");
    const prop = await callTool("propose_fix", {incident:data}, "Draft a minimal fix. Nothing is written yet.");
    pending = prop; showIncident(data, prop);
    updateMetrics(route.tool, data, Math.round(performance.now()-t0));
    await step("awaiting confirmation","Human approval required before any write", async()=>{});
    stepsEl.lastChild.className = "step wait";
    await reply(text, "propose_fix", {...prop, incident:data});
  }else{
    updateMetrics(route.tool, data, Math.round(performance.now()-t0));
    await reply(text, route.tool, data);
  }
}
async function confirmPending(text){
  const p = pending; stepsEl.innerHTML = ""; rawLog = "";
  $("confirmBtn").disabled = true;
  setState("thinking","Opening draft pull request…");
  await ensureSession();
  const res = await callTool("open_fix_pr", {proposal_id:p.proposal_id, confirmed:true}, "Human confirmed · opening a draft PR");
  const out = $("incResult"); out.textContent = "";
  if(res.status === "created" && String(res.pr_url).startsWith("https://github.com/")){
    const a = document.createElement("a"); a.href = res.pr_url; a.target = "_blank"; a.rel = "noopener"; a.textContent = "Draft PR opened: "+res.pr_url; out.appendChild(a);
  }else out.textContent = `Dry run: would open a draft PR on ${res.branch}. Set GITHUB_TOKEN to create it for real.`;
  $("confirmBtn").hidden = true; pending = null;
  await reply(text, "open_fix_pr", res);
}
async function ask(text){
  text = text.trim(); if(!text||busy) return;
  busy = true; $("send").disabled = true; $("q").value="";
  you.textContent = "“"+text+"”"; say.textContent="";
  const t0 = performance.now();
  try{
    if(pending && /^(alexa,?\s*)?(yes|confirm|confirmed|go ahead|do it|open (the )?(pr|pull request))\b/i.test(text)) await confirmPending(text);
    else if(pending && /^(alexa,?\s*)?(no|cancel|dismiss|discard)\b/i.test(text)){ dismiss(); await typeOut("Okay, I discarded the draft. Nothing was changed."); }
    else await handleCommand(text, t0);
  }catch(e){
    say.className="say err"; say.textContent = e.message || "Could not reach the MCP server. Check that FastAPI is running on port 8000.";
    if(!sessionId) setMode(false);
    $("confirmBtn").disabled = false;
  }
  setState("idle","Ready"); busy=false; $("send").disabled=false;
}

/* ---------- events ---------- */
$("send").onclick = ()=>ask($("q").value);
$("q").addEventListener("keydown",e=>{ if(e.key==="Enter") ask($("q").value); });
document.querySelectorAll(".chip").forEach(c=>c.onclick=()=>ask(c.textContent));
$("confirmBtn").onclick = ()=>ask("confirm");
$("dismissBtn").onclick = dismiss;

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if(SR){
  const rec = new SR(); rec.lang="en-US"; rec.interimResults=false;
  rec.onstart=()=>{ $("mic").classList.add("on"); setState("listening", pending ? "Say “confirm” to open the PR…" : "Listening…"); };
  rec.onend=()=>{ $("mic").classList.remove("on"); if(!busy) setState("idle","Ready"); };
  rec.onresult=e=>ask(e.results[0][0].transcript);
  $("mic").onclick=()=>{ speechSynthesis.cancel(); try{rec.start();}catch(_){} };
}else{ $("mic").style.display="none"; }

connect().catch(()=>setMode(false));