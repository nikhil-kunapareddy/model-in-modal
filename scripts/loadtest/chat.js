// k6 load test: ramp concurrent streaming chat users against the deployed gateway.
//
//   k6 run --summary-export=scripts/loadtest/results/summary.json scripts/loadtest/chat.js
//   (env: API=<gateway url>  PEAK=80  HOLD=3m)
//
// Needs scripts/loadtest/results/secrets.json from make_tokens.py. Every request
// carries the load-test header, which skips per-session/per-IP limits only: the
// budget check and the global stream cap still apply, so 503 busy at the top of
// the ramp is expected behaviour, not an error.

import http from "k6/http";
import { sleep } from "k6";
import exec from "k6/execution";
import { Counter, Rate, Trend } from "k6/metrics";

const API = (__ENV.API || "https://nikhilkunapareddy--model-in-modal-gateway.us-east.modal.run").replace(/\/+$/, "");
const PEAK = Number(__ENV.PEAK || 80);
const HOLD = __ENV.HOLD || "3m";
const SECRETS = JSON.parse(open("./results/secrets.json"));

export const options = {
  scenarios: {
    chat: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        { duration: "1m", target: Math.min(10, PEAK) },
        { duration: "1m30s", target: Math.min(40, PEAK) },
        { duration: "1m30s", target: PEAK },
        { duration: HOLD, target: PEAK },
        { duration: "30s", target: 0 },
      ],
      gracefulRampDown: "90s",
    },
  },
  setupTimeout: "30m",
  summaryTrendStats: ["avg", "p(50)", "p(95)", "max", "count"],
};

const ttft = new Trend("ttft_ms", true); // gateway-measured: request in -> first token out
const ttfb = new Trend("ttfb_ms", true); // client-measured time to first byte
const tokensPerSec = new Trend("tokens_per_s"); // decode speed of each completed stream
const streamOk = new Rate("stream_ok"); // 200 with a final "done" event
const errors = new Rate("errors"); // anything that is not a completed stream or expected backpressure
const outputTokens = new Counter("output_tokens");
const busy = new Counter("busy_503");
const warming = new Counter("warming_503");
const limited = new Counter("rate_limited_429");
const failures = new Counter("failures");

const PROMPTS = [
  "Explain how a refrigerator keeps food cold, in about 150 words.",
  "Describe the water cycle for a ten-year-old, in about 150 words.",
  "Give five practical tips for learning a new language, one short paragraph each.",
  "Explain what an API is, with a restaurant analogy, in about 150 words.",
  "Summarize the causes of the seasons on Earth in about 120 words.",
  "Write a short story (about 150 words) about a robot who learns to paint.",
  "Explain the difference between RAM and storage in about 120 words.",
  "List the planets of the solar system with one interesting fact each.",
];

function headers(token) {
  return {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    "X-Loadtest-Key": SECRETS.loadtest_key,
  };
}

function parseJson(text) {
  try { return JSON.parse(text); } catch (e) { return {}; }
}

function sseEvents(body) {
  return String(body || "")
    .split(/\n\n/)
    .filter((block) => block.startsWith("data:"))
    .map((block) => parseJson(block.slice(5)));
}

function chat(token, content, tags) {
  return http.post(`${API}/api/chat`, JSON.stringify({ messages: [{ role: "user", content }] }),
    { headers: headers(token), timeout: "360s", tags });
}

// Wake the model before any load: retry until one chat streams.
export function setup() {
  const started = Date.now();
  for (;;) {
    const r = chat(SECRETS.tokens[0], "Say ok.", { phase: "setup" });
    const body = parseJson(r.body);
    if (r.status === 200) {
      console.log(`model warm ${Math.round((Date.now() - started) / 1000)}s after start`);
      return { warmAfterS: (Date.now() - started) / 1000 };
    }
    if (r.status === 503 && body.error === "credits_exhausted") throw new Error("credits exhausted");
    if (Date.now() - started > 25 * 60 * 1000) throw new Error("model never warmed up");
    sleep(r.status === 503 && body.error === "warming_up" ? body.retry_after || 10 : 5);
  }
}

export default function () {
  const token = SECRETS.tokens[(exec.vu.idInTest - 1) % SECRETS.tokens.length];
  const prompt = PROMPTS[Math.floor(Math.random() * PROMPTS.length)];
  const r = chat(token, prompt, { phase: "load" });

  if (r.status === 200) {
    const events = sseEvents(r.body);
    const done = events.find((e) => e.type === "done");
    ttfb.add(r.timings.waiting);
    streamOk.add(!!done);
    errors.add(!done);
    if (!done) {
      failures.add(1);
    } else {
      outputTokens.add(done.output_tokens || 0);
      if (typeof done.ttft_ms === "number") ttft.add(done.ttft_ms);
      const decodeS = (r.timings.duration - (done.ttft_ms ?? r.timings.waiting)) / 1000;
      if (done.output_tokens > 1 && decodeS > 0) tokensPerSec.add(done.output_tokens / decodeS);
    }
  } else {
    const body = parseJson(r.body);
    streamOk.add(false);
    if (r.status === 503 && body.error === "busy") { busy.add(1); errors.add(false); }
    else if (r.status === 503 && body.error === "warming_up") { warming.add(1); errors.add(false); }
    else if (r.status === 429) { limited.add(1); errors.add(true); } // must not happen with the bypass
    else { failures.add(1); errors.add(true); }
    sleep(body.retry_after || 2);
  }
  sleep(1 + Math.random() * 2); // think time between messages
}
