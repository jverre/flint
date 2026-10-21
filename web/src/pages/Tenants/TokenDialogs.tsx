import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { errMsg, tenantActions } from "../../api/hosts";
import type { Token } from "../../api/types";
import { Dialog, useToast } from "../../components/ui";

/** Name the token, create it, then show the secret once. */
export function CreateTokenDialog({ tenant, onClose }: { tenant: string; onClose: () => void }) {
  const qc = useQueryClient(), toast = useToast();
  const [name, setName] = useState(`${tenant}-token`);
  const [secret, setSecret] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  if (secret == null) {
    return (
      <Dialog open onClose={onClose} title={`Create a token for ${tenant}`} width={480}
        sub={`A service token pinned to ${tenant}. It cannot act on other tenants or manage tokens.`}
        actions={[{ label: "Cancel", kind: "ghost" }, { label: "Create token", kind: "pri", disabled: !name.trim(), onClick: async () => {
          try {
            const r = await tenantActions.createToken(tenant, name.trim());
            qc.invalidateQueries({ queryKey: ["tokens"] });
            setSecret(r.token); return false;
          } catch (e) { toast(errMsg(e), { kind: "err" }); return false; }
        } }]}>
        <label className="fld">Name
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)} spellCheck={false} />
        </label>
      </Dialog>
    );
  }
  return (
    <Dialog open onClose={onClose} title="Token created" width={560}
      sub="This is the only time the secret is shown. Copy it now."
      actions={[{ label: "I've saved it", kind: "pri", id: "saved" }]}>
      <div className="secret" data-testid="secret">{secret}</div>
      <div className="row" style={{ marginTop: 10 }}>
        <button className="btn" onClick={async () => {
          try { await navigator.clipboard.writeText(secret); setCopied(true); toast("Token copied"); }
          catch { toast("Couldn't copy. Select the token and copy it by hand.", { kind: "err" }); }
        }}>{copied ? "Copied" : "Copy"}</button>
      </div>
    </Dialog>
  );
}

export function RevokeDialog({ token, onClose }: { token: Token; onClose: () => void }) {
  const qc = useQueryClient(), toast = useToast();
  return (
    <Dialog open onClose={onClose} title={`Revoke ${token.name || token.id}?`} width={460}
      sub="Anything using this token stops working."
      actions={[{ label: "Cancel", kind: "ghost" }, { label: "Revoke", kind: "danger", onClick: async () => {
        try { await tenantActions.revokeToken(token.id); qc.invalidateQueries({ queryKey: ["tokens"] }); toast(`${token.name || token.id} revoked`); }
        catch (e) { toast(errMsg(e), { kind: "err" }); return false; }
      } }]} />
  );
}

export function QuotaDialog({ tenant, current, used, onClose }: { tenant: string; current: number; used: number; onClose: () => void }) {
  const qc = useQueryClient(), toast = useToast();
  const [v, setV] = useState(String(current));
  const n = Number(v), valid = v.trim() !== "" && Number.isInteger(n) && n >= 0;
  return (
    <Dialog open onClose={onClose} title={`Max VMs for ${tenant}`} width={460}
      sub={`${used} VMs counted now. 0 means unlimited.`}
      actions={[{ label: "Cancel", kind: "ghost" }, { label: "Save quota", kind: "pri", disabled: !valid, onClick: async () => {
        try {
          await tenantActions.setQuota(tenant, n);
          qc.invalidateQueries({ queryKey: ["quota", tenant] });
          toast(n ? `${tenant} limited to ${n} VMs` : `${tenant} is unlimited`);
        } catch (e) { toast(errMsg(e), { kind: "err" }); return false; }
      } }]}>
      <label className="fld">Max VMs
        <input autoFocus inputMode="numeric" value={v} onChange={(e) => setV(e.target.value)} aria-invalid={!valid} />
      </label>
      {!valid && <p className="note errc">Enter a whole number, 0 or more.</p>}
    </Dialog>
  );
}
