import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { invalidateVMs, vmActions } from "../../api/hooks";
import { useToast } from "../../components/ui";
import { errMsg, fmtDuration, Head, NetworkView, type TabProps } from "./common";

export default function Settings({ vm }: TabProps) {
  const toast = useToast(), qc = useQueryClient();
  const [secs, setSecs] = useState(String(vm.timeoutSeconds ?? ""));
  const [policy, setPolicy] = useState<"kill" | "pause">(vm.timeoutPolicy ?? "kill");
  const [busy, setBusy] = useState(false);
  useEffect(() => { setSecs(String(vm.timeoutSeconds ?? "")); setPolicy(vm.timeoutPolicy ?? "kill"); }, [vm.timeoutSeconds, vm.timeoutPolicy]);
  const n = Number(secs), valid = Number.isInteger(n) && n > 0;
  const save = async () => {
    setBusy(true);
    try { await vmActions.setTimeout(vm.id, n, policy); toast(`Auto-${policy} set to ${fmtDuration(n)}.`); invalidateVMs(qc, vm.id); }
    catch (e) { toast(errMsg(e), { kind: "err" }); }
    finally { setBusy(false); }
  };
  return (
    <div className="vm-settings">
      <section>
        <Head title="Auto-stop" meta={<span className="dim">{vm.timeoutSeconds ? `current: auto-${vm.timeoutPolicy ?? "kill"} after ${fmtDuration(vm.timeoutSeconds)}` : "current: not set"}</span>} />
        <form className="vm-form" onSubmit={(e) => { e.preventDefault(); if (valid) save(); }}>
          <label>Timeout (seconds)<input className="vm-input" type="number" min={1} step={1} value={secs} onChange={(e) => setSecs(e.target.value)} placeholder="e.g. 3600" /></label>
          <label>When it expires
            <select className="vm-input" value={policy} onChange={(e) => setPolicy(e.target.value as "kill" | "pause")}><option value="kill">Kill the VM</option><option value="pause">Pause the VM</option></select>
          </label>
          <button className="btn" disabled={!valid || busy}>Save</button>
        </form>
      </section>
      <section>
        <Head title="Network policy" />
        <NetworkView vm={vm} />
      </section>
      <section>
        <Head title="Raw VM" />
        <details className="vm-raw"><summary>Show JSON</summary><pre>{JSON.stringify(vm.raw, null, 2)}</pre></details>
      </section>
    </div>
  );
}
