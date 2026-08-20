// Execute the dashboard script against a minimal DOM stub. Syntax checking
// cannot catch a temporal-dead-zone reference, which throws at load and kills
// every initializer after it.
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const src = html.split(/<script>\n/).pop().split(/<\/script>/)[0];

const mkEl = () => {
  const el = {
    style: {}, dataset: {}, classList: { add(){}, remove(){}, toggle(){} },
    children: [], options: [], selectedOptions: [], value: "", textContent: "",
    innerHTML: "", checked: false, hidden: false, disabled: false,
    appendChild(){}, removeChild(){}, setAttribute(){}, getAttribute(){ return null; },
    addEventListener(){}, removeEventListener(){}, querySelector(){ return mkEl(); },
    querySelectorAll(){ return []; }, closest(){ return mkEl(); }, focus(){},
    setSelectionRange(){}, showModal(){}, close(){}, remove(){}, insertAdjacentHTML(){},
    getBoundingClientRect(){ return { width: 0, height: 0, top: 0, left: 0 }; },
  };
  return el;
};
const store = {};
global.localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
global.document = {
  getElementById: () => mkEl(), querySelector: () => mkEl(), querySelectorAll: () => [],
  createElement: () => mkEl(), addEventListener(){}, body: mkEl(),
  documentElement: mkEl(), head: mkEl(), title: "",
};
global.window = { location: { search: "", hash: "", href: "http://localhost/" },
                  addEventListener(){}, matchMedia: () => ({ matches: false, addEventListener(){} }),
                  localStorage: global.localStorage, setTimeout, clearTimeout };
global.navigator = { clipboard: { writeText: async () => {} }, userAgent: "node" };
global.fetch = async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => "" });
global.Chart = function () { return { destroy(){}, update(){}, data: {}, options: {} }; };
global.Chart.register = () => {};
global.EventSource = function () { return { addEventListener(){}, close(){} }; };
global.requestAnimationFrame = cb => setTimeout(cb, 0);
global.getComputedStyle = () => ({ getPropertyValue: () => "" });
global.location = global.window.location;
global.history = { pushState(){}, replaceState(){} };
global.URLSearchParams = URLSearchParams;
global.alert = () => {};
global.confirm = () => false;
global.CustomEvent = function () {};
global.Blob = function () {};
global.URL = URL;

try {
  new Function(src)();
  console.log("RUNTIME OK — top-level script executed without throwing");
} catch (e) {
  console.error("RUNTIME FAILURE:", e.constructor.name + ":", e.message);
  process.exit(1);
}
