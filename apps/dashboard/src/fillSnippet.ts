/** Auto-fill bookmarklet builder (owner decision 2026-09-21).

Login-walled Google Forms (HTTP 401 to every server-side client) can never be
read — and therefore never pre-filled — by the portal backend. The fill step
moves into the owner's own signed-in browser instead: this module builds a
`javascript:` bookmarklet with the owner's profile values baked in (the same
values the Approvals screen already shows). Install once (drag to the
bookmarks bar), click it on any placement form page: text fields fill,
dropdowns/radios/checkboxes pick the exact option, everything else is
reported in an on-page banner. File uploads and judgement calls stay manual
(browsers forbid scripting file inputs; judgements have no safe value).

Security posture: values-only, no API token, no network calls — the snippet
cannot reach the portal API (and never tries). It lives in the owner's own
bookmarks, same exposure as browser history. */

const HINTS: [string, string][] = [
  ["teacher", "\\b(teacher|faculty|presenter|professor|speaker|mentor|conducted by|taken by|presented by|session was taken)\\b"],
  ["company", "\\b(compan(?:y|ies)|organization|organisation|firm|recruiter|which company)\\b"],
  ["registration_number", "\\bregistration\\b|\\bregn\\.?\\b|\\breg\\.?\\s*no"],
  ["roll_number", "\\broll\\b"],
  ["student_id", "\\b(student\\s*id|scholar\\s*(no|number)|uid)\\b"],
  ["email", "\\be-?mail\\b"],
  ["mobile", "\\b(mobile|phone|contact\\s*(no|number)|tel)\\b"],
  ["backlog_count", "\\b(backlogs?|arrears?)\\b"],
  ["cgpa", "\\b(cgpa|sgpa)\\b"],
  ["tenth_percent", "\\b(10th|x(th)?\\s*class|matric)\\b"],
  ["twelfth_percent", "\\b(12th|xii(th)?\\s*class|intermediate)\\b"],
  ["branch", "\\b(course|branch|programme|program|stream|specialization)\\b"],
  ["batch", "\\bbatch\\b"],
  ["full_name", "\\bname\\b"],
];

// NOTE: the runner below is plain browser JS — no ${...} interpolations (the
// only substitution is __V__ via string replace) so TS template literals stay
// inert and the output survives bookmarklet encoding.
const RUNNER = `(()=>{const V=__V__;const H=__H__;const sleep=ms=>new Promise(r=>setTimeout(r,ms));` +
`function setVal(el,v){const p=el.tagName==="TEXTAREA"?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;` +
`const d=Object.getOwnPropertyDescriptor(p,"value");if(d&&d.set){d.set.call(el,v);}` +
`else{el.value=v;}el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}));}` +
`function pick(cands,val){const t=val.trim().toLowerCase();let best=null,bs=0;` +
`for(const c of cands){const s=(c.getAttribute("aria-label")||c.textContent||"").trim();if(!s)continue;` +
`if(s.toLowerCase()===t)return c;const a=new Set(s.toLowerCase().split(/\\s+/));const b=new Set(t.split(/\\s+/));` +
`let n=0;b.forEach(x=>{if(a.has(x))n++;});const sc=n/Math.max(b.size,1);if(sc>bs){best=c;bs=sc;}}` +
`return bs>=0.5?best:null;}` +
`(async()=>{let filled=[],skipped=[];const items=document.querySelectorAll('div[role="listitem"]');` +
`for(const it of items){const h=it.querySelector('[role="heading"]');if(!h)continue;` +
`const q=(h.textContent||"").replace(/\\*\\s*$/,"").trim();if(!q)continue;` +
`const rule=H.find(r=>{try{return new RegExp(r[1],"i").test(q);}catch(e){return false;}});` +
`const val=rule&&V[rule[0]];if(!val){skipped.push(q+" (no value — fill manually)");continue;}` +
`try{const lb=it.querySelector('div[role="listbox"]');` +
`if(lb){lb.click();await sleep(900);const opts=Array.from(document.querySelectorAll('div[role="option"]')).filter(o=>o.offsetParent!==null);` +
`const texts=opts.map(o=>(o.textContent||"").trim());const exact=texts.find(t=>t.toLowerCase()===String(val).trim().toLowerCase());` +
`if(exact){opts[texts.indexOf(exact)].click();filled.push(q);}else{document.body.click();skipped.push(q+" (option not found — pick manually)");}continue;}` +
`const rc=it.querySelectorAll('div[role="radio"],div[role="checkbox"]');` +
`if(rc.length){const c=pick(Array.from(rc),String(val));if(c){c.click();filled.push(q);}else{skipped.push(q+" (option not found — pick manually)");}continue;}` +
`const inp=it.querySelector('input[type="text"],input[type="email"],input[type="date"],textarea');` +
`if(inp){setVal(inp,String(val));filled.push(q);continue;}` +
`skipped.push(q+" (unsupported field — fill manually)");}catch(e){skipped.push(q+" (fill error — do manually)");}}` +
`const d=document.createElement("div");d.style.cssText="position:fixed;top:12px;right:12px;z-index:99999;max-width:340px;background:#fff;border:2px solid #1a6f5c;border-radius:10px;padding:12px;font:13px sans-serif;color:#111;box-shadow:0 4px 16px rgba(0,0,0,.25)";` +
`const t=document.createElement("b");t.textContent="PIA auto-fill: "+filled.length+" filled, "+skipped.length+" left";d.appendChild(t);` +
`skipped.forEach(s=>{const r=document.createElement("div");r.textContent="\\u2718 "+s;d.appendChild(r);});` +
`const f=document.createElement("div");f.style.cssText="margin-top:8px;color:#666";f.textContent="Review everything, then click Submit.";d.appendChild(f);` +
`document.body.appendChild(d);})();})();`;

export function buildFillSnippet(
  values: Record<string, string | number | null | undefined>,
): string {
  const bag: Record<string, string> = {};
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined && String(value).trim()) {
      bag[key] = String(value).trim();
    }
  }
  const code = RUNNER.replace("__V__", JSON.stringify(bag)).replace(
    "__H__",
    JSON.stringify(HINTS),
  );
  // encodeURIComponent (not encodeURI): `#` must not terminate the URL and
  // `&lt;` must survive the href attribute un-decoded until execution.
  return "javascript:" + encodeURIComponent(code);
}
