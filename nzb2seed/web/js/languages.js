// The languages scene and P2P release names say they are in. Nothing in the name usually
// means English; a language other than English is named with a tag, by the conventions
// release groups follow: FRENCH / TRUEFRENCH / VFF / VOSTFR, GERMAN, SPANISH / CASTELLANO /
// LATiN, iTALiAN, DUTCH / NL, FLEMISH, NORDiC (several Nordic tracks), DANiSH, SWEDiSH,
// NORWEGiAN, FiNNiSH, POLiSH / PL, PORTUGUESE / BRAZiLiAN, and so on. MULTi or DUAL marks
// more than one audio track.
import { norm } from "./util.js";

// [key, what the selector says, the tags that name it]
export const LANGUAGES = [
  ["multi", "Multi / dual audio", ["multi", "multisubs", "dual", "dual-audio", "dualaudio"]],
  ["french", "French", ["french", "truefrench", "vff", "vfq", "vfi", "vf2", "vostfr", "subfrench"]],
  ["german", "German", ["german", "deutsch", "subbed-german"]],
  ["spanish", "Spanish", ["spanish", "castellano", "latino"]],
  ["italian", "Italian", ["italian", "ita"]],
  ["dutch", "Dutch", ["dutch", "nl", "nlsubs", "nlsub"]],
  ["flemish", "Flemish", ["flemish", "vlaams"]],
  ["nordic", "Nordic", ["nordic"]],
  ["danish", "Danish", ["danish", "dk"]],
  ["swedish", "Swedish", ["swedish", "swesub"]],
  ["norwegian", "Norwegian", ["norwegian"]],
  ["finnish", "Finnish", ["finnish"]],
  ["portuguese", "Portuguese / Brazilian", ["portuguese", "brazilian", "pt-br", "ptbr"]],
  ["polish", "Polish", ["polish", "pl", "plsub", "lektor"]],
  ["czech", "Czech", ["czech", "cz"]],
  ["hungarian", "Hungarian", ["hungarian"]],
  ["russian", "Russian", ["russian", "rus"]],
  ["turkish", "Turkish", ["turkish"]],
  ["greek", "Greek", ["greek"]],
  ["hebrew", "Hebrew", ["hebrew"]],
  ["arabic", "Arabic", ["arabic"]],
  ["hindi", "Hindi", ["hindi"]],
  ["japanese", "Japanese", ["japanese", "jpn"]],
  ["korean", "Korean", ["korean"]],
  ["chinese", "Chinese", ["chinese", "chs", "cht", "mandarin", "cantonese"]],
  ["thai", "Thai", ["thai"]],
];

// a tag counts only as a whole dot/space/dash-separated part of the name - and only after the
// year, season or resolution, where groups put it: "The.French.Dispatch.2021..." is in English
const TAG = new Map(LANGUAGES.flatMap(([key, , tags]) => tags.map(t => [t, key])));
const MARK = /^((19|20)\d\d|s\d{1,3}(e\d{1,4})?|\d{3,4}p|complete)$/;

/* the languages a release name names (an empty set: no language tag - usually English) */
export function languagesOf(title) {
  const parts = norm(title).split(/[.\-_\s\[\]()]+/);
  const out = new Set();
  const start = parts.findIndex(p => MARK.test(p));
  for (let i = start < 0 ? 1 : start + 1; i < parts.length; i++) {
    const one = TAG.get(parts[i]);
    const two = i + 1 < parts.length ? TAG.get(parts[i] + "-" + parts[i + 1]) : undefined;   // pt-br, dual-audio
    if (two) out.add(two);
    else if (one) out.add(one);
  }
  return out;
}

/* does a release fit the language chosen: "any", "none" (no tag - usually English), or a key */
export function fitsLanguage(title, choice) {
  if (!choice || choice === "any") return true;
  const langs = languagesOf(title);
  return choice === "none" ? langs.size === 0 : langs.has(choice);
}
