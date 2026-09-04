# OIDC Token 时钟偏差配置

> 定制分支：`codex/persistent-project-files`
> 记录日期：2026-09-05

## 1. 修改目的

上游 DeerFlow 对 OIDC ID Token 的时间声明采用严格校验。当 DeerFlow 所在主机与 OIDC Provider 的系统时间存在很小偏差时，合法 Token 可能因 `iat` 略晚于本机时间，或 `exp` 刚刚越过本机时间而被拒绝。

本项目为 OIDC Provider 增加可配置的时钟偏差容忍值。当前部署在 `config.yaml` 中配置为 30 秒，用于容忍基础设施间的短暂时钟误差。

这项修改只影响 OIDC ID Token 的时间声明校验，不改变 Token 的签名、签发方、受众和 nonce 校验。

## 2. 实际行为

新增 Provider 配置项：

```yaml
authorization:
  oidc:
    providers:
      keycloak:
        clock_skew_seconds: 30
```

配置规则：

- 单位为秒。
- 默认值仍为 `0`，即未配置时保持上游的严格时间校验行为。
- 允许范围为 `0` 到 `300`。
- 当前项目部署值为 `30`。

回调处理会将该值传递给 PyJWT 的 `jwt.decode(..., leeway=clock_skew_seconds)`。它会作用于 ID Token 中受 PyJWT 校验的时间声明，包括 `exp`、`iat`，以及 Token 存在时的 `nbf`。

以 30 秒为例：

- Token 的 `iat` 最多可比 DeerFlow 当前时间晚 30 秒。
- Token 到达 `exp` 后最多保留 30 秒的时钟偏差容忍窗口。
- `nbf` 存在时，最多允许提前 30 秒通过时间校验。

## 3. 调用链

```text
config.yaml
  -> OIDCProviderConfig.clock_skew_seconds
  -> oauth_callback(...)
  -> OIDCService.authenticate_callback(...)
  -> OIDCService.validate_id_token(...)
  -> jwt.decode(..., leeway=clock_skew_seconds)
```

涉及文件：

- `backend/packages/harness/deerflow/config/auth_config.py`
  - 定义 `clock_skew_seconds`，默认值为 0，限制范围为 0–300。
- `backend/app/gateway/routers/auth.py`
  - 从当前 Provider 配置读取该值并传入 OIDC 回调认证服务。
- `backend/app/gateway/auth/oidc.py`
  - 在回调和 ID Token 校验方法间传递该值，并设置 PyJWT `leeway`。
- `config.example.yaml`
  - 提供配置说明和示例。
- `config.yaml`
  - 当前本地部署对 Keycloak 使用 30 秒；该文件属于部署配置，不提交到 Git。

## 4. 安全边界

30 秒仅用于处理系统时钟漂移，不是延长登录会话有效期，也不是刷新 Token。以下校验保持不变：

- 使用 JWKS 验证 Token 签名。
- 限制允许的签名算法。
- 校验 `iss` 与 OIDC discovery metadata 一致。
- 校验 `aud` 与当前 `client_id` 一致。
- 启用 nonce 时继续执行恒定时间 nonce 比较。
- `exp`、`iss`、`sub`、`aud` 仍是必需声明。

应优先保证 DeerFlow 主机和 OIDC Provider 都使用 NTP 同步。只有确有时钟误差时才配置容忍值，并保持尽可能小；当前值固定为 30 秒。

## 5. 验证要点

修改或升级 OIDC 代码后至少验证：

1. 未配置 `clock_skew_seconds` 时仍按 0 秒严格校验。
2. 配置 30 秒后，偏差不超过 30 秒的 `iat`、`exp` 或 `nbf` Token 能通过时间校验。
3. 偏差超过 30 秒的 Token 仍被拒绝。
4. 签名、issuer、audience 或 nonce 错误的 Token 不因 leeway 配置而通过。
5. `clock_skew_seconds` 小于 0 或大于 300 时配置加载失败。
