// node --test tests/   — browser adapter + chain orchestration, with a fake environment (no DOM, no network).
const test = require("node:test");
const assert = require("node:assert");
const SA = require("../ads/adapters.js");

function fakeEnv(win, opts) {
  opts = opts || {};
  let t = 0; const timers = [];
  const env = {
    win, now: () => t,
    setTimeout: (f, ms) => { const h = { f, at: t + ms, off: false }; timers.push(h); return h; },
    clearTimeout: (h) => { if (h) h.off = true; },
    addScript: (url, attrs, ok, err) => { opts.scriptFails ? err() : (opts.onScript && opts.onScript(win), ok()); },
    ensureContainer: () => {},
    advance: async (ms) => { const end = t + ms; while (t < end) { t = Math.min(end, t + 100); timers.slice().filter(h => !h.off && h.at <= t).forEach(h => { h.off = true; h.f(); }); await new Promise(r => setImmediate(r)); } },
  };
  return env;
}
const hooks = () => ({ shownCalls: 0, onShown() { this.shownCalls++; } });

test("AdsGram SDK blocked -> failed/blocked, nothing shown", async () => {
  const env = fakeEnv({}, { scriptFails: true });
  const r = await SA.createAdapters(env).adsgram.run({ unit_id: "58138", sdk: "x" }, hooks());
  assert.deepStrictEqual([r.status, r.reason, r.shown], ["failed", "blocked", false]);
});

test("AdsGram SDK never defines window.Adsgram -> timeout", async () => {
  const env = fakeEnv({}, {});
  const p = SA.createAdapters(env).adsgram.run({ unit_id: "58138", sdk: "x" }, hooks());
  await env.advance(11000);
  const r = await p; assert.deepStrictEqual([r.status, r.reason], ["failed", "timeout"]);
});

function adsgramWin(showImpl) {
  const ls = {};
  return { Adsgram: { init: (o) => { assert.strictEqual(o.blockId, "58138"); return {
    addEventListener: (e, f) => { ls[e] = f; }, removeEventListener() {}, show: () => showImpl(ls) }; } } };
}

test("AdsGram completed (reward event + done:true) -> completed", async () => {
  const env = fakeEnv(adsgramWin(async (ls) => { ls.onStart(); ls.onReward(); return { done: true }; }));
  const r = await SA.createAdapters(env).adsgram.run({ unit_id: 58138 }, hooks());
  assert.deepStrictEqual([r.status, r.shown], ["completed", true]);
});

test("AdsGram interstitial (complete but no reward) is NOT a reward", async () => {
  const env = fakeEnv(adsgramWin(async (ls) => { ls.onStart(); ls.onComplete(); return { done: true }; }));
  const r = await SA.createAdapters(env).adsgram.run({ unit_id: 58138 }, hooks());
  assert.strictEqual(r.status, "failed");
});

test("AdsGram skipped/closed -> skipped", async () => {
  const env = fakeEnv(adsgramWin(async (ls) => { ls.onStart(); ls.onSkip(); throw { done: false, state: "playing", error: false }; }));
  const r = await SA.createAdapters(env).adsgram.run({ unit_id: 58138 }, hooks());
  assert.strictEqual(r.status, "skipped");
});

test("AdsGram no banner -> no_fill", async () => {
  const env = fakeEnv(adsgramWin(async (ls) => { ls.onBannerNotFound(); throw { done: false, error: true }; }));
  const r = await SA.createAdapters(env).adsgram.run({ unit_id: 58138 }, hooks());
  assert.deepStrictEqual([r.status, r.reason, r.shown], ["failed", "no_fill", false]);
});

test("Tads widget unavailable -> blocked", async () => {
  const r = await SA.createAdapters(fakeEnv({}, { scriptFails: true })).tads.run({ unit_id: "13070", sdk: "x" }, hooks());
  assert.deepStrictEqual([r.status, r.reason], ["failed", "blocked"]);
});

test("Tads onAdsNotFound -> no_fill; onShowReward -> completed", async () => {
  for (const [mode, want] of [["none", "failed"], ["reward", "completed"]]) {
    let cfgRef;
    const win = { tads: { init: (c) => { cfgRef = c; return { showAd: async () => { setImmediate(() => mode === "reward" ? c.onShowReward("ad1") : c.onAdsNotFound()); } }; } } };
    const r = await SA.createAdapters(fakeEnv(win)).tads.run({ unit_id: "13070" }, hooks());
    assert.strictEqual(r.status, want);
    if (want === "failed") assert.strictEqual(r.reason, "no_fill");
  }
});

test("Tads click is not a completed view (onClickReward ignored)", async () => {
  let c0;
  const win = { tads: { init: (c) => { c0 = c; return { showAd: async () => { setImmediate(() => { c.onClickReward("x"); c.onAdsNotFound(); }); } }; } } };
  const r = await SA.createAdapters(fakeEnv(win)).tads.run({ unit_id: "13070" }, hooks());
  assert.strictEqual(r.status, "failed");
});

test("runChain: first provider fails undisplayed -> server hands next; both fail -> exhausted", async () => {
  const calls = [];
  const adapters = { adsgram: { run: async () => ({ status: "failed", reason: "no_fill", shown: false }) },
                     tads: { run: async () => ({ status: "failed", reason: "timeout", shown: false }) } };
  const api = async (path, body) => { calls.push([path, body.event]); return body.event === "fail" && body.ad_id === "a1" ? { ok: true, next: { ok: true, network: "tads", ad_id: "a2" } } : { ok: true, exhausted: true }; };
  const out = await SA.runChain({ ok: true, network: "adsgram", ad_id: "a1" }, adapters, api, null);
  assert.strictEqual(out.kind, "exhausted"); assert.strictEqual(out.displayed, false);
  assert.deepStrictEqual(calls.map(c => c[1]), ["fail", "fail"]);
});

test("runChain: displayed-then-failed never chains a second ad", async () => {
  const adapters = { adsgram: { run: async (c, h) => { h.onShown(); return { status: "failed", reason: "error", shown: true }; } } };
  const api = async () => ({ ok: true, exhausted: true });
  const out = await SA.runChain({ network: "adsgram", ad_id: "a1" }, adapters, api, null);
  assert.deepStrictEqual([out.kind, out.displayed], ["exhausted", true]);
});

test("runChain: skipped reports skip and stops", async () => {
  const seen = [];
  const out = await SA.runChain({ network: "adsgram", ad_id: "a1" }, { adsgram: { run: async () => ({ status: "skipped", shown: true }) } },
    async (p, b) => { seen.push(b.event); return { ok: true }; }, null);
  assert.strictEqual(out.kind, "skipped"); assert.ok(seen.includes("skip"));
});

test("claim: polls while server says wait, then returns server verdict", async () => {
  let n = 0;
  const r = await SA.claim({ ad_id: "a" }, {}, async () => (++n < 3 ? { ok: true, wait: 1 } : { ok: true, delivered: true }), async () => {}, 10);
  assert.strictEqual(n, 3); assert.strictEqual(r.delivered, true);
});

test("claim: gives up after max tries (no infinite loading)", async () => {
  const r = await SA.claim({ ad_id: "a" }, {}, async () => ({ ok: true, wait: 1 }), async () => {}, 4);
  assert.strictEqual(r.ok, false);
});
