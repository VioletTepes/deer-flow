import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createProject,
  deleteProject,
  deleteProjectFile,
  listProjectFiles,
  listProjects,
  uploadProjectFile,
} from "./api";

export function useProjects() {
  return useQuery({ queryKey: ["projects"], queryFn: listProjects });
}

export function useProjectFiles(projectId: string | null) {
  return useQuery({
    queryKey: ["projects", projectId, "files"],
    queryFn: () => listProjectFiles(projectId!),
    enabled: Boolean(projectId),
  });
}

export function useCreateProject() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: createProject,
    onSuccess: () => client.invalidateQueries({ queryKey: ["projects"] }),
  });
}

export function useDeleteProject() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: deleteProject,
    onSuccess: () => client.invalidateQueries({ queryKey: ["projects"] }),
  });
}

export function useUploadProjectFile(projectId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => uploadProjectFile(projectId!, file),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["projects", projectId, "files"] }),
  });
}

export function useDeleteProjectFile(projectId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (fileId: string) => deleteProjectFile(projectId!, fileId),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["projects", projectId, "files"] }),
  });
}
