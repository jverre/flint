// Template-page data: one template (cluster adds host_classes) and import/delete actions.
import { useQuery, type QueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { Template } from "./types";

export function useTemplate(id: string | undefined, enabled = true) {
  return useQuery<Template>({
    queryKey: ["template", id], enabled: !!id && enabled, retry: false,
    queryFn: async () => (await api.get<{ template: Template }>(`/templates/${encodeURIComponent(id!)}`)).template,
  });
}

export interface ImportOpts { name: string; image: string; rootfsSizeMb: number; tenant?: string; auth?: { username: string; password: string } }
export const importTemplate = (o: ImportOpts) =>
  api.post<{ template_id: string; status: string; build_host?: string }>("/templates", {
    body: { name: o.name, image: o.image, rootfs_size_mb: o.rootfsSizeMb, ...(o.tenant ? { tenant: o.tenant } : {}), ...(o.auth ? { registry_auth: o.auth } : {}) },
  });
export const deleteTemplate = (id: string) => api.del(`/templates/${encodeURIComponent(id)}`);
export const refreshTemplates = (qc: QueryClient) => { qc.invalidateQueries({ queryKey: ["templates"] }); qc.invalidateQueries({ queryKey: ["template"] }); };
