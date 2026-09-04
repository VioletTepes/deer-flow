"use client";

import {
  DownloadIcon,
  FileIcon,
  FolderIcon,
  PlusIcon,
  Trash2Icon,
  UploadIcon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  WorkspaceBody,
  WorkspaceContainer,
  WorkspaceHeader,
} from "@/components/workspace/workspace-container";
import { useI18n } from "@/core/i18n/hooks";
import {
  downloadProjectFile,
  useCreateProject,
  useDeleteProject,
  useDeleteProjectFile,
  useProjectFiles,
  useProjects,
  useUploadProjectFile,
} from "@/core/projects";
import { cn } from "@/lib/utils";

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export default function ProjectsPage() {
  const { t } = useI18n();
  const labels = t.projects;
  const projects = useProjects();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [name, setName] = useState("");
  const uploadRef = useRef<HTMLInputElement>(null);
  const createProject = useCreateProject();
  const deleteProject = useDeleteProject();
  const files = useProjectFiles(selectedId);
  const uploadFile = useUploadProjectFile(selectedId);
  const deleteFile = useDeleteProjectFile(selectedId);

  useEffect(() => {
    if (!selectedId && projects.data?.[0]) setSelectedId(projects.data[0].project_id);
    if (selectedId && projects.data && !projects.data.some((item) => item.project_id === selectedId)) {
      setSelectedId(projects.data[0]?.project_id ?? null);
    }
  }, [projects.data, selectedId]);

  const selected = projects.data?.find((item) => item.project_id === selectedId);

  const submitProject = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    try {
      const created = await createProject.mutateAsync(trimmed);
      setSelectedId(created.project_id);
      setName("");
      setCreateOpen(false);
      toast.success(labels.created);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <WorkspaceContainer>
      <WorkspaceHeader />
      <WorkspaceBody className="items-stretch">
        <div className="grid h-full min-h-0 w-full grid-cols-[minmax(12rem,17rem)_minmax(0,1fr)] max-sm:grid-cols-1">
          <aside className="flex min-h-0 flex-col border-r max-sm:max-h-48 max-sm:border-r-0 max-sm:border-b">
            <div className="flex h-14 shrink-0 items-center justify-between border-b px-4">
              <h1 className="text-sm font-semibold">{labels.title}</h1>
              <Button size="icon" variant="ghost" onClick={() => setCreateOpen(true)} title={labels.newProject}>
                <PlusIcon />
              </Button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              {projects.data?.map((project) => (
                <button
                  type="button"
                  key={project.project_id}
                  onClick={() => setSelectedId(project.project_id)}
                  className={cn(
                    "flex h-10 w-full items-center gap-2 rounded-md px-3 text-left text-sm transition-colors",
                    selectedId === project.project_id ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-muted",
                  )}
                >
                  <FolderIcon className="size-4 shrink-0" />
                  <span className="truncate">{project.name}</span>
                </button>
              ))}
              {!projects.isLoading && projects.data?.length === 0 && (
                <p className="text-muted-foreground px-3 py-6 text-center text-sm">{labels.emptyProjects}</p>
              )}
            </div>
          </aside>

          <section className="flex min-h-0 flex-col">
            <div className="flex h-14 shrink-0 items-center justify-between gap-3 border-b px-4 sm:px-6">
              <h2 className="min-w-0 truncate text-sm font-medium">{selected?.name ?? labels.selectProject}</h2>
              {selected && (
                <div className="flex shrink-0 items-center gap-1">
                  <input
                    ref={uploadRef}
                    type="file"
                    className="hidden"
                    onChange={async (event) => {
                      const file = event.target.files?.[0];
                      event.target.value = "";
                      if (!file) return;
                      try {
                        await uploadFile.mutateAsync(file);
                        toast.success(labels.uploaded);
                      } catch (error) {
                        toast.error(error instanceof Error ? error.message : String(error));
                      }
                    }}
                  />
                  <Button size="sm" onClick={() => uploadRef.current?.click()} disabled={uploadFile.isPending}>
                    <UploadIcon />
                    {labels.upload}
                  </Button>
                  <Button size="icon" variant="ghost" title={labels.deleteProject} onClick={() => setDeleteOpen(true)}>
                    <Trash2Icon />
                  </Button>
                </div>
              )}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-2 sm:px-6">
              {files.data?.map((file) => (
                <div key={file.file_id} className="group flex min-h-14 items-center gap-3 border-b py-2">
                  <FileIcon className="text-muted-foreground size-4 shrink-0" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">{file.display_name}</div>
                    <div className="text-muted-foreground text-xs">{formatBytes(file.size_bytes)}</div>
                  </div>
                  <Button size="icon" variant="ghost" title={labels.downloadFile} onClick={() => void downloadProjectFile(file.project_id, file)}>
                    <DownloadIcon />
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    title={labels.deleteFile}
                    onClick={async () => {
                      try {
                        await deleteFile.mutateAsync(file.file_id);
                        toast.success(labels.deleted);
                      } catch (error) {
                        toast.error(error instanceof Error ? error.message : String(error));
                      }
                    }}
                  >
                    <Trash2Icon />
                  </Button>
                </div>
              ))}
              {selected && !files.isLoading && files.data?.length === 0 && (
                <div className="text-muted-foreground flex h-48 flex-col items-center justify-center gap-3 text-sm">
                  <FileIcon className="size-6" />
                  {labels.emptyFiles}
                </div>
              )}
            </div>
          </section>
        </div>
      </WorkspaceBody>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>{labels.newProject}</DialogTitle></DialogHeader>
          <Input value={name} onChange={(event) => setName(event.target.value)} placeholder={labels.projectName} onKeyDown={(event) => { if (event.key === "Enter") void submitProject(); }} />
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>{labels.cancel}</Button>
            <Button onClick={() => void submitProject()} disabled={!name.trim() || createProject.isPending}>{labels.create}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{labels.deleteProject}</DialogTitle>
            <DialogDescription>{labels.deleteProjectConfirm}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)}>{labels.cancel}</Button>
            <Button
              variant="destructive"
              onClick={async () => {
                if (!selectedId) return;
                try {
                  await deleteProject.mutateAsync(selectedId);
                  setDeleteOpen(false);
                  toast.success(labels.deleted);
                } catch (error) {
                  toast.error(error instanceof Error ? error.message : String(error));
                }
              }}
            >
              <Trash2Icon />
              {labels.deleteProject}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </WorkspaceContainer>
  );
}
