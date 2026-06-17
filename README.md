# codie-sidecar-build

构建 codie 的 **MCP sidecar 容器镜像**,并多架构推送到 **ghcr.io**(后续同步到阿里 ACR / 腾讯 TCR)。

与 [`codie-agent-build`](https://github.com/shanhaobo/codie-agent-build)(包装外部 agent fork)相对:**本仓库存放 codie 自有的 sidecar 源码**,不依赖任何 fork。

## 这个仓库构建什么

5 个 MCP sidecar(均为 codie 自有源码,自包含上下文):

| 镜像 | sidecar | 作用 |
|---|---|---|
| `codie-media-mcp` | media | 媒体(yt-dlp) |
| `codie-browser-mcp` | browser | Playwright 代理 |
| `codie-search-mcp` | search | web 搜索(需 `SEARCH_API_KEY`) |
| `codie-memory-mcp` | memory | FTS5 跨 agent 记忆 |
| `codie-home-mcp` | home | Home Assistant 代理(需 `HA_TOKEN`) |

> `codie_host` sidecar 是 **PyInstaller 二进制**、随 Bridge 打包,**不是容器镜像**,不在此流水线。

### 第三方现成镜像(mirror,非 codie 源码)

有些 MCP sidecar 是**外部现成镜像**(非 codie 自有源码,自己 build 是重复造轮子)。
这类只做 **registry 间镜像搬运**——把上游镜像多架构整体复制到 ghcr,**不 build**——
让 Docker 守护进程连不上 Docker Hub 的机器(如 CN 网络:`registry-1.docker.io` 超时但
`ghcr.io` 可达)也能从 ghcr 拉到。走独立的 `mirror-thirdparty-images.yml`,与上面
build 自有源码的流水线正交。

| ghcr 镜像 | 上游 | sidecar | 运行参数(Bridge 自定义 sidecar 表单) |
|---|---|---|---|
| `firecrawl-mcp` | `mcp/firecrawl:latest`(Docker Hub) | firecrawl | port `3000` · path `/mcp` · env `HTTP_STREAMABLE_SERVER=true` + `FIRECRAWL_API_KEY`(密钥) |

加一个第三方镜像:在 `mirror-thirdparty-images.yml` 的 matrix 里加一行 `{ src, dest }`。

## 目录

```
docker-registry/
  scripts/                build-<x>-mcp-docker.sh + _lib.sh(与 codie-agent-build 同源的一份拷贝)
  sidecars/<name>/        sidecar 自包含上下文(Dockerfile + server.py + pyproject)
.github/workflows/
  build-sidecar-images.yml  多架构构建 → ghcr.io
```

## CI 怎么跑(GitHub Actions)

`build-sidecar-images.yml`,**每架构在各自原生 runner 上构建,无 QEMU**(amd64 → `ubuntu-latest`,arm64 → `ubuntu-24.04-arm`)。两段式:

**Stage 1 `build`**(矩阵 5 sidecar × 2 架构 = 10 job):checkout 本仓库 → setup Buildx → 登录 ghcr → 跑 `build-<x>-mcp-docker.sh`(`BUILD_MODE=digest`,单架构,按 digest 推送,`--metadata-file` 取 digest → 上传 artifact)。

**Stage 2 `merge`**(矩阵 5 sidecar,`needs: build`):下载该镜像两个架构的 digest → `docker buildx imagetools create` 合并多架构 manifest,打 `:latest` + `:YYYYMMDD-HHMM-<sha>`。

**触发**:`workflow_dispatch`(手动)或打 `sidecar-v*` tag。

> 依赖全是公开 PyPI 包;无硬编码密钥(token/key 全走环境变量)。

## 加 ACR / TCR(以后)

同 codie-agent-build:加 ACR/TCR 的 `docker/login-action` + secrets,merge 步骤用 `imagetools create` 多 `-t` 同时打到多个 registry。
