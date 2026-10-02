// ChartSight web UI: a static client of the ChartSight HTTP API (chartsight/api.py).
// Every node is built with textContent via h(); notes are user input and never become markup.
"use strict";

const $ = (sel) => document.querySelector(sel);

const SEGMENTS = {
  COMMUNITY_NA: "Community, non-dual, aged",
  COMMUNITY_ND: "Community, non-dual, disabled",
  COMMUNITY_FBA: "Community, full-benefit dual, aged",
  COMMUNITY_FBD: "Community, full-benefit dual, disabled",
  COMMUNITY_PBA: "Community, partial-benefit dual, aged",
  COMMUNITY_PBD: "Community, partial-benefit dual, disabled",
  INSTITUTIONAL: "Institutional (long-term)",
};

const state = { text: "", result: null, risk: null, view: "original", samples: [], quota: null };

// "Live · Claude Haiku 4.5 · 87 left today" when the server caps live analyses per day.
function liveLabel(label) {
  const q = state.quota;
  return q ? `${label} · ${Math.max(0, q.limit - q.used)} left today` : label;
}

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

// Gap text uses **bold** for the condition name; render that and nothing else.
function rich(text) {
  return String(text)
    .split(/\*\*(.+?)\*\*/g)
    .map((part, i) => (i % 2 ? h("b", null, part) : part));
}

const money = (n) => (n < 0 ? "−$" : "$") + Math.abs(Math.round(n)).toLocaleString("en-US");
const debounce = (fn, ms) => {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
};

function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    return null;
  }
  return null;
}

