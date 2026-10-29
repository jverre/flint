import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { fileActions, useFilePreview, useFiles } from "../../api/hooks";
import type { FileEntry } from "../../api/types";
import { Dialog, Empty, useToast } from "../../components/ui";
import { errMsg, NeedsRunning, type TabProps } from "./common";

const CAP = 256 * 1024;
const join = (dir: string, name: string) => (dir === "/" ? "" : dir.replace(/\/$/, "")) + "/" + name;
const sizeText = (n: number) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MiB" : n >= 1024 ? (n / 1024).toFixed(1) + " KiB" : n + " B");

export default function Files({ vm, running, onResume, resuming }: TabProps) {
  const [path, setPath] = useState("/");
  const [sel, setSel] = useState<FileEntry | null>(null);
  const [del, setDel] = useState<FileEntry | null>(null);
  const [mk, setMk] = useState(false), [folder, setFolder] = useState("");
  const [busy, setBusy] = useState(false);
  const up = useRef<HTMLInputElement>(null);
  const toast = useToast(), qc = useQueryClient();
  const files = useFiles(vm.id, path, running);
  const selPath = sel ? join(path, sel.name) : "";

  const prev = useFilePreview(vm.id, selPath, sel?.size ?? 0, CAP, !!sel && !sel.is_dir && running);

  if (!running) return <NeedsRunning vm={vm} what="The file browser" onResume={onResume} resuming={resuming} />;

  const refresh = () => qc.invalidateQueries({ queryKey: ["files", vm.id, path] });
  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); toast(label); refresh(); return true; }
    catch (e) { toast(errMsg(e), { kind: "err" }); return false; }
    finally { setBusy(false); }
  };
  const download = () => sel && run(`Downloaded ${sel.name}.`, async () => {
    const b = await fileActions.download(vm.id, selPath);
    const a = document.createElement("a"); a.href = URL.createObjectURL(b); a.download = sel.name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  });
  const crumbs = path.split("/").filter(Boolean);
  const entries = [...(files.data ?? [])].sort((a, b) => Number(b.is_dir) - Number(a.is_dir) || a.name.localeCompare(b.name));
  const cd = (p: string) => { setPath(p); setSel(null); };

  return (
    <div className="vm-files">
      <div className="vm-fbar">
        <nav className="vm-crumbs" aria-label="Path">
          <button className="vm-link" onClick={() => cd("/")}>/</button>
          {crumbs.map((c, i) => <span key={i}><button className="vm-link" onClick={() => cd("/" + crumbs.slice(0, i + 1).join("/"))}>{c}</button>{i < crumbs.length - 1 && <span className="dim"> / </span>}</span>)}
        </nav>
        <span className="grow" />
        <input ref={up} type="file" hidden onChange={async (e) => {
          const f = e.target.files?.[0]; e.target.value = "";
          if (f) await run(`Uploaded ${f.name}.`, () => fileActions.upload(vm.id, join(path, f.name), f));
        }} />
        <button className="btn sm" disabled={busy} onClick={() => up.current?.click()}>Upload</button>
        <button className="btn sm" disabled={busy} onClick={() => { setFolder(""); setMk(true); }}>New folder</button>
      </div>
      <div className="vm-fs">
        <div className="vm-ftree">
          {files.isLoading ? <p className="vm-note">Loading…</p>
            : files.isError ? <div className="vm-note errc">{errMsg(files.error)} <button className="btn sm" onClick={() => files.refetch()}>Retry</button>{path !== "/" && <button className="btn sm" onClick={() => cd("/")}>Back to /</button>}</div>
            : (
              <table className="tbl vm-tbl" aria-label="Files">
                <thead><tr><th scope="col">Name</th><th scope="col" className="num">Size</th><th scope="col">Mode</th></tr></thead>
                <tbody>
                  {path !== "/" && <tr><td colSpan={3}><button className="vm-link" onClick={() => cd(path.replace(/\/[^/]+\/?$/, "") || "/")}>..</button></td></tr>}
                  {entries.map((f) => (
                    <tr key={f.name} className={sel?.name === f.name ? "sel" : ""}>
                      <td><button className={`vm-link vm-fname${f.is_dir ? " dir" : ""}`} onClick={() => (f.is_dir ? cd(join(path, f.name)) : setSel(f))}>{f.name}{f.is_dir ? "/" : ""}</button></td>
                      <td className="num dim">{f.is_dir ? "" : sizeText(f.size)}</td><td className="dim">{f.mode}</td>
                    </tr>
                  ))}
                  {!entries.length && <tr><td colSpan={3} className="dim">This directory is empty.</td></tr>}
                </tbody>
              </table>
            )}
        </div>
        <div className="vm-pv">
          {!sel ? <Empty title="No file selected">Pick a file to preview its text.</Empty> : (
            <>
              <div className="vm-pvh">
                <b title={selPath}>{selPath}</b><span className="dim">{sizeText(sel.size)}</span><span className="grow" />
                <button className="btn sm" disabled={busy} onClick={download}>Download</button>
                <button className="btn sm danger" disabled={busy} onClick={() => setDel(sel)}>Delete</button>
              </div>
              <pre className="vm-code">{prev.isLoading ? "Loading…" : prev.isError ? errMsg(prev.error) : prev.data?.binary ? "Binary file: preview not available. Use Download." : prev.data?.text}</pre>
              {prev.data?.truncated && !prev.data.binary && <p className="vm-note dim">Preview is capped at 256 KB. Download to get the whole file.</p>}
            </>
          )}
        </div>
      </div>
      <Dialog open={mk} onClose={() => setMk(false)} title="New folder" sub={`In ${path}`} width={440}
        actions={[{ label: "Cancel", kind: "ghost" }, { label: "Create", kind: "pri", disabled: !folder.trim() || folder.includes("/"), onClick: () => run(`Created ${folder}.`, () => fileActions.mkdir(vm.id, join(path, folder.trim()))) }]}>
        <input className="vm-input" autoFocus placeholder="folder name" aria-label="Folder name" value={folder} onChange={(e) => setFolder(e.target.value)} />
      </Dialog>
      <Dialog open={!!del} onClose={() => setDel(null)} title={`Delete ${del?.name ?? ""}?`} sub="This removes it from the VM's disk. It can't be undone." width={460}
        actions={[{ label: "Cancel", kind: "ghost" }, { label: "Delete", kind: "danger", onClick: async () => {
          if (!del) return;
          const ok = await run(`Deleted ${del.name}.`, () => fileActions.remove(vm.id, join(path, del.name), del.is_dir));
          if (ok) setSel(null);
          return ok;
        } }]} />
    </div>
  );
}
