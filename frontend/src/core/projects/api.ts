import { fetch } from "../api/fetcher";
import { getBackendBaseURL } from "../config";

export interface Project {
  project_id: string;
  user_id: string;
  name: string;
  created_at: string;
  updated_at: string;
}

export interface ProjectFile {
  file_id: string;
  project_id: string;
  display_name: string;
  media_type: string | null;
  size_bytes: number;
  sha256: string;
  source_type: string;
  source_thread_id: string | null;
  version: number;
  status: string;
  created_at: string;
  updated_at: string;
}

async function checked(response: Response): Promise<Response> {
  if (response.ok) return response;
  const body = (await response.json().catch(() => null)) as {
    detail?: unknown;
  } | null;
  throw new Error(
    typeof body?.detail === "string" ? body.detail : "Project request failed",
  );
}

export async function listProjects(): Promise<Project[]> {
  return (await checked(await fetch(`${getBackendBaseURL()}/api/projects`))).json();
}

export async function createProject(name: string): Promise<Project> {
  return (
    await checked(
      await fetch(`${getBackendBaseURL()}/api/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      }),
    )
  ).json();
}

export async function deleteProject(projectId: string): Promise<void> {
  await checked(
    await fetch(`${getBackendBaseURL()}/api/projects/${projectId}`, {
      method: "DELETE",
    }),
  );
}

export async function listProjectFiles(projectId: string): Promise<ProjectFile[]> {
  return (
    await checked(
      await fetch(`${getBackendBaseURL()}/api/projects/${projectId}/files`),
    )
  ).json();
}

export async function uploadProjectFile(projectId: string, file: File): Promise<ProjectFile> {
  const form = new FormData();
  form.append("file", file);
  return (
    await checked(
      await fetch(`${getBackendBaseURL()}/api/projects/${projectId}/files`, {
        method: "POST",
        body: form,
      }),
    )
  ).json();
}

export async function deleteProjectFile(projectId: string, fileId: string): Promise<void> {
  await checked(
    await fetch(`${getBackendBaseURL()}/api/projects/${projectId}/files/${fileId}`, {
      method: "DELETE",
    }),
  );
}

export async function downloadProjectFile(projectId: string, file: ProjectFile): Promise<void> {
  const response = await checked(
    await fetch(`${getBackendBaseURL()}/api/projects/${projectId}/files/${file.file_id}/content`),
  );
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = file.display_name;
  anchor.click();
  URL.revokeObjectURL(url);
}