async function api(path, body) {
  const headers = { "Content-Type": "application/json" };
  const key = storage("cs-key");
  if (key) headers["X-API-Key"] = key;
  const res = await fetch(path, body ? { method: "POST", headers, body: JSON.stringify(body) } : { headers });
  if (res.status === 401) {
    $("#key-banner").hidden = false;
    throw new Error("This server requires an API key.");
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : j.detail?.[0]?.msg || detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

function showError(message) {
  const el = $("#error");
  el.textContent = message || "";
  el.hidden = !message;
}

function setEngine(label, live) {
  const pill = $("#engine");
  pill.classList.toggle("live", live);
  pill.classList.toggle("sample", !live);
  pill.querySelector(".pill-text").textContent = label;
}

// --------------------------------------------------------------------------- //
// Theme
// --------------------------------------------------------------------------- //
function initTheme() {
  $("#theme-toggle").addEventListener("click", () => {
    const current =
      document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    storage("cs-theme", next);
  });
}

// --------------------------------------------------------------------------- //
// Composer
// --------------------------------------------------------------------------- //
function renderSamples() {
  const box = $("#samples");
  box.replaceChildren(
    ...state.samples.map((s, i) =>
      h("button", { class: "chip", type: "button", "data-i": i, onclick: () => pickSample(i) }, s.title)
    )
  );
}

function pickSample(i) {
  $("#note").value = state.samples[i].text;
  document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", Number(c.dataset.i) === i));
}

async function analyze() {
  const text = $("#note").value;
  if (!text.trim()) return;
  const btn = $("#analyze");
  btn.disabled = true;
  btn.classList.add("busy");
  showError("");
  try {
    const result = await api("/v1/analyze", { text });
    state.text = text;
    state.result = result;
    state.risk = result.raf;
    fillAssumptions(result.raf);
    render();
    $("#results").hidden = false;
  } catch (err) {
    showError(`Analysis failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.classList.remove("busy");
  }
}

// --------------------------------------------------------------------------- //
// Rendering
// --------------------------------------------------------------------------- //
function render() {
  const r = state.result;
  const live = r.engine.startsWith("Amazon");
  if (live && state.quota) state.quota.used += 1;
  if (r.quota_exhausted) setEngine("Daily live limit reached", false);
  else setEngine(live ? liveLabel(r.engine.replace("Amazon Bedrock · ", "Live · ")) : "Sample engine", live);
  $("#foot-engine").textContent = live
    ? `Analyzed with ${r.engine}${r.region ? ` · ${r.region}` : ""}`
    : "Sample output from the local keyword engine. Connect AWS for live analysis.";
  renderStats();
  renderNotices();
  renderNote();
  renderCodes();
  renderRisk();
  renderGaps();
}

const realGaps = (gaps) => gaps.filter((g) => !/appears specific/i.test(g));

function renderStats() {
  const r = state.result;
  const risk = state.risk;
  const stat = (k, v, cls) => h("div", { class: `stat ${cls || ""}` }, h("div", { class: "k" }, k), h("div", { class: "v" }, v));
  $("#stats").replaceChildren(
    stat("Risk score (RAF)", risk.payment.toFixed(3), "hero-stat"),
    stat("Annual value", money(risk.annual_dollars)),
    stat("Codes", r.conditions.length),
    stat("Documentation gaps", realGaps(r.gaps).length),
    stat("PHI redacted", r.phi.length)
  );
}

function renderNotices() {
  const r = state.result;
  const notices = [];
  if (r.quota_exhausted) {
    notices.push(h("div", { class: "notice" }, h("b", null, "Today's live-analysis limit is used up, "), "so the free sample engine answered this note. Live analysis resets at midnight UTC."));
  }
  if (r.fallback_reason) {
    notices.push(h("div", { class: "notice" }, h("b", null, "Bedrock was unavailable, "), "so the local sample engine answered. ", h("span", { class: "mono" }, r.fallback_reason)));
  }
  if (r.rejected_codes?.length) {
    notices.push(
      h("div", { class: "notice" },
        h("b", null, `${r.rejected_codes.length} code(s) discarded by the guardrail`),
        " because they are not in the official FY2026 ICD-10-CM code set.",
        h("ul", null, r.rejected_codes.map((c) => h("li", null, h("span", { class: "mono" }, c.code), ` for “${c.text}”`))))
    );
  }
  if (r.ungrounded?.length) {
    notices.push(
      h("div", { class: "notice" },
        h("b", null, `${r.ungrounded.length} condition(s) with no fitting official code`),
        " after grounding (for example, a negated mention).",
        h("ul", null, r.ungrounded.map((c) => h("li", null, `“${c.text}” (first pass `, h("span", { class: "mono" }, c.code), ")"))))
    );
  }
  $("#notices").replaceChildren(...notices);
}

function renderNote() {
  const text = state.text;
  const r = state.result;
  const conds = r.conditions.map((c, i) => ({ ...c, i })).filter((c) => c.end > c.begin);
  const points = new Set([0, text.length]);
  for (const s of [...conds, ...r.phi]) {
    points.add(Math.max(0, Math.min(text.length, s.begin)));
    points.add(Math.max(0, Math.min(text.length, s.end)));
  }
  const cuts = [...points].sort((a, b) => a - b);
  const out = [];
  for (let k = 0; k < cuts.length - 1; k++) {
    const [a, b] = [cuts[k], cuts[k + 1]];
    if (a >= b) continue;
    const phi = r.phi.find((p) => p.begin <= a && b <= p.end);
    const ids = conds.filter((c) => c.begin <= a && b <= c.end).map((c) => c.i);
    if (phi && state.view === "redacted") {
      if (a === phi.begin) out.push(h("span", { class: "phi-chip", title: "Redacted PHI" }, phi.type));
      continue;
    }
    let node = text.slice(a, b);
    if (phi) node = h("span", { class: "phi", title: `PHI · ${phi.type}` }, node);
    if (ids.length) node = h("span", { class: "ev", "data-c": ids.join(" ") }, node);
    out.push(node);
  }
  $("#note-view").replaceChildren(...out);
}

function setActive(ids) {
  const want = new Set(ids.map(String));
  document.querySelectorAll("#note-view .ev").forEach((el) => {
    el.classList.toggle("on", el.dataset.c.split(" ").some((id) => want.has(id)));
  });
  document.querySelectorAll(".code-card").forEach((el) => el.classList.toggle("on", want.has(el.dataset.i)));
}

function renderCodes() {
  const r = state.result;
  $("#codes-meta").textContent = r.grounded ? "Grounded on the FY2026 code set" : "";
  if (!r.conditions.length) {
    $("#codes").replaceChildren(h("p", { class: "empty" }, "No codeable conditions found in this note."));
    return;
  }
  $("#codes").replaceChildren(
    ...r.conditions.map((c, i) => {
      const hccs = c.hcc_v28.length
        ? c.hcc_v28.map((t) => h("span", { class: "tag tag-hcc", title: t.label }, t.hcc.replace("HCC", "HCC ")))
        : [h("span", { class: "tag", title: "Does not risk-adjust under CMS-HCC V28" }, "No HCC")];
      const corrected =
        c.first_pass_code && c.first_pass_code !== c.code
          ? h("span", { class: "corrected", title: "First-pass code replaced by grounding" }, "grounded from ", h("s", { class: "mono" }, c.first_pass_code))
          : null;
      const pct = Math.round(c.confidence * 100);
      return h(
        "div",
        { class: "code-card", "data-i": i, onmouseenter: () => setActive([i]), onmouseleave: () => setActive([]) },
        h("div", { class: "code-top" }, h("span", { class: "code" }, c.code), h("span", { class: "desc" }, c.description)),
        h("div", { class: "quote" }, `“${c.text}”`),
        h(
          "div",
          { class: "code-bottom" },
          hccs,
          corrected,
          h("span", { class: "conf", title: "Model confidence" }, h("span", { class: "conf-track" }, h("span", { class: "conf-fill", style: `display:block;width:${pct}%` })), `${pct}%`)
        ),
        c.candidates?.length
          ? h("details", { class: "cands" }, h("summary", null, `${c.candidates.length} official candidates considered`), h("div", null, c.candidates.map((k) => k.code).join("  ")))
          : null
      );
    })
  );
}

function bar(fraction, neg) {
  const pct = Math.max(0, Math.min(1, fraction)) * 100;
  return h("div", { class: "bar-track" }, h("div", { class: `bar-fill${neg ? " neg" : ""}`, style: `width:${pct}%` }));
}

function renderRisk() {
  const risk = state.risk;
  $("#risk-meta").textContent = `CMS-HCC V28 · ${risk.demographics.label}`;

  const max = Math.max(...risk.terms.map((t) => Math.abs(t.coefficient)), 0.001);
  $("#terms").replaceChildren(
    ...risk.terms.map((t) =>
      h(
        "div",
        { class: `bar-row${t.coefficient < 0 ? " neg" : ""}`, title: `${t.variable}: ${t.label}` },
        h("span", { class: "bar-label" }, h("span", { class: "mono" }, t.variable), t.label),
        h("span", { class: "bar-val" }, t.coefficient.toFixed(3)),
        bar(Math.abs(t.coefficient) / max, t.coefficient < 0)
      )
    ),
    h("div", { class: "total" }, h("span", null, "Raw model score"), h("b", null, risk.raw.toFixed(3))),
    h("div", { class: "total" }, h("span", null, "Payment RAF (÷ 1.067, − 5.9%)"), h("b", null, risk.payment.toFixed(3))),
    h("div", { class: "total" }, h("span", null, `Annual, at ${money(risk.base_rate_pmpm)} PMPM`), h("b", null, money(risk.annual_dollars)))
  );

  const dropped = Object.entries(risk.dropped_by_hierarchy);
  $("#hierarchy").textContent = dropped.map(([lo, hi]) => `${lo} not counted: outranked by ${hi}.`).join(" ");
  $("#hierarchy").hidden = !dropped.length;

  const opps = risk.opportunities;
  if (!opps.length) {
    $("#opps").replaceChildren(h("p", { class: "empty" }, "Every coded condition is already documented at full specificity."));
    return;
  }
  const top = Math.max(...opps.flatMap((o) => o.outcomes.map((x) => x.delta_dollars)), 1);
  $("#opps").replaceChildren(
    ...opps.map((o) => {
      const deltas = o.outcomes.map((x) => x.delta_dollars);
      const best = Math.max(...deltas, 0);
      const worst = Math.min(...deltas, best);
      const range = best > 0 ? (worst === best ? `+${money(best)}/yr` : `${money(worst)} – ${money(best)}/yr`) : "No RAF impact";
      const outcomes = [...o.outcomes].sort((a, b) => b.delta_dollars - a.delta_dollars);
      return h(
        "div",
        { class: "opp" },
        h("div", { class: "opp-head" },
          h("span", { class: "opp-title" }, h("span", { class: "mono" }, o.code), " ", o.description),
          h("span", { class: `opp-range${best > 0 ? "" : " zero"}` }, range)),
        h(
          "div",
          { class: "outcomes" },
          outcomes.map((x) =>
            h(
              "div",
              { class: "outcome", title: `e.g. ${x.example_codes.join(", ")} · ΔRAF ${x.delta_raf.toFixed(3)}` },
              h("span", { class: "lbl" }, x.label.replace(/^HCC\d+\s+/, ""), " ", h("span", { class: "mono" }, x.example_codes.slice(0, 2).join(", "))),
              h("span", { class: "amt" }, x.delta_dollars > 0 ? `+${money(x.delta_dollars)}` : "$0"),
              bar(x.delta_dollars / top)
            )
          )
        )
      );
    })
  );
}

function renderGaps() {
  const gaps = state.result.gaps;
  $("#gaps").replaceChildren(
    ...gaps.map((g) => h("li", { class: /appears specific/i.test(g) ? "ok" : "" }, rich(g)))
  );
}

// --------------------------------------------------------------------------- //
// Risk assumptions: re-score through /v1/raf (deterministic, no model call)
// --------------------------------------------------------------------------- //
function fillAssumptions(risk) {
  $("#segment").value = risk.segment;
  $("#age").value = risk.demographics.age;
  $("#sex").value = risk.demographics.sex === 1 ? "M" : "F";
  $("#base-rate").value = risk.base_rate_pmpm.toFixed(2);
}

// Aged segments have no rates under 65 (younger beneficiaries are entitled by disability),
// so keep the segment consistent with the age as it changes.
const COUNTERPART = { COMMUNITY_NA: "COMMUNITY_ND", COMMUNITY_FBA: "COMMUNITY_FBD", COMMUNITY_PBA: "COMMUNITY_PBD" };
function syncSegmentToAge() {
  const age = Number($("#age").value);
  const seg = $("#segment");
  const aged = Object.keys(COUNTERPART);
  if (age < 65 && aged.includes(seg.value)) seg.value = COUNTERPART[seg.value];
  const back = Object.entries(COUNTERPART).find(([, d]) => d === seg.value);
  if (age >= 65 && back) seg.value = back[0];
}

const rescore = debounce(async () => {
  if (!state.result) return;
  const age = Number($("#age").value);
  const rate = Number($("#base-rate").value);
  if (!Number.isFinite(age) || age < 0 || age > 125 || !(rate > 0)) return;
  try {
    state.risk = await api("/v1/raf", {
      codes: state.result.conditions.map((c) => c.code),
      demographics: { age, sex: $("#sex").value },
      segment: $("#segment").value,
      base_rate_pmpm: rate,
    });
    renderStats();
    renderRisk();
    showError("");
  } catch (err) {
    showError(`Re-scoring failed: ${err.message}`);
  }
}, 250);

// --------------------------------------------------------------------------- //
// Wiring
// --------------------------------------------------------------------------- //
function wire() {
  $("#segment").replaceChildren(...Object.entries(SEGMENTS).map(([v, label]) => h("option", { value: v }, label)));
  for (const id of ["#segment", "#sex", "#base-rate"]) $(id).addEventListener("input", rescore);
  $("#age").addEventListener("input", () => {
    syncSegmentToAge();
    rescore();
  });

  $("#analyze").addEventListener("click", analyze);
  $("#note").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) analyze();
  });
  $("#note").addEventListener("input", () => {
    document.querySelectorAll(".chip.on").forEach((c) => {
      if (state.samples[c.dataset.i].text !== $("#note").value) c.classList.remove("on");
    });
  });

  document.querySelectorAll(".seg button").forEach((b) =>
    b.addEventListener("click", () => {
      state.view = b.dataset.view;
      document.querySelectorAll(".seg button").forEach((x) => {
        x.classList.toggle("on", x === b);
        x.setAttribute("aria-selected", String(x === b));
      });
      if (state.result) renderNote();
    })
  );

  const noteView = $("#note-view");
  noteView.addEventListener("mouseover", (e) => {
    const ev = e.target.closest(".ev");
    setActive(ev ? ev.dataset.c.split(" ") : []);
  });
  noteView.addEventListener("mouseleave", () => setActive([]));
  noteView.addEventListener("click", (e) => {
    const ev = e.target.closest(".ev");
    const card = ev && document.querySelector(`.code-card[data-i="${ev.dataset.c.split(" ")[0]}"]`);
    card?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  $("#key-save").addEventListener("click", () => {
    storage("cs-key", $("#key-input").value.trim() || null);
    $("#key-banner").hidden = true;
    start();
  });
}

async function start() {
  showError("");
  try {
    const health = await api("/health");
    const live = !health.engine.startsWith("local");
    state.quota = health.live_quota || null;
    setEngine(live ? liveLabel(`Live · ${health.model}`) : "Sample engine", live);
    if (health.auth_required && !storage("cs-key")) $("#key-banner").hidden = false;

    state.samples = await api("/v1/samples");
    renderSamples();
    if (state.samples.length && !$("#note").value) pickSample(0);
    // The sample engine is free, so show a result straight away. Live analysis waits for a click.
    if (!live && !state.result) analyze();
  } catch (err) {
    showError(`Couldn't reach the ChartSight API: ${err.message}`);
    setEngine("Offline", false);
  }
}

initTheme();
wire();
start();
