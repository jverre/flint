import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useEvents, useQuota } from "../../api/hooks";
import type { FlintEvent, Token, VM } from "../../api/types";
import { Empty, Feed, Kpis, Sec, SkeletonRows, StateChip, type FeedItem } from "../../components/ui";
import { fmtClock, fmtSince } from "../../kit/format";
import { tenantVar } from "../../kit/svg";
import { CreateTokenDialog, QuotaDialog, RevokeDialog } from "./TokenDialogs";

const COLOR: Record<string, string> = { "vm.created": "var(--spark)", "vm.deleted": "var(--dim)", "vm.paused": "var(--muted)", "vm.resumed": "var(--ok)", "vm.moved": "var(--spark)", "vm.lost": "var(--err)" };

function activity(events: (FlintEvent & { _k: string })[], tenant: string, vms: VM[]): FeedItem[] {
  const name = (e: FlintEvent) => e.vm_id ? (vms.find((v) => v.id === e.vm_id)?.name ?? (e.vm?.labels?.name || e.vm_id.slice(0, 8))) : "";
  return events.filter((e) => e.tenant === tenant).slice(0, 30).map((e, i) => {
    let what = e.type.replace(/^vm\./, "").replace(/_/g, " ");
    if (e.type === "vm.state_changed" && e.to) what = `now ${String(e.to)}`;
    if (e.type === "vm.moved") what = `moved ${String(e.from_host ?? "?")} → ${String(e.to_host ?? "?")}`;
    return { key: e._k, time: fmtClock(e.ts), color: e.type === "vm.state_changed" && e.to === "error" ? "var(--err)" : COLOR[e.type] ?? "var(--dim)", body: <><b>{name(e)}</b> {what}</>, fresh: i === 0 };
  });
}

export function TenantDetail({ tenant, vms, tokens, admin, loading }: { tenant: string; vms: VM[]; tokens: Token[]; admin: boolean; loading: boolean }) {
  const nav = useNavigate();
  const quota = useQuota(tenant);
  const events = useEvents().events;
  const [dlg, setDlg] = useState<"quota" | "create" | Token | null>(null);
  const mine = vms.filter((v) => v.tenant === tenant);
  const running = mine.filter((v) => v.state === "running").length, paused = mine.filter((v) => v.state === "paused").length;
  const pinned = tokens.filter((t) => t.tenants.includes(tenant));
  const open = tokens.filter((t) => t.tenants.length === 0).length;
  const max = quota.data?.max_vms;
  const feed = useMemo(() => activity(events, tenant, vms), [events, tenant, vms]);

  return (
    <div className="page tenants">
      <header className="page-head">
        <div>
          <nav className="crumb"><Link to="/tenants">Tenants</Link> / {tenant}</nav>
          <h1><span className="sw big" style={{ background: `var(${tenantVar(tenant)})` }} />{tenant}</h1>
          <div className="facts"><b>{mine.length} {mine.length === 1 ? "VM" : "VMs"}</b>{admin && <> · {pinned.length} pinned {pinned.length === 1 ? "token" : "tokens"}</>}{max != null && <> · {max > 0 ? `quota ${max} VMs` : "no VM quota"}</>}</div>
        </div>
        <Kpis items={[
          { label: "VMs", value: mine.length, unit: max == null ? "" : max > 0 ? `/ ${max}` : "/ unlimited" },
          { label: "Running", value: running, col: "var(--spark)" },
          { label: "Paused", value: paused },
        ]} />
      </header>

      <div className="page-body">
        <div className="main-col" style={{ gap: 18 }}>
          <section className="vmsec">
            <Sec l="1" title="VMs" meta={`${running} running of ${mine.length}`} />
            <div className="scroll">
              <table className="tbl" aria-label={`VMs of ${tenant}`}>
                <thead><tr><th scope="col">VM</th><th scope="col">State</th><th scope="col">Host</th><th scope="col">Template</th><th scope="col" className="num">Age</th></tr></thead>
                <tbody>
                  {loading && <SkeletonRows rows={4} cols={5} />}
                  {mine.map((v) => (
                    <tr key={v.id} onClick={() => nav(`/vms/${v.id}`)}>
                      <td><Link to={`/vms/${v.id}`} onClick={(e) => e.stopPropagation()}><b className="vmn">{v.name}</b></Link></td>
                      <td><StateChip state={v.state} /></td>
                      <td className="mut">{v.host || "—"}</td>
                      <td className="mut">{v.template}</td>
                      <td className="num mut">{fmtSince(v.createdAt)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!loading && !mine.length && <Empty title="No VMs in this tenant">Create one with <code>flint create --tenant {tenant}</code>.</Empty>}
            </div>
          </section>

          <section>
            <Sec l="2" title="Quota" meta={admin && <button className="btn sm" onClick={() => setDlg("quota")}>Edit…</button>} />
            {quota.isError ? <p className="mut">Couldn't load the quota. <button className="btn sm" onClick={() => quota.refetch()}>Retry</button></p> : (
              <dl className="kv">
                <dt>Max VMs</dt><dd>{max == null ? "…" : max > 0 ? max : "unlimited"}</dd>
                <dt>VMs counted</dt><dd>{quota.data?.vms ?? "…"}{max ? <span className="dim"> / {max}</span> : null}</dd>
              </dl>
            )}
            {max != null && max > 0 && <div className="meter qm"><i style={{ width: `${Math.min(100, ((quota.data?.vms ?? 0) / max) * 100)}%`, background: (quota.data?.vms ?? 0) >= max ? "var(--err)" : undefined }} /></div>}
          </section>
        </div>

        <aside className="side" aria-label="Access and activity">
          {admin && (
            <section>
              <Sec l="A" title="Access" meta={<button className="btn sm" onClick={() => setDlg("create")}>Create token</button>} />
              {pinned.map((t) => (
                <div className="ri" key={t.id}>
                  <span><b>{t.name || t.id}</b> <span className="d11">{t.tenants.length > 1 ? `${t.tenants.length} tenants` : "this tenant"}</span></span>
                  <small>created {fmtSince(t.created_at)} ago</small>
                  <button className="btn sm danger" onClick={() => setDlg(t)}>Revoke</button>
                </div>
              ))}
              {!pinned.length && <p className="note">No token is pinned to {tenant}.</p>}
              {open > 0 && <p className="note">{open} {open === 1 ? "token is" : "tokens are"} not pinned and can act on any tenant.</p>}
            </section>
          )}
          <section className="grow-sec">
            <Sec l={admin ? "B" : "A"} title="Activity" />
            <div className="feedbox">{feed.length ? <Feed items={feed} max={16} /> : <p className="note">No events for {tenant} since this page opened.</p>}</div>
          </section>
        </aside>
      </div>

      {dlg === "quota" && <QuotaDialog tenant={tenant} current={max ?? 0} used={quota.data?.vms ?? mine.length} onClose={() => setDlg(null)} />}
      {dlg === "create" && <CreateTokenDialog tenant={tenant} onClose={() => setDlg(null)} />}
      {dlg && typeof dlg === "object" && <RevokeDialog token={dlg} onClose={() => setDlg(null)} />}
    </div>
  );
}
