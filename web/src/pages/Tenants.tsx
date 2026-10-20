import { useParams } from "react-router-dom";
import { tenantsOf, useIsAdmin, usePlatform, useTokens, useVms } from "../api/hooks";
import { Empty } from "../components/ui";
import { TenantDetail } from "./Tenants/TenantDetail";
import { TenantList } from "./Tenants/TenantList";
import "./tenants.css";

export default function TenantsPage() {
  const plat = usePlatform().data, admin = useIsAdmin();
  const { tenant } = useParams();
  const vms = useVms(), tokens = useTokens(admin);
  if (!plat) return <div className="page"><h1>Tenants</h1></div>;
  if (plat.mode !== "cluster") return <div className="page"><h1>Tenants</h1><Empty title="Tenants only exist in a Flint cluster">This is a single daemon, so every VM belongs to the same workspace.</Empty></div>;
  if (vms.isError) return <div className="page"><h1>Tenants</h1><Empty title="Couldn't load VMs" action={<button className="btn" onClick={() => vms.refetch()}>Retry</button>}>{(vms.error as Error).message}</Empty></div>;
  const all = vms.data ?? [], tk = tokens.data ?? [];
  if (tenant) return <TenantDetail tenant={tenant} vms={all} tokens={tk} admin={admin} loading={vms.isLoading} />;
  return <TenantList tenants={tenantsOf(all, tk)} vms={all} tokens={tk} loading={vms.isLoading} admin={admin} />;
}
