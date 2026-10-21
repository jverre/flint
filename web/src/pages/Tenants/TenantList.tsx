import { useNavigate } from "react-router-dom";
import { useQuota } from "../../api/hooks";
import type { Token, VM } from "../../api/types";
import { Empty, Kpis, SkeletonRows } from "../../components/ui";
import { tenantVar } from "../../kit/svg";

export const QuotaCell = ({ tenant, used }: { tenant: string; used: number }) => {
  const q = useQuota(tenant);
  if (q.isLoading) return <span className="sk" style={{ width: 60 }} />;
  if (!q.data) return <span className="dim">—</span>;
  const max = q.data.max_vms;
  return (
    <div className="mc">
      {max > 0 && <div className="meter"><i style={{ width: `${Math.min(100, (q.data.vms / max) * 100)}%`, background: q.data.vms >= max ? "var(--err)" : undefined }} /></div>}
      {max > 0 ? <>{q.data.vms}<span className="dim">/ {max}</span></> : <span className="dim">{used} · unlimited</span>}
    </div>
  );
};

export function TenantList({ tenants, vms, tokens, loading, admin }: { tenants: string[]; vms: VM[]; tokens: Token[]; loading: boolean; admin: boolean }) {
  const nav = useNavigate();
  const of = (t: string) => vms.filter((v) => v.tenant === t);
  return (
    <div className="page tenants">
      <header className="page-head">
        <div>
          <h1>Tenants</h1>
          <div className="facts"><b>{tenants.length} {tenants.length === 1 ? "tenant" : "tenants"}</b> · {vms.length} VMs</div>
        </div>
        <Kpis items={[
          { label: "Tenants", value: tenants.length },
          { label: "VMs", value: vms.length },
          { label: "Running", value: vms.filter((v) => v.state === "running").length, col: "var(--spark)" },
          { label: "Paused", value: vms.filter((v) => v.state === "paused").length },
        ]} />
      </header>
      <div className="page-body full">
        <div className="main-col scroll">
          <table className="tbl" aria-label="Tenants">
            <thead><tr><th scope="col">Tenant</th><th scope="col" className="num">VMs</th><th scope="col" className="num">Running</th><th scope="col" className="num">Paused</th><th scope="col">Quota</th>{admin && <th scope="col" className="num">Pinned tokens</th>}</tr></thead>
            <tbody>
              {loading && <SkeletonRows rows={4} cols={admin ? 6 : 5} />}
              {tenants.map((t) => {
                const v = of(t);
                return (
                  <tr key={t} onClick={() => nav(`/tenants/${t}`)}>
                    <td><div className="nm"><span className="sw" style={{ background: `var(${tenantVar(t)})` }} /><button className="nmbtn" onClick={() => nav(`/tenants/${t}`)}>{t}</button></div></td>
                    <td className="num">{v.length}</td>
                    <td className="num">{v.filter((x) => x.state === "running").length}</td>
                    <td className="num">{v.filter((x) => x.state === "paused").length}</td>
                    <td><QuotaCell tenant={t} used={v.length} /></td>
                    {admin && <td className="num">{tokens.filter((k) => k.tenants.includes(t)).length}</td>}
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!loading && !tenants.length && <Empty title="No tenants yet">A tenant appears when it has a VM or a pinned token. Create one with <code>flint create --tenant NAME</code>.</Empty>}
        </div>
      </div>
    </div>
  );
}
