# 永久项目文件与用户隔离

> 定制分支：`codex/persistent-project-files`
> 记录日期：2026-09-05

## 1. 修改目的

上游 DeerFlow 的工作区文件以会话为生命周期，位于 `/mnt/user-data/workspace` 或 `/mnt/user-data/outputs`。删除会话时，这些文件可以随会话目录一起清理，新会话也不能直接访问旧会话工作区。

本项目新增独立的永久项目文件能力，目标只有两个：

1. 用户明确保存到项目的文件不随会话清理，重启 DeerFlow 后仍然存在。
2. 项目和文件按当前登录用户隔离，用户不能查看、读取、修改或删除其他用户的数据。

该能力属于 DeerFlow 自身，不依赖自然语言查询业务，不新增 MCP，也不需要修改专病库 API 或其他业务系统。

## 2. 两类文件的边界

| 类型 | 位置 | 生命周期 | Agent 使用方式 |
| --- | --- | --- | --- |
| 会话工作区文件 | `/mnt/user-data/workspace`、`/mnt/user-data/outputs` | 跟随当前会话 | 直接使用沙箱文件工具 |
| 永久项目文件 | `DEER_FLOW_HOME/users/{user_id}/projects/...` | 独立于会话 | 通过项目文件工具列出、挂载或保存 |

永久目录不会作为共享可写目录直接暴露给 Agent。Agent 分析永久文件前，必须把指定文件复制到当前会话沙箱；Agent 生成的文件只有在用户明确要求保存时，才会从当前会话复制到永久项目。

## 3. 存储模型

### 3.1 元数据

数据库迁移 `0019_projects_files` 新增两张表：

- `projects`
  - `project_id`：项目 ID。
  - `user_id`：项目所有者。
  - `name`：项目名称，同一用户内唯一。
- `project_files`
  - `file_id`：文件 ID。
  - `project_id`、`user_id`：所属项目和用户。
  - `display_name`：用户看到的文件名，同一项目内唯一。
  - `relative_path`：永久内容相对路径。
  - `size_bytes`、`sha256`、`media_type`：内容元数据。
  - `source_type`、`source_thread_id`：文件来源。
  - `version`、`status` 和创建、更新时间。

SQL 和内存存储分别由 `ProjectRepository` 与 `MemoryProjectRepository` 实现。所有仓储读取和写入都携带 `user_id` 过滤条件。内存实现只用于开发或测试，进程退出后元数据会丢失；需要满足永久保存目标时必须使用 SQLite 或 PostgreSQL，当前部署使用 SQLite。

### 3.2 文件内容

永久文件内容位于会话目录之外：

```text
DEER_FLOW_HOME/
  users/
    {user_id}/
      projects/
        {project_id}/
          files/
            {uuid}/
              {display_name}
```

`Paths.project_file_path()` 会验证用户 ID、项目 ID 和最终路径，阻止 `..` 等路径穿越。随机 UUID 目录用于避免不同文件内容在物理路径上互相覆盖。

删除会话只删除该用户的 thread 目录，不影响上述 project 目录。删除永久文件会同时删除数据库记录和文件内容；删除项目会删除项目元数据、关联文件元数据和该项目的永久目录。

## 4. 用户隔离

隔离同时作用在 API、数据库和文件系统三层：

- API 从认证上下文取得当前 `user_id`，不接受客户端自行指定文件所有者。
- Repository 的项目和文件操作均按当前用户过滤。
- 永久内容写入 `users/{user_id}/projects/{project_id}`，不同用户拥有不同根目录。
- attach/import 接口还会校验目标 thread 的所有权。
- Agent 工具从运行时上下文解析当前用户，不接受用户提供宿主机路径。
- PAT 增加 `projects:read`、`projects:write`、`projects:delete` 权限范围，项目接口仍按权限和所有权共同校验。

只知道其他用户的 `project_id` 或 `file_id` 不足以访问文件；不属于当前用户的数据按不存在处理。

## 5. Agent 与沙箱交互

新增三个 Lead Agent 工具：

- `list_project_files`
  - 列出当前用户的所有项目或指定项目中的永久文件。
  - 返回项目名、文件名、大小、状态和内容是否存在。
  - 不向 Agent 返回可直接访问的宿主机永久路径。
- `attach_project_file`
  - 将一个永久文件复制到当前会话沙箱。
  - 返回当前沙箱可使用的 `/mnt/user-data/workspace/project/{project_id}/{file_name}` 路径。
  - 永久副本保持不变。
- `save_project_file`
  - 将当前会话 `/mnt/user-data/workspace` 或 `/mnt/user-data/outputs` 下的文件保存到指定永久项目。
  - 不接受其他路径，不允许跨用户保存。

Lead Agent prompt 按语义区分两类文件，不依赖固定句式。例如“我的项目文件”“之前保存的文件”“跨会话保留的文件”应调用永久项目文件工具；“当前工作区”“本次会话的文件”应使用普通沙箱文件工具。含义不明确时，Agent 应说明两类范围或向用户确认。

