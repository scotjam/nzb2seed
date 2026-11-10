// How much a tracker lets you download, as two dropdowns - one for the percentage, one for
// the megabytes - each: None allowed (0), Full downloads allowed ("full"), Not specified
// (left out) or Custom (a number above 0). The same rules as config.tracker_limit.
import { html } from "../lib.js";

export const MODES = [["custom", "Custom"], ["none", "None allowed"], ["full", "Full downloads allowed"], ["unset", "Not specified"]];

/* the dropdown a stored value shows - a tracker never set: Not specified in both */
export function modeOf(v) {
  if (v === undefined || v === null) return "unset";
  if (v === "") return "custom";                          // Custom chosen, no number yet
  if (String(v).toLowerCase() === "full") return "full";
  if (Number(v) === 0) return "none";
  return "custom";
}

/* the value stored for a dropdown choice (and the number typed for Custom) */
export function valueFor(mode, num) {
  if (mode === "none") return 0;
  if (mode === "full") return "full";
  if (mode === "unset") return null;
  return num === "" || num == null ? "" : Number(num);
}

/* N(one), F(ull), U(nspecified), C(ustom), E (Custom, no number yet), X (Custom, not above 0) */
function kind(v, isPct) {
  const m = modeOf(v);
  if (m === "none") return "N";
  if (m === "full") return "F";
  if (m === "unset") return "U";
  if (v === "") return "E";
  const n = Number(v);
  if (isPct && n >= 100) return "H";
  return n > 0 && Number.isFinite(n) ? "C" : "X";
}

/* What would not behave as it reads - "" when the pair is one of the usual ones: Full or a
   custom % with MB not specified or custom; % not specified with custom or full MB; full
   in both; none
   allowed in one or both, the other not specified; both not specified. */
export function limitWarning(pct, mb) {
  const p = kind(pct, true), m = kind(mb, false);
  if (p === "H") return "A custom percentage must be below 100 - choose Full downloads allowed for the whole torrent.";
  if (p === "X") return "A custom percentage must be above 0 - choose None allowed if it allows nothing.";
  if (m === "X") return "Custom megabytes must be above 0 - choose None allowed if it allows nothing.";
  if (p === "E") return "Custom % cannot be blank: enter a percentage, or choose Not specified, None allowed or Full downloads allowed.";
  if (m === "E") return "Custom MB cannot be blank: enter megabytes, or choose Not specified, None allowed or Full downloads allowed.";
  const P = p, M = m;
  if (P === "U" && M === "U") return "";                          // not set: its jobs ask
  if ((P === "C" || P === "F") && M === "N")
    return "The percentage would be ignored: None allowed in MB means nothing is downloaded. Choose Not specified for MB - or None allowed for % too, if it allows nothing.";
  if (P === "N" && (M === "C" || M === "F"))
    return "The megabytes would be ignored: None allowed in % means nothing is downloaded. Choose Not specified for % - or None allowed for MB too, if it allows nothing.";
  if (P === "C" && M === "F") return `This only lets ${pct}% of the torrent be downloaded - Full downloads allowed in MB does not raise it. Choose Not specified for MB.`;
  return "";
}

/* What a pair that behaves as it reads does - for the note beside it ("" for one that
   warns: limitWarning says why). Each limit keeps 10% of it free (see config.HEADROOM). */
export function limitInfo(pct, mb) {
  if (limitWarning(pct, mb)) return "";
  const p = kind(pct, true), m = kind(mb, false);
  const free = " - 10% of the limit is kept free in case a piece needs to be downloaded twice";
  if (p === "U" && m === "U")
    return "Not set: nothing is downloaded from it automatically, and a build that stops short on it asks you how much it allows.";
  if (p === "N" || m === "N")
    return "None allowed: nothing is ever downloaded from it automatically, and its builds do not ask. Override on a job still can.";
  if ((p === "F" || p === "U") && (m === "F" || m === "U"))
    return "Full downloads allowed: a build that stops short on it can download whatever is missing, however much.";
  if (p === "C" && m === "U") return `Up to ${pct}% of the torrent can be downloaded from it${free}.`;
  if ((p === "U" || p === "F") && m === "C")
    return `Up to ${mb} MB per torrent can be downloaded from it, whatever share of the torrent that is${free}.`;
  if (p === "C" && m === "C")
    return `Up to ${pct}% of the torrent or ${mb} MB, whichever is less, can be downloaded from it${free}.`;
  return "";
}

/* the note beside a pair: what it does, or a warning when it would not behave as it reads */
export function LimitNote({ pct, mb }) {
  const warning = limitWarning(pct, mb);
  return warning ? html`<small class="limitwarn" role="alert">${warning}</small>`
    : html`<small class="limitinfo">${limitInfo(pct, mb)}</small>`;
}

/* one dropdown, with the number box beside it while Custom is chosen */
export function LimitPick({ value, isPct, where, onChange }) {
  const mode = modeOf(value);
  const unit = isPct ? "%" : "MB";
  const num = mode === "custom" && value !== undefined && value !== "" ? String(value) : "";
  return html`<span class="limitpick">
    <select aria-label=${`${unit} ${where} allows`} value=${mode}
      onChange=${(e) => onChange(valueFor(e.target.value, e.target.value === "custom" ? num : ""))}>
      ${MODES.map(([k, label]) => html`<option value=${k}>${label}</option>`)}
    </select>
    ${mode === "custom" && html`<input type="number" min=${isPct ? "0.1" : "1"} max=${isPct ? "99.9" : undefined}
      step=${isPct ? "0.1" : "1"} placeholder=${unit} value=${num} aria-label=${`Custom ${unit} ${where} allows`}
      onInput=${(e) => onChange(valueFor("custom", e.target.value))} />`}
  </span>`;
}
