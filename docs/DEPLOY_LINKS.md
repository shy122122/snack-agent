# 项目链接配置

这几条链接可以放到简历或作品集里。

## 当前可用链接

| 类型 | 链接 |
|---|---|
| GitHub 项目 | `https://github.com/shy122122/snack-agent` |
| 项目展示页（当前推荐） | `https://htmlpreview.github.io/?https://github.com/shy122122/snack-agent/blob/main/docs/index.html` |
| PRD 文档 | `https://github.com/shy122122/snack-agent/blob/main/docs/PRD.md` |
| 在线前端配置 | `https://github.com/shy122122/snack-agent/blob/main/render.yaml` |

> 不建议把 `cdn.jsdelivr.net/gh/.../index.html` 放到简历里。jsDelivr 更适合分发静态文件，打开 HTML 时可能显示源码，而不是渲染页面。

## 简历建议写法

```text
GitHub：https://github.com/shy122122/snack-agent
项目展示：https://htmlpreview.github.io/?https://github.com/shy122122/snack-agent/blob/main/docs/index.html
PRD文档：https://github.com/shy122122/snack-agent/blob/main/docs/PRD.md
```

## 正式 GitHub Pages 链接

后续在仓库 `Settings -> Pages` 中将 Source 设置为 `GitHub Actions` 后，正式展示页会是：

```text
https://shy122122.github.io/snack-agent/
```

如果 GitHub Pages 没开启，上面这个正式链接会打不开；在此之前先使用 HTMLPreview 链接。

## Render 在线前端

项目已补充 `render.yaml` 和 `server.py`，适合部署到 Render。

部署入口：

```text
https://render.com/deploy?repo=https://github.com/shy122122/snack-agent
```

部署后入口通常为：

```text
https://snack-agent.onrender.com/console#overview
```

如果 Render 自动生成了不同域名，把简历里的「在线前端」链接替换为实际域名即可。