### 5.1 同名文件

当前策略是不覆盖：同一项目内已有相同 `display_name` 时，旧文件保持不变。

`save_project_file` 会在写入前检查同名文件，同时捕获并发写入时的数据库唯一键冲突。冲突以结构化业务结果返回：

```json
{
  "success": false,
  "code": "project_file_name_conflict",
  "name": "bubble_sort.py",
  "message": "A file named 'bubble_sort.py' already exists ..."
}
```

Agent 可以据此向用户说明冲突并使用其他文件名保存。SQLite `IntegrityError` 不再直接暴露给用户，失败写入产生的文件内容会被清理。

项目文件功能不设置额外的 100 MiB 产品限制。具体部署仍受反向代理、底层沙箱传输和可用存储空间等基础设施约束。

## 6. 不同沙箱类型

永久存储始终由 Gateway 侧管理，沙箱只接触当前会话副本：

- Local 和使用 thread data mounts 的 AIO/Docker 沙箱：Gateway 在该用户当前 thread 的挂载目录与永久目录之间复制文件。
- 不使用 thread data mounts 的远程沙箱：Gateway 或 Agent 工具通过沙箱 `update_file`、`download_file` API 传输字节。
- K8s/Provisioner 部署：交互协议与远程 AIO 相同；`DEER_FLOW_HOME` 必须挂载持久卷，才能保证 Gateway Pod 重建后文件仍存在。多 Gateway 实例还必须共享同一持久存储和数据库。

无论使用哪种沙箱，永久项目根目录都不直接挂载为 Agent 可写目录。

要实现真正的跨服务重启永久保存，部署必须同时满足：

- 项目元数据使用持久化的 SQLite 或 PostgreSQL，而不是 memory backend。
- `DEER_FLOW_HOME` 位于宿主持久目录或持久卷中，不能使用容器临时可写层。
- 备份时同时备份项目数据库和 `DEER_FLOW_HOME/users/*/projects`，两者必须保持一致。

## 7. HTTP API 与前端

Gateway 新增 `/api/projects` 路由，支持：

- 创建、列出、读取、重命名和删除项目。
- 上传、列出、读取、下载和删除项目文件。
- 将永久文件 attach 到指定 thread。
- 将指定 thread 的工作区或输出文件保存到项目。

DeerFlow 前端新增 `/workspace/projects` 页面和“项目文件”导航入口，支持创建和删除项目，以及上传、下载和删除永久文件。Agent 保存或 attach 文件时复用相同的项目元数据和永久内容。

## 8. 主要改动文件

后端与存储：

- `backend/packages/harness/deerflow/config/paths.py`
- `backend/packages/harness/deerflow/persistence/migrations/versions/0019_projects_files.py`
- `backend/packages/harness/deerflow/persistence/projects/`
- `backend/packages/harness/deerflow/persistence/models/__init__.py`
- `backend/app/gateway/project_files.py`
- `backend/app/gateway/routers/projects.py`
- `backend/app/gateway/app.py`
- `backend/app/gateway/deps.py`
- `backend/app/gateway/authz.py`
- `backend/app/gateway/auth/pat.py`

Agent：

- `backend/packages/harness/deerflow/tools/builtins/project_files_tool.py`
- `backend/packages/harness/deerflow/tools/builtins/__init__.py`
- `backend/packages/harness/deerflow/tools/tools.py`
- `backend/packages/harness/deerflow/agents/lead_agent/prompt.py`

前端：

- `frontend/src/app/workspace/projects/page.tsx`
- `frontend/src/core/projects/`
- `frontend/src/components/workspace/workspace-container.tsx`
- `frontend/src/components/workspace/workspace-nav-chat-list.tsx`
- `frontend/src/core/i18n/locales/en-US.ts`
- `frontend/src/core/i18n/locales/zh-CN.ts`

测试：

- `backend/tests/test_project_persistence.py`
- `backend/tests/test_project_file_tools.py`
- 迁移 head 和持久化 bootstrap 相关测试。

## 9. 验证要点

每次升级上游 DeerFlow 或修改该能力后，至少验证：

1. 用户 A 无法通过项目 ID、文件 ID 或接口读取用户 B 的数据。
2. 删除 thread 后永久项目文件仍存在，新 thread 可以列出并 attach 该文件。
3. DeerFlow/Gateway 重启后，项目元数据和文件内容仍存在。
4. Local、Docker AIO 和实际部署使用的远程/K8s 沙箱均可完成 attach 和 save。
5. Agent 能区分“当前工作区文件”和“永久项目文件”的自然语言表达。
6. 同名保存不覆盖旧文件，也不向用户显示数据库异常。
7. 路径穿越、其他用户路径和非 workspace/outputs 来源均被拒绝。

当前核心自动化测试命令：

```bash
cd backend
uv run pytest tests/test_project_file_tools.py tests/test_project_persistence.py -q
uv run ruff check packages/harness/deerflow/tools/builtins/project_files_tool.py tests/test_project_file_tools.py
```
