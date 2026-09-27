// Loads the classic page or the new app into jsdom, against the fake server, with the
// helpers the scenarios use to act on it by what is written on screen.
import { JSDOM, VirtualConsole } from "jsdom";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { join } from "node:path";
import { makeServer } from "./fake.mjs";

export async function load(ui, web, tab, settingsPayload) {
  const server = makeServer(settingsPayload);
  const confirms = [], errors = [];
  let inflight = 0;
  const fetch = async (...a) => { inflight++; try { return await server.fetch(...a); } finally { inflight--; } };
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", (e) => { if (!/Not implemented/.test(e.message)) errors.push(e.message); });
  virtualConsole.on("error", (e) => errors.push(String(e)));
  const stub = (w) => {
    w.fetch = fetch;
    w.confirm = (msg) => { confirms.push(msg); return true; };
    w.alert = () => {};
    w.scrollTo = () => {};
    w.Element.prototype.scrollIntoView = function () {};
    // a drawing context that draws nothing: jsdom has no canvas
    w.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => {}, set: () => true });
    w.EventSource = undefined;                       // the new app falls back to fetching
    w.addEventListener("error", (e) => errors.push(e.message));
  };
  // the classic page is kept only here, as the reference the new one is compared with
  const file = ui === "classic" ? join(import.meta.dirname, "baseline", "classic.html") : join(web, "index.html");
  const dom = new JSDOM(readFileSync(file, "utf8"), {
    url: "http://nzb2seed.test/#" + tab, pretendToBeVisual: true, virtualConsole,
    runScripts: ui === "classic" ? "dangerously" : undefined, beforeParse: stub,
  });
  const w = dom.window;
  if (ui === "new") {
    // jsdom cannot run <script type="module">: the app's modules are imported here, with
    // the page's window as their globals
    for (const k of ["window", "document", "location", "localStorage", "navigator", "getComputedStyle",
                     "requestAnimationFrame", "cancelAnimationFrame", "confirm", "alert", "fetch", "EventSource",
                     "HTMLElement", "Node", "Event", "KeyboardEvent", "MouseEvent", "SVGElement", "Text"]) {
      Object.defineProperty(globalThis, k, { value: w[k], configurable: true, writable: true });
    }
    await import(pathToFileURL(join(web, "js", "app.js")).href);
  }
  const doc = w.document;

  const settle = async () => {
    for (let quiet = 0, n = 0; quiet < 4 && n < 200; n++) {
      await new Promise(r => setTimeout(r, 15));
      quiet = inflight ? 0 : quiet + 1;
    }
  };
  const hidden = (el) => {
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      if (e.classList.contains("hidden") || e.hasAttribute("hidden")) return true;
      if (e.tagName === "DETAILS" && !e.open && e !== el && !e.firstElementChild?.contains(el)) return true;
    }
    return false;
  };
  const text = (el) => {
    if (!el) return "";
    const out = [];
    const walk = (n) => {
      if (n.nodeType === 3) { out.push(n.nodeValue); return; }
      if (n.nodeType !== 1 || hidden(n) || ["SCRIPT", "STYLE", "TEMPLATE"].includes(n.tagName)) return;
      if (n.classList.contains("buildbar") && n !== el) return;     // compared on its own
      if (n.classList.contains("groupchips") && n !== el) return;   // new: tested on its own
      if (n.tagName === "INPUT" && ["text", "number", "password"].includes(n.type)) out.push(` [${n.value}] `);
      if (n.tagName === "SELECT") { out.push(` [${n.options[n.selectedIndex]?.textContent ?? ""}] `); return; }
      if (n.tagName === "INPUT" && n.type === "checkbox") out.push(n.checked ? " [x] " : " [ ] ");
      for (const c of n.childNodes) walk(c);
      if (/^(DIV|P|LI|TR|H\d|LABEL|BUTTON|SECTION|FIELDSET|LEGEND|SUMMARY|PRE|TD|TH|SMALL|SPAN|B|A|CODE)$/.test(n.tagName)) out.push(" ");
    };
    walk(el);
    return out.join("").replace(/\s+/g, " ").trim();
  };
  const all = (sel) => [...doc.querySelectorAll(sel)].filter(e => !hidden(e));
  const view = () => doc.querySelector(`#view-${w.location.hash.slice(1) || "build"}`);
  /* the visible control whose own text (or aria-label / placeholder) is this */
  const find = (label, sel = "button, [role=button], a, summary, label, input, select, textarea, span.btn") => {
    const want = label.toLowerCase();
    // a form control is named by its aria-label, its placeholder, or the label around it
    // (without the values of the controls inside that label) - as a screen reader names it
    const labelOf = (e) => {
      const l = e.closest("label");
      if (!l) return "";
      const c = l.cloneNode(true);
      c.querySelectorAll("input, select, textarea, small").forEach(x => x.remove());
      return c.textContent.replace(/\s+/g, " ").trim();
    };
    const own = (e) => (e.getAttribute("aria-label") || e.getAttribute("placeholder")
      || (/^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName) ? labelOf(e) : text(e))).toLowerCase();
    const hits = all(sel).filter(e => own(e) === want);
    const loose = hits.length ? hits : all(sel).filter(e => own(e).startsWith(want));
    if (!loose.length) throw new Error(`${ui}: nothing on screen says "${label}"`);
    return loose[0];
  };
  const act = {
    click: async (label, sel) => { find(label, sel).click(); await settle(); },
    type: async (label, value) => {
      const el = find(label, "input, textarea");
      el.value = value; el.dispatchEvent(new w.Event("input", { bubbles: true }));
      el.dispatchEvent(new w.Event("change", { bubbles: true })); await settle();
    },
    choose: async (label, value) => {
      const el = find(label, "select");
      el.value = value; el.dispatchEvent(new w.Event("change", { bubbles: true })); await settle();
    },
    submit: async (label) => {
      const el = find(label, "input");
      el.form.dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true })); await settle();
    },
    go: async (tab) => { w.location.hash = "#" + tab; await settle(); },
    find, all, text, view, settle,
  };
  await settle();
  return { w, doc, act, calls: server.calls, confirms, errors, server };
}
