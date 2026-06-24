# Cloudflare Pages 部署说明

Cloudflare Pages 适合已有 Cloudflare 账号和域名托管的情况。它提供免费 HTTPS 和全球 CDN，但不同套餐对单文件大小、构建上传和流量策略可能变化，安装包较大时需要先部署验证。

## 操作步骤

1. 登录 Cloudflare。
2. 进入 `Workers & Pages > Create application > Pages`。
3. 选择连接 GitHub 仓库。
4. 选择包含本项目的仓库。
5. 构建设置填写：

```text
Framework preset: None
Build command: 留空
Build output directory: release_site
Root directory: 留空或项目根目录
```

6. 点击部署。
7. 部署成功后会得到 `*.pages.dev` 地址。

## 自定义域名

1. 在 Pages 项目中选择 `Custom domains`。
2. 填写域名，例如 `download.example.com`。
3. 按 Cloudflare 提示添加 DNS 或把域名迁入 Cloudflare。
4. 等待 HTTPS 生效。

## 验证

```bat
python release_site\scripts\verify_public_update.py https://你的项目.pages.dev/ --download
```

成功后，把客户端更新地址改成：

```text
https://你的项目.pages.dev/releases/latest/update.json
```

## 注意

如果安装包下载 404、403 或上传失败，优先改用 GitHub Pages、Nginx 或对象存储作为下载源，并在 `prepare_release.py` 中使用正式下载域名重新生成 update.json。
