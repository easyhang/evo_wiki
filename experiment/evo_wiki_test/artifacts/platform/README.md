# Evo wiki platform

只读 Web 知识平台产物：Wiki 静态站、SPA 和 nginx 配置。RAG reader 请求全部转发到可信查询网关 `http://127.0.0.1:8765`；Nginx 不再持有 LightRAG 地址或凭据。

当前是仅限本机开发的 local_single_user 模式，不得公网部署。

## 启动顺序

```bash
export EVO_WIKI_QUERY_AUDIT_KEY='由秘密管理系统提供的随机值'
evo-wiki gateway check --root <workspace>
evo-wiki gateway serve --root <workspace>
cd artifacts/platform
nginx -p . -c nginx.conf
```

打开 `http://localhost:8080`(`Wiki`)、`http://localhost:8080/app`(`问答/图谱`)。

## 上线：网关规则

nginx.conf 只代理可信查询网关的 reader 路径。LightRAG 应只绑定 loopback 或私有网络，浏览器和 Nginx 都不能直连它。trusted-proxy 模式要求 Basic Auth、OAuth 或等价的已验证身份来源。

## Docker 自封装(工具不生成镜像,开发者自理)

平台目录是标准静态站,Docker 封装只需:

```dockerfile
FROM nginx:1.27-alpine
COPY . /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/nginx.conf
EXPOSE 8080
```

```yaml
# docker-compose.yml
services:
  platform:
    build: ./artifacts/platform
    ports: ["8080:8080"]
    # gateway 必须在容器可访问的私有地址运行；不要把 LightRAG 凭据
    # 放入 platform 容器。
```

工具只对“平台目录 + nginx 配置的正确性”负责,不对“容器能否跑起来”负责。
