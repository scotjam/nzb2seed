// The Assemble tab: a .torrent you already have plus NZB downloads that already finished -
// or none: with no folders, everything comes from Usenet.
import { html, useRef, useState } from "../lib.js";
import { api } from "../api.js";
import { openJob, toast } from "../store.js";
import { fileB64 } from "../util.js";
import { Options, useOptions } from "../components/options.js";

export function AssembleTab() {
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
    <h1>Assemble from files you already have</h1>
    <p class="lede">For a .torrent you already have and NZB downloads that are already finished. Everything from arranging the files onward runs exactly as in a normal build. The folders are optional: with none, everything is downloaded from Usenet.</p>
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
