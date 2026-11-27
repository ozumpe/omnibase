// The landing page's only script: reads data/site.json (written by
// scripts/export_site_data.py) and fills the page. Every value goes in through
// textContent or SVG attributes, never as HTML.
"use strict";

const SVG = "http://www.w3.org/2000/svg";

function el(tag, attrs, text) {
  const node = tag.startsWith("svg:")
    ? document.createElementNS(SVG, tag.slice(4))
    : document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}

function setField(name, text) {
  for (const node of document.querySelectorAll(`[data-field="${name}"]`)) node.textContent = text;
}

const price = (v) => (v === null || v === undefined ? "–" : `$${v.toFixed(2)}`);
const dollars = (v) => (v === null || v === undefined ? "–" : `$${v.toFixed(2)}`);
const day = (iso) => new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

function drawChart(weeks) {
  const box = document.getElementById("chart");
  box.replaceChildren();
  const judged = weeks.filter((w) => w.actual !== null);
  if (judged.length < 2) {
    box.append(el("p", { class: "empty" },
      "The chart fills as weeks are scored after the merge. The first ones arrive with the next Friday run."));
    return;
  }
  const W = 860, H = 260, pad = { l: 48, r: 12, t: 12, b: 28 };
  const values = judged.flatMap((w) => [w.predicted, w.actual, w.baseline]).filter((v) => v !== null);
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 0.1;
  const x = (i) => pad.l + (i * (W - pad.l - pad.r)) / (judged.length - 1);
  const y = (v) => pad.t + ((hi + span * 0.1 - v) * (H - pad.t - pad.b)) / (span * 1.2);
  const svg = el("svg:svg", { viewBox: `0 0 ${W} ${H}` });
  for (const tick of [lo, (lo + hi) / 2, hi]) {
    svg.append(el("svg:line", { x1: pad.l, x2: W - pad.r, y1: y(tick), y2: y(tick), stroke: "var(--line)" }));
    svg.append(el("svg:text", { x: pad.l - 6, y: y(tick) + 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, `$${tick.toFixed(2)}`));
  }
  const series = [
    ["baseline", "var(--baseline)", "4 4"],
    ["actual", "var(--actual)", ""],
    ["predicted", "var(--forecast)", ""],
  ];
  for (const [key, colour, dash] of series) {
    const points = judged.map((w, i) => (w[key] === null ? null : `${x(i)},${y(w[key])}`)).filter(Boolean);
    svg.append(el("svg:polyline", { points: points.join(" "), fill: "none", stroke: colour, "stroke-width": 2, "stroke-dasharray": dash }));
  }
  judged.forEach((w, i) => {
    if (i === 0 || i === judged.length - 1 || i % 4 === 0) {
      const anchor = i === 0 ? "start" : i === judged.length - 1 ? "end" : "middle";
      svg.append(el("svg:text", { x: x(i), y: H - 8, "text-anchor": anchor, "font-size": 11, fill: "var(--muted)" }, day(w.next_week)));
    }
  });
  box.append(svg);
  const legend = el("div", { class: "legend" });
  for (const [label, colour] of [["Its forecast", "var(--forecast)"], ["Actual", "var(--actual)"], ["Baseline", "var(--baseline)"]]) {
    const item = el("span");
    item.append(el("i", { style: `background:${colour}` }), document.createTextNode(label));
    legend.append(item);
  }
  box.append(legend);
}

function drawFeed(tried, testRepo) {
  const feed = document.getElementById("feed");
  feed.replaceChildren();
  for (const item of [...tried].reverse()) {
    const pass = item.verdict === "passed every gate" || item.verdict === "merged by a human" || item.verdict === "promoted";
    const fail = item.verdict === "rejected" || item.verdict === "failed to run";
    const li = el("li", { class: pass ? "pass" : fail ? "fail" : "" });
    li.append(el("span", { class: "when" }, new Date(item.ts).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })));
    const body = el("span");
    body.append(el("span", { class: "verdict" }, `${item.verdict[0].toUpperCase()}${item.verdict.slice(1)}. `));
    body.append(document.createTextNode(item.why));
    if (item.pr && /^\d+$/.test(item.pr)) {
      body.append(document.createTextNode(" "));
      body.append(el("a", { href: `${testRepo}/pull/${item.pr}` }, `Read PR ${item.pr}`));
    }
    li.append(body);
    li.append(el("span", { class: "cost" }, item.cost_usd ? dollars(item.cost_usd) : "$0"));
    feed.append(li);
  }
}

async function main() {
  const response = await fetch("data/site.json", { cache: "no-cache" });
  const data = await response.json();
  setField("question", data.question);
  const weeks = data.scoreboard.weeks;
  const last = weeks.length ? weeks[weeks.length - 1] : null;
  setField("predicted", last ? price(last.predicted) : "–");
  setField("baseline", last ? price(last.baseline) : "–");
  setField("actual", last ? price(last.actual) : "–");
  setField("streak", String(data.scoreboard.streak));
  drawChart(weeks);
  const merged = data.tried.find((t) => t.verdict === "merged by a human");
  document.getElementById("exam-note").textContent = merged
    ? `The rule in use passed its exam: it beat "${data.baseline}" by at least ${data.margin_pct}% on held-out history, and a human merged it on ${day(merged.ts)}.`
    : `To be merged, a rule must beat "${data.baseline}" by at least ${data.margin_pct}% on held-out history.`;
  drawFeed(data.tried, data.test_repo_url);
  setField("spent", dollars(data.cost.spent_usd));
  setField("budget", dollars(data.cost.budget_usd));
  setField("per_accepted", dollars(data.cost.per_accepted_usd));
  setField("generated_at", day(data.generated_at));
  setField("code_version", data.code_version || "the engine");
  for (const link of document.querySelectorAll("a[data-href]")) {
    link.href = `${data.repo_url}/blob/develop/${link.dataset.href}`;
  }
}

main().catch(() => setField("question", "The page data could not be loaded."));
