// Template-page data: one template (cluster adds host_classes) and build/delete actions.
import { useQuery, type QueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { Template } from "./types";

export function useTemplate(id: string | undefined, enabled = true) {
  return useQuery<Template>({
    queryKey: ["template", id], enabled: !!id && enabled, retry: false,
    queryFn: async () => (await api.get<{ template: Template }>(`/templates/${encodeURIComponent(id!)}`)).template,
  });
}

export interface BuildOpts { name: string; dockerfile: string; rootfsSizeMb: number; tenant?: string }
export const buildTemplate = (o: BuildOpts) =>
  api.post<{ template_id: string; status: string; build_host?: string }>("/templates/build", {
    body: { name: o.name, dockerfile: o.dockerfile, rootfs_size_mb: o.rootfsSizeMb, ...(o.tenant ? { tenant: o.tenant } : {}) },
  });
export const deleteTemplate = (id: string) => api.del(`/templates/${encodeURIComponent(id)}`);
export const refreshTemplates = (qc: QueryClient) => { qc.invalidateQueries({ queryKey: ["templates"] }); qc.invalidateQueries({ queryKey: ["template"] }); };
