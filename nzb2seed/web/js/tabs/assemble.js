// The Assemble tab: files arranged into a seed, from your drive or found on Usenet. Two parts:
// a .torrent you already have plus folders holding its files (with none, everything comes
// from Usenet) - or, with no torrent at all, a season or a whole series grabbed from Usenet
// (the Seasons part, #assemble/seasons; the old #seasons address leads there).
import { html, useRef, useState } from "../lib.js";
import { api } from "../api.js";
import { assemblePart, go, openJob, toast, useStore } from "../store.js";
import { fileB64 } from "../util.js";
import { Options, useOptions } from "../components/options.js";
import { SeasonsTab } from "./seasons.js";

export function AssembleTab({ active }) {
  const part = useStore(assemblePart);
  const pick = (p) => { assemblePart.set(p); go(p === "seasons" ? "assemble/seasons" : "assemble"); };
  return html`
    <h1>Assemble</h1>
    <p class="lede">Arrange files into a torrent seed - from files already on your hard drive, or found on Usenet.
      Have a .torrent? Point it at the folders holding its files: they are arranged to match it, and whatever they
      lack is downloaded from Usenet (with no folders, all of it is). No torrent? Grab a season, or a whole series,
      from Usenet instead: one release group and resolution, every episode checked, with a report.</p>
    <fieldset class="field radios"><legend>Start from</legend>
      <label class="check"><input type="radio" name="asmpart" checked=${part === "torrent"} onChange=${() => pick("torrent")} />
        <span>A .torrent and the files you have</span></label>
      <label class="check"><input type="radio" name="asmpart" checked=${part === "seasons"} onChange=${() => pick("seasons")} />
        <span>A season or whole series, found on Usenet</span></label>
    </fieldset>
    ${part === "seasons" ? html`<${SeasonsTab} active=${active} />` : html`<${FromTorrent} />`}`;
}

function FromTorrent() {
  const [opts, setOpts] = useOptions(true);
  const [path, setPath] = useState(""), [sources, setSources] = useState(""), [output, setOutput] = useState("");
  const fileRef = useRef(null);
  async function submit(e) {
    e.preventDefault();
    const body = { sources: sources.split("\n"), options: { ...opts, output_dir: output.trim() } };
    const f = fileRef.current.files[0];
    if (f) { body.torrent_b64 = await fileB64(f); body.torrent_name = f.name; }
    else body.torrent_path = path;
    try { const r = await api("/api/assemble", body); openJob(r.id); }
    catch (err) { toast(err.message); }
  }
  return html`
    <h2>Assemble from files you already have</h2>
    <p class="status">For a .torrent you already have and NZB downloads that are already finished. Everything from arranging the files onward runs exactly as in a normal build. The folders are optional: with none, everything is downloaded from Usenet.</p>
    <form class="form" onSubmit=${submit}>
      <label class="field"><span>.torrent file</span>
        <input type="file" ref=${fileRef} accept=".torrent,application/x-bittorrent" />
        <small>Or type the path of a .torrent on the machine running nzb2seed:</small>
        <input type="text" placeholder="D:\\torrents\\Release.Name.torrent" value=${path} onInput=${(e) => setPath(e.target.value)} />
      </label>
      <label class="field"><span>Folders holding the NZB download(s) (optional)</span>
        <textarea rows="4" placeholder="One folder per line, as seen by the machine running nzb2seed"
          value=${sources} onInput=${(e) => setSources(e.target.value)}></textarea>
      </label>
      <label class="field"><span>Put the torrent's files in</span>
        <input type="text" placeholder="Leave empty to use the output folder from Settings, or the parent of the first folder" value=${output} onInput=${(e) => setOutput(e.target.value)} />
      </label>
      <div class="opts actions"><${Options} value=${opts} onChange=${setOpts} assemble /></div>
      <div class="actions"><button class="btn primary">Assemble</button></div>
    </form>`;
}
